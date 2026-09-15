// Blabbit service worker
// Minimal caching: static assets only. Never touches /socket.io/ (websocket) traffic.

const CACHE_NAME = 'blabbit-static-v1';
const PRECACHE_URLS = [
  '/',
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

  // Only handle GET requests for our own origin's static assets.
  if (event.request.method !== 'GET' || url.origin !== self.location.origin) {
    return;
  }

  event.respondWith(
    caches.match(event.request).then((cached) => {
      if (cached) return cached;
      return fetch(event.request).then((response) => {
        // Only cache successful, basic (same-origin) responses for static files.
        if (
          response.ok &&
          response.type === 'basic' &&
          url.pathname.startsWith('/static/')
        ) {
          const responseClone = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(event.request, responseClone));
        }
        return response;
      }).catch(() => cached);
    })
  );
});
