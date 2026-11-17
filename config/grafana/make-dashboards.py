#!/usr/bin/env python3
"""Writes the Grafana dashboards (dashboards/*.json) from the panels below. Change a panel here,
run it (python3 config/grafana/make-dashboards.py), commit both, and run install.sh again.

  chain.json    the DebitMyData chain: hashrate, difficulty, mining cost and reference price,
                block time, miners, reorganizations, the latest blocks (the chain watch's database)
  servers.json  the monitoring and pool servers: CPU, memory, disks, network, the pool's node,
                stratum, database and jobs (Prometheus)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

OUT = Path(__file__).resolve().parent / "dashboards"
PG = {"type": "grafana-postgresql-datasource", "uid": "dmdwatch"}
PROM = {"type": "prometheus", "uid": "prometheus"}
RED, GOLD, GREEN, BLUE = "#c80815", "#c99a06", "#1fa463", "#3274d9"


def sql(ref: str, text: str, fmt: str = "time_series") -> dict[str, Any]:
    return {"refId": ref, "datasource": PG, "editorMode": "code", "format": fmt, "rawQuery": True,
            "rawSql": " ".join(text.split())}


def prom(ref: str, expr: str, legend: str = "") -> dict[str, Any]:
    return {"refId": ref, "datasource": PROM, "expr": expr, "legendFormat": legend, "range": True}


class Board:
    def __init__(self) -> None:
        self.panels: list[dict[str, Any]] = []
        self.y = 0
        self.next_id = 1

    def add(self, panel: dict[str, Any], w: int, h: int, x: int = 0, new_row: bool = True) -> None:
        if new_row and x == 0 and self.panels:
            self.y = max(p["gridPos"]["y"] + p["gridPos"]["h"] for p in self.panels)
        panel["id"] = self.next_id
        self.next_id += 1
        panel["gridPos"] = {"x": x, "y": self.y, "w": w, "h": h}
        self.panels.append(panel)

    def row(self, title: str) -> None:
        self.add({"type": "row", "title": title, "collapsed": False, "panels": []}, 24, 1)


def stat(title: str, target: dict[str, Any], unit: str, colour: str = RED,
         decimals: int | None = None, desc: str = "") -> dict[str, Any]:
    defaults: dict[str, Any] = {"unit": unit, "color": {"mode": "fixed", "fixedColor": colour}}
    if decimals is not None:
        defaults["decimals"] = decimals
    return {"type": "stat", "title": title, "description": desc, "targets": [target],
            "datasource": target["datasource"],
            "fieldConfig": {"defaults": defaults, "overrides": []},
            "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                        "colorMode": "value", "graphMode": "area", "textMode": "value",
                        "justifyMode": "center"}}


def series(title: str, targets: list[dict[str, Any]], unit: str, colours: list[str],
           desc: str = "", log: bool = False, interval: str = "", fill: int = 15,
           extra: dict[str, Any] | None = None) -> dict[str, Any]:
    custom: dict[str, Any] = {"drawStyle": "line", "lineWidth": 2, "fillOpacity": fill,
                              "gradientMode": "opacity", "showPoints": "never",
                              "spanNulls": True, "lineInterpolation": "smooth"}
    if log:
        custom["scaleDistribution"] = {"type": "log", "log": 10}
    overrides = [
        {"matcher": {"id": "byFrameRefID", "options": t["refId"]},
         "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": c}}]}
        for t, c in zip(targets, colours, strict=False)
    ]
    panel: dict[str, Any] = {
        "type": "timeseries", "title": title, "description": desc, "targets": targets,
        "datasource": targets[0]["datasource"],
        "fieldConfig": {"defaults": {"unit": unit, "custom": custom}, "overrides": overrides},
        "options": {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
                    "tooltip": {"mode": "multi", "sort": "none"}},
    }
    if interval:
        panel["interval"] = interval
    if extra:
        panel.update(extra)
    return panel


def table(title: str, target: dict[str, Any], desc: str = "") -> dict[str, Any]:
    return {"type": "table", "title": title, "description": desc, "targets": [target],
            "datasource": target["datasource"],
            "fieldConfig": {"defaults": {"custom": {"align": "auto"}}, "overrides": []},
            "options": {"showHeader": True, "cellHeight": "sm"}}


def dashboard(uid: str, title: str, board: Board, time_from: str, refresh: str,
              variables: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "uid": uid, "title": title, "tags": ["debitmydata"], "timezone": "browser",
        "schemaVersion": 39, "version": 1, "editable": False, "graphTooltip": 1,
        "time": {"from": time_from, "to": "now"}, "refresh": refresh,
        "templating": {"list": variables or []}, "annotations": {"list": []},
        "links": [{"title": "DMD Stats", "type": "link", "url": "https://stats.debitmydata.io/",
                   "targetBlank": True}],
        "panels": board.panels,
    }


# ------------------------------------------------------------------------------- the chain

MINER = ("CASE WHEN pool THEN 'DebitMyData pool' ELSE coalesce(nullif(split_part(coinbase_tag, "
         "' | ', -1), ''), (SELECT k FROM jsonb_object_keys(payouts) AS k LIMIT 1), 'unknown') END")

chain = Board()
chain.add(stat("Block height", prom("A", "dmd_chain_height"), "none", BLUE, 0), 4, 4)
chain.add(stat("Hashrate (24h)", prom("A", "dmd_chain_hashrate_24h"), "Hs"), 4, 4, 4)
chain.add(stat("Difficulty", prom("A", "dmd_chain_difficulty"), "short", GOLD), 4, 4, 8)
chain.add(stat("Mining cost of 1 DMD", prom("A", "dmd_mining_cost_usd"), "currencyUSD", GOLD, 8),
          4, 4, 12)
chain.add(stat("Reference price", prom("A", "dmd_reference_price_usd"), "currencyUSD", RED, 6,
               "The DebitMyData Gateway's reference price (not a market price)."), 4, 4, 16)
chain.add(stat("Open alerts (critical)", prom("A", 'chainwatch_alerts_open{severity="critical"}'),
               "none", RED, 0, "sudo dmd-chainwatch-admin status lists them."), 4, 4, 20)

chain.add(series(
    "Hashrate", [sql("A", """SELECT $__timeGroupAlias(time, $__interval),
        sum(difficulty) * 4294967296 / ($__interval_ms / 1000.0) AS "Hashrate"
        FROM blocks WHERE $__timeFilter(time) GROUP BY 1 ORDER BY 1""")],
    "Hs", [RED], "The work of the blocks found in each interval, divided by its length.",
    interval="1h"), 12, 9)
chain.add(series(
    "Difficulty", [sql("A", """SELECT $__timeGroupAlias(time, $__interval),
        avg(difficulty) AS "Difficulty" FROM blocks WHERE $__timeFilter(time)
        GROUP BY 1 ORDER BY 1""")], "short", [GOLD], interval="10m"), 12, 9, 12)
chain.add(series(
    "Mining cost and reference price",
    [sql("A", """SELECT $__timeGroupAlias(time, $__interval), avg(mining_cost_usd) AS "Mining cost"
        FROM blocks WHERE $__timeFilter(time) AND mining_cost_usd > 0 GROUP BY 1 ORDER BY 1"""),
     sql("B", """SELECT $__timeGroupAlias(observed_at, $__interval),
        avg(usd_per_dmd) AS "Reference price" FROM prices WHERE $__timeFilter(observed_at)
        GROUP BY 1 ORDER BY 1""")],
    "currencyUSD", [GOLD, RED],
    "Log scale. Mining cost: the electricity to mine 1 DMD at the approved figures "
    "(config/mining-cost.toml). Reference price: the gateway's, as recorded.",
    log=True, interval="10m", fill=0), 12, 9)
chain.add(series(
    "Minutes between blocks", [sql("A", """SELECT $__timeGroupAlias(time, $__interval),
        ($__interval_ms / 60000.0) / count(*) AS "Minutes per block" FROM blocks
        WHERE $__timeFilter(time) GROUP BY 1 ORDER BY 1""")],
    "m", [BLUE], "The chain aims at 2.5 minutes.", interval="1h", fill=0,
    extra={"fieldConfig": {"defaults": {"unit": "none", "decimals": 2, "custom": {
        "drawStyle": "line", "lineWidth": 2, "fillOpacity": 0, "showPoints": "never",
        "spanNulls": True, "thresholdsStyle": {"mode": "dashed"}},
        "thresholds": {"mode": "absolute", "steps": [{"color": "transparent", "value": None},
                                                     {"color": GREEN, "value": 2.5}]}},
        "overrides": []}}), 12, 9, 12)

pie = table("Who found the blocks", sql("A", f"""SELECT {MINER} AS "Miner", count(*) AS "Blocks"
    FROM blocks WHERE $__timeFilter(time) GROUP BY 1 ORDER BY 2 DESC""", "table"))
pie.update({"type": "piechart", "options": {
    "reduceOptions": {"values": True, "calcs": ["lastNotNull"], "fields": ""},
    "pieType": "donut", "legend": {"displayMode": "table", "placement": "right",
                                   "values": ["value", "percent"], "showLegend": True},
    "tooltip": {"mode": "single"}}})
chain.add(pie, 8, 9)
chain.add(series(
    "Share of blocks found by the pool", [sql("A", """SELECT $__timeGroupAlias(time, $__interval),
        avg(CASE WHEN pool THEN 1.0 ELSE 0.0 END) AS "Pool" FROM blocks
        WHERE $__timeFilter(time) GROUP BY 1 ORDER BY 1""")],
    "percentunit", [RED], "Below 100%: someone mines outside the pool.", interval="1h"), 8, 9, 8)
chain.add(table("Reorganizations seen", sql("A", """SELECT detected_at AS "Seen",
    fork_height AS "After block", depth AS "Blocks replaced" FROM reorgs
    ORDER BY detected_at DESC LIMIT 50""", "table"),
    "Blocks that left the chain while the chain watch was watching."), 8, 9, 16)
chain.add(table("Latest blocks", sql("A", f"""SELECT height AS "Block", time AS "Time",
    {MINER} AS "Miner", round(difficulty::numeric, 2) AS "Difficulty", tx_count AS "Transactions",
    reward_data / 100000000.0 AS "Reward (DMD)", hash AS "Hash" FROM blocks
    ORDER BY height DESC LIMIT 50""", "table")), 24, 10)

# ------------------------------------------------------------------------------- servers

servers = Board()
servers.row("Servers")
servers.add(series("CPU used", [prom("A", '100 - avg by (server) (rate(node_cpu_seconds_total'
                                     '{mode="idle"}[5m])) * 100', "{{server}}")],
                   "percent", [RED, BLUE]), 8, 8)
servers.add(series("Memory used", [prom("A", "(1 - node_memory_MemAvailable_bytes / "
                                        "node_memory_MemTotal_bytes) * 100", "{{server}}")],
                   "percent", [RED, BLUE]), 8, 8, 8)
servers.add(series("Disk used", [prom("A", '(1 - node_filesystem_avail_bytes{fstype!~"tmpfs|'
                                      'overlay|squashfs"} / node_filesystem_size_bytes{fstype!~'
                                      '"tmpfs|overlay|squashfs"}) * 100',
                                      "{{server}} {{mountpoint}}")],
                   "percent", [RED, BLUE, GOLD]), 8, 8, 16)
servers.add(series("Network in", [prom("A", 'sum by (server) (rate(node_network_receive_bytes_'
                                       'total{device!~"lo|wg.*"}[5m]))', "{{server}}")],
                   "Bps", [RED, BLUE]), 12, 8)
servers.add(series("Network out", [prom("A", 'sum by (server) (rate(node_network_transmit_bytes'
                                        '_total{device!~"lo|wg.*"}[5m]))', "{{server}}")],
                   "Bps", [RED, BLUE]), 12, 8, 12)
servers.row("The pool server")
servers.add(stat("Pool server reachable", prom("A", 'up{job="pool"}'), "bool_yes_no", GREEN, 0,
                 "Through the WireGuard tunnel."), 4, 4)
servers.add(stat("Stratum listening", prom("A", "min(dmd_pool_stratum_listening)"),
                 "bool_yes_no", GREEN, 0), 4, 4, 4)
servers.add(stat("Pool node answers", prom("A", "dmd_pool_node_up"), "bool_yes_no", GREEN, 0),
            4, 4, 8)
servers.add(stat("Pool node peers", prom("A", "dmd_pool_node_peers"), "none", BLUE, 0), 4, 4, 12)
servers.add(stat("Database (MariaDB)", prom("A", "dmd_pool_mariadb_up"), "bool_yes_no", GREEN, 0),
            4, 4, 16)
servers.add(stat("Jobs running", prom("A", "sum(dmd_pool_job_running)"), "none", BLUE, 0,
                 "main, loop2 and blocks: 3 when all run."), 4, 4, 20)
servers.add(series("Pool node and monitor node: height",
                   [prom("A", "max(dmd_chain_height)", "monitor"),
                    prom("B", "max(dmd_pool_node_blocks)", "pool")],
                   "none", [BLUE, RED], "The two lines should be one.", fill=0), 24, 8)

for name, board, title, since, refresh in (
    ("chain", chain, "DebitMyData chain", "now-30d", "5m"),
    ("servers", servers, "DebitMyData servers", "now-24h", "1m"),
):
    OUT.mkdir(exist_ok=True)
    data = dashboard(f"dmd-{name}", title, board, since, refresh)
    (OUT / f"{name}.json").write_text(json.dumps(data, indent=1) + "\n")
    print(f"{OUT / name}.json: {len(board.panels)} panels")
