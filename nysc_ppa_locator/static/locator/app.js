'use strict';
// No analytics, external scripts or background collection of members' locations.
document.documentElement.classList.add('js');
const menuToggle = document.querySelector('[data-menu-toggle]');
const menuLinks = document.getElementById('primary-links');
if (menuToggle && menuLinks) {
  menuToggle.hidden = false;
  const closeMenu = () => { menuLinks.classList.remove('is-open'); menuToggle.setAttribute('aria-expanded', 'false'); };
  menuToggle.addEventListener('click', () => { const open = menuToggle.getAttribute('aria-expanded') !== 'true'; menuToggle.setAttribute('aria-expanded', String(open)); menuLinks.classList.toggle('is-open', open); });
  menuLinks.addEventListener('keydown', event => { if (event.key === 'Escape') { closeMenu(); menuToggle.focus(); }});
  menuLinks.addEventListener('click', event => { if (event.target.closest('a')) closeMenu(); });
}
try { const raw = localStorage.getItem('ppa-offline-v1'); if (raw) { const records = JSON.parse(raw); localStorage.setItem('ppa-offline-v1', JSON.stringify(Array.isArray(records) ? records.filter(item => item && Date.now() - Number(item.savedAt) < 30 * 86400000).slice(0, 20) : [])); }} catch (_) {}
const feedback = (id, text) => { const node = document.getElementById(id); if (node) node.textContent = text; };
const state = document.getElementById('state-select');
const lga = document.getElementById('lga-select');
if (state && lga) {
  let pending;
  state.addEventListener('change', async () => {
    if (pending) pending.abort();
    pending = new AbortController();
    lga.replaceChildren(new Option('Choose LGA', ''));
    if (!state.value) return;
    lga.disabled = true;
    try {
      const response = await fetch(`/api/load-lgas/?state=${encodeURIComponent(state.value)}`, {signal: pending.signal});
      if (!response.ok) throw new Error('Unavailable');
      for (const item of await response.json()) lga.add(new Option(item.name, item.id));
      feedback('location-feedback', 'Select your LGA to narrow the search.');
    } catch (error) {
      if (error.name !== 'AbortError') feedback('location-feedback', 'Could not load LGAs. Check your connection and select the state again.');
    } finally { lga.disabled = false; }
  });
}
const submittingForms = new WeakSet();
for (const form of document.querySelectorAll('form')) {
  form.addEventListener('submit', event => {
    if (submittingForms.has(form)) { event.preventDefault(); return; }
    queueMicrotask(() => {
      if (event.defaultPrevented) return;
      submittingForms.add(form);
      const button = event.submitter || form.querySelector('button[type=submit],button:not([type])');
      // Keep native controls enabled until serialization so a named submitter's
      // action is included. The form guard prevents duplicate submissions.
      if (button) { button.setAttribute('aria-busy', 'true'); button.setAttribute('aria-disabled', 'true'); button.setAttribute('data-submit-pending', ''); }
    });
  });
}
window.addEventListener('pageshow', () => {
  for (const form of document.querySelectorAll('form')) submittingForms.delete(form);
  for (const button of document.querySelectorAll('[data-submit-pending]')) { button.removeAttribute('aria-busy'); button.removeAttribute('aria-disabled'); button.removeAttribute('data-submit-pending'); }
});
for (const button of document.querySelectorAll('[data-copy]')) button.addEventListener('click', async () => {
  try { await navigator.clipboard.writeText(button.dataset.copy); feedback('action-feedback', 'Address copied.'); }
  catch (_) { feedback('action-feedback', 'Copy is unavailable. Select and copy the address above.'); }
});
for (const button of document.querySelectorAll('[data-share]')) button.addEventListener('click', async () => {
  try { if (navigator.share) await navigator.share({title: document.title, url: location.href}); else { await navigator.clipboard.writeText(location.href); feedback('action-feedback', 'Place link copied.'); }}
  catch (error) { if (error.name !== 'AbortError') feedback('action-feedback', 'Share is unavailable. Copy the page link from your browser.'); }
});
for (const button of document.querySelectorAll('[data-offline-save]')) button.addEventListener('click', () => {
  try {
    const stored = JSON.parse(localStorage.getItem('ppa-offline-v1') || '[]');
    const items = Array.isArray(stored) ? stored : [];
    const data = button.dataset;
    const record = {id: data.id, name: data.name, address: data.address, area: data.area, landmark: data.landmark, entrance: data.entrance, directions: data.directions, savedAt: Date.now()};
    localStorage.setItem('ppa-offline-v1', JSON.stringify([record, ...items.filter(item => item.id !== record.id)].slice(0, 20)));
    feedback('action-feedback', 'Address saved on this device for 30 days. Maps routing still needs a connection. You can remove it from Offline places.');
  } catch (_) { feedback('action-feedback', 'This browser could not save the address. Try copying it instead.'); }
});
if ('serviceWorker' in navigator) navigator.serviceWorker.register('/service-worker.js').catch(() => {});
