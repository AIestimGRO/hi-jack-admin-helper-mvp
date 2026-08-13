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

  const formatDuration = (value) => {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return '—';
    const seconds = Math.max(0, Number(value)) / 1000;
    if (seconds < 60) return `${seconds.toFixed(1)}s`;
    const minutes = Math.floor(seconds / 60);
    const remainder = (seconds - minutes * 60).toFixed(1).padStart(4, '0');
    return `${minutes}:${remainder}`;
  };

  const calendarRatingRow = (row, period) => {
    const article = document.createElement('article');
    const placeNumber = Number(row.place || 0);
    article.className = [
      placeNumber > 0 && placeNumber <= 3 ? `podium podium-${placeNumber}` : '',
      period === 'month' && !row.place ? 'is-calibrating' : '',
    ].filter(Boolean).join(' ');

    const place = document.createElement('span');
    place.className = 'leaderboard-place';
    place.textContent = row.place ? String(row.place) : '•••';

    const player = document.createElement('span');
    player.className = 'leaderboard-player';
    const name = document.createElement('strong');
    name.textContent = row.display_name || 'Игрок';
    const details = document.createElement('small');
    if (period === 'month') {
      details.textContent = `${Number(row.accuracy || 0)}% · ${Number(row.completed_count || 0)} игр · ${Number(row.active_days || 0)} активных дней`;
    } else {
      details.textContent = `${Number(row.accuracy || 0)}% · ${formatDuration(row.average_answer_time_ms)}/ответ · ${Number(row.completed_count || 0)} игр`;
    }
    player.append(name, details);

    const score = document.createElement('strong');
    if (period === 'month' && !row.place) {
      score.className = 'leaderboard-calibration';
      score.textContent = 'Калибровка';
      const small = document.createElement('small');
      small.textContent = `${Number(row.completed_count || 0)}/3 игр · ${Number(row.question_total || 0)}/30 ответов`;
      score.append(small);
    } else {
      score.className = 'leaderboard-points';
      score.textContent = period === 'month' ? String(row.rating_score ?? 0) : String(row.points ?? 0);
      const small = document.createElement('small');
      small.textContent = period === 'month' ? 'балл' : 'points';
      score.append(small);
    }

    article.append(place, player, score);
    return article;
  };

  const renderCalendarJacksideRating = async () => {
    if (!location.pathname.startsWith('/account')) return;
    const params = new URLSearchParams(location.search);
    if ((params.get('tab') || '') !== 'rating') return;
    const section = params.get('section') || 'month';
    if (!['month', 'all'].includes(section)) return;
    const period = section === 'all' ? 'year' : 'month';
    const list = document.querySelector('.jackside-leaderboard');
    if (!list) return;

    const tabs = document.querySelector('.rating-period-tabs');
    const yearTab = tabs?.querySelector('a[href*="section=all"]');
    if (yearTab) yearTab.textContent = 'Год';

    let payload;
    try { payload = await json(`/api/account/jackside-calendar-rating?period=${period}`); } catch (_) { return; }
    const rows = Array.isArray(payload.rows) ? payload.rows : [];
    list.textContent = '';
    if (!rows.length) {
      const empty = document.createElement('div');
      empty.className = 'member-card smart-empty';
      empty.innerHTML = '<span>HJ</span><div><h3>За этот период пока нет завершённых игр</h3></div>';
      list.append(empty);
    } else {
      rows.forEach((row) => list.append(calendarRatingRow(row, period)));
    }

    const sectionRoot = list.closest('.jackside-rating-section');
    const heading = sectionRoot?.querySelector('.app-section-head h2');
    if (heading) heading.textContent = `Рейтинг JACKSIDE — ${period === 'month' ? 'месяц' : 'год'}`;
    const note = sectionRoot?.querySelector('.jackside-rating-note');
    if (note) {
      note.textContent = period === 'month'
        ? `Календарный месяц ${payload.label || ''}. Учитываются только завершённые JACKSIDE этого месяца; прошлый месяц не смешивается с текущим.`
        : `Календарный год ${payload.label || ''}. JACKSIDE points считаются только по завершённым играм этого года.`;
    }
    list.dataset.calendarRating = period;
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
    const profileRefs = Array.isArray(payload.profile_refs) ? payload.profile_refs : [];

    rows.slice(0, profileRefs.length).forEach((article, index) => {
      const strong = article.querySelector('.leaderboard-player strong');
      if (!strong || strong.closest('a.player-profile-link')) return;
      const profileRef = String(profileRefs[index] || '').trim();
      if (!profileRef) return;
      const link = document.createElement('a');
      link.className = 'player-profile-link';
      link.href = `/players/p/${encodeURIComponent(profileRef)}`;
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
    renderCalendarJacksideRating().finally(linkLeaderboardRows);
    observeRatingChanges();
    window.setTimeout(() => {
      compactFeaturedJackside();
      injectSocialHub();
      renderCalendarJacksideRating().finally(linkLeaderboardRows);
    }, 650);
  };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', run, { once: true });
  else run();
})();
