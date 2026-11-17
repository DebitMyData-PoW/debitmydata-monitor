// DMD Stats as an app (a web app people add to their phone or computer), the same way as the
// pool's (pool.debitmydata.io) and id.debitmydata.io's:
// - The "Get the app" window offers itself on phones and tablets a moment after the page opens,
//   and on any device from the footer's "Get the app" (or a link ending in #get-the-app).
// - It is never offered by itself inside the app, nor when the browser says the app is
//   installed (getInstalledRelatedApps): the web app, or the Google Play app
//   (io.debitmydata.stats) once manifest.json lists it. Only "Not now" is remembered, in this
//   browser, for 30 days.
// - When the browser offers its own one-tap install (Chrome, Edge, Samsung Internet), the window
//   shows the Install button; otherwise the steps for this device (iPhone: Share, then Add to
//   Home Screen; Android: the browser's menu).
// The service worker (/sw.js) shows a page when there is no network.
(function () {
  "use strict";
  var DECLINED = "dmd-stats-app-not-now", DECLINE_DAYS = 30, NAME = "DMD Stats";
  function $(s) { return document.querySelector(s); }

  if ("serviceWorker" in navigator) {
    window.addEventListener("load", function () { navigator.serviceWorker.register("/sw.js").catch(function () {}); });
  }

  var box = $("#dmd-install");
  if (!box) return;
  var inApp = window.matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
  if (inApp) document.documentElement.classList.add("dmd-in-app");
  var touch = window.matchMedia("(pointer: coarse)").matches;
  var iphone = /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  var device = iphone ? "iphone" : touch ? "android" : "computer";
  var canTell = "getInstalledRelatedApps" in navigator;
  var title = $("#dmd-install-title"), offerTitle = title.textContent;
  var how = $("#dmd-install-how"), go = $("#dmd-install-go"), later = $("#dmd-install-later");
  var prompt = null;	// the browser's own install dialog, when it offers one
  var installed = false, ready = false, closed = false;

  function declinedLately() {
    try {
      var at = Number(localStorage.getItem(DECLINED));
      return at > 0 && Date.now() - at < DECLINE_DAYS * 24 * 60 * 60 * 1000;
    } catch (e) { return false; }
  }
  function isInstalled() { return installed || inApp; }
  function fill() {
    var done = isInstalled();
    title.textContent = done ? (inApp ? "You're using the app" : "The app is already installed") : offerTitle;
    if (inApp) how.textContent = "This is the " + NAME + " app.";
    else if (done) how.textContent = "Open " + NAME + " from your " + (device === "computer" ? "computer's apps." : "home screen.");
    else if (prompt) how.textContent = "Install it: it opens full screen, like any app. Free, no app store.";
    else if (device === "computer") how.textContent = "In Chrome or Edge, click the install icon at the right of the address bar. In Safari on a Mac, choose File, then Add to Dock.";
    else how.textContent = "Add it to your home screen in two taps:";
    var byHand = !done && !prompt;
    $("#dmd-install-iphone").hidden = !(byHand && device === "iphone");
    $("#dmd-install-android").hidden = !(byHand && device === "android");
    $("#dmd-install-note").hidden = done || canTell;
    go.hidden = done || !prompt;
    later.textContent = done ? "OK" : "Not now";
  }
  function open() {
    fill();
    if (box.open) return;
    if (typeof box.showModal === "function") box.showModal(); else box.setAttribute("open", "");
  }
  function close() {
    closed = true;
    if (!box.open) return;
    if (typeof box.close === "function") box.close(); else box.removeAttribute("open");
  }
  // By itself: phones and tablets, a moment after the page opens, while nothing is being typed.
  function offer() {
    if (!ready || closed || !touch || isInstalled() || declinedLately()) return;
    var a = document.activeElement;
    if (a && (a.tagName === "INPUT" || a.tagName === "TEXTAREA")) return;
    open();
  }
  var check = (canTell ? navigator.getInstalledRelatedApps().then(function (apps) {
    return apps.some(function (app) { return app.platform === "webapp" || app.platform === "play"; });
  }, function () { return false; }) : Promise.resolve(false)).then(function (yes) { installed = yes; });
  // Asked for: always opens, and forgets an earlier "Not now".
  function ask() {
    closed = false;
    try { localStorage.removeItem(DECLINED); } catch (e) {}
    check.then(open);
  }

  window.addEventListener("beforeinstallprompt", function (event) {
    event.preventDefault();
    prompt = event;
    if (box.open) fill(); else offer();
  });
  go.addEventListener("click", function () {
    if (!prompt) return;
    var asked = prompt;
    prompt = null;
    asked.prompt();
    asked.userChoice.then(function (choice) {
      if (choice.outcome === "accepted") installed = true;
      close();	// "Cancel" in the browser's dialog only closes ours
    });
  });
  window.addEventListener("appinstalled", function () { installed = true; close(); });
  // "Not now": the only answer remembered.
  later.addEventListener("click", function () {
    if (!isInstalled()) { try { localStorage.setItem(DECLINED, String(Date.now())); } catch (e) {} }
    close();
  });
  $("#dmd-install-close").addEventListener("click", close);
  box.addEventListener("cancel", function (event) { event.preventDefault(); close(); });	// Escape, or the phone's Back
  box.addEventListener("click", function (event) { if (event.target === box) close(); });	// a tap outside the window
  // The footer's "Get the app" (and any link to #get-the-app) opens it here.
  document.addEventListener("click", function (event) {
    var link = event.target.closest && event.target.closest('a[href$="#get-the-app"]');
    if (!link) return;
    event.preventDefault();
    ask();
  });
  if (location.hash === "#get-the-app") {
    history.replaceState(null, "", location.pathname + location.search);
    ask();
  }
  check.then(function () { setTimeout(function () { ready = true; offer(); }, 2500); });
})();
