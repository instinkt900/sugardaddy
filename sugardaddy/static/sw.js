// sugardaddy service worker.
//
// This app is used on a LAN/VPN where the network is essentially always
// reachable, and it's still under active development, so freshness beats
// aggressive caching: everything is network-first with a cache fallback. When
// online you always get the latest code/markup; the cached shell only kicks in
// if the network is unavailable. Live data (/api/, /healthz) is never cached.
//
// It also receives Web Push messages: the server signs and encrypts them itself
// (see sugardaddy/notify.py), so the payload arriving here has only been relayed
// by the browser's push service, never read by it.
const CACHE = "sugardaddy-v4";
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
  "/static/icons/icon-badge-96.png",
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

self.addEventListener("push", (e) => {
  // A push with an undecodable body still deserves a notification: Android shows
  // a generic "site updated" one if showNotification is never called.
  let d = {};
  try {
    d = e.data ? e.data.json() : {};
  } catch (_) {
    d = {};
  }
  e.waitUntil(
    self.registration.showNotification(d.title || "Sugar Daddy", {
      body: d.body || "Something needs logging.",
      icon: "/static/icons/icon-192.png",
      // Android keeps only this image's ALPHA for the status bar and paints the
      // result white, so it has to be the bare droplet on transparency — point it
      // at the full-colour icon and you get a featureless white square. Generated
      // from icon-512.png by tools/make_badge_icon.py.
      badge: "/static/icons/icon-badge-96.png",
      tag: d.tag || "sugardaddy",       // replaces the previous one instead of stacking
      renotify: d.renotify !== false,   // reposts replace quietly; only the first alerts
      silent: d.silent === true,
      data: { url: d.url || "/" },
    })
  );
});

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || "/";
  // Focus the already-open app if there is one — opening a second window every
  // time you tap a notification gets old fast.
  e.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((wins) => {
      for (const w of wins) {
        if (new URL(w.url).pathname === url && "focus" in w) return w.focus();
      }
      return self.clients.openWindow(url);
    })
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
