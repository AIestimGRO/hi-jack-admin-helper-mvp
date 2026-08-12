(() => {
  const ensureHotfixStyle = () => {
    if (document.querySelector('link[data-prelaunch-ui-hotfix]')) return;
    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = '/static/css/prelaunch-ui-hotfix.css?v=1';
    link.dataset.prelaunchUiHotfix = 'true';
    document.head.appendChild(link);
  };

  const json = async (url) => {
    const response = await fetch(url, { credentials: 'same-origin', headers: { Accept: 'application/json' } });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.json();
  };

  const compactFeaturedJackside = () => {
    const card = document.querySelector('.quiz-feature-card');
    if (!card?.querySelector('.quiz-feature-art')) return;
    const copy = card.querySelector('.quiz-feature-copy');
    if (!copy) return;

    const dateLine = [...copy.children].find((node) => (
      node.tagName === 'P'
      && !node.classList.contains('quiz-feature-prize')
      && !node.classList.contains('quiz-feature-hint')
      && !node.classList.contains('quiz-feature-seated')
    ));
    if (!dateLine) return;

    const times = [...String(dateLine.textContent || '').matchAll(/\b(\d{1,2}:\d{2})\b/g)];
    if (!times.length) return;
    const time = times.at(-1)[1];
    dateLine.textContent = card.classList.contains('upcoming') ? `Старт · ${time}` : `Сегодня · ${time}`;
  };

  const socialCard = (links) => {
    const card = document.createElement('a');
    card.className = 'social-hub-card';
    card.href = '/account/links';
    const labels = links.slice(0, 3).map((item) => item.link_type === 'telegram' ? 'Telegram' : item.link_type === 'miniapp' ? 'Mini App' : item.link_type === 'maps' ? 'Карты' : item.title);
    card.innerHTML = `<span class="social-hub-mark">HJ</span><span><strong>Hi, Jack в сети</strong><small>${labels.join(' · ')}</small></span><b aria-hidden="true">›</b>`;
    return card;
  };

  const injectSocialHub = async () => {
    if (!location.pathname.startsWith('/account') || location.pathname === '/account/links') return;
    let payload;
    try { payload = await json('/api/account/club-links'); } catch (_) { return; }
    const links = Array.isArray(payload.links) ? payload.links : [];
    if (!links.length) return;

    const params = new URLSearchParams(location.search);
    const tab = params.get('tab') || 'home';
    const eligible = links.filter((item) => tab === 'profile' ? item.show_profile : item.show_home);
    if (!eligible.length) return;

    const mount = document.querySelector('.member-app-page') || document.querySelector('.member-shell');
    if (!mount || mount.querySelector('.social-hub-card')) return;
    const card = socialCard(eligible);

    if (tab === 'home') {
      const gameCard = mount.querySelector('.quiz-feature-card');
      const gameSection = gameCard?.closest('.home-section');
      if (gameSection) gameSection.insertAdjacentElement('afterend', card);
      else mount.appendChild(card);
    } else if (tab === 'profile') {
      const firstProfileSection = mount.querySelector('.profile-section, .profile-rich-section');
      if (firstProfileSection) firstProfileSection.insertAdjacentElement('beforebegin', card);
      else mount.appendChild(card);
    } else {
      mount.appendChild(card);
    }
  };

  let ratingLinkSerial = 0;
  const linkLeaderboardRows = async () => {
    if (!location.pathname.startsWith('/account')) return;
    const params = new URLSearchParams(location.search);
    if ((params.get('tab') || '') !== 'rating') return;

    const requestedSection = params.get('section') || 'month';
    const isClub = requestedSection === 'club';
    let rows;
    let section;
    let period = '';

    if (isClub) {
      rows = [...document.querySelectorAll('.hijack-rating-list article')];
      if (!rows.length) rows = [...document.querySelectorAll('.club-leaderboard:not(.jackside-leaderboard) article')];
      section = 'club';
      period = document.querySelector('[data-hijack-period].active')?.dataset.hijackPeriod || 'global';
    } else {
      rows = [...document.querySelectorAll('.jackside-leaderboard article')];
      section = ['today', 'month', 'all'].includes(requestedSection) ? requestedSection : 'month';
    }
    if (!rows.length) return;

    const serial = ++ratingLinkSerial;
    const limit = Math.min(rows.length, 100);
    const query = new URLSearchParams({ section, offset: '0', limit: String(limit) });
    if (isClub) query.set('period', period);

    let payload;
    try { payload = await json(`/api/account/rating-profile-links?${query}`); } catch (_) { return; }
    if (serial !== ratingLinkSerial) return;
    const clientIds = Array.isArray(payload.client_ids) ? payload.client_ids : [];

    rows.slice(0, clientIds.length).forEach((article, index) => {
      const strong = article.querySelector('.leaderboard-player strong');
      if (!strong || strong.closest('a.player-profile-link')) return;
      const clientId = Number(clientIds[index]);
      if (!Number.isInteger(clientId) || clientId <= 0) return;
      const link = document.createElement('a');
      link.className = 'player-profile-link';
      link.href = `/players/${clientId}`;
      link.setAttribute('aria-label', `Открыть профиль ${String(strong.textContent || 'игрока').trim()}`);
      strong.replaceWith(link);
      link.appendChild(strong);
    });

    const head = document.querySelector('.jackside-rating-section .app-section-head, .club-rating-summary, .hijack-rating-head');
    if (head && !document.querySelector('.players-directory-shortcut')) {
      const shortcut = document.createElement('a');
      shortcut.className = 'players-directory-shortcut';
      shortcut.href = '/players';
      shortcut.textContent = 'Профили игроков ›';
      head.appendChild(shortcut);
    }
  };

  const observeRatingChanges = () => {
    const page = document.querySelector('.member-app-page');
    const params = new URLSearchParams(location.search);
    if (!page || (params.get('tab') || '') !== 'rating') return;
    let timer = 0;
    const observer = new MutationObserver(() => {
      window.clearTimeout(timer);
      timer = window.setTimeout(linkLeaderboardRows, 80);
    });
    observer.observe(page, { subtree: true, childList: true });
  };

  const run = () => {
    ensureHotfixStyle();
    compactFeaturedJackside();
    injectSocialHub();
    linkLeaderboardRows();
    observeRatingChanges();
    window.setTimeout(() => {
      compactFeaturedJackside();
      injectSocialHub();
      linkLeaderboardRows();
    }, 650);
  };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', run, { once: true });
  else run();
})();
