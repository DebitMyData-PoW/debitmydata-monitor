"use strict";
// The DMD Stats app's service worker (js/install.js registers it).
// - Pages and figures always come from the network, so they are live. Without a network, a
//   short "You're offline" page shows instead of the browser's error.
// - The app's icon and the notifications' badge are kept, for that page and the notices.
// - The notifications (js/notify.js) arrive here and are shown; a tap opens the page they are
//   about (the stats page, or a transaction in the explorer). The monitoring server sends
//   {title, body, url, tag, id, time} (chainwatch/notify.py).
const CACHE = "dmd-stats-app-2";
const ICON = "/images/app/icon-192.png?v=1";
const BADGE = "/images/app/badge-96.png?v=1";

const OFFLINE = `<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Offline · DMD Stats</title>
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;font:16px/1.5 system-ui,sans-serif;background:#fff;color:#13111a;text-align:center;padding:24px}
@media (prefers-color-scheme:dark){body{background:#0b0a0f;color:#f7f5fa}}img{width:96px;height:96px}h1{font-size:1.4rem;margin:12px 0 6px}
p{margin:0 0 18px;color:#6e6a78}button{font:600 1rem system-ui,sans-serif;color:#fff;background:linear-gradient(135deg,#e0112b,#ff5a36);border:0;border-radius:999px;padding:12px 22px}</style></head>
<body><div><img src="${ICON}" alt=""><h1>You're offline</h1><p>The network's figures need an internet connection.</p>
<button onclick="location.reload()">Try again</button></div></body></html>`;

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE)
      .then((cache) => cache.addAll([ICON, BADGE].map((path) => new Request(path, { cache: "reload" }))))
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
  if (url.pathname === "/images/app/icon-192.png" || url.pathname === "/images/app/badge-96.png") {
    event.respondWith(caches.match(request, { ignoreSearch: true }).then((cached) => cached || fetch(request)));
  }
});

// A notification from the monitoring server.
self.addEventListener("push", (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (e) { data = { body: event.data ? event.data.text() : "" }; }
  const title = data.title || "DMD Stats";
  event.waitUntil(self.registration.showNotification(title, {
    body: data.body || "",
    icon: ICON,
    badge: BADGE,
    tag: data.tag || undefined,      // a newer notice of the same kind replaces the older one
    renotify: !!data.tag,
    timestamp: data.time ? data.time * 1000 : undefined,
    data: { url: data.url || "/" },
  }).then(() => self.clients.matchAll({ type: "window", includeUncontrolled: true }))
    .then((list) => list.forEach((client) => client.postMessage({ type: "dmd-stats-notice" }))));   // the bell's list
});

// The browser replaced the subscription (its keys expired, say): the server is given the new one.
self.addEventListener("pushsubscriptionchange", (event) => {
  const old = event.oldSubscription;
  if (!old) return;
  event.waitUntil(
    (event.newSubscription ? Promise.resolve(event.newSubscription) : self.registration.pushManager.subscribe(old.options))
      .then((subscription) => fetch("/push", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "renew", old: old.endpoint, subscription: subscription.toJSON() }),
      }))
      .catch(() => {})
  );
});

// A tap on it: the page it is about, in the app or tab already open when it is this site's.
self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = new URL((event.notification.data && event.notification.data.url) || "/", self.location.origin);
  const here = target.origin === self.location.origin;
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((list) => {
      if (here) {
        for (const client of list) {
          if (client.url === target.href && "focus" in client) return client.focus();
        }
        for (const client of list) {
          if ("navigate" in client && "focus" in client) return client.navigate(target.href).then((c) => (c || client).focus());
        }
      }
      return self.clients.openWindow(target.href);
    })
  );
});
