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

  function applyScreenBackground() {
    const screen = activeScreenName();
    if (screen === 'final-question') {
      applyBackground(finalBackground);
      return;
    }
    if (screen !== 'question') clearBackground();
  }

  function formatCountdown(milliseconds) {
    const totalSeconds = Math.max(0, Math.ceil(milliseconds / 1000));
    const minutes = Math.floor(totalSeconds / 60);
    const seconds = totalSeconds % 60;
    return `${minutes}:${String(seconds).padStart(2, '0')}`;
  }

  function ensureIntroUrgencyStyles() {
    if (document.getElementById('jackside-intro-urgency-styles')) return;
    const style = document.createElement('style');
    style.id = 'jackside-intro-urgency-styles';
    style.textContent = `
      .jackside-intro-urgency {
        margin: 16px 0;
        padding: 14px 16px;
        border: 1px solid rgba(255,255,255,.18);
        border-radius: 16px;
        text-align: center;
        background: rgba(0,0,0,.24);
      }
      .jackside-intro-urgency strong,
      .jackside-intro-urgency span { display: block; }
      .jackside-intro-urgency strong { font-size: 1rem; }
      .jackside-intro-urgency span { margin-top: 6px; opacity: .86; }
      .jackside-intro-urgency b {
        font-size: 1.45rem;
        letter-spacing: .04em;
        animation: jacksideUrgencyBlink 1s steps(2,end) infinite;
      }
      @keyframes jacksideUrgencyBlink { 50% { opacity: .35; } }
      @media (prefers-reduced-motion: reduce) {
        .jackside-intro-urgency b { animation: none; }
      }
      .final-outcome-actions {
        display: grid;
        gap: 10px;
        width: 100%;
      }
    `;
    document.head.append(style);
  }

  function ensureIntroUrgency(screen) {
    let box = screen.querySelector('.jackside-intro-urgency');
    if (box) return box;
    box = document.createElement('div');
    box.className = 'jackside-intro-urgency';
    box.hidden = true;
    box.innerHTML = '<strong>Торопитесь, квиз уже начался</strong><span>До конца квиза осталось</span><b>0:00</b>';
    const firstAction = screen.querySelector('button, .quiz-primary, .quiz-actions');
    if (firstAction) firstAction.before(box);
    else screen.append(box);
    return box;
  }

  function updateIntroUrgency() {
    const start = Date.parse(app.dataset.activeFrom || '');
    const end = Date.parse(app.dataset.activeUntil || '');
    const now = Date.now() + serverOffset;
    const active = Number.isFinite(start) && Number.isFinite(end) && now >= start && now < end;
    ['welcome', 'daily-prize', 'daily-jackcoin'].forEach((name) => {
      const screen = app.querySelector(`[data-screen="${name}"]`);
      if (!screen) return;
      const box = ensureIntroUrgency(screen);
      box.hidden = !active;
      if (active) box.querySelector('b').textContent = formatCountdown(end - now);
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
          } else if (response.status === 404 && attempt >= 2) {
            window.location.reload();
            return;
          }
        } catch (_) {
          // Retry while the server remains authoritative for final resolution.
        }
        await new Promise((resolve) => window.setTimeout(resolve, attempt < 8 ? 350 : 750));
      }
    } finally {
      resolving = false;
    }
  }

  function deadlineTick() {
    applyScreenBackground();
    updateIntroUrgency();
    decorateFinalOutcomeActions();
    if (activeScreenName() !== 'final-question' || !Number.isFinite(finalDeadline)) return;
    if (Date.now() + serverOffset < finalDeadline) return;
    const button = app.querySelector('.final-answer-button');
    if (button) button.disabled = true;
    void forceFinalResolution();
  }

  const screenObserver = new MutationObserver(() => {
    applyScreenBackground();
    updateIntroUrgency();
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

  ensureIntroUrgencyStyles();
  decorateFinalOutcomeActions();
  clearBackground();
  updateIntroUrgency();
  window.setInterval(deadlineTick, 250);
})();
