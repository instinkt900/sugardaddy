// sugardaddy service worker — caches the app shell so the UI loads instantly and
// works offline, while always fetching live glucose/entry data from the network.
const CACHE = "sugardaddy-v1";
const SHELL = [
  "/", "/desktop",
  "/manifest.webmanifest",
  "/static/style.css",
  "/static/common.js",
  "/static/phone.js",
  "/static/desktop.js",
  "/static/htmx.min.js",
  "/static/chart.umd.min.js",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  if (e.request.method !== "GET") return; // logging POST/PATCH/DELETE: never intercept
  const url = new URL(e.request.url);

  // Live data must always be fresh — let it hit the network directly.
  if (url.pathname.startsWith("/api/") || url.pathname === "/healthz") return;

  // Pages: network-first (get updates), fall back to cache when offline.
  if (e.request.mode === "navigate") {
    e.respondWith(
      fetch(e.request).catch(() => caches.match(e.request).then((r) => r || caches.match("/")))
    );
    return;
  }

  // Static assets: cache-first, populate on miss.
  e.respondWith(
    caches.match(e.request).then((cached) =>
      cached ||
      fetch(e.request).then((resp) => {
        const copy = resp.clone();
        caches.open(CACHE).then((c) => c.put(e.request, copy));
        return resp;
      })
    )
  );
});
