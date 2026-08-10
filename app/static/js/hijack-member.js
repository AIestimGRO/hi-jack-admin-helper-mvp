(() => {
  const page = document.querySelector('.member-app-page');
  if (!page) return;

  const tab = page.dataset.accountTab || '';
  const RATING_PAGE_SIZE = 25;

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

  function ratingRow(row, meClientId, mode) {
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
    const details = document.createElement('small');
    const detailParts = [];
    if (Number(row.client_id) === Number(meClientId)) detailParts.push('Это ты');
    detailParts.push(`${Number(row.kills || 0)} киллов`);
    if (mode !== 'latest') detailParts.push(`${Number(row.tournaments || 0)} турниров`);
    details.textContent = detailParts.join(' · ');
    player.append(name, details);

    const points = document.createElement('strong');
    points.className = 'leaderboard-points';
    points.textContent = String(row.points ?? 0);
    const suffix = document.createElement('small');
    suffix.textContent = 'очков';
    points.append(suffix);

    article.append(place, player, points);
    return article;
  }

  function renderHiJackRating() {
    if (tab !== 'rating') return;
    const sectionParam = new URL(window.location.href).searchParams.get('section');
    if (sectionParam !== 'club') return;

    const oldSummary = page.querySelector('.club-rating-summary');
    const oldList = page.querySelector('.club-leaderboard:not(.jackside-leaderboard)');
    if (!oldSummary || !oldList || page.querySelector('.hijack-rating-hub')) return;

    const hub = document.createElement('section');
    hub.className = 'hijack-rating-hub';
    hub.innerHTML = `
      <div class="hijack-rating-head">
        <div><p class="member-eyebrow">HI, JACK!</p><h2>Рейтинг клуба</h2></div>
        <div class="hijack-rating-me" data-hijack-me></div>
      </div>
      <nav class="hijack-rating-periods" aria-label="Период рейтинга HI, JACK!">
        <button type="button" data-hijack-period="global">Глобальный</button>
        <button type="button" data-hijack-period="month">Месяц</button>
        <button type="button" data-hijack-period="latest">Последний турнир</button>
      </nav>
      <div class="hijack-rating-caption" data-hijack-caption></div>
      <div class="club-leaderboard hijack-rating-list" data-hijack-list></div>
      <button class="member-secondary hijack-rating-more" type="button" data-hijack-more hidden>Показать ещё</button>
    `;

    oldSummary.replaceWith(hub);
    oldList.remove();

    const list = hub.querySelector('[data-hijack-list]');
    const caption = hub.querySelector('[data-hijack-caption]');
    const meBox = hub.querySelector('[data-hijack-me]');
    const moreButton = hub.querySelector('[data-hijack-more]');
    const buttons = Array.from(hub.querySelectorAll('[data-hijack-period]'));

    let currentMode = 'global';
    let loaded = 0;
    let requestSerial = 0;

    const setLoading = (loading) => {
      buttons.forEach((button) => { button.disabled = loading; });
      moreButton.disabled = loading;
      if (loading && loaded === 0) {
        caption.textContent = 'Загружаем рейтинг…';
      }
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

      meBox.textContent = '';
      const small = document.createElement('small');
      small.textContent = 'Твоё место';
      const strong = document.createElement('strong');
      strong.textContent = payload.me?.place ? `#${payload.me.place}` : '—';
      const span = document.createElement('span');
      span.textContent = payload.me
        ? `${payload.me.points} очков · ${payload.me.kills} киллов`
        : 'Нет данных';
      meBox.append(small, strong, span);
    };

    const loadPage = async (mode, append = false) => {
      const serial = ++requestSerial;
      if (!append) {
        currentMode = mode;
        loaded = 0;
        list.textContent = '';
        moreButton.hidden = true;
      }
      buttons.forEach((button) => button.classList.toggle('active', button.dataset.hijackPeriod === mode));
      setLoading(true);

      const url = `/api/account/hijack-rating-page?period=${encodeURIComponent(mode)}&offset=${loaded}&limit=${RATING_PAGE_SIZE}`;
      const payload = await getJson(url);
      if (serial !== requestSerial) return;
      setLoading(false);

      if (!payload) {
        if (!append) showEmpty('Не удалось загрузить рейтинг');
        caption.textContent = 'Не удалось получить данные. Попробуйте ещё раз.';
        return;
      }
      if (!payload.has_data) {
        showEmpty('Рейтинг пока не загружен');
        meBox.textContent = '';
        moreButton.hidden = true;
        return;
      }

      const rows = Array.isArray(payload.rows) ? payload.rows : [];
      if (!append && !rows.length) showEmpty('Нет данных за этот период');
      else {
        const meClientId = payload.me?.client_id || null;
        rows.forEach((row) => list.append(ratingRow(row, meClientId, mode)));
      }

      loaded += rows.length;
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
    loadPage('global', false);
  }

  function referralNode(node, depth) {
    const wrapper = document.createElement(node.children?.length ? 'details' : 'div');
    wrapper.className = `referral-tree-node depth-${Math.min(depth, 6)}`;
    if (wrapper.tagName === 'DETAILS' && depth <= 2) wrapper.open = true;

    const head = document.createElement(wrapper.tagName === 'DETAILS' ? 'summary' : 'div');
    head.className = 'referral-tree-person';
    const avatar = document.createElement('span');
    avatar.className = 'referral-tree-avatar';
    avatar.textContent = (node.display_name || 'HJ').trim().slice(0, 1).toUpperCase();
    const copy = document.createElement('span');
    copy.className = 'referral-tree-copy';
    const name = document.createElement('strong');
    name.textContent = node.display_name || 'Игрок';
    const meta = document.createElement('small');
    const childCount = Array.isArray(node.children) ? node.children.length : 0;
    meta.textContent = `${node.completed_days || 0}/3 дней${node.qualified ? ' · квалифицирован' : ''}${childCount ? ` · +${childCount}` : ''}`;
    copy.append(name, meta);
    head.append(avatar, copy);
    wrapper.append(head);

    if (node.children?.length) {
      const children = document.createElement('div');
      children.className = 'referral-tree-children';
      node.children.forEach((child) => children.append(referralNode(child, depth + 1)));
      wrapper.append(children);
    }
    return wrapper;
  }

  function renderReferralTree(payload) {
    if (tab !== 'profile' || !payload?.root) return;
    const card = page.querySelector('.profile-referral-card');
    if (!card || card.querySelector('.referral-tree-shell')) return;

    const history = card.querySelector('.profile-referral-history');
    const shell = document.createElement('section');
    shell.className = 'referral-tree-shell';
    shell.innerHTML = `
      <div class="referral-tree-head">
        <div><small>Структура приглашений</small><strong>Реферальное дерево</strong></div>
        <div class="referral-tree-stats">
          <span><b>${Number(payload.direct || 0)}</b> 1 линия</span>
          <span><b>${Number(payload.total || 0)}</b> всего</span>
          <span><b>${Number(payload.max_depth || 0)}</b> глубина</span>
        </div>
      </div>
      <div class="referral-tree-scroll" data-referral-tree></div>
    `;
    const tree = shell.querySelector('[data-referral-tree]');
    const root = document.createElement('div');
    root.className = 'referral-tree-root';
    const rootBadge = document.createElement('span');
    rootBadge.className = 'referral-tree-root-badge';
    rootBadge.textContent = 'Вы';
    const rootName = document.createElement('strong');
    rootName.textContent = payload.root.display_name || 'Вы';
    root.append(rootBadge, rootName);
    tree.append(root);

    const firstLine = document.createElement('div');
    firstLine.className = 'referral-tree-first-line';
    if (payload.root.children?.length) {
      payload.root.children.forEach((child) => firstLine.append(referralNode(child, 1)));
    } else {
      const empty = document.createElement('p');
      empty.className = 'member-muted';
      empty.textContent = 'Приглашённых игроков пока нет.';
      firstLine.append(empty);
    }
    tree.append(firstLine);
    if (payload.truncated) {
      const note = document.createElement('p');
      note.className = 'member-muted referral-tree-note';
      note.textContent = 'Очень глубокая или циклическая ветка была безопасно ограничена.';
      tree.append(note);
    }
    if (history) history.insertAdjacentElement('beforebegin', shell);
    else card.append(shell);
  }

  if (tab === 'rating' && new URL(window.location.href).searchParams.get('section') === 'club') {
    renderHiJackRating();
  }
  if (tab === 'profile') {
    getJson('/api/account/referral-tree').then(renderReferralTree);
  }
})();
