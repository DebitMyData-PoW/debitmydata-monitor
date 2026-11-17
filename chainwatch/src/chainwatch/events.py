"""What happens on the chain, for the notifications: the chain watch records it here as it
happens, and the notifications service (notify.py) tells each device what it chose to hear about.

  block         a new block: its height, time and miner
  new_miner     a block from a miner that found none in the 30 days before
  transfer      a large transfer (DMDW_LARGE_TRANSFER_DMD or more)
  address       a transaction to or from an address that a device follows, or one at the top
                of the rich list (with its place: rank)
  reorg         blocks that left the chain, and how many
  halving       the block reward halved; halving_soon: about a day before (576 blocks)
  branch        a competing branch or invalid blocks (the forks check)
  status        the chain needs attention, or is back to normal (the stats page's status)
  daily         the day's summary

Blocks found more than LIVE_HOURS ago (the first fill, a long catch-up after a stop) make no
event, except the followed addresses' transactions of the last day. Events are kept a week.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from chainwatch import chain
from chainwatch.alerts import Message
from chainwatch.sync import SyncResult

LIVE_HOURS = 2
ADDRESS_HOURS = 24
NEW_MINER_DAYS = 30
DAY_BLOCKS = 576
KEEP = timedelta(days=7)


def add(conn: psycopg.Connection[Any], now: datetime, kind: str, data: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO events (time, kind, data) VALUES (%s, %s, %s)", (now, kind, Jsonb(data))
    )


def _once(conn: psycopg.Connection[Any], kind: str, key: str) -> bool:
    """True the first time an event of this kind and key is recorded."""
    return (
        conn.execute(
            "SELECT 1 FROM events WHERE kind = %s AND data->>'key' = %s", (kind, key)
        ).fetchone()
        is None
    )


def record_sync(
    conn: psycopg.Connection[Any],
    result: SyncResult,
    now: datetime,
    ranks: dict[str, int] | None = None,
) -> int:
    """The events of a sync pass: a reorganization, the new blocks and what they hold. ranks:
    the top of the rich list (address: place), written into the events of those addresses."""
    count = 0
    if result.removed and result.fork_height is not None:
        add(conn, now, "reorg", {"fork_height": result.fork_height, "depth": result.removed})
        count += 1
    if result.first_added is not None:
        live = now - timedelta(hours=LIVE_HOURS)
        rows = conn.execute(
            "SELECT height, hash, time, miner FROM blocks WHERE height >= %s AND time > %s "
            "ORDER BY height",
            (result.first_added, live),
        ).fetchall()
        for r in rows:
            height, miner = r["height"], r["miner"] or "unknown"
            add(
                conn,
                now,
                "block",
                {
                    "height": height,
                    "hash": r["hash"],
                    "time": r["time"].isoformat(),
                    "miner": miner,
                },
            )
            count += 1
            if miner != "unknown" and _new_miner(conn, miner, height, r["time"]):
                add(conn, now, "new_miner", {"height": height, "miner": miner})
                count += 1
            if height > 0 and height % chain.HALVING_BLOCKS == 0:
                reward = chain.subsidy_data(height) / chain.DATA_PER_DMD
                add(conn, now, "halving", {"height": height, "reward_dmd": reward})
                count += 1
            if (height + DAY_BLOCKS) % chain.HALVING_BLOCKS == 0:
                add(
                    conn,
                    now,
                    "halving_soon",
                    {
                        "height": height + DAY_BLOCKS,
                        "reward_dmd": chain.subsidy_data(height + DAY_BLOCKS) / chain.DATA_PER_DMD,
                    },
                )
                count += 1
        for t in conn.execute(
            "SELECT txid, height, value_data FROM transfers WHERE height >= %s AND time > %s "
            "ORDER BY height, value_data DESC",
            (result.first_added, live),
        ).fetchall():
            add(
                conn,
                now,
                "transfer",
                {
                    "txid": t["txid"],
                    "height": t["height"],
                    "dmd": t["value_data"] / chain.DATA_PER_DMD,
                },
            )
            count += 1
    recent = (now - timedelta(hours=ADDRESS_HOURS)).isoformat()
    for a in result.activity:
        if a.get("time", "") > recent:
            rank = (ranks or {}).get(a["address"])
            add(conn, now, "address", {**a, "rank": rank} if rank else a)
            count += 1
    return count


def _new_miner(conn: psycopg.Connection[Any], miner: str, height: int, when: datetime) -> bool:
    """No block from this miner in the 30 days before (on a chain older than that)."""
    since = when - timedelta(days=NEW_MINER_DAYS)
    older = conn.execute("SELECT 1 FROM blocks WHERE time < %s LIMIT 1", (since,)).fetchone()
    if older is None:
        return False  # a young chain: every miner would be new
    seen = conn.execute(
        "SELECT 1 FROM blocks WHERE miner = %s AND height < %s AND time > %s LIMIT 1",
        (miner, height, since),
    ).fetchone()
    return seen is None


def record_messages(
    conn: psycopg.Connection[Any], messages: Iterable[Message], now: datetime
) -> int:
    """The events of the alerts the checks raised: branches and invalid blocks, the chain's
    status on the stats page (stalled, a fork between views), the daily summary."""
    count = 0
    for m in messages:
        f = m.finding
        if f.key.startswith(("forks.competing.", "forks.invalid.")) and m.state == "event":
            if _once(conn, "branch", f.key):
                add(conn, now, "branch", {"key": f.key, "title": f.title, "detail": f.detail})
                count += 1
        elif f.key == "stall.blocks" or (f.key.startswith("views.") and f.key.endswith(".block")):
            if m.state in ("open", "resolved"):
                state = "normal" if m.state == "resolved" else "attention"
                add(conn, now, "status", {"key": f.key, "state": state, "title": f.title})
                count += 1
        elif f.key.startswith("daily.") and m.state == "event" and _once(conn, "daily", f.key):
            add(conn, now, "daily", {"key": f.key, "title": f.title, "detail": f.detail})
            count += 1
    return count


def prune(conn: psycopg.Connection[Any], now: datetime) -> None:
    conn.execute("DELETE FROM events WHERE time < %s", (now - KEEP,))
