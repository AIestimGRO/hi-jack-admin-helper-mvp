(() => {
  const accountForm = document.querySelector('.account-registry-form');
  if (accountForm) {
    const telegramId = accountForm.querySelector('[name="telegram_id"]');
    const telegramUserId = accountForm.querySelector('[name="telegram_user_id"]');
    const hasTelegram = Boolean(
      telegramId?.value.trim() || telegramUserId?.value.trim()
    );
    const action = accountForm.getAttribute('action') || '';
    const match = action.match(/^\/master\/member-accounts\/(\d+)\/update$/);
    const csrf = accountForm.querySelector('[name="csrf_token"]')?.value || '';

    if (hasTelegram && match && csrf && !document.querySelector('.account-registry-telegram')) {
      const details = document.createElement('details');
      details.className = 'account-registry-telegram';
      details.open = true;

      const summary = document.createElement('summary');
      summary.textContent = 'Telegram-привязка';

      const card = document.createElement('div');
      card.className = 'account-registry-telegram-card';

      const copy = document.createElement('div');
      copy.className = 'account-registry-telegram-copy';
      const title = document.createElement('strong');
      title.textContent = 'Telegram привязан';
      const description = document.createElement('p');
      description.className = 'muted';
      description.textContent = 'После отвязки этот Telegram можно будет связать с другим личным кабинетом.';
      copy.append(title, description);

      const unlinkForm = document.createElement('form');
      unlinkForm.method = 'post';
      unlinkForm.action = `/master/member-accounts/${match[1]}/unlink-telegram`;

      const csrfInput = document.createElement('input');
      csrfInput.type = 'hidden';
      csrfInput.name = 'csrf_token';
      csrfInput.value = csrf;

      const button = document.createElement('button');
      button.type = 'submit';
      button.textContent = 'Отвязать Telegram';
      button.addEventListener('click', (event) => {
        if (!window.confirm('Отвязать Telegram от этой учётной записи?')) {
          event.preventDefault();
        }
      });

      unlinkForm.append(csrfInput, button);
      card.append(copy, unlinkForm);
      details.append(summary, card);
      accountForm.insertAdjacentElement('afterend', details);
    }
  }

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
