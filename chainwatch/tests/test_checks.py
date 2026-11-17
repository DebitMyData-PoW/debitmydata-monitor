from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from typing import Any

import psycopg
import pytest

from chainwatch import checks, price
from chainwatch.alerts import CRITICAL, INFO, WARNING
from chainwatch.checks import Context
from chainwatch.cli import run_checks
from chainwatch.config import Settings
from chainwatch.sync import sync

from .conftest import PERIODS, FakeNode, utc


class Views:
    """The explorers and Prometheus (the pool agent's figures), as the checks see them."""

    def __init__(self, node: FakeNode) -> None:
        self.node = node
        self.explorer_lag = 0
        self.explorer_hashes: dict[int, str] = {}
        self.pool: dict[str, float] | None = None
        self.down: set[str] = set()
        self.alerts: list[dict[str, Any]] = []

    def fetch(self, url: str) -> str:
        site = "explorer" if "explorer" in url else "mempool"
        if site in self.down:
            raise OSError("timed out")
        tip = self.node.blocks[-1]["height"] - self.explorer_lag
        if url.endswith("/api/getblockcount") or url.endswith("/api/blocks/tip/height"):
            return str(tip)
        height = int(url.rsplit("=", 1)[-1] if "index=" in url else url.rsplit("/", 1)[-1])
        if height > tip:
            return "There was an error."
        return self.explorer_hashes.get(height, self.node.blocks[height]["hash"])

    def prometheus(self, base: str, path: str) -> Any:
        if path == "/api/v1/alerts":
            return {"data": {"alerts": self.alerts}}
        if "pool" in self.down or self.pool is None:
            return {"data": {"result": []}}
        name = path.split("query=", 1)[1]
        return {"data": {"result": [{"value": [0, str(self.pool[name])]}]}}


@pytest.fixture
def views(node: FakeNode) -> Views:
    return Views(node)


def context(conn: Any, node: FakeNode, settings: Settings, views: Views, at: int) -> Context:
    return Context(conn, node, settings, utc(at), views.fetch, views.prometheus)


def run(
    conn: Any,
    node: FakeNode,
    settings: Settings,
    views: Views,
    at: int,
    only: set[str] | None = None,
) -> dict[str, Any]:
    findings, _ = run_checks(context(conn, node, settings, views, at), only)
    return {f.key: f for f in findings}


def test_node_and_stall(
    conn: psycopg.Connection[Any], node: FakeNode, settings: Settings, views: Views
) -> None:
    node.add(10)
    last = node.blocks[-1]["time"]
    assert run(conn, node, settings, views, last + 60, {"node", "stall"}) == {}
    node.peers = 0
    found = run(conn, node, settings, views, last + 31 * 60, {"node", "stall"})
    assert found["node.peers"].severity == WARNING
    assert (found["stall.blocks"].severity, found["stall.blocks"].title) == (
        WARNING,
        "No new DebitMyData block for 31 minutes",
    )
    found = run(conn, node, settings, views, last + 61 * 60, {"stall"})
    assert found["stall.blocks"].severity == CRITICAL
    node.down = True
    found = run(conn, node, settings, views, last + 61 * 60)
    assert "node.down" in found and "stall.blocks" not in found


def test_views_agree_then_differ(
    conn: psycopg.Connection[Any], node: FakeNode, settings: Settings, views: Views
) -> None:
    node.add(100)
    views.explorer_lag = 1
    tip = node.blocks[-1]
    views.pool = {
        "dmd_pool_node_blocks": 99,
        "dmd_pool_node_compare_height": 97,
        "dmd_pool_node_compare_prefix": int(node.blocks[97]["hash"][:13], 16),
    }
    assert run(conn, node, settings, views, tip["time"], {"views"}) == {}

    # The explorer has another block at 96 (2 below its tip, 98): nothing the first time
    views.explorer_hashes[96] = "f" * 64
    assert run(conn, node, settings, views, tip["time"], {"views"}) == {}
    found = run(conn, node, settings, views, tip["time"] + 60, {"views"})
    fork = found["views.explorer.block"]
    assert fork.severity == CRITICAL
    assert fork.title == (
        "The monitor's node and the public explorer (explorer.debitmydata.io) are on "
        "different chains"
    )
    assert f"this node has {node.blocks[96]['hash']} and the public explorer" in fork.detail
    # the mempool explorer agrees (same answers here, but its own path)
    assert "views.mempool.block" in found  # (the fake gives both the same hashes)
    # The pool's node too
    views.pool["dmd_pool_node_compare_prefix"] = 0x1234
    run(conn, node, settings, views, tip["time"] + 120, {"views"})
    found = run(conn, node, settings, views, tip["time"] + 180, {"views"})
    assert found["views.pool.block"].title == (
        "The monitor's node and the pool's node are on different chains"
    )

    # The explorer stops answering: its fork alert stays; after 15 minutes, it says so
    views.down.add("explorer")
    for minute in range(14):
        found = run(conn, node, settings, views, tip["time"] + 240 + 60 * minute, {"views"})
        assert "views.explorer.block" in found and "views.explorer.unreachable" not in found
    found = run(conn, node, settings, views, tip["time"] + 1200, {"views"})
    assert "views.explorer.unreachable" in found

    # Back, on the same chain: everything ends
    views.down.clear()
    views.explorer_hashes.clear()
    views.pool["dmd_pool_node_compare_prefix"] = int(node.blocks[97]["hash"][:13], 16)
    assert run(conn, node, settings, views, tip["time"] + 1300, {"views"}) == {}


def test_views_heights(
    conn: psycopg.Connection[Any], node: FakeNode, settings: Settings, views: Views
) -> None:
    node.add(50)
    views.explorer_lag = 10
    found = run(conn, node, settings, views, node.blocks[-1]["time"], {"views"})
    assert found["views.explorer.height"].title == (
        "The public explorer (explorer.debitmydata.io) is 10 blocks behind the monitor's node"
    )
    assert "views.pool.height" not in found and "views.pool.block" not in found


def _tip(height: int, branchlen: int, status: str) -> dict[str, Any]:
    return {"height": height, "hash": f"{height:064x}", "branchlen": branchlen, "status": status}


def test_forks(
    conn: psycopg.Connection[Any], node: FakeNode, settings: Settings, views: Views
) -> None:
    node.add(201)
    node.tips_extra = [
        _tip(199, 1, "valid-fork"),
        _tip(197, 3, "valid-fork"),
        _tip(190, 7, "valid-fork"),
        _tip(203, 3, "headers-only"),
        _tip(201, 4, "valid-headers"),
        _tip(199, 1, "invalid"),
        _tip(120, 10, "valid-fork"),
    ]
    found = run(conn, node, settings, views, node.blocks[-1]["time"], {"forks"})
    assert {k: f.severity for k, f in found.items()} == {
        "forks.reorg.194": WARNING,
        "forks.reorg.183.deep": CRITICAL,
        "forks.competing.197": WARNING,
        "forks.invalid.198": WARNING,
    }
    assert all(f.kind == "event" and f.audience == "private" for f in found.values())
    public = replace(settings, public_reorgs=True)
    found = run(conn, node, public, views, node.blocks[-1]["time"], {"forks"})
    assert found["forks.reorg.194"].audience == "both"


def test_hashrate_and_difficulty(
    conn: psycopg.Connection[Any], node: FakeNode, settings: Settings, views: Views
) -> None:
    node.add(24 * 26, difficulty=1000)  # a day and 2 hours at the usual pace
    node.add(30, difficulty=1000, seconds=30)  # then 5 times as many blocks for 15 minutes
    node.add(100, difficulty=4000, seconds=30)
    sync(conn, node, PERIODS, "Official", utc(node.blocks[-1]["time"]))
    at = node.blocks[-1]["time"] + 10
    found = run(conn, node, settings, views, at, {"hashrate", "difficulty"})
    jump = next(f for k, f in found.items() if k.startswith("hashrate.up."))
    assert jump.title.startswith("DebitMyData hashrate jumped: ")
    assert jump.audience == "both" and jump.kind == "event"
    # the last hour's average (100 blocks at 4000, 20 at 1000) against a day before (1000)
    assert found[f"difficulty.up.{utc(at):%Y%m%d}"].title == (
        "DebitMyData difficulty up 250% in a day"
    )
    # not again for 6 hours
    found = run(conn, node, settings, views, at + 600, {"hashrate"})
    assert not any(k.startswith("hashrate.") for k in found)


def test_miners(
    conn: psycopg.Connection[Any], node: FakeNode, settings: Settings, views: Views
) -> None:
    node.add(100, payouts={"dm1qalice": Decimal(30), "dm1qbob": Decimal(5)})
    node.add(44, tag="SoloMiner", payouts={"dm1qsolo": Decimal(35)})
    sync(conn, node, PERIODS, "Official", utc(node.blocks[-1]["time"]))
    found = run(conn, node, settings, views, node.blocks[-1]["time"], {"miners"})
    assert found["miners.outside"].title == (
        "A miner outside the pool found 31% of the last 144 blocks"
    )
    assert "SoloMiner: 44 of the last 144" in found["miners.outside"].detail
    payee = found["miners.payee"]
    assert payee.title == "One address got 86% of the pool's payouts in the last blocks"
    assert payee.detail.startswith("dm1qalice received 86% of what the pool's last 100 blocks")
    # the pool's own wallet is not a miner
    excluded = replace(settings, pool_addresses=["dm1qalice"])
    found = run(conn, node, excluded, views, node.blocks[-1]["time"], {"miners"})
    assert "miners.payee" in found and found["miners.payee"].detail.startswith("dm1qbob")


def test_price(
    conn: psycopg.Connection[Any], node: FakeNode, settings: Settings, views: Views
) -> None:
    node.add(5)
    at = node.blocks[-1]["time"]
    found = run(conn, node, settings, views, at, {"price"})
    assert found["price.stale"].detail.startswith("The monitor has no price from the gateway")
    inputs = {
        "mining_cost_usd_per_dmd": "0.00003",
        "minimum_usd_per_dmd": "0.001",
        "price_from": "minimum",
    }
    price.record(conn, utc(at - 600), Decimal("0.001"), inputs, "gateway")
    price.record(
        conn, utc(at - 300), Decimal("0.0011"), {**inputs, "price_from": "mining_cost"}, "gateway"
    )
    found = run(conn, node, settings, views, at, {"price"})
    change = next(iter(found.values()))
    assert change.key.startswith("price.change.") and change.severity == INFO
    assert change.title == "DMD reference price up 10%: $0.0011"
    found = run(conn, node, settings, views, at + 3600, {"price"})
    assert "price.stale" in found


def test_server_alerts_from_prometheus(
    conn: psycopg.Connection[Any], node: FakeNode, settings: Settings, views: Views
) -> None:
    node.add(3)
    views.alerts = [
        {
            "state": "firing",
            "labels": {"alertname": "PoolServerDown", "server": "pool", "severity": "critical"},
            "annotations": {"summary": "The pool server does not answer", "description": "d"},
        },
        {"state": "pending", "labels": {"alertname": "DiskFull"}, "annotations": {}},
    ]
    found = run(conn, node, settings, views, node.blocks[-1]["time"], {"server"})
    assert list(found) == ["server.PoolServerDown.pool"]
    assert found["server.PoolServerDown.pool"].severity == CRITICAL


def test_daily_summary(
    conn: psycopg.Connection[Any], node: FakeNode, settings: Settings, views: Views
) -> None:
    node.add(1200)
    sync(conn, node, PERIODS, "Official", utc(node.blocks[-1]["time"]))
    last = utc(node.blocks[-1]["time"])
    midnight = last.replace(hour=0, minute=0, second=0, microsecond=0)
    assert (
        run(
            conn,
            node,
            settings,
            views,
            int((midnight + timedelta(minutes=2)).timestamp()),
            {"daily"},
        )
        == {}
    )
    found = run(conn, node, settings, views, int(last.timestamp()), {"daily"})
    summary = next(iter(found.values()))
    day = midnight - timedelta(days=1)
    assert summary.key == f"daily.{day:%Y-%m-%d}" and summary.audience == "public"
    assert "Blocks: 576" in summary.detail
    assert "one every 2.5 minutes on average" in summary.detail
    assert "Found by the DebitMyData pool: 100% of the blocks" in summary.detail


def test_a_failing_check_is_reported(
    conn: psycopg.Connection[Any],
    node: FakeNode,
    settings: Settings,
    views: Views,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node.add(3)

    def broken(ctx: Context) -> list[Any]:
        raise ValueError("boom")

    monkeypatch.setitem(checks.CHECKS, "difficulty", broken)
    findings, completed = run_checks(context(conn, node, settings, views, node.blocks[-1]["time"]))
    keys = {f.key for f in findings}
    assert "monitor.difficulty" in keys and "difficulty" not in completed
    assert "node" in completed


def test_lwma_swings_are_not_news(
    conn: psycopg.Connection[Any], node: FakeNode, settings: Settings, views: Views
) -> None:
    node.add(24 * 25, difficulty=1000)
    node.add(24, difficulty=1300)  # 30% higher for the last hour: LWMA does that
    sync(conn, node, PERIODS, "Official", utc(node.blocks[-1]["time"]))
    found = run(conn, node, settings, views, node.blocks[-1]["time"] + 10, {"difficulty"})
    assert not any(k.startswith("difficulty.") for k in found)
