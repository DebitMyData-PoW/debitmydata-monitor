from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import psycopg

from chainwatch import chain
from chainwatch.sync import coinbase_tag, payouts, recompute_costs, sync

from .conftest import PERIODS, POOL_ADDRESS, FakeNode

NOW = datetime(2026, 11, 17, 12, tzinfo=UTC)


def test_coinbase_text_and_payouts() -> None:
    assert coinbase_tag("03a1b2c3" + b"Official".hex()) == "Official"
    assert coinbase_tag("zz") == ""
    paid = payouts(
        [
            {"value": Decimal("30.5"), "scriptPubKey": {"address": "dm1qa"}},
            {"value": Decimal("4.5"), "scriptPubKey": {"address": "dm1qb"}},
            {"value": Decimal("0.00000001"), "scriptPubKey": {"address": "dm1qa"}},
            {"value": Decimal(0), "scriptPubKey": {"type": "nulldata"}},
        ]
    )
    assert paid == {"dm1qa": 3_050_000_001, "dm1qb": 450_000_000}


def test_first_fill_then_new_blocks(conn: psycopg.Connection[Any], node: FakeNode) -> None:
    node.add(250)
    result = sync(conn, node, PERIODS, "Official", NOW)
    assert (result.added, result.removed, result.tip_height) == (250, 0, 249)
    row = conn.execute("SELECT * FROM blocks WHERE height = 249").fetchone()
    assert row["pool"] is True and row["coinbase_tag"] == "Official"
    assert row["payouts"] == {POOL_ADDRESS: 3_500_000_000}
    assert row["subsidy_data"] == row["reward_data"] == 3_500_000_000
    # the weekly average over all the blocks so far (fewer than a week): difficulty × 2^32
    assert row["hashes_per_block"] == 1000 * 2**32
    expected = chain.mining_cost_usd(
        Decimal(1000 * 2**32), Decimal(35), Decimal(20), Decimal("0.06")
    )
    assert row["mining_cost_usd"] == expected
    assert (
        conn.execute("SELECT mining_cost_usd FROM blocks WHERE height = 0").fetchone()[
            "mining_cost_usd"
        ]
        == 0
    )

    node.add(3)
    result = sync(conn, node, PERIODS, "Official", NOW)
    assert (result.added, result.removed) == (3, 0)
    assert sync(conn, node, PERIODS, "Official", NOW).added == 0


def test_a_reorganization_replaces_the_blocks(
    conn: psycopg.Connection[Any], node: FakeNode
) -> None:
    node.add(120)
    sync(conn, node, PERIODS, "Official", NOW)
    left = node.replace_from(117, 4, tag="other-miner", payouts={"dm1qsomeone": Decimal(35)})
    result = sync(conn, node, PERIODS, "Official", NOW)
    assert (result.removed, result.added, result.fork_height) == (3, 4, 116)
    reorg = conn.execute("SELECT * FROM reorgs").fetchone()
    assert (reorg["fork_height"], reorg["depth"], reorg["left_hashes"]) == (116, 3, left)
    hashes = [
        r["hash"]
        for r in conn.execute(
            "SELECT hash FROM blocks WHERE height >= 117 ORDER BY height"
        ).fetchall()
    ]
    assert hashes == [b["hash"] for b in node.blocks[117:]]
    assert conn.execute("SELECT pool FROM blocks WHERE height = 118").fetchone()["pool"] is False


def test_filling_in_parts(conn: psycopg.Connection[Any], node: FakeNode) -> None:
    node.add(330)
    assert sync(conn, node, PERIODS, "Official", NOW, max_blocks=200).added == 200
    assert sync(conn, node, PERIODS, "Official", NOW, max_blocks=200).added == 130
    count = conn.execute("SELECT count(*) AS n FROM blocks").fetchone()["n"]
    assert count == 330


def test_recompute_costs(conn: psycopg.Connection[Any], node: FakeNode) -> None:
    node.add(50)
    sync(conn, node, PERIODS, "Official", NOW)
    doubled = [replace(PERIODS[0], electricity_usd_per_kwh=Decimal("0.12"))]
    before = conn.execute("SELECT mining_cost_usd FROM blocks WHERE height = 49").fetchone()
    assert recompute_costs(conn, doubled) == 49
    after = conn.execute("SELECT mining_cost_usd FROM blocks WHERE height = 49").fetchone()
    # twice the electricity price, twice the cost (to the last of its 15 decimals)
    assert abs(after["mining_cost_usd"] - before["mining_cost_usd"] * 2) <= Decimal("1e-15")
