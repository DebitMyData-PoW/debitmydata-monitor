"use strict";
// The DMD Stats app's service worker (js/install.js registers it).
// - Pages and figures always come from the network, so they are live. Without a network, a
//   short "You're offline" page shows instead of the browser's error.
// - The app's icon is kept, for that page.
const CACHE = "dmd-stats-app-1";
const ICON = "/images/app/icon-192.png?v=1";

const OFFLINE = `<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Offline · DMD Stats</title>
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;font:16px/1.5 system-ui,sans-serif;background:#fff;color:#211f1f;text-align:center;padding:24px}
@media (prefers-color-scheme:dark){body{background:#18181d;color:#ececf1}}img{width:96px;height:96px}h1{font-size:1.4rem;margin:12px 0 6px}
p{margin:0 0 18px;color:#7d8797}button{font:700 1rem system-ui,sans-serif;color:#fff;background:#c80815;border:0;border-radius:999px;padding:12px 22px}</style></head>
<body><div><img src="${ICON}" alt=""><h1>You're offline</h1><p>The network's figures need an internet connection.</p>
<button onclick="location.reload()">Try again</button></div></body></html>`;

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE)
      .then((cache) => cache.add(new Request(ICON, { cache: "reload" })))
      .catch(() => {})
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((names) => Promise.all(names.filter((name) => name.startsWith("dmd-stats-app-") && name !== CACHE).map((name) => caches.delete(name))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (request.mode === "navigate") {
    event.respondWith(fetch(request).catch(() => new Response(OFFLINE, { headers: { "Content-Type": "text/html; charset=utf-8" } })));
    return;
  }
  if (url.pathname === "/images/app/icon-192.png") {
    event.respondWith(caches.match(request, { ignoreSearch: true }).then((cached) => cached || fetch(request)));
  }
});
