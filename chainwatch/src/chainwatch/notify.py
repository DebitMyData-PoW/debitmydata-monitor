"""The notifications of the stats page (web push), as each device chose them.

Anyone can turn them on from the stats page (the bell, or the Notifications card), without an
account: the browser gives the page a subscription (its push service's address and the device's
keys), and the page gives it to /push (api.py) with what the device wants. Each device has a row
in push_devices: what it wants (prefs), what it was told last (memory), and its place in the
events (event_cursor).

What a device can choose (prefs; None or false: off):

  The network
    blocks        every new block (several at once: one notice)
    hashrate      the 24-hour hashrate moved by this many percent
    difficulty    the difficulty (the average of the last 24 blocks: LWMA moves it at every
                  block) moved by this many percent
    price         the reference price moved by this many percent
                  Each of the three either way since the last notice (its *_direction "both"),
                  or only up from its lowest since then ("up"), or only down from its highest
                  since then ("down"): hashrate_direction, difficulty_direction, price_direction
    price_targets up to 5 prices: the reference price reached one, from below or above (once,
                  until it has moved away from it and comes back)
    transfers     a transfer of this many DMD or more (from DMDW_LARGE_TRANSFER_DMD)
    slow          no new block for this many minutes, and when blocks come again
    halving       a day before the block reward halves, and when it does
    daily         the day's summary, after midnight UTC
  The chain's safety
    status        the stats page's status: the chain needs attention, and when it is normal again
    reorg         a reorganization of this many blocks or more
    branches      a competing branch, or invalid blocks
    new_miners    a miner finds a block, its first in 30 days
    concentration one miner found this many percent of the last 144 blocks (6 hours), and when
                  it goes back under
  Pools and miners (by the name on the stats page: up to 10)
    blocks        each block it finds
    hashrate      its hashrate (its share of the day's blocks × the network's) moved by this
                  many percent since the last notice
    silence       no block from it for this many hours, and when it finds one again
  Addresses (up to 10)
    every transaction to or from the address, mining rewards included, once it is in a block
  richlist        an address of the rich list's top 10 sent or received this many DMD or more in
                  a transaction (its mining rewards left out)

A change "since the last notice" is measured from the value the device was last told about (or
had when it chose that figure), so a slow drift is told once it adds up.

The service (dmd-chainwatch notify) looks every 5 seconds whether something changed (a new event,
block or price, a device's choices, a test asked for), and at least every minute. Each device gets
one notice per kind per run (several blocks: one notice), 8 at most. A notice its push service
refuses is tried again at the next run; a device whose browser dropped the subscription (404,
410), or whose push service refused 100 runs in a row, is removed.
"""

from __future__ import annotations

import copy
import hashlib
import logging
import math
import re
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import psycopg
from psycopg.types.json import Jsonb

from chainwatch import chain, push
from chainwatch.config import Settings

log = logging.getLogger(__name__)

MAX_POOLS = 10
MAX_ADDRESSES = 10
MAX_PRICE_TARGETS = 5
DIRECTIONS = ("both", "up", "down")
DIRECTED = ("hashrate", "difficulty", "price")  # the figures with a direction to choose
MAX_NOTICES = 8
MAX_FAILURES = 100
INBOX_ITEMS = 40
INBOX_DAYS = 3
FULL_EVERY = 60  # seconds: a full run at least this often (the silences, the slow blocks)
RECENT_BLOCKS = 144
SENDERS = 16

DEFAULT_PREFS: dict[str, Any] = {
    "blocks": False,
    "hashrate": 10,
    "hashrate_direction": "both",
    "difficulty": None,
    "difficulty_direction": "both",
    "price": 5,
    "price_direction": "both",
    "price_targets": [],
    "transfers": None,
    "slow": 30,
    "halving": True,
    "daily": False,
    "status": True,
    "reorg": 2,
    "branches": True,
    "new_miners": False,
    "concentration": None,
    "richlist": None,
    "pools": {},
    "addresses": [],
}
RICHLIST_TOP = 10
FLAGS = ("blocks", "halving", "daily", "status", "branches", "new_miners")
# name: (lowest, highest) of the figure a device can choose
RANGES: dict[str, tuple[float, float]] = {
    "hashrate": (1, 1000),
    "difficulty": (1, 1000),
    "price": (1, 1000),
    "concentration": (10, 100),
    "slow": (10, 1440),
    "reorg": (1, 100),
}
POOL_RANGES = {"hashrate": (1, 1000), "silence": (1, 168)}
ADDRESS = re.compile(r"^(dm1[02-9ac-hj-np-z]{6,87}|[1-9A-HJ-NP-Za-km-z]{25,40})$")


# --------------------------------------------------------------------------- the choices


def _figure(value: Any, low: float, high: float, name: str) -> float | None:
    if value is None or value is False or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name}: a number is needed") from None
    if not math.isfinite(number) or not low <= number <= high:
        raise ValueError(f"{name}: between {low:g} and {high:g}")
    number = round(number, 1)
    return int(number) if number == int(number) else number


def clean_prefs(
    raw: Any, transfer_floor: float, valid_address: Callable[[str], bool]
) -> dict[str, Any]:
    """What a device asked for, checked: the defaults for what it left out. ValueError (with a
    message for the page) when something cannot be taken."""
    if not isinstance(raw, dict):
        raise ValueError("the choices are missing")
    prefs = copy.deepcopy(DEFAULT_PREFS)
    for name in FLAGS:
        if name in raw:
            prefs[name] = bool(raw[name])
    for name, (low, high) in RANGES.items():
        if name in raw:
            prefs[name] = _figure(raw[name], low, high, name)
    if "transfers" in raw:
        prefs["transfers"] = _figure(raw["transfers"], transfer_floor, 1e10, "transfers")
    if "richlist" in raw:
        prefs["richlist"] = _figure(raw["richlist"], 1, 1e10, "richlist")
    for name in DIRECTED:
        key = name + "_direction"
        if key in raw:
            if raw[key] not in DIRECTIONS:
                raise ValueError(f"{key}: both, up or down")
            prefs[key] = raw[key]
    targets = raw.get("price_targets") or []
    if not isinstance(targets, list) or len(targets) > MAX_PRICE_TARGETS:
        raise ValueError(f"price targets: {MAX_PRICE_TARGETS} at most")
    prices: list[float] = []
    for target in targets:
        try:
            value = float(target)
        except (TypeError, ValueError):
            raise ValueError("price targets: a price in US dollars") from None
        if not math.isfinite(value) or not 0 < value <= 1_000_000:
            raise ValueError("price targets: more than $0 and up to $1,000,000")
        value = float(f"{value:.8g}")
        if value not in prices:
            prices.append(value)
    prefs["price_targets"] = sorted(prices)
    pools = raw.get("pools") or {}
    if not isinstance(pools, dict) or len(pools) > MAX_POOLS:
        raise ValueError(f"pools: {MAX_POOLS} at most")
    prefs["pools"] = {}
    for name, choice in pools.items():
        if not isinstance(name, str) or not 0 < len(name) <= 100 or not isinstance(choice, dict):
            raise ValueError("pools: a name and its choices")
        one = {
            "blocks": bool(choice.get("blocks")),
            "hashrate": _figure(choice.get("hashrate"), *POOL_RANGES["hashrate"], "hashrate"),
            "silence": _figure(choice.get("silence"), *POOL_RANGES["silence"], "silence"),
        }
        if one["blocks"] or one["hashrate"] is not None or one["silence"] is not None:
            prefs["pools"][name] = one
    addresses = raw.get("addresses") or []
    if not isinstance(addresses, list) or len(addresses) > MAX_ADDRESSES:
        raise ValueError(f"addresses: {MAX_ADDRESSES} at most")
    seen: list[str] = []
    for address in addresses:
        text = str(address).strip()
        if not ADDRESS.fullmatch(text) or not valid_address(text):
            raise ValueError(f"{text[:60]} is not a DMD address")
        if text not in seen:
            seen.append(text)
    prefs["addresses"] = seen
    return prefs


# --------------------------------------------------------------------------- formats


def _height(h: int) -> str:
    return f"{h:,}"


def _rate(v: float) -> str:
    for size, unit in (
        (1e18, "EH/s"),
        (1e15, "PH/s"),
        (1e12, "TH/s"),
        (1e9, "GH/s"),
        (1e6, "MH/s"),
    ):
        if v >= size:
            return f"{v / size:,.2f} {unit}"
    return f"{v:,.0f} H/s"


def _usd(v: float) -> str:
    if v >= 1:
        return f"${v:,.2f}"
    digits = max(2, min(12, 3 - math.floor(math.log10(v)))) if v > 0 else 2
    return "$" + f"{v:.{digits}f}".rstrip("0").rstrip(".")


def _dmd(data: float) -> str:
    value = data / chain.DATA_PER_DMD
    if value >= 100:
        return f"{value:,.2f} DMD"
    return f"{value:,.8f}".rstrip("0").rstrip(".") + " DMD"


def _big(v: float) -> str:
    for size, unit in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if v >= size:
            return f"{v / size:,.2f}{unit}"
    return f"{v:,.2f}"


def _short(text: str, keep: int = 6) -> str:
    return text if len(text) <= 2 * keep + 1 else f"{text[:keep]}…{text[-keep:]}"


def _ago(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 90:
        return f"{minutes} minutes"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes:02d} min" if hours < 48 else f"{hours // 24} days"


def _when(t: datetime) -> str:
    return f"{t.astimezone(UTC):%b %-d, %H:%M} UTC"


def _miner(name: str) -> str:
    """A miner's name at the start of a sentence."""
    return "The DebitMyData pool" if name == chain.PUBLIC_POOL else name


def _miner_in(name: str) -> str:
    """A miner's name inside a sentence."""
    return "the DebitMyData pool" if name == chain.PUBLIC_POOL else name


def _tag(name: str) -> str:
    return hashlib.sha256(name.encode()).hexdigest()[:10]


# --------------------------------------------------------------------------- the figures


@dataclass
class Figures:
    """The chain's figures at one moment: what the threshold notices compare with."""

    now: datetime
    height: int | None = None
    last_time: datetime | None = None
    hashrate: float | None = None  # 24 hours
    difficulty: float | None = None  # the last 24 blocks' average
    price: float | None = None
    day_blocks: int = 0
    day_shares: dict[str, float] = field(default_factory=dict)
    recent_blocks: int = 0
    recent_shares: dict[str, float] = field(default_factory=dict)
    last_by_miner: dict[str, tuple[int, datetime]] = field(default_factory=dict)


def figures(conn: psycopg.Connection[Any], now: datetime, miners: Iterable[str] = ()) -> Figures:
    f = Figures(now)
    tip = conn.execute(
        "SELECT height, time, difficulty, chainwork FROM blocks ORDER BY height DESC LIMIT 1"
    ).fetchone()
    if tip is None:
        return f
    f.height, f.last_time = tip["height"], tip["time"]
    hour = conn.execute(
        "SELECT avg(difficulty) AS d FROM (SELECT difficulty FROM blocks ORDER BY height DESC "
        "LIMIT 24) AS last"
    ).fetchone()
    f.difficulty = float(hour["d"]) if hour and hour["d"] else tip["difficulty"]
    before = conn.execute(
        "SELECT chainwork FROM blocks WHERE time <= %s ORDER BY height DESC LIMIT 1",
        (now - timedelta(days=1),),
    ).fetchone()
    if before is not None:
        f.hashrate = (int(tip["chainwork"]) - int(before["chainwork"])) / 86400
    price = conn.execute(
        "SELECT usd_per_dmd::float8 AS v FROM prices ORDER BY observed_at DESC LIMIT 1"
    ).fetchone()
    f.price = price["v"] if price else None
    day = conn.execute(
        "SELECT miner, count(*) AS n FROM blocks WHERE time > %s GROUP BY miner",
        (now - timedelta(days=1),),
    ).fetchall()
    f.day_blocks = sum(r["n"] for r in day)
    f.day_shares = {r["miner"]: r["n"] / f.day_blocks for r in day if r["miner"]}
    recent = conn.execute(
        "SELECT miner, count(*) AS n FROM (SELECT miner FROM blocks ORDER BY height DESC "
        "LIMIT %s) AS last GROUP BY miner",
        (RECENT_BLOCKS,),
    ).fetchall()
    f.recent_blocks = sum(r["n"] for r in recent)
    f.recent_shares = {r["miner"]: r["n"] / f.recent_blocks for r in recent if r["miner"]}
    names = sorted(set(miners))
    if names:
        for r in conn.execute(
            "SELECT DISTINCT ON (miner) miner, height, time FROM blocks WHERE miner = ANY(%s) "
            "ORDER BY miner, height DESC",
            (names,),
        ).fetchall():
            f.last_by_miner[r["miner"]] = (r["height"], r["time"])
    return f


# --------------------------------------------------------------------------- one device


def _notice(title: str, body: str, url: str = "/", tag: str = "") -> dict[str, Any]:
    return {"title": title[:120], "body": body[:400], "url": url, "tag": tag}


def _blocks_notice(rows: list[dict[str, Any]], who: str | None = None) -> dict[str, Any]:
    heights = [r["height"] for r in rows]
    span = (
        f"Block {_height(heights[0])}"
        if len(heights) == 1
        else f"Blocks {_height(min(heights))}–{_height(max(heights))}"
    )
    if who is not None:
        title = f"{_miner(who)} found " + ("a block" if len(rows) == 1 else f"{len(rows)} blocks")
        return _notice(title, span + ".", "/", "pool-" + _tag(who))
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["miner"]] = counts.get(r["miner"], 0) + 1
    if len(rows) == 1:
        return _notice(
            "New block",
            f"{span}, found by {_miner_in(rows[0]['miner'])}.",
            "/",
            "block",
        )
    parts = ", ".join(f"{n} by {_miner_in(m)}" for m, n in counts.items())
    return _notice(f"{len(rows)} new blocks", f"{span}: {parts}.", "/", "block")


def _address_notice(address: str, rows: list[dict[str, Any]], tx_url: str) -> dict[str, Any]:
    got = sum(int(r["received"]) for r in rows)
    sent = sum(int(r["sent"]) for r in rows)
    heights = sorted({int(r["height"]) for r in rows})
    where = (
        f"in block {_height(heights[0])}"
        if len(heights) == 1
        else f"in blocks {_height(heights[0])}–{_height(heights[-1])}"
    )
    short = _short(address, 7)
    url = tx_url + rows[-1]["txid"] if tx_url else "/"
    if len(rows) > 1:
        moves = []
        if got:
            moves.append(f"received {_dmd(got)}")
        if sent:
            moves.append(f"sent {_dmd(sent)}")
        return _notice(
            f"{len(rows)} transactions: {short}",
            f"{' and '.join(moves).capitalize() or 'Moved DMD'} {where} (amounts with change).",
            url,
        )
    r = rows[0]
    net = got - sent
    if r.get("reward"):
        return _notice(f"Mining reward: {_dmd(got)}", f"To {short}, {where}.", url)
    if net > 0:
        return _notice(f"Received {_dmd(net)}", f"To {short}, {where}.", url)
    if net < 0:
        fee_note = " (with the fee)" if got else ""
        return _notice(f"Sent {_dmd(-net)}", f"From {short}{fee_note}, {where}.", url)
    return _notice(
        f"DMD moved: {short}", f"A transaction spent from and paid back to it, {where}.", url
    )


def _threshold(
    memory: dict[str, Any],
    key: str,
    value: float | None,
    percent: float | None,
    now: datetime,
    direction: str = "both",
) -> tuple[float, float, datetime] | None:
    """The change, when it is this many percent or more: (change, from what, its time), and the
    new value is kept. Measured from the value of the last notice (or of when it was chosen):
    "both" ways; or only up from the lowest value since then ("up"), or only down from the
    highest ("down"), so that a rise after a fall counts from the bottom."""
    bases = memory.setdefault("base", {})
    if percent is None or value is None or value <= 0:
        if percent is None:
            bases.pop(key, None)
        return None
    stamp = now.isoformat()
    base = bases.get(key)
    if not base or base.get("v", 0) <= 0:
        bases[key] = {
            "v": value,
            "t": stamp,
            "lo": value,
            "lo_t": stamp,
            "hi": value,
            "hi_t": stamp,
        }
        return None
    if value < base.get("lo", base["v"]):
        base["lo"], base["lo_t"] = value, stamp
    if value > base.get("hi", base["v"]):
        base["hi"], base["hi_t"] = value, stamp
    if direction == "up":
        start, since = base.get("lo", base["v"]), base.get("lo_t", base["t"])
    elif direction == "down":
        start, since = base.get("hi", base["v"]), base.get("hi_t", base["t"])
    else:
        start, since = base["v"], base["t"]
    change = value / start - 1
    wanted = {"up": change > 0, "down": change < 0}.get(direction, True)
    if not wanted or abs(change) * 100 < percent:
        return None
    bases[key] = {"v": value, "t": stamp, "lo": value, "lo_t": stamp, "hi": value, "hi_t": stamp}
    return change, start, datetime.fromisoformat(since)


def _from(value: str, direction: str, t: datetime, first: bool = False) -> str:
    """Where a change was measured from: the value at the last notice, or (one way only) the
    lowest or highest since then."""
    since = {"up": "its lowest", "down": "its highest"}.get(direction)
    word = "From" if first else "from"
    if since:
        return f"{word} {value}, {since} since your last notice ({_when(t)})"
    return f"{word} {value} on {_when(t)}"


def _side(price: float, target: float) -> str:
    if abs(price - target) <= target * 1e-9:
        return "at"
    return "above" if price > target else "below"


def _price_targets(
    memory: dict[str, Any], targets: list[float], price: float | None, now: datetime
) -> list[dict[str, Any]]:
    """The targets the price reached since it was last seen: on it, or past it (from below to
    above, or from above to below). Not again until it has moved away and comes back."""
    sides: dict[str, str] = memory.setdefault("targets", {})
    keys = {f"{t:.8g}": t for t in targets}
    for key in [k for k in sides if k not in keys]:
        del sides[key]
    if not targets:
        memory.pop("targets", None)
    seen = memory.get("price_seen")
    out = []
    if price is None:
        return out
    before = f" (from {_usd(seen['v'])} before)" if seen and seen.get("v") != price else ""
    for key, target in keys.items():
        side, last = _side(price, target), sides.get(key)
        sides[key] = side
        if last is None or last == side:
            continue
        if side == "at":
            title = f"Reference price reached {_usd(target)}"
        elif last == "at":
            continue  # moving away from it: told when it comes back
        else:
            way = "rose above" if side == "above" else "fell below"
            title = f"Reference price {way} {_usd(target)}: {_usd(price)}"
        out.append(
            _notice(
                title,
                f"Your price alert at {_usd(target)}{before}. DebitMyData's reference price, "
                "not a market price.",
                "/",
                "target-" + key,
            )
        )
    memory["price_seen"] = {"v": price, "t": now.isoformat()}
    return out


def _moved(change: float) -> str:
    return f"{'up' if change > 0 else 'down'} {abs(change):.0%}" if abs(change) >= 0.01 else "moved"


def collect(
    device: dict[str, Any],
    events: list[dict[str, Any]],
    figs: Figures,
    settings: Settings,
) -> tuple[list[dict[str, Any]], dict[str, Any], int]:
    """What this device should be told now: the notices, its new memory and event cursor."""
    prefs = {**DEFAULT_PREFS, **(device["prefs"] or {})}
    memory: dict[str, Any] = copy.deepcopy(device["memory"] or {})
    cursor = int(device["event_cursor"])
    mine = [e for e in events if e["id"] > cursor]
    if mine:
        cursor = max(e["id"] for e in mine)
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for e in mine:
        by_kind.setdefault(e["kind"], []).append(e["data"])
    safety: list[dict[str, Any]] = []
    out: list[dict[str, Any]] = []
    pools: dict[str, dict[str, Any]] = prefs["pools"] or {}
    now = figs.now

    # the chain's safety
    if prefs["status"]:
        for e in by_kind.get("status", [])[-1:]:
            if e["state"] == "attention":
                safety.append(
                    _notice(
                        "The DebitMyData chain needs attention", e["title"] + ".", "/", "status"
                    )
                )
            else:
                safety.append(
                    _notice(
                        "The DebitMyData chain is back to normal",
                        f"Resolved: {e['title']}.",
                        "/",
                        "status",
                    )
                )
    if prefs["reorg"] is not None:
        deep = [e for e in by_kind.get("reorg", []) if e["depth"] >= prefs["reorg"]]
        for e in deep[-2:]:
            n = e["depth"]
            safety.append(
                _notice(
                    f"Chain reorganization: {n} block{'s' if n > 1 else ''}",
                    f"{n} block{'s' if n > 1 else ''} after block {_height(e['fork_height'])} "
                    f"{'were' if n > 1 else 'was'} replaced by others. Payments in them need to be "
                    "confirmed again.",
                )
            )
    if prefs["branches"]:
        for e in by_kind.get("branch", [])[-2:]:
            safety.append(_notice(e["title"], e["detail"]))
    if prefs["new_miners"]:
        for e in by_kind.get("new_miner", [])[-2:]:
            safety.append(
                _notice(
                    f"New miner: {_miner(e['miner'])}",
                    f"It found block {_height(e['height'])}, its first in 30 days.",
                    "/",
                    "miner-" + _tag(e["miner"]),
                )
            )
    if prefs["concentration"] is not None and figs.recent_blocks >= RECENT_BLOCKS // 2:
        above = memory.setdefault("above", {})
        limit = prefs["concentration"] / 100
        for name, share in sorted(figs.recent_shares.items(), key=lambda x: -x[1]):
            if share >= limit and name not in above:
                above[name] = True
                safety.append(
                    _notice(
                        f"{_miner(name)}: {share:.0%} of the blocks",
                        f"{_miner(name)} found {share:.0%} of the last {figs.recent_blocks} blocks "
                        f"(about 6 hours), {prefs['concentration']:g}% or more.",
                        "/",
                        "share-" + _tag(name),
                    )
                )
        for name in list(above):
            share = figs.recent_shares.get(name, 0.0)
            if share < limit:
                del above[name]
                safety.append(
                    _notice(
                        f"{_miner(name)}: back under {prefs['concentration']:g}%",
                        f"It found {share:.0%} of the last {figs.recent_blocks} blocks.",
                        "/",
                        "share-" + _tag(name),
                    )
                )
    else:
        memory.pop("above", None)
    out += safety

    # the followed addresses
    followed = set(prefs["addresses"] or [])
    moves: dict[str, list[dict[str, Any]]] = {}
    for e in by_kind.get("address", []):
        if e["address"] in followed:
            moves.setdefault(e["address"], []).append(e)
    for address, rows in moves.items():
        out.append(_address_notice(address, rows, settings.explorer_tx_url))
    if prefs["richlist"] is not None:
        minimum = prefs["richlist"] * chain.DATA_PER_DMD
        for e in by_kind.get("address", []):
            rank = e.get("rank")
            net = int(e["received"]) - int(e["sent"])
            if not rank or rank > RICHLIST_TOP or e.get("reward") or e["address"] in followed:
                continue
            if abs(net) < minimum:
                continue
            verb = "received" if net > 0 else "sent"
            out.append(
                _notice(
                    f"Top 10 address #{rank} {verb} {_dmd(abs(net))}",
                    f"{_short(e['address'], 7)}, number {rank} of the rich list, in block "
                    f"{_height(int(e['height']))}.",
                    settings.explorer_tx_url + e["txid"] if settings.explorer_tx_url else "/",
                )
            )

    # large transfers
    if prefs["transfers"] is not None:
        big = [e for e in by_kind.get("transfer", []) if e["dmd"] >= prefs["transfers"]]
        if len(big) == 1:
            e = big[0]
            out.append(
                _notice(
                    f"Large transfer: {_dmd(e['dmd'] * chain.DATA_PER_DMD)}",
                    f"In block {_height(e['height'])}: transaction {_short(e['txid'], 8)}.",
                    settings.explorer_tx_url + e["txid"] if settings.explorer_tx_url else "/",
                    "transfer",
                )
            )
        elif big:
            total = sum(e["dmd"] for e in big)
            largest = max(big, key=lambda e: e["dmd"])
            out.append(
                _notice(
                    f"{len(big)} large transfers: {_dmd(total * chain.DATA_PER_DMD)} in all",
                    f"The largest: {_dmd(largest['dmd'] * chain.DATA_PER_DMD)}, in block "
                    f"{_height(largest['height'])}.",
                    "/",
                    "transfer",
                )
            )

    # blocks: every one, or those of the followed pools
    blocks = by_kind.get("block", [])
    back: set[str] = set()
    silent = memory.setdefault("silent", {})
    for name, choice in pools.items():
        last = figs.last_by_miner.get(name)
        if choice.get("silence") is None or last is None:
            silent.pop(name, None)
            continue
        if name in silent and last[0] > silent[name]["height"]:
            gap = (last[1] - datetime.fromisoformat(silent[name]["time"])).total_seconds()
            out.append(
                _notice(
                    f"{_miner(name)} found a block again",
                    f"Block {_height(last[0])}, after {_ago(gap)} without one.",
                    "/",
                    "pool-" + _tag(name),
                )
            )
            del silent[name]
            back.add(name)
        elif name not in silent and (now - last[1]).total_seconds() >= choice["silence"] * 3600:
            silent[name] = {"height": last[0], "time": last[1].isoformat()}
            out.append(
                _notice(
                    f"No block from {_miner_in(name)} for {_ago((now - last[1]).total_seconds())}",
                    f"Its last: block {_height(last[0])}, at {_when(last[1])}.",
                    "/",
                    "pool-" + _tag(name),
                )
            )
    for name in list(silent):
        if name not in pools:
            del silent[name]
    if prefs["blocks"] and blocks:
        out.append(_blocks_notice(blocks))
    elif blocks:
        for name, choice in pools.items():
            theirs = [b for b in blocks if b["miner"] == name]
            if choice.get("blocks") and theirs and name not in back:
                out.append(_blocks_notice(theirs, name))

    # no new block for a while, and when they come again
    if prefs["slow"] is not None and figs.height is not None and figs.last_time is not None:
        slow = memory.get("slow")
        waited = (now - figs.last_time).total_seconds()
        if slow and figs.height > slow["height"]:
            gap = (figs.last_time - datetime.fromisoformat(slow["time"])).total_seconds()
            out.append(
                _notice(
                    "New blocks again",
                    f"Block {_height(figs.height)} came {_ago(gap)} after the one before.",
                    "/",
                    "slow",
                )
            )
            memory.pop("slow", None)
        elif not slow and waited >= prefs["slow"] * 60:
            memory["slow"] = {"height": figs.height, "time": figs.last_time.isoformat()}
            out.append(
                _notice(
                    f"No new block for {_ago(waited)}",
                    f"The last is block {_height(figs.height)}, at {_when(figs.last_time)}. "
                    "A block comes every 2.5 minutes on average.",
                    "/",
                    "slow",
                )
            )
    else:
        memory.pop("slow", None)

    # the figures that moved by the chosen percentage, either way or the way chosen
    way = {name: prefs.get(name + "_direction") or "both" for name in DIRECTED}
    moved = _threshold(memory, "hashrate", figs.hashrate, prefs["hashrate"], now, way["hashrate"])
    if moved and figs.hashrate:
        change, base, t = moved
        out.append(
            _notice(
                f"Hashrate {_moved(change)}: {_rate(figs.hashrate)}",
                f"The network's 24-hour hashrate, {_from(_rate(base), way['hashrate'], t)}.",
                "/",
                "hashrate",
            )
        )
    moved = _threshold(
        memory, "difficulty", figs.difficulty, prefs["difficulty"], now, way["difficulty"]
    )
    if moved and figs.difficulty:
        change, base, t = moved
        out.append(
            _notice(
                f"Difficulty {_moved(change)}: {_big(figs.difficulty)}",
                f"The last hour's average, {_from(_big(base), way['difficulty'], t)}.",
                "/",
                "difficulty",
            )
        )
    moved = _threshold(memory, "price", figs.price, prefs["price"], now, way["price"])
    if moved and figs.price:
        change, base, t = moved
        out.append(
            _notice(
                f"Reference price {_moved(change)}: {_usd(figs.price)}",
                f"{_from(_usd(base), way['price'], t, first=True)}. DebitMyData's reference "
                "price, not a market price.",
                "/",
                "price",
            )
        )
    out += _price_targets(memory, prefs.get("price_targets") or [], figs.price, now)
    for name, choice in pools.items():
        share = figs.day_shares.get(name, 0.0)
        rate = share * figs.hashrate if figs.hashrate and figs.day_blocks >= 100 else None
        moved = _threshold(memory, "pool-" + name, rate, choice.get("hashrate"), now)
        if moved and rate:
            change, base, t = moved
            out.append(
                _notice(
                    f"{_miner(name)}: hashrate {_moved(change)}",
                    f"About {_rate(rate)}: {share:.0%} of the day's blocks × the network's "
                    f"hashrate; from {_rate(base)} on {_when(t)}.",
                    "/",
                    "poolrate-" + _tag(name),
                )
            )
    for key in [k for k in memory.get("base", {}) if k.startswith("pool-")]:
        if key[5:] not in pools:
            del memory["base"][key]

    # the calendar
    if prefs["halving"]:
        for e in by_kind.get("halving_soon", [])[-1:]:
            out.append(
                _notice(
                    "The block reward halves in about a day",
                    f"At block {_height(e['height'])}: {e['reward_dmd']:g} DMD per "
                    "block from then on.",
                    "/",
                    "halving",
                )
            )
        for e in by_kind.get("halving", [])[-1:]:
            out.append(
                _notice(
                    "The DMD block reward halved",
                    f"From block {_height(e['height'])}: {e['reward_dmd']:g} DMD per block.",
                    "/",
                    "halving",
                )
            )
    if prefs["daily"]:
        for e in by_kind.get("daily", [])[-1:]:
            out.append(_notice(e["title"], e["detail"].replace("\n", " · "), "/", "daily"))
    return out[:MAX_NOTICES], memory, cursor


# --------------------------------------------------------------------------- the service


def endpoint_hash(endpoint: str) -> str:
    return hashlib.sha256(endpoint.encode()).hexdigest()


def latest_event(conn: psycopg.Connection[Any]) -> int:
    row = conn.execute("SELECT coalesce(max(id), 0) AS id FROM events").fetchone()
    return int(row["id"]) if row else 0


def watched_addresses(conn: psycopg.Connection[Any]) -> frozenset[str]:
    rows = conn.execute(
        "SELECT DISTINCT jsonb_array_elements_text(prefs->'addresses') AS a FROM push_devices"
    ).fetchall()
    return frozenset(r["a"] for r in rows)


def inbox(sent: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    since = (now - timedelta(days=INBOX_DAYS)).timestamp()
    return [i for i in sent if i.get("time", 0) >= since][-INBOX_ITEMS:]


class Notifier:
    def __init__(self, settings: Settings, conn: psycopg.Connection[Any], keys: push.Keys) -> None:
        self.settings, self.conn, self.keys = settings, conn, keys
        self.last_mark: Any = None
        self.last_full = 0.0
        self.pool = ThreadPoolExecutor(max_workers=SENDERS, thread_name_prefix="push")

    def mark(self) -> Any:
        return self.conn.execute(
            "SELECT (SELECT coalesce(max(id), 0) FROM events) AS e, "
            "(SELECT max(height) FROM blocks) AS b, (SELECT max(observed_at) FROM prices) AS p, "
            "(SELECT count(*) FROM push_devices WHERE test_at IS NOT NULL) AS t, "
            "(SELECT max(updated_at) FROM push_devices) AS u, "
            "(SELECT count(*) FROM push_devices) AS n"
        ).fetchone()

    def due(self, clock: float) -> bool:
        mark = self.mark()
        if mark == self.last_mark and clock - self.last_full < FULL_EVERY:
            return False
        self.last_mark, self.last_full = mark, clock
        return True

    def _send(self, device: dict[str, Any], messages: list[dict[str, Any]]) -> tuple[str, str]:
        """Sends a device's notices in order: "ok", "gone" (dropped by its browser) or
        "failed" (to try again), with the push service's last answer."""
        answer = ""
        for message in messages:
            code, text = push.deliver(self.keys, self.settings.push_subject, device, message)
            answer = f"{code} {text}".strip()
            if code in (404, 410):
                return "gone", answer
            if not 200 <= code < 300:
                return "failed", answer
        return "ok", answer

    def run_once(self, now: datetime) -> int:
        devices = self.conn.execute("SELECT * FROM push_devices ORDER BY id").fetchall()
        if not devices:
            return 0
        start = min(int(d["event_cursor"]) for d in devices)
        events = self.conn.execute(
            "SELECT id, kind, data FROM events WHERE id > %s ORDER BY id LIMIT 5000", (start,)
        ).fetchall()
        miners = {name for d in devices for name in ((d["prefs"] or {}).get("pools") or {})}
        figs = figures(self.conn, now, miners)
        work = []
        for device in devices:
            messages, memory, cursor = collect(device, events, figs, self.settings)
            if device["test_at"] is not None:
                messages.insert(
                    0, _notice("Test notice", "Notifications work on this device.", "/", "test")
                )
            stamp = int(now.timestamp())
            for n, m in enumerate(messages):
                m["id"] = f"{stamp}-{device['id']}-{n}"
                m["time"] = stamp
            work.append((device, messages, memory, cursor))
        results = list(
            self.pool.map(lambda w: self._send(w[0], w[1]) if w[1] else ("ok", ""), work)
        )
        sent = 0
        for (device, messages, memory, cursor), (state, answer) in zip(work, results, strict=True):
            host = urlsplit(device["endpoint"]).hostname
            if device["test_at"] is not None:
                log.info("test notice to device %s (%s): %s", device["id"], host, answer)
            if state == "gone" or (state == "failed" and device["failures"] + 1 >= MAX_FAILURES):
                log.info("device %s (%s) removed: %s", device["id"], host, answer)
                self.conn.execute("DELETE FROM push_devices WHERE id = %s", (device["id"],))
                continue
            if state == "failed":
                if device["failures"] % 20 == 0:
                    log.info("device %s (%s) not reached: %s", device["id"], host, answer)
                self.conn.execute(
                    "UPDATE push_devices SET failures = failures + 1, test_at = NULL WHERE id = %s",
                    (device["id"],),
                )
                continue
            sent += len(messages)
            items = inbox(
                (device["sent"] or [])
                + [
                    {k: m[k] for k in ("id", "time", "title", "body", "url")}
                    for m in messages
                    if m.get("tag") != "test"
                ],
                now,
            )
            if (
                messages
                or memory != (device["memory"] or {})
                or cursor != device["event_cursor"]
                or device["failures"]
            ):
                self.conn.execute(
                    "UPDATE push_devices SET memory = %s, event_cursor = %s, sent = %s, "
                    "failures = 0, test_at = NULL WHERE id = %s",
                    (Jsonb(memory), cursor, Jsonb(items), device["id"]),
                )
        if sent:
            log.info("%d notices sent", sent)
        return sent
