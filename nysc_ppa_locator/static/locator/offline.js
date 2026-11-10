'use strict';
const root = document.getElementById('offline-places');
function render() {
  root.replaceChildren();
  let places = [];
  try {
    const data = JSON.parse(localStorage.getItem('ppa-offline-v1') || '[]');
    if (Array.isArray(data)) places = data.filter(item => item && Date.now() - Number(item.savedAt) < 30 * 86400000).slice(0, 20);
    localStorage.setItem('ppa-offline-v1', JSON.stringify(places));
  } catch (_) { document.getElementById('offline-feedback').textContent = 'Device storage is unavailable.'; }
  if (!places.length) { const p = document.createElement('p'); p.textContent = 'No addresses saved. Open a community-contributed PPA and choose Save address offline.'; root.append(p); }
  for (const place of places) {
    const card = document.createElement('article'); card.className = 'card section';
    const title = document.createElement('h2'); title.textContent = place.name; card.append(title);
    for (const text of [place.address, place.area, place.landmark && `Landmark: ${place.landmark}`, place.entrance && `Entrance: ${place.entrance}`, `Saved ${new Date(place.savedAt).toLocaleDateString()}`]) {
      if (text) { const p = document.createElement('p'); p.textContent = text; card.append(p); }
    }
    try {
      const url = new URL(place.directions);
      if (url.protocol === 'https:' && url.hostname === 'www.google.com' && url.pathname === '/maps/dir/') { const link = document.createElement('a'); link.textContent = 'Open directions when connected'; link.href = url.href; link.rel = 'noopener noreferrer'; card.append(link); }
    } catch (_) {}
    const remove = document.createElement('button'); remove.type = 'button'; remove.className = 'secondary'; remove.textContent = 'Remove this address';
    remove.addEventListener('click', () => { localStorage.setItem('ppa-offline-v1', JSON.stringify(places.filter(item => item.id !== place.id))); render(); }); card.append(document.createElement('p'), remove); root.append(card);
  }
}
document.getElementById('clear-offline').addEventListener('click', () => { try { localStorage.removeItem('ppa-offline-v1'); render(); } catch (_) {} });
render();
