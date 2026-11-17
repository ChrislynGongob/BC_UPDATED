(() => {
  const byId = (id) => document.getElementById(id);
  const search = byId('ip-search');
  const rows = [...document.querySelectorAll('#ip-rows tr[data-filter]')];
  const filters = byId('ip-filters');
  const empty = byId('ip-empty');
  const scrim = byId('ip-scrim');
  const drawer = byId('ip-drawer');
  const amount = byId('ip-amount-received');
  const change = byId('ip-change');
  const save = byId('ip-save');
  const dueInput = byId('ip-amount-due');
  const form = byId('ip-payment-form');
  let filter = 'all';
  let currentDueCents = 0;
  let previouslyFocused = null;

  const peso = (cents) => `₱${(cents / 100).toLocaleString('en-PH', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

  function renderRows() {
    const query = (search.value || '').trim().toLocaleLowerCase();
    let visible = 0;
    rows.forEach((row) => {
      const matchesFilter = filter === 'all' || row.dataset.filter === filter;
      const matchesSearch = !query || row.dataset.search.includes(query);
      row.hidden = !(matchesFilter && matchesSearch);
      if (!row.hidden) visible += 1;
    });
    empty.hidden = visible > 0;
  }

  function updateChange() {
    const raw = amount.value.trim();
    if (!raw || !Number.isFinite(Number(raw))) {
      change.hidden = true;
      save.disabled = true;
      return;
    }
    const receivedCents = Math.round(Number(raw) * 100);
    change.hidden = false;
    if (receivedCents < currentDueCents) {
      change.className = 'ip-change ip-short';
      change.textContent = `Short by ${peso(currentDueCents - receivedCents)}`;
      save.disabled = true;
    } else {
      change.className = 'ip-change';
      change.textContent = receivedCents === currentDueCents
        ? 'Exact amount received.'
        : `Change to give: ${peso(receivedCents - currentDueCents)}`;
      save.disabled = receivedCents <= 0;
    }
  }

  function openDrawer(button) {
    previouslyFocused = document.activeElement;
    const due = Number(button.dataset.due);
    currentDueCents = Math.round(due * 100);
    byId('ip-request-id').value = button.dataset.requestId;
    byId('ip-drawer-reference').textContent = button.dataset.reference;
    form.dataset.confirmReference = button.dataset.reference;
    byId('ip-drawer-resident').textContent = button.dataset.resident;
    byId('ip-drawer-document').textContent = button.dataset.document;
    byId('ip-drawer-purpose').textContent = button.dataset.purpose;
    byId('ip-drawer-due').textContent = peso(currentDueCents);
    dueInput.value = peso(currentDueCents);
    amount.value = '';
    byId('ip-receipt').value = '';
    byId('ip-payment-at').value = form.dataset.paymentNow || form.dataset.initialPaymentAt || byId('ip-payment-at').defaultValue;
    change.hidden = true;
    save.disabled = true;
    scrim.hidden = false;
    drawer.classList.add('ip-open');
    drawer.setAttribute('aria-hidden', 'false');
    document.body.classList.add('payment-drawer-open');
    byId('ip-amount-received').focus();
  }

  function closeDrawer() {
    drawer.classList.remove('ip-open');
    drawer.setAttribute('aria-hidden', 'true');
    scrim.hidden = true;
    document.body.classList.remove('payment-drawer-open');
    previouslyFocused?.focus();
  }

  search.addEventListener('input', renderRows);
  filters.addEventListener('click', (event) => {
    const button = event.target.closest('[data-filter]');
    if (!button) return;
    filter = button.dataset.filter;
    filters.querySelectorAll('[data-filter]').forEach((chip) => {
      chip.setAttribute('aria-pressed', String(chip === button));
    });
    renderRows();
  });
  byId('ip-rows').addEventListener('click', (event) => {
    const button = event.target.closest('[data-open-payment]');
    if (button) openDrawer(button);
  });
  amount.addEventListener('input', updateChange);
  byId('ip-exact').addEventListener('click', () => {
    amount.value = (currentDueCents / 100).toFixed(2);
    updateChange();
  });
  byId('ip-cancel').addEventListener('click', closeDrawer);
  byId('ip-close').addEventListener('click', closeDrawer);
  scrim.addEventListener('click', closeDrawer);
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && drawer.classList.contains('ip-open')) closeDrawer();
  });
  form.addEventListener('submit', (event) => {
    updateChange();
    if (save.disabled) event.preventDefault();
  });
  renderRows();
})();
