// Runs in <head>, before the page is drawn: the theme (light unless this site's cookie says
// dark), the chart colours for it, and in the app the launch screen (icon and name), once per start.
(function () {
  "use strict";
  var root = document.documentElement, t = null;
  try {
    var m = document.cookie.match(/(?:^|;\s*)dmd-theme-stats=(light|dark)/);
    t = m ? m[1] : localStorage.getItem("dmd-theme-stats");
  } catch (e) {}
  if (t !== "light" && t !== "dark") { t = "light"; }
  root.setAttribute("data-theme", t);
  root.style.colorScheme = t;
  window.dmdChartTheme = function () {
    var dark = root.getAttribute("data-theme") === "dark";
    return { fore: dark ? "#9a9ba6" : "#7d8797", grid: dark ? "#2c2c35" : "#f0f1f4", tooltip: dark ? "dark" : "light",
      red: dark ? "#ff5a64" : "#c80815", gold: dark ? "#e6b422" : "#c99a06", green: dark ? "#2fbf7a" : "#098551",
      zoom: dark ? "#64748b" : "#94a3b8" };	// the zoom bar: discreet, under the chart
  };

  try {
    var app = window.matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
    if (!app || sessionStorage.getItem("dmd-app-started")) return;
    sessionStorage.setItem("dmd-app-started", "1");
  } catch (e) { return; }
  var start = Date.now(), done = false;
  root.classList.add("dmd-launch");
  function hide() {
    if (done) return;
    done = true;
    setTimeout(function () {
      root.classList.add("dmd-launch-out");
      setTimeout(function () { root.classList.remove("dmd-launch", "dmd-launch-out"); }, 400);
    }, Math.max(0, 700 - (Date.now() - start)));
  }
  if (document.readyState === "complete") hide(); else window.addEventListener("load", hide);
  setTimeout(hide, 3000);
})();
