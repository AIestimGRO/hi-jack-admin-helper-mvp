(() => {
  if (window.location.pathname !== '/master/hijack-rating') return;

  const csrf = document.querySelector('input[name="csrf_token"]')?.value || '';

  function field(label, input) {
    const wrap = document.createElement('label');
    wrap.style.display = 'grid';
    wrap.style.gap = '5px';
    wrap.style.fontSize = '11px';
    wrap.style.color = 'var(--muted,#99a4a2)';
    wrap.append(label, input);
    return wrap;
  }

  function textInput(name, value = '', type = 'text') {
    const input = document.createElement('input');
    input.name = name;
    input.type = type;
    input.value = value;
    input.style.minHeight = '40px';
    input.style.width = '100%';
    return input;
  }

  function hiddenCsrf() {
    const input = document.createElement('input');
    input.type = 'hidden';
    input.name = 'csrf_token';
    input.value = csrf;
    return input;
  }

  function manualForm(action) {
    const form = document.createElement('form');
    form.action = action;
    form.method = 'post';
    form.style.display = 'grid';
    form.style.gridTemplateColumns = 'minmax(150px,1.4fr) minmax(90px,.8fr) minmax(90px,.8fr)';
    form.style.gap = '8px';
    form.append(hiddenCsrf());
    form.append(
      field('Phone', textInput('phone', '', 'tel')),
      field('ИГР Рейт', textInput('rating_points', '0', 'text')),
      field('ИГР Кил', textInput('kills', '0', 'text')),
    );
    const actions = document.createElement('div');
    actions.style.gridColumn = '1 / -1';
    actions.style.display = 'flex';
    actions.style.flexWrap = 'wrap';
    actions.style.gap = '8px';
    const save = document.createElement('button');
    save.type = 'submit';
    save.name = 'action';
    save.value = 'save';
    save.className = 'primary';
    save.textContent = 'Сохранить игрока';
    const remove = document.createElement('button');
    remove.type = 'submit';
    remove.name = 'action';
    remove.value = 'delete';
    remove.className = 'danger-outline';
    remove.textContent = 'Удалить игрока по Phone';
    remove.addEventListener('click', (event) => {
      if (!window.confirm('Удалить строку этого игрока из выбранного рейтинга?')) event.preventDefault();
    });
    actions.append(save, remove);
    form.append(actions);
    return form;
  }

  function baselineCard(baseline) {
    const section = document.createElement('section');
    section.className = 'card';
    section.style.padding = '20px';
    section.style.marginBottom = '18px';
    section.style.borderColor = 'rgba(224,184,82,.24)';
    const loaded = baseline && Number(baseline.total_rows || 0) > 0;
    section.innerHTML = `
      <p class="eyebrow">Стартовая база</p>
      <h2 style="margin:4px 0 6px">Накопленный глобальный рейтинг</h2>
      <p class="muted" style="margin:0 0 14px">Загружается один раз как текущий накопленный итог. Дальше каждый новый турнир добавляется сверху. Повторная загрузка полностью заменяет эту стартовую базу.</p>
      <p style="margin:0 0 14px;font-size:11px">${loaded
        ? `${baseline.total_rows} строк · найдено ${baseline.matched_rows} · ожидают регистрации ${baseline.unmatched_rows} · некорректных ${baseline.invalid_rows}`
        : 'Стартовая база ещё не загружена.'}</p>
    `;

    const upload = document.createElement('form');
    upload.action = '/api/master/hijack-rating/baseline';
    upload.method = 'post';
    upload.enctype = 'multipart/form-data';
    upload.style.display = 'grid';
    upload.style.gap = '8px';
    upload.append(hiddenCsrf());
    const file = document.createElement('input');
    file.type = 'file';
    file.name = 'rating_file';
    file.accept = '.xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet';
    file.required = true;
    upload.append(field('Excel: Phone / ИГР Рейт / ИГР Кил', file));
    const button = document.createElement('button');
    button.className = 'primary';
    button.type = 'submit';
    button.textContent = loaded ? 'Полностью заменить глобальный рейтинг' : 'Загрузить глобальный рейтинг';
    upload.addEventListener('submit', (event) => {
      if (loaded && !window.confirm('Старый стартовый глобальный рейтинг будет полностью заменён новым файлом. Продолжить?')) event.preventDefault();
    });
    upload.append(button);

    const details = document.createElement('details');
    details.style.marginTop = '12px';
    const summary = document.createElement('summary');
    summary.style.cursor = 'pointer';
    summary.style.fontWeight = '800';
    summary.textContent = 'Исправить одного игрока вручную';
    details.append(summary, manualForm('/api/master/hijack-rating/baseline/entry'));
    section.append(upload, details);
    return section;
  }

  function tournamentEditor(item, article) {
    const details = document.createElement('details');
    details.style.gridColumn = '1 / -1';
    details.style.paddingTop = '8px';
    details.style.borderTop = '1px solid rgba(255,255,255,.07)';
    const summary = document.createElement('summary');
    summary.style.cursor = 'pointer';
    summary.style.fontWeight = '800';
    summary.textContent = 'Исправить / полностью заменить турнир';
    details.append(summary);

    const replace = document.createElement('form');
    replace.action = `/api/master/hijack-rating/${item.id}/replace`;
    replace.method = 'post';
    replace.enctype = 'multipart/form-data';
    replace.style.display = 'grid';
    replace.style.gridTemplateColumns = '1fr 160px';
    replace.style.gap = '8px';
    replace.style.marginTop = '12px';
    replace.append(hiddenCsrf());
    replace.append(
      field('Название турнира', textInput('tournament_name', item.tournament_name || '')),
      field('Дата турнира', textInput('tournament_date', item.tournament_date || '', 'date')),
    );
    const file = document.createElement('input');
    file.type = 'file';
    file.name = 'rating_file';
    file.accept = '.xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet';
    file.required = true;
    const fileLabel = field('Новый полный Excel этого турнира', file);
    fileLabel.style.gridColumn = '1 / -1';
    replace.append(fileLabel);
    const replaceButton = document.createElement('button');
    replaceButton.type = 'submit';
    replaceButton.className = 'primary';
    replaceButton.textContent = 'Полностью заменить турнир';
    replaceButton.style.gridColumn = '1 / -1';
    replace.append(replaceButton);
    replace.addEventListener('submit', (event) => {
      if (!window.confirm('Все старые строки этого турнира будут заменены новым Excel. Продолжить?')) event.preventDefault();
    });

    const manualTitle = document.createElement('strong');
    manualTitle.textContent = 'Ручная правка одного игрока';
    manualTitle.style.display = 'block';
    manualTitle.style.margin = '14px 0 8px';
    details.append(replace, manualTitle, manualForm(`/api/master/hijack-rating/${item.id}/entry`));
    article.append(details);
  }

  fetch('/api/master/hijack-rating/manage', {
    credentials: 'same-origin',
    headers: { Accept: 'application/json' },
  }).then((response) => response.ok ? response.json() : null).then((state) => {
    if (!state) return;
    const grid = document.querySelector('.hj-rating-admin-grid');
    if (grid && !document.querySelector('[data-hijack-baseline-card]')) {
      const card = baselineCard(state.baseline);
      card.dataset.hijackBaselineCard = '1';
      grid.insertAdjacentElement('beforebegin', card);
    }

    const byId = new Map((state.imports || []).map((item) => [String(item.id), item]));
    document.querySelectorAll('.hj-rating-import-row').forEach((article) => {
      if (article.dataset.ratingEditor === '1') return;
      const form = article.querySelector('form[action*="/api/master/hijack-rating/"][action$="/delete"]');
      const match = form?.action.match(/\/api\/master\/hijack-rating\/(\d+)\/delete$/);
      const item = match ? byId.get(match[1]) : null;
      if (!item) return;
      article.dataset.ratingEditor = '1';
      tournamentEditor(item, article);
    });
  }).catch(() => {});
})();
