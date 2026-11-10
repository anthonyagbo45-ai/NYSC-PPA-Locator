'use strict';
// Execute the worker's real event handlers against an isolated browser API fake.
// This tests behavior rather than merely searching its source for safety comments.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const origin = 'https://ppa.example.com';
const handlers = new Map();
const writes = [];
const matches = [];
const added = [];
let fetchFails = false;
let fetchOk = true;
const response = { ok: true, clone: () => ({ body: 'shell asset' }) };
const cachedResponse = { offline: true };
const context = vm.createContext({
  URL,
  self: {
    location: new URL(origin),
    addEventListener: (name, handler) => handlers.set(name, handler),
    skipWaiting: async () => {},
    clients: { claim: async () => {} },
  },
  caches: {
    open: async () => ({
      addAll: async assets => added.push(...assets),
      put: async request => writes.push(new URL(request.url).pathname),
      match: async path => { matches.push(path); return cachedResponse; },
    }),
    keys: async () => [],
    delete: async () => true,
    match: async path => { matches.push(path); return cachedResponse; },
  },
  fetch: async () => {
    if (fetchFails) throw new Error('Simulated disconnection');
    return { ...response, ok: fetchOk };
  },
});
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), context, { filename: 'service-worker.js' });

async function dispatch(kind, request) {
  let pending;
  handlers.get(kind)({ request, waitUntil: value => { pending = value; }, respondWith: value => { pending = value; } });
  return pending ? await pending : undefined;
}
function request(path, mode = 'navigate', method = 'GET') {
  return { url: new URL(path, origin).href, mode, method };
}

(async () => {
  await dispatch('install');
  assert.deepEqual(added.sort(), ['/static/locator/app.css', '/static/locator/offline.html', '/static/locator/offline.js'].sort());
  await dispatch('fetch', request('/static/locator/app.css', 'cors'));
  assert.deepEqual(writes, ['/static/locator/app.css']);
  writes.length = 0;
  for (const path of ['/login/', '/register/', '/profile/', '/saved/', '/ppas/1/', '/my-reports/']) {
    await dispatch('fetch', request(path));
  }
  assert.deepEqual(writes, []);
  assert.deepEqual(matches, []);
  assert.equal(await dispatch('fetch', request('/ppas/1/confirm/', 'cors', 'POST')), undefined);
  assert.equal(await dispatch('fetch', request('/api/load-lgas/?state=1', 'cors')), undefined);
  assert.equal(await dispatch('fetch', request('https://maps.googleapis.com/maps/api/js', 'cors')), undefined);
  assert.deepEqual(writes, []);
  fetchOk = false;
  await dispatch('fetch', request('/static/locator/app.css', 'cors'));
  assert.deepEqual(writes, []);
  fetchFails = true;
  assert.equal(await dispatch('fetch', request('/profile/')), cachedResponse);
  assert.deepEqual(matches, ['/static/locator/offline.html']);
  assert.deepEqual(writes, []);
  console.log('Service worker shell-only caching checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
