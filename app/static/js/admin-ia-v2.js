(() => {
  const qs = (selector, root = document) => root.querySelector(selector);
  const qsa = (selector, root = document) => [...root.querySelectorAll(selector)];

  const syncReleaseAction = (dialog) => {
    if (!dialog) return;
    const source = qs('[data-release-source]', dialog)?.value || '';
    const submit = qs('[data-release-submit]', dialog);
    const hint = qs('[data-release-hint]', dialog);
    if (submit) submit.textContent = source ? 'Создать и запланировать' : 'Создать черновик';
    if (hint) {
      hint.textContent = source
        ? 'Вопросы будут скопированы, проверены и выпуск сразу станет доступен по расписанию.'
        : 'Пустой выпуск сохраняется черновиком: сначала добавьте вопросы, затем запланируйте его.';
    }
  };

  const fetchDateConflicts = async (issueDate, excludeIssueId = '') => {
    const params = new URLSearchParams({ issue_date: issueDate });
    if (excludeIssueId) params.set('exclude_issue_id', excludeIssueId);
    const response = await fetch(`/api/master/jackside/date-conflicts?${params.toString()}`, {
      headers: { Accept: 'application/json' },
      credentials: 'same-origin',
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const payload = await response.json();
    return Array.isArray(payload.issues) ? payload.issues : [];
  };

  const confirmSameDayIfNeeded = async (form, excludeIssueId = '') => {
    const issueDate = qs('[name="issue_date"]', form)?.value || '';
    const confirmed = qs('[data-same-day-confirm]', form);
    if (!issueDate || confirmed?.value === '1') return true;
    const conflicts = await fetchDateConflicts(issueDate, excludeIssueId);
    if (!conflicts.length) return true;
    const rows = conflicts.map((item) => {
      const time = String(item.starts_at_local || '').slice(11, 16) || 'без времени';
      return `• ${time} — ${item.title || 'JACKSIDE'} (${item.status || '—'})`;
    });
    const message = [
      `На ${issueDate.split('-').reverse().join('.')} уже есть JACKSIDE:`,
      '',
      ...rows,
      '',
      'Создать/перенести ещё один выпуск на эту дату?',
    ].join('\n');
    if (!window.confirm(message)) return false;
    if (confirmed) confirmed.value = '1';
    return true;
  };

  const installSameDayGuard = (form, excludeIssueId = '') => {
    if (!form || form.dataset.sameDayGuard === 'true') return;
    form.dataset.sameDayGuard = 'true';
    const dateInput = qs('[name="issue_date"]', form);
    dateInput?.addEventListener('change', () => {
      const confirmed = qs('[data-same-day-confirm]', form);
      if (confirmed) confirmed.value = '0';
    });
    form.addEventListener('submit', async (event) => {
      const confirmed = qs('[data-same-day-confirm]', form);
      if (confirmed?.value === '1') return;
      event.preventDefault();
      try {
        const allowed = await confirmSameDayIfNeeded(form, excludeIssueId);
        if (allowed) form.requestSubmit();
      } catch (error) {
        window.HJAdminToast?.(`Не удалось проверить расписание: ${error.message}`, 'error');
      }
    });
  };

  const openReleaseDialog = (source = '') => {
    const dialog = qs('[data-release-dialog]');
    if (!dialog) {
      const suffix = source ? `?source=${encodeURIComponent(source)}` : '';
      location.assign(`/master/jackside${suffix}`);
      return;
    }
    const sourceSelect = qs('[data-release-source]', dialog);
    if (sourceSelect && source) sourceSelect.value = source;
    const confirmed = qs('[data-same-day-confirm]', dialog);
    if (confirmed) confirmed.value = '0';
    syncReleaseAction(dialog);
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
    const sourceSelect = qs('[data-release-source]', dialog);
    sourceSelect?.addEventListener('change', () => syncReleaseAction(dialog));
    syncReleaseAction(dialog);
    installSameDayGuard(qs('[data-release-form]', dialog));
    const selected = sourceSelect?.value;
    if (selected && new URL(location.href).searchParams.get('source')) openReleaseDialog(selected);
  };

  const openEditReleaseDialog = async (issueId) => {
    const dialog = qs('[data-edit-release-dialog]');
    const form = qs('[data-edit-release-form]', dialog);
    if (!dialog || !form) return;
    try {
      const response = await fetch(`/api/master/jackside/issues/${issueId}/schedule`, {
        headers: { Accept: 'application/json' },
        credentials: 'same-origin',
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = await response.json();
      const issue = payload.issue || {};
      if (!issue.editable) {
        window.HJAdminToast?.('Старт этого выпуска уже наступил — расписание заблокировано', 'error');
        return;
      }
      form.action = `/api/master/jackside/issues/${issueId}/reschedule`;
      qs('[data-edit-issue-id]', form).value = String(issueId);
      qs('[data-edit-date]', form).value = issue.issue_date || '';
      qs('[data-edit-start]', form).value = issue.starts_at_local || '';
      qs('[data-edit-title]', form).value = issue.title || '';
      const confirmed = qs('[data-same-day-confirm]', form);
      if (confirmed) confirmed.value = '0';
      const note = qs('[data-edit-release-note]', dialog);
      if (note) {
        note.textContent = Number(issue.participants || 0) > 0
          ? `До старта расписание можно изменить. Выпуск уже видели/заняли место: ${Number(issue.participants)} участн. После сохранения им будет показываться новое время.`
          : 'До фактического старта можно изменить дату, время и название. Вопросы, призы и история выпуска не теряются.';
      }
      form.dataset.excludeIssueId = String(issueId);
      if (typeof dialog.showModal === 'function') dialog.showModal();
      else dialog.setAttribute('open', '');
    } catch (error) {
      window.HJAdminToast?.(`Не удалось открыть расписание: ${error.message}`, 'error');
    }
  };

  const installEditReleaseDialog = () => {
    const dialog = qs('[data-edit-release-dialog]');
    const form = qs('[data-edit-release-form]', dialog);
    qsa('[data-edit-issue]').forEach((button) => {
      button.addEventListener('click', () => openEditReleaseDialog(button.dataset.editIssue || ''));
    });
    if (!dialog || !form) return;
    qsa('[data-close-edit-release]', dialog).forEach((button) => {
      button.addEventListener('click', () => dialog.close());
    });
    dialog.addEventListener('click', (event) => {
      if (event.target === dialog) dialog.close();
    });
    if (form.dataset.sameDayGuard !== 'true') {
      form.dataset.sameDayGuard = 'true';
      qs('[name="issue_date"]', form)?.addEventListener('change', () => {
        const confirmed = qs('[data-same-day-confirm]', form);
        if (confirmed) confirmed.value = '0';
      });
      form.addEventListener('submit', async (event) => {
        const confirmed = qs('[data-same-day-confirm]', form);
        if (confirmed?.value === '1') return;
        event.preventDefault();
        try {
          const allowed = await confirmSameDayIfNeeded(form, form.dataset.excludeIssueId || '');
          if (allowed) form.requestSubmit();
        } catch (error) {
          window.HJAdminToast?.(`Не удалось проверить расписание: ${error.message}`, 'error');
        }
      });
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
      // Campaign editing remains usable if this convenience request fails.
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

  const ajaxSaveReferralQualification = (form) => {
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const button = qs('button[type="submit"]', form);
      const original = button?.textContent || 'Сохранить';
      if (button) {
        button.disabled = true;
        button.textContent = 'Сохраняю…';
      }
      try {
        const response = await fetch(form.action, {
          method: 'POST',
          body: new FormData(form),
          credentials: 'same-origin',
          redirect: 'follow',
          headers: { 'X-Requested-With': 'XMLHttpRequest' },
        });
        if (!response.ok) throw new Error(`Ошибка ${response.status}`);
        window.HJAdminToast?.('Настройки дополнительной реферальной награды сохранены');
      } catch (error) {
        window.HJAdminToast?.(error.message || 'Не удалось сохранить настройки', 'error');
      } finally {
        if (button) {
          button.disabled = false;
          button.textContent = original;
        }
      }
    });
  };

  const injectReferralQualificationIntoEconomy = async () => {
    if (location.pathname !== '/master/economy' || qs('[data-referral-economy]')) return;
    const oldQuickIssue = qs('.prelaunch-quick-issue');
    if (oldQuickIssue) oldQuickIssue.hidden = true;
    const anchor = qs('.prelaunch-audit') || oldQuickIssue;
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
        <div class="section-head"><div><p class="eyebrow">Дополнительная механика</p><h2>Активный реферал · 3 дня</h2></div></div>
        <p class="muted">Это отдельная опциональная материальная награда после 3 завершённых JACKSIDE в 3 разные даты. Она не заменяет и не задерживает базовые JC-награды L1/L2/L3 за первый и повторные JACKSIDE — они настраиваются выше.</p>
        <form method="post" action="/api/master/jackside-referrals/settings">
          <input type="hidden" name="csrf_token" value="${payload.csrf_token || ''}">
          <div class="ia-referral-grid"></div>
          <button class="primary" type="submit">Сохранить дополнительную награду</button>
        </form>`;
      const grid = qs('.ia-referral-grid', section);
      const delivery = (name, value) => {
        const select = document.createElement('select');
        select.name = name;
        select.append(new Option('Автоматически', 'automatic'), new Option('Кодом', 'code'));
        select.value = value || 'automatic';
        return select;
      };
      const amount = (name, value) => Object.assign(document.createElement('input'), {
        name, type: 'number', min: '0', max: '1000', value: String(value || 0),
      });
      const fields = [
        ['Награда рефоводу', buildPreferenceSelect('referrer_preference_code', data.referrer_preference_code, preferences)],
        ['Количество рефоводу', amount('referrer_amount', data.referrer_amount)],
        ['Выдача рефоводу', delivery('referrer_delivery_mode', data.referrer_delivery_mode)],
        ['Награда приглашённому', buildPreferenceSelect('invited_preference_code', data.invited_preference_code, preferences)],
        ['Количество приглашённому', amount('invited_amount', data.invited_amount)],
        ['Выдача приглашённому', delivery('invited_delivery_mode', data.invited_delivery_mode)],
      ];
      fields.forEach(([title, input]) => {
        const label = document.createElement('label');
        label.append(document.createTextNode(title), input);
        grid.append(label);
      });
      ajaxSaveReferralQualification(qs('form', section));
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
    const pageHeading = qs('.page-head h1');
    if (pageHeading) pageHeading.textContent = 'Звания и достижения';
    if (!qs('.ia-subsection-tools', panel)) {
      const tools = document.createElement('nav');
      tools.className = 'ia-subsection-tools';
      tools.innerHTML = '<strong>Коллекция игрока</strong><a class="button" href="/master/engagement-icons">Иконки коллекции</a><a href="/master/economy">Реферальная экономика →</a>';
      panel.prepend(tools);
    }
  };

  const simplifyJacksideBuilder = () => {
    const builder = qs('[data-quiz-builder][data-campaign-type="daily_414"]');
    if (!builder) return;
    const publishForm = qs('form[action$="/publish-version"]', builder);
    if (publishForm) publishForm.remove();
    const back = qs('.back', builder);
    if (back) {
      back.href = '/master/jackside';
      back.textContent = '← JACKSIDE';
    }
    qsa('.hj-create-section .hj-section-heading span', builder).forEach((node) => {
      if ((node.textContent || '').includes('Новая версия')) {
        node.textContent = 'Изменения относятся к этому выпуску JACKSIDE';
      }
    });
    qsa('.hj-quiz-summary > span', builder).forEach((node) => {
      if ((node.textContent || '').trim().startsWith('Версия')) node.hidden = true;
    });
  };

  const run = () => {
    installReleaseDialog();
    installEditReleaseDialog();
    fixLegacyIssueSelect();
    addLegacyCopyToCampaignCards();
    injectReferralQualificationIntoEconomy();
    simplifyEngagementPage();
    simplifyJacksideBuilder();
  };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', run, { once: true });
  else run();
})();
