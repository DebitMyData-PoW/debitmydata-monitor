// DMD Stats: the figures and the charts, from the files the monitoring server writes every minute
// (/api/summary.json, /api/chart-<range>.json, /api/miners.json). Nothing here sends anything.
(function () {
  "use strict";
  var root = document.documentElement;
  var state = { metric: "price", range: "30d", minerRange: "24h", summary: null, charts: {}, miners: null };
  var chart = null;
  function $(id) { return document.getElementById(id); }

  // ------------------------------------------------------------------ formats
  function usd(v) {
    if (v === null || v === undefined || isNaN(v)) return "–";
    if (v >= 1) return "$" + v.toLocaleString("en-US", { maximumFractionDigits: 2 });
    if (v === 0) return "$0";
    var digits = Math.max(2, Math.min(12, 3 - Math.floor(Math.log10(v))));
    return "$" + v.toFixed(digits).replace(/0+$/, "").replace(/\.$/, "");
  }
  function hashrate(v) {
    if (v === null || v === undefined) return "–";
    var units = [[1e18, "EH/s"], [1e15, "PH/s"], [1e12, "TH/s"], [1e9, "GH/s"], [1e6, "MH/s"], [1e3, "kH/s"]];
    for (var i = 0; i < units.length; i++) {
      if (v >= units[i][0]) return (v / units[i][0]).toLocaleString("en-US", { maximumFractionDigits: 2 }) + " " + units[i][1];
    }
    return Math.round(v) + " H/s";
  }
  function bigNumber(v) {
    if (v === null || v === undefined) return "–";
    var units = [[1e12, "T"], [1e9, "B"], [1e6, "M"], [1e3, "K"]];
    for (var i = 0; i < units.length; i++) {
      if (v >= units[i][0]) return (v / units[i][0]).toLocaleString("en-US", { maximumFractionDigits: 2 }) + units[i][1];
    }
    return v.toLocaleString("en-US", { maximumFractionDigits: 2 });
  }
  function minutes(seconds) {
    if (!seconds) return "–";
    return (seconds / 60).toLocaleString("en-US", { maximumFractionDigits: 2 }) + " min";
  }
  function percent(v) { return v === null || v === undefined ? "–" : (v * 100).toLocaleString("en-US", { maximumFractionDigits: 1 }) + "%"; }
  function change(v, label) {
    if (v === null || v === undefined) return document.createTextNode(label || "");
    var span = document.createElement("span");
    span.className = "dmd-change " + (v > 0 ? "up" : v < 0 ? "down" : "");
    span.textContent = (v > 0 ? "▲ " : v < 0 ? "▼ " : "") + percent(Math.abs(v)) + (label ? " " + label : "");
    return span;
  }
  function put(id, value, sub) {
    var el = $(id);
    if (!el) return;
    el.textContent = value;
    if (sub !== undefined) {
      var s = $(id + "-sub");
      s.textContent = "";
      if (typeof sub === "string") s.textContent = sub; else s.appendChild(sub);
    }
  }
  function ago(iso) {
    var s = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
    if (s < 60) return s + " s ago";
    if (s < 3600) return Math.floor(s / 60) + " min ago";
    if (s < 86400) return Math.floor(s / 3600) + " h ago";
    return Math.floor(s / 86400) + " days ago";
  }
  function getJSON(path) {
    return fetch(path, { cache: "no-cache" }).then(function (r) {
      if (!r.ok) throw new Error(path + ": " + r.status);
      return r.json();
    });
  }

  // ------------------------------------------------------------------ figures
  function showSummary(s) {
    state.summary = s;
    var ref = s.reference_price;
    if (ref) {
      var from = ref.price_from === "minimum" ? "DebitMyData's minimum rate" : "the mining cost";
      var sub = document.createElement("span");
      sub.appendChild(document.createTextNode("from " + from));
      if (ref.change_24h) { sub.appendChild(document.createTextNode(" · ")); sub.appendChild(change(ref.change_24h, "24h")); }
      put("s-price", usd(ref.usd_per_dmd), sub);
    } else {
      put("s-price", "–", "not recorded yet");
    }
    put("s-cost", usd(s.mining_cost_usd), change(s.mining_cost_change_24h, "24h"));
    put("s-hashrate", hashrate(s.hashrate_24h), (function () {
      var span = document.createElement("span");
      span.appendChild(document.createTextNode("last hour " + hashrate(s.hashrate_1h) + " · "));
      span.appendChild(change(s.hashrate_change_24h, ""));
      return span;
    })());
    put("s-difficulty", bigNumber(s.difficulty), change(s.difficulty_change_24h, "24h"));
    put("s-height", s.height.toLocaleString("en-US"), s.last_block ? "last block " + ago(s.last_block.time) : "");
    put("s-blocktime", minutes(s.block_seconds_24h));
    put("s-blocks", s.blocks_24h.toLocaleString("en-US"));
    put("s-pool", percent(s.pool_share_24h));
    $("k-mined").textContent = s.coins_mined_dmd.toLocaleString("en-US") + " DMD";
    $("k-reward").textContent = s.block_reward_dmd + " DMD";
    var left = s.next_halving_height - s.height;
    var days = left * 150 / 86400;
    $("k-halving").textContent = "block " + s.next_halving_height.toLocaleString("en-US") + " (in about " +
      (days >= 60 ? Math.round(days / 30.4) + " months" : Math.round(days) + " days") + ")";
    $("k-ath").textContent = s.hashrate_ath ? hashrate(s.hashrate_ath.hashrate) + " (" + s.hashrate_ath.day + ")" : "–";
    $("k-last").textContent = s.last_block ? new Date(s.last_block.time).toLocaleString() : "–";

    var pill = $("dmd-status"), notes = $("dmd-notes");
    var stale = s.last_block && Date.now() - new Date(s.last_block.time).getTime() > 30 * 60 * 1000;
    notes.textContent = "";
    if (s.status && s.status.normal && !stale) {
      pill.className = "dmd-pill ok";
      pill.textContent = "The chain is running normally";
    } else {
      pill.className = "dmd-pill alert";
      pill.textContent = "The chain needs attention";
      var list = (s.status && s.status.notes) || [];
      if (!list.length && stale) list = ["No new block for " + ago(s.last_block.time).replace(" ago", "")];
      list.forEach(function (n) { var li = document.createElement("li"); li.textContent = n; notes.appendChild(li); });
    }
    $("dmd-updated").textContent = "Updated " + new Date(s.updated_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  function showMiners() {
    var m = state.miners && state.miners[state.minerRange];
    var list = $("dmd-miners");
    list.textContent = "";
    if (!m || !m.miners.length) { list.innerHTML = "<li><span class=\"who\">No blocks in this period.</span></li>"; return; }
    m.miners.slice(0, 8).forEach(function (who) {
      var li = document.createElement("li"), name = document.createElement("span"), n = document.createElement("span");
      var bar = document.createElement("span"), fill = document.createElement("i");
      li.className = who.pool ? "pool" : "other";
      name.className = "who";
      name.textContent = who.pool ? "DebitMyData pool" : who.name;
      n.className = "n";
      n.textContent = percent(who.share) + " · " + who.blocks.toLocaleString("en-US") + " blocks";
      bar.className = "bar";
      fill.style.width = Math.max(1, who.share * 100) + "%";
      bar.appendChild(fill);
      li.appendChild(name); li.appendChild(n); li.appendChild(bar);
      list.appendChild(li);
    });
  }

  // ------------------------------------------------------------------ the chart
  var METRICS = {
    price: {
      note: "Log scale. The reference price is the DebitMyData Gateway's (not a market price), as it recorded it; the mining cost is the electricity it takes to mine 1 DMD. The shaded part is from before the gateway: the cost is computed from the chain.",
      // A log scale, drawn as the powers of ten (the chart's own log scale fails below $1):
      // empty or zero points are left out.
      series: function (pts) {
        function only(i) {
          return pts.filter(function (p) { return p[i] > 0; }).map(function (p) { return [p[0] * 1000, Math.log10(p[i])]; });
        }
        return [{ name: "Reference price", data: only(1) }, { name: "Mining cost", data: only(2) }];
      },
      colours: function (c) { return [c.red, c.gold]; },
      format: function (v) { return v === null || v === undefined ? "–" : usd(Math.pow(10, v)); }, log: true, type: "line"
    },
    hashrate: {
      note: "Worked out from the blocks found: an hour's average on the 24-hour chart, 6 hours on 7 days, a day on the longer ones.",
      series: function (pts) { return [{ name: "Hashrate", data: pts.map(function (p) { return [p[0] * 1000, p[4]]; }) }]; },
      colours: function (c) { return [c.red]; }, format: hashrate, type: "area"
    },
    difficulty: {
      note: "The chain's difficulty, adjusted at every block (LWMA) so that one comes every 2.5 minutes on average.",
      series: function (pts) { return [{ name: "Difficulty", data: pts.map(function (p) { return [p[0] * 1000, p[3]]; }) }]; },
      colours: function (c) { return [c.red]; }, format: bigNumber, type: "area"
    },
    blocktime: {
      note: "The average time between blocks, over the same windows as the hashrate. The chain aims at 2.5 minutes.",
      series: function (pts) { return [{ name: "Block time", data: pts.map(function (p) { return [p[0] * 1000, p[5] ? p[5] / 60 : null]; }) }]; },
      colours: function (c) { return [c.gold]; },
      format: function (v) { return v === null || v === undefined ? "–" : v.toFixed(2) + " min"; }, type: "line", target: 2.5
    }
  };

  function legend(def, c, series) {
    var box = $("dmd-legend");
    box.textContent = "";
    def.colours(c).forEach(function (colour, i) {
      var s = document.createElement("span");
      s.style.setProperty("--c", colour);
      s.textContent = series[i].name;
      box.appendChild(s);
    });
  }

  // The log scale's axis: from the power of ten below the lowest point to the one above the
  // highest, a line at each power ($0.00001, $0.0001, $0.001...)
  function logAxis(series, def) {
    var values = [];
    series.forEach(function (s) { s.data.forEach(function (p) { values.push(p[1]); }); });
    var low = values.length ? Math.floor(Math.min.apply(null, values)) : -5;
    var high = values.length ? Math.ceil(Math.max.apply(null, values)) : -2;
    if (high <= low) high = low + 1;
    return { min: low, max: high, tickAmount: high - low,
      labels: { formatter: function (v) { return def.format(v); }, minWidth: 40 } };
  }

  function draw() {
    var data = state.charts[state.range];
    if (!data || typeof ApexCharts === "undefined") return;
    var def = METRICS[state.metric], c = window.dmdChartTheme();
    var series = def.series(data.points);
    var annotations = { xaxis: [], yaxis: [] };
    var since = state.summary && state.summary.reference_price && state.summary.reference_price.since;
    if (state.metric === "price" && since && data.points.length && data.points[0][0] * 1000 < new Date(since).getTime()) {
      annotations.xaxis.push({ x: data.points[0][0] * 1000, x2: new Date(since).getTime(), fillColor: c.grid, opacity: 0.6,
        borderColor: "transparent", label: { text: "computed", borderColor: "transparent", style: { background: "transparent", color: c.fore } } });
    }
    if (def.target) annotations.yaxis.push({ y: def.target, borderColor: c.green, strokeDashArray: 5,
      label: { text: "target", borderColor: "transparent", style: { background: "transparent", color: c.green } } });
    var options = {
      chart: { type: def.type, height: window.innerWidth < 760 ? 300 : 360, foreColor: c.fore, fontFamily: "Mada, system-ui, sans-serif",
        toolbar: { show: false }, zoom: { enabled: false }, animations: { enabled: false }, background: "transparent" },
      series: series,
      colors: def.colours(c),
      stroke: { width: 2.5, curve: "smooth" },
      fill: def.type === "area" ? { type: "gradient", gradient: { opacityFrom: 0.35, opacityTo: 0.02 } } : { opacity: 1 },
      dataLabels: { enabled: false },
      grid: { borderColor: c.grid, strokeDashArray: 3, padding: { left: 8, right: 12 } },
      legend: { show: false },
      markers: { size: 0, hover: { size: 4 } },
      xaxis: { type: "datetime", labels: { datetimeUTC: false }, axisBorder: { show: false }, axisTicks: { show: false },
        tooltip: { enabled: false } },
      yaxis: def.log ? logAxis(series, def)
        : { forceNiceScale: true, labels: { formatter: function (v) { return def.format(v); }, minWidth: 40 } },
      tooltip: { theme: c.tooltip, shared: true, x: { format: state.range === "24h" || state.range === "7d" ? "MMM d, HH:mm" : "MMM d, yyyy" },
        y: { formatter: function (v) { return def.format(v); } } },
      annotations: annotations,
      noData: { text: "No data for this period yet" }
    };
    legend(def, c, series);
    $("dmd-chart-note").textContent = def.note;
    $("dmd-chart").setAttribute("aria-label", series.map(function (s) { return s.name; }).join(" and ") + ", " + state.range);
    if (chart) chart.destroy();
    chart = new ApexCharts($("dmd-chart"), options);
    chart.render();
  }

  function loadChart(range) {
    return getJSON("/api/chart-" + range + ".json").then(function (data) { state.charts[range] = data; });
  }

  function select(groupId, attr, value) {
    Array.prototype.forEach.call($(groupId).querySelectorAll("button"), function (b) {
      b.setAttribute("aria-pressed", b.getAttribute(attr) === value ? "true" : "false");
    });
  }
  $("dmd-metrics").addEventListener("click", function (e) {
    var b = e.target.closest("button[data-metric]");
    if (!b) return;
    state.metric = b.getAttribute("data-metric");
    select("dmd-metrics", "data-metric", state.metric);
    draw();
  });
  $("dmd-ranges").addEventListener("click", function (e) {
    var b = e.target.closest("button[data-range]");
    if (!b) return;
    state.range = b.getAttribute("data-range");
    select("dmd-ranges", "data-range", state.range);
    (state.charts[state.range] ? Promise.resolve() : loadChart(state.range)).then(draw).catch(function () {});
  });
  $("dmd-miner-ranges").addEventListener("click", function (e) {
    var b = e.target.closest("button[data-range]");
    if (!b) return;
    state.minerRange = b.getAttribute("data-range");
    select("dmd-miner-ranges", "data-range", state.minerRange);
    showMiners();
  });

  // ------------------------------------------------------------------ the theme switch
  var toggle = $("dmd-theme-toggle");
  function dark() { return root.getAttribute("data-theme") === "dark"; }
  function label() {
    var meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", dark() ? "#1d1d23" : "#ffffff");
    toggle.setAttribute("aria-label", dark() ? "Switch to the light theme" : "Switch to the dark theme");
    toggle.title = dark() ? "Light theme" : "Dark theme";
  }
  toggle.addEventListener("click", function () {
    var t = dark() ? "light" : "dark";
    root.setAttribute("data-theme", t);
    root.style.colorScheme = t;
    try { localStorage.setItem("dmd-theme-stats", t); } catch (e) {}
    var secure = location.protocol === "https:" ? "; secure" : "";
    document.cookie = "dmd-theme-stats=" + t + "; path=/; max-age=31536000; samesite=lax" + secure;
    label();
    draw();
  });
  label();
  $("dmd-year").textContent = String(new Date().getFullYear());

  // ------------------------------------------------------------------ loading, and every minute
  function refresh() {
    var range = state.range;
    return Promise.all([
      getJSON("/api/summary.json").then(showSummary),
      getJSON("/api/miners.json").then(function (m) { state.miners = m; showMiners(); }),
      loadChart(range)
    ]).then(function () {
      Object.keys(state.charts).forEach(function (r) { if (r !== range) delete state.charts[r]; });
      draw();
    }).catch(function () {
      var pill = $("dmd-status");
      if (!state.summary) { pill.className = "dmd-pill"; pill.textContent = "The figures could not be loaded"; }
    });
  }
  var resized = null;
  window.addEventListener("resize", function () { clearTimeout(resized); resized = setTimeout(draw, 250); });
  refresh();
  setInterval(function () { if (!document.hidden) refresh(); }, 60000);
  document.addEventListener("visibilitychange", function () { if (!document.hidden) refresh(); });
})();
