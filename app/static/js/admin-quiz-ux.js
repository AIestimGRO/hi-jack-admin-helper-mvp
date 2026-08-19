(() => {
  if (window.HJAdminQuizUxInstalled) return;
  window.HJAdminQuizUxInstalled = true;

  const qs = (selector, root = document) => root.querySelector(selector);
  const qsa = (selector, root = document) => [...root.querySelectorAll(selector)];
  const toast = (message, kind = 'success') => window.HJAdminToast?.(message, kind);

  const statusLabels = {
    draft: 'Черновик',
    scheduled: 'Запланирован',
    lobby: 'Лобби',
    main_live: 'Идёт игра',
    waiting_final: 'Ждём финал',
    final_live: 'Финальный стол',
    closed: 'Завершён',
    cancelled: 'Отменён',
    technical_review: 'Техпроверка',
  };

  const installQuizLibraryNav = () => {
    const nav = qs('.admin-persistent-nav nav');
    if (!nav || qs('[data-quiz-library-nav]', nav)) return;
    const jackside = qsa('a', nav).find((link) => new URL(link.href, location.href).pathname === '/master/jackside');
    if (!jackside) return;
    const link = document.createElement('a');
    const params = new URLSearchParams(location.search);
    const active = location.pathname === '/master' && params.get('tab') === 'campaigns';
    link.className = active ? 'active' : '';
    link.dataset.quizLibraryNav = 'true';
    link.href = '/master?tab=campaigns';
    link.innerHTML = '<span>?</span><div><strong>Обычные квизы</strong><small>Промо, тематические и разовые квизы</small></div>';
    jackside.insertAdjacentElement('afterend', link);
  };

  const polishQuizLibrary = () => {
    if (location.pathname !== '/master') return;
    const params = new URLSearchParams(location.search);
    if (params.get('tab') !== 'campaigns') return;
    document.body.classList.add('admin-quiz-library');
    const panel = qs('[data-master-panel="campaigns"]');
    if (!panel || qs('.quiz-library-hero', panel)) return;

    const firstHead = qs('.section-head', panel);
    if (firstHead) {
      const h2 = qs('h2', firstHead);
      if (h2) h2.textContent = 'Новый обычный квиз';
    }

    const createForm = qs('form.campaign-create', panel);
    if (createForm) {
      const type = qs('[name="campaign_type"]', createForm);
      if (type) type.value = 'classic';
      const typeLabel = type?.closest('label');
      if (typeLabel) typeLabel.hidden = true;
    }

    const hero = document.createElement('section');
    hero.className = 'quiz-library-hero';
    hero.innerHTML = `
      <div>
        <p class="eyebrow">Игра · отдельный раздел</p>
        <h1>Обычные квизы</h1>
        <p class="muted">Разовые, тематические и промо-квизы. JACKSIDE 4:14 управляется отдельно и больше не смешивается с ними.</p>
      </div>
      <div class="quiz-library-actions">
        <a class="button" href="/master/jackside">JACKSIDE 4:14</a>
        <a class="button" href="/master/reports">Результаты</a>
      </div>`;
    panel.prepend(hero);

    const classicTab = qs('[data-campaign-tab="classic"]', panel);
    classicTab?.click();
    qsa('[data-campaign-kind="daily_414"]', panel).forEach((node) => { node.hidden = true; });
  };

  const normalizeBuilderCopy = () => {
    const builder = qs('[data-quiz-builder]');
    if (!builder || builder.dataset.campaignType !== 'daily_414') return;

    qsa('.hj-quiz-summary span', builder).forEach((item) => {
      if (item.textContent.includes('Основной раунд')) {
        const strong = qs('strong', item);
        if (strong) strong.textContent = strong.textContent.replace(/\s*\/\s*10\s*$/, '');
      }
      if (item.textContent.includes('JACKCOIN')) {
        const strong = qs('strong', item);
        if (strong) strong.textContent = strong.textContent.replace('за 10/10', 'за идеальный результат');
      }
    });

    qsa('.alert', builder).forEach((alert) => {
      if (/опубликовано\s+\d+\s+из\s+10\s+вопросов/i.test(alert.textContent)) alert.remove();
    });

    const orderNote = qs('.daily-order-note', builder);
    if (orderNote) {
      orderNote.innerHTML = '<strong>Основной раунд:</strong> количество вопросов задаёт мастер. Общий таймер для всего выпуска всегда 4 минуты 14 секунд. Финальный стол идёт отдельным списком; каждый финальный вопрос имеет собственное время.';
    }

    qsa('select[name="game_round"] option', builder).forEach((option) => {
      if (option.value === 'main') option.textContent = 'Основной раунд';
    });

    const back = qs('a.back', builder);
    if (back) {
      back.href = '/master/jackside';
      back.textContent = '← JACKSIDE';
    }
    qsa('.hj-hero-actions a', builder).forEach((link) => {
      if (link.textContent.trim() === 'Настройки' || link.textContent.trim() === 'К выпускам') {
        link.href = '/master/jackside';
        link.textContent = 'К выпускам';
      }
    });
  };

  const csrfToken = () => qs('[data-release-form] [name="csrf_token"]')?.value || qs('[name="csrf_token"]')?.value || '';

  const setPillStatus = (container, status) => {
    const pill = qs('.type-pill:not(.ia-legacy-pill)', container) || qs('.type-pill', container);
    if (!pill) return;
    pill.dataset.status = status;
    pill.textContent = statusLabels[status] || status;
  };

  const markIssueScheduled = (issueId) => {
    const escaped = CSS.escape(String(issueId));
    qsa(`[data-jackside-publish="${escaped}"], [data-jackside-check="${escaped}"]`).forEach((button) => button.remove());
    qsa(`[data-edit-issue="${escaped}"]`).forEach((marker) => {
      const container = marker.closest('.ia-campaign-card, .ia-release-row');
      if (container) setPillStatus(container, 'scheduled');
    });
  };

  const issueAction = async (issueId, action, button) => {
    if (!issueId || !csrfToken()) return;
    const original = button.textContent;
    button.disabled = true;
    button.textContent = action === 'schedule' ? 'Публикую…' : 'Проверяю…';
    try {
      const body = new FormData();
      body.set('csrf_token', csrfToken());
      const response = await fetch(`/api/master/jackside-issues/${encodeURIComponent(issueId)}/${action}`, {
        method: 'POST',
        body,
        credentials: 'same-origin',
        redirect: 'follow',
      });
      const finalUrl = new URL(response.url || location.href, location.href);
      const error = finalUrl.searchParams.get('error');
      if (!response.ok || error) throw new Error(error || `Ошибка ${response.status}`);
      const message = finalUrl.searchParams.get('ok') || (action === 'schedule' ? 'Выпуск запланирован' : 'Выпуск готов');
      if (action === 'schedule') markIssueScheduled(issueId);
      toast(message);
      if (action !== 'schedule') {
        button.disabled = false;
        button.textContent = original;
      }
    } catch (error) {
      toast(error?.message || 'Не удалось выполнить действие', 'error');
      button.disabled = false;
      button.textContent = original;
    }
  };

  const issueIdFromCard = (card) => {
    const edit = qs('[data-edit-issue]', card)?.dataset.editIssue;
    if (edit) return edit;
    const repeat = qs('[data-repeat-source^="issue:"]', card)?.dataset.repeatSource || '';
    return repeat.startsWith('issue:') ? repeat.slice(6) : '';
  };

  const addDraftActions = (card, actions, status) => {
    if (status !== 'draft' || !actions || qs('[data-jackside-publish]', actions)) return;
    const issueId = issueIdFromCard(card);
    if (!issueId) return;

    const check = document.createElement('button');
    check.type = 'button';
    check.className = 'jackside-check-button';
    check.dataset.jacksideCheck = issueId;
    check.textContent = 'Проверить';
    check.addEventListener('click', () => issueAction(issueId, 'validate', check));

    const publish = document.createElement('button');
    publish.type = 'button';
    publish.className = 'jackside-publish-button';
    publish.dataset.jacksidePublish = issueId;
    publish.textContent = 'Опубликовать и запланировать';
    publish.addEventListener('click', () => issueAction(issueId, 'schedule', publish));

    actions.prepend(publish);
    actions.prepend(check);
  };

  function enhanceJacksideWorkspace() {
    const workspace = qs('[data-jackside-workspace]');
    if (!workspace) return;

    const quiet = qs('.ia-quiet-link[href="/master/jackside-issues"]', workspace);
    if (quiet) quiet.remove();

    qsa('.ia-campaign-card', workspace).forEach((card) => {
      const pill = qs('.type-pill:not(.ia-legacy-pill)', card);
      const raw = pill?.textContent.trim() || '';
      if (pill && statusLabels[raw]) setPillStatus(card, raw);
      const actions = qs('.ia-card-actions', card);
      addDraftActions(card, actions, raw);
      qsa('.ia-card-stats span', card).forEach((stat) => {
        stat.innerHTML = stat.innerHTML.replace('/10', '');
      });
    });

    qsa('.ia-release-row', workspace).forEach((row) => {
      const pill = qs('.type-pill', row);
      const raw = pill?.textContent.trim() || '';
      if (pill && statusLabels[raw]) setPillStatus(row, raw);
      qsa('span', row).forEach((node) => {
        if (/\d+\/10\s*·\s*финал/i.test(node.textContent)) node.textContent = node.textContent.replace('/10', '');
      });
      addDraftActions(row, qs('.ia-row-actions', row), raw);
    });

    if (!qs('.jackside-ops-bar', workspace)) {
      const head = qs('.ia-page-head', workspace);
      const bar = document.createElement('section');
      bar.className = 'jackside-ops-bar';
      bar.innerHTML = `
        <div>
          <p class="eyebrow">Рабочий процесс</p>
          <h2>Создал → добавил вопросы → опубликовал</h2>
          <p class="muted">Черновики не видны игрокам. Когда вопросы готовы, нажмите «Опубликовать и запланировать» прямо на карточке выпуска.</p>
        </div>
        <div class="jackside-release-actions"><a class="button" href="/master?tab=campaigns">Обычные квизы</a><a class="button" href="/master/reports">Результаты</a></div>`;
      head?.insertAdjacentElement('afterend', bar);
    }
  }

  const redirectLegacyIssueScreen = () => {
    if (location.pathname !== '/master/jackside-issues') return false;
    const params = new URLSearchParams(location.search);
    const target = new URL('/master/jackside', location.origin);
    if (params.get('ok')) target.searchParams.set('ok', params.get('ok'));
    if (params.get('error')) target.searchParams.set('error', params.get('error'));
    location.replace(target.href);
    return true;
  };

  if (redirectLegacyIssueScreen()) return;
  installQuizLibraryNav();
  polishQuizLibrary();
  normalizeBuilderCopy();
  enhanceJacksideWorkspace();
})();