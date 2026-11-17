import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import psycopg
import pytest

from chainwatch import cli, price
from chainwatch.config import Settings

from .conftest import FakeNode

CONFIG = Path(__file__).resolve().parents[2] / "config" / "mining-cost.toml"


def test_a_pass_of_the_service(
    conn: psycopg.Connection[Any],
    node: FakeNode,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node.add(600)
    monkeypatch.setattr(cli, "_rpc", lambda s: node)
    monkeypatch.setattr(
        price,
        "fetch_json",
        lambda url: {
            "usd_per_unit": "0.001",
            "observed_at": "2026-11-17T11:55:00Z",
            "inputs": {"price_from": "minimum", "mining_cost_usd_per_dmd": "0.0000003"},
        },
    )
    monkeypatch.setattr(cli, "SYNC_BATCH", 250)
    s = replace(
        settings,
        mining_cost_file=CONFIG,
        explorer_url="",
        mempool_url="",
        prometheus_url="http://127.0.0.1:1",
    )
    service = cli.Service(s)
    service.pass_once()  # 250 blocks, the price, the checks, the files
    assert service.best is None  # not at the node's tip yet
    service.next_check = service.next_publish = 0
    service.pass_once()
    service.pass_once()
    assert service.best == node.blocks[-1]["hash"]
    summary = json.loads((s.public_dir / "api" / "summary.json").read_text())
    assert summary["height"] == 599
    assert summary["reference_price"]["usd_per_dmd"] == 0.001
    # Prometheus is not there: the check says so, and nothing else breaks
    open_keys = {
        r["key"]
        for r in conn.execute("SELECT key FROM alerts WHERE resolved_at IS NULL").fetchall()
    }
    assert "monitor.server" in open_keys
    assert "dmd_chain_height 599" in s.metrics_file.read_text()
