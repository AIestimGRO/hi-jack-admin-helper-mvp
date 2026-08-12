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

  const linkLeaderboardNames = async () => {
    if (!location.pathname.startsWith('/account')) return;
    const params = new URLSearchParams(location.search);
    if ((params.get('tab') || '') !== 'rating') return;
    let payload;
    try { payload = await json('/api/account/player-directory'); } catch (_) { return; }
    const players = Array.isArray(payload.players) ? payload.players : [];
    const grouped = new Map();
    players.forEach((player) => {
      const key = String(player.display_name || '').trim().toLocaleLowerCase('ru-RU');
      if (!key) return;
      const list = grouped.get(key) || [];
      list.push(player);
      grouped.set(key, list);
    });

    document.querySelectorAll('.leaderboard-player strong').forEach((strong) => {
      if (strong.closest('a')) return;
      const key = String(strong.textContent || '').trim().toLocaleLowerCase('ru-RU');
      const matches = grouped.get(key) || [];
      if (matches.length !== 1) return;
      const link = document.createElement('a');
      link.className = 'player-profile-link';
      link.href = `/players/${matches[0].client_id}`;
      strong.replaceWith(link);
      link.appendChild(strong);
    });

    const head = document.querySelector('.jackside-rating-section .app-section-head, .club-rating-summary');
    if (head && !document.querySelector('.players-directory-shortcut')) {
      const shortcut = document.createElement('a');
      shortcut.className = 'players-directory-shortcut';
      shortcut.href = '/players';
      shortcut.textContent = 'Профили игроков ›';
      head.appendChild(shortcut);
    }
  };

  const run = () => {
    ensureHotfixStyle();
    compactFeaturedJackside();
    injectSocialHub();
    linkLeaderboardNames();
    window.setTimeout(() => {
      compactFeaturedJackside();
      injectSocialHub();
      linkLeaderboardNames();
    }, 650);
  };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', run, { once: true });
  else run();
})();
