// sugardaddy service worker.
//
// This app is used on a LAN/VPN where the network is essentially always
// reachable, and it's still under active development, so freshness beats
// aggressive caching: everything is network-first with a cache fallback. When
// online you always get the latest code/markup; the cached shell only kicks in
// if the network is unavailable. Live data (/api/, /healthz) is never cached.
const CACHE = "sugardaddy-v2";
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
  // Precache the shell so a cold offline start still renders, then take over.
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

  // Only handle same-origin requests; let anything else pass through untouched.
  if (url.origin !== self.location.origin) return;

  // Live data must always be fresh and never cached.
  if (url.pathname.startsWith("/api/") || url.pathname === "/healthz") return;

  // Everything else (pages + static assets): network-first, refreshing the
  // cache on every successful fetch, falling back to cache when offline.
  e.respondWith(
    fetch(e.request)
      .then((resp) => {
        if (resp && resp.ok) {
          const copy = resp.clone();
          caches.open(CACHE).then((c) => c.put(e.request, copy));
        }
        return resp;
      })
      .catch(() =>
        caches.match(e.request).then(
          (cached) => cached || (e.request.mode === "navigate" ? caches.match("/") : undefined)
        )
      )
  );
});
