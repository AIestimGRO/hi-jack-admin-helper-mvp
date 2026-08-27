(() => {
  const page = document.querySelector('.member-app-page');
  const launcher = document.querySelector('[data-chat-launcher]');

  function csrfToken() {
    return document.querySelector('input[name="csrf_token"]')?.value || '';
  }

  function formatTournamentDate(value) {
    if (!value) return '';
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return value;
    const date = new Intl.DateTimeFormat('ru-RU', {
      day: '2-digit',
      month: '2-digit',
    }).format(parsed);
    const weekday = new Intl.DateTimeFormat('ru-RU', {
      weekday: 'short',
    }).format(parsed).replace('.', '').toLowerCase();
    const time = new Intl.DateTimeFormat('ru-RU', {
      hour: '2-digit',
      minute: '2-digit',
    }).format(parsed);
    return `${date}, ${weekday} / ${time}`;
  }

  function tournamentCard(item, nearest = false) {
    const article = document.createElement('article');
    article.className = 'tournament-shell-card tournament-neon-card';
    if (item.external_url) article.classList.add('has-link');

    const slots = Number(item.max_slots || 0);
    const details = [
      item.format_text || '',
      item.buy_in_text || '',
      slots > 0 ? `${slots} мест` : '',
    ].filter(Boolean);

    article.innerHTML = `
      <div class="tournament-neon-grid" aria-hidden="true"></div>
      <div class="tournament-neon-copy">
        <span class="tournament-kicker">
          <i aria-hidden="true"></i>
          ${nearest ? 'JACKSIDE // БЛИЖАЙШИЙ' : 'JACKSIDE // TOURNAMENT'}
        </span>
        <h3></h3>
        <div class="tournament-date-chip">
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <circle cx="12" cy="12" r="8.6"></circle>
            <path d="M12 7.3v5.1l3.2 1.8"></path>
          </svg>
          <span class="tournament-date"></span>
        </div>
        ${item.description ? '<p class="tournament-description"></p>' : ''}
        <div class="tournament-shell-meta"></div>
        <div class="tournament-shell-action-slot"></div>
      </div>
      <div class="tournament-neon-visual" aria-hidden="true">
        <div class="tournament-neon-halo"></div>
        <div class="tournament-icon-frame">
          <img
            class="tournament-icon-fallback"
            src="/static/img/brand/jackside-logo.webp"
            alt=""
          >
        </div>
      </div>
    `;

    article.querySelector('h3').textContent = item.title || 'Турнир Hi, Jack!';
    article.querySelector('.tournament-date').textContent = formatTournamentDate(item.starts_at);

    const description = article.querySelector('.tournament-description');
    if (description) description.textContent = item.description;

    const meta = article.querySelector('.tournament-shell-meta');
    details.forEach((value) => {
      const span = document.createElement('span');
      span.textContent = value;
      meta.append(span);
    });

    const iconFrame = article.querySelector('.tournament-icon-frame');
    if (item.external_icon_url) {
      const icon = document.createElement('img');
      icon.className = 'tournament-icon';
      icon.src = item.external_icon_url;
      icon.alt = '';
      icon.loading = nearest ? 'eager' : 'lazy';
      icon.decoding = 'async';
      icon.addEventListener('load', () => {
        iconFrame.classList.add('has-icon');
      });
      icon.addEventListener('error', () => {
        icon.remove();
        iconFrame.classList.remove('has-icon');
      });
      iconFrame.append(icon);
    }

    const actionSlot = article.querySelector('.tournament-shell-action-slot');
    if (item.external_url) {
      const action = document.createElement('a');
      action.className = 'tournament-shell-action is-active';
      action.href = item.external_url;
      action.target = '_blank';
      action.rel = 'noopener noreferrer';
      action.innerHTML = '<span>В турнир</span><b aria-hidden="true">↗</b>';
      actionSlot.append(action);
    } else {
      const status = document.createElement('span');
      status.className = 'tournament-shell-action is-disabled';
      status.textContent = 'Регистрация скоро';
      actionSlot.append(status);
    }
    return article;
  }

  function mountHomeTournament(state) {
    if (!page || page.dataset.accountTab !== 'home' || !state?.nearest_tournament) return;
    const gameSection = page.querySelector('.home-section');
    if (!gameSection || page.querySelector('.home-tournament-banner')) return;
    const wrapper = document.createElement('section');
    wrapper.className = 'home-section home-tournament-banner';
    const heading = document.createElement('div');
    heading.className = 'app-section-head';
    heading.innerHTML = '<div><p class="member-eyebrow">Расписание</p><h2>Турниры Hi, Jack!</h2></div><a href="/account?tab=quizzes&schedule=tournaments">Все турниры</a>';
    wrapper.append(heading, tournamentCard(state.nearest_tournament, true));
    gameSection.insertAdjacentElement('afterend', wrapper);
  }

  function cleanHomeCopy() {
    if (!page || page.dataset.accountTab !== 'home') return;
    page.querySelector('.home-heading')?.remove();
    const walletLink = page.querySelector('.jc-wallet-bottom a');
    if (walletLink) walletLink.innerHTML = 'Hi, Store <span aria-hidden="true">→</span>';

    page.querySelectorAll('.home-section .app-section-head').forEach((head) => {
      const eyebrow = head.querySelector('.member-eyebrow');
      const heading = head.querySelector('h2');
      if (eyebrow?.textContent.trim() === 'THE VAULT') {
        eyebrow.remove();
        if (heading) heading.textContent = 'Hi, Store';
      }
      if (eyebrow?.textContent.trim() === 'Прогресс' && heading?.textContent.trim() === 'Статистика') {
        heading.remove();
      }
    });
  }

  function mountStoreTabs() {
    if (!page || page.dataset.accountTab !== 'vault' || page.querySelector('.store-tabs')) return;
    const heading = page.querySelector('.vault-page-heading');
    if (heading) {
      const title = heading.querySelector('h1');
      if (title) title.textContent = 'Hi, Store';
      heading.querySelector('.member-eyebrow')?.remove();
      heading.querySelector('.member-muted')?.remove();
    }

    page.querySelectorAll('.jack-card-top small').forEach((node) => {
      if (node.textContent.trim() === 'THE VAULT') node.textContent = 'HI, STORE';
    });

    const sections = Array.from(page.querySelectorAll(':scope > .vault-section'));
    const active = sections.find((section) => section.classList.contains('vault-active-section'))
      || sections.find((section) => section.querySelector('h2')?.textContent.includes('Активные награды'));
    const market = sections.find((section) => section.querySelector('.vault-catalog-grid'));
    if (!market) return;

    const tabs = document.createElement('nav');
    tabs.className = 'store-tabs';
    tabs.setAttribute('aria-label', 'Hi, Store');
    tabs.innerHTML = '<button type="button" data-store-tab="market">Market</button><button type="button" data-store-tab="cards">My Cards</button>';
    heading?.insertAdjacentElement('afterend', tabs);

    market.classList.add('store-panel');
    market.dataset.storePanel = 'market';
    if (active) {
      active.classList.add('store-panel');
      active.dataset.storePanel = 'cards';
    }
    const emptyCards = sections.find((section) => !section.querySelector('.vault-catalog-grid') && section !== active && section.querySelector('h2')?.textContent.includes('Активные награды'));
    if (emptyCards) {
      emptyCards.classList.add('store-panel');
      emptyCards.dataset.storePanel = 'cards';
    }

    const activate = (name, push = true) => {
      const target = name === 'cards' ? 'cards' : 'market';
      tabs.querySelectorAll('button').forEach((button) => button.classList.toggle('active', button.dataset.storeTab === target));
      page.querySelectorAll('[data-store-panel]').forEach((panel) => { panel.hidden = panel.dataset.storePanel !== target; });
      if (push) {
        const url = new URL(window.location.href);
        url.searchParams.set('store', target);
        history.replaceState({}, '', url);
      }
    };
    tabs.addEventListener('click', (event) => {
      const button = event.target.closest('[data-store-tab]');
      if (button) activate(button.dataset.storeTab);
    });
    const requested = new URL(window.location.href).searchParams.get('store');
    activate(requested === 'cards' ? 'cards' : 'market', false);
  }

  function mountScheduleTabs(state) {
    if (!page || page.dataset.accountTab !== 'quizzes' || page.querySelector('.schedule-tabs')) return;
    const quizList = page.querySelector('.campaign-list');
    if (!quizList) return;

    const tabs = document.createElement('nav');
    tabs.className = 'schedule-tabs';
    tabs.setAttribute('aria-label', 'Расписание');
    tabs.innerHTML = '<button type="button" data-schedule-tab="quizzes">Квизы</button><button type="button" data-schedule-tab="tournaments">Турниры Hi, Jack!</button>';
    quizList.insertAdjacentElement('beforebegin', tabs);
    quizList.classList.add('schedule-panel');
    quizList.dataset.schedulePanel = 'quizzes';

    const tournamentPanel = document.createElement('section');
    tournamentPanel.className = 'schedule-panel tournament-shell-list';
    tournamentPanel.dataset.schedulePanel = 'tournaments';
    const tournaments = Array.isArray(state?.tournaments) ? state.tournaments : [];
    if (tournaments.length) tournaments.forEach((item) => tournamentPanel.append(tournamentCard(item)));
    else {
      const empty = document.createElement('div');
      empty.className = 'tournament-shell-empty';
      empty.textContent = 'Расписание турниров готовится.';
      tournamentPanel.append(empty);
    }
    quizList.insertAdjacentElement('afterend', tournamentPanel);

    const activate = (name, push = true) => {
      const target = name === 'tournaments' ? 'tournaments' : 'quizzes';
      tabs.querySelectorAll('button').forEach((button) => button.classList.toggle('active', button.dataset.scheduleTab === target));
      page.querySelectorAll('[data-schedule-panel]').forEach((panel) => { panel.hidden = panel.dataset.schedulePanel !== target; });
      if (push) {
        const url = new URL(window.location.href);
        url.searchParams.set('schedule', target);
        history.replaceState({}, '', url);
      }
    };
    tabs.addEventListener('click', (event) => {
      const button = event.target.closest('[data-schedule-tab]');
      if (button) activate(button.dataset.scheduleTab);
    });
    const requested = new URL(window.location.href).searchParams.get('schedule');
    activate(requested === 'tournaments' ? 'tournaments' : 'quizzes', false);
  }

  function applyAvatar(url, kind) {
    const targets = [
      document.querySelector('.profile-avatar-large'),
      document.querySelector('.member-profile-button'),
    ].filter(Boolean);
    targets.forEach((target) => {
      let image = target.querySelector('img[data-product-avatar]');
      if (!url) {
        image?.remove();
        target.classList.remove('has-product-avatar');
        return;
      }
      if (!image) {
        image = document.createElement('img');
        image.dataset.productAvatar = '1';
        image.alt = '';
        target.prepend(image);
      }
      image.classList.toggle('is-sticker', kind === 'sticker');
      image.src = `${url}?v=${Date.now()}`;
      image.addEventListener('error', () => {
        image.remove();
        target.classList.remove('has-product-avatar');
      }, { once: true });
      target.classList.add('has-product-avatar');
    });
  }

  function mountProfileEditor(state) {
    if (!page || page.dataset.accountTab !== 'profile' || page.querySelector('.product-profile-editor')) return;
    const accountPanel = page.querySelector('.profile-panel');
    const heading = page.querySelector('.profile-heading');
    if (!accountPanel || !heading) return;
    const nickname = state?.profile?.nickname || '';
    if (nickname) {
      const h1 = heading.querySelector('h1');
      if (h1) h1.textContent = nickname;
    }
    applyAvatar(state?.profile?.avatar_url || null, state?.profile?.avatar_kind || 'photo');

    const editor = document.createElement('section');
    editor.className = 'product-profile-editor';
    editor.innerHTML = `
      <h3>Профиль</h3>
      <div class="product-profile-grid">
        <form class="product-profile-form" action="/account/profile/update" method="post">
          <input type="hidden" name="csrf_token">
          <label>Прозвище<input type="text" name="nickname" maxlength="40" placeholder="Как тебя показывать в клубе"></label>
          <div class="product-profile-actions"><button type="submit">Сохранить прозвище</button></div>
        </form>
        <form class="product-profile-form" action="/account/profile/avatar" method="post" enctype="multipart/form-data">
          <input type="hidden" name="csrf_token">
          <label>Тип аватарки<select name="avatar_kind"><option value="photo">Фото</option><option value="sticker">Стикер PNG / WEBP</option></select></label>
          <label>Изображение<input type="file" name="avatar" accept="image/png,image/webp,image/jpeg" required></label>
          <div class="product-profile-actions"><button type="submit">Загрузить аватарку</button></div>
        </form>
        <form class="product-profile-form" action="/account/profile/avatar/remove" method="post">
          <input type="hidden" name="csrf_token">
          <div class="product-profile-actions"><button class="secondary" type="submit">Убрать аватарку</button></div>
        </form>
      </div>`;
    editor.querySelectorAll('input[name="csrf_token"]').forEach((input) => { input.value = csrfToken(); });
    editor.querySelector('input[name="nickname"]').value = nickname;
    accountPanel.insertAdjacentElement('beforebegin', editor);
  }

  function normalizeCustomEngagementIcons() {
    document.querySelectorAll('[data-engagement-icon-path]').forEach((holder) => {
      const path = holder.dataset.engagementIconPath || '';
      if (!path || holder.querySelector('img')) return;
      holder.textContent = '';
      holder.classList.add('has-custom-art');
      const image = document.createElement('img');
      image.src = path;
      image.alt = '';
      image.loading = 'lazy';
      holder.append(image);
    });
  }

  function setChatUnread(count) {
    if (!launcher) return;
    const value = Math.max(0, Number(count || 0));
    const badge = launcher.querySelector('.member-chat-unread');
    launcher.classList.toggle('has-unread', value > 0);
    if (badge) badge.textContent = value > 99 ? '99+' : String(value);
  }

  function chatShouldBeHidden() {
    if (!launcher) return true;
    if (window.location.pathname.startsWith('/quiz')) return true;
    return Boolean(document.querySelector('[data-quiz-active="true"], .quiz-play-shell, .final-table-live'));
  }

  function placeChatLauncher() {
    if (!launcher) return;
    launcher.classList.toggle('is-hidden', chatShouldBeHidden());
    launcher.classList.remove('is-obstructed');
    if (launcher.classList.contains('is-hidden')) return;
    const nav = document.querySelector('.member-bottom-nav');
    const navVisible = nav && getComputedStyle(nav).display !== 'none';
    const navHeight = navVisible ? Math.ceil(nav.getBoundingClientRect().height) + 14 : 18;
    launcher.style.setProperty('--chat-nav-offset', `${navHeight}px`);
    launcher.style.setProperty('--chat-lift', '0px');

    let lift = 0;
    const avoid = Array.from(document.querySelectorAll([
      '.member-shell button',
      '.member-shell input',
      '.member-shell select',
      '.member-shell textarea',
      '.app-primary-action',
      '.profile-settings-link',
      '.vault-buy-button',
      '.reward-activate-button',
      '[data-chat-avoid]',
    ].join(','))).filter((node) => {
      const style = getComputedStyle(node);
      const rect = node.getBoundingClientRect();
      return style.display !== 'none'
        && style.visibility !== 'hidden'
        && rect.width > 0
        && rect.height > 0
        && rect.bottom > 0
        && rect.top < innerHeight;
    });

    for (let pass = 0; pass < 8; pass += 1) {
      launcher.style.setProperty('--chat-lift', `${lift}px`);
      const bubble = launcher.getBoundingClientRect();
      const hit = avoid.find((node) => {
        const rect = node.getBoundingClientRect();
        return !(bubble.right < rect.left || bubble.left > rect.right || bubble.bottom < rect.top || bubble.top > rect.bottom);
      });
      if (!hit) return;
      const rect = hit.getBoundingClientRect();
      lift += Math.max(0, bubble.bottom - rect.top) + 12;
      if (bubble.top < 82) break;
    }

    launcher.classList.add('is-obstructed');
  }

  function prepareChatLauncher() {
    if (!launcher) return;
    const back = `${window.location.pathname}${window.location.search}`;
    launcher.href = `/account/chats?back=${encodeURIComponent(back)}`;
    placeChatLauncher();
    let frame = 0;
    const schedule = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(placeChatLauncher);
    };
    window.addEventListener('resize', schedule, { passive: true });
    window.addEventListener('scroll', schedule, { passive: true });
    new MutationObserver(schedule).observe(document.body, { subtree: true, childList: true, attributes: true, attributeFilter: ['hidden', 'class', 'open'] });
  }

  async function loadState() {
    if (!page && !launcher) return null;
    try {
      const response = await fetch('/api/account/product-shell', { credentials: 'same-origin', headers: { Accept: 'application/json' } });
      if (!response.ok) return null;
      return await response.json();
    } catch (_) {
      return null;
    }
  }

  cleanHomeCopy();
  mountStoreTabs();
  normalizeCustomEngagementIcons();
  prepareChatLauncher();

  loadState().then((state) => {
    if (!state) return;
    mountHomeTournament(state);
    mountScheduleTabs(state);
    mountProfileEditor(state);
    setChatUnread(state.chat?.unread || 0);
    placeChatLauncher();
  });
})();
