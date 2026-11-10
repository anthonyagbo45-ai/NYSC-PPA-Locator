'use strict';
const CACHE = 'ppa-shell-v1';
const ASSETS = ['/static/locator/offline.html', '/static/locator/offline.js', '/static/locator/app.css'];
self.addEventListener('install', event => event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(ASSETS)).then(() => self.skipWaiting())));
self.addEventListener('activate', event => event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(key => key.startsWith('ppa-shell-') && key !== CACHE).map(key => caches.delete(key)))).then(() => self.clients.claim())));
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin || event.request.method !== 'GET') return;
  if (ASSETS.includes(url.pathname)) {
    event.respondWith(caches.open(CACHE).then(async cache => {
      try { const response = await fetch(event.request); if (response.ok) await cache.put(event.request, response.clone()); return response; }
      catch (_) { return cache.match(url.pathname); }
    }));
  } else if (event.request.mode === 'navigate') {
    // Never cache account pages, CSRF tokens, private requests or Google content.
    event.respondWith(fetch(event.request).catch(() => caches.match('/static/locator/offline.html')));
  }
});
