(() => {
  const page = document.querySelector('.member-app-page');
  if (!page) return;

  function activatePanels(kind, requested, push = true) {
    const isStore = kind === 'store';
    const target = isStore
      ? (requested === 'cards' ? 'cards' : 'market')
      : (requested === 'tournaments' ? 'tournaments' : 'quizzes');
    const buttonSelector = isStore ? '[data-store-tab]' : '[data-schedule-tab]';
    const panelSelector = isStore ? '[data-store-panel]' : '[data-schedule-panel]';
    const key = isStore ? 'store' : 'schedule';

    page.querySelectorAll(buttonSelector).forEach((button) => {
      const value = isStore ? button.dataset.storeTab : button.dataset.scheduleTab;
      button.classList.toggle('active', value === target);
    });
    page.querySelectorAll(panelSelector).forEach((panel) => {
      const value = isStore ? panel.dataset.storePanel : panel.dataset.schedulePanel;
      panel.hidden = value !== target;
    });
    if (push) {
      const url = new URL(window.location.href);
      url.searchParams.set(key, target);
      history.replaceState({}, '', url);
    }
  }

  page.querySelector('.store-tabs')?.addEventListener('click', (event) => {
    const button = event.target.closest('[data-store-tab]');
    if (button) activatePanels('store', button.dataset.storeTab);
  });

  page.querySelector('.schedule-tabs')?.addEventListener('click', (event) => {
    const button = event.target.closest('[data-schedule-tab]');
    if (button) activatePanels('schedule', button.dataset.scheduleTab);
  });

  document.querySelectorAll('[data-referral-copy]').forEach((button) => {
    button.addEventListener('click', async () => {
      const selector = button.getAttribute('data-referral-copy');
      const input = selector ? document.querySelector(selector) : null;
      if (!input) return;
      try {
        await navigator.clipboard.writeText(input.value);
        const oldText = button.textContent;
        button.textContent = 'Скопировано';
        window.setTimeout(() => { button.textContent = oldText; }, 1400);
      } catch (_) {
        input.focus();
        input.select();
        document.execCommand('copy');
      }
    });
  });

  page.querySelectorAll('.profile-emblem-card').forEach((details) => {
    details.addEventListener('toggle', () => {
      if (!details.open) return;
      const grid = details.closest('.profile-emblem-grid');
      grid?.querySelectorAll('.profile-emblem-card[open]').forEach((other) => {
        if (other !== details) other.open = false;
      });
      if (!window.matchMedia('(max-width: 680px)').matches) return;
      window.requestAnimationFrame(() => {
        details.scrollIntoView({
          behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth',
          block: 'nearest',
          inline: 'center',
        });
      });
    });
  });

  async function getJson(url) {
    try {
      const response = await fetch(url, {
        credentials: 'same-origin',
        headers: { Accept: 'application/json' },
      });
      if (!response.ok) return null;
      return await response.json();
    } catch (_) {
      return null;
    }
  }

  function clubRatingRow(row, profileRef, mode, meClientId) {
    const article = document.createElement('article');
    article.className = [
      Number(row.client_id) === Number(meClientId) ? 'is-you' : '',
      Number(row.place) <= 3 ? `podium podium-${row.place}` : '',
    ].filter(Boolean).join(' ');

    const place = document.createElement('span');
    place.className = 'leaderboard-place';
    place.textContent = String(row.place || '—');

    const player = document.createElement('span');
    player.className = 'leaderboard-player';
    const name = document.createElement('strong');
    name.textContent = row.display_name || 'Игрок';
    if (profileRef) {
      const link = document.createElement('a');
      link.className = 'player-profile-link';
      link.href = `/players/p/${encodeURIComponent(profileRef)}`;
      link.append(name);
      player.append(link);
    } else {
      player.append(name);
    }
    const details = document.createElement('small');
    const detailParts = [];
    if (Number(row.client_id) === Number(meClientId)) detailParts.push('Это ты');
    detailParts.push(`${Number(row.kills || 0)} киллов`);
    if (mode !== 'latest') detailParts.push(`${Number(row.tournaments || 0)} турниров`);
    details.textContent = detailParts.join(' · ');
    player.append(details);

    const points = document.createElement('strong');
    points.className = 'leaderboard-points';
    points.textContent = String(row.points ?? 0);
    const suffix = document.createElement('small');
    suffix.textContent = 'очков';
    points.append(suffix);

    article.append(place, player, points);
    return article;
  }

  function installClubRating() {
    const hub = page.querySelector('[data-hijack-rating-hub]');
    if (!hub) return;
    const list = hub.querySelector('[data-hijack-list]');
    const caption = hub.querySelector('[data-hijack-caption]');
    const meBox = hub.querySelector('[data-hijack-me]');
    const moreButton = hub.querySelector('[data-hijack-more]');
    const buttons = Array.from(hub.querySelectorAll('[data-hijack-period]'));
    if (!list || !caption || !meBox || !moreButton) return;

    let currentMode = 'global';
    let loaded = Number(list.dataset.hijackLoaded || list.querySelectorAll('article').length || 0);
    let requestSerial = 0;

    const setLoading = (loading) => {
      buttons.forEach((button) => { button.disabled = loading; });
      moreButton.disabled = loading;
    };

    const showEmpty = (message) => {
      list.textContent = '';
      const empty = document.createElement('div');
      empty.className = 'member-card smart-empty';
      empty.innerHTML = `<span>HJ</span><div><h3>${message}</h3><p>После следующей загрузки рейтинга здесь появятся игроки.</p></div>`;
      list.append(empty);
    };

    const applyMeta = (payload, mode) => {
      if (mode === 'latest') {
        caption.textContent = payload.tournament_name
          ? `${payload.tournament_name}${payload.tournament_date ? ` · ${payload.tournament_date}` : ''}`
          : 'Последний загруженный турнир';
      } else if (mode === 'month') {
        caption.textContent = `Сумма рейтингов всех турниров месяца · ${payload.label || ''}`;
      } else {
        caption.textContent = 'Глобальный рейтинг · весь накопленный период';
      }
      const label = meBox.querySelector('small');
      const strong = meBox.querySelector('strong');
      if (label) label.textContent = 'Твоё место в рейтинге';
      if (strong) strong.textContent = payload.me?.place ? `#${payload.me.place}` : '—';
    };

    const loadPage = async (mode, append = false) => {
      const serial = ++requestSerial;
      if (!append) {
        currentMode = mode;
        loaded = 0;
        list.textContent = '';
        moreButton.hidden = true;
      }
      buttons.forEach((button) => {
        button.classList.toggle('active', button.dataset.hijackPeriod === mode);
      });
      setLoading(true);

      const params = new URLSearchParams({
        period: mode,
        offset: String(loaded),
        limit: '25',
      });
      const linkParams = new URLSearchParams({
        section: 'club',
        period: mode,
        offset: String(loaded),
        limit: '25',
      });
      const [payload, links] = await Promise.all([
        getJson(`/api/account/hijack-rating-page?${params}`),
        getJson(`/api/account/rating-profile-links?${linkParams}`),
      ]);
      if (serial !== requestSerial) return;
      setLoading(false);

      if (!payload) {
        if (!append) showEmpty('Не удалось загрузить рейтинг');
        return;
      }
      if (!payload.has_data) {
        showEmpty('Рейтинг пока не загружен');
        moreButton.hidden = true;
        applyMeta(payload, mode);
        return;
      }

      const rows = Array.isArray(payload.rows) ? payload.rows : [];
      const refs = Array.isArray(links?.profile_refs) ? links.profile_refs : [];
      if (!append && !rows.length) showEmpty('Нет данных за этот период');
      else rows.forEach((row, index) => {
        list.append(clubRatingRow(row, refs[index] || '', mode, payload.me?.client_id || null));
      });

      loaded += rows.length;
      list.dataset.hijackLoaded = String(loaded);
      moreButton.hidden = !payload.has_more;
      moreButton.textContent = payload.has_more
        ? `Показать ещё · ${loaded} из ${Number(payload.total || loaded)}`
        : '';
      applyMeta(payload, mode);
    };

    hub.addEventListener('click', (event) => {
      const button = event.target.closest('[data-hijack-period]');
      if (button) loadPage(button.dataset.hijackPeriod, false);
    });
    moreButton.addEventListener('click', () => loadPage(currentMode, true));
  }

  function installStableChat() {
    const launcher = document.querySelector('[data-chat-launcher]');
    if (!launcher) return;
    let pinnedOpen = false;
    let frame = 0;
    const importantSelectors = [
      '.app-primary-action',
      '.vault-buy-button',
      '.reward-activate-button',
      '.profile-settings-link',
      '.store-tabs button',
      '.schedule-tabs button',
      '.rating-period-tabs a',
      '.rating-section-tabs a',
      '[data-chat-avoid]',
    ];

    const overlaps = (a, b) => !(
      a.right <= b.left || a.left >= b.right || a.bottom <= b.top || a.top >= b.bottom
    );

    const expandedRect = () => {
      const nav = document.querySelector('.member-bottom-nav');
      const navRect = nav?.getBoundingClientRect();
      const size = window.innerWidth <= 720 ? 48 : 52;
      const rightGap = window.innerWidth <= 720 ? 12 : 14;
      const bottom = navRect && navRect.height > 0 ? navRect.top - 14 : window.innerHeight - 18;
      return {
        left: window.innerWidth - rightGap - size,
        right: window.innerWidth - rightGap,
        top: bottom - size,
        bottom,
      };
    };

    const refresh = () => {
      if (launcher.classList.contains('is-hidden') || pinnedOpen) {
        launcher.classList.remove('chat-collapsed');
        return;
      }
      const bubble = expandedRect();
      const important = Array.from(document.querySelectorAll(importantSelectors.join(','))).filter((node) => {
        if (node.closest('.member-bottom-nav') || node === launcher || launcher.contains(node)) return false;
        const style = getComputedStyle(node);
        const rect = node.getBoundingClientRect();
        return style.display !== 'none'
          && style.visibility !== 'hidden'
          && rect.width > 0
          && rect.height > 0
          && rect.bottom > 0
          && rect.top < window.innerHeight;
      });
      launcher.classList.toggle('chat-collapsed', important.some((node) => overlaps(bubble, node.getBoundingClientRect())));
    };

    const schedule = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(refresh);
    };

    launcher.addEventListener('click', (event) => {
      if (!launcher.classList.contains('chat-collapsed')) return;
      event.preventDefault();
      event.stopPropagation();
      pinnedOpen = true;
      launcher.classList.remove('chat-collapsed');
      launcher.focus({ preventScroll: true });
    }, true);
    window.addEventListener('resize', schedule, { passive: true });
    window.addEventListener('scroll', schedule, { passive: true });
    schedule();
  }

  installClubRating();
  installStableChat();
})();
