(() => {
  const ensureHotfixStyle = () => {
    if (!document.querySelector('link[data-prelaunch-ui-hotfix]')) {
      const link = document.createElement('link');
      link.rel = 'stylesheet';
      link.href = '/static/css/prelaunch-ui-hotfix.css?v=1';
      link.dataset.prelaunchUiHotfix = 'true';
      document.head.appendChild(link);
    }
    if (!document.querySelector('link[data-admin-quiz-ux]')) {
      const link = document.createElement('link');
      link.rel = 'stylesheet';
      link.href = '/static/css/admin-quiz-ux.css?v=1';
      link.dataset.adminQuizUx = 'true';
      document.head.appendChild(link);
    }
    if (!document.querySelector('script[data-admin-quiz-ux]')) {
      const script = document.createElement('script');
      script.src = '/static/js/admin-quiz-ux.js?v=1';
      script.defer = true;
      script.dataset.adminQuizUx = 'true';
      document.head.appendChild(script);
    }
  };

  const toast = (message, kind = 'success') => {
    let node = document.querySelector('[data-admin-toast]');
    if (!node) {
      node = document.createElement('div');
      node.dataset.adminToast = 'true';
      node.className = 'prelaunch-admin-toast';
      node.setAttribute('role', 'status');
      node.setAttribute('aria-live', 'polite');
      document.body.appendChild(node);
    }
    node.classList.remove('success', 'error', 'show');
    node.classList.add(kind);
    node.textContent = message;
    requestAnimationFrame(() => node.classList.add('show'));
    window.clearTimeout(node._hideTimer);
    node._hideTimer = window.setTimeout(() => node.classList.remove('show'), 2600);
  };

  window.HJAdminToast = toast;

  const hideLabel = (form, name) => {
    const input = form.querySelector(`[name="${name}"]`);
    const label = input?.closest('label');
    if (label) label.hidden = true;
  };

  const compactDailyCampaign = (form) => {
    const economy = form.querySelector('.campaign-jackcoin-fields');
    if (!economy) return;

    [
      'bonus_preference_code',
      'bonus_amount',
      'reward_delivery_mode',
      'pass_score',
      'quiz_time_limit_seconds',
      'max_attempts',
      'verification_required',
      'jackcoin_per_correct',
      'jackcoin_completion_bonus',
      'jackcoin_perfect_bonus',
      'reward_validity_mode',
      'reward_validity_value',
      'reward_valid_from',
      'reward_valid_until',
      'active_from',
      'active_until',
    ].forEach((name) => hideLabel(form, name));

    const referral = form.querySelector('.campaign-referral-fields');
    if (referral) referral.hidden = true;

    const legend = economy.querySelector('legend');
    if (legend) legend.textContent = 'Параметры выпуска';

    economy.querySelectorAll('.central-economy-note, p.muted').forEach((node) => { node.hidden = true; });

    const finalPrize = economy.querySelector('[data-final-prize-settings]');
    const prizeType = finalPrize?.querySelector('[name="final_prize_type"]');
    const prizeTypeLabel = prizeType?.closest('label');
    if (prizeTypeLabel) {
      const textNode = [...prizeTypeLabel.childNodes].find((node) => node.nodeType === Node.TEXT_NODE);
      if (textNode) textNode.textContent = 'Дополнительный приз выпуска';
    }

    if (!form.querySelector('.jackside-central-note')) {
      const note = document.createElement('div');
      note.className = 'jackside-central-note';
      note.innerHTML = '<strong>JACKSIDE 4:14</strong><span>Экономика, рефералы, одна попытка и общий таймер управляются централизованно.</span><div><a href="/master/jackside">Выпуски</a><a href="/master/economy">Экономика JC</a></div>';
      const firstLabel = form.querySelector('label');
      if (firstLabel) firstLabel.insertAdjacentElement('afterend', note);
      else form.prepend(note);
    }

    form.classList.add('jackside-compact-edit');
  };

  const responseMessage = async (response, fallback) => {
    const type = response.headers.get('content-type') || '';
    if (!type.includes('application/json')) return fallback;
    try {
      const payload = await response.clone().json();
      return payload.message || payload.detail || payload.error || fallback;
    } catch (_) {
      return fallback;
    }
  };

  const refreshFragment = async (response, selector, scrollX, scrollY) => {
    const html = await response.clone().text();
    if (!html) return false;
    const parsed = new DOMParser().parseFromString(html, 'text/html');
    const next = parsed.querySelector(selector);
    const current = document.querySelector(selector);
    if (!next || !current) return false;
    current.replaceWith(next);
    requestAnimationFrame(() => window.scrollTo(scrollX, scrollY));
    return true;
  };

  const releaseConflicts = async (form) => {
    const issueDate = form.querySelector('[name="issue_date"]')?.value || '';
    if (!issueDate) return [];
    const params = new URLSearchParams({ issue_date: issueDate });
    const issueId = form.querySelector('[data-edit-issue-id]')?.value || '';
    if (issueId) params.set('exclude_issue_id', issueId);
    const response = await fetch(`/api/master/jackside/date-conflicts?${params.toString()}`, {
      headers: { Accept: 'application/json' },
      credentials: 'same-origin',
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const payload = await response.json();
    return Array.isArray(payload.issues) ? payload.issues : [];
  };

  const installReleaseSubmitGuard = (form) => {
    if (!form || form.dataset.releaseSubmitGuard === 'true') return;
    form.dataset.releaseSubmitGuard = 'true';
    form.dataset.sameDayGuard = 'true';
    const confirmed = form.querySelector('[data-same-day-confirm]');
    const dateInput = form.querySelector('[name="issue_date"]');
    dateInput?.addEventListener('change', () => {
      if (confirmed) confirmed.value = '0';
      form.dataset.releaseReady = '0';
    });

    form.addEventListener('submit', async (event) => {
      if (form.dataset.releaseReady === '1') {
        if (form.dataset.releaseSubmitting === '1') {
          event.preventDefault();
          return;
        }
        form.dataset.releaseSubmitting = '1';
        const button = event.submitter || form.querySelector('button[type="submit"]');
        if (button) {
          button.disabled = true;
          button.dataset.originalText = button.textContent || '';
          button.textContent = form.matches('[data-edit-release-form]') ? 'Сохраняю…' : 'Создаю…';
        }
        return;
      }

      event.preventDefault();
      if (form.dataset.releaseChecking === '1') return;
      form.dataset.releaseChecking = '1';
      const submitter = event.submitter || form.querySelector('button[type="submit"]');
      try {
        const conflicts = await releaseConflicts(form);
        let sameDayConfirmed = false;
        if (conflicts.length) {
          const rows = conflicts.map((item) => {
            const time = String(item.starts_at_local || '').slice(11, 16) || 'без времени';
            return `• ${time} — ${item.title || 'JACKSIDE'} (${item.status || '—'})`;
          });
          const question = [
            'На эту дату уже есть JACKSIDE:',
            '',
            ...rows,
            '',
            'Создать или перенести ещё один выпуск на эту дату?',
          ].join('\n');
          if (!window.confirm(question)) return;
          sameDayConfirmed = true;
        }
        if (confirmed) confirmed.value = sameDayConfirmed ? '1' : '0';
        form.dataset.releaseReady = '1';
        form.requestSubmit(submitter || undefined);
      } catch (error) {
        toast(`Не удалось проверить расписание: ${error.message}`, 'error');
      } finally {
        form.dataset.releaseChecking = '0';
      }
    });
  };

  const installReleaseSubmitGuards = () => {
    document.querySelectorAll('[data-release-form], [data-edit-release-form]').forEach(installReleaseSubmitGuard);
  };

  const installReloadFreeSave = (form, options = {}) => {
    if (form.dataset.ajaxSaveInstalled === 'true') return;
    form.dataset.ajaxSaveInstalled = 'true';

    form.addEventListener('submit', async (event) => {
      if (event.defaultPrevented) return;
      event.preventDefault();

      const submit = event.submitter || form.querySelector('button[type="submit"], input[type="submit"]');
      const originalText = submit?.textContent || submit?.value || '';
      const scrollX = window.scrollX;
      const scrollY = window.scrollY;
      if (submit) {
        submit.disabled = true;
        if (submit.tagName === 'INPUT') submit.value = 'Сохраняю…';
        else submit.textContent = 'Сохраняю…';
      }

      try {
        const response = await fetch(form.action, {
          method: (form.method || 'POST').toUpperCase(),
          body: new FormData(form),
          credentials: 'same-origin',
          redirect: 'follow',
          headers: { 'X-Requested-With': 'XMLHttpRequest' },
        });
        const finalUrl = new URL(response.url || location.href, location.href);
        const error = finalUrl.searchParams.get('error');
        if (!response.ok || error) {
          throw new Error(error || await responseMessage(response, `Ошибка ${response.status}`));
        }

        if (finalUrl.origin === location.origin && finalUrl.pathname !== location.pathname) {
          window.location.assign(finalUrl.href);
          return;
        }

        const refreshSelector = options.refreshSelector || form.dataset.ajaxRefresh || '';
        if (refreshSelector) {
          const refreshed = await refreshFragment(response, refreshSelector, scrollX, scrollY);
          if (refreshed) run();
        }

        if (options.resetOnSuccess || form.dataset.ajaxReset === 'true') form.reset();
        toast(finalUrl.searchParams.get('ok') || await responseMessage(response, 'Изменения сохранены'));
      } catch (error) {
        toast(error?.message || 'Не удалось сохранить изменения', 'error');
      } finally {
        if (submit && document.contains(submit)) {
          submit.disabled = false;
          if (submit.tagName === 'INPUT') submit.value = originalText || 'Сохранить';
          else submit.textContent = originalText || 'Сохранить';
        }
      }
    });
  };

  const isGenericReloadFreeCandidate = (form) => {
    if (!(form instanceof HTMLFormElement)) return false;
    if ((form.method || 'get').toLowerCase() !== 'post') return false;
    if (form.dataset.noAjax === 'true' || form.dataset.fullNavigation === 'true') return false;
    if (form.matches('#quick-question-form, #bulk-question-form, [data-existing-question-form]')) return false;
    if (form.matches('form.campaign-create, [data-release-form], [data-edit-release-form]')) return false;
    if (form.querySelector('input[type="file"]')) return false;
    if ((form.enctype || '').toLowerCase().includes('multipart/form-data')) return false;
    if (form.target && form.target !== '_self') return false;

    const action = new URL(form.action || location.href, location.href);
    if (action.origin !== location.origin) return false;
    if (['/login', '/logout'].includes(action.pathname)) return false;
    if (action.pathname.startsWith('/clients/import')) return false;
    return true;
  };

  const installGenericReloadFreeActions = () => {
    if (!document.querySelector('.admin-current-user')) return;
    document.querySelectorAll('form').forEach((form) => {
      if (!isGenericReloadFreeCandidate(form)) return;
      let refreshSelector = '';
      let resetOnSuccess = false;
      if (location.pathname === '/master/club-links') {
        refreshSelector = '.prelaunch-page';
        resetOnSuccess = form.action.endsWith('/create');
      } else if (location.pathname === '/master/referrals') {
        refreshSelector = '.prelaunch-page';
      }
      installReloadFreeSave(form, { refreshSelector, resetOnSuccess });
    });
  };

  const centralizeCampaignEconomy = () => {
    if (location.pathname !== '/master') return;
    ensureHotfixStyle();

    const createForm = document.querySelector('form.campaign-create');
    const typeSelect = createForm?.querySelector('select[name="campaign_type"]');
    const dailyOption = typeSelect?.querySelector('option[value="daily_414"]');
    if (dailyOption) {
      dailyOption.remove();
      if (!createForm.querySelector('.jackside-create-route-note')) {
        const note = document.createElement('p');
        note.className = 'muted jackside-create-route-note';
        note.innerHTML = 'JACKSIDE создаётся в отдельном разделе <a href="/master/jackside">JACKSIDE</a>. Здесь находятся только обычные квизы.';
        createForm.prepend(note);
      }
    }

    document.querySelectorAll('.campaign-jackcoin-fields').forEach((fieldset) => {
      ['jackcoin_per_correct', 'jackcoin_completion_bonus', 'jackcoin_perfect_bonus'].forEach((name) => {
        const input = fieldset.querySelector(`[name="${name}"]`);
        const label = input?.closest('label');
        if (label) label.hidden = true;
      });
      if (!fieldset.querySelector('.central-economy-note')) {
        const note = document.createElement('p');
        note.className = 'muted central-economy-note';
        note.innerHTML = 'Базовые начисления задаются централизованно в <a href="/master/economy">Экономике JACKCOIN</a>.';
        const legend = fieldset.querySelector('legend');
        if (legend) legend.insertAdjacentElement('afterend', note);
        else fieldset.prepend(note);
      }
    });

    document.querySelectorAll('form.campaign-edit').forEach((form) => {
      compactDailyCampaign(form);
      installReloadFreeSave(form);
    });
  };

  const keepQuizManagerScoped = () => {
    if (!location.pathname.startsWith('/master/quiz-builder/')) return;
    const builder = document.querySelector('[data-quiz-builder]');
    const isDaily = builder?.dataset.campaignType === 'daily_414';
    if (document.body.dataset.adminAccessRole === 'quiz_manager') {
      document.querySelectorAll('a[href="/master?tab=campaigns"]').forEach((link) => {
        link.href = '/staff/quizzes';
        if (link.textContent.trim() === 'Настройки') link.textContent = 'К списку квизов';
      });
      return;
    }
    document.querySelectorAll('a[href="/master?tab=campaigns"]').forEach((link) => {
      link.href = isDaily ? '/master/jackside' : '/master?tab=campaigns';
      if (link.classList.contains('back')) {
        link.textContent = isDaily ? '← JACKSIDE' : '← Обычные квизы';
      } else if (link.textContent.trim() === 'Настройки') {
        link.textContent = isDaily ? 'К выпускам' : 'Настройки квиза';
      }
    });
  };

  const installQuizExportAction = () => {
    const builder = document.querySelector('[data-quiz-builder]');
    if (!builder || !location.pathname.startsWith('/master/quiz-builder/')) return;
    const actions = builder.querySelector('.hj-hero-actions');
    const campaignId = builder.dataset.campaignId;
    if (!actions || !campaignId || actions.querySelector('[data-quiz-export]')) return;

    const link = document.createElement('a');
    link.className = 'button';
    link.dataset.quizExport = 'true';
    link.href = `/api/master/quiz-campaigns/${encodeURIComponent(campaignId)}/export.zip`;
    link.textContent = 'Экспортировать ZIP';
    actions.appendChild(link);
  };

  const run = () => {
    ensureHotfixStyle();
    installReleaseSubmitGuards();
    centralizeCampaignEconomy();
    keepQuizManagerScoped();
    installQuizExportAction();
    installGenericReloadFreeActions();
  };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', run, { once: true });
  else run();
})();