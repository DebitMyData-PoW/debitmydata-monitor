"""Keeps the blocks table equal to the node's chain.

On each pass: find the last block the table and the node agree on (normally the table's tip),
remove the table's blocks above it (they left the chain: a reorganization, recorded), then add
the node's blocks after it, in batches. The first pass fills the table from block 0, which takes
a few minutes; the charts have the whole history from then on.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from chainwatch import chain
from chainwatch.config import CostPeriod, period_for
from chainwatch.rpc import Rpc

log = logging.getLogger(__name__)

BATCH = 100
PRINTABLE = re.compile(rb"[\x20-\x7e]{4,}")


@dataclass
class SyncResult:
    added: int = 0
    removed: int = 0
    fork_height: int | None = None
    tip_height: int | None = None


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
        script = out.get("scriptPubKey") or {}
        address = script.get("address")
        if not address:
            addresses = script.get("addresses") or []
            address = addresses[0] if addresses else None
        value = int((Decimal(str(out.get("value", 0))) * chain.DATA_PER_DMD).to_integral_value())
        if address and value > 0:
            paid[address] = paid.get(address, 0) + value
    return paid


def _row(
    block: dict[str, Any],
    work_before: int | None,
    window: int,
    periods: list[CostPeriod],
    pool_tag: str,
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
        "pool": bool(pool_tag) and pool_tag in tag,
        "reward_data": reward,
        "subsidy_data": subsidy,
        "payouts": Jsonb(paid),
        "hashes_per_block": per_block.quantize(Decimal(1)),
        "mining_cost_usd": cost,
        "cost_figures_from": period.starts if work_before is not None else None,
    }


INSERT = """
INSERT INTO blocks (height, hash, time, median_time, difficulty, chainwork, tx_count, size,
    coinbase_tag, pool, reward_data, subsidy_data, payouts, hashes_per_block, mining_cost_usd,
    cost_figures_from)
VALUES (%(height)s, %(hash)s, %(time)s, %(median_time)s, %(difficulty)s, %(chainwork)s,
    %(tx_count)s, %(size)s, %(coinbase_tag)s, %(pool)s, %(reward_data)s, %(subsidy_data)s,
    %(payouts)s, %(hashes_per_block)s, %(mining_cost_usd)s, %(cost_figures_from)s)
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
        blocks = rpc.batch([("getblock", [h, 2]) for h in hashes])
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
        rows = []
        for block in blocks:
            height = int(block["height"])
            window = min(chain.WINDOW_BLOCKS, height)
            before = _work_at(conn, cache, height - window) if window > 0 else None
            row = _row(block, before, window, periods, pool_tag)
            cache[height] = row["chainwork"]
            rows.append(row)
        for old in [h for h in cache if h < end - chain.WINDOW_BLOCKS]:
            del cache[old]
        with conn.transaction():
            conn.cursor().executemany(INSERT, rows)
        result.added += len(rows)
        if result.added and result.added % 10_000 < BATCH:
            log.info("blocks up to %d stored", end)
        start = end + 1
    return result


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
