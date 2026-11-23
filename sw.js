// self-destruct v5: if an ancient client somehow still runs us, nuke all caches and unregister
// ANOSagent portal service worker — v1 (Oct 2026)
// Strategy: precache nothing; runtime-cache GET statics (icons, manifest, tiles).
// HTML and /api/* are always network-first so data is never stale.
const CACHE = 'anos-pwa-v4';
const CACHEABLE = /^\/(icon-|apple-touch-icon|anos-.*\.png|manifest\.webmanifest|tiles\/)/;

self.addEventListener('install', (e) => self.skipWaiting());
self.addEventListener('activate', (e) => {
  // wipe every cache from any version, then claim so old shells die now
  e.waitUntil(caches.keys().then(keys => Promise.all(keys.map(k => caches.delete(k)))));
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return; // leaflet CDN etc. — let browser handle

  if (CACHEABLE.test(url.pathname)) {
    // cache-first for immutable-ish assets
    e.respondWith(
      caches.match(req).then((hit) => {
        if (hit) return hit;
        return fetch(req).then((res) => {
          if (res.ok) {
            const clone = res.clone();
            caches.open(CACHE).then((c) => c.put(req, clone));
          }
          return res;
        });
      })
    );
    return;
  }

  if (req.mode === 'navigate') {
    // network-first for pages; fall back to cache when offline
    e.respondWith(
      fetch(req).then((res) => {
        const clone = res.clone();
        caches.open(CACHE).then((c) => c.put(req, clone));
        return res;
      }).catch(() => caches.match(req).then((hit) => hit || caches.match('/')))
    );
    return;
  }
  // /api/* and everything else: plain network, no cache
});
