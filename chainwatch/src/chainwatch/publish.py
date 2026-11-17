"""The public stats page's files, and the numbers Prometheus reads.

The stats page (stats.debitmydata.io) is static: nginx serves these files from the public folder,
and Cloudflare caches them for a minute. Nothing on that site runs code on the server.

  api/summary.json            the latest figures, with their changes over a day
  api/chart-<range>.json      the chart points of a range: 24h, 7d, 30d, 1y, all
  api/miners.json             who found the last day's and week's blocks
  api/dmd-daily.csv           one line per day since block 1 (for spreadsheets)
  api/dmd-blocks.csv          one line per block (rewritten once an hour)

Hashrate is worked out from the blocks: the work they took (from the chain's total work) divided
by the time they took, over a window that grows with the range (1 hour on the 24-hour chart, a
day on the long ones), so one lucky block does not spike the chart. Difficulty is exact.
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

from chainwatch import __version__, chain
from chainwatch.checks import PUBLIC_POOL, miner_shares

DAY = 86400
RANGES = {
    # name: (span in seconds or None for everything, step, hashrate window)
    "24h": (DAY, 600, 3600),
    "7d": (7 * DAY, 3600, 6 * 3600),
    "30d": (30 * DAY, 4 * 3600, DAY),
    "1y": (365 * DAY, DAY, DAY),
    "all": (None, DAY, DAY),
}


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


def load_chain(conn: psycopg.Connection[Any]) -> Chain:
    heights, times, diff, work, cost, pool = [], [], [], [], [], []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT height, extract(epoch FROM time)::bigint AS t, difficulty, "
            "chainwork::text AS w, mining_cost_usd::float8 AS c, pool FROM blocks ORDER BY height"
        )
        for r in cur:
            heights.append(r["height"])
            times.append(r["t"])
            diff.append(r["difficulty"])
            work.append(int(r["w"]))
            cost.append(r["c"])
            pool.append(r["pool"])
    running, top = [], 0
    for t in times:
        top = max(top, t)
        running.append(top)
    return Chain(heights, times, running, diff, work, cost, pool)


@dataclass
class Prices:
    times: list[int]
    values: list[float]
    rows: list[dict[str, Any]]

    def at(self, t: int) -> float | None:
        i = bisect.bisect_right(self.times, t) - 1
        return self.values[i] if i >= 0 else None


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


def chart(c: Chain, p: Prices, name: str, now: int) -> dict[str, Any]:
    span, step, window = RANGES[name]
    if not c.times:
        return {"range": name, "points": []}
    end = now - now % step
    first = c.sorted_times[0] if span is None else now - span
    start = max(first - first % step + step, c.sorted_times[0])
    points = []
    t = start
    while t <= end:
        i = c.at(t)
        if i >= 0:
            rate = c.hashrate(t, window)
            count = c.blocks_between(t - window, t)
            points.append(
                [
                    t,
                    _num(p.at(t)),
                    _num(c.cost[i]),
                    _num(c.difficulty[i], 8),
                    _num(rate),
                    round(window / count, 1) if count else None,
                    c.blocks_between(t - step, t),
                ]
            )
        t += step
    # the last point is now, so the chart reaches the latest block
    if points and points[-1][0] < now:
        i = len(c.times) - 1
        rate = c.hashrate(now, window)
        count = c.blocks_between(now - window, now)
        points.append(
            [
                now,
                _num(p.at(now)),
                _num(c.cost[i]),
                _num(c.difficulty[i], 8),
                _num(rate),
                round(window / count, 1) if count else None,
                c.blocks_between(now - step, now),
            ]
        )
    return {
        "range": name,
        "step": step,
        "hashrate_window": window,
        "fields": [
            "time",
            "price",
            "mining_cost",
            "difficulty",
            "hashrate",
            "block_seconds",
            "blocks",
        ],
        "points": points,
    }


def summary(
    conn: psycopg.Connection[Any], c: Chain, p: Prices, now: int, status: dict[str, Any]
) -> dict[str, Any]:
    i = len(c.times) - 1
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
        "block_reward_dmd": subsidy / chain.DATA_PER_DMD,
        "next_halving_height": (height // chain.HALVING_BLOCKS + 1) * chain.HALVING_BLOCKS,
        "coins_mined_dmd": chain.coins_mined_data(height) / chain.DATA_PER_DMD,
        "status": status,
        "monitor": f"DebitMyData chain watch {__version__}",
    }


def miners(conn: psycopg.Connection[Any], now: datetime) -> dict[str, Any]:
    out = {}
    for name, days in (("24h", 1), ("7d", 7)):
        shares = miner_shares(conn, since=now - timedelta(days=days))
        total = shares["blocks"] or 1
        out[name] = {
            "blocks": shares["blocks"],
            "miners": [
                {"name": who, "blocks": n, "share": round(n / total, 4), "pool": who == PUBLIC_POOL}
                for who, n in sorted(shares["miners"].items(), key=lambda x: -x[1])
            ],
        }
    return out


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
        ]
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT height, time, hash, difficulty, tx_count, size, pool, coinbase_tag, "
            "mining_cost_usd FROM blocks ORDER BY height"
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
                ]
            )
    return out.getvalue()


def public_status(conn: psycopg.Connection[Any]) -> dict[str, Any]:
    """What the public page says about the chain's health: open alerts about the chain itself
    (blocks stopped, a fork between views), by title only."""
    rows = conn.execute(
        "SELECT title, severity FROM alerts WHERE resolved_at IS NULL AND kind = 'condition' "
        "AND (key LIKE 'stall.%%' OR key LIKE 'views.%%.block') ORDER BY severity, first_seen"
    ).fetchall()
    return {"normal": not rows, "notes": [r["title"] for r in rows]}


def publish(
    conn: psycopg.Connection[Any], folder: Path, now: datetime, blocks_csv_too: bool
) -> Chain:
    t = int(now.timestamp())
    c = load_chain(conn)
    if not c.times:
        return c
    p = load_prices(conn)
    api = folder / "api"
    for name in RANGES:
        _write(api / f"chart-{name}.json", json.dumps(chart(c, p, name, t), separators=(",", ":")))
    _write(api / "summary.json", json.dumps(summary(conn, c, p, t, public_status(conn)), indent=1))
    _write(api / "miners.json", json.dumps(miners(conn, now), indent=1))
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
