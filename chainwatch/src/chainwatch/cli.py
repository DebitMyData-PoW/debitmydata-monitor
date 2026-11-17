"""dmd-chainwatch: the service and its commands.

  dmd-chainwatch run              the service (systemd: dmd-chainwatch.service)
  dmd-chainwatch sync             add the node's new blocks now, with progress (the first fill)
  dmd-chainwatch check            run the checks once and print what they find (sends nothing)
  dmd-chainwatch publish          write the stats page's files now
  dmd-chainwatch import-prices F  add the gateway's earlier prices, from its CSV export (- : stdin)
  dmd-chainwatch recompute-costs  work out every block's mining cost again (new approved figures)
  dmd-chainwatch test-alerts      send a test message to each alert channel
  dmd-chainwatch status           the latest figures, and the open alerts
  dmd-chainwatch notify           the notifications service (systemd: dmd-notify.service)
  dmd-chainwatch push-keys        make the notifications' key, once (install.sh does it)

The service, every 10 seconds: new blocks into the database (and what happened, for the
notifications), or else the volume of blocks stored by an earlier version; every minute: the
checks, the alerts, the stats page's files and Prometheus's figures; every 5 minutes: the
gateway's price, and the exchanges' figures once DMD trades on one (DMDW_COINGECKO_ID).
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from chainwatch import __version__, alerts, api, db, events, market, notify, price, publish, push
from chainwatch.alerts import CRITICAL, WARNING, Finding, Message
from chainwatch.checks import CHECKS, Context
from chainwatch.config import Settings, load_cost_periods
from chainwatch.rpc import Rpc, RpcError, RpcUnavailable
from chainwatch.sync import fill_flows, recompute_costs, rich_list, sync

log = logging.getLogger("chainwatch")

NODE_CHECKS = {"node", "stall", "views", "forks"}
LOOP_SECONDS = 10
CHECK_SECONDS = 60
PRICE_SECONDS = 300
BLOCKS_CSV_SECONDS = 3600
WATCHED_SECONDS = 30
SYNC_BATCH = 5000  # blocks per pass while filling: the checks keep running in between
FILL_BATCH = 1000  # blocks per pass whose volume is read again (blocks of an earlier version)
NOTIFY_SECONDS = 5


def _now() -> datetime:
    return datetime.now(UTC)


def _rpc(settings: Settings) -> Rpc:
    return Rpc(settings.rpc_url, settings.rpc_cookie, settings.rpc_user, settings.rpc_password)


def run_checks(ctx: Context, only: set[str] | None = None) -> tuple[list[Finding], set[str]]:
    findings: list[Finding] = []
    completed = {"monitor"}
    node_up = True
    try:
        ctx.rpc.call("getblockcount")
    except (RpcUnavailable, RpcError) as exc:
        node_up = False
        completed.add("node")
        findings.append(
            Finding(
                "node.down",
                WARNING,
                "The monitor's DebitMyData node does not answer",
                f"{exc}. The checks that need it wait: systemctl status debitmydata, "
                "journalctl -u debitmydata.",
            )
        )
    for name, check in CHECKS.items():
        if only is not None and name not in only:
            continue
        if name in NODE_CHECKS and not node_up:
            continue
        try:
            with ctx.conn.transaction():
                findings.extend(check(ctx))
            completed.add(name)
        except Exception as exc:  # one check failing never stops the others
            log.warning("check %s: %s", name, exc, exc_info=log.isEnabledFor(logging.DEBUG))
            findings.append(
                Finding(
                    f"monitor.{name}",
                    WARNING,
                    f"The monitor could not run its {name} check",
                    f"{type(exc).__name__}: {str(exc)[:500]}",
                )
            )
    return findings, completed


def _node_figures(rpc: Rpc) -> dict[str, float]:
    try:
        info = rpc.call("getblockchaininfo")
        return {
            "peers": float(rpc.call("getconnectioncount")),
            "initial_download": 1.0 if info.get("initialblockdownload") else 0.0,
            "headers": float(info.get("headers", 0)),
        }
    except (RpcUnavailable, RpcError):
        return {}


class Service:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.periods = load_cost_periods(settings.mining_cost_file)
        self.conn = db.connect(settings.database_url)
        db.create_schema(self.conn)
        self.rpc = _rpc(settings)
        self.best: str | None = None
        self.next_check = self.next_price = self.next_publish = 0.0
        self.next_blocks_csv = self.next_watched = 0.0
        self.watched: frozenset[str] = frozenset()
        self.ranks: dict[str, int] = {}
        self.stopping = False

    @property
    def large(self) -> int:
        return int(self.settings.large_transfer_dmd * 100_000_000)

    def sync_blocks(self) -> bool:
        """New blocks into the database; True when something changed."""
        try:
            best = self.rpc.call("getbestblockhash")
        except (RpcUnavailable, RpcError) as exc:
            log.info("node: %s", exc)
            return False
        clock = time.monotonic()
        if clock >= self.next_watched:
            self.next_watched = clock + WATCHED_SECONDS
            top = max(self.settings.watch_richlist, notify.RICHLIST_TOP)
            self.ranks = {r["address"]: n for n, r in enumerate(rich_list(self.conn, top), start=1)}
            self.watched = notify.watched_addresses(self.conn) | frozenset(self.ranks)
        if best == self.best:
            # nothing new: the volume of blocks stored by an earlier version, a batch at a time
            try:
                if fill_flows(self.conn, self.rpc, FILL_BATCH, self.large):
                    return True
            except (RpcUnavailable, RpcError) as exc:
                log.info("volume: %s", exc)
            return False
        now = _now()
        result = sync(
            self.conn,
            self.rpc,
            self.periods,
            self.settings.pool_tag,
            now,
            max_blocks=SYNC_BATCH,
            large=self.large,
            watched=self.watched,
        )
        with self.conn.transaction():
            events.record_sync(self.conn, result, now, self.ranks)
        if result.added < SYNC_BATCH:
            self.best = best
        if result.added or result.removed:
            log.info(
                "blocks: %d added, %d removed (node at %s)",
                result.added,
                result.removed,
                result.tip_height,
            )
        return bool(result.added or result.removed)

    def pass_once(self) -> None:
        clock = time.monotonic()
        changed = self.sync_blocks()
        if clock >= self.next_price:
            self.next_price = clock + PRICE_SECONDS
            try:
                if price.poll(self.conn, self.settings.price_url):
                    changed = True
            except price.PriceUnavailable as exc:
                log.info("price: %s", exc)
            try:
                if market.poll(self.conn, self.settings, _now()):
                    changed = True
            except market.MarketUnavailable as exc:
                log.info("market: %s", exc)
        if clock >= self.next_check:
            self.next_check = clock + CHECK_SECONDS
            now = _now()
            ctx = Context(self.conn, self.rpc, self.settings, now)
            findings, completed = run_checks(ctx)
            with self.conn.transaction():
                messages = alerts.apply(self.conn, findings, completed, now)
                events.record_messages(self.conn, messages, now)
                events.prune(self.conn, now)
            if messages:
                sent = alerts.send(self.settings, messages)
                with self.conn.transaction():
                    alerts.mark_sent(self.conn, sent, now)
            changed = True
        if changed or clock >= self.next_publish:
            self.next_publish = clock + CHECK_SECONDS
            now = _now()
            csv_due = clock >= self.next_blocks_csv
            if csv_due:
                self.next_blocks_csv = clock + BLOCKS_CSV_SECONDS
            with self.conn.transaction():
                chain = publish.publish(
                    self.conn,
                    self.settings.public_dir,
                    now,
                    csv_due,
                    float(self.settings.large_transfer_dmd),
                    "CoinGecko" if self.settings.coingecko_id else "",
                    self.settings.pool_addresses,
                )
                publish.metrics(
                    self.conn, self.settings.metrics_file, now, _node_figures(self.rpc), chain
                )

    def run(self) -> None:
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "stopping", True))
        log.info("DebitMyData chain watch %s started", __version__)
        while not self.stopping:
            started = time.monotonic()
            try:
                self.pass_once()
            except Exception:
                log.exception("pass failed; trying again shortly")
                if self.conn.closed or self.conn.broken:
                    try:
                        self.conn = db.connect(self.settings.database_url)
                    except Exception:
                        log.exception("database")
            while not self.stopping and time.monotonic() - started < LOOP_SECONDS:
                time.sleep(0.5)
        log.info("stopped")


# --------------------------------------------------------------------------- commands


def cmd_sync(settings: Settings) -> int:
    service = Service(settings)
    total = 0
    while True:
        result = sync(
            service.conn,
            service.rpc,
            service.periods,
            settings.pool_tag,
            _now(),
            max_blocks=SYNC_BATCH,
        )
        total += result.added
        print(f"blocks: {total} added, node at {result.tip_height}", flush=True)
        if result.added < SYNC_BATCH:
            return 0


def cmd_check(settings: Settings) -> int:
    service = Service(settings)
    ctx = Context(service.conn, service.rpc, settings, _now())
    with service.conn.transaction(force_rollback=True):  # a dry run: nothing kept
        findings, completed = run_checks(ctx)
    for f in findings:
        print(f"[{f.severity}] {f.key}: {f.title}\n    {f.detail}\n")
    print(f"checks run: {', '.join(sorted(completed))}; findings: {len(findings)}")
    return 1 if any(f.severity == CRITICAL for f in findings) else 0


def cmd_publish(settings: Settings) -> int:
    service = Service(settings)
    with service.conn.transaction():
        chain = publish.publish(
            service.conn,
            settings.public_dir,
            _now(),
            True,
            float(settings.large_transfer_dmd),
            "CoinGecko" if settings.coingecko_id else "",
            settings.pool_addresses,
        )
        publish.metrics(
            service.conn, settings.metrics_file, _now(), _node_figures(service.rpc), chain
        )
    print(f"written to {settings.public_dir / 'api'}")
    return 0


def cmd_import(settings: Settings, path: str) -> int:
    conn = db.connect(settings.database_url)
    db.create_schema(conn)
    source = sys.stdin if path == "-" else Path(path)
    added, skipped = price.import_csv(conn, source)
    print(f"{added} prices added, {skipped} already there")
    return 0


def cmd_recompute(settings: Settings) -> int:
    conn = db.connect(settings.database_url)
    periods = load_cost_periods(settings.mining_cost_file)
    print(f"{recompute_costs(conn, periods)} blocks updated")
    return 0


def cmd_test_alerts(settings: Settings) -> int:
    test = Finding(
        "test.alerts",
        "info",
        "Test message from the DebitMyData monitor",
        "If you read this, alerts reach this channel. Nothing is wrong.",
        kind="event",
        audience="both",
    )
    sent = alerts.send(settings, [Message(test, "event")])
    channels = []
    if settings.discord_webhook:
        channels.append("private Discord")
    if settings.smtp_host and settings.email_to:
        channels.append("email to " + ", ".join(settings.email_to))
    if settings.public_discord_webhook:
        channels.append("public Discord")
    print(f"sent to: {', '.join(channels) or 'nothing (no channel set)'}")
    return 0 if sent or not channels else 1


def cmd_notify(settings: Settings) -> int:
    """The notifications: the /push API for the stats page, and the sender."""
    keys = push.load_keys(settings.push_key_file)
    if keys is None:
        log.error(
            "no notifications key in %s: run dmd-chainwatch push-keys", settings.push_key_file
        )
    conn = db.connect(settings.database_url)
    db.create_schema(conn)
    server = api.serve(api.PushApi(settings, keys, _rpc(settings)), settings.push_listen)
    stopping = False

    def stop(*_: Any) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    notifier = notify.Notifier(settings, conn, keys) if keys else None
    log.info("DebitMyData notifications %s started", __version__)
    while not stopping:
        started = time.monotonic()
        try:
            if notifier and notifier.due(started):
                notifier.run_once(_now())
        except Exception:
            log.exception("notifications: trying again shortly")
            if conn.closed or conn.broken:
                try:
                    conn = db.connect(settings.database_url)
                    if notifier:
                        notifier.conn = conn
                except Exception:
                    log.exception("database")
        while not stopping and time.monotonic() - started < NOTIFY_SECONDS:
            time.sleep(0.5)
    server.shutdown()
    log.info("stopped")
    return 0


def cmd_push_keys(settings: Settings) -> int:
    keys, new = push.make_keys(settings.push_key_file)
    print(f"{'made' if new else 'already there'}: {settings.push_key_file}")
    print(f"public key: {keys.public}")
    return 0


def cmd_status(settings: Settings) -> int:
    conn = db.connect(settings.database_url)
    tip = db.tip(conn)
    print(
        f"database: block {tip['height']} ({tip['time']:%Y-%m-%d %H:%M} UTC)"
        if tip
        else "database: no blocks yet"
    )
    row = conn.execute(
        "SELECT count(*) AS n, min(observed_at) AS a, max(observed_at) AS b FROM prices"
    ).fetchone()
    print(
        f"prices: {row['n']}"
        + (f", from {row['a']:%Y-%m-%d} to {row['b']:%Y-%m-%d %H:%M}" if row["n"] else "")
    )
    rows = conn.execute(
        "SELECT key, severity, title, first_seen FROM alerts "
        "WHERE resolved_at IS NULL ORDER BY first_seen"
    ).fetchall()
    filled = conn.execute(
        "SELECT count(*) AS n, count(*) FILTER (WHERE volume_data IS NULL) AS v FROM blocks"
    ).fetchone()
    if filled and filled["v"]:
        done = filled["n"] - filled["v"]
        print(f"volume: {done} of {filled['n']} blocks read (the rest follows)")
    devices = conn.execute("SELECT count(*) AS n FROM push_devices").fetchone()
    print(f"notifications: {devices['n'] if devices else 0} devices")
    print(f"open alerts: {len(rows)}")
    for r in rows:
        print(
            f"  [{r['severity']}] {r['title']} (since {r['first_seen']:%Y-%m-%d %H:%M}, {r['key']})"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dmd-chainwatch",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "command",
        choices=[
            "run",
            "sync",
            "check",
            "publish",
            "import-prices",
            "recompute-costs",
            "test-alerts",
            "status",
            "notify",
            "push-keys",
        ],
    )
    parser.add_argument(
        "file", nargs="?", help="import-prices: the gateway's CSV export (- : standard input)"
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    settings = Settings.from_env()
    commands: dict[str, Any] = {
        "sync": cmd_sync,
        "check": cmd_check,
        "publish": cmd_publish,
        "recompute-costs": cmd_recompute,
        "test-alerts": cmd_test_alerts,
        "status": cmd_status,
        "notify": cmd_notify,
        "push-keys": cmd_push_keys,
    }
    if args.command == "run":
        Service(settings).run()
        return 0
    if args.command == "import-prices":
        if not args.file:
            parser.error("import-prices needs the CSV file")
        return cmd_import(settings, args.file)
    return int(commands[args.command](settings))


if __name__ == "__main__":
    raise SystemExit(main())
