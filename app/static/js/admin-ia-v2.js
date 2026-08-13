(() => {
  const qs = (selector, root = document) => root.querySelector(selector);
  const qsa = (selector, root = document) => [...root.querySelectorAll(selector)];

  const openReleaseDialog = (source = '') => {
    const dialog = qs('[data-release-dialog]');
    if (!dialog) {
      const suffix = source ? `?source=${encodeURIComponent(source)}` : '';
      location.assign(`/master/jackside${suffix}`);
      return;
    }
    const sourceSelect = qs('[data-release-source]', dialog);
    if (sourceSelect && source) sourceSelect.value = source;
    if (typeof dialog.showModal === 'function') dialog.showModal();
    else dialog.setAttribute('open', '');
  };

  const installReleaseDialog = () => {
    const dialog = qs('[data-release-dialog]');
    qsa('[data-open-release]').forEach((button) => {
      button.addEventListener('click', () => openReleaseDialog());
    });
    qsa('[data-repeat-source]').forEach((button) => {
      button.addEventListener('click', () => openReleaseDialog(button.dataset.repeatSource || ''));
    });
    if (!dialog) return;
    qsa('[data-close-release]', dialog).forEach((button) => {
      button.addEventListener('click', () => dialog.close());
    });
    dialog.addEventListener('click', (event) => {
      if (event.target === dialog) dialog.close();
    });
    const selected = qs('[data-release-source]', dialog)?.value;
    if (selected && new URL(location.href).searchParams.get('source')) {
      openReleaseDialog(selected);
    }
  };

  const fetchLegacySources = async () => {
    const response = await fetch('/api/master/jackside-issues/legacy-sources', {
      headers: { Accept: 'application/json' },
      credentials: 'same-origin',
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const payload = await response.json();
    return Array.isArray(payload.sources) ? payload.sources : [];
  };

  const fixLegacyIssueSelect = async () => {
    const select = qs('#legacy-jackside-source');
    const button = qs('#legacy-jackside-copy-button');
    const status = qs('#legacy-jackside-source-status');
    if (!select || !button) return;
    try {
      const sources = await fetchLegacySources();
      select.replaceChildren();
      if (!sources.length) {
        select.append(new Option('Старых JACKSIDE-квизов не найдено', ''));
        select.disabled = true;
        button.disabled = true;
        if (status) status.textContent = 'Старые daily_414-квизы с вопросами не найдены.';
        return;
      }
      select.append(new Option('Выберите старый квиз', ''));
      sources.forEach((item) => {
        const label = `${item.title} · ${Number(item.main_question_count || 0)} осн. · финал ${Number(item.final_question_count || 0)} · прохождений ${Number(item.submission_count || 0)}`;
        select.append(new Option(label, String(item.id)));
      });
      select.disabled = false;
      button.disabled = false;
      if (status) status.textContent = `Найдено старых квизов: ${sources.length}.`;
    } catch (error) {
      select.replaceChildren(new Option('Не удалось загрузить список', ''));
      select.disabled = true;
      button.disabled = true;
      if (status) status.textContent = `Ошибка загрузки: ${error.message}`;
    }
  };

  const addLegacyCopyToCampaignCards = async () => {
    if (location.pathname !== '/master') return;
    const cards = qsa('[data-campaign-kind="daily_414"]');
    if (!cards.length) return;
    try {
      const sources = await fetchLegacySources();
      const byCode = new Map(sources.map((item) => [String(item.code), item]));
      cards.forEach((card) => {
        const code = qs('code', card)?.textContent?.trim() || '';
        const source = byCode.get(code);
        if (!source || qs('[data-legacy-card-copy]', card)) return;
        const actions = qs('.campaign-actions', card) || card;
        const link = document.createElement('a');
        link.className = 'button ia-card-copy-link';
        link.dataset.legacyCardCopy = 'true';
        link.href = `/master/jackside?source=${encodeURIComponent(`legacy:${source.id}`)}`;
        link.textContent = 'Новый выпуск из этого квиза';
        actions.prepend(link);
      });
    } catch (_) {
      // Old campaign editing remains fully usable when the helper request fails.
    }
  };

  const buildPreferenceSelect = (name, value, preferences) => {
    const select = document.createElement('select');
    select.name = name;
    select.append(new Option('Без материальной награды', ''));
    preferences.forEach((item) => select.append(new Option(item.title, item.code)));
    select.value = value || '';
    return select;
  };

  const injectReferralQualificationIntoEconomy = async () => {
    if (location.pathname !== '/master/economy' || qs('[data-referral-economy]')) return;
    const anchor = qs('.prelaunch-audit') || qs('.prelaunch-quick-issue');
    if (!anchor) return;
    try {
      const response = await fetch('/api/master/referral-qualification-settings', {
        headers: { Accept: 'application/json' },
        credentials: 'same-origin',
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = await response.json();
      const data = payload.settings || {};
      const preferences = Array.isArray(payload.preferences) ? payload.preferences : [];
      const section = document.createElement('section');
      section.className = 'card ia-referral-economy';
      section.dataset.referralEconomy = 'true';
      section.innerHTML = `
        <div class="section-head"><div><p class="eyebrow">Реферальная экономика</p><h2>Квалифицированный реферал</h2></div></div>
        <p class="muted">Квалификация: 3 завершённых выпуска JACKSIDE в 3 разные календарные даты. Здесь задаётся материальная награда; JC-суммы уровней сети находятся выше в общей экономике.</p>
        <form method="post" action="/api/master/jackside-referrals/settings">
          <input type="hidden" name="csrf_token" value="${payload.csrf_token || ''}">
          <div class="ia-referral-grid"></div>
          <button class="primary" type="submit">Сохранить квалификацию</button>
        </form>`;
      const grid = qs('.ia-referral-grid', section);
      const fields = [
        ['Награда рефоводу', buildPreferenceSelect('referrer_preference_code', data.referrer_preference_code, preferences)],
        ['Количество рефоводу', Object.assign(document.createElement('input'), { name: 'referrer_amount', type: 'number', min: '0', max: '1000', value: String(data.referrer_amount || 0) })],
        ['Выдача рефоводу', (() => { const s = document.createElement('select'); s.name='referrer_delivery_mode'; s.append(new Option('Автоматически','automatic'),new Option('Кодом','code')); s.value=data.referrer_delivery_mode || 'automatic'; return s; })()],
        ['Награда приглашённому', buildPreferenceSelect('invited_preference_code', data.invited_preference_code, preferences)],
        ['Количество приглашённому', Object.assign(document.createElement('input'), { name: 'invited_amount', type: 'number', min: '0', max: '1000', value: String(data.invited_amount || 0) })],
        ['Выдача приглашённому', (() => { const s = document.createElement('select'); s.name='invited_delivery_mode'; s.append(new Option('Автоматически','automatic'),new Option('Кодом','code')); s.value=data.invited_delivery_mode || 'automatic'; return s; })()],
      ];
      fields.forEach(([title, input]) => {
        const label = document.createElement('label');
        label.append(document.createTextNode(title), input);
        grid.append(label);
      });
      anchor.before(section);
    } catch (_) {
      // Economy remains usable even if the secondary settings block cannot load.
    }
  };

  const simplifyEngagementPage = () => {
    if (location.pathname !== '/master' || new URL(location.href).searchParams.get('tab') !== 'engagement') return;
    const panel = qs('[data-master-panel="engagement"]');
    if (!panel) return;
    const referralForm = qs('form[action="/api/master/jackside-referrals/settings"]', panel);
    const heading = referralForm?.previousElementSibling;
    const note = referralForm?.nextElementSibling;
    if (heading) heading.hidden = true;
    if (referralForm) referralForm.hidden = true;
    if (note && note.matches('p')) note.hidden = true;
    const tabs = qs('.master-tabs');
    if (tabs) tabs.hidden = true;
    if (!qs('.ia-subsection-tools', panel)) {
      const tools = document.createElement('nav');
      tools.className = 'ia-subsection-tools';
      tools.innerHTML = '<strong>Звания и достижения</strong><a class="button" href="/master/engagement-icons">Иконки коллекции</a><a href="/master/economy">Реферальная экономика →</a>';
      panel.prepend(tools);
    }
  };

  const simplifyHijackRating = () => {
    if (location.pathname !== '/master/hijack-rating') return;
    document.body.classList.add('hj-rating-clean');
    qsa('.hj-rating-import-card').forEach((card) => {
      if (card.textContent.includes('Новые условия HI, JACK!')) card.classList.add('hj-rating-conditions-panel');
    });
  };

  const run = () => {
    installReleaseDialog();
    fixLegacyIssueSelect();
    addLegacyCopyToCampaignCards();
    injectReferralQualificationIntoEconomy();
    simplifyEngagementPage();
    simplifyHijackRating();
  };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', run, { once: true });
  else run();
})();
