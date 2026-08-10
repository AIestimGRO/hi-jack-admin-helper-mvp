(() => {
  const page = document.querySelector('.member-app-page[data-account-tab="profile"]');
  if (!page) return;

  const url = new URL(window.location.href);
  const view = url.searchParams.get('view') === 'settings' ? 'settings' : 'main';
  page.dataset.profileView = view;

  function csrfToken() {
    return document.querySelector('input[name="csrf_token"]')?.value || '';
  }

  function configureHeader() {
    const header = page.querySelector('.profile-heading');
    if (!header) return;
    const copy = header.querySelector('div:not(.profile-avatar-large)');
    if (!copy) return;
    const eyebrow = copy.querySelector('.member-eyebrow');
    const title = copy.querySelector('h1');
    const muted = copy.querySelector('.member-muted');

    const action = document.createElement('a');
    action.className = 'profile-account-settings-button';
    if (view === 'settings') {
      action.href = '/account?tab=profile';
      action.innerHTML = '<span aria-hidden="true">←</span> Профиль';
      if (title) title.textContent = 'Настройки аккаунта';
      if (muted) muted.textContent = 'Профиль и личные данные';
    } else {
      action.href = '/account?tab=profile&view=settings';
      action.innerHTML = 'Настройки аккаунта <span aria-hidden="true">›</span>';
    }
    if (eyebrow) eyebrow.replaceWith(action);
    else copy.prepend(action);
  }

  function simplifyProfileView() {
    page.querySelector('#activity')?.remove();
    page.classList.add(view === 'settings' ? 'profile-settings-mode' : 'profile-main-mode');
  }

  function iconNode(item) {
    const holder = document.createElement('span');
    holder.className = 'profile-emblem-icon';
    holder.setAttribute('aria-hidden', 'true');
    if (item.icon_path) {
      holder.classList.add('has-custom-art');
      const image = document.createElement('img');
      image.src = item.icon_path;
      image.alt = '';
      image.loading = 'lazy';
      holder.append(image);
    } else {
      holder.textContent = item.icon || (item.kind === 'achievement' ? '◆' : '★');
    }
    return holder;
  }

  function subtitle(item) {
    if (item.state === 'locked') {
      return item.kind === 'achievement' ? 'Не открыто · достижение' : 'Не открыто · звание';
    }
    if (item.kind === 'achievement') return 'Достижение получено';
    if (item.temporary) return 'Активное звание';
    if (item.selected) return 'Основное звание';
    return 'Звание получено';
  }

  function collectionCard(item) {
    const details = document.createElement('details');
    details.className = [
      'profile-emblem-card',
      item.state === 'locked' ? 'is-locked' : 'is-active-title',
      item.kind === 'achievement' ? 'is-achievement' : '',
      item.temporary ? 'is-temporary' : '',
      item.selected ? 'is-selected' : '',
    ].filter(Boolean).join(' ');
    details.dataset.emblemState = item.state === 'locked' ? 'locked' : 'unlocked';
    details.dataset.emblemKind = item.kind || 'title';

    const summary = document.createElement('summary');
    const copy = document.createElement('span');
    copy.className = 'profile-emblem-summary-copy';
    const name = document.createElement('strong');
    name.textContent = item.name || (item.kind === 'achievement' ? 'Достижение' : 'Звание');
    const small = document.createElement('small');
    small.textContent = subtitle(item);
    copy.append(name, small);
    summary.append(iconNode(item), copy);

    const detail = document.createElement('div');
    detail.className = 'profile-emblem-detail';
    const description = document.createElement('p');
    description.textContent = item.description || (item.state === 'locked' ? 'Это достижение ещё предстоит открыть.' : 'Награда HI, JACK CLUB!');
    detail.append(description);

    const meta = document.createElement('span');
    meta.className = 'profile-emblem-meta';
    if (item.state === 'locked') {
      meta.textContent = 'Ещё не открыто';
    } else if (item.kind === 'achievement') {
      meta.textContent = 'Открыто';
    } else if (item.temporary && item.expires_at) {
      meta.textContent = 'Активно до ' + String(item.expires_at).slice(0, 16).replace('T', ' ');
    } else {
      meta.textContent = item.selected ? 'Сейчас отображается как основное звание' : 'Звание получено';
    }
    detail.append(meta);

    if (item.state === 'active' && item.kind === 'title' && !item.temporary && !item.selected && item.member_title_id) {
      const form = document.createElement('form');
      form.action = `/account/titles/${item.member_title_id}/select`;
      form.method = 'post';
      const csrf = document.createElement('input');
      csrf.type = 'hidden';
      csrf.name = 'csrf_token';
      csrf.value = csrfToken();
      const button = document.createElement('button');
      button.className = 'profile-title-select';
      button.type = 'submit';
      button.textContent = 'Сделать основным';
      form.append(csrf, button);
      detail.append(form);
    }

    details.append(summary, detail);
    details.addEventListener('toggle', () => {
      if (!details.open) return;
      const grid = details.closest('.profile-emblem-grid');
      grid?.querySelectorAll('.profile-emblem-card[open]').forEach((other) => {
        if (other !== details) other.open = false;
      });
      window.requestAnimationFrame(() => {
        details.scrollIntoView({
          behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth',
          block: 'nearest',
          inline: 'center',
        });
      });
    });
    return details;
  }

  function prioritizeCollection(items) {
    return items
      .map((item, index) => ({ item, index }))
      .sort((left, right) => {
        const leftLocked = left.item?.state === 'locked' ? 1 : 0;
        const rightLocked = right.item?.state === 'locked' ? 1 : 0;
        if (leftLocked !== rightLocked) return leftLocked - rightLocked;

        const leftSelected = left.item?.selected ? 0 : 1;
        const rightSelected = right.item?.selected ? 0 : 1;
        if (leftLocked === 0 && leftSelected !== rightSelected) return leftSelected - rightSelected;

        return left.index - right.index;
      })
      .map(({ item }) => item);
  }

  function renderCollection(payload) {
    if (view !== 'main') return;
    const stage = page.querySelector('.profile-achievement-stage');
    const grid = stage?.querySelector('.profile-emblem-grid');
    const intro = stage?.querySelector('.profile-achievement-intro');
    if (!stage || !grid || !intro) return;

    intro.innerHTML = '<div><h2 id="profile-emblems-heading">Hi, Titles!</h2></div>';
    stage.querySelector('.profile-current-title')?.remove();
    stage.querySelector('.profile-title-crown')?.remove();
    grid.textContent = '';

    const rawItems = Array.isArray(payload?.items) ? payload.items : [];
    const items = prioritizeCollection(rawItems);
    if (!items.length) {
      const empty = document.createElement('div');
      empty.className = 'profile-empty-emblems';
      empty.textContent = 'Коллекция появится вместе с первыми званиями и достижениями.';
      grid.append(empty);
      return;
    }
    items.forEach((item) => grid.append(collectionCard(item)));
    grid.dataset.twoRowTitles = '1';
    grid.dataset.unlockedCount = String(items.filter((item) => item.state !== 'locked').length);
    grid.setAttribute('aria-label', `Hi, Titles! · открыто ${Number(payload.active_count || 0)} из ${Number(payload.total_count || items.length)}`);
  }

  async function loadCollection() {
    if (view !== 'main') return;
    try {
      const response = await fetch('/api/account/title-collection', {
        credentials: 'same-origin',
        headers: { Accept: 'application/json' },
      });
      if (!response.ok) return;
      renderCollection(await response.json());
    } catch (_) {
      // Keep the server-rendered profile usable even if the enhancement fails.
    }
  }

  configureHeader();
  simplifyProfileView();
  loadCollection();
})();
