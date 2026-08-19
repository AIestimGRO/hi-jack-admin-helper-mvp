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

  function applyScreenBackground() {
    const screen = activeScreenName();
    if (screen === 'final-question') {
      applyBackground(finalBackground);
      return;
    }
    if (screen !== 'question') clearBackground();
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

  window.fetch = async (...args) => {
    const response = await originalFetch(...args);
    const target = typeof args[0] === 'string' ? args[0] : args[0]?.url || '';
    if (target.includes('/api/quiz/final-table/status')) {
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
          const response = await originalFetch(
            `/api/quiz/final-table/status?campaign=${encodeURIComponent(campaign)}`,
            { headers: { Accept: 'application/json' }, cache: 'no-store' },
          );
          if (response.ok) {
            const data = await response.json();
            const previousIndex = finalQuestionIndex;
            rememberStatus(data);
            const changedQuestion = (
              data.state === 'final_question'
              && Number(data.question_index) !== Number(previousIndex)
            );
            if (data.state !== 'final_question' || changedQuestion) {
              window.location.reload();
              return;
            }
          }
        } catch (_) {
          // The regular poll and this watchdog both retry; keep the player on a
          // controlled resolving state instead of leaving a dead 0:00 screen.
        }
        await new Promise((resolve) => window.setTimeout(resolve, attempt < 8 ? 350 : 750));
      }
    } finally {
      resolving = false;
    }
  }

  function deadlineTick() {
    applyScreenBackground();
    if (activeScreenName() !== 'final-question' || !Number.isFinite(finalDeadline)) return;
    if (Date.now() + serverOffset < finalDeadline) return;
    const button = app.querySelector('.final-answer-button');
    if (button) button.disabled = true;
    void forceFinalResolution();
  }

  const screenObserver = new MutationObserver(() => {
    applyScreenBackground();
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

  clearBackground();
  window.setInterval(deadlineTick, 250);
})();
