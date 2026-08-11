(() => {
  const root = document.querySelector('[data-security-journal]');
  if (!root) return;

  const clientId = root.dataset.clientId;
  if (!clientId) return;

  const list = root.querySelector('[data-security-journal-list]');
  const empty = root.querySelector('[data-security-journal-empty]');

  const formatDate = (value) => {
    if (!value) return '—';
    const normalized = value.includes('T') ? value : value.replace(' ', 'T') + 'Z';
    const date = new Date(normalized);
    if (Number.isNaN(date.getTime())) return value;
    return new Intl.DateTimeFormat('ru-RU', {
      day: '2-digit',
      month: '2-digit',
      year: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    }).format(date);
  };

  fetch(`/api/master/member-security-events?client_id=${encodeURIComponent(clientId)}`, {
    credentials: 'same-origin',
    headers: { Accept: 'application/json' },
  })
    .then((response) => {
      if (!response.ok) throw new Error('security_journal_unavailable');
      return response.json();
    })
    .then((payload) => {
      const events = Array.isArray(payload.events) ? payload.events : [];
      if (!events.length) {
        if (empty) empty.hidden = false;
        return;
      }
      if (!list) return;
      list.replaceChildren();
      for (const event of events) {
        const row = document.createElement('div');
        row.className = 'account-registry-security-row';

        const copy = document.createElement('span');
        const title = document.createElement('strong');
        title.textContent = event.label || event.action || 'Событие';
        const date = document.createElement('small');
        date.textContent = formatDate(event.created_at);
        copy.append(title, date);

        const badge = document.createElement('i');
        badge.textContent = event.action || 'event';

        row.append(copy, badge);
        list.append(row);
      }
    })
    .catch(() => {
      if (empty) {
        empty.hidden = false;
        empty.textContent = 'Журнал временно недоступен.';
      }
    });
})();
