(() => {
  const dialog = document.querySelector('[data-winner-prize-dialog]');
  const form = dialog?.querySelector('[data-winner-prize-form]');
  const select = dialog?.querySelector('[data-winner-prize-select]');
  const title = dialog?.querySelector('[data-winner-prize-title]');
  const status = dialog?.querySelector('[data-winner-prize-status]');
  if (!dialog || !form || !select) return;

  const showToast = (message, type = 'success') => {
    if (window.HJAdminToast) window.HJAdminToast(message, type);
  };

  const closeDialog = () => {
    if (typeof dialog.close === 'function') dialog.close();
    else dialog.removeAttribute('open');
  };

  const setButtonState = (issueId, rewardTitle) => {
    document.querySelectorAll(`[data-winner-prize-issue="${issueId}"]`).forEach((button) => {
      button.textContent = rewardTitle ? 'Приз ✓' : 'Приз';
      button.title = rewardTitle
        ? `Дополнительный приз: ${rewardTitle}`
        : 'Дополнительный приз победителю';
    });
  };

  const loadPrize = async (issueId, button) => {
    button.disabled = true;
    try {
      const response = await fetch(`/api/master/jackside/issues/${issueId}/winner-prize`, {
        headers: { Accept: 'application/json' },
        credentials: 'same-origin',
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok || !payload.ok) {
        throw new Error(payload.message || `Ошибка ${response.status}`);
      }
      const issue = payload.issue || {};
      if (!issue.editable) {
        showToast('Выпуск уже стартовал — дополнительный приз зафиксирован', 'error');
        return;
      }

      form.action = `/api/master/jackside/issues/${issueId}/winner-prize`;
      form.dataset.issueId = String(issueId);
      const csrf = form.querySelector('[name="csrf_token"]');
      if (csrf && payload.csrf_token) csrf.value = payload.csrf_token;
      if (title) title.textContent = issue.title || 'JACKSIDE';
      if (status) status.textContent = '';

      select.replaceChildren(new Option('Без дополнительного приза', '0'));
      if (issue.final_prize_type === 'jackcoin') {
        select.append(new Option(
          `Текущий приз: ${Number(issue.final_prize_jackcoin_amount || 0)} JC — оставить без изменений`,
          '-1'
        ));
      }
      (issue.rewards || []).forEach((reward) => {
        const days = Number(reward.validity_days || 0);
        const validity = days > 0 ? ` · ${days} дн.` : ' · без срока';
        const market = reward.is_active ? '' : ' · не продаётся';
        select.append(new Option(
          `${reward.title}${validity}${market}`,
          String(reward.id)
        ));
      });

      if (issue.final_prize_type === 'reward_card' && issue.final_prize_catalog_reward_id) {
        select.value = String(issue.final_prize_catalog_reward_id);
      } else if (issue.final_prize_type === 'jackcoin') {
        select.value = '-1';
      } else {
        select.value = '0';
      }
      setButtonState(issueId, issue.final_prize_type === 'reward_card' ? issue.final_prize_title : null);

      if (typeof dialog.showModal === 'function') dialog.showModal();
      else dialog.setAttribute('open', '');
    } catch (error) {
      showToast(error.message || 'Не удалось открыть настройки приза', 'error');
    } finally {
      button.disabled = false;
    }
  };

  document.querySelectorAll('[data-winner-prize-issue]').forEach((button) => {
    button.addEventListener('click', () => {
      const issueId = button.dataset.winnerPrizeIssue || '';
      if (issueId) loadPrize(issueId, button);
    });
  });

  dialog.querySelectorAll('[data-close-winner-prize]').forEach((button) => {
    button.addEventListener('click', closeDialog);
  });
  dialog.addEventListener('click', (event) => {
    if (event.target === dialog) closeDialog();
  });

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const issueId = form.dataset.issueId || '';
    const submit = form.querySelector('button[type="submit"]');
    const original = submit?.textContent || 'Сохранить';
    if (submit) {
      submit.disabled = true;
      submit.textContent = 'Сохраняю…';
    }
    if (status) status.textContent = '';
    try {
      const response = await fetch(form.action, {
        method: 'POST',
        body: new FormData(form),
        credentials: 'same-origin',
        headers: {
          Accept: 'application/json',
          'X-Requested-With': 'XMLHttpRequest',
        },
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok || !payload.ok) {
        throw new Error(payload.message || `Ошибка ${response.status}`);
      }
      const rewardTitle = payload.final_prize_type === 'reward_card'
        ? payload.reward_title
        : null;
      setButtonState(issueId, rewardTitle);
      closeDialog();
      showToast(rewardTitle
        ? `Дополнительный приз сохранён: ${rewardTitle}`
        : 'Дополнительный приз отключён');
    } catch (error) {
      if (status) status.textContent = error.message || 'Не удалось сохранить приз';
      showToast(error.message || 'Не удалось сохранить приз', 'error');
    } finally {
      if (submit) {
        submit.disabled = false;
        submit.textContent = original;
      }
    }
  });
})();