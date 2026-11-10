'use strict';
for (const form of document.querySelectorAll('[data-account-form]')) {
    const state = form.querySelector('#id_state_posted');
    const lga = form.querySelector('#id_lga_posted');
    const status = form.querySelector('[data-lga-status]');
    let loading = false;
    let requestNumber = 0;
    // The server renders the selected LGA and its options, including on validation
    // errors. Only fetch when the member changes state, so that value is preserved.
    state?.addEventListener('change', async () => {
        const currentRequest = ++requestNumber;
        lga.replaceChildren(new Option(state.value ? 'Choose LGA' : 'Choose state first', ''));
        if (!state.value) {
            lga.disabled = false;
            status.textContent = '';
            loading = false;
            return;
        }
        lga.disabled = true;
        loading = true;
        status.textContent = 'Loading local governments…';
        try {
            const response = await fetch(`/api/load-lgas/?state=${encodeURIComponent(state.value)}`);
            if (!response.ok) throw new Error('Request failed');
            const rows = await response.json();
            if (currentRequest !== requestNumber) return;
            for (const row of rows) lga.append(new Option(row.name, row.id));
            status.textContent = rows.length ? '' : 'No local governments found. Try another state.';
        } catch {
            if (currentRequest === requestNumber) {
                status.textContent = 'Could not load local governments. Check your connection, then choose the state again to retry.';
            }
        } finally {
            if (currentRequest === requestNumber) {
                lga.disabled = false;
                loading = false;
            }
        }
    });
    form.addEventListener('submit', event => {
        if (loading) {
            event.preventDefault();
            status.textContent = 'Please wait for local governments to finish loading.';
            return;
        }
        const submit = form.querySelector('button[type="submit"]');
        if (submit) {
            submit.disabled = true;
            submit.textContent = form.dataset.busyLabel || 'Saving…';
        }
    });
}
