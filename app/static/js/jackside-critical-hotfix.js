(() => {
  const app = document.getElementById('quiz-app');
  if (!app || app.dataset.campaignType !== 'daily_414') return;

  const campaign = app.dataset.campaign || '';
  const root = document.documentElement;
  const originalFetch = window.fetch.bind(window);
  let serverOffset = 0;
  let finalDeadline = null;
  let finalQuestionIndex = null;
  let finalBackground = '';
  let resolving = false;
  let lastForcedAt = 0;
  let mainRoundSeconds = 254;
  let finalOutcomeRefining = false;
  let finalOutcomeRefined = false;

  const initialServerNow = Date.parse(app.dataset.serverNow || '');
  if (Number.isFinite(initialServerNow)) serverOffset = initialServerNow - Date.now();

  app.dataset.campaignBackground = '';

  function clearBackground() {
    app.style.removeProperty('--quiz-background');
    root.style.removeProperty('--quiz-background');
    app.classList.remove('has-quiz-background');
  }

  function applyBackground(url) {
    if (!url) {
      clearBackground();
      return;
    }
    const value = `url("${url}")`;
    app.style.setProperty('--quiz-background', value);
    root.style.setProperty('--quiz-background', value);
    app.classList.add('has-quiz-background');
  }

  function activeScreenName() {
    return app.querySelector('[data-screen].active')?.dataset.screen || '';
  }

  function showScreen(name) {
    app.querySelectorAll('[data-screen]').forEach((screen) => {
      screen.classList.toggle('active', screen.dataset.screen === name);
    });
  }

  function applyScreenBackground() {
    const screen = activeScreenName();
    if (screen === 'final-question') {
      applyBackground(finalBackground);
      return;
    }
    if (screen !== 'question') clearBackground();
  }

  function countdownParts(milliseconds) {
    const totalSeconds = Math.max(0, Math.ceil(milliseconds / 1000));
    return {
      minutes: String(Math.floor(totalSeconds / 60)).padStart(2, '0'),
      seconds: String(totalSeconds % 60).padStart(2, '0'),
    };
  }

  function ensureIntroUrgency(screen) {
    let box = screen.querySelector('.jackside-intro-urgency');
    if (box) return box;
    box = document.createElement('div');
    box.className = 'jackside-intro-urgency';
    box.hidden = true;
    box.innerHTML = [
      '<span class="urgency-kicker">JACKSIDE 4:14</span>',
      '<strong class="urgency-title">Торопитесь, квиз уже начался</strong>',
      '<span class="urgency-caption">До конца квиза осталось</span>',
      '<span class="jackside-intro-clock" aria-live="polite">',
      '<b class="urgency-mm">04</b><i class="urgency-colon">:</i><b class="urgency-ss">14</b>',
      '</span>',
    ].join('');
    const firstAction = screen.querySelector('button, .quiz-primary, .quiz-actions');
    if (firstAction) firstAction.before(box);
    else screen.append(box);
    return box;
  }

  function mainRoundEndMs() {
    const start = Date.parse(app.dataset.activeFrom || '');
    if (!Number.isFinite(start)) return null;
    return start + Math.max(1, Number(mainRoundSeconds || 254)) * 1000;
  }

  function updateIntroUrgency() {
    const start = Date.parse(app.dataset.activeFrom || '');
    const end = mainRoundEndMs();
    const now = Date.now() + serverOffset;
    const active = Number.isFinite(start) && Number.isFinite(end) && now >= start && now < end;
    ['welcome', 'daily-prize', 'daily-jackcoin'].forEach((name) => {
      const screen = app.querySelector(`[data-screen="${name}"]`);
      if (!screen) return;
      const box = ensureIntroUrgency(screen);
      box.hidden = !active;
      if (!active) return;
      const parts = countdownParts(end - now);
      box.querySelector('.urgency-mm').textContent = parts.minutes;
      box.querySelector('.urgency-ss').textContent = parts.seconds;
    });
  }

  function cleanLobbyMinimumCopy() {
    const lobby = app.querySelector('[data-screen="final-lobby"]');
    if (!lobby) return;
    lobby.querySelectorAll('*').forEach((node) => {
      if (node.children.length) return;
      const value = node.textContent || '';
      if (!value.includes('нужно минимум')) return;
      node.textContent = value
        .replace(/\s*\(нужно минимум\s+\d+\)/gi, '')
        .replace(/нужно минимум\s+\d+/gi, '')
        .replace(/\s{2,}/g, ' ')
        .trim();
    });
  }

  function decorateFinalOutcomeActions() {
    const screen = app.querySelector('[data-screen="final-outcome"]');
    const existing = screen?.querySelector('.final-account-link');
    if (!screen || !existing) return;
    existing.href = '/account';
    existing.textContent = 'Вернуться в JACKSIDE';

    let actions = screen.querySelector('.final-outcome-actions');
    if (!actions) {
      actions = document.createElement('div');
      actions.className = 'final-outcome-actions';
      existing.replaceWith(actions);
      actions.append(existing);
    }

    if (!actions.querySelector('.final-rating-link')) {
      const rating = document.createElement('a');
      rating.className = 'quiz-secondary final-rating-link';
      rating.href = '/account?tab=rating';
      rating.textContent = 'Открыть рейтинг';
      actions.append(rating);
    }
  }

  async function refineFinalOutcome() {
    if (
      finalOutcomeRefined
      || finalOutcomeRefining
      || !campaign
      || activeScreenName() !== 'final-outcome'
    ) return;

    finalOutcomeRefining = true;
    try {
      const response = await originalFetch(
        `/api/jackside/final-outcome?campaign=${encodeURIComponent(campaign)}`,
        { headers: { Accept: 'application/json' }, cache: 'no-store' },
      );
      if (!response.ok) return;
      const data = await response.json();
      if (!data || data.state === 'pending') return;

      const screen = app.querySelector('[data-screen="final-outcome"]');
      if (!screen || !screen.classList.contains('active')) return;
      const mark = screen.querySelector('.final-outcome-mark');
      const title = screen.querySelector('.final-outcome-title');
      const message = screen.querySelector('.final-outcome-message');

      if (data.state === 'correct_not_first') {
        if (mark) mark.textContent = '✓';
        if (title) title.textContent = 'Ответ верный, но не первым';
        app.classList.remove('quiz-winner');
      } else if (data.state === 'eliminated' && data.answer_correct === false) {
        if (mark) mark.textContent = '×';
        if (title) title.textContent = 'Ответ неверный';
        app.classList.remove('quiz-winner');
      }
      if (message && data.message) message.textContent = data.message;
      finalOutcomeRefined = true;
    } catch (_) {
      // Keep the native final outcome if the refinement endpoint is unavailable.
    } finally {
      finalOutcomeRefining = false;
    }
  }

  function renderRecoveredFinalOutcome(data) {
    if (!data || data.state === 'pending' || data.state === 'final_question') return false;
    const screen = app.querySelector('[data-screen="final-outcome"]');
    if (!screen) return false;
    const mark = screen.querySelector('.final-outcome-mark');
    const kicker = screen.querySelector('.final-outcome-kicker');
    const title = screen.querySelector('.final-outcome-title');
    const message = screen.querySelector('.final-outcome-message');
    if (kicker) kicker.textContent = 'Финальный стол';
    if (mark) mark.textContent = data.state === 'winner' ? '★' : '♠';
    if (title) {
      if (data.state === 'winner') title.textContent = 'Победа!';
      else if (data.state === 'cancelled') title.textContent = 'Финальный стол не состоялся';
      else title.textContent = 'Финальный стол завершён';
    }
    if (message) message.textContent = data.message || 'Результат сохранён.';
    if (data.state === 'winner') app.classList.add('quiz-winner');
    finalDeadline = null;
    finalQuestionIndex = null;
    finalBackground = '';
    clearBackground();
    showScreen('final-outcome');
    decorateFinalOutcomeActions();
    void refineFinalOutcome();
    return true;
  }

  function rememberMeta(data) {
    if (!data || typeof data !== 'object') return;
    const seconds = Number(data.time_limit_seconds || 0);
    if (Number.isFinite(seconds) && seconds > 0 && seconds < 3600) {
      mainRoundSeconds = seconds;
    }
    const serverNow = Date.parse(data.server_now || '');
    if (Number.isFinite(serverNow)) serverOffset = serverNow - Date.now();
    updateIntroUrgency();
  }

  function rememberStatus(data) {
    if (!data || typeof data !== 'object') return;
    const serverNow = Date.parse(data.server_now || '');
    if (Number.isFinite(serverNow)) serverOffset = serverNow - Date.now();
    if (data.state === 'final_question') {
      const deadline = Date.parse(data.question_deadline_at || '');
      finalDeadline = Number.isFinite(deadline) ? deadline : null;
      finalQuestionIndex = Number.isInteger(data.question_index)
        ? data.question_index
        : Number(data.question_index);
      finalBackground = data.question?.section?.background_image || '';
    } else {
      finalDeadline = null;
      finalQuestionIndex = null;
      finalBackground = '';
    }
    applyScreenBackground();
  }

  async function fetchPersistedFinal() {
    try {
      const response = await originalFetch(
        `/api/jackside/final-result?campaign=${encodeURIComponent(campaign)}`,
        { headers: { Accept: 'application/json' }, cache: 'no-store' },
      );
      if (!response.ok) return null;
      return await response.json();
    } catch (_) {
      return null;
    }
  }

  window.fetch = async (...args) => {
    const response = await originalFetch(...args);
    const target = typeof args[0] === 'string' ? args[0] : args[0]?.url || '';
    if (target.includes('/api/quiz/questions')) {
      response.clone().json().then(rememberMeta).catch(() => {});
    }
    if (target.includes('/api/quiz/final-table/status')) {
      if (response.status === 404) {
        const recovered = await fetchPersistedFinal();
        if (recovered) {
          rememberStatus(recovered);
          return new Response(JSON.stringify(recovered), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        }
      }
      response.clone().json().then(rememberStatus).catch(() => {});
    }
    return response;
  };

  async function forceFinalResolution() {
    if (resolving || !campaign || activeScreenName() !== 'final-question') return;
    const now = Date.now();
    if (now - lastForcedAt < 300) return;
    lastForcedAt = now;
    resolving = true;
    const waiting = app.querySelector('.final-answer-wait');
    if (waiting) {
      waiting.hidden = false;
      waiting.textContent = 'Подводим итог…';
    }
    try {
      for (let attempt = 0; attempt < 30; attempt += 1) {
        if (activeScreenName() !== 'final-question') return;
        try {
          const response = await window.fetch(
            `/api/quiz/final-table/status?campaign=${encodeURIComponent(campaign)}`,
            { headers: { Accept: 'application/json' }, cache: 'no-store' },
          );
          if (response.ok) {
            const data = await response.json();
            const previousIndex = finalQuestionIndex;
            rememberStatus(data);
            if (renderRecoveredFinalOutcome(data)) return;
            const changedQuestion = (
              data.state === 'final_question'
              && Number(data.question_index) !== Number(previousIndex)
            );
            if (changedQuestion) {
              window.location.reload();
              return;
            }
          }
        } catch (_) {
          // Retry while the server remains authoritative for final resolution.
        }
        await new Promise((resolve) => window.setTimeout(resolve, attempt < 8 ? 350 : 750));
      }

      const recovered = await fetchPersistedFinal();
      if (recovered && renderRecoveredFinalOutcome(recovered)) return;
      if (waiting) waiting.textContent = 'Результат сохраняется. Попробуйте обновить через несколько секунд.';
    } finally {
      resolving = false;
    }
  }

  function deadlineTick() {
    applyScreenBackground();
    updateIntroUrgency();
    cleanLobbyMinimumCopy();
    decorateFinalOutcomeActions();
    void refineFinalOutcome();
    if (activeScreenName() !== 'final-question' || !Number.isFinite(finalDeadline)) return;
    if (Date.now() + serverOffset < finalDeadline) return;
    const button = app.querySelector('.final-answer-button');
    if (button) button.disabled = true;
    void forceFinalResolution();
  }

  const screenObserver = new MutationObserver(() => {
    applyScreenBackground();
    updateIntroUrgency();
    cleanLobbyMinimumCopy();
    decorateFinalOutcomeActions();
    deadlineTick();
  });
  app.querySelectorAll('[data-screen]').forEach((screen) => {
    screenObserver.observe(screen, { attributes: true, attributeFilter: ['class'] });
  });

  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') {
      deadlineTick();
      if (activeScreenName() === 'final-question') void forceFinalResolution();
    }
  });

  decorateFinalOutcomeActions();
  cleanLobbyMinimumCopy();
  clearBackground();
  updateIntroUrgency();
  window.setInterval(deadlineTick, 250);
})();
