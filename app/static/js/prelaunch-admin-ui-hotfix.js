(() => {
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
      note.innerHTML = '<strong>JACKSIDE 4:14</strong><span>Экономика, рефералы, 10 вопросов, 1 попытка и общий таймер управляются централизованно.</span><div><a href="/master/jackside-issues">Выпуски</a><a href="/master/economy">Экономика JC</a></div>';
      const firstLabel = form.querySelector('label');
      if (firstLabel) firstLabel.insertAdjacentElement('afterend', note);
      else form.prepend(note);
    }

    form.classList.add('jackside-compact-edit');
  };

  const installReloadFreeSave = (form) => {
    if (form.dataset.ajaxSaveInstalled === 'true') return;
    form.dataset.ajaxSaveInstalled = 'true';

    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const submit = form.querySelector('button[type="submit"]');
      const originalText = submit?.textContent || '';
      if (submit) {
        submit.disabled = true;
        submit.textContent = 'Сохраняю…';
      }

      try {
        const response = await fetch(form.action, {
          method: (form.method || 'POST').toUpperCase(),
          body: new FormData(form),
          credentials: 'same-origin',
          redirect: 'follow',
        });
        const finalUrl = new URL(response.url || location.href, location.href);
        const error = finalUrl.searchParams.get('error');
        if (!response.ok || error) throw new Error(error || `Ошибка ${response.status}`);
        toast(finalUrl.searchParams.get('ok') || 'Изменения сохранены');
      } catch (error) {
        toast(error?.message || 'Не удалось сохранить изменения', 'error');
      } finally {
        if (submit) {
          submit.disabled = false;
          submit.textContent = originalText || 'Сохранить';
        }
      }
    });
  };

  const run = () => {
    if (location.pathname !== '/master') return;
    document.querySelectorAll('form.campaign-edit').forEach((form) => {
      compactDailyCampaign(form);
      installReloadFreeSave(form);
    });
  };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', run, { once: true });
  else run();
})();
