(() => {
  const form = document.querySelector('[data-registration-form]');
  if (!form) return;

  const birthday = form.querySelector('[data-registration-birth-date]');
  const phone = form.querySelector('[data-registration-phone]');
  const message = form.querySelector('[data-registration-extra-message]');
  const submit = form.querySelector('[data-registration-submit]');

  function showMessage(text) {
    if (!message) return;
    message.textContent = text || '';
    message.hidden = !text;
  }

  form.addEventListener('submit', async (event) => {
    if (form.dataset.extraValidated === '1') return;
    event.preventDefault();
    showMessage('');
    if (!birthday?.value) {
      showMessage('Укажите дату рождения');
      birthday?.focus();
      return;
    }
    if (!phone?.value.trim()) {
      showMessage('Укажите номер телефона');
      phone?.focus();
      return;
    }

    const csrf = form.querySelector('input[name="csrf_token"]')?.value || '';
    const payload = new FormData();
    payload.set('csrf_token', csrf);
    payload.set('birth_date', birthday.value);
    payload.set('phone', phone.value);

    if (submit) submit.disabled = true;
    try {
      const response = await fetch('/api/account/register/draft-extra', {
        method: 'POST',
        body: payload,
        credentials: 'same-origin',
        headers: { Accept: 'application/json' },
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok || !data.ok) {
        showMessage(data.error || 'Проверьте номер телефона и дату рождения');
        return;
      }
      form.dataset.extraValidated = '1';
      form.requestSubmit();
    } catch (_) {
      showMessage('Не удалось проверить данные. Попробуйте ещё раз');
    } finally {
      if (submit && form.dataset.extraValidated !== '1') submit.disabled = false;
    }
  });
})();
