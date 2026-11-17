// The API page: the theme switch, "Try it" (the file's start, live) and "Copy".
(function () {
  "use strict";
  var root = document.documentElement;
  var toggle = document.getElementById("dmd-theme-toggle");
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
  });
  label();
  var year = document.getElementById("dmd-year");
  if (year) year.textContent = String(new Date().getFullYear());

  // "Try it": the live file, shortened (a chart's points and candles: their first and last)
  function shorten(data) {
    if (data && Array.isArray(data.points)) {
      var copy = JSON.parse(JSON.stringify(data));
      if (copy.points.length > 3) copy.points = [copy.points[0], "…", copy.points[copy.points.length - 1]];
      Object.keys(copy.candles || {}).forEach(function (k) {
        var c = copy.candles[k];
        if (c.length > 2) copy.candles[k] = [c[0], "…"];
      });
      return copy;
    }
    ["addresses", "transfers"].forEach(function (k) { if (data && Array.isArray(data[k]) && data[k].length > 5) data[k] = data[k].slice(0, 5).concat(["…"]); });
    return data;
  }
  Array.prototype.forEach.call(document.querySelectorAll("[data-try]"), function (button) {
    button.addEventListener("click", function () {
      var out = button.closest(".dmd-endpoint").querySelector(".dmd-try-out");
      if (!out.hidden) { out.hidden = true; button.textContent = "Try it"; return; }
      out.hidden = false;
      out.textContent = "Loading…";
      button.textContent = "Hide";
      fetch(button.getAttribute("data-try"), { cache: "no-cache" }).then(function (r) { return r.json(); })
        .then(function (data) { out.textContent = JSON.stringify(shorten(data), null, 1); })
        .catch(function () { out.textContent = "The file could not be read just now."; });
    });
  });
  Array.prototype.forEach.call(document.querySelectorAll("[data-copy]"), function (button) {
    button.addEventListener("click", function () {
      var text = button.getAttribute("data-copy");
      (navigator.clipboard ? navigator.clipboard.writeText(text) : Promise.reject()).then(function () {
        button.textContent = "Copied";
        setTimeout(function () { button.textContent = "Copy"; }, 1500);
      }).catch(function () {});
    });
  });
})();
