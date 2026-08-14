(() => {
  const root = document.querySelector('[data-tournament-detail]');
  if (!root) return;

  const registerForm = root.querySelector('[data-tournament-register]');
  const cancelForm = root.querySelector('[data-tournament-cancel]');
  const statusEl = root.querySelector('[data-my-status]');
  const registeredEl = root.querySelector('[data-registered-count]');
  const waitlistEl = root.querySelector('[data-waitlist-count]');
  const seatsEl = root.querySelector('[data-seats-left]');
  const noteEl = root.querySelector('[data-registration-note]');
  const flashEls = root.querySelectorAll('[data-tournament-flash]');

  const statusLabel = (value) => ({
    registered: 'Ты зарегистрирован',
    waitlist: 'Ты в листе ожидания',
    checked_in: 'Check-in пройден',
    played: 'Участие завершено',
    cancelled: 'Регистрация отменена'
  }[value] || 'Не зарегистрирован');

  const showFlash = (message, isError = false) => {
    const target = flashEls[flashEls.length - 1];
    if (!target) return;
    target.hidden = false;
    target.classList.toggle('success', !isError);
    target.classList.toggle('error', isError);
    target.textContent = message;
  };

  const render = (item) => {
    if (!item) return;
    statusEl.textContent = statusLabel(item.my_registration_status);
    registeredEl.textContent = item.registered_count;
    waitlistEl.textContent = item.waitlist_count;
    seatsEl.textContent = item.seats_left === null ? '∞' : item.seats_left;
    registerForm.hidden = !item.can_register;
    cancelForm.hidden = !item.can_cancel;
    const registerButton = registerForm.querySelector('button');
    if (registerButton) {
      registerButton.textContent = item.registration_state === 'waitlist'
        ? 'Встать в лист ожидания'
        : 'Участвовать';
    }
    let note = '';
    if (item.registration_state === 'not_open') note = 'Регистрация ещё не открыта.';
    if (item.registration_state === 'closed') note = 'Регистрация закрыта.';
    if (item.registration_state === 'waitlist' && !item.my_registration_status) {
      note = 'Основные места заняты. Можно встать в лист ожидания.';
    }
    noteEl.textContent = note;
  };

  const submit = async (form) => {
    const button = form.querySelector('button');
    if (button) button.disabled = true;
    try {
      const response = await fetch(form.action, {
        method: 'POST',
        body: new FormData(form),
        headers: { Accept: 'application/json', 'X-Requested-With': 'fetch' },
        credentials: 'same-origin'
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok || !payload.ok) throw new Error(payload.error || 'Не удалось сохранить регистрацию');
      render(payload.tournament);
      showFlash(payload.message || 'Готово');
    } catch (error) {
      showFlash(error.message || 'Не удалось выполнить действие', true);
    } finally {
      if (button) button.disabled = false;
    }
  };

  [registerForm, cancelForm].forEach((form) => {
    if (!form) return;
    form.addEventListener('submit', (event) => {
      event.preventDefault();
      submit(form);
    });
  });
})();
