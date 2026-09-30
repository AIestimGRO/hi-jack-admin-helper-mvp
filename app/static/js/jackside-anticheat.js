(() => {
  const app = document.getElementById('quiz-app');
  if (!app || app.dataset.campaignType !== 'daily_414') return;
  if (window.HJJacksideAntiCheatInstalled) return;
  window.HJJacksideAntiCheatInstalled = true;

  const ANTI_CHEAT_DEBOUNCE_MS = 1000;
  const campaign = app.dataset.campaign || 'default';
  const pendingKey = `jackside:${campaign}:anti-cheat-pending`;
  const noticeKey = `jackside:${campaign}:anti-cheat-notice`;
  const originalFetch = window.fetch.bind(window);

  let attemptToken = '';
  let questions = [];
  let currentIndex = 0;
  let hiddenTimer = null;
  let skipInFlight = false;
  let internalNavigation = false;

  function safeParse(value) {
    try { return JSON.parse(value); } catch (_) { return null; }
  }

  function getStoredPending() {
    try {
      const raw = sessionStorage.getItem(pendingKey);
      const parsed = raw ? safeParse(raw) : null;
      return parsed && typeof parsed === 'object' ? parsed : null;
    } catch (_) {
      return null;
    }
  }

  function setStoredPending(value) {
    try { sessionStorage.setItem(pendingKey, JSON.stringify(value)); } catch (_) { /* private mode */ }
  }

  function clearStoredPending() {
    try { sessionStorage.removeItem(pendingKey); } catch (_) { /* private mode */ }
  }

  function setNotice(message) {
    try { sessionStorage.setItem(noticeKey, message); } catch (_) { /* private mode */ }
  }

  function takeNotice() {
    try {
      const message = sessionStorage.getItem(noticeKey) || '';
      if (message) sessionStorage.removeItem(noticeKey);
      return message;
    } catch (_) {
      return '';
    }
  }

  function activeQuestionScreen() {
    return app.querySelector('[data-screen="question"].active');
  }

  function activeQuestion() {
    const screen = activeQuestionScreen();
    if (!screen || !attemptToken || !questions.length) return null;
    const step = screen.querySelector('.quiz-step')?.textContent || '';
    const match = step.match(/Вопрос\s+(\d+)\s+из\s+(\d+)/i);
    let index = Number.isInteger(currentIndex) ? currentIndex : 0;
    if (match) index = Math.max(0, Number(match[1]) - 1);
    const question = questions[index];
    if (!question?.id) return null;
    return { questionId: String(question.id), index };
  }

  function maybeShowNotice() {
    const screen = activeQuestionScreen();
    if (!screen) return;
    const message = takeNotice();
    if (!message) return;
    const validation = screen.querySelector('.quiz-validation');
    if (validation) validation.textContent = message;
  }

  function watchQuestionScreen() {
    const screen = app.querySelector('[data-screen="question"]');
    if (!screen) return;
    const observer = new MutationObserver(() => {
      if (screen.classList.contains('active')) window.setTimeout(maybeShowNotice, 0);
    });
    observer.observe(screen, { attributes: true, attributeFilter: ['class'] });
    if (screen.classList.contains('active')) window.setTimeout(maybeShowNotice, 0);
  }

  function requestBody(init) {
    const body = init?.body;
    if (typeof body !== 'string') return null;
    return safeParse(body);
  }

  function requestPath(input) {
    try {
      const value = typeof input === 'string' ? input : input?.url;
      return new URL(value, window.location.href).pathname;
    } catch (_) {
      return '';
    }
  }

  async function captureResponse(path, input, init, response) {
    try {
      if (path === '/api/quiz/start' && response.ok) {
        const data = await response.clone().json();
        attemptToken = String(data.attempt_token || '');
        questions = Array.isArray(data.questions) ? data.questions : [];
        currentIndex = Number(data.current_index || 0);
        window.setTimeout(() => { void processStoredPending(); }, 0);
        return;
      }
      if (path === '/api/quiz/answer' && response.ok) {
        const payload = requestBody(init);
        const questionId = String(payload?.question_id || '');
        const index = questions.findIndex((item) => String(item?.id || '') === questionId);
        if (index >= 0) currentIndex = Math.min(index + 1, Math.max(0, questions.length - 1));
        return;
      }
      if (path === '/api/quiz/finish' && response.ok) clearStoredPending();
    } catch (_) {
      // Anti-cheat observation must never break the quiz request itself.
    }
  }

  window.fetch = async function patchedFetch(input, init) {
    const path = requestPath(input);
    const response = await originalFetch(input, init);
    void captureResponse(path, input, init, response);
    return response;
  };

  function clearHiddenTimer() {
    if (hiddenTimer) window.clearTimeout(hiddenTimer);
    hiddenTimer = null;
  }

  function forcedFinishScreen(result) {
    const success = app.querySelector('[data-screen="success"]');
    if (!success) {
      internalNavigation = true;
      window.location.assign('/account?tab=stats');
      return;
    }
    app.querySelectorAll('[data-screen]').forEach((screen) => screen.classList.remove('active'));
    success.classList.add('active');
    app.classList.remove('quiz-winner', 'quiz-not-won');

    const mark = success.querySelector('.quiz-success-mark');
    if (mark) mark.textContent = '!';
    const title = success.querySelector('.quiz-success-title');
    if (title) title.textContent = result?.title || 'Основной раунд завершён';
    const message = success.querySelector('.quiz-success-message');
    if (message) {
      message.textContent = 'Вы покинули страницу более чем на 1 секунду. Последний вопрос засчитан как неправильный.';
    }
    const score = success.querySelector('.quiz-score-message');
    if (score && Number(result?.max_correct_count || 0) > 0) {
      score.hidden = false;
      score.textContent = `Правильных ответов: ${Number(result.correct_count || 0)} из ${Number(result.max_correct_count || 0)}. Баллы: ${Number(result.score || 0)} из ${Number(result.max_score || 0)}.`;
      score.classList.toggle('passed', Boolean(result?.passed));
    }

    const dailyResult = success.querySelector('.daily-result');
    if (dailyResult) {
      dailyResult.hidden = false;
      const jc = dailyResult.querySelector('.daily-result-jackcoin');
      if (jc) jc.textContent = `+${Number(result?.jackcoin_awarded || 0)} JC`;
      const streak = dailyResult.querySelector('.daily-result-streak');
      if (streak) streak.textContent = `${Number(result?.streak_days || 0)} дн.`;
      const place = dailyResult.querySelector('.daily-result-place');
      const placeWrap = place?.parentElement;
      if (placeWrap) {
        placeWrap.hidden = result?.daily_place == null;
        if (result?.daily_place != null) place.textContent = String(result.daily_place);
      }
      const participants = dailyResult.querySelector('.daily-result-participants');
      const participantsWrap = participants?.parentElement;
      if (participantsWrap) {
        participantsWrap.hidden = result?.participant_count == null;
        if (result?.participant_count != null) participants.textContent = String(result.participant_count);
      }
      const finalStatus = dailyResult.querySelector('.daily-result-final-status');
      if (finalStatus) finalStatus.textContent = 'Вне отбора';
      const prize = dailyResult.querySelector('.daily-result-prize');
      if (prize) prize.textContent = 'JACKCOIN за правильные ответы сохранены. Последний вопрос был засчитан как неправильный из-за ухода со страницы.';
    }

    const retry = success.querySelector('.quiz-retry');
    if (retry) retry.hidden = true;
    const share = success.querySelector('.quiz-share');
    if (share) share.hidden = true;
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  async function applyPenalty(record, { keepalive = false } = {}) {
    if (skipInFlight || !record?.attemptToken || !record?.questionId) return;
    const elapsed = Math.max(0, Date.now() - Number(record.hiddenAt || 0));
    if (elapsed < ANTI_CHEAT_DEBOUNCE_MS) return;
    skipInFlight = true;
    try {
      const response = await originalFetch('/api/quiz/anti-cheat/skip', {
        method: 'POST',
        credentials: 'same-origin',
        keepalive,
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify({
          attempt_token: record.attemptToken,
          question_id: record.questionId,
          hidden_ms: elapsed,
        }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || data.error || `Ошибка ${response.status}`);

      if (!data.skipped) {
        if (['already_answered', 'already_advanced', 'attempt_finished'].includes(data.reason)) {
          clearStoredPending();
        }
        return;
      }

      clearStoredPending();
      if (data.finished) {
        const result = data.result || {};
        if (result.main_prize_eligible && result.final_table_starts_at) {
          internalNavigation = true;
          window.location.reload();
          return;
        }
        forcedFinishScreen(result);
        return;
      }

      setNotice('Вы покинули страницу более чем на 1 секунду. Предыдущий вопрос засчитан как неправильный.');
      internalNavigation = true;
      window.location.reload();
    } catch (error) {
      if (document.visibilityState === 'visible') {
        const validation = activeQuestionScreen()?.querySelector('.quiz-validation');
        if (validation) validation.textContent = `Не удалось применить защиту от ухода со страницы: ${error.message || 'ошибка'}`;
      }
    } finally {
      skipInFlight = false;
    }
  }

  function rememberHiddenQuestion() {
    const active = activeQuestion();
    if (!active) return;
    const record = {
      campaign,
      attemptToken,
      questionId: active.questionId,
      questionIndex: active.index,
      hiddenAt: Date.now(),
    };
    setStoredPending(record);
    clearHiddenTimer();
    hiddenTimer = window.setTimeout(() => {
      const pending = getStoredPending();
      if (
        document.visibilityState === 'hidden'
        && pending
        && pending.attemptToken === record.attemptToken
        && pending.questionId === record.questionId
      ) {
        void applyPenalty(pending, { keepalive: true });
      }
    }, ANTI_CHEAT_DEBOUNCE_MS);
  }

  async function processStoredPending() {
    const pending = getStoredPending();
    if (!pending) return;
    if (pending.campaign !== campaign) {
      clearStoredPending();
      return;
    }
    if (attemptToken && pending.attemptToken !== attemptToken) {
      clearStoredPending();
      return;
    }
    const elapsed = Math.max(0, Date.now() - Number(pending.hiddenAt || 0));
    if (elapsed < ANTI_CHEAT_DEBOUNCE_MS) {
      if (document.visibilityState === 'visible') clearStoredPending();
      return;
    }
    await applyPenalty(pending, { keepalive: document.visibilityState === 'hidden' });
  }

  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') {
      rememberHiddenQuestion();
      return;
    }
    clearHiddenTimer();
    void processStoredPending();
  });

  window.addEventListener('pagehide', () => {
    if (internalNavigation) return;
    if (!getStoredPending()) rememberHiddenQuestion();
  });

  window.addEventListener('pageshow', () => {
    window.setTimeout(() => { void processStoredPending(); }, 0);
  });

  watchQuestionScreen();
})();
