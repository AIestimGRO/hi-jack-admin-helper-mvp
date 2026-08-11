(() => {
  const page = document.querySelector('.member-app-page');
  if (!page || page.dataset.accountTab !== 'profile') return;
  if (page.querySelector('.account-security-panel')) return;

  const csrf = document.querySelector('input[name="csrf_token"]')?.value || '';
  const profilePanel = page.querySelector('.profile-panel');
  if (!profilePanel || !csrf) return;

  const panel = document.createElement('section');
  panel.className = 'account-security-panel';
  panel.innerHTML = `
    <header>
      <h3>Настройки аккаунта</h3>
      <small>Безопасность и личные данные</small>
    </header>

    <details class="account-security-item" data-security-email>
      <summary><span><small>Почта</small><strong data-security-email-value>—</strong></span><b>Сменить</b></summary>
      <div class="account-security-body">
        <p>Код подтверждения придёт на новый адрес. После подтверждения новая почта станет логином для входа.</p>
        <form class="account-security-form" action="/account/security/email/request" method="post">
          <input type="hidden" name="csrf_token" value="${csrf}">
          <input type="email" name="new_email" autocomplete="email" maxlength="254" placeholder="Новая почта" required>
          <button type="submit">Отправить код</button>
        </form>
        <form class="account-security-form" action="/account/security/email/confirm" method="post">
          <input type="hidden" name="csrf_token" value="${csrf}">
          <input type="text" name="code" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{6}" maxlength="6" placeholder="6-значный код" required>
          <button type="submit">Подтвердить</button>
        </form>
      </div>
    </details>

    <details class="account-security-item" data-security-phone>
      <summary><span><small>Телефон</small><strong data-security-phone-value>—</strong></span><b>Сменить</b></summary>
      <div class="account-security-body">
        <p>Код придёт на текущую привязанную почту. Старый номер сохранится только как технический alias для истории рейтинга HI, JACK!.</p>
        <form class="account-security-form" action="/account/security/phone/request" method="post">
          <input type="hidden" name="csrf_token" value="${csrf}">
          <input type="tel" name="new_phone" autocomplete="tel" maxlength="32" placeholder="Новый номер телефона" required>
          <button type="submit">Отправить код</button>
        </form>
        <form class="account-security-form" action="/account/security/phone/confirm" method="post">
          <input type="hidden" name="csrf_token" value="${csrf}">
          <input type="text" name="code" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{6}" maxlength="6" placeholder="6-значный код" required>
          <button type="submit">Подтвердить</button>
        </form>
      </div>
    </details>

    <details class="account-security-item is-danger">
      <summary><span><small>Аккаунт</small><strong>Удаление аккаунта</strong></span><b>Удалить</b></summary>
      <div class="account-security-body">
        <p>Личные данные, привязки, аватар и активные сессии будут удалены. Игровая история и рейтинговые результаты останутся только в обезличенном виде, чтобы не ломать общую статистику клуба.</p>
        <form class="account-security-form" action="/account/security/delete/request" method="post">
          <input type="hidden" name="csrf_token" value="${csrf}">
          <button class="account-security-danger-button" type="submit">Получить код удаления</button>
        </form>
        <form class="account-security-form account-security-danger-form" action="/account/security/delete/confirm" method="post">
          <input type="hidden" name="csrf_token" value="${csrf}">
          <input type="text" name="code" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{6}" maxlength="6" placeholder="Код" required>
          <input type="text" name="confirmation" autocomplete="off" placeholder="Введите УДАЛИТЬ" required>
          <button class="account-security-danger-button" type="submit">Удалить аккаунт</button>
        </form>
      </div>
    </details>
  `;
  profilePanel.insertAdjacentElement('afterend', panel);

  fetch('/api/account/security-state', {
    credentials: 'same-origin',
    headers: { Accept: 'application/json' },
  })
    .then((response) => (response.ok ? response.json() : null))
    .then((state) => {
      if (!state) return;
      const email = panel.querySelector('[data-security-email-value]');
      const phone = panel.querySelector('[data-security-phone-value]');
      if (email) email.textContent = state.email || '—';
      if (phone) phone.textContent = state.phone || '—';
    })
    .catch(() => {});
})();
