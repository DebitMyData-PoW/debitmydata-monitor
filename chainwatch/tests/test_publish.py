import csv
import io
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg

from chainwatch import price, publish
from chainwatch.config import Settings
from chainwatch.sync import sync

from .conftest import PERIODS, FakeNode, utc


def test_the_stats_files(
    conn: psycopg.Connection[Any], node: FakeNode, settings: Settings, tmp_path: Path
) -> None:
    node.add(576 * 3)  # three days
    node.add(10, tag="SoloMiner", payouts={"dm1qsolo": Decimal(35)})
    now = node.blocks[-1]["time"] + 30
    sync(conn, node, PERIODS, "Official", utc(now))
    price.record(
        conn,
        utc(now - 2 * 86400),
        Decimal("0.001"),
        {
            "mining_cost_usd_per_dmd": "0.00000004",
            "minimum_usd_per_dmd": "0.001",
            "price_from": "minimum",
        },
        "gateway-import",
    )
    price.record(
        conn,
        utc(now - 600),
        Decimal("0.001"),
        {
            "mining_cost_usd_per_dmd": "0.00000004",
            "minimum_usd_per_dmd": "0.001",
            "price_from": "minimum",
        },
        "gateway",
    )

    chain = publish.publish(conn, tmp_path, utc(now), True)
    api = tmp_path / "api"
    summary = json.loads((api / "summary.json").read_text())
    assert summary["height"] == 576 * 3 + 9
    assert summary["blocks_24h"] == 576
    assert summary["block_seconds_24h"] == 150.0
    # 1000 × 2^32 hashes every 150 seconds
    assert abs(summary["hashrate_24h"] - 1000 * 2**32 / 150) / summary["hashrate_24h"] < 0.01
    assert summary["reference_price"]["usd_per_dmd"] == 0.001
    assert summary["reference_price"]["price_from"] == "minimum"
    assert summary["pool_share_24h"] == round(566 / 576, 4)
    assert summary["block_reward_dmd"] == 35
    assert summary["next_halving_height"] == 300_000
    assert summary["status"] == {"normal": True, "notes": []}

    day = json.loads((api / "chart-24h.json").read_text())
    assert day["fields"][:3] == ["time", "price", "mining_cost"]
    assert len(day["points"]) in (144, 145)
    assert day["points"][-1][0] == now
    everything = json.loads((api / "chart-all.json").read_text())
    first = everything["points"][0]
    assert first[1] is None  # no gateway price yet back then
    assert everything["points"][-1][1] == 0.001

    miners = json.loads((api / "miners.json").read_text())
    assert miners["24h"]["miners"][0] == {
        "name": "the DebitMyData pool",
        "blocks": 566,
        "share": round(566 / 576, 4),
        "pool": True,
    }
    assert miners["24h"]["miners"][1]["name"] == "SoloMiner"

    daily = list(csv.DictReader(io.StringIO((api / "dmd-daily.csv").read_text())))
    assert len(daily) >= 2 and daily[0]["blocks"] and daily[0]["reference_price_usd_close"] == ""
    blocks = (api / "dmd-blocks.csv").read_text().splitlines()
    assert len(blocks) == 576 * 3 + 10 + 1
    assert blocks[-1].split(",")[6:8] == ["0", "SoloMiner"]

    metrics_file = tmp_path / "chainwatch.prom"
    publish.metrics(conn, metrics_file, utc(now), {"peers": 8.0, "initial_download": 0.0}, chain)
    text = metrics_file.read_text()
    assert f"dmd_chain_height {576 * 3 + 9}" in text
    assert "dmd_reference_price_usd 0.001" in text
    assert "dmd_node_peers 8.0" in text
    assert 'chainwatch_alerts_open{severity="critical"} 0' in text


def test_price_import(conn: psycopg.Connection[Any], tmp_path: Path) -> None:
    export = tmp_path / "dmd-price-records.csv"
    export.write_text(
        "observed_at,usd_per_unit,inputs\n"
        '2026-10-27 18:05:00.123+00,0.001000000000000000,"{""price_from"": ""minimum"", '
        '""mining_cost_usd_per_dmd"": ""0.0000301"", ""minimum_usd_per_dmd"": ""0.001""}"\n'
        '2026-10-24 09:00:00+00,0.000031000000000000,"{""mining_cost_usd_per_dmd"": '
        '""0.000031""}"\n'
    )
    assert price.import_csv(conn, export) == (2, 0)
    assert price.import_csv(conn, export) == (0, 2)
    rows = conn.execute("SELECT * FROM prices ORDER BY observed_at").fetchall()
    assert [r["price_from"] for r in rows] == [None, "minimum"]
    assert rows[1]["mining_cost_usd"] == Decimal("0.0000301")
    assert rows[1]["source"] == "gateway-import"
