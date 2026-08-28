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

  async function postForm(url, payload) {
    const response = await fetch(url, {
      method: 'POST',
      body: payload,
      credentials: 'same-origin',
      headers: { Accept: 'application/json' },
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || !data.ok) {
      throw new Error(data.error || 'Не удалось проверить данные');
    }
    return data;
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
    const identityPayload = new FormData();
    identityPayload.set('csrf_token', csrf);
    identityPayload.set('birth_date', birthday.value);
    identityPayload.set('phone', phone.value);

    if (submit) submit.disabled = true;
    try {
      await postForm('/api/account/register/draft-extra', identityPayload);
      form.dataset.extraValidated = '1';
      form.requestSubmit();
    } catch (error) {
      showMessage(error?.message || 'Не удалось проверить данные. Попробуйте ещё раз');
    } finally {
      if (submit && form.dataset.extraValidated !== '1') submit.disabled = false;
    }
  });
})();
