(() => {
  const json = async (url) => {
    const response = await fetch(url, { credentials: 'same-origin', headers: { Accept: 'application/json' } });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.json();
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
    mount.appendChild(socialCard(eligible));
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
    injectSocialHub();
    linkLeaderboardNames();
    window.setTimeout(() => {
      injectSocialHub();
      linkLeaderboardNames();
    }, 650);
  };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', run, { once: true });
  else run();
})();
