// DMD Stats' notifications (web push), the same way as the pool's: the bell in the top bar with
// this device's recent notices, and the Notifications card where it chooses what it wants: the
// network, the chain's safety, pools and miners, wallet addresses, each with its own figure.
// The monitoring server keeps the choices with the browser's subscription (/push) and sends the
// notices; turning them off here removes both. Nothing else is sent anywhere.
(function () {
  "use strict";
  var box = document.getElementById("dmd-bellbox"), card = document.getElementById("notifications");
  if (!box || !card) return;
  function $(id) { return document.getElementById(id); }
  var bell = $("dmd-bell"), panel = $("dmd-bell-panel");
  var list = panel.querySelector(".dmd-bell-list"), empty = panel.querySelector(".dmd-bell-empty");
  var all = panel.querySelector(".dmd-bell-all"), settings = panel.querySelector(".dmd-bell-settings");
  var actions = panel.querySelector(".dmd-bell-actions"), bellOn = panel.querySelector(".dmd-bell-on");
  var bellOff = panel.querySelector(".dmd-bell-off"), bellTest = panel.querySelector(".dmd-bell-test");
  var form = $("dmd-notify-form"), badge = $("dmd-notify-state"), msg = $("dmd-notify-msg");
  var turnOn = $("dmd-notify-on"), turnOff = $("dmd-notify-off"), test = $("dmd-notify-test");
  var poolList = $("dmd-pools"), poolPick = $("dmd-pool-pick"), addressList = $("dmd-addresses"), addressNew = $("dmd-address-new");

  var SEEN = "dmd-stats-seen", POOL = "the DebitMyData pool";
  var FLAGS = ["blocks", "halving", "daily", "status", "branches", "new_miners"];
  var FIGURES = ["hashrate", "difficulty", "price", "transfers", "slow", "reorg", "concentration", "richlist"];
  var ADDRESS = /^(dm1[02-9ac-hj-np-z]{6,87}|[1-9A-HJ-NP-Za-km-z]{25,40})$/;
  var config = null, sub = null, on = false, loaded = false, busy = false, testing = false;
  var items = [], pools = {}, addresses = [], note = "", saveTimer = null;
  var can = "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
  var iphone = /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  var inApp = window.matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;

  function blocked() { return can && Notification.permission === "denied"; }
  function minerName(name) { return name === POOL ? "DebitMyData pool" : name; }
  function say(text, bad) { msg.textContent = text || ""; msg.className = "dmd-small" + (bad ? " bad" : ""); }
  function post(data) {
    return fetch("/push", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (j) {
          if (!r.ok) throw new Error(j.error || (r.status === 429 ? "Too many requests: wait a moment." : "The server did not answer. Try again in a moment."));
          return j;
        });
      });
  }

  // ------------------------------------------------------------------ the choices in the card
  function figureBox(name) { return form.querySelector('.dmd-num[data-for="' + name + '"]'); }
  function readForm() {
    var prefs = {};
    FLAGS.forEach(function (n) { prefs[n] = form.elements[n].checked; });
    FIGURES.forEach(function (n) {
      var num = figureBox(n);
      prefs[n] = form.elements[n].checked ? Number(num.value) : null;
    });
    prefs.pools = pools;
    prefs.addresses = addresses.slice();
    return prefs;
  }
  function invalid() {
    var bad = null;
    FIGURES.forEach(function (n) {
      var num = figureBox(n);
      if (!bad && form.elements[n].checked && (num.value === "" || !num.checkValidity())) bad = num;
    });
    Array.prototype.forEach.call(poolList.querySelectorAll(".dmd-num"), function (num) {
      var box = num.closest("label").querySelector('input[type="checkbox"]');
      if (!bad && box.checked && (num.value === "" || !num.checkValidity())) bad = num;
    });
    return bad;
  }
  function fillForm(prefs) {
    FLAGS.forEach(function (n) { if (n in prefs) form.elements[n].checked = !!prefs[n]; });
    FIGURES.forEach(function (n) {
      if (!(n in prefs)) return;
      form.elements[n].checked = prefs[n] !== null && prefs[n] !== undefined;
      if (prefs[n] !== null && prefs[n] !== undefined) figureBox(n).value = prefs[n];
    });
    pools = prefs.pools || {};
    addresses = (prefs.addresses || []).slice();
    shade();
    drawPools();
    drawAddresses();
  }
  function shade() {
    Array.prototype.forEach.call(form.querySelectorAll(".dmd-opt"), function (label) {
      var box = label.querySelector('input[type="checkbox"]');
      label.classList.toggle("off", !box.checked);
    });
  }

  function number(value, min, max, step, label, onChange) {
    var input = document.createElement("input");
    input.type = "number"; input.className = "dmd-num"; input.min = min; input.max = max; input.step = step;
    input.inputMode = "decimal"; input.value = value; input.setAttribute("aria-label", label);
    input.addEventListener("input", onChange);
    return input;
  }
  function drawPools() {
    poolList.textContent = "";
    Object.keys(pools).forEach(function (name) {
      var choice = pools[name], li = document.createElement("li"), head = document.createElement("div");
      var b = document.createElement("b"), remove = document.createElement("button"), row = document.createElement("div");
      head.className = "dmd-follow-head"; row.className = "dmd-follow-choices";
      b.textContent = minerName(name);
      remove.type = "button"; remove.className = "dmd-unfollow"; remove.textContent = "Unfollow";
      remove.addEventListener("click", function () { delete pools[name]; drawPools(); changed(); });
      head.appendChild(b); head.appendChild(remove);
      function option(text, key, field) {
        var label = document.createElement("label"), box = document.createElement("input"), span = document.createElement("span");
        box.type = "checkbox";
        box.checked = key === "blocks" ? !!choice.blocks : choice[key] !== null && choice[key] !== undefined;
        label.appendChild(box);
        if (field) {
          var kept = choice[key] !== null && choice[key] !== undefined ? choice[key] : field.value;
          var num = number(kept, field.min, field.max, field.step, field.label, function () {
            box.checked = true; choice[key] = Number(num.value); changed();
          });
          span.appendChild(document.createTextNode(text[0]));
          span.appendChild(num);
          span.appendChild(document.createTextNode(text[1]));
          box.addEventListener("change", function () { choice[key] = box.checked ? Number(num.value) : null; changed(); });
        } else {
          span.textContent = text;
          box.addEventListener("change", function () { choice.blocks = box.checked; changed(); });
        }
        label.appendChild(span);
        row.appendChild(label);
      }
      option("Each block it finds", "blocks");
      option(["Hashrate moves by ", "%"], "hashrate", { value: 25, min: 1, max: 1000, step: 1, label: "Its hashrate change, percent" });
      option(["No block for ", " h"], "silence", { value: 2, min: 1, max: 168, step: 1, label: "Hours without a block" });
      li.appendChild(head); li.appendChild(row);
      poolList.appendChild(li);
    });
    fillPick();
  }
  function fillPick() {
    var miners = window.dmdMiners || {}, names = [];
    ["24h", "7d", "30d"].forEach(function (r) {
      ((miners[r] && miners[r].miners) || []).forEach(function (m) { if (names.indexOf(m.name) < 0) names.push(m.name); });
    });
    if (names.indexOf(POOL) < 0) names.unshift(POOL);
    poolPick.textContent = "";
    var first = document.createElement("option");
    first.value = ""; first.textContent = Object.keys(pools).length >= ((config && config.max_pools) || 10) ? "That's the most you can follow" : "Choose a pool or miner…";
    poolPick.appendChild(first);
    names.filter(function (n) { return !(n in pools); }).forEach(function (n) {
      var o = document.createElement("option");
      o.value = n; o.textContent = minerName(n);
      poolPick.appendChild(o);
    });
  }
  function drawAddresses() {
    addressList.textContent = "";
    addresses.forEach(function (address) {
      var li = document.createElement("li"), head = document.createElement("div"), code = document.createElement("code");
      var remove = document.createElement("button");
      head.className = "dmd-follow-head";
      code.textContent = address;
      remove.type = "button"; remove.className = "dmd-unfollow"; remove.textContent = "Unfollow";
      remove.addEventListener("click", function () { addresses = addresses.filter(function (a) { return a !== address; }); drawAddresses(); changed(); });
      head.appendChild(code); head.appendChild(remove);
      li.appendChild(head);
      addressList.appendChild(li);
    });
  }
  $("dmd-pool-add").addEventListener("click", function () {
    var name = poolPick.value;
    if (!name) return;
    if (Object.keys(pools).length >= ((config && config.max_pools) || 10)) { say("You can follow 10 pools and miners at most.", true); return; }
    pools[name] = { blocks: true, hashrate: null, silence: null };
    drawPools();
    changed();
  });
  function addAddress() {
    var address = addressNew.value.trim();
    if (!address) return;
    if (!ADDRESS.test(address)) { say("That is not a DMD address: it starts with dm1 (or D).", true); return; }
    if (addresses.indexOf(address) >= 0) { addressNew.value = ""; return; }
    if (addresses.length >= ((config && config.max_addresses) || 10)) { say("You can follow 10 addresses at most.", true); return; }
    addresses.push(address);
    addressNew.value = "";
    drawAddresses();
    changed();
  }
  $("dmd-address-add").addEventListener("click", addAddress);
  addressNew.addEventListener("keydown", function (e) { if (e.key === "Enter") { e.preventDefault(); addAddress(); } });
  form.addEventListener("change", function (e) { if (e.target.matches('.dmd-opt > input[type="checkbox"]')) { shade(); changed(); } });
  form.addEventListener("input", function (e) {
    var num = e.target.closest(".dmd-opt .dmd-num");
    if (!num) return;
    form.elements[num.getAttribute("data-for")].checked = true;
    shade();
    changed();
  });
  form.addEventListener("submit", function (e) { e.preventDefault(); });

  function problem(input) {
    var what = input.getAttribute("aria-label") || "a figure";
    var range = input.max ? "from " + input.min + " to " + input.max : input.min + " or more";
    return "Check the figure: " + what + " (" + range + ").";
  }

  // Changes are kept at once while notifications are on (after a short pause in typing)
  function changed() {
    if (!on) return;
    clearTimeout(saveTimer);
    saveTimer = setTimeout(save, 700);
  }
  function save() {
    var bad = invalid();
    if (bad) { say(problem(bad), true); return; }
    if (!sub) return;
    post({ action: "update", endpoint: sub.endpoint, prefs: readForm() }).then(function (r) {
      fillForm(r.prefs || readForm());
      say("Saved: this device follows your new choices.");
    }).catch(function (err) { say(err.message, true); });
  }

  // ------------------------------------------------------------------ on and off
  function key() {
    var s = config.key.replace(/-/g, "+").replace(/_/g, "/"); s += "===".slice((s.length + 3) % 4);
    var raw = atob(s), out = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
    return out;
  }
  function sameKey(s) {	// a subscription made with another key is made again
    var k = s.options && s.options.applicationServerKey;
    if (!k) return true;
    var a = new Uint8Array(k), b = key();
    if (a.length !== b.length) return false;
    for (var i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
    return true;
  }
  function start() {
    if (busy || !config || !config.enabled) return;
    var bad = invalid();
    if (bad) { say(problem(bad), true); return; }
    busy = true; note = "Turning on…"; draw();
    Notification.requestPermission().then(function (p) {
      if (p !== "granted") throw new Error(p === "denied" ? "Notifications are blocked for this site in your browser settings." : "Notifications were not allowed.");
      return navigator.serviceWorker.ready;
    }).then(function (reg) {
      return reg.pushManager.getSubscription().then(function (s) {
        if (s && !sameKey(s)) return s.unsubscribe().then(function () { return null; });
        return s;
      }).then(function (s) { return s || reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key() }); });
    }).then(function (s) {
      sub = s;
      return post({ action: "subscribe", subscription: s.toJSON(), prefs: readForm() });
    }).then(function (r) {
      on = true; note = ""; items = r.items || [];
      fillForm(r.prefs || readForm());
      say("Notifications are on for this device. Send yourself a test notice to check.");
    }).catch(function (err) {
      note = ""; say((err && err.message) || "Something went wrong. Try again in a moment.", true);
    }).then(function () { busy = false; draw(); });
  }
  function stop() {
    if (busy || !sub) return;
    busy = true; draw();
    var s = sub;
    post({ action: "unsubscribe", endpoint: s.endpoint }).then(function () { return s.unsubscribe(); }).then(function () {
      sub = null; on = false; items = [];
      say("Notifications are off on this device.");
    }).catch(function (err) { say((err && err.message) || "Something went wrong. Try again in a moment.", true); })
      .then(function () { busy = false; draw(); });
  }
  function sendTest() {
    if (!sub || testing) return;
    testing = true; draw();
    post({ action: "test", endpoint: sub.endpoint }).then(function () {
      say("A test notice is on its way: it should arrive within a few seconds.");
    }).catch(function (err) { say(err.message, true); }).then(function () {
      setTimeout(function () { testing = false; draw(); }, 10000);
      draw();
    });
  }

  // ------------------------------------------------------------------ the bell
  function seen() { try { return JSON.parse(localStorage.getItem(SEEN) || "[]"); } catch (e) { return []; } }
  function keepSeen(ids) { try { localStorage.setItem(SEEN, JSON.stringify(ids.slice(-500))); } catch (e) {} }
  function fresh() { var s = seen(); return items.filter(function (i) { return s.indexOf(i.id) < 0; }); }
  function ago(t) {
    var s = Math.max(0, Math.round(Date.now() / 1000 - t));
    if (s < 60) return "just now";
    if (s < 3600) return Math.floor(s / 60) + " min ago";
    if (s < 86400) return Math.floor(s / 3600) + " h ago";
    return new Date(t * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" });
  }
  function draw() {
    var news = fresh();
    bell.setAttribute("data-unread", news.length ? "1" : "0");
    bell.title = news.length ? news.length + (news.length > 1 ? " new notifications" : " new notification") : "Notifications";
    bell.setAttribute("aria-label", bell.title);
    list.textContent = "";
    news.forEach(function (item) {
      var li = document.createElement("li"), b = document.createElement("button");
      var head = document.createElement("span"), title = document.createElement("b"), when = document.createElement("time");
      var body = document.createElement("span");
      b.type = "button"; b.title = "Mark as seen";
      title.textContent = item.title;
      when.textContent = ago(item.time);
      when.dateTime = new Date(item.time * 1000).toISOString();
      body.textContent = item.body;
      head.className = "dmd-bell-item-head"; body.className = "dmd-bell-item-body";
      head.appendChild(title); head.appendChild(when);
      b.appendChild(head); b.appendChild(body);
      b.addEventListener("click", function (event) { event.stopPropagation(); keepSeen(seen().concat([item.id])); draw(); });
      li.appendChild(b);
      list.appendChild(li);
    });
    empty.hidden = news.length > 0 && !note;
    var why = "";
    if (!can && iphone && !inApp) why = 'On an iPhone, notifications come through the app: <a href="#get-the-app">add it to your Home Screen</a>, open it, and turn them on there.';
    else if (!can) why = "This browser cannot show notifications.";
    else if (blocked()) why = "Notifications are blocked for this site in your browser settings.";
    else if (loaded && config && !config.enabled) why = "Notifications are not available right now.";
    if (note) empty.textContent = note;
    else if (!loaded) empty.textContent = "Loading…";
    else if (on) empty.textContent = "You're all caught up.";
    else if (why) empty.innerHTML = why;
    else empty.textContent = "Get a notice on this device when something happens on the DebitMyData chain, even when this page is closed.";
    all.hidden = news.length < 2;
    var possible = loaded && can && !blocked() && config && config.enabled;
    actions.hidden = on || !possible;
    bellOn.disabled = turnOn.disabled = busy;
    bellOff.hidden = bellTest.hidden = turnOff.hidden = test.hidden = !on;
    bellOff.disabled = turnOff.disabled = busy;
    bellTest.disabled = test.disabled = busy || testing;
    turnOn.hidden = on || !possible;
    badge.textContent = on ? "On" : "Off";
    badge.className = "dmd-badge" + (on ? " on" : "");
    if (loaded && !on && why && !msg.textContent) { msg.innerHTML = why; msg.className = "dmd-small"; }
  }
  function open(yes) {
    panel.hidden = !yes;
    bell.setAttribute("aria-expanded", yes ? "true" : "false");
    if (yes) draw();
  }
  function load() {
    var ready = config ? Promise.resolve(config) : fetch("/push", { cache: "no-store" }).then(function (r) { return r.ok ? r.json() : { enabled: false }; })
      .catch(function () { return { enabled: false }; });
    return ready.then(function (c) {
      config = c;
      box.hidden = false;
      var floor = c.transfer_floor_dmd || 1000, transfers = figureBox("transfers");
      transfers.min = floor;
      if (Number(transfers.value) < floor) transfers.value = floor * 10;
      if (!can || !c.enabled) return { on: false };
      return navigator.serviceWorker.ready.then(function (reg) { return reg.pushManager.getSubscription(); }).then(function (s) {
        sub = s;
        if (!s) return { on: false };
        return post({ action: "get", endpoint: s.endpoint }).catch(function () { return { on: false }; });
      });
    }).then(function (r) {
      var first = !loaded;
      loaded = true;
      on = !!r.on;
      items = r.items || [];
      if (on && (first || !saveTimer)) fillForm(r.prefs);
      draw();
    }).catch(function () { loaded = true; draw(); });
  }
  function changedPicks() { fillPick(); }

  bell.addEventListener("click", function (event) { event.stopPropagation(); open(panel.hidden); });
  all.addEventListener("click", function (event) { event.stopPropagation(); keepSeen(seen().concat(items.map(function (i) { return i.id; }))); draw(); });
  bellOn.addEventListener("click", function (event) { event.stopPropagation(); start(); });
  bellOff.addEventListener("click", function (event) { event.stopPropagation(); stop(); });
  bellTest.addEventListener("click", function (event) { event.stopPropagation(); sendTest(); });
  turnOn.addEventListener("click", start);
  turnOff.addEventListener("click", stop);
  test.addEventListener("click", sendTest);
  document.addEventListener("click", function (event) { if (!panel.hidden && !box.contains(event.target)) open(false); });
  document.addEventListener("keydown", function (event) { if (event.key === "Escape" && !panel.hidden) { open(false); bell.focus(); } });
  settings.addEventListener("click", function (event) {
    event.preventDefault();
    open(false);
    card.scrollIntoView({ behavior: "smooth", block: "start" });
    card.classList.remove("dmd-flash");
    void card.offsetWidth;
    card.classList.add("dmd-flash");
  });
  document.addEventListener("dmd-miners", changedPicks);
  if ("serviceWorker" in navigator) navigator.serviceWorker.addEventListener("message", function (event) {
    if (event.data && event.data.type === "dmd-stats-notice") load();
  });
  shade();
  fillPick();
  draw();
  setInterval(function () { if (!document.hidden) load(); }, 60000);
  load();
})();
