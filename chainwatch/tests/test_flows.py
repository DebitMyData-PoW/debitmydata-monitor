from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import psycopg

from chainwatch import chain
from chainwatch.sync import fill_flows, flows, rich_list, sync

from .conftest import PERIODS, POOL_ADDRESS, FakeNode, spend

NOW = datetime(2026, 11, 17, 12, tzinfo=UTC)
DMD = chain.DATA_PER_DMD


def balances(conn: psycopg.Connection[Any]) -> dict[str, int]:
    return {
        r["address"]: int(r["balance_data"])
        for r in conn.execute("SELECT address, balance_data FROM balances").fetchall()
        if r["balance_data"]
    }


def test_volume_leaves_out_change_and_rewards() -> None:
    block = {
        "height": 7,
        "tx": [
            {
                "txid": "cb",
                "vin": [{"coinbase": "00"}],
                "vout": [{"value": Decimal(35), "scriptPubKey": {"address": "dm1qminer"}}],
            },
            # 100 DMD from alice: 60 to bob, 39.9 back to alice (change), 0.1 fee
            spend("t1", [("dm1qalice", "100")], [("dm1qbob", "60"), ("dm1qalice", "39.9")]),
            # 2000 DMD from carol to dave: a large transfer
            spend("t2", [("dm1qcarol", "2000.5")], [("dm1qdave", "2000")]),
            # carol to herself: nothing moved
            spend("t3", [("dm1qcarol", "5")], [("dm1qcarol", "4.99")]),
        ],
    }
    moved = flows(block, 1000 * DMD, frozenset({"dm1qalice", "dm1qminer"}))
    assert moved.volume == 2060 * DMD
    assert moved.fees == int(Decimal("0.61") * DMD)
    assert moved.transfers == [("t2", 2000 * DMD, int(Decimal("0.5") * DMD))]
    assert moved.moves == {
        "dm1qalice": int(Decimal("-60.1") * DMD),
        "dm1qbob": 60 * DMD,
        "dm1qcarol": int(Decimal("-2000.51") * DMD),
        "dm1qdave": 2000 * DMD,
    }
    by_address = {(a["address"], a["txid"]): a for a in moved.activity}
    assert by_address[("dm1qminer", "cb")]["reward"] is True
    assert by_address[("dm1qminer", "cb")]["received"] == 35 * DMD
    alice = by_address[("dm1qalice", "t1")]
    assert (alice["sent"], alice["received"]) == (100 * DMD, int(Decimal("39.9") * DMD))
    # read without what the transactions spend: not known
    old = {**block, "tx": [block["tx"][0], {**block["tx"][1], "vin": [{"txid": "x"}]}]}
    assert flows(old).volume is None


def test_balances_follow_the_chain_and_its_reorganizations(
    conn: psycopg.Connection[Any], node: FakeNode
) -> None:
    node.add(10)  # 350 DMD to the pool's address
    node.add(
        1, txs=[spend("pay", [(POOL_ADDRESS, "35")], [("dm1qbig", "30"), (POOL_ADDRESS, "4.9")])]
    )
    node.add(2)
    sync(conn, node, PERIODS, "Official", NOW)
    assert balances(conn) == {
        POOL_ADDRESS: 13 * 35 * DMD - int(Decimal("30.1") * DMD),
        "dm1qbig": 30 * DMD,
    }
    row = conn.execute(
        "SELECT volume_data, fees_data, miner FROM blocks WHERE height = 10"
    ).fetchone()
    assert (row["volume_data"], row["fees_data"]) == (30 * DMD, int(Decimal("0.1") * DMD))
    assert row["miner"] == chain.PUBLIC_POOL
    assert [r["address"] for r in rich_list(conn)] == [POOL_ADDRESS, "dm1qbig"]

    # the block with the payment leaves the chain: its moves are taken back
    node.replace_from(10, 3, tag="other", payouts={"dm1qother": Decimal(35)})
    result = sync(conn, node, PERIODS, "Official", NOW)
    assert result.removed == 3
    assert balances(conn) == {POOL_ADDRESS: 10 * 35 * DMD, "dm1qother": 3 * 35 * DMD}
    assert conn.execute("SELECT count(*) AS n FROM address_moves").fetchone()["n"] == 0


def test_blocks_of_an_earlier_version_are_counted_once(
    conn: psycopg.Connection[Any], node: FakeNode
) -> None:
    node.add(5)
    node.add(1, txs=[spend("big", [(POOL_ADDRESS, "35")], [("dm1qwhale", "34.9")])])
    node.add(4)
    sync(conn, node, PERIODS, "Official", NOW)
    # as an earlier version left them: no volume, no balances
    conn.execute("UPDATE blocks SET volume_data = NULL, fees_data = NULL")
    conn.execute("DELETE FROM balances")
    conn.execute("DELETE FROM address_moves")
    conn.execute("DELETE FROM transfers")
    assert rich_list(conn) == []  # not every block is counted yet
    assert fill_flows(conn, node, 4, 10 * DMD) == 4  # newest first
    assert rich_list(conn) == []
    assert fill_flows(conn, node, 100, 10 * DMD) == 6
    assert fill_flows(conn, node, 100, 10 * DMD) == 0
    assert balances(conn) == {
        POOL_ADDRESS: 10 * 35 * DMD - 35 * DMD,
        "dm1qwhale": int(Decimal("34.9") * DMD),
    }
    assert [r["address"] for r in rich_list(conn)] == [POOL_ADDRESS, "dm1qwhale"]
    transfer = conn.execute("SELECT * FROM transfers").fetchone()
    assert (transfer["txid"], transfer["height"], transfer["value_data"]) == (
        "big",
        5,
        int(Decimal("34.9") * DMD),
    )


def test_the_followed_addresses_activity(conn: psycopg.Connection[Any], node: FakeNode) -> None:
    node.add(3)
    node.add(
        1, txs=[spend("t", [(POOL_ADDRESS, "35")], [("dm1qfriend", "20"), (POOL_ADDRESS, "15")])]
    )
    result = sync(
        conn, node, PERIODS, "Official", NOW, watched=frozenset({"dm1qfriend", POOL_ADDRESS})
    )
    assert result.first_added == 0
    got = {(a["address"], a["height"], a["reward"]) for a in result.activity}
    assert ("dm1qfriend", 3, False) in got
    assert (POOL_ADDRESS, 3, True) in got  # the reward of block 3
    assert (POOL_ADDRESS, 0, True) in got
