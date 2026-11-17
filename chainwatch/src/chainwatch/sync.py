"""Keeps the blocks table equal to the node's chain.

On each pass: find the last block the table and the node agree on (normally the table's tip),
remove the table's blocks above it (they left the chain: a reorganization, recorded), then add
the node's blocks after it, in batches. The first pass fills the table from block 0, which takes
a few minutes; the charts have the whole history from then on.

Each block is read with its transactions and what they spend (getblock, verbosity 3), for:
  volume     the DMD its transactions sent to other addresses: an output back to an address the
             transaction spends from is change, and does not count; the coinbase (the mining
             reward) does not count either
  fees       what its transactions paid to the miner
  transfers  its transactions that sent DMDW_LARGE_TRANSFER_DMD or more (the large transfers)
  activity   what its transactions (the coinbase included) sent to or from the addresses that
             devices follow, and the top of the rich list (the notifications and the alerts)
  balances   what its transactions add to or take from each address (address_moves), with its
             coinbase's payouts: the balances of every address, for the rich list
The blocks stored by an earlier version get their volume, fees and large transfers later
(fill_flows), newest first; -1: the node could not tell (not known).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from chainwatch import chain
from chainwatch.config import CostPeriod, period_for
from chainwatch.rpc import Rpc, RpcError

log = logging.getLogger(__name__)

BATCH = 100
PRINTABLE = re.compile(rb"[\x20-\x7e]{4,}")
LARGE_TRANSFER_DATA = 1000 * chain.DATA_PER_DMD


@dataclass
class SyncResult:
    added: int = 0
    removed: int = 0
    fork_height: int | None = None
    tip_height: int | None = None
    first_added: int | None = None  # the height of the first block added
    activity: list[dict[str, Any]] = field(default_factory=list)  # of the followed addresses


@dataclass
class Flows:
    volume: int | None  # data; None: not known (the block was read without what it spends)
    fees: int | None
    transfers: list[tuple[str, int, int | None]]  # (txid, data sent, fee) of the large ones
    activity: list[dict[str, Any]]
    moves: dict[str, int] = field(default_factory=dict)  # address: data in (+) or out (-)


def _data(value: Any) -> int:
    return int((Decimal(str(value or 0)) * chain.DATA_PER_DMD).to_integral_value())


def _address(script: dict[str, Any]) -> str | None:
    address = script.get("address")
    if not address:
        addresses = script.get("addresses") or []
        address = addresses[0] if addresses else None
    return str(address) if address else None


def flows(
    block: dict[str, Any], large: int = LARGE_TRANSFER_DATA, watched: frozenset[str] = frozenset()
) -> Flows:
    """The DMD a block's transactions moved, their fees, the large transfers, and what they did
    to the followed addresses."""
    height = int(block["height"])
    volume = fees = 0
    known = True
    transfers: list[tuple[str, int, int | None]] = []
    activity: list[dict[str, Any]] = []
    moves: dict[str, int] = {}
    for index, tx in enumerate(block.get("tx") or []):
        if not isinstance(tx, dict):
            return Flows(None, None, [], [])  # read without its transactions
        outs = [
            (_address(o.get("scriptPubKey") or {}), _data(o.get("value")))
            for o in tx.get("vout", [])
        ]
        txid = str(tx.get("txid", ""))
        if index == 0:  # the coinbase: the mining reward (and the fees), not volume
            for address in {a for a, v in outs if a in watched and v > 0}:
                got = sum(v for a, v in outs if a == address)
                activity.append(
                    {
                        "address": address,
                        "txid": txid,
                        "height": height,
                        "received": got,
                        "sent": 0,
                        "reward": True,
                    }
                )
            continue
        ins: list[tuple[str | None, int]] = []
        for vin in tx.get("vin", []):
            prevout = vin.get("prevout")
            if not isinstance(prevout, dict):
                known = False
                break
            ins.append((_address(prevout.get("scriptPubKey") or {}), _data(prevout.get("value"))))
        if not known:
            continue
        senders = {a for a, _ in ins if a}
        moved = sum(v for a, v in outs if v > 0 and a not in senders)
        fee = sum(v for _, v in ins) - sum(v for _, v in outs)
        volume += moved
        fees += fee
        if moved >= large:
            transfers.append((txid, moved, fee))
        for who, value in ins:
            if who:
                moves[who] = moves.get(who, 0) - value
        for who, value in outs:
            if who and value:
                moves[who] = moves.get(who, 0) + value
        for address in (senders | {a for a, _ in outs if a}) & watched:
            sent = sum(v for a, v in ins if a == address)
            got = sum(v for a, v in outs if a == address)
            if sent or got:
                activity.append(
                    {
                        "address": address,
                        "txid": txid,
                        "height": height,
                        "received": got,
                        "sent": sent,
                        "reward": False,
                    }
                )
    if not known:
        return Flows(None, None, [], activity)
    return Flows(volume, fees, transfers, activity, {a: v for a, v in moves.items() if v})


def coinbase_tag(coinbase_hex: str) -> str:
    """The readable text in a coinbase (a pool writes its name there): runs of 4 or more
    printable characters, joined with " | "."""
    try:
        raw = bytes.fromhex(coinbase_hex)
    except ValueError:
        return ""
    return " | ".join(m.group().decode("ascii").strip() for m in PRINTABLE.finditer(raw))[:200]


def payouts(vout: list[dict[str, Any]]) -> dict[str, int]:
    """The coinbase's outputs by address, in data (outputs without an address are left out:
    the segwit commitment, OP_RETURN)."""
    paid: dict[str, int] = {}
    for out in vout:
        address = _address(out.get("scriptPubKey") or {})
        value = _data(out.get("value", 0))
        if address and value > 0:
            paid[address] = paid.get(address, 0) + value
    return paid


def _row(
    block: dict[str, Any],
    work_before: int | None,
    window: int,
    periods: list[CostPeriod],
    pool_tag: str,
    moved: Flows | None = None,
) -> dict[str, Any]:
    height = int(block["height"])
    coinbase = block["tx"][0]
    tag = coinbase_tag(coinbase["vin"][0].get("coinbase", ""))
    paid = payouts(coinbase.get("vout", []))
    reward = int(
        sum(
            (Decimal(str(o.get("value", 0))) for o in coinbase.get("vout", [])),
            Decimal(0),
        )
        * chain.DATA_PER_DMD
    )
    subsidy = chain.subsidy_data(height)
    work = int(str(block["chainwork"]), 16)
    when = datetime.fromtimestamp(int(block["time"]), UTC)
    period = period_for(periods, when.date())
    if work_before is not None and window > 0:
        per_block = chain.hashes_per_block(work, work_before, window)
        cost = chain.mining_cost_usd(
            per_block,
            Decimal(subsidy) / chain.DATA_PER_DMD,
            period.efficiency_joules_per_terahash,
            period.electricity_usd_per_kwh,
        )
    else:
        per_block, cost = Decimal(0), Decimal(0)
    pool = bool(pool_tag) and pool_tag in tag
    moved = moved or flows(block)
    return {
        "height": height,
        "hash": block["hash"],
        "time": when,
        "median_time": datetime.fromtimestamp(int(block.get("mediantime", block["time"])), UTC),
        "difficulty": float(block["difficulty"]),
        "chainwork": work,
        "tx_count": int(block.get("nTx", len(block["tx"]))),
        "size": int(block.get("size", 0)),
        "coinbase_tag": tag,
        "pool": pool,
        "miner": chain.miner_name(pool, tag, paid),
        "volume_data": moved.volume,
        "fees_data": moved.fees,
        "reward_data": reward,
        "subsidy_data": subsidy,
        "payouts": Jsonb(paid),
        "hashes_per_block": per_block.quantize(Decimal(1)),
        "mining_cost_usd": cost,
        "cost_figures_from": period.starts if work_before is not None else None,
    }


INSERT = """
INSERT INTO blocks (height, hash, time, median_time, difficulty, chainwork, tx_count, size,
    coinbase_tag, pool, miner, volume_data, fees_data, reward_data, subsidy_data, payouts,
    hashes_per_block, mining_cost_usd, cost_figures_from)
VALUES (%(height)s, %(hash)s, %(time)s, %(median_time)s, %(difficulty)s, %(chainwork)s,
    %(tx_count)s, %(size)s, %(coinbase_tag)s, %(pool)s, %(miner)s, %(volume_data)s,
    %(fees_data)s, %(reward_data)s, %(subsidy_data)s, %(payouts)s, %(hashes_per_block)s,
    %(mining_cost_usd)s, %(cost_figures_from)s)
"""
MOVE = "INSERT INTO address_moves (height, address, delta_data) VALUES (%s, %s, %s)"
BALANCE = """
INSERT INTO balances (address, balance_data, last_height) VALUES (%s, %s, %s)
ON CONFLICT (address) DO UPDATE SET balance_data = balances.balance_data + EXCLUDED.balance_data,
    last_height = greatest(balances.last_height, EXCLUDED.last_height)
"""


def _balances(
    conn: psycopg.Connection[Any],
    blocks: list[tuple[int, dict[str, int], dict[str, int]]],
    sign: int,
) -> None:
    """Adds (sign 1) or takes back (-1) what blocks did to the balances: (height, the coinbase's
    payouts, the transactions' moves) for each."""
    total: dict[str, tuple[int, int]] = {}
    for height, paid, moved in blocks:
        for source in (paid, moved):
            for address, value in source.items():
                before, top = total.get(address, (0, 0))
                total[address] = (before + sign * int(value), max(top, height))
    if total:
        conn.cursor().executemany(BALANCE, [(a, v, h) for a, (v, h) in total.items() if v])


TRANSFER = """
INSERT INTO transfers (txid, height, time, value_data, fee_data) VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (txid) DO UPDATE SET height = EXCLUDED.height, time = EXCLUDED.time,
    value_data = EXCLUDED.value_data, fee_data = EXCLUDED.fee_data
"""


def _common_height(conn: psycopg.Connection[Any], rpc: Rpc, node_tip: int) -> int:
    """The highest height where the table and the node have the same block (-1: none)."""
    row = conn.execute("SELECT max(height) AS h FROM blocks").fetchone()
    top = row["h"] if row and row["h"] is not None else -1
    height = min(top, node_tip)
    while height >= 0:
        low = max(0, height - 49)
        heights = list(range(height, low - 1, -1))
        node_hashes = rpc.batch([("getblockhash", [h]) for h in heights])
        ours = {
            r["height"]: r["hash"]
            for r in conn.execute(
                "SELECT height, hash FROM blocks WHERE height BETWEEN %s AND %s", (low, height)
            ).fetchall()
        }
        for h, node_hash in zip(heights, node_hashes, strict=True):
            if ours.get(h) == node_hash:
                return h
        height = low - 1
    return -1


def _work_at(conn: psycopg.Connection[Any], cache: dict[int, int], height: int) -> int | None:
    if height in cache:
        return cache[height]
    row = conn.execute("SELECT chainwork FROM blocks WHERE height = %s", (height,)).fetchone()
    return None if row is None else int(row["chainwork"])


def sync(
    conn: psycopg.Connection[Any],
    rpc: Rpc,
    periods: list[CostPeriod],
    pool_tag: str,
    now: datetime,
    max_blocks: int | None = None,
    large: int = LARGE_TRANSFER_DATA,
    watched: frozenset[str] = frozenset(),
) -> SyncResult:
    result = SyncResult()
    node_tip = int(rpc.call("getblockcount"))
    result.tip_height = node_tip
    common = _common_height(conn, rpc, node_tip)
    with conn.transaction():
        left = conn.execute(
            "SELECT height, hash FROM blocks WHERE height > %s ORDER BY height", (common,)
        ).fetchall()
        if left:
            counted = conn.execute(
                "SELECT height, payouts FROM blocks WHERE height > %s AND volume_data IS NOT NULL",
                (common,),
            ).fetchall()
            taken: dict[int, dict[str, int]] = {}
            for m in conn.execute(
                "SELECT height, address, delta_data FROM address_moves WHERE height > %s", (common,)
            ).fetchall():
                taken.setdefault(m["height"], {})[m["address"]] = int(m["delta_data"])
            _balances(
                conn,
                [(r["height"], r["payouts"] or {}, taken.get(r["height"], {})) for r in counted],
                -1,
            )
            conn.execute("DELETE FROM blocks WHERE height > %s", (common,))
            conn.execute(
                "INSERT INTO reorgs (detected_at, fork_height, depth, left_hashes) "
                "VALUES (%s, %s, %s, %s)",
                (now, common, len(left), Jsonb([r["hash"] for r in left])),
            )
            result.removed, result.fork_height = len(left), common
            log.warning("%d blocks after block %d left the chain", len(left), common)

    start = common + 1
    stop = node_tip if max_blocks is None else min(node_tip, common + max_blocks)
    # The total work of the blocks a week back from those being added (for the weekly average)
    cache: dict[int, int] = {
        r["height"]: int(r["chainwork"])
        for r in conn.execute(
            "SELECT height, chainwork FROM blocks WHERE height BETWEEN %s AND %s",
            (max(0, start - chain.WINDOW_BLOCKS), start - 1),
        ).fetchall()
    }
    while start <= stop:
        end = min(stop, start + BATCH - 1)
        hashes = rpc.batch([("getblockhash", [h]) for h in range(start, end + 1)])
        blocks = rpc.batch([("getblock", [h, 3]) for h in hashes])
        # The chain may have moved while the batch was read: each block must follow the one
        # before it (the next pass starts again from the last block both agree on).
        want = None
        if start > 0:
            previous = conn.execute(
                "SELECT hash FROM blocks WHERE height = %s", (start - 1,)
            ).fetchone()
            want = previous["hash"] if previous else None
        for block in blocks:
            if int(block["height"]) > 0 and block.get("previousblockhash") != want:
                log.info("the chain moved during the sync; starting again from block %d", start)
                return result
            want = block["hash"]
        rows, big, activity, counts, move_rows = [], [], [], [], []
        for block in blocks:
            height = int(block["height"])
            window = min(chain.WINDOW_BLOCKS, height)
            before = _work_at(conn, cache, height - window) if window > 0 else None
            moved = flows(block, large, watched)
            row = _row(block, before, window, periods, pool_tag, moved)
            cache[height] = row["chainwork"]
            rows.append(row)
            big += [(txid, height, row["time"], v, fee) for txid, v, fee in moved.transfers]
            activity += [{**a, "time": row["time"].isoformat()} for a in moved.activity]
            if moved.volume is not None:
                counts.append((height, row["payouts"].obj, moved.moves))
                move_rows += [(height, a, v) for a, v in moved.moves.items()]
        for old in [h for h in cache if h < end - chain.WINDOW_BLOCKS]:
            del cache[old]
        with conn.transaction():
            conn.cursor().executemany(INSERT, rows)
            if big:
                conn.cursor().executemany(TRANSFER, big)
            if move_rows:
                conn.cursor().executemany(MOVE, move_rows)
            _balances(conn, counts, 1)
        if result.first_added is None:
            result.first_added = start
        result.activity += activity
        result.added += len(rows)
        if result.added and result.added % 10_000 < BATCH:
            log.info("blocks up to %d stored", end)
        start = end + 1
    return result


FILL_BLOCKS = 1000  # blocks per read at most
FILL_TRANSACTIONS = 2000  # transactions per read at first (the service adjusts it)


def _read_old(rpc: Rpc, rows: list[dict[str, Any]]) -> list[dict[str, Any] | None]:
    """The blocks with what their transactions spend; one the node will not give that way is
    read without it (None: not even that), so that one block cannot stop the count."""
    try:
        return list(rpc.batch([("getblock", [r["hash"], 3]) for r in rows]))
    except RpcError:
        if len(rows) > 1:
            return [b for r in rows for b in _read_old(rpc, [r])]
    log.warning("block %s: read without what it spends", rows[0]["height"])
    try:
        return [rpc.call("getblock", rows[0]["hash"], 2)]
    except RpcError as exc:
        log.warning("block %s: %s", rows[0]["height"], exc)
        return [None]


def fill_flows(
    conn: psycopg.Connection[Any],
    rpc: Rpc,
    transactions: int = FILL_TRANSACTIONS,
    large: int = LARGE_TRANSFER_DATA,
) -> int:
    """Volume, fees, large transfers and balances of blocks stored without them (by an earlier
    version), newest first: as many blocks as hold about this many transactions (one at least,
    FILL_BLOCKS at most), so that a busy part of the chain is read in smaller pieces. Returns how
    many blocks were done."""
    candidates = conn.execute(
        "SELECT height, hash, time, payouts, tx_count FROM blocks WHERE volume_data IS NULL "
        "ORDER BY height DESC LIMIT %s",
        (FILL_BLOCKS,),
    ).fetchall()
    rows, total = [], 0
    for r in candidates:
        if rows and total + r["tx_count"] > transactions:
            break
        rows.append(r)
        total += r["tx_count"]
    if not rows:
        return 0
    blocks = _read_old(rpc, rows)
    updates, big, counts, move_rows = [], [], [], []
    for r, block in zip(rows, blocks, strict=True):
        moved = flows(block, large) if block else Flows(None, None, [], [])
        if moved.volume is None:  # the node cannot tell what it spends: -1, not known
            moved = Flows(-1, None, [], [])
        updates.append((moved.volume, moved.fees, r["hash"]))
        big += [(txid, r["height"], r["time"], v, fee) for txid, v, fee in moved.transfers]
        counts.append((r["height"], r["payouts"] or {}, moved.moves))
        move_rows += [(r["height"], a, v) for a, v in moved.moves.items()]
    with conn.transaction():
        # a block the chain left meanwhile is not changed (its hash is gone): nor counted
        done = conn.cursor()
        kept = []
        for update, count in zip(updates, counts, strict=True):
            done.execute(
                "UPDATE blocks SET volume_data = %s, fees_data = %s "
                "WHERE hash = %s AND volume_data IS NULL",
                update,
            )
            if done.rowcount:
                kept.append(count)
        heights = {c[0] for c in kept}
        if big:
            conn.cursor().executemany(TRANSFER, [b for b in big if b[1] in heights])
        if move_rows:
            conn.cursor().executemany(MOVE, [m for m in move_rows if m[0] in heights])
        _balances(conn, kept, 1)
    return len(rows)


def blocks_to_count(conn: psycopg.Connection[Any]) -> int:
    row = conn.execute("SELECT count(*) AS n FROM blocks WHERE volume_data IS NULL").fetchone()
    return int(row["n"]) if row else 0


def rich_list(conn: psycopg.Connection[Any], top: int = 100) -> list[dict[str, Any]]:
    """The addresses with the most DMD, once every block is counted (else empty)."""
    if conn.execute("SELECT 1 FROM blocks WHERE volume_data IS NULL LIMIT 1").fetchone():
        return []
    return list(
        conn.execute(
            "SELECT address, balance_data FROM balances WHERE balance_data > 0 "
            "ORDER BY balance_data DESC, address LIMIT %s",
            (top,),
        ).fetchall()
    )


def recompute_costs(
    conn: psycopg.Connection[Any], periods: list[CostPeriod], chunk: int = 5000
) -> int:
    """Works out every block's mining cost again (after a change of the approved figures)."""
    changed = 0
    top = conn.execute("SELECT coalesce(max(height), -1) AS h FROM blocks").fetchone()["h"]
    works: dict[int, int] = {}
    for low in range(0, top + 1, chunk):
        high = min(top, low + chunk - 1)
        need_low = max(0, low - chain.WINDOW_BLOCKS)
        for r in conn.execute(
            "SELECT height, chainwork FROM blocks WHERE height BETWEEN %s AND %s",
            (need_low, high),
        ).fetchall():
            works[r["height"]] = int(r["chainwork"])
        rows = conn.execute(
            "SELECT height, time, subsidy_data FROM blocks WHERE height BETWEEN %s AND %s",
            (low, high),
        ).fetchall()
        updates = []
        for r in rows:
            height = r["height"]
            window = min(chain.WINDOW_BLOCKS, height)
            if window == 0 or (height - window) not in works:
                continue
            period = period_for(periods, r["time"].date())
            per_block = chain.hashes_per_block(works[height], works[height - window], window)
            cost = chain.mining_cost_usd(
                per_block,
                Decimal(r["subsidy_data"]) / chain.DATA_PER_DMD,
                period.efficiency_joules_per_terahash,
                period.electricity_usd_per_kwh,
            )
            updates.append((per_block.quantize(Decimal(1)), cost, period.starts, height))
        with conn.transaction():
            conn.cursor().executemany(
                "UPDATE blocks SET hashes_per_block = %s, mining_cost_usd = %s, "
                "cost_figures_from = %s WHERE height = %s",
                updates,
            )
        changed += len(updates)
        for h in [h for h in works if h < high - chain.WINDOW_BLOCKS]:
            del works[h]
    return changed
