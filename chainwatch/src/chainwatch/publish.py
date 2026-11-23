"""The public stats page's files, and the numbers Prometheus reads.

The stats page (stats.debitmydata.io) is static: nginx serves these files from the public folder,
and Cloudflare caches them for a minute. The page's API (stats.debitmydata.io/api/) explains them.

  api/summary.json            the latest figures, with their changes over a day
  api/chart-<range>.json      the chart points and candles of a range: 15m, 1h, 4h, 24h, 7d, 30d,
                              1y, all (30d, 1y and all are written again every 10 minutes)
  api/miners.json             who found the last day's, week's and month's blocks
  api/blocks.json             the last 1,000 blocks and who found each (DMD Mining reads it)
  api/transfers.json          the large transfers of the last week
  api/richlist.json           the 100 addresses with the most DMD (the monitor's own count)
  api/coin.json               the coin's basic facts and figures in one file, for exchanges and
                              listing sites (supply, market cap, hashrate, difficulty, reward)
  api/circulating-supply.txt  the DMD in circulation, as a plain number (CoinGecko and
  api/total-supply.txt        CoinMarketCap read these); the total supply, the same number
  api/dmd-daily.csv           one line per day since block 1 (for spreadsheets)
  api/dmd-blocks.csv          one line per block (rewritten once an hour)

Hashrate is worked out from the blocks: the work they took (from the chain's total work) divided
by the time they took, over a window that grows with the range (1 hour up to the 24-hour chart, 6
hours on 7 days, a day on the longer ones), so one lucky block does not spike the chart.
Difficulty is exact. Volume is the DMD the blocks' transactions sent to other addresses (sync.py);
the market cap is the reference price × the DMD mined so far.

A chart's points are values at their time (the price, the difficulty, the hashrate over the window
before it) and counts over the step before it (blocks, volume, transactions). Its candles cover a
step each, from their time: open, high, low and close of the value over that step.
"""

from __future__ import annotations

import bisect
import csv
import io
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg

from chainwatch import __version__, chain, pools
from chainwatch.checks import PUBLIC_POOL, miner_shares
from chainwatch.sync import rich_list

DAY = 86400
RANGES = {
    # name: (span in seconds or None for everything, step, hashrate window)
    "15m": (900, 30, 3600),
    "1h": (3600, 60, 3600),
    "4h": (4 * 3600, 300, 3600),
    "24h": (DAY, 600, 3600),
    "7d": (7 * DAY, 3600, 6 * 3600),
    "30d": (30 * DAY, 4 * 3600, DAY),
    "1y": (365 * DAY, DAY, DAY),
    "all": (None, DAY, DAY),
}
SLOW_RANGES = {"30d": 600, "1y": 600, "all": 600}  # seconds between two writes
FIELDS = [
    "time",
    "price",
    "mining_cost",
    "difficulty",
    "hashrate",
    "block_seconds",
    "blocks",
    "volume",
    "market_cap",
    "transactions",
    "trading_volume_24h",
    "market_price",
]
MARKET_FRESH = 3600  # seconds: an exchange figure older than this is not shown as the current one
CANDLES = ["price", "hashrate", "difficulty", "market_cap"]


@dataclass
class Chain:
    """Every block, as columns (times made never to go back, for searching)."""

    heights: list[int]
    times: list[int]
    sorted_times: list[int]
    difficulty: list[float]
    work: list[int]
    cost: list[float]
    pool: list[bool]
    volume: list[int]  # data; -1: not known yet
    txs: list[int]  # transactions besides the coinbase
    _sums: tuple[list[int], list[int], list[int]] | None = None

    def at(self, t: int) -> int:
        """Index of the last block found at or before t (-1: none)."""
        return bisect.bisect_right(self.sorted_times, t) - 1

    def hashrate(self, t: int, window: int) -> float | None:
        i, j = self.at(t), self.at(t - window)
        if i < 0 or j < 0 or i <= j:
            return None
        return (self.work[i] - self.work[j]) / window

    def blocks_between(self, start: int, end: int) -> int:
        return max(0, self.at(end) - self.at(start))

    def sums(self, a: int, b: int) -> tuple[int | None, int]:
        """Volume (None while some is not known) and transactions of blocks a+1 to b."""
        if self._sums is None:
            vol, unknown, txs = [0], [0], [0]
            for v, n in zip(self.volume, self.txs, strict=True):
                vol.append(vol[-1] + max(v, 0))
                unknown.append(unknown[-1] + (1 if v < 0 else 0))
                txs.append(txs[-1] + n)
            self._sums = (vol, unknown, txs)
        vol, unknown, txs = self._sums
        a, b = a + 1, b + 1
        if b <= a:
            return 0, 0
        return (None if unknown[b] - unknown[a] else vol[b] - vol[a]), txs[b] - txs[a]


def load_chain(conn: psycopg.Connection[Any]) -> Chain:
    heights, times, diff, work, cost, pool, volume, txs = [], [], [], [], [], [], [], []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT height, extract(epoch FROM time)::bigint AS t, difficulty, "
            "chainwork::text AS w, mining_cost_usd::float8 AS c, pool, "
            "coalesce(volume_data, -1) AS v, tx_count FROM blocks ORDER BY height"
        )
        for r in cur:
            heights.append(r["height"])
            times.append(r["t"])
            diff.append(r["difficulty"])
            work.append(int(r["w"]))
            cost.append(r["c"])
            pool.append(r["pool"])
            volume.append(int(r["v"]))
            txs.append(max(0, r["tx_count"] - 1))
    running, top = [], 0
    for t in times:
        top = max(top, t)
        running.append(top)
    return Chain(heights, times, running, diff, work, cost, pool, volume, txs)


@dataclass
class Prices:
    times: list[int]
    values: list[float]
    rows: list[dict[str, Any]]

    def at(self, t: int) -> float | None:
        i = bisect.bisect_right(self.times, t) - 1
        return self.values[i] if i >= 0 else None

    def between(self, start: int, end: int) -> list[float]:
        """The prices recorded after start, up to end."""
        return self.values[
            bisect.bisect_right(self.times, start) : bisect.bisect_right(self.times, end)
        ]


@dataclass
class Market:
    """The exchanges' figures (market.py): empty until DMD trades on one."""

    times: list[int]
    volume: list[float | None]
    price: list[float | None]

    def at(self, t: int) -> tuple[float | None, float | None]:
        i = bisect.bisect_right(self.times, t) - 1
        if i < 0 or t - self.times[i] > MARKET_FRESH:
            return None, None
        return self.volume[i], self.price[i]


def load_market(conn: psycopg.Connection[Any]) -> Market:
    rows = conn.execute(
        "SELECT extract(epoch FROM observed_at)::bigint AS t, volume_24h_usd::float8 AS v, "
        "price_usd::float8 AS p FROM market ORDER BY observed_at"
    ).fetchall()
    return Market([r["t"] for r in rows], [r["v"] for r in rows], [r["p"] for r in rows])


def load_prices(conn: psycopg.Connection[Any]) -> Prices:
    rows = conn.execute(
        "SELECT extract(epoch FROM observed_at)::bigint AS t, usd_per_dmd::float8 AS v, "
        "observed_at, usd_per_dmd, mining_cost_usd, minimum_usd, price_from "
        "FROM prices ORDER BY observed_at"
    ).fetchall()
    return Prices([r["t"] for r in rows], [r["v"] for r in rows], rows)


def _write(path: Path, content: str) -> None:
    """Writes the file whole, then puts it in place: a reader never sees half of it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(content)
    os.chmod(temp, 0o644)
    os.replace(temp, path)


def _num(value: float | None, places: int = 6) -> float | None:
    if value is None:
        return None
    return float(f"{value:.{places}g}")


def _change(now: float | None, before: float | None) -> float | None:
    if now is None or before is None or before == 0:
        return None
    return round(now / before - 1, 4)


def _dmd(data: int | None) -> float | None:
    return None if data is None else round(data / chain.DATA_PER_DMD, 8)


def _candle(t: int, values: list[float | None]) -> list[float | None] | None:
    """[time, open, high, low, close] from the values in order (the first is the open)."""
    known = [v for v in values if v is not None]
    if not known:
        return None
    return [t, _num(known[0], 8), _num(max(known), 8), _num(min(known), 8), _num(known[-1], 8)]


def chart(c: Chain, p: Prices, name: str, now: int, m: Market | None = None) -> dict[str, Any]:
    span, step, window = RANGES[name]
    if not c.times:
        return {"range": name, "points": []}
    end = now - now % step
    first = c.sorted_times[0] if span is None else now - span
    start = max(first - first % step + step, c.sorted_times[0])
    ticks = list(range(start, end + 1, step))
    # the last point is now, so the chart reaches the latest block
    if not ticks or ticks[-1] < now:
        ticks.append(now)
    points: list[list[Any]] = []
    candles: dict[str, list[list[float | None]]] = {k: [] for k in CANDLES}
    previous = ticks[0] - step
    for t in ticks:
        i, before = c.at(t), c.at(previous)
        if i < 0:
            previous = t
            continue
        rate = c.hashrate(t, window)
        count = c.blocks_between(t - window, t)
        volume, transactions = c.sums(before, i)
        price = p.at(t)
        traded, market_price = m.at(t) if m else (None, None)
        supply = chain.coins_mined_data(c.heights[i]) / chain.DATA_PER_DMD
        points.append(
            [
                t,
                _num(price),
                _num(c.cost[i]),
                _num(c.difficulty[i], 8),
                _num(rate),
                round(window / count, 1) if count else None,
                i - max(before, -1),
                _dmd(volume),
                _num(price * supply) if price is not None else None,
                transactions,
                _num(traded),
                _num(market_price),
            ]
        )
        # the candles of (previous, t], at previous
        inside = range(max(before, 0) + (1 if before >= 0 else 0), i + 1)
        diffs: list[float | None] = [
            c.difficulty[max(before, 0)],
            *(c.difficulty[k] for k in inside),
        ]
        moments = sorted(
            {previous, t}
            | {c.times[k] for k in inside if previous < c.times[k] <= t}
            | {
                c.times[k] + window
                for k in range(max(0, c.at(previous - window)), max(0, c.at(t - window) + 1))
                if previous < c.times[k] + window <= t
            }
        )
        rates: list[float | None] = [c.hashrate(m, window) for m in moments]
        prices: list[float | None] = [p.at(previous), *p.between(previous, t)]
        if price is not None and (len(prices) < 2 or prices[-1] != price):
            prices.append(price)
        series: tuple[tuple[str, list[float | None]], ...] = (
            ("price", prices),
            ("hashrate", rates),
            ("difficulty", diffs),
            ("market_cap", [None if v is None else v * supply for v in prices]),
        )
        for key, values in series:
            candle = _candle(previous, values)
            if candle:
                candles[key].append(candle)
        previous = t
    return {
        "range": name,
        "step": step,
        "hashrate_window": window,
        "fields": FIELDS,
        "points": points,
        "candle_fields": ["time", "open", "high", "low", "close"],
        "candles": candles,
    }


def summary(
    conn: psycopg.Connection[Any],
    c: Chain,
    p: Prices,
    now: int,
    status: dict[str, Any],
    m: Market | None = None,
    market_source: str = "",
) -> dict[str, Any]:
    i = len(c.times) - 1
    traded, market_price = m.at(now) if m else (None, None)
    traded_day, _ = m.at(now - DAY) if m else (None, None)
    day_ago = c.at(now - DAY)
    height = c.heights[i]
    rate_1h = c.hashrate(now, 3600)
    rate_24h = c.hashrate(now, DAY)
    rate_prev = c.hashrate(now - DAY, DAY)
    blocks_24h = c.blocks_between(now - DAY, now)
    latest = p.rows[-1] if p.rows else None
    price_day = p.at(now - DAY)
    tip = conn.execute("SELECT hash, time FROM blocks WHERE height = %s", (height,)).fetchone()
    # the highest daily average hashrate (all-time high)
    best, best_day = 0.0, None
    if c.times:
        day = c.sorted_times[0] - c.sorted_times[0] % DAY + DAY
        while day <= now:
            r = c.hashrate(day, DAY)
            if r and r > best:
                best, best_day = r, datetime.fromtimestamp(day - DAY, UTC).date().isoformat()
            day += DAY
    pool_24h = sum(1 for k in range(max(0, c.at(now - DAY) + 1), i + 1) if c.pool[k])
    subsidy = chain.subsidy_data(height + 1)
    volume_24h, transactions_24h = c.sums(c.at(now - DAY), i)
    volume_prev, _ = c.sums(c.at(now - 2 * DAY), c.at(now - DAY))
    mined = chain.coins_mined_data(height) / chain.DATA_PER_DMD
    cap = float(latest["usd_per_dmd"]) * mined if latest else None
    cap_day = (
        price_day * chain.coins_mined_data(c.heights[day_ago]) / chain.DATA_PER_DMD
        if price_day is not None and day_ago >= 0
        else None
    )
    return {
        "updated_at": datetime.fromtimestamp(now, UTC).isoformat(),
        "height": height,
        "last_block": {"hash": tip["hash"], "time": tip["time"].isoformat()} if tip else None,
        "difficulty": c.difficulty[i],
        "difficulty_change_24h": _change(c.difficulty[i], c.difficulty[day_ago])
        if day_ago >= 0
        else None,
        "hashrate_1h": _num(rate_1h),
        "hashrate_24h": _num(rate_24h),
        "hashrate_change_24h": _change(rate_24h, rate_prev),
        "hashrate_ath": {"hashrate": _num(best), "day": best_day} if best_day else None,
        "blocks_24h": blocks_24h,
        "block_seconds_24h": round(DAY / blocks_24h, 1) if blocks_24h else None,
        "pool_share_24h": round(pool_24h / blocks_24h, 4) if blocks_24h else None,
        "mining_cost_usd": _num(c.cost[i]),
        "mining_cost_change_24h": _change(c.cost[i], c.cost[day_ago]) if day_ago >= 0 else None,
        "reference_price": {
            "usd_per_dmd": float(latest["usd_per_dmd"]),
            "price_from": latest["price_from"],
            "minimum_usd": float(latest["minimum_usd"]) if latest["minimum_usd"] else None,
            "mining_cost_usd": float(latest["mining_cost_usd"])
            if latest["mining_cost_usd"] is not None
            else None,
            "observed_at": latest["observed_at"].isoformat(),
            "change_24h": _change(float(latest["usd_per_dmd"]), price_day),
            "since": datetime.fromtimestamp(p.times[0], UTC).isoformat(),
        }
        if latest
        else None,
        "market_cap_usd": _num(cap),
        "market_cap_change_24h": _change(cap, cap_day),
        "volume_24h_dmd": _dmd(volume_24h),
        "volume_change_24h": _change(
            None if volume_24h is None else float(volume_24h),
            None if volume_prev is None else float(volume_prev),
        ),
        "transactions_24h": transactions_24h,
        "block_reward_dmd": subsidy / chain.DATA_PER_DMD,
        "next_halving_height": (height // chain.HALVING_BLOCKS + 1) * chain.HALVING_BLOCKS,
        "coins_mined_dmd": mined,
        "market": {
            "listed": bool(m and m.times),
            "source": market_source or None,
            "trading_volume_24h_usd": _num(traded),
            "trading_volume_change_24h": _change(traded, traded_day),
            "price_usd": _num(market_price),
            "observed_at": datetime.fromtimestamp(m.times[-1], UTC).isoformat()
            if m and m.times
            else None,
        },
        "status": status,
        "monitor": f"DebitMyData chain watch {__version__}",
    }


def miners(conn: psycopg.Connection[Any], now: datetime) -> dict[str, Any]:
    # (A pool of DebitMyData's list by its slug, for its logo: the DebitMyData pool is its
    # "official".)
    listed = pools.current().by_name()
    official = next((p for p in listed.values() if p.slug == "official"), None)
    if official is not None:
        listed[PUBLIC_POOL] = official
    out = {}
    for name, days in (("24h", 1), ("7d", 7), ("30d", 30)):
        shares = miner_shares(conn, since=now - timedelta(days=days))
        total = shares["blocks"] or 1
        out[name] = {
            "blocks": shares["blocks"],
            "miners": [
                {
                    "name": who,
                    "blocks": n,
                    "share": round(n / total, 4),
                    "pool": who == PUBLIC_POOL,
                    **({"slug": listed[who].slug} if who in listed else {}),
                }
                for who, n in sorted(shares["miners"].items(), key=lambda x: -x[1])
            ],
        }
    return out


LATEST_BLOCKS = 1000


def latest_blocks(conn: psycopg.Connection[Any], count: int = LATEST_BLOCKS) -> dict[str, Any]:
    """The last blocks, newest first, each with who found it as the miners list names them
    (miners.json): DMD Mining (mining.debitmydata.io) follows the pools with it."""
    listed = pools.current().by_name()
    official = next((p for p in listed.values() if p.slug == "official"), None)
    if official is not None:
        listed[PUBLIC_POOL] = official
    rows = conn.execute(
        "SELECT height, time, hash, difficulty, tx_count, size, pool, coinbase_tag, miner, "
        "fees_data FROM blocks ORDER BY height DESC LIMIT %s",
        (count,),
    ).fetchall()
    out = []
    for r in rows:
        who = r["miner"] or ""
        fees = r["fees_data"]
        reward = chain.subsidy_data(r["height"]) + (fees or 0)
        out.append(
            {
                "height": r["height"],
                "time": int(r["time"].timestamp()),
                "hash": r["hash"],
                "difficulty": float(r["difficulty"]),
                "miner": who,
                "slug": listed[who].slug if who in listed else None,
                "pool": bool(r["pool"]),
                "coinbase_text": r["coinbase_tag"] or "",
                "transactions": r["tx_count"],
                "size": r["size"],
                "reward_dmd": _dmd(reward) if fees is not None else None,
            }
        )
    return {"blocks": out, "count": len(out)}


def daily_csv(c: Chain, p: Prices, now: int) -> str:
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(
        [
            "date_utc",
            "first_block",
            "last_block",
            "blocks",
            "average_block_seconds",
            "difficulty_close",
            "hashrate_hs",
            "mining_cost_usd_close",
            "reference_price_usd_close",
            "pool_blocks",
            "volume_dmd",
            "transactions",
            "market_cap_usd_close",
        ]
    )
    if not c.times:
        return out.getvalue()
    day = c.sorted_times[0] - c.sorted_times[0] % DAY
    while day < now - now % DAY:
        end = day + DAY
        i, j = c.at(end), c.at(day)
        n = i - j
        if n > 0 and i >= 0:
            rate = c.hashrate(end, DAY)
            pool_n = sum(1 for k in range(j + 1, i + 1) if c.pool[k])
            price = p.at(end)
            volume, transactions = c.sums(j, i)
            cap = chain.market_cap_usd(price, c.heights[i])
            w.writerow(
                [
                    datetime.fromtimestamp(day, UTC).date().isoformat(),
                    c.heights[j + 1],
                    c.heights[i],
                    n,
                    round(DAY / n, 1),
                    c.difficulty[i],
                    f"{rate:.0f}" if rate else "",
                    f"{c.cost[i]:.15f}".rstrip("0").rstrip("."),
                    "" if price is None else f"{Decimal(str(price)):f}",
                    pool_n,
                    "" if volume is None else f"{Decimal(volume) / chain.DATA_PER_DMD:f}",
                    transactions,
                    "" if cap is None else f"{cap:.2f}",
                ]
            )
        day = end
    return out.getvalue()


def blocks_csv(conn: psycopg.Connection[Any]) -> str:
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(
        [
            "height",
            "time_utc",
            "hash",
            "difficulty",
            "transactions",
            "size_bytes",
            "pool",
            "coinbase_text",
            "mining_cost_usd",
            "miner",
            "volume_dmd",
            "fees_dmd",
        ]
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT height, time, hash, difficulty, tx_count, size, pool, coinbase_tag, "
            "mining_cost_usd, miner, volume_data, fees_data FROM blocks ORDER BY height"
        )
        for r in cur:
            w.writerow(
                [
                    r["height"],
                    r["time"].strftime("%Y-%m-%dT%H:%M:%SZ"),
                    r["hash"],
                    r["difficulty"],
                    r["tx_count"],
                    r["size"],
                    1 if r["pool"] else 0,
                    r["coinbase_tag"],
                    format(r["mining_cost_usd"].normalize(), "f"),
                    r["miner"] or "",
                    ""
                    if r["volume_data"] is None or r["volume_data"] < 0
                    else f"{Decimal(r['volume_data']) / chain.DATA_PER_DMD:f}",
                    ""
                    if r["fees_data"] is None
                    else f"{Decimal(r['fees_data']) / chain.DATA_PER_DMD:f}",
                ]
            )
    return out.getvalue()


def transfers(conn: psycopg.Connection[Any], now: datetime, floor: float) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT txid, height, time, value_data, fee_data FROM transfers WHERE time > %s "
        "ORDER BY height DESC, value_data DESC LIMIT 200",
        (now - timedelta(days=7),),
    ).fetchall()
    return {
        "from_dmd": floor,
        "days": 7,
        "transfers": [
            {
                "txid": r["txid"],
                "height": r["height"],
                "time": r["time"].isoformat(),
                "dmd": _dmd(r["value_data"]),
                "fee_dmd": _dmd(r["fee_data"]),
            }
            for r in rows
        ],
    }


# Every cycle's 32 halvings, block by block: 20,999,999.964 DMD.
SUPPLY_PER_CYCLE_DATA = sum(
    (chain.INITIAL_SUBSIDY_DATA >> k) * chain.HALVING_BLOCKS
    for k in range(chain.SUBSIDY_CYCLE_BLOCKS // chain.HALVING_BLOCKS)
)


def plain_dmd(data: int) -> str:
    """An amount as a plain number of DMD, the way listing sites read a supply: no exponent, no
    thousands separator, no trailing zeros (5335155, or 5335155.5)."""
    whole, rest = divmod(data, chain.DATA_PER_DMD)
    return f"{whole}.{rest:08d}".rstrip("0") if rest else str(whole)


def coin(s: dict[str, Any]) -> dict[str, Any]:
    """The coin's basic facts and live figures in one file, from the summary, for exchanges,
    listing sites and anyone who wants them without the stats page's other fields.

    The supply is every block's reward since block 1 (DMD had no pre-mine and nothing is
    locked, so the circulating and total supplies are the same). There is no maximum: the
    reward starts a new cycle after 32 halvings, so ``max_dmd`` is null and ``per_cycle_dmd``
    says how much one cycle makes. The market cap uses the market price once DMD trades on an
    exchange, and the reference price until then (``market_cap_from`` says which), to the cent
    (the summary's figures keep 6 significant digits, enough for the page)."""
    mined = s["coins_mined_dmd"]
    market = s["market"]
    reference = s["reference_price"]
    if market["listed"] and market["price_usd"] is not None:
        cap, cap_from = round(market["price_usd"] * mined, 2), "market_price"
    elif reference:
        cap, cap_from = round(reference["usd_per_dmd"] * mined, 2), "reference_price"
    else:
        cap, cap_from = None, None
    return {
        "updated_at": s["updated_at"],
        "name": "DebitMyData PoW",
        "ticker": "DMD",
        "algorithm": "SHA-256",
        "decimals": 8,
        "block_time_seconds": chain.BLOCK_SECONDS,
        "coinbase_maturity_blocks": chain.COINBASE_MATURITY,
        "height": s["height"],
        "last_block": s["last_block"],
        "difficulty": s["difficulty"],
        "hashrate": s["hashrate_24h"],
        "block_reward_dmd": s["block_reward_dmd"],
        "next_halving_height": s["next_halving_height"],
        "blocks_to_next_halving": s["next_halving_height"] - s["height"],
        "supply": {
            "circulating_dmd": mined,
            "total_dmd": mined,
            "max_dmd": None,
            "per_cycle_dmd": SUPPLY_PER_CYCLE_DATA / chain.DATA_PER_DMD,
            "halving_blocks": chain.HALVING_BLOCKS,
            "cycle_blocks": chain.SUBSIDY_CYCLE_BLOCKS,
        },
        "price": {
            "listed": market["listed"],
            "market_usd": market["price_usd"],
            "reference_usd": reference["usd_per_dmd"] if reference else None,
            "reference_from": reference["price_from"] if reference else None,
        },
        "market_cap_usd": cap,
        "market_cap_from": cap_from,
        "trading_volume_24h_usd": market["trading_volume_24h_usd"],
        "chain_normal": s["status"]["normal"],
        "links": {
            "website": "https://debitmydata.io/",
            "docs": "https://docs.debitmydata.io/api/coin-data/",
            "rich_list": "https://stats.debitmydata.io/api/richlist.json",
            "explorers": [
                "https://mempool.debitmydata.io/",
                "https://chain.debitmydata.io/",
                "https://explorer.debitmydata.io/",
            ],
            "source_code": "https://github.com/DebitMyData-PoW/DebitMyData-PoW-Core",
        },
    }


def richlist(conn: psycopg.Connection[Any], pool_addresses: list[str]) -> dict[str, Any]:
    """The 100 addresses with the most DMD, from the monitor's own count of every block."""
    rows = rich_list(conn, 100)
    pending = conn.execute("SELECT count(*) AS n FROM blocks WHERE volume_data IS NULL").fetchone()
    tip = conn.execute("SELECT max(height) AS h FROM blocks").fetchone()
    mined = chain.coins_mined_data(tip["h"] or 0) if tip else 0
    pools = set(pool_addresses)
    return {
        "ready": bool(rows),
        "blocks_left_to_count": pending["n"] if pending else 0,
        "height": tip["h"] if tip else None,
        "addresses": [
            {
                "rank": n,
                "address": r["address"],
                "dmd": _dmd(r["balance_data"]),
                "share_of_mined": round(r["balance_data"] / mined, 6) if mined else None,
                "pool": r["address"] in pools,
            }
            for n, r in enumerate(rows, start=1)
        ],
    }


def public_status(conn: psycopg.Connection[Any]) -> dict[str, Any]:
    """What the public page says about the chain's health: open alerts about the chain itself
    (blocks stopped, a fork between views), by title only."""
    rows = conn.execute(
        "SELECT title, severity FROM alerts WHERE resolved_at IS NULL AND kind = 'condition' "
        "AND (key LIKE 'stall.%%' OR key LIKE 'views.%%.block') ORDER BY severity, first_seen"
    ).fetchall()
    return {"normal": not rows, "notes": [r["title"] for r in rows]}


def publish(
    conn: psycopg.Connection[Any],
    folder: Path,
    now: datetime,
    blocks_csv_too: bool,
    large_transfer_dmd: float = 1000,
    market_source: str = "",
    pool_addresses: list[str] | None = None,
) -> Chain:
    t = int(now.timestamp())
    c = load_chain(conn)
    if not c.times:
        return c
    p = load_prices(conn)
    m = load_market(conn)
    api = folder / "api"
    for name in RANGES:
        path = api / f"chart-{name}.json"
        every = SLOW_RANGES.get(name)
        if every and not blocks_csv_too and path.exists() and t - path.stat().st_mtime < every:
            continue
        _write(path, json.dumps(chart(c, p, name, t, m), separators=(",", ":")))
    s = summary(conn, c, p, t, public_status(conn), m, market_source)
    _write(api / "summary.json", json.dumps(s, indent=1))
    _write(api / "coin.json", json.dumps(coin(s), indent=1))
    mined = plain_dmd(chain.coins_mined_data(s["height"]))
    _write(api / "circulating-supply.txt", mined)
    _write(api / "total-supply.txt", mined)
    _write(api / "miners.json", json.dumps(miners(conn, now), indent=1))
    _write(api / "blocks.json", json.dumps(latest_blocks(conn), separators=(",", ":")))
    _write(api / "transfers.json", json.dumps(transfers(conn, now, large_transfer_dmd), indent=1))
    _write(api / "richlist.json", json.dumps(richlist(conn, pool_addresses or []), indent=1))
    _write(api / "dmd-daily.csv", daily_csv(c, p, t))
    if blocks_csv_too or not (api / "dmd-blocks.csv").exists():
        _write(api / "dmd-blocks.csv", blocks_csv(conn))
    return c


# --------------------------------------------------------------------------- Prometheus


def metrics(
    conn: psycopg.Connection[Any],
    path: Path,
    now: datetime,
    node: dict[str, float],
    c: Chain | None,
) -> None:
    """The node_exporter textfile: Prometheus reads it with this server's other figures."""
    lines: list[str] = []

    def gauge(name: str, value: float | None, help_text: str, labels: str = "") -> None:
        if value is None:
            return
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} gauge")
        lines.append(f"{name}{labels} {value}")

    t = int(now.timestamp())
    gauge("chainwatch_last_run_timestamp_seconds", t, "When the chain watch last ran.")
    if c and c.times:
        i = len(c.times) - 1
        gauge("dmd_chain_height", c.heights[i], "Height of the chain's last block.")
        gauge("dmd_chain_last_block_timestamp_seconds", c.times[i], "Time of the last block.")
        gauge("dmd_chain_difficulty", c.difficulty[i], "Difficulty of the last block.")
        gauge("dmd_chain_hashrate_1h", c.hashrate(t, 3600), "Hashes per second, last hour.")
        gauge("dmd_chain_hashrate_24h", c.hashrate(t, DAY), "Hashes per second, last day.")
        gauge("dmd_chain_blocks_24h", c.blocks_between(t - DAY, t), "Blocks in the last day.")
        gauge("dmd_mining_cost_usd", c.cost[i], "Mining cost of 1 DMD in USD.")
    row = conn.execute(
        "SELECT usd_per_dmd::float8 AS v, extract(epoch FROM observed_at) AS t FROM prices "
        "ORDER BY observed_at DESC LIMIT 1"
    ).fetchone()
    if row:
        gauge("dmd_reference_price_usd", row["v"], "The gateway's DMD reference price.")
        gauge("dmd_reference_price_timestamp_seconds", int(row["t"]), "When it was computed.")
    helps = {
        "peers": "Peers of this server's DebitMyData node.",
        "initial_download": "1 while this server's node is still syncing.",
        "headers": "Best header height this server's node knows.",
    }
    for key, text in helps.items():
        if key in node:
            gauge(f"dmd_node_{key}", node[key], text)
    open_alerts = conn.execute(
        "SELECT severity, count(*) AS n FROM alerts WHERE resolved_at IS NULL "
        "AND kind = 'condition' GROUP BY severity"
    ).fetchall()
    counts = {r["severity"]: r["n"] for r in open_alerts}
    lines.append("# HELP chainwatch_alerts_open Open alerts of the chain watch.")
    lines.append("# TYPE chainwatch_alerts_open gauge")
    for severity in ("critical", "warning"):
        lines.append(f'chainwatch_alerts_open{{severity="{severity}"}} {counts.get(severity, 0)}')
    _write(path, "\n".join(lines) + "\n")
