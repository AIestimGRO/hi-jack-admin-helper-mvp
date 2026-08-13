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
    if (selected && new URL(location.href).searchParams.get('source')) openReleaseDialog(selected);
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
        window.HJAdminToast?.('Настройки квалифицированных рефералов сохранены');
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
        <div class="section-head"><div><p class="eyebrow">Реферальная экономика</p><h2>Квалифицированный реферал</h2></div></div>
        <p class="muted">Квалификация: 3 завершённых выпуска JACKSIDE в 3 разные календарные даты. Здесь задаётся материальная награда; JC-суммы уровней сети находятся выше в общей экономике.</p>
        <form method="post" action="/api/master/jackside-referrals/settings">
          <input type="hidden" name="csrf_token" value="${payload.csrf_token || ''}">
          <div class="ia-referral-grid"></div>
          <button class="primary" type="submit">Сохранить квалификацию</button>
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

  const run = () => {
    installReleaseDialog();
    fixLegacyIssueSelect();
    addLegacyCopyToCampaignCards();
    injectReferralQualificationIntoEconomy();
    simplifyEngagementPage();
  };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', run, { once: true });
  else run();
})();
