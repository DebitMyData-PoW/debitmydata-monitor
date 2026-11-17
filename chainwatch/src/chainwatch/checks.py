"""The checks, run every minute. Each returns what it finds (alerts.Finding); the key's first part
is the check's name.

  node        this server's DebitMyData node: on the main chain, synced, with peers
  stall       no new block for 30 minutes (warning) or an hour (critical)
  views       the other views of the chain have the same block as this node, 2 blocks below the
              lower of the two tips: the public explorer, the mempool explorer, and the pool's
              node (through the pool agent); different blocks mean one of them is on another
              chain (a fork), reported after 2 checks in a row since two new blocks racing at
              the tip settle within a minute or two
  forks       the node's own branches (getchaintips) from the last 60 blocks: a reorganization
              of 2 blocks or more (critical from 6, the confirmations DMD payments need), a
              competing branch, invalid blocks; each one is sent once
  hashrate    the last hour's hashrate 3 times the last day's, or a third of it
  difficulty  the difficulty 25% higher or lower than a day before
  miners      one miner outside the pool with a quarter of the last 144 blocks (6 hours), and one
              address with half of the pool's payouts in them
  price       the gateway's reference price still updating; a change of 5% or more
  server      the Prometheus alerts about the servers (disks, memory, the pool server)
  daily       the day's summary for the DMD community, after midnight UTC
"""

from __future__ import annotations

import json
import logging
import re
import statistics
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import psycopg

from chainwatch import db
from chainwatch.alerts import CRITICAL, INFO, WARNING, Finding
from chainwatch.config import Settings
from chainwatch.price import USER_AGENT
from chainwatch.rpc import Rpc

log = logging.getLogger(__name__)

HASH = re.compile(r"[0-9a-f]{64}")
COMPARE_DEPTH = 2
COMPARE_ALERT_AFTER = 2
HEIGHT_TOLERANCE = 3
UNREACHABLE_ALERT_AFTER = 15  # checks (minutes) a view may be unreachable before an alert
BRANCH_ALERT_BLOCKS = 2
RECENT_BRANCH_BLOCKS = 60
DMD_CONFIRMATIONS = 6
MINER_WINDOW = 144  # 6 hours of blocks
EVENT_COOLDOWN = timedelta(hours=6)
PRICE_STALE = timedelta(minutes=30)
PUBLIC_POOL = "the DebitMyData pool"


@dataclass
class Context:
    conn: psycopg.Connection[Any]
    rpc: Rpc
    settings: Settings
    now: datetime
    fetch_text: Callable[[str], str] = field(default=lambda url: _fetch_text(url))
    prometheus: Callable[[str, str], Any] = field(
        default=lambda base, path: _prometheus(base, path)
    )

    def state(self, key: str) -> Any:
        return db.get_state(self.conn, key)

    def put(self, key: str, value: Any) -> None:
        db.put_state(self.conn, key, value, self.now)


def _fetch_text(url: str, timeout: float = 15) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read(4096).decode("utf-8", "replace").strip()


def _prometheus(base: str, path: str) -> Any:
    request = urllib.request.Request(base + path, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read())


def _ths(hashes_per_second: float) -> str:
    units = [(1e18, "EH/s"), (1e15, "PH/s"), (1e12, "TH/s"), (1e9, "GH/s"), (1e6, "MH/s")]
    for size, unit in units:
        if hashes_per_second >= size:
            return f"{hashes_per_second / size:,.2f} {unit}"
    return f"{hashes_per_second:,.0f} H/s"


def _usd(value: Decimal | float | None) -> str:
    if value is None:
        return "–"
    v = Decimal(str(value))
    if v >= 1:
        return f"${v:,.2f}"
    return "$" + format(v.quantize(Decimal("1e-8")).normalize(), "f")


# --------------------------------------------------------------------------- node and stall


def check_node(ctx: Context) -> list[Finding]:
    info = ctx.rpc.call("getblockchaininfo")
    findings = []
    if info.get("chain") != "main":
        findings.append(
            Finding(
                "node.chain",
                CRITICAL,
                "The monitor's DebitMyData node is not on the main chain",
                f"It runs on {info.get('chain')!r}. Check /etc/debitmydata/debitmydata.conf.",
            )
        )
    if info.get("initialblockdownload"):
        findings.append(
            Finding(
                "node.syncing",
                WARNING,
                "The monitor's DebitMyData node is still syncing",
                f"Block {info.get('blocks')} of {info.get('headers')}. The checks that compare "
                "blocks wait until it is done.",
            )
        )
    peers = int(ctx.rpc.call("getconnectioncount"))
    if peers == 0:
        findings.append(
            Finding(
                "node.peers",
                WARNING,
                "The monitor's DebitMyData node has no peers",
                "It sees no other DebitMyData node, so it cannot see new blocks. Check its network "
                "(journalctl -u debitmydata) and the seed nodes.",
            )
        )
    return findings


def check_stall(ctx: Context) -> list[Finding]:
    info = ctx.rpc.call("getblockchaininfo")
    if info.get("initialblockdownload"):
        return []
    header = ctx.rpc.call("getblockheader", info["bestblockhash"])
    last = datetime.fromtimestamp(int(header["time"]), UTC)
    minutes = int((ctx.now - last).total_seconds() // 60)
    if minutes < ctx.settings.stall_warning_minutes:
        return []
    severity = CRITICAL if minutes >= ctx.settings.stall_critical_minutes else WARNING
    return [
        Finding(
            "stall.blocks",
            severity,
            f"No new DebitMyData block for {minutes} minutes",
            f"The last block is {info['blocks']}, at {last:%H:%M} UTC; a block comes every 2.5 "
            "minutes on average. Most of the chain's hashrate is the pool's: check the pool's "
            "stratum and its node (the pool server's alerts), then whether other nodes see newer "
            "blocks (the explorers).",
        )
    ]


# --------------------------------------------------------------------------- other views


@dataclass
class View:
    name: str
    label: str
    height: Callable[[], int]
    # the height to compare at (None: not this time), from this node's and the view's heights
    compare_at: Callable[[int, int], int | None]
    block: Callable[[int], str]  # its block at that height: the hash, or its first 13 digits


def _views(ctx: Context) -> list[View]:
    s = ctx.settings

    def below_both(ours: int, theirs: int) -> int | None:
        height = min(ours, theirs) - COMPARE_DEPTH
        return height if height >= 1 else None

    views = []
    if s.explorer_url:
        views.append(
            View(
                "explorer",
                "the public explorer (explorer.debitmydata.io)",
                lambda: int(ctx.fetch_text(f"{s.explorer_url}/api/getblockcount")),
                below_both,
                lambda h: ctx.fetch_text(f"{s.explorer_url}/api/getblockhash?index={h}").lower(),
            )
        )
    if s.mempool_url:
        views.append(
            View(
                "mempool",
                "the mempool explorer (mempool.debitmydata.io)",
                lambda: int(ctx.fetch_text(f"{s.mempool_url}/api/blocks/tip/height")),
                below_both,
                lambda h: ctx.fetch_text(f"{s.mempool_url}/api/block-height/{h}").lower(),
            )
        )

    def pool_compare_at(ours: int, theirs: int) -> int | None:
        height = _pool_metric(ctx, "dmd_pool_node_compare_height")
        return height if 1 <= height <= ours else None  # ahead of this node: next time

    if not s.pool_view:
        return views
    views.append(
        View(
            "pool",
            "the pool's node",
            lambda: _pool_metric(ctx, "dmd_pool_node_blocks"),
            pool_compare_at,
            lambda h: _pool_block(ctx, h),
        )
    )
    return views


def _pool_metric(ctx: Context, name: str) -> int:
    answer = ctx.prometheus(
        ctx.settings.prometheus_url, "/api/v1/query?" + urllib.parse.urlencode({"query": name})
    )
    result = answer.get("data", {}).get("result", [])
    if not result:
        raise LookupError("no figures from the pool agent (is the tunnel up?)")
    return int(float(result[0]["value"][1]))


def _pool_block(ctx: Context, height: int) -> str:
    """The pool agent gives the first 13 hex digits of its block 2 below its tip (a whole hash
    does not fit in a Prometheus number), and that block's height."""
    if _pool_metric(ctx, "dmd_pool_node_compare_height") != height:
        raise LookupError("the pool's node moved to a new block while being read")
    return format(_pool_metric(ctx, "dmd_pool_node_compare_prefix"), "013x")


def check_views(ctx: Context) -> list[Finding]:
    info = ctx.rpc.call("getblockchaininfo")
    if info.get("initialblockdownload"):
        return []
    ours_height = int(info["blocks"])
    findings: list[Finding] = []
    state = ctx.state("views") or {}
    for view in _views(ctx):
        old = state.get(view.name, {})
        try:
            theirs_height = view.height()
            height = view.compare_at(ours_height, theirs_height)
            theirs = ours = ""
            if height is not None:
                theirs = view.block(height)
                ours = str(ctx.rpc.call("getblockhash", height)).lower()
        except (urllib.error.URLError, OSError, ValueError, LookupError, TimeoutError) as exc:
            misses = int(old.get("misses", 0)) + 1
            state[view.name] = {**old, "misses": misses}
            log.info("view %s: %s", view.name, exc)
            if misses >= UNREACHABLE_ALERT_AFTER:
                findings.append(
                    Finding(
                        f"views.{view.name}.unreachable",
                        WARNING,
                        f"The monitor cannot read {view.label}",
                        f"For {misses} minutes: {str(exc)[:200]}. Its blocks are not compared "
                        "until it answers again.",
                    )
                )
            # what was found before still stands until it can be checked again
            findings.extend(Finding(**old[k]) for k in ("open_block", "open_height") if old.get(k))
            continue
        record: dict[str, Any] = {"misses": 0, "differ": int(old.get("differ", 0))}
        if height is None:
            if old.get("open_block"):
                record["open_block"] = old["open_block"]
                findings.append(Finding(**old["open_block"]))
        else:
            if view.name == "pool":
                same = len(theirs) == 13 and ours.startswith(theirs)
            else:
                same = ours == theirs or not HASH.fullmatch(theirs)  # not a hash: next time
            record["differ"] = 0 if same else record["differ"] + 1
            if record["differ"] >= COMPARE_ALERT_AFTER:
                f = Finding(
                    f"views.{view.name}.block",
                    CRITICAL,
                    f"The monitor's node and {view.label} are on different chains",
                    f"At block {height}, this node has {ours} and {view.label} "
                    f"{theirs}{'…' if view.name == 'pool' else ''} ({record['differ']} checks "
                    f"in a row). One of them follows another chain since a block below {height}. "
                    "Compare with the other views on the monitoring page: the chain the pool "
                    "mines has most of the hashrate. If the gateway's node is on the other "
                    "chain, its own monitor alerts too: stop DMD payments with its kill switch.",
                )
                findings.append(f)
                record["open_block"] = f.__dict__
        if abs(ours_height - theirs_height) > HEIGHT_TOLERANCE:
            behind = "behind" if theirs_height < ours_height else "ahead of"
            f = Finding(
                f"views.{view.name}.height",
                WARNING,
                f"{view.label[0].upper()}{view.label[1:]} is "
                f"{abs(ours_height - theirs_height)} blocks {behind} the monitor's node",
                f"This node: block {ours_height}. {view.label[0].upper()}{view.label[1:]}: "
                f"block {theirs_height}. Behind: it is syncing or stuck. Far ahead or behind: it "
                "may be on another chain.",
            )
            findings.append(f)
            record["open_height"] = f.__dict__
        state[view.name] = record
    ctx.put("views", state)
    return findings


# --------------------------------------------------------------------------- forks


def check_forks(ctx: Context) -> list[Finding]:
    tips = ctx.rpc.call("getchaintips")
    active = max(int(t["height"]) for t in tips if t.get("status") == "active")
    audience = "both" if ctx.settings.public_reorgs else "private"
    findings = []
    for tip in tips:
        status, height = tip.get("status"), int(tip["height"])
        length, tip_hash = int(tip["branchlen"]), str(tip.get("hash", ""))
        fork = height - length  # the last block the branch shares with the node's chain
        if status == "active" or height < active - RECENT_BRANCH_BLOCKS:
            continue
        branch = f"Its last block: {height} ({tip_hash}). The node's chain: block {active}."
        if status == "valid-fork" and length >= BRANCH_ALERT_BLOCKS:
            deep = length >= DMD_CONFIRMATIONS
            findings.append(
                Finding(
                    f"forks.reorg.{fork}" + (".deep" if deep else ""),
                    CRITICAL if deep else WARNING,
                    f"The DebitMyData chain was reorganized: {length} blocks after block {fork} "
                    "were replaced",
                    f"{branch} "
                    + (
                        f"That is as deep as the {DMD_CONFIRMATIONS} confirmations DMD payments "
                        "need: treat it as a possible double spend until shown otherwise. The "
                        "gateway's own monitor checks its payments; look at who mined the "
                        "replacing blocks (the miners chart)."
                        if deep
                        else "Two or more blocks replaced at once is rare: someone mined a chain "
                        "of their own. If it happens again, look at who mined the replacing blocks."
                    ),
                    kind="event",
                    audience=audience,
                )
            )
        elif status in ("valid-headers", "headers-only") and length >= BRANCH_ALERT_BLOCKS:
            if fork >= active:
                continue  # the node's own chain, ahead of it: still downloading
            longer = height > active
            findings.append(
                Finding(
                    f"forks.competing.{fork}",
                    WARNING,
                    f"A competing DebitMyData branch of {length} blocks, from block {fork}",
                    f"{branch} Someone mines another chain from block {fork}. "
                    + (
                        "It is longer than the node's own, which the node has not switched to "
                        "(yet): if it does, that is a reorganization, and an alert follows."
                        if longer
                        else "It is shorter, so the node stays on its own chain."
                    ),
                    kind="event",
                )
            )
        elif status == "invalid":
            findings.append(
                Finding(
                    f"forks.invalid.{fork}",
                    WARNING,
                    f"Invalid DebitMyData blocks after block {fork}",
                    f"{branch} The node refused {length} block{'s' if length > 1 else ''} that "
                    "break the chain's rules: a miner on another or a broken version of "
                    "DebitMyData Core. If more follow, compare the nodes' versions.",
                    kind="event",
                )
            )
    return findings


# --------------------------------------------------------------------------- hashrate


def _work_between(ctx: Context, start: datetime, end: datetime) -> tuple[float, int] | None:
    """Hashes per second from the blocks found in [start, end), and how many there were."""
    row = ctx.conn.execute(
        "SELECT count(*) AS n, min(height) AS lo, max(height) AS hi FROM blocks "
        "WHERE time >= %s AND time < %s",
        (start, end),
    ).fetchone()
    if not row or not row["n"] or row["lo"] is None or row["lo"] < 1:
        return None
    works = {
        r["height"]: int(r["chainwork"])
        for r in ctx.conn.execute(
            "SELECT height, chainwork FROM blocks WHERE height IN (%s, %s)",
            (row["lo"] - 1, row["hi"]),
        ).fetchall()
    }
    if len(works) < 2:
        return None
    seconds = (end - start).total_seconds()
    return (works[row["hi"]] - works[row["lo"] - 1]) / seconds, int(row["n"])


def check_hashrate(ctx: Context) -> list[Finding]:
    hour = ctx.now.replace(minute=0, second=0, microsecond=0)
    last = _work_between(ctx, ctx.now - timedelta(hours=1), ctx.now)
    if last is None:
        return []
    hourly = []
    for i in range(1, 25):
        part = _work_between(ctx, ctx.now - timedelta(hours=i + 1), ctx.now - timedelta(hours=i))
        if part is not None:
            hourly.append(part[0])
    if len(hourly) < 12:
        return []
    usual = statistics.median(hourly)
    if usual <= 0:
        return []
    ratio = last[0] / usual
    factor = float(ctx.settings.hashrate_jump_factor)
    if 1 / factor < ratio < factor:
        return []
    direction = "up" if ratio >= factor else "down"
    previous = ctx.state(f"hashrate.{direction}")
    if previous and ctx.now - datetime.fromisoformat(previous) < EVENT_COOLDOWN:
        return []
    ctx.put(f"hashrate.{direction}", ctx.now.isoformat())
    words = f"{ratio:.1f} times" if ratio >= 1 else f"{1 / ratio:.1f} times less than"
    return [
        Finding(
            f"hashrate.{direction}.{hour:%Y%m%d%H}",
            WARNING,
            f"DebitMyData hashrate {'jumped' if direction == 'up' else 'dropped'}: "
            f"{_ths(last[0])} in the last hour",
            f"{last[1]} blocks in the last hour: about {_ths(last[0])}, {words} the last day's "
            f"usual {_ths(usual)} (the median hour). "
            + (
                "New mining power arrived: a big miner joining, or hashrate rented from a market."
                if direction == "up"
                else "Mining power left: a big miner stopped, or the pool lost miners."
            ),
            kind="event",
            audience="both",
        )
    ]


def check_difficulty(ctx: Context) -> list[Finding]:
    now_row = ctx.conn.execute(
        "SELECT difficulty FROM blocks ORDER BY height DESC LIMIT 1"
    ).fetchone()
    day_row = ctx.conn.execute(
        "SELECT difficulty FROM blocks WHERE time <= %s ORDER BY height DESC LIMIT 1",
        (ctx.now - timedelta(days=1),),
    ).fetchone()
    if not now_row or not day_row or day_row["difficulty"] <= 0:
        return []
    change = now_row["difficulty"] / day_row["difficulty"] - 1
    limit = float(ctx.settings.difficulty_change_share)
    if abs(change) < limit:
        return []
    direction = "up" if change > 0 else "down"
    return [
        Finding(
            f"difficulty.{direction}.{ctx.now:%Y%m%d}",
            INFO,
            f"DebitMyData difficulty {direction} {abs(change):.0%} in a day",
            f"Difficulty {now_row['difficulty']:,.2f}, from {day_row['difficulty']:,.2f} a day "
            "ago. It follows the hashrate: each block adjusts it (LWMA), so a block keeps coming "
            "every 2.5 minutes on average.",
            kind="event",
            audience="both",
        )
    ]


# --------------------------------------------------------------------------- miners


def miner_shares(
    conn: psycopg.Connection[Any], blocks: int = MINER_WINDOW, since: datetime | None = None
) -> dict[str, Any]:
    """Who mined the last blocks (or those found since a moment): the pool (its tag in the
    coinbase) or others, by their tag or the address they paid themselves; and the addresses
    the pool's blocks paid."""
    if since is None:
        rows = conn.execute(
            "SELECT pool, coinbase_tag, payouts FROM blocks ORDER BY height DESC LIMIT %s",
            (blocks,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT pool, coinbase_tag, payouts FROM blocks WHERE time > %s", (since,)
        ).fetchall()
    miners: dict[str, int] = {}
    payees: dict[str, int] = {}
    for r in rows:
        if r["pool"]:
            miners[PUBLIC_POOL] = miners.get(PUBLIC_POOL, 0) + 1
            for address, value in (r["payouts"] or {}).items():
                payees[address] = payees.get(address, 0) + int(value)
        else:
            paid = r["payouts"] or {}
            top = max(paid, key=lambda a: paid[a]) if paid else ""
            who = r["coinbase_tag"].split(" | ")[-1] if r["coinbase_tag"] else top
            who = who or "unknown"
            miners[who] = miners.get(who, 0) + 1
    return {"blocks": len(rows), "miners": miners, "payees": payees}


def check_miners(ctx: Context) -> list[Finding]:
    shares = miner_shares(ctx.conn)
    total = shares["blocks"]
    if total < MINER_WINDOW // 2:
        return []
    audience = "both" if ctx.settings.public_concentration else "private"
    findings = []
    limit = float(ctx.settings.outside_miner_share)
    for who, count in sorted(shares["miners"].items(), key=lambda x: -x[1]):
        if who == PUBLIC_POOL or count / total < limit:
            continue
        findings.append(
            Finding(
                "miners.outside",
                WARNING,
                f"A miner outside the pool found {count / total:.0%} of the last {total} blocks",
                f"{who}: {count} of the last {total} DebitMyData blocks (6 hours). Mining power "
                "outside the pool is new on this chain; it is only a threat when it gets close to "
                "half (51%): then it could replace recent blocks. Watch the hashrate and the "
                "reorganization alerts.",
                audience=audience,
            )
        )
        break
    payees = shares["payees"]
    pool_blocks = shares["miners"].get(PUBLIC_POOL, 0)
    excluded = set(ctx.settings.pool_addresses)
    paid = {a: v for a, v in payees.items() if a not in excluded}
    whole = sum(paid.values())
    if whole > 0:
        top = max(paid, key=lambda a: paid[a])
        share = paid[top] / whole
        if share >= float(ctx.settings.pool_payee_share):
            findings.append(
                Finding(
                    "miners.payee",
                    WARNING,
                    f"One address got {share:.0%} of the pool's payouts in the last blocks",
                    f"{top} received {share:.0%} of what the pool's last {pool_blocks} blocks "
                    "paid (PPLNS: its share of the pool's work). One miner with that much "
                    "of the pool's hashrate could leave with it at any time.",
                    audience=audience,
                )
            )
    return findings


# --------------------------------------------------------------------------- price


def check_price(ctx: Context) -> list[Finding]:
    rows = ctx.conn.execute(
        "SELECT observed_at, usd_per_dmd, price_from FROM prices "
        "WHERE source <> 'gateway-import' OR observed_at > %s ORDER BY observed_at DESC LIMIT 2",
        (ctx.now - timedelta(days=1),),
    ).fetchall()
    findings = []
    if not rows or ctx.now - rows[0]["observed_at"] > PRICE_STALE:
        since = rows[0]["observed_at"] if rows else None
        findings.append(
            Finding(
                "price.stale",
                WARNING,
                "The gateway's DMD reference price is not updating",
                f"The latest one the monitor has is from {since:%Y-%m-%d %H:%M} UTC. "
                if since
                else "The monitor has no price from the gateway yet. "
                + f"It reads {ctx.settings.price_url} every 5 minutes: check that the address "
                "answers, and the gateway's own monitor.",
            )
        )
        return findings
    if len(rows) == 2 and rows[1]["usd_per_dmd"] > 0:
        change = rows[0]["usd_per_dmd"] / rows[1]["usd_per_dmd"] - 1
        if abs(change) >= ctx.settings.price_change_share:
            when = rows[0]["observed_at"]
            findings.append(
                Finding(
                    f"price.change.{when:%Y%m%d%H%M}",
                    INFO,
                    f"DMD reference price {'up' if change > 0 else 'down'} {abs(change):.0%}: "
                    f"{_usd(rows[0]['usd_per_dmd'])}",
                    f"From {_usd(rows[1]['usd_per_dmd'])}. The DebitMyData Gateway's reference "
                    "price: the higher of DebitMyData's minimum rate and the cost of mining DMD "
                    f"(now from the {rows[0]['price_from'] or 'mining cost'}). Not a market price.",
                    kind="event",
                    audience="both",
                )
            )
    return findings


# --------------------------------------------------------------------------- servers


def check_server(ctx: Context) -> list[Finding]:
    answer = ctx.prometheus(ctx.settings.prometheus_url, "/api/v1/alerts")
    findings = []
    for alert in answer.get("data", {}).get("alerts", []):
        if alert.get("state") != "firing":
            continue
        labels, notes = alert.get("labels", {}), alert.get("annotations", {})
        name = labels.get("alertname", "alert")
        instance = labels.get("server") or labels.get("instance", "")
        severity = CRITICAL if labels.get("severity") == "critical" else WARNING
        # one alert per disk, port or job of a server
        parts = [name, instance] + [
            labels[k] for k in ("mountpoint", "port", "task") if k in labels
        ]
        findings.append(
            Finding(
                "server." + ".".join(p for p in parts if p),
                severity,
                notes.get("summary", name),
                notes.get("description", "") or f"Prometheus alert {name} on {instance}.",
            )
        )
    return findings


# --------------------------------------------------------------------------- the daily summary


def daily_summary(conn: psycopg.Connection[Any], day: datetime) -> Finding | None:
    """The summary of the UTC day that ended at ``day`` (midnight)."""
    start, end = day - timedelta(days=1), day
    row = conn.execute(
        "SELECT count(*) AS n, min(height) AS lo, max(height) AS hi, avg(difficulty) AS diff, "
        "sum(CASE WHEN pool THEN 1 ELSE 0 END) AS pool FROM blocks WHERE time >= %s AND time < %s",
        (start, end),
    ).fetchone()
    if not row or not row["n"] or row["lo"] is None or row["lo"] < 1:
        return None
    works = {
        r["height"]: (int(r["chainwork"]), r["mining_cost_usd"])
        for r in conn.execute(
            "SELECT height, chainwork, mining_cost_usd FROM blocks WHERE height IN (%s, %s)",
            (row["lo"] - 1, row["hi"]),
        ).fetchall()
    }
    rate = (works[row["hi"]][0] - works[row["lo"] - 1][0]) / 86400
    cost = works[row["hi"]][1]
    price = conn.execute(
        "SELECT usd_per_dmd FROM prices WHERE observed_at < %s ORDER BY observed_at DESC LIMIT 1",
        (end,),
    ).fetchone()
    seconds = 86400 / row["n"]
    lines = [
        f"Blocks: {row['n']} (from {row['lo']} to {row['hi']}), one every "
        f"{seconds / 60:.1f} minutes on average",
        f"Hashrate: {_ths(rate)} on average",
        f"Difficulty: {row['diff']:,.2f} on average",
        f"Found by the DebitMyData pool: {row['pool'] / row['n']:.0%} of the blocks",
        f"Mining cost of 1 DMD: {_usd(cost)} (electricity, at the approved figures)",
    ]
    if price:
        lines.append(
            f"Reference price (DebitMyData Gateway, not a market price): "
            f"{_usd(price['usd_per_dmd'])}"
        )
    return Finding(
        f"daily.{start:%Y-%m-%d}",
        INFO,
        f"DebitMyData network on {start:%B %-d, %Y}",
        "\n".join(lines),
        kind="event",
        audience="public",
    )


def check_daily(ctx: Context) -> list[Finding]:
    midnight = ctx.now.replace(hour=0, minute=0, second=0, microsecond=0)
    if ctx.now - midnight < timedelta(minutes=5):
        return []  # the last blocks of the day may still be arriving
    summary = daily_summary(ctx.conn, midnight)
    return [summary] if summary else []


CHECKS: dict[str, Callable[[Context], list[Finding]]] = {
    "node": check_node,
    "stall": check_stall,
    "views": check_views,
    "forks": check_forks,
    "hashrate": check_hashrate,
    "difficulty": check_difficulty,
    "miners": check_miners,
    "price": check_price,
    "server": check_server,
    "daily": check_daily,
}
