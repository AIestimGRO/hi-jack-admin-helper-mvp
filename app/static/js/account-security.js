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
        <p>Для смены почты подтвердите текущий пароль. Код подтверждения придёт на новый адрес, после чего он станет логином для входа.</p>
        <form class="account-security-form" action="/account/security/email/request" method="post">
          <input type="hidden" name="csrf_token" value="${csrf}">
          <input type="password" name="current_password" autocomplete="current-password" maxlength="128" placeholder="Текущий пароль" required>
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
        <p>Код придёт на текущую привязанную почту. Новый номер не может быть привязан к другому аккаунту. После смены старый номер освобождается.</p>
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

    <details class="account-security-item" data-security-birthday>
      <summary><span><small>Дата рождения</small><strong data-security-birthday-value>—</strong></span><b data-security-birthday-action>Указать</b></summary>
      <div class="account-security-body">
        <p data-security-birthday-copy>Дата рождения указывается один раз. Если нужно исправление, обратитесь к администратору клуба.</p>
        <a class="account-security-link" href="/account/birthday" data-security-birthday-link>Указать дату рождения</a>
      </div>
    </details>

    <details class="account-security-item">
      <summary><span><small>Пароль</small><strong>Сменить пароль</strong></span><b>Сменить</b></summary>
      <div class="account-security-body">
        <p>После смены пароля все остальные активные сессии аккаунта будут закрыты.</p>
        <form class="account-security-form" action="/account/security/password/change" method="post">
          <input type="hidden" name="csrf_token" value="${csrf}">
          <input type="password" name="current_password" autocomplete="current-password" minlength="6" maxlength="128" placeholder="Текущий пароль" required>
          <input type="password" name="new_password" autocomplete="new-password" minlength="8" maxlength="128" placeholder="Новый пароль" required>
          <input type="password" name="new_password_confirmation" autocomplete="new-password" minlength="8" maxlength="128" placeholder="Повторите новый пароль" required>
          <button type="submit">Сменить пароль</button>
        </form>
      </div>
    </details>

    <details class="account-security-item">
      <summary><span><small>Правила и приватность</small><strong>Документы и согласия</strong></span><b>Открыть</b></summary>
      <div class="account-security-body">
        <p>Пользовательское соглашение, Политика персональных данных и отдельные добровольные согласия на рассылку, изображение и публичный рейтинг.</p>
        <a class="account-security-link" href="/account/legal">Управлять документами и согласиями</a>
      </div>
    </details>

    <details class="account-security-item is-danger">
      <summary><span><small>Аккаунт</small><strong>Удаление аккаунта</strong></span><b>Удалить</b></summary>
      <div class="account-security-body">
        <p>Личные данные, привязки, аватар и активные сессии будут удалены. Телефон, почта и Telegram освободятся для новой регистрации. Игровая история и рейтинговые результаты останутся только в обезличенном виде, чтобы не ломать общую статистику клуба.</p>
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

  Promise.all([
    fetch('/api/account/security-state', {
      credentials: 'same-origin',
      headers: { Accept: 'application/json' },
    }).then((response) => (response.ok ? response.json() : null)),
    fetch('/api/account/identity-state', {
      credentials: 'same-origin',
      headers: { Accept: 'application/json' },
    }).then((response) => (response.ok ? response.json() : null)),
  ])
    .then(([security, identity]) => {
      const email = panel.querySelector('[data-security-email-value]');
      const phone = panel.querySelector('[data-security-phone-value]');
      const birthday = panel.querySelector('[data-security-birthday-value]');
      const birthdayAction = panel.querySelector('[data-security-birthday-action]');
      const birthdayLink = panel.querySelector('[data-security-birthday-link]');
      const birthdayCopy = panel.querySelector('[data-security-birthday-copy]');
      if (email && security) email.textContent = security.email || '—';
      if (phone && security) phone.textContent = security.phone || '—';
      const birthDate = identity?.birth_date || '';
      if (birthday) birthday.textContent = birthDate ? birthDate.split('-').reverse().join('.') : 'Не указана';
      if (birthDate) {
        if (birthdayAction) birthdayAction.textContent = 'Сохранено';
        if (birthdayLink) birthdayLink.remove();
        if (birthdayCopy) birthdayCopy.textContent = 'Для исправления даты рождения обратитесь к мастер-администратору клуба.';
      }
    })
    .catch(() => {});
})();
