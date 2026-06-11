// Thai Master PWA service worker
// Cache strategy:
//   - HTML/document: network-first (so updates propagate quickly)
//   - courses.json / audio-manifest.json: stale-while-revalidate
//   - Audio MP3: cache-first (lazily filled on first listen)
//   - Google Fonts CSS + font binaries: cache-first
//   - Other static (icon.svg, manifest.json): cache-first
// Bump VERSION whenever shell assets change so old caches get evicted.

const VERSION = 'v6';
const SHELL_CACHE = 'thai-shell-' + VERSION;
const DATA_CACHE  = 'thai-data-'  + VERSION;
const FONT_CACHE  = 'thai-font-'  + VERSION;
const AUDIO_CACHE = 'thai-audio-' + VERSION;

const SHELL_URLS = [
  './',
  './index.html',
  './manifest.json',
  './icon.svg',
  './icon-192.png',
  './icon-512.png'
];

self.addEventListener('install', (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(SHELL_CACHE);
    await cache.addAll(SHELL_URLS);
    await self.skipWaiting();
  })());
});

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    const keep = new Set([SHELL_CACHE, DATA_CACHE, FONT_CACHE, AUDIO_CACHE]);
    const keys = await caches.keys();
    await Promise.all(keys.filter(k => !keep.has(k)).map(k => caches.delete(k)));
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);

  // Audio MP3 — cache-first, fills on demand
  if (url.origin === self.location.origin && url.pathname.includes('/audio/') && url.pathname.endsWith('.mp3')) {
    event.respondWith(cacheFirst(req, AUDIO_CACHE));
    return;
  }

  // Course/audio manifest — stale-while-revalidate so updates propagate but UI never blocks
  if (url.origin === self.location.origin && (url.pathname.endsWith('/courses.json') || url.pathname.endsWith('/audio-manifest.json'))) {
    event.respondWith(staleWhileRevalidate(req, DATA_CACHE));
    return;
  }

  // Google Fonts (CSS + font binaries)
  if (url.host === 'fonts.googleapis.com' || url.host === 'fonts.gstatic.com') {
    event.respondWith(cacheFirst(req, FONT_CACHE));
    return;
  }

  // Same-origin: HTML network-first, everything else cache-first
  if (url.origin === self.location.origin) {
    if (req.mode === 'navigate' || req.destination === 'document' || url.pathname === '/' || url.pathname.endsWith('/') || url.pathname.endsWith('.html')) {
      event.respondWith(networkFirst(req, SHELL_CACHE));
    } else {
      event.respondWith(cacheFirst(req, SHELL_CACHE));
    }
  }
});

async function cacheFirst(req, cacheName) {
  const cache = await caches.open(cacheName);
  const cached = await cache.match(req);
  if (cached) return cached;
  try {
    const res = await fetch(req);
    if (res && res.ok) cache.put(req, res.clone());
    return res;
  } catch (e) {
    return cached || Response.error();
  }
}

async function networkFirst(req, cacheName) {
  const cache = await caches.open(cacheName);
  try {
    const res = await fetch(req);
    if (res && res.ok) cache.put(req, res.clone());
    return res;
  } catch (e) {
    const cached = await cache.match(req) || await cache.match('./index.html') || await cache.match('./');
    return cached || Response.error();
  }
}

async function staleWhileRevalidate(req, cacheName) {
  const cache = await caches.open(cacheName);
  const cached = await cache.match(req);
  const network = fetch(req).then(res => {
    if (res && res.ok) cache.put(req, res.clone());
    return res;
  }).catch(() => null);
  return cached || (await network) || Response.error();
}

// Allow clients to trigger immediate activation (e.g. on a "reload to update" prompt)
self.addEventListener('message', (event) => {
  if (event.data === 'SKIP_WAITING') self.skipWaiting();
});
