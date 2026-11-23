import csv
import io
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import pytest

from chainwatch import price, publish
from chainwatch.config import Settings
from chainwatch.sync import sync

from .conftest import PERIODS, POOL_ADDRESS, FakeNode, utc


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

    # the new figures: market cap, volume (all known: synced with what they spend), candles
    assert summary["market_cap_usd"] == pytest.approx(0.001 * (576 * 3 + 9) * 35)
    assert summary["volume_24h_dmd"] == 0 and summary["transactions_24h"] == 0
    assert summary["market"] == {
        "listed": False,
        "source": None,
        "trading_volume_24h_usd": None,
        "trading_volume_change_24h": None,
        "price_usd": None,
        "observed_at": None,
    }
    at = {f: i for i, f in enumerate(day["fields"])}
    last = day["points"][-1]
    assert last[at["market_cap"]] == summary["market_cap_usd"]
    assert last[at["trading_volume_24h"]] is None and last[at["volume"]] == 0
    assert set(day["candles"]) == {"price", "hashrate", "difficulty", "market_cap"}
    candle = day["candles"]["difficulty"][-1]
    assert candle[1:] == [1000.0, 1000.0, 1000.0, 1000.0]
    for _, o, h, low, c in day["candles"]["hashrate"]:
        assert low <= min(o, c) and h >= max(o, c)
    latest = json.loads((api / "blocks.json").read_text())["blocks"]
    assert len(latest) == 1000 and latest[0]["height"] == 576 * 3 + 9
    assert latest[0]["miner"] == "SoloMiner" and latest[0]["reward_dmd"] == 35
    assert latest[-1]["height"] == latest[0]["height"] - 999
    assert latest[10]["pool"] and latest[10]["miner"] == "the DebitMyData pool"
    assert latest[0]["time"] == now - 30 and latest[0]["hash"] == node.blocks[-1]["hash"]
    quarter = json.loads((api / "chart-15m.json").read_text())
    assert quarter["step"] == 30 and len(quarter["points"]) >= 30
    assert quarter["points"][-1][0] == now  # the last point is now: the chart's live dot
    richlist = json.loads((api / "richlist.json").read_text())
    assert richlist["ready"] and richlist["addresses"][0]["address"] == POOL_ADDRESS

    coin = json.loads((api / "coin.json").read_text())
    assert coin["ticker"] == "DMD" and coin["decimals"] == 8 and coin["algorithm"] == "SHA-256"
    assert coin["height"] == summary["height"] and coin["hashrate"] == summary["hashrate_24h"]
    assert coin["blocks_to_next_halving"] == 300_000 - summary["height"]
    assert coin["supply"]["circulating_dmd"] == summary["height"] * 35
    assert coin["supply"]["total_dmd"] == coin["supply"]["circulating_dmd"]
    assert coin["supply"]["max_dmd"] is None
    assert coin["supply"]["per_cycle_dmd"] == 20_999_999.964
    assert coin["price"] == {
        "listed": False,
        "market_usd": None,
        "reference_usd": 0.001,
        "reference_from": "minimum",
    }
    assert coin["market_cap_from"] == "reference_price"
    assert coin["market_cap_usd"] == round(summary["height"] * 35 * 0.001, 2)
    # the plain numbers CoinGecko and CoinMarketCap read: no exponent, no separator
    circulating = (api / "circulating-supply.txt").read_text()
    assert circulating == str(summary["height"] * 35)
    assert (api / "total-supply.txt").read_text() == circulating

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


def test_the_exchanges_figures(
    conn: psycopg.Connection[Any],
    node: FakeNode,
    settings: Settings,
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    from dataclasses import replace

    from chainwatch import market

    node.add(600)
    now = node.blocks[-1]["time"] + 30
    sync(conn, node, PERIODS, "Official", utc(now))
    assert market.poll(conn, settings, utc(now)) is False  # off without a CoinGecko id
    on = replace(settings, coingecko_id="debitmydata")
    monkeypatch.setattr(
        market,
        "fetch",
        lambda s: {
            "debitmydata": {
                "usd": Decimal("0.0021"),
                "usd_24h_vol": Decimal("15234.5"),
                "last_updated_at": now - 60,
            }
        },
    )
    assert market.poll(conn, on, utc(now)) is True
    assert market.poll(conn, on, utc(now)) is False  # the same figure
    publish.publish(conn, tmp_path, utc(now), True, 1000, "CoinGecko")
    summary = json.loads((tmp_path / "api" / "summary.json").read_text())
    assert summary["market"]["listed"] and summary["market"]["source"] == "CoinGecko"
    assert summary["market"]["trading_volume_24h_usd"] == 15234.5
    assert summary["market"]["price_usd"] == 0.0021
    day = json.loads((tmp_path / "api" / "chart-24h.json").read_text())
    assert day["points"][-1][day["fields"].index("trading_volume_24h")] == 15234.5


def test_plain_dmd() -> None:
    assert publish.plain_dmd(533_515_500_000_000) == "5335155"
    assert publish.plain_dmd(533_515_550_000_000) == "5335155.5"
    assert publish.plain_dmd(1) == "0.00000001"
    assert publish.plain_dmd(0) == "0"
