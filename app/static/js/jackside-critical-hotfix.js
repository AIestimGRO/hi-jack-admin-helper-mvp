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

  function countdownParts(milliseconds) {
    const totalSeconds = Math.max(0, Math.ceil(milliseconds / 1000));
    return {
      minutes: String(Math.floor(totalSeconds / 60)).padStart(2, '0'),
      seconds: String(totalSeconds % 60).padStart(2, '0'),
    };
  }

  function ensureIntroUrgencyStyles() {
    if (document.getElementById('jackside-intro-urgency-styles')) return;
    const style = document.createElement('style');
    style.id = 'jackside-intro-urgency-styles';
    style.textContent = `
      .jackside-intro-urgency {
        position: relative;
        overflow: hidden;
        margin: 20px 0 18px;
        padding: 16px 14px 18px;
        border: 1px solid rgba(64, 224, 208, .42);
        border-radius: 22px;
        text-align: center;
        background:
          radial-gradient(circle at 50% 0%, rgba(29, 211, 198, .14), transparent 58%),
          linear-gradient(180deg, rgba(7, 30, 28, .92), rgba(2, 12, 12, .94));
        box-shadow:
          0 0 0 1px rgba(255,255,255,.025) inset,
          0 16px 36px rgba(0,0,0,.24),
          0 0 26px rgba(21, 190, 184, .08);
      }
      .jackside-intro-urgency::before {
        content: '';
        position: absolute;
        inset: 0;
        pointer-events: none;
        background: linear-gradient(115deg, transparent 30%, rgba(255,255,255,.055) 48%, transparent 64%);
      }
      .jackside-intro-urgency .urgency-kicker {
        position: relative;
        display: block;
        margin: 0 0 8px;
        color: #8df4e9;
        font-size: 12px;
        font-weight: 800;
        letter-spacing: .12em;
        text-transform: uppercase;
      }
      .jackside-intro-urgency .urgency-title {
        position: relative;
        display: block;
        margin: 0;
        color: #fff;
        font-size: clamp(18px, 5vw, 24px);
        font-weight: 850;
        line-height: 1.08;
      }
      .jackside-intro-urgency .urgency-caption {
        position: relative;
        display: block;
        margin: 9px 0 11px;
        color: rgba(222, 247, 243, .72);
        font-size: 12px;
        line-height: 1.25;
      }
      .jackside-intro-clock {
        position: relative;
        display: inline-flex;
        align-items: center;
        justify-content: center;
        min-width: 176px;
        padding: 10px 20px 11px;
        border: 1px solid rgba(78, 235, 222, .52);
        border-radius: 999px;
        background: rgba(2, 15, 15, .88);
        box-shadow:
          0 0 22px rgba(38, 222, 209, .16),
          inset 0 0 18px rgba(25, 180, 172, .08);
        color: #b9fff8;
        font-variant-numeric: tabular-nums;
        font-weight: 900;
        letter-spacing: .04em;
        line-height: 1;
      }
      .jackside-intro-clock .urgency-mm,
      .jackside-intro-clock .urgency-ss {
        display: inline-block;
        min-width: 2.1ch;
        font-size: clamp(30px, 9vw, 42px);
        text-shadow: 0 0 16px rgba(80, 244, 229, .28);
      }
      .jackside-intro-clock .urgency-colon {
        display: inline-block;
        margin: 0 5px 3px;
        color: #47d8cc;
        font-size: clamp(26px, 7vw, 36px);
        animation: jacksideUrgencyBlink 1s steps(2,end) infinite;
      }
      @keyframes jacksideUrgencyBlink { 50% { opacity: .28; } }
      @media (prefers-reduced-motion: reduce) {
        .jackside-intro-clock .urgency-colon { animation: none; }
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

  window.fetch = async (...args) => {
    const response = await originalFetch(...args);
    const target = typeof args[0] === 'string' ? args[0] : args[0]?.url || '';
    if (target.includes('/api/quiz/questions')) {
      response.clone().json().then(rememberMeta).catch(() => {});
    }
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
