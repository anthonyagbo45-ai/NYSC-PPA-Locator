'use strict';
(() => {
  const configNode = document.getElementById('navigation-config');
  const dialog = document.getElementById('route-dialog');
  if (!configNode || !dialog) return;
  let config;
  try { config = JSON.parse(configNode.textContent); } catch (_) { return; }
  const map = document.getElementById('route-map');
  const form = document.getElementById('route-origin-form');
  const input = document.getElementById('route-origin');
  const mode = document.getElementById('route-mode');
  const status = document.getElementById('route-status');
  const liveLink = document.getElementById('route-live-link');
  const summary = document.getElementById('route-summary');
  const duration = document.getElementById('route-duration');
  const distance = document.getElementById('route-distance');
  const summaryNote = document.getElementById('route-summary-note');
  let origin = null;
  let opener = null;
  let controller = null;
  let mapTimer = null;
  let generation = 0;
  let locationRequest = 0;

  function tell(message, error = false) {
    status.textContent = message;
    status.dataset.state = error ? 'error' : 'ready';
  }
  function clearSummary(message = 'Journey estimates appear when a route is available.') {
    duration.textContent = '—'; distance.textContent = '—';
    summary.setAttribute('aria-busy', 'false'); summaryNote.textContent = message;
  }
  function placeholder(title, note = 'Your destination stays selected.') {
    const wrap = document.createElement('div'); wrap.className = 'route-map-placeholder';
    const symbol = document.createElement('span'); symbol.className = 'route-map-symbol'; symbol.textContent = '↗'; symbol.setAttribute('aria-hidden', 'true');
    const heading = document.createElement('p'); heading.textContent = title;
    const text = document.createElement('p'); text.className = 'muted'; text.textContent = note;
    wrap.append(symbol, heading, text); map.replaceChildren(wrap);
  }
  function originText() {
    return origin?.address || (origin ? `${origin.latitude},${origin.longitude}` : '');
  }
  function updateLiveLink() {
    try {
      const url = new URL(config.external_url);
      if (url.protocol !== 'https:' || url.hostname !== 'www.google.com' || !url.pathname.startsWith('/maps/dir/')) return;
      url.searchParams.set('travelmode', mode.value);
      url.searchParams.set('dir_action', 'navigate');
      if (origin) url.searchParams.set('origin', originText()); else url.searchParams.delete('origin');
      liveLink.href = url.href;
    } catch (_) {}
  }
  function formatDuration(seconds) {
    const minutes = Math.max(1, Math.ceil(seconds / 60));
    return minutes < 60 ? `${minutes} min` : `${Math.floor(minutes / 60)} hr${minutes % 60 ? ` ${minutes % 60} min` : ''}`;
  }
  function formatDistance(meters) {
    return meters < 1000 ? `${Math.round(meters)} m` : `${new Intl.NumberFormat(undefined, {maximumFractionDigits: 1}).format(meters / 1000)} km`;
  }
  async function estimate(currentGeneration) {
    if (controller) controller.abort();
    controller = new AbortController();
    const requestController = controller;
    const csrf = form.querySelector('[name=csrfmiddlewaretoken]')?.value;
    let endpoint;
    try {
      endpoint = new URL(config.summary_url || '/api/route/', location.origin);
      if (endpoint.origin !== location.origin || !csrf) throw new Error('Unavailable');
    } catch (_) { clearSummary('Travel estimates are unavailable. You can still use the route map.'); return; }
    clearSummary('Getting travel estimates…'); summary.setAttribute('aria-busy', 'true');
    const payload = {origin, mode: mode.value};
    if (config.ppa_id) payload.ppa_id = config.ppa_id;
    else if (config.preview_token) payload.preview_token = config.preview_token;
    else { clearSummary('Travel estimates are unavailable. You can still use the route map.'); return; }
    const timeout = setTimeout(() => requestController.abort(), 14000);
    try {
      const response = await fetch(endpoint.href, {method: 'POST', credentials: 'same-origin', cache: 'no-store',
        headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrf}, body: JSON.stringify(payload), signal: requestController.signal});
      const result = await response.json();
      if (currentGeneration !== generation || !dialog.open) return;
      if (!response.ok || result.status !== 'ok' || !Number.isFinite(result.duration_seconds) || result.duration_seconds < 0 || !Number.isFinite(result.distance_meters) || result.distance_meters < 0) {
        clearSummary(result.status === 'no_route' ? 'No route was found for this starting point and travel mode. Try a nearby landmark or another mode.' : 'Travel estimates are unavailable. The map or live navigation may still provide a route.');
        return;
      }
      duration.textContent = formatDuration(result.duration_seconds);
      distance.textContent = formatDistance(result.distance_meters);
      summary.setAttribute('aria-busy', 'false');
      summaryNote.textContent = 'Estimated journey. Check the map route and current travel conditions.';
    } catch (_) {
      if (currentGeneration === generation && dialog.open) clearSummary('Travel estimates could not be loaded. You can retry the route or start live navigation.');
    } finally { clearTimeout(timeout); }
  }
  function showRoute() {
    if (!origin || !dialog.open) return;
    generation += 1;
    const currentGeneration = generation;
    if (controller) controller.abort();
    clearTimeout(mapTimer);
    updateLiveLink();
    if (!navigator.onLine) {
      placeholder('You’re offline', 'Your destination is still available. Connect to load maps and live routes.');
      clearSummary('Maps and travel estimates require a connection.');
      tell('You’re offline. Your destination has been kept. Retry when your connection returns.', true);
      return;
    }
    if (config.browser_key && typeof config.destination === 'string' && config.destination) {
      const url = new URL('https://www.google.com/maps/embed/v1/directions');
      url.search = new URLSearchParams({key: config.browser_key, origin: originText(), destination: config.destination, mode: mode.value}).toString();
      const frame = document.createElement('iframe');
      frame.title = `Route to ${config.name || 'your PPA'}`;
      frame.referrerPolicy = 'strict-origin-when-cross-origin';
      frame.allowFullscreen = true;
      frame.loading = 'eager';
      frame.addEventListener('load', () => {
        if (currentGeneration !== generation) return;
        clearTimeout(mapTimer);
        tell('Check your starting point, destination and route on the map.');
      });
      frame.addEventListener('error', () => {
        if (currentGeneration === generation) tell('The route map could not load. Retry, or use Start live navigation.', true);
      });
      frame.src = url.href; map.replaceChildren(frame);
      tell('Loading your route map…');
      mapTimer = setTimeout(() => { if (currentGeneration === generation && dialog.open) tell('The map is taking longer than expected. You can retry or start live navigation.', true); }, 16000);
    } else {
      placeholder('The route map is temporarily unavailable', 'You can still open live navigation to your selected PPA.');
      tell('The route map is unavailable here. Your destination is ready for live navigation.', true);
    }
    estimate(currentGeneration);
  }
  function locate() {
    if (!dialog.open) return;
    const currentRequest = ++locationRequest;
    if (!navigator.onLine) { placeholder('You’re offline', 'Connect to load maps and routes, or keep the address for later.'); tell('You’re offline. You can enter a starting address and retry once connected.', true); input.focus(); return; }
    if (!navigator.geolocation) { tell('Current location is unavailable. Enter your starting address instead.', true); input.focus(); return; }
    tell('Finding your starting point. Allow location access, or enter an address below.');
    navigator.geolocation.getCurrentPosition(position => {
      if (currentRequest !== locationRequest || !dialog.open) return;
      const latitude = position.coords.latitude; const longitude = position.coords.longitude;
      if (!Number.isFinite(latitude) || !Number.isFinite(longitude) || Math.abs(latitude) > 90 || Math.abs(longitude) > 180) {
        tell('Your starting location could not be read. Enter an address instead.', true); input.focus(); return;
      }
      origin = {latitude, longitude}; input.value = ''; input.placeholder = 'Current location selected — or enter another address'; showRoute();
    }, error => {
      if (currentRequest !== locationRequest || !dialog.open) return;
      tell(error.code === 1 ? 'Location access was not granted. Enter your starting address below.' : 'Your current location could not be found. Enter a starting address or try location again.', true);
      input.focus();
    }, {timeout: 10000, maximumAge: 60000, enableHighAccuracy: false});
  }
  function close() {
    generation += 1; locationRequest += 1;
    if (controller) controller.abort();
    clearTimeout(mapTimer);
    origin = null; input.value = ''; input.placeholder = 'Enter an address or nearby landmark';
    placeholder('Your route will appear here.', 'Choose your starting point to continue.'); clearSummary();
    updateLiveLink(); document.body.classList.remove('route-dialog-open');
    opener?.focus();
  }
  for (const button of document.querySelectorAll('[data-open-directions]')) button.addEventListener('click', () => {
    opener = button;
    if (!dialog.open) dialog.showModal();
    document.body.classList.add('route-dialog-open');
    updateLiveLink(); locate();
  });
  dialog.querySelector('[data-close-directions]').addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', close);
  form.addEventListener('submit', event => {
    event.preventDefault();
    const address = input.value.trim();
    if (address.length < 3 || address.length > 250 || /[\u0000-\u001f\u007f]/.test(address)) {
      tell('Enter a starting address or landmark, including its town or state.', true); input.focus(); return;
    }
    locationRequest += 1; origin = {address}; showRoute();
  });
  for (const button of dialog.querySelectorAll('[data-route-location]')) button.addEventListener('click', locate);
  mode.addEventListener('change', () => { updateLiveLink(); if (origin) showRoute(); });
  dialog.querySelector('[data-route-retry]').addEventListener('click', () => {
    if (origin) showRoute(); else { tell('Enter a starting address or choose Use my current location.'); input.focus(); }
  });
  window.addEventListener('offline', () => {
    if (!dialog.open) return;
    generation += 1; locationRequest += 1;
    if (controller) controller.abort(); clearTimeout(mapTimer);
    placeholder('You’re offline', 'Your destination is still available. Connect to load maps and routes.'); clearSummary('Maps and travel estimates require a connection.');
    tell('Connection lost. Your destination is still selected. Retry when you’re online.', true);
  });
  window.addEventListener('online', () => { if (dialog.open) tell('Connection restored. Choose Retry route map to continue.'); });
  document.documentElement.classList.add('route-navigation-ready');
})();
