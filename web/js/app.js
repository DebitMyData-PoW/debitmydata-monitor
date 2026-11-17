// DMD Stats: the figures and the charts, from the files the monitoring server writes every minute
// (/api/summary.json, /api/chart-<range>.json, /api/miners.json). Nothing here sends anything.
// The chart: a metric, a period, lines or candles, and the zoom bar under it (drag its window, or
// the handles at its ends, with a mouse or a finger; arrow keys too); the choices are kept in this
// browser.
(function () {
  "use strict";
  var root = document.documentElement;
  var KEPT = "dmd-stats-chart";
  var state = { metric: "hashrate", range: "30d", style: "line", volume: "exchanges", minerRange: "24h",
    summary: null, charts: {}, miners: null, zoom: null };
  try {
    var kept = JSON.parse(localStorage.getItem(KEPT) || "{}");
    ["metric", "range", "style", "volume"].forEach(function (k) { if (typeof kept[k] === "string") state[k] = kept[k]; });
  } catch (e) {}
  var main = null, brush = null, current = null;
  function $(id) { return document.getElementById(id); }
  function keep() {
    try { localStorage.setItem(KEPT, JSON.stringify({ metric: state.metric, range: state.range, style: state.style, volume: state.volume })); } catch (e) {}
  }

  // ------------------------------------------------------------------ formats
  function usd(v) {
    if (v === null || v === undefined || isNaN(v)) return "–";
    if (v >= 1) return "$" + v.toLocaleString("en-US", { maximumFractionDigits: 2 });
    if (v === 0) return "$0";
    var digits = Math.max(2, Math.min(12, 3 - Math.floor(Math.log10(v))));
    return "$" + v.toFixed(digits).replace(/0+$/, "").replace(/\.$/, "");
  }
  function bigNumber(v) {
    if (v === null || v === undefined || isNaN(v)) return "–";
    var units = [[1e12, "T"], [1e9, "B"], [1e6, "M"], [1e3, "K"]];
    for (var i = 0; i < units.length; i++) {
      if (v >= units[i][0]) return (v / units[i][0]).toLocaleString("en-US", { maximumFractionDigits: 2 }) + units[i][1];
    }
    return v.toLocaleString("en-US", { maximumFractionDigits: 2 });
  }
  function usdBig(v) { return v === null || v === undefined || isNaN(v) ? "–" : v >= 1000 ? "$" + bigNumber(v) : usd(v); }
  function dmd(v) { return v === null || v === undefined || isNaN(v) ? "–" : bigNumber(v) + " DMD"; }
  function hashrate(v) {
    if (v === null || v === undefined || isNaN(v)) return "–";
    var units = [[1e18, "EH/s"], [1e15, "PH/s"], [1e12, "TH/s"], [1e9, "GH/s"], [1e6, "MH/s"], [1e3, "kH/s"]];
    for (var i = 0; i < units.length; i++) {
      if (v >= units[i][0]) return (v / units[i][0]).toLocaleString("en-US", { maximumFractionDigits: 2 }) + " " + units[i][1];
    }
    return Math.round(v) + " H/s";
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
  function line() {
    var span = document.createElement("span");
    Array.prototype.forEach.call(arguments, function (part, i) {
      if (part === null || part === undefined || part === "") return;
      if (span.childNodes.length) span.appendChild(document.createTextNode(" · "));
      span.appendChild(typeof part === "string" ? document.createTextNode(part) : part);
    });
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
      put("s-price", usd(ref.usd_per_dmd), line("from " + from, ref.change_24h ? change(ref.change_24h, "24h") : null));
    } else {
      put("s-price", "–", "not recorded yet");
    }
    put("s-cap", usdBig(s.market_cap_usd), line("at the reference price", s.market_cap_change_24h ? change(s.market_cap_change_24h, "24h") : null));
    var m = s.market || {};
    if (m.listed && m.trading_volume_24h_usd !== null && m.trading_volume_24h_usd !== undefined) {
      put("s-traded", usdBig(m.trading_volume_24h_usd), line(change(m.trading_volume_change_24h, "24h"), m.source ? "from " + m.source : null));
    } else {
      put("s-traded", "–", "not on an exchange yet");
    }
    var refPrice = ref ? ref.usd_per_dmd : null;
    put("s-volume", dmd(s.volume_24h_dmd), s.volume_24h_dmd === null || s.volume_24h_dmd === undefined
      ? "being counted" : line(refPrice ? "≈ " + usdBig(s.volume_24h_dmd * refPrice) : null, change(s.volume_change_24h, "")));
    put("s-cost", usd(s.mining_cost_usd), change(s.mining_cost_change_24h, "24h"));
    put("s-hashrate", hashrate(s.hashrate_24h), line("last hour " + hashrate(s.hashrate_1h), change(s.hashrate_change_24h, "")));
    put("s-difficulty", bigNumber(s.difficulty), change(s.difficulty_change_24h, "24h"));
    put("s-height", s.height.toLocaleString("en-US"), s.last_block ? "last block " + ago(s.last_block.time) : "");
    put("s-blocktime", minutes(s.block_seconds_24h));
    put("s-blocks", s.blocks_24h.toLocaleString("en-US"));
    put("s-txs", s.transactions_24h === undefined ? "–" : s.transactions_24h.toLocaleString("en-US"));
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

  // ------------------------------------------------------------------ the chart's data
  // Each metric gives its series for lines (points at their time) or candles (one per step, at
  // its start), its format, and whether it has candles. Values on a log scale are their log10.
  function fieldsOf(data) { var idx = {}; (data.fields || []).forEach(function (f, i) { idx[f] = i; }); return idx; }
  function pointsOf(data, name, scale) {
    var i = fieldsOf(data)[name];
    if (i === undefined) return [];
    return data.points.filter(function (p) { return p[i] !== null && p[i] !== undefined && (!scale || p[i] > 0); })
      .map(function (p) { return [p[0] * 1000, scale ? scale(p[i]) : p[i]]; });
  }
  function candlesOf(data, name, scale) {
    var list = (data.candles && data.candles[name]) || [];
    return list.map(function (c) {
      var y = c.slice(1, 5);
      if (scale) { if (y.some(function (v) { return !(v > 0); })) return null; y = y.map(scale); }
      return { x: c[0] * 1000, y: y };
    }).filter(Boolean);
  }
  function stepStart(data, name) {	// counts over a step: drawn at the step's start
    var i = fieldsOf(data)[name], step = (data.step || 0) * 1000, out = [], last = null;
    if (i === undefined) return out;
    data.points.forEach(function (p) {
      var t = p[0] * 1000, start = last === null ? t - step : last;
      last = t;
      if (p[i] !== null && p[i] !== undefined) out.push([start, p[i]]);
    });
    return out;
  }
  function log10(v) { return Math.log10(v); }
  function pow10(v) { return v === null || v === undefined ? null : Math.pow(10, v); }

  var METRICS = {
    hashrate: {
      note: "Worked out from the blocks found: an hour's average up to the 24-hour chart, 6 hours on 7 days, a day on the longer ones. A candle shows how that average moved during its period.",
      candles: true, format: hashrate, colours: function (c) { return [c.red]; },
      series: function (d, candles) {
        return candles ? [{ name: "Hashrate", type: "candlestick", data: candlesOf(d, "hashrate") }]
          : [{ name: "Hashrate", type: "area", data: pointsOf(d, "hashrate") }];
      }
    },
    price: {
      note: "Log scale. The reference price is the DebitMyData Gateway's (not a market price), as it recorded it; the mining cost is the electricity it takes to mine 1 DMD. The shaded part is from before the gateway: the cost is computed from the chain.",
      candles: true, log: true, format: function (v) { return usd(pow10(v)); }, colours: function (c) { return [c.red, c.gold]; },
      series: function (d, candles) {
        var cost = { name: "Mining cost", type: "line", data: pointsOf(d, "mining_cost", log10) };
        return candles ? [{ name: "Reference price", type: "candlestick", data: candlesOf(d, "price", log10) }, cost]
          : [{ name: "Reference price", type: "line", data: pointsOf(d, "price", log10) }, cost];
      }
    },
    cap: {
      note: "The reference price × the DMD mined so far. The reference price is not a market price: once DMD trades on an exchange, its market price says more.",
      candles: true, format: usdBig, colours: function (c) { return [c.red]; },
      series: function (d, candles) {
        return candles ? [{ name: "Market cap", type: "candlestick", data: candlesOf(d, "market_cap") }]
          : [{ name: "Market cap", type: "area", data: pointsOf(d, "market_cap") }];
      }
    },
    volume: {
      candles: false,
      note: function () {
        return state.volume === "exchanges"
          ? "What the exchanges trade in 24 hours, in US dollars, from CoinGecko, once DMD is listed on an exchange."
          : "The DMD the chain's transactions sent to other addresses, in each period: change sent back to the sender, and the mining rewards, are left out.";
      },
      format: function (v) { return state.volume === "exchanges" ? usdBig(v) : dmd(v); },
      colours: function (c) { return state.volume === "exchanges" ? [c.red] : [c.gold]; },
      series: function (d) {
        return state.volume === "exchanges"
          ? [{ name: "Trading volume (24h)", type: "area", data: pointsOf(d, "trading_volume_24h") }]
          : [{ name: "On-chain volume", type: "bar", data: stepStart(d, "volume") }];
      }
    },
    difficulty: {
      note: "The chain's difficulty, adjusted at every block (LWMA) so that one comes every 2.5 minutes on average.",
      candles: true, format: bigNumber, colours: function (c) { return [c.red]; },
      series: function (d, candles) {
        return candles ? [{ name: "Difficulty", type: "candlestick", data: candlesOf(d, "difficulty") }]
          : [{ name: "Difficulty", type: "area", data: pointsOf(d, "difficulty") }];
      }
    },
    blocktime: {
      note: "The average time between blocks, over the same windows as the hashrate. The chain aims at 2.5 minutes.",
      candles: false, target: 2.5, colours: function (c) { return [c.gold]; },
      format: function (v) { return v === null || v === undefined ? "–" : v.toFixed(2) + " min"; },
      series: function (d) {
        var i = fieldsOf(d).block_seconds;
        return [{ name: "Block time", type: "line", data: d.points.map(function (p) { return [p[0] * 1000, p[i] ? p[i] / 60 : null]; }) }];
      }
    }
  };

  function legend(def, c, series) {
    var box = $("dmd-legend");
    box.textContent = "";
    def.colours(c).forEach(function (colour, i) {
      if (!series[i]) return;
      var s = document.createElement("span");
      s.style.setProperty("--c", colour);
      s.textContent = series[i].name;
      box.appendChild(s);
    });
  }

  // The values of the series inside [min, max] (all of them without a window)
  function valuesIn(series, min, max) {
    var out = [];
    series.forEach(function (s) {
      s.data.forEach(function (p) {
        var x = Array.isArray(p) ? p[0] : p.x;
        if (min !== undefined && (x < min || x > max)) return;
        var y = Array.isArray(p) ? p[1] : p.y;
        if (Array.isArray(y)) y.forEach(function (v) { out.push(v); });
        else if (y !== null && y !== undefined) out.push(y);
      });
    });
    return out;
  }
  // The y axis for what is shown: on a log scale, a line at each power of ten ($0.0001, $0.001...)
  function yaxisFor(def, series, min, max) {
    var values = valuesIn(series, min, max);
    var labels = { formatter: function (v) { return def.format(v); }, minWidth: 40 };
    if (def.log) {
      // a little room above and below, so a line on a power of ten is not on the frame
      var low = values.length ? Math.floor(Math.min.apply(null, values) - 0.05) : -5;
      var high = values.length ? Math.ceil(Math.max.apply(null, values) + 0.05) : -2;
      if (high <= low) high = low + 1;
      return { min: low, max: high, tickAmount: high - low, labels: labels };
    }
    if (min === undefined || !values.length) return { forceNiceScale: true, min: series.some(function (s) { return s.type === "bar"; }) ? 0 : undefined, labels: labels };
    var lo = Math.min.apply(null, values), hi = Math.max.apply(null, values), pad = (hi - lo) * 0.08 || Math.abs(hi) * 0.05 || 1;
    var bars = series.some(function (s) { return s.type === "bar"; });
    return { min: bars ? 0 : Math.max(0, lo - pad), max: hi + pad, forceNiceScale: true, labels: labels };
  }

  // The tooltip of the candles: open, high, low, close (and the line under them)
  function candleTooltip(def, series) {
    return function (o) {
      var s = series[o.seriesIndex], p = s && s.data[o.dataPointIndex];
      if (!p) return "";
      var when = new Date(p.x);
      var head = '<div class="apexcharts-tooltip-title">' + when.toLocaleString([], tooltipDate()) + "</div>";
      if (!Array.isArray(p.y)) return head + '<div class="dmd-tip"><span>' + s.name + "</span><b>" + def.format(p.y) + "</b></div>";
      var rows = [["Open", p.y[0]], ["High", p.y[1]], ["Low", p.y[2]], ["Close", p.y[3]]];
      return head + '<div class="dmd-tip">' + rows.map(function (r) { return "<span>" + r[0] + "</span><b>" + def.format(r[1]) + "</b>"; }).join("") + "</div>";
    };
  }
  function tooltipDate() {
    return state.range === "1y" || state.range === "all" ? { year: "numeric", month: "short", day: "numeric" }
      : { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" };
  }

  // ------------------------------------------------------------------ drawing
  function destroy() {
    if (brush) { brush.destroy(); brush = null; }
    if (main) { main.destroy(); main = null; }
  }

  function controls() {
    var def = METRICS[state.metric];
    $("dmd-styles").hidden = !def.candles;
    $("dmd-volumes").hidden = state.metric !== "volume";
    select("dmd-metrics", "data-metric", state.metric);
    select("dmd-ranges", "data-range", state.range);
    select("dmd-styles", "data-style", state.style);
    select("dmd-volumes", "data-volume", state.volume);
  }

  function draw() {
    var data = state.charts[state.range];
    controls();
    if (!data || typeof ApexCharts === "undefined") return;
    var def = METRICS[state.metric], c = window.dmdChartTheme();
    var candles = def.candles && state.style === "candles";
    var series = def.series(data, candles);
    if (candles) {	// one data format in a chart: the lines' points as {x, y} too
      series.forEach(function (s) { s.data = s.data.map(function (p) { return Array.isArray(p) ? { x: p[0], y: p[1] } : p; }); });
    }
    var note = typeof def.note === "function" ? def.note() : def.note;
    var empty = $("dmd-chart-empty");
    destroy();
    $("dmd-chart-note").textContent = note;
    legend(def, c, series);
    if (state.metric === "volume" && state.volume === "exchanges" && !series[0].data.length) {
      empty.hidden = false;
      empty.innerHTML = "";
      var b = document.createElement("b"), p = document.createElement("p"), go = document.createElement("button");
      b.textContent = "DMD is not on an exchange yet";
      p.textContent = "Its trading volume will show here from its first day of trading on an exchange, with its changes over 24 hours.";
      go.type = "button"; go.className = "dmd-button dmd-ghost"; go.textContent = "See the on-chain volume";
      go.addEventListener("click", function () { state.volume = "chain"; keep(); draw(); });
      empty.appendChild(b); empty.appendChild(p); empty.appendChild(go);
      $("dmd-chart").setAttribute("aria-label", "Trading volume: none yet");
      $("dmd-zoom").parentNode.hidden = true;
      return;
    }
    empty.hidden = true;
    $("dmd-zoom").parentNode.hidden = false;

    var first = series[0].data, xs = valuesIn([{ data: first.map(function (p) { return Array.isArray(p) ? [p[0], p[0]] : [p.x, p.x]; }) }]);
    var fullMin = xs.length ? Math.min.apply(null, xs) : undefined, fullMax = xs.length ? Math.max.apply(null, xs) : undefined;
    var zoom = state.zoom && fullMin !== undefined && state.zoom.max > fullMin && state.zoom.min < fullMax ? state.zoom : null;
    if (!zoom) state.zoom = null;
    var annotations = { xaxis: [], yaxis: [] };
    var since = state.summary && state.summary.reference_price && state.summary.reference_price.since;
    if ((state.metric === "price" || state.metric === "cap") && since && data.points.length && data.points[0][0] * 1000 < new Date(since).getTime()) {
      annotations.xaxis.push({ x: data.points[0][0] * 1000, x2: new Date(since).getTime(), fillColor: c.grid, opacity: 0.6,
        borderColor: "transparent", label: { text: "computed", borderColor: "transparent", style: { background: "transparent", color: c.fore } } });
    }
    if (def.target) annotations.yaxis.push({ y: def.target, borderColor: c.green, strokeDashArray: 5,
      label: { text: "target", borderColor: "transparent", style: { background: "transparent", color: c.green } } });
    var types = series.map(function (s) { return s.type; });
    var phone = window.innerWidth < 760;
    var options = {
      chart: { id: "dmd-main", type: types[0] === "candlestick" ? "candlestick" : types[0], height: phone ? 300 : 360, foreColor: c.fore,
        fontFamily: "Mada, system-ui, sans-serif", toolbar: { show: false }, zoom: { enabled: false }, animations: { enabled: false },
        background: "transparent" },
      series: series,
      colors: def.colours(c),
      stroke: { width: types.map(function (t) { return t === "candlestick" ? 1 : t === "bar" ? 0 : 2.5; }), curve: "smooth",
        colors: candles ? types.map(function (t, i) { return t === "candlestick" ? c.fore : def.colours(c)[i]; }) : undefined },
      fill: { type: types.map(function (t) { return t === "area" ? "gradient" : "solid"; }), gradient: { opacityFrom: 0.35, opacityTo: 0.02 },
        opacity: types.map(function (t) { return t === "bar" ? 0.85 : 1; }) },
      plotOptions: { candlestick: { colors: { upward: c.green, downward: c.red }, wick: { useFillColor: true } },
        bar: { columnWidth: "80%", borderRadius: 2 } },
      dataLabels: { enabled: false },
      grid: { borderColor: c.grid, strokeDashArray: 3, padding: { left: 8, right: 12 } },
      legend: { show: false },
      markers: { size: 0, hover: { size: 4 } },
      xaxis: { type: "datetime", labels: { datetimeUTC: false }, axisBorder: { show: false }, axisTicks: { show: false },
        tooltip: { enabled: false }, min: zoom ? zoom.min : undefined, max: zoom ? zoom.max : undefined },
      yaxis: yaxisFor(def, series, zoom ? zoom.min : undefined, zoom ? zoom.max : undefined),
      tooltip: candles ? { theme: c.tooltip, shared: false, custom: candleTooltip(def, series) }
        : { theme: c.tooltip, shared: true, intersect: false, x: { format: state.range === "1y" || state.range === "all" ? "MMM d, yyyy" : "MMM d, HH:mm" },
          y: { formatter: function (v) { return def.format(v); } } },
      annotations: annotations,
      noData: { text: "No data for this period yet" }
    };
    $("dmd-chart").setAttribute("aria-label", series.map(function (s) { return s.name; }).join(" and ") + ", " + state.range);
    current = { def: def, series: series, min: fullMin, max: fullMax };
    main = new ApexCharts($("dmd-chart"), options);
    main.render();
    drawBrush(def, c, series);
    $("dmd-zoom-reset").hidden = !zoom;
  }

  // The zoom bar: the whole period small, under a window to drag or stretch (like CoinMarketCap's).
  // The small chart only shows the period; the window and its handles are the page's own, moved
  // with pointer events, so a mouse and a finger work the same.
  function brushLine(series) {
    var s = series[0];
    return s.data.map(function (p) { return Array.isArray(p) ? p : [p.x, Array.isArray(p.y) ? p.y[3] : p.y]; });
  }
  var pending = null;
  function drawBrush(def, c, series) {
    if (current.min === undefined) return;
    brush = new ApexCharts($("dmd-zoom"), {
      chart: { type: "area", height: window.innerWidth < 760 ? 76 : 64, sparkline: { enabled: true },
        background: "transparent", animations: { enabled: false } },
      series: [{ name: series[0].name, data: brushLine(series) }],
      colors: [def.colours(c)[0]],
      stroke: { width: 1.5, curve: "smooth" },
      fill: { type: "gradient", gradient: { opacityFrom: 0.3, opacityTo: 0.02 } },
      tooltip: { enabled: false },
      xaxis: { type: "datetime", min: current.min, max: current.max },
      yaxis: { min: series[0].type === "bar" ? 0 : undefined }
    });
    brush.render();
    placeWindow();
  }
  function zoomTo(min, max) {
    if (!current || !main) return;
    var whole = max - min >= (current.max - current.min) * 0.995;
    state.zoom = whole ? null : { min: min, max: max };
    $("dmd-zoom-reset").hidden = !state.zoom;
    placeWindow();
    if (pending) return;
    pending = window.requestAnimationFrame(function () {
      pending = null;
      if (!main) return;
      var z = state.zoom;
      main.updateOptions({ xaxis: { min: z ? z.min : undefined, max: z ? z.max : undefined },
        yaxis: yaxisFor(current.def, current.series, z ? z.min : undefined, z ? z.max : undefined) }, false, false, false);
    });
  }

  var track = $("dmd-zoom-track"), win = $("dmd-zoom-window"), drag = null;
  function span() { return current.max - current.min; }
  function shown() { return state.zoom || { min: current.min, max: current.max }; }
  function placeWindow() {
    if (!current || current.min === undefined || !(span() > 0)) return;
    var z = shown();
    var a = Math.max(0, (z.min - current.min) / span()), b = Math.min(1, (z.max - current.min) / span());
    win.style.left = a * 100 + "%";
    win.style.width = Math.max(0, b - a) * 100 + "%";
    $("dmd-zoom-shade-l").style.width = a * 100 + "%";
    $("dmd-zoom-shade-r").style.left = b * 100 + "%";
    $("dmd-zoom-shade-r").style.width = (1 - b) * 100 + "%";
    var when = tooltipDate();
    win.setAttribute("aria-valuetext", state.zoom ? new Date(z.min).toLocaleString([], when) + " to " + new Date(z.max).toLocaleString([], when) : "the whole period");
  }
  // a window of 3% of the period at least, inside the period
  function setRange(min, max, keepWidth) {
    var least = span() * 0.03;
    if (keepWidth) {
      var width = Math.min(max - min, span());
      min = Math.max(current.min, Math.min(min, current.max - width));
      max = min + width;
    } else {
      min = Math.max(current.min, min);
      max = Math.min(current.max, max);
      if (max - min < least) {
        if (drag && drag.mode === "min") min = max - least; else max = min + least;
      }
    }
    zoomTo(min, max);
  }
  function timeAt(clientX) {
    var r = track.getBoundingClientRect();
    return current.min + Math.max(0, Math.min(1, (clientX - r.left) / r.width)) * span();
  }
  track.addEventListener("pointerdown", function (e) {
    if (!current || !main || (e.pointerType === "mouse" && e.button !== 0)) return;
    var grip = e.target.closest("[data-grip]"), z = shown(), mode = grip ? grip.getAttribute("data-grip") : "move";
    if (!grip && !win.contains(e.target)) {
      // outside the window: the window goes there (a quarter of the period when nothing is zoomed)
      var width = state.zoom ? z.max - z.min : span() / 4, t = timeAt(e.clientX);
      setRange(t - width / 2, t + width / 2, true);
      z = shown();
    }
    drag = { mode: mode, x: e.clientX, min: z.min, max: z.max, width: track.getBoundingClientRect().width };
    try { track.setPointerCapture(e.pointerId); } catch (err) {}
    e.preventDefault();
  });
  track.addEventListener("pointermove", function (e) {
    if (!drag) return;
    var dt = (e.clientX - drag.x) / drag.width * span();
    if (drag.mode === "move") setRange(drag.min + dt, drag.max + dt, true);
    else if (drag.mode === "min") setRange(Math.min(drag.min + dt, drag.max), drag.max);
    else setRange(drag.min, Math.max(drag.max + dt, drag.min));
  });
  function stop() { drag = null; }
  track.addEventListener("pointerup", stop);
  track.addEventListener("pointercancel", stop);
  track.addEventListener("lostpointercapture", stop);
  track.addEventListener("dblclick", function () { state.zoom = null; draw(); });
  // the keyboard: the window or a handle, by 5% of the period (1% with Shift)
  track.addEventListener("keydown", function (e) {
    if (!current || (e.key !== "ArrowLeft" && e.key !== "ArrowRight")) return;
    var grip = e.target.closest("[data-grip]"), z = shown();
    var dt = (e.key === "ArrowLeft" ? -1 : 1) * span() * (e.shiftKey ? 0.01 : 0.05);
    if (!grip) setRange(z.min + dt, z.max + dt, true);
    else if (grip.getAttribute("data-grip") === "min") setRange(Math.min(z.min + dt, z.max), z.max);
    else setRange(z.min, Math.max(z.max + dt, z.min));
    e.preventDefault();
  });
  $("dmd-zoom-reset").addEventListener("click", function () { state.zoom = null; draw(); });

  function loadChart(range) {
    return getJSON("/api/chart-" + range + ".json").then(function (data) { state.charts[range] = data; });
  }

  function select(groupId, attr, value) {
    Array.prototype.forEach.call($(groupId).querySelectorAll("button"), function (b) {
      b.setAttribute("aria-pressed", b.getAttribute(attr) === value ? "true" : "false");
    });
  }
  function on(groupId, attr, apply) {
    $(groupId).addEventListener("click", function (e) {
      var b = e.target.closest("button[" + attr + "]");
      if (!b) return;
      apply(b.getAttribute(attr));
      keep();
    });
  }
  on("dmd-metrics", "data-metric", function (v) { if (METRICS[v]) { state.metric = v; draw(); } });
  on("dmd-styles", "data-style", function (v) { state.style = v === "candles" ? "candles" : "line"; draw(); });
  on("dmd-volumes", "data-volume", function (v) { state.volume = v === "chain" ? "chain" : "exchanges"; draw(); });
  on("dmd-ranges", "data-range", function (v) {
    state.range = v;
    state.zoom = null;
    controls();
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
  if (!METRICS[state.metric]) state.metric = "hashrate";
  if (!document.querySelector('#dmd-ranges [data-range="' + state.range + '"]')) state.range = "30d";
  controls();
  function refresh() {
    var range = state.range;
    return Promise.all([
      getJSON("/api/summary.json").then(showSummary),
      getJSON("/api/miners.json").then(function (m) { state.miners = m; window.dmdMiners = m; showMiners(); document.dispatchEvent(new CustomEvent("dmd-miners")); }),
      loadChart(range)
    ]).then(function () {
      Object.keys(state.charts).forEach(function (r) { if (r !== range) delete state.charts[r]; });
      draw();
    }).catch(function () {
      var pill = $("dmd-status");
      if (!state.summary) { pill.className = "dmd-pill"; pill.textContent = "The figures could not be loaded"; }
    });
  }
  var resized = null, width = window.innerWidth;
  window.addEventListener("resize", function () {
    if (window.innerWidth === width) return;	// a phone's bars sliding in and out
    width = window.innerWidth;
    clearTimeout(resized); resized = setTimeout(draw, 250);
  });
  refresh();
  setInterval(function () { if (!document.hidden) refresh(); }, 60000);
  document.addEventListener("visibilitychange", function () { if (!document.hidden) refresh(); });
})();
