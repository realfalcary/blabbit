// Blabbit service worker
// Network-first for the app shell (HTML) so deploys always show up.
// Cache-first for static assets (images/CSS/JS) for speed + basic offline support.
// Never touches /socket.io/ (websocket) traffic.

const CACHE_NAME = 'blabbit-static-v2';
const PRECACHE_URLS = [
  '/static/favicon.png',
  '/static/logo.png'
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(PRECACHE_URLS))
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys
          .filter((key) => key !== CACHE_NAME)
          .map((key) => caches.delete(key))
      )
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  // Never intercept websocket / socket.io traffic — let it pass straight through.
  if (url.pathname.startsWith('/socket.io/')) {
    return;
  }

  // Only handle GET requests for our own origin.
  if (event.request.method !== 'GET' || url.origin !== self.location.origin) {
    return;
  }

  // Navigation requests (the HTML page itself) — always go to the network first,
  // so new deploys show up immediately. Fall back to cache only if offline.
  if (event.request.mode === 'navigate') {
    event.respondWith(
      fetch(event.request).catch(() => caches.match(event.request))
    );
    return;
  }

  // Static assets — cache-first for speed, but refresh the cache in the background.
  if (url.pathname.startsWith('/static/')) {
    event.respondWith(
      caches.match(event.request).then((cached) => {
        const fetchPromise = fetch(event.request).then((response) => {
          if (response.ok && response.type === 'basic') {
            const responseClone = response.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(event.request, responseClone));
          }
          return response;
        }).catch(() => cached);
        return cached || fetchPromise;
      })
    );
  }
});
