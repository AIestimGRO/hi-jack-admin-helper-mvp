(() => {
  const app = document.getElementById('quiz-app');
  if (!app) return;

  const campaign = app.dataset.campaign || 'default';
  const isDaily414 = app.dataset.campaignType === 'daily_414';
  const state = { meta: null, verifiedIdentity: null, rememberedIdentity: null, attemptToken: null, questions: [], answers: {}, index: 0, deadline: null, timeLimit: 0, timer: null, scheduleTimer: null, serverOffset: 0, submitting: false, finishing: false, shareUrl: null, riverShown: false, finalPoll: null, finalTimer: null, finalQuestionIndex: null, finalAnswer: null, finalResult: null };
  const screens = [...app.querySelectorAll('[data-screen]')];
  const show = (name) => screens.forEach((screen) => screen.classList.toggle('active', screen.dataset.screen === name));
  const errorText = (message) => typeof message === 'string' ? message : 'Попробуй ещё раз чуть позже.';
  const identityForm = app.querySelector('.quiz-identity');

  async function readJson(response, fallbackMessage) {
    const contentType = response.headers.get('content-type') || '';
    if (!contentType.includes('application/json')) {
      throw new Error(fallbackMessage || 'Сервис временно недоступен. Обнови страницу и попробуй ещё раз.');
    }
    try {
      return await response.json();
    } catch (_) {
      throw new Error(fallbackMessage || 'Не удалось прочитать ответ сервера. Попробуй ещё раз.');
    }
  }

  async function jsonRequest(url, body) {
    const response = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'application/json' }, body: JSON.stringify(body) });
    const data = await readJson(response, 'Не удалось выполнить действие. Обнови страницу и попробуй ещё раз.');
    if (!response.ok) throw new Error(data.detail || data.error || 'Не удалось выполнить действие');
    return data;
  }

  function setBackground(url) {
    if (url) {
      app.style.setProperty('--quiz-background', `url("${url}")`);
      document.documentElement.style.setProperty('--quiz-background', `url("${url}")`);
      app.classList.add('has-quiz-background');
    } else {
      app.style.removeProperty('--quiz-background');
      document.documentElement.style.removeProperty('--quiz-background');
      app.classList.remove('has-quiz-background');
    }
  }

  function scheduledNow() { return Date.now() + state.serverOffset; }

  function showEnded() {
    if (state.scheduleTimer) window.clearInterval(state.scheduleTimer);
    state.scheduleTimer = null;
    const end = Date.parse(app.dataset.activeUntil || '');
    if (Number.isFinite(end)) {
      app.querySelector('.quiz-ended-message').textContent = `Время участия закончилось ${new Date(end).toLocaleString('ru-RU')}.`;
    }
    show('ended');
  }

  const flowKey = `daily414:${campaign}:flow`;
  const getFlow = () => {
    try { return sessionStorage.getItem(flowKey) || ''; } catch (_) { return ''; }
  };
  const setFlow = (step) => {
    try { sessionStorage.setItem(flowKey, step); } catch (_) { /* private mode */ }
  };

  function applyServerNow(value) {
    const serverNow = Date.parse(value || '');
    if (Number.isFinite(serverNow)) state.serverOffset = serverNow - Date.now();
  }

  function isPastCampaignStart() {
    const start = Date.parse(app.dataset.activeFrom || '');
    return Number.isFinite(start) && scheduledNow() >= start;
  }

  async function startQuizAfterCountdown() {
    show('loading');
    app.dataset.scheduleState = 'active';
    setFlow('countdown');
    for (let attempt = 0; attempt < 45; attempt += 1) {
      try {
        const started = await startQuiz({ fromCountdown: true });
        if (started) return;
      } catch (error) {
        if (!/Квиз начнётся/.test(error.message) || attempt === 44) {
          app.querySelector('.quiz-error-message').textContent = errorText(error.message);
          show('error');
          return;
        }
      }
      await new Promise((resolve) => window.setTimeout(resolve, 1000));
    }
    app.querySelector('.quiz-error-message').textContent = 'Не удалось начать квиз вовремя. Обнови страницу.';
    show('error');
  }

  function startCountdown({ onComplete } = {}) {
    if (state.scheduleTimer) {
      window.clearInterval(state.scheduleTimer);
      state.scheduleTimer = null;
    }
    setFlow('countdown');
    const start = Date.parse(app.dataset.activeFrom || '');
    if (!Number.isFinite(start)) {
      (onComplete || loadMeta)();
      return;
    }
    if (scheduledNow() >= start) {
      app.dataset.scheduleState = 'active';
      (onComplete || loadMeta)();
      return;
    }
    const output = {
      days: app.querySelector('[data-countdown-days]'),
      hours: app.querySelector('[data-countdown-hours]'),
      minutes: app.querySelector('[data-countdown-minutes]'),
      seconds: app.querySelector('[data-countdown-seconds]'),
    };
    app.querySelector('.quiz-schedule-time').textContent = `Старт: ${new Date(start).toLocaleString('ru-RU')}`;
    let completing = false;
    const finish = () => {
      if (completing) return;
      completing = true;
      window.clearInterval(state.scheduleTimer);
      state.scheduleTimer = null;
      if (state.countdownVisibilityHandler) {
        document.removeEventListener('visibilitychange', state.countdownVisibilityHandler);
        state.countdownVisibilityHandler = null;
      }
      app.dataset.scheduleState = 'active';
      (onComplete || loadMeta)();
    };
    const tick = () => {
      const remaining = Math.max(0, start - scheduledNow());
      const totalSeconds = Math.max(0, Math.ceil(remaining / 1000));
      output.days.textContent = String(Math.floor(totalSeconds / 86400)).padStart(2, '0');
      output.hours.textContent = String(Math.floor((totalSeconds % 86400) / 3600)).padStart(2, '0');
      output.minutes.textContent = String(Math.floor((totalSeconds % 3600) / 60)).padStart(2, '0');
      output.seconds.textContent = String(totalSeconds % 60).padStart(2, '0');
      if (remaining <= 0) finish();
    };
    if (state.countdownVisibilityHandler) {
      document.removeEventListener('visibilitychange', state.countdownVisibilityHandler);
    }
    state.countdownVisibilityHandler = () => {
      if (document.visibilityState === 'visible') tick();
    };
    document.addEventListener('visibilitychange', state.countdownVisibilityHandler);
    show('countdown');
    tick();
    state.scheduleTimer = window.setInterval(tick, 200);
  }

  function initialize() {
    applyServerNow(app.dataset.serverNow);
    setBackground(app.dataset.campaignBackground || '');
    if (app.dataset.scheduleState === 'ended') {
      showEnded();
      return;
    }
    // 4:14: welcome screens first; countdown only after "СЕСТЬ ЗА СТОЛ".
    if (app.dataset.scheduleState === 'upcoming' && !isDaily414) startCountdown();
    else loadMeta();
  }

  function identityValues() {
    const values = Object.fromEntries(new FormData(identityForm));
    return { phone: (values.phone || '').trim(), username: (values.username || '').trim(), name: (values.name || '').trim(), nickname: (values.nickname || '').trim() };
  }

  function validateIdentity(values) {
    if (state.verifiedIdentity?.verified) return;
    if (!values.phone) throw new Error('Укажи номер телефона');
  }

  function showIdentityMethods() {
    app.querySelector('.quiz-identity-methods').hidden = false;
    identityForm.hidden = true;
    show('identity');
  }

  function showPhoneIdentity() {
    app.querySelector('.quiz-identity-methods').hidden = true;
    identityForm.hidden = false;
    setIdentityError('');
    identityForm.querySelector('[name="phone"]').focus();
  }

  async function loadMeta() {
    stopTimer();
    show('loading');
    try {
      const response = await fetch(`/api/quiz/questions?campaign=${encodeURIComponent(campaign)}`, { headers: { Accept: 'application/json' } });
      const data = await readJson(response, 'Не удалось загрузить квиз. Обнови страницу и попробуй ещё раз.');
      if (!response.ok) throw new Error(data.error || data.detail || 'Не удалось загрузить квиз');
      state.meta = data;
      if (data.schedule_state) app.dataset.scheduleState = data.schedule_state;
      if (data.active_from) app.dataset.activeFrom = data.active_from;
      if (data.active_until) app.dataset.activeUntil = data.active_until;
      const countLabel = data.questions_count === 1 ? '1 вопрос' : `${data.questions_count} вопросов`;
      const timeLabel = data.time_limit_seconds > 0 ? ` · общее время ${formatTime(data.time_limit_seconds * 1000)}` : ' · без ограничения времени';
      app.querySelector('.quiz-welcome-meta').textContent = `${countLabel}${timeLabel} · попыток: ${data.max_attempts}`;
      app.querySelector('[data-content="welcome-kicker"]').textContent = data.content.welcome_kicker;
      app.querySelector('[data-content="welcome-text"]').textContent = data.content.welcome_text;
      app.querySelector('[data-content="identity-text"]').textContent = data.content.identity_text;
      app.querySelector('[data-action="identify"]').textContent = data.content.start_button_text;

      if (isDaily414) {
        const resumedFinal = await resumeFinalFlow({ force: getFlow() === 'lobby' });
        if (resumedFinal) {
          setFlow('lobby');
          return;
        }
      }

      const identityResponse = await fetch(`/api/quiz/identity?campaign=${encodeURIComponent(campaign)}`, { headers: { Accept: 'application/json' } });
      const identity = await readJson(identityResponse, 'Не удалось проверить сохранённый вход.');
      if (!identityResponse.ok) throw new Error(identity.error || identity.detail || 'Не удалось проверить сохранённый вход');
      if (identity.remembered) {
        showRemembered(identity);
        return;
      }
      if (identity.verified) showVerified(identity);
      if (identity.verified && identity.method === 'telegram' && app.dataset.telegramVerified === '1') {
        await startQuiz();
        return;
      }

      if (isDaily414) {
        const flow = getFlow();
        const upcoming = app.dataset.scheduleState === 'upcoming' && !isPastCampaignStart();
        if (!upcoming && (flow === 'countdown' || flow === 'playing' || flow === 'jackcoin' || flow === 'prize')) {
          await startQuizAfterCountdown();
          return;
        }
        if (upcoming && (flow === 'countdown' || flow === 'playing')) {
          startCountdown({ onComplete: () => { void startQuizAfterCountdown(); } });
          return;
        }
        if (flow === 'prize') {
          show('daily-prize');
          return;
        }
        if (flow === 'jackcoin') {
          show('daily-jackcoin');
          return;
        }
        setFlow('welcome');
        show('welcome');
        return;
      }

      show('welcome');
    } catch (error) {
      if (/Квиз завершён/.test(error.message)) {
        showEnded();
        return;
      }
      if (/Квиз начнётся/.test(error.message)) {
        app.dataset.scheduleState = 'upcoming';
        if (isDaily414) show('welcome');
        else startCountdown();
        return;
      }
      app.querySelector('.quiz-error-message').textContent = errorText(error.message);
      show('error');
    }
  }

  function showRemembered(identity) {
    state.rememberedIdentity = identity;
    const displayName = identity.display_name || 'участник Hi, Jack';
    app.querySelector('.quiz-remembered-greeting').textContent = `Добрый день, ${displayName}! Это вы?`;
    app.querySelector('.quiz-remembered-error').textContent = '';
    show('remembered');
  }

  function showVerified(identity) {
    state.verifiedIdentity = identity;
    const box = app.querySelector('.quiz-verified');
    let label = 'Данные подтверждены';
    if (identity.method === 'telegram') label = `Telegram подтверждён${identity.username ? `: @${identity.username}` : ''}`;
    else if (identity.method === 'email') label = `Email подтверждён${identity.email ? `: ${identity.email}` : ''}`;
    else if (identity.method === 'device') label = `Это вы — ${identity.display_name || 'можно продолжать'}`;
    else if (identity.method === 'member') label = `Аккаунт JACKSIDE подтверждён${identity.display_name ? `: ${identity.display_name}` : ''}`;
    box.textContent = `✓ ${label}`;
    box.hidden = false;
  }

  function setIdentityError(message) { app.querySelector('.quiz-identity-error').textContent = message ? errorText(message) : ''; }

  async function confirmRememberedIdentity() {
    if (state.submitting) return;
    state.submitting = true;
    const buttons = app.querySelectorAll('[data-screen="remembered"] button');
    buttons.forEach((button) => { button.disabled = true; });
    app.querySelector('.quiz-remembered-error').textContent = '';
    try {
      const data = await jsonRequest('/api/quiz/identity/confirm', { campaign });
      showVerified(data.identity);
      state.rememberedIdentity = null;
      state.submitting = false;
      await startQuiz();
    } catch (error) {
      app.querySelector('.quiz-remembered-error').textContent = errorText(error.message);
    } finally {
      state.submitting = false;
      buttons.forEach((button) => { button.disabled = false; });
    }
  }

  async function forgetRememberedIdentity() {
    if (state.submitting) return;
    state.submitting = true;
    const buttons = app.querySelectorAll('[data-screen="remembered"] button');
    buttons.forEach((button) => { button.disabled = true; });
    app.querySelector('.quiz-remembered-error').textContent = '';
    try {
      await jsonRequest('/api/quiz/identity/forget', {});
      state.rememberedIdentity = null;
      state.verifiedIdentity = null;
      const box = app.querySelector('.quiz-verified');
      box.hidden = true;
      box.textContent = '';
      showIdentityMethods();
    } catch (error) {
      app.querySelector('.quiz-remembered-error').textContent = errorText(error.message);
    } finally {
      state.submitting = false;
      buttons.forEach((button) => { button.disabled = false; });
    }
  }

  function clearTelegramVerifiedFlag() {
    if (app.dataset.telegramVerified !== '1') return;
    app.dataset.telegramVerified = '0';
    const url = new URL(window.location.href);
    url.searchParams.delete('telegram_verified');
    window.history.replaceState({}, '', `${url.pathname}${url.search}${url.hash}`);
  }

  async function requestDailySeat() {
    if (state.submitting) return;
    setFlow('jackcoin');
    if (app.dataset.scheduleState === 'upcoming' && !isPastCampaignStart()) {
      startCountdown({ onComplete: () => { void startQuizAfterCountdown(); } });
      return;
    }
    await startQuizAfterCountdown();
  }

  async function startQuiz({ fromCountdown = false } = {}) {
    if (state.submitting) return false;
    const activeUntil = Date.parse(app.dataset.activeUntil || '');
    if (Number.isFinite(activeUntil) && scheduledNow() >= activeUntil) {
      showEnded();
      return false;
    }
    const values = identityValues();
    if (!isDaily414) {
      try { validateIdentity(values); } catch (error) { setIdentityError(error.message); return false; }
    } else if (!state.verifiedIdentity?.verified) {
      state.verifiedIdentity = { verified: true, method: 'member' };
    }
    state.submitting = true;
    const button = isDaily414
      ? app.querySelector('[data-action="daily-start"]')
      : identityForm.querySelector('[data-action="start"]');
    if (button) {
      button.disabled = true;
      button.textContent = 'Открываем…';
    }
    setIdentityError('');
    try {
      const data = await jsonRequest('/api/quiz/start', { campaign, referrer_id: app.dataset.referrer, source: app.dataset.source, ...values });
      state.attemptToken = data.attempt_token;
      state.questions = data.questions || [];
      state.answers = data.answers || {};
      state.index = Number(data.current_index || 0);
      state.deadline = data.deadline_at ? new Date(data.deadline_at).getTime() : null;
      state.timeLimit = Number(data.time_limit_seconds || 0);
      state.riverShown = state.index >= 9;
      if (!state.questions.length) throw new Error('В квизе пока нет опубликованных вопросов');
      if (data.resumed) {
        const box = app.querySelector('.quiz-verified');
        box.textContent = 'Продолжаем ранее начатую попытку';
        box.hidden = false;
      }
      clearTelegramVerifiedFlag();
      setFlow('playing');
      renderQuestion();
      startTimer();
      return true;
    } catch (error) {
      setIdentityError(error.message);
      if (/Квиз завершён/.test(error.message)) {
        showEnded();
        return false;
      }
      if (/Квиз начнётся/.test(error.message)) {
        if (fromCountdown || isPastCampaignStart()) throw error;
        app.dataset.scheduleState = 'upcoming';
        startCountdown({ onComplete: () => { void startQuizAfterCountdown(); } });
        return false;
      }
      if (/исчерпан|успешно пройден/.test(error.message)) {
        if (isDaily414 && await resumeFinalFlow({ force: true })) {
          setFlow('lobby');
          return true;
        }
        app.querySelector('.quiz-error-message').textContent = errorText(error.message);
        show('error');
        return false;
      }
      if (fromCountdown) throw error;
      if (isDaily414) {
        app.querySelector('.quiz-error-message').textContent = errorText(error.message);
        show('error');
      } else {
        showIdentityMethods();
      }
      return false;
    } finally {
      state.submitting = false;
      if (button) {
        button.disabled = false;
        button.textContent = isDaily414 ? 'СЕСТЬ ЗА СТОЛ' : 'Продолжить';
      }
    }
  }

  function currentQuestion() { return state.questions[state.index]; }
  function emptyAnswer(question) { return question.type === 'multi_choice' ? [] : ''; }
  function hasAnswer(question, value = state.answers[question.id]) { return Array.isArray(value) ? value.length > 0 : Boolean(String(value || '').trim()); }

  function renderQuestion() {
    const question = currentQuestion();
    const savedAnswer = state.answers[question.id] ?? emptyAnswer(question);
    const screen = app.querySelector('[data-screen="question"]');
    screen.querySelector('.quiz-step').textContent = `Вопрос ${state.index + 1} из ${state.questions.length}`;
    screen.querySelector('.quiz-progress span').style.width = `${((state.index + 1) / state.questions.length) * 100}%`;
    const section = question.section || {};
    screen.dataset.theme = section.theme || 'theory';
    const questionBackground = section.background_image || app.dataset.campaignBackground || '';
    setBackground(questionBackground);
    if (questionBackground) screen.style.setProperty('--section-background', `url("${questionBackground}")`);
    else screen.style.removeProperty('--section-background');
    const sectionLabel = screen.querySelector('.quiz-section-label');
    const stageNames = { preflop: 'ПРЕФЛОП', flop: 'ФЛОП', turn: 'ТЕРН', river: 'РИВЕР' };
    const sectionTitle = isDaily414 ? stageNames[question.game_stage] : section.title;
    sectionLabel.hidden = !sectionTitle;
    sectionLabel.textContent = sectionTitle || '';
    const media = screen.querySelector('.quiz-question-media');
    const questionImage = media.querySelector('.quiz-question-image');
    media.hidden = !question.image_path;
    questionImage.src = question.image_path || '';
    questionImage.alt = question.image_path ? question.title : '';
    screen.querySelector('.quiz-question-title').textContent = question.title;
    screen.querySelector('.quiz-validation').textContent = '';
    // Prefer the dedicated question container; never write into final-table options.
    const options = screen.querySelector('[data-role="question-options"]') || screen.querySelector('.quiz-options');
    if (!options) throw new Error('Не найден блок вариантов ответа');
    options.replaceChildren();
    if (question.type === 'text') {
      const textarea = document.createElement('textarea');
      textarea.maxLength = 1000;
      textarea.placeholder = question.placeholder || 'Напиши свой ответ';
      textarea.value = savedAnswer;
      textarea.addEventListener('input', () => { state.answers[question.id] = textarea.value; });
      options.append(textarea);
    } else {
      const optionList = question.options || [];
      if (!optionList.length) {
        screen.querySelector('.quiz-validation').textContent = 'Варианты ответа пока не настроены. Обнови страницу или сообщи администратору.';
      }
      optionList.forEach((option) => {
        const label = document.createElement('label'); label.className = 'quiz-option';
        const input = document.createElement('input'); input.type = question.type === 'multi_choice' ? 'checkbox' : 'radio'; input.name = question.id; input.value = option.id;
        input.checked = Array.isArray(savedAnswer) ? savedAnswer.includes(option.id) : savedAnswer === option.id;
        label.classList.toggle('selected', input.checked);
        input.addEventListener('change', () => {
          state.answers[question.id] = question.type === 'multi_choice' ? [...options.querySelectorAll('input:checked')].map((item) => item.value) : option.id;
          options.querySelectorAll('.quiz-option').forEach((item) => item.classList.toggle('selected', item.querySelector('input').checked));
        });
        const marker = document.createElement('span'); const text = document.createElement('strong'); text.textContent = option.text;
        label.append(input, marker, text); options.append(label);
      });
    }
    screen.querySelector('[data-action="back"]').disabled = isDaily414 || state.index === 0;
    screen.querySelector('[data-action="next"]').textContent = state.index === state.questions.length - 1 ? 'Завершить тест' : 'Далее';
    show('question');
  }

  async function saveCurrentAnswer({ requireAnswer = false } = {}) {
    const question = currentQuestion(); const answer = state.answers[question.id] ?? emptyAnswer(question);
    if (!hasAnswer(question, answer)) {
      if (requireAnswer && question.required) throw new Error('Выбери ответ, чтобы продолжить');
      if (!Object.prototype.hasOwnProperty.call(state.answers, question.id)) return;
    }
    await jsonRequest('/api/quiz/answer', { attempt_token: state.attemptToken, question_id: question.id, answer });
  }

  async function navigate(direction) {
    if (state.submitting || state.finishing) return;
    if (isDaily414 && direction === 'back') return;
    state.submitting = true;
    const validation = app.querySelector('[data-screen="question"] .quiz-validation');
    app.querySelectorAll('[data-screen="question"] .quiz-actions button').forEach((button) => { button.disabled = true; });
    try {
      await saveCurrentAnswer({ requireAnswer: direction === 'next' });
      if (direction === 'back') state.index = Math.max(0, state.index - 1);
      else if (state.index < state.questions.length - 1) {
        state.index += 1;
        if (isDaily414 && state.questions[state.index]?.river_reveal && !state.riverShown) {
          state.riverShown = true;
          show('river');
          return;
        }
      }
      else { await finishQuestions(); return; }
      renderQuestion();
    } catch (error) { validation.textContent = errorText(error.message); }
    finally { state.submitting = false; if (!state.finishing) { app.querySelector('[data-action="back"]').disabled = isDaily414 || state.index === 0; app.querySelector('[data-action="next"]').disabled = false; } }
  }

  async function finishQuestions() {
    if (state.finishing) return;
    state.finishing = true; stopTimer();
    try { showResult(await jsonRequest('/api/quiz/finish', { attempt_token: state.attemptToken })); }
    catch (error) { app.querySelector('[data-screen="question"] .quiz-validation').textContent = errorText(error.message); state.finishing = false; startTimer(); }
  }

  function showResult(data) {
    state.finalResult = data;
    if (
      data.campaign_type === 'daily_414'
      && data.main_prize_eligible
      && data.final_table_available
    ) {
      startFinalLobby(data);
      return;
    }
    const title = app.querySelector('.quiz-success-title'); const mark = app.querySelector('.quiz-success-mark');
    app.classList.remove('quiz-winner', 'quiz-not-won'); title.textContent = data.title || 'Спасибо!';
    if (data.outcome === 'won') { mark.textContent = '★'; app.classList.add('quiz-winner'); launchConfetti(); }
    else if (data.outcome === 'not_won') { mark.textContent = '♥'; app.classList.add('quiz-not-won'); }
    else mark.textContent = '✓';
    app.querySelector('.quiz-success-message').textContent = data.message || 'Твои ответы сохранены.';
    const score = app.querySelector('.quiz-score-message');
    if (data.max_correct_count > 0) { score.textContent = `Правильных ответов: ${data.correct_count} из ${data.max_correct_count}. Баллы: ${data.score} из ${data.max_score}.`; score.hidden = false; score.classList.toggle('passed', Boolean(data.passed)); } else score.hidden = true;
    const dailyResult = app.querySelector('.daily-result');
    if (dailyResult) {
      dailyResult.hidden = data.campaign_type !== 'daily_414';
      if (!dailyResult.hidden) {
        dailyResult.querySelector('.daily-result-jackcoin').textContent = `+${data.jackcoin_awarded} JC`;
        dailyResult.querySelector('.daily-result-streak').textContent = `${data.streak_days} дн.`;
        dailyResult.querySelector('.daily-result-time').textContent = formatPreciseTime(data.completion_time_ms);
        const prize = dailyResult.querySelector('.daily-result-prize');
        if (data.main_prize_eligible) {
          prize.textContent = data.daily_place
            ? `Предварительное место отбора: ${data.daily_place}. Финальный стол начнётся одновременно для всех.`
            : 'Результат участвует в отборе за финальный стол.';
        } else {
          prize.textContent = 'JACKCOIN, награда и серия сохранены. Отбор за финальный стол уже закрыт.';
        }
      }
    }
    const reward = app.querySelector('.quiz-reward'); reward.hidden = !data.reward_code;
    if (data.reward_code) {
      app.querySelector('.quiz-reward-code').textContent = data.reward_code;
      app.querySelector('.quiz-reward-validity').textContent = data.reward_valid_until ? `Действует до ${new Date(data.reward_valid_until).toLocaleString('ru-RU')}` : 'Без ограничения срока';
    }
    const retry = app.querySelector('.quiz-retry'); retry.hidden = !data.retry_allowed;
    if (data.retry_allowed) retry.textContent = `Ещё одна попытка (${data.attempts_left} осталось)`;
    state.shareUrl = data.share_url || null;
    const share = app.querySelector('.quiz-share');
    share.hidden = !state.shareUrl;
    share.querySelector('.quiz-share-status').textContent = '';
    show('success');
  }

  async function fetchFinalStatus() {
    const response = await fetch(
      `/api/quiz/final-table/status?campaign=${encodeURIComponent(campaign)}`,
      { headers: { Accept: 'application/json' } },
    );
    const data = await readJson(response, 'Не удалось обновить финальный стол.');
    if (!response.ok) throw new Error(data.detail || data.error || 'Не удалось обновить финальный стол');
    applyServerNow(data.server_now);
    return data;
  }

  async function resumeFinalFlow({ force = false } = {}) {
    try {
      const data = await fetchFinalStatus();
      if (data.state === 'main_round') return false;
      handleFinalStatus(data);
      setFlow('lobby');
      return true;
    } catch (error) {
      if (force) {
        app.querySelector('.quiz-error-message').textContent = errorText(error.message);
        show('error');
        return true;
      }
      return false;
    }
  }

  function stopFinalQuestionTimer() {
    if (state.finalTimer) window.clearInterval(state.finalTimer);
    state.finalTimer = null;
  }

  function stopFinalFlow() {
    stopFinalQuestionTimer();
    if (state.finalPoll) window.clearInterval(state.finalPoll);
    state.finalPoll = null;
  }

  function startFinalPolling() {
    if (state.finalPoll) return;
    state.finalPoll = window.setInterval(async () => {
      try {
        handleFinalStatus(await fetchFinalStatus());
      } catch (error) {
        const lobbyActive = app.querySelector('[data-screen="final-lobby"]')?.classList.contains('active');
        const startsAt = Date.parse(state.finalResult?.final_table_starts_at || '');
        if (lobbyActive && Number.isFinite(startsAt) && scheduledNow() >= startsAt) {
          const message = app.querySelector('.final-lobby-message');
          if (message) message.textContent = errorText(error.message);
        }
      }
    }, 1000);
  }

  function renderFinalLobby(data = {}) {
    if (data.starts_at) {
      state.finalResult = {
        ...(state.finalResult || {}),
        final_table_starts_at: data.starts_at,
      };
    }
    if (data.correct_count != null || data.provisional_place != null) {
      state.finalResult = {
        ...(state.finalResult || {}),
        correct_count: data.correct_count ?? state.finalResult?.correct_count,
        max_correct_count: data.max_correct_count ?? state.finalResult?.max_correct_count,
        jackcoin_awarded: data.jackcoin_awarded ?? state.finalResult?.jackcoin_awarded,
        daily_place: data.provisional_place ?? state.finalResult?.daily_place,
      };
    }
    const startsAt = Date.parse(data.starts_at || state.finalResult?.final_table_starts_at || '');
    const message = app.querySelector('.final-lobby-message');
    const place = app.querySelector('.final-lobby-place');
    const result = state.finalResult;
    message.textContent = result?.correct_count != null
      ? `${result.correct_count} из ${result.max_correct_count || result.correct_count} правильно · +${result.jackcoin_awarded || 0} JACKCOIN.`
      : (data.message || 'Основной раунд завершён. Собираем десятку лучших игроков.');
    const provisionalPlace = data.provisional_place ?? result?.daily_place;
    const youStanding = Array.isArray(data.standings)
      ? data.standings.find((item) => item.is_you)
      : null;
    const placeValue = youStanding?.place ?? provisionalPlace;
    place.hidden = placeValue == null;
    if (placeValue != null) {
      const scoreHint = youStanding?.correct_count != null
        ? ` (${youStanding.correct_count} верных)`
        : '';
      place.textContent = `Сейчас вы на ${placeValue}-м месте отбора${scoreHint}. В финал попадут не более 10 игроков.`;
    }
    const lobbyScreen = app.querySelector('[data-screen="final-lobby"]');
    if (lobbyScreen.classList.contains('active') && state.finalTimer) {
      startFinalPolling();
      return;
    }
    stopFinalQuestionTimer();
    const output = app.querySelector('.final-lobby-countdown strong');
    let zeroFetchPending = false;
    let lastZeroFetchAt = 0;
    const tick = () => {
      const remaining = Number.isFinite(startsAt) ? Math.max(0, startsAt - scheduledNow()) : 0;
      output.textContent = formatTime(remaining);
      if (remaining > 0) return;
      const now = Date.now();
      if (zeroFetchPending || now - lastZeroFetchAt < 800) return;
      zeroFetchPending = true;
      lastZeroFetchAt = now;
      fetchFinalStatus()
        .then((status) => {
          handleFinalStatus(status);
        })
        .catch((error) => {
          message.textContent = errorText(error.message);
        })
        .finally(() => {
          zeroFetchPending = false;
        });
    };
    tick();
    state.finalTimer = window.setInterval(tick, 250);
    show('final-lobby');
    startFinalPolling();
  }

  function startFinalLobby(result) {
    state.finalResult = result;
    setFlow('lobby');
    renderFinalLobby({
      starts_at: result.final_table_starts_at,
      provisional_place: result.daily_place,
      correct_count: result.correct_count,
      max_correct_count: result.max_correct_count,
      jackcoin_awarded: result.jackcoin_awarded,
    });
    fetchFinalStatus().then(handleFinalStatus).catch(() => {});
  }

  function renderFinalQuestion(data) {
    startFinalPolling();
    const isNewQuestion = state.finalQuestionIndex !== data.question_index;
    state.finalQuestionIndex = data.question_index;
    const question = data.question;
    app.querySelector('.final-table-heading .quiz-section-label').textContent = data.heads_up ? 'ХЕДЗ-АП' : 'ФИНАЛЬНЫЙ СТОЛ';
    app.querySelector('.final-active-count').textContent = `В игре: ${data.active_count}`;
    app.querySelector('.final-question-number').textContent = `Вопрос финала ${question.final_number}`;
    const media = app.querySelector('.final-question-media');
    const image = media.querySelector('.final-question-image');
    media.hidden = !question.image_path;
    image.src = question.image_path || '';
    image.alt = question.image_path ? question.title : '';
    app.querySelector('.final-question-title').textContent = question.title;
    const button = app.querySelector('.final-answer-button');
    const waiting = app.querySelector('.final-answer-wait');
    const validation = app.querySelector('.final-question-validation');
    validation.textContent = '';
    if (isNewQuestion) {
      state.finalAnswer = question.type === 'multi_choice' ? [] : '';
      const options = app.querySelector('.final-question-options');
      options.replaceChildren();
      if (question.type === 'text') {
        const textarea = document.createElement('textarea');
        textarea.maxLength = 1000;
        textarea.placeholder = question.placeholder || 'Напишите ответ';
        textarea.addEventListener('input', () => { state.finalAnswer = textarea.value; });
        options.append(textarea);
      } else {
        (question.options || []).forEach((option) => {
          const label = document.createElement('label');
          label.className = 'quiz-option';
          const input = document.createElement('input');
          input.type = question.type === 'multi_choice' ? 'checkbox' : 'radio';
          input.name = `final-${question.id}`;
          input.value = option.id;
          input.addEventListener('change', () => {
            state.finalAnswer = question.type === 'multi_choice'
              ? [...options.querySelectorAll('input:checked')].map((item) => item.value)
              : option.id;
            options.querySelectorAll('.quiz-option').forEach((item) => {
              item.classList.toggle('selected', item.querySelector('input').checked);
            });
          });
          const marker = document.createElement('span');
          const text = document.createElement('strong');
          text.textContent = option.text;
          label.append(input, marker, text);
          options.append(label);
        });
      }
    }
    button.hidden = data.answered;
    button.disabled = data.answered;
    waiting.hidden = !data.answered;
    app.querySelectorAll('.final-question-options input, .final-question-options textarea').forEach((field) => {
      field.disabled = data.answered;
    });
    stopFinalQuestionTimer();
    const deadline = Date.parse(data.question_deadline_at || '');
    const timer = app.querySelector('.final-question-timer');
    const number = timer.querySelector('strong');
    const bar = timer.querySelector('span');
    const tick = () => {
      const remaining = Math.max(0, deadline - scheduledNow());
      number.textContent = formatTime(remaining);
      bar.style.width = `${Math.min(100, (remaining / (Number(data.question_seconds || 30) * 1000)) * 100)}%`;
      timer.classList.toggle('urgent', remaining <= 10000);
      if (remaining <= 0) {
        stopFinalQuestionTimer();
        button.disabled = true;
      }
    };
    tick();
    state.finalTimer = window.setInterval(tick, 200);
    show('final-question');
  }

  function renderFinalOutcome(data) {
    stopFinalFlow();
    const mark = app.querySelector('.final-outcome-mark');
    const kicker = app.querySelector('.final-outcome-kicker');
    const title = app.querySelector('.final-outcome-title');
    const message = app.querySelector('.final-outcome-message');
    kicker.textContent = 'Финальный стол';
    mark.textContent = '♠';
    if (data.state === 'winner') {
      mark.textContent = '★';
      title.textContent = 'Победа!';
      message.textContent = data.message;
      app.classList.add('quiz-winner');
      launchConfetti();
    } else if (data.state === 'eliminated') {
      title.textContent = 'Раздача завершена';
      message.textContent = `${data.message} Вопрос на вылет: ${data.eliminated_question}.`;
    } else if (data.state === 'not_qualified') {
      title.textContent = 'Топ-10 сформирован';
      message.textContent = data.message;
    } else if (data.state === 'not_eligible') {
      kicker.textContent = 'Основной квиз';
      title.textContent = 'Результат сохранён';
      message.textContent = data.message;
    } else if (data.state === 'unavailable') {
      title.textContent = 'Основной раунд завершён';
      message.textContent = data.message;
    } else {
      title.textContent = 'Финальный стол завершён';
      message.textContent = data.message || 'Результат сохранён.';
    }
    show('final-outcome');
  }

  function handleFinalStatus(data) {
    if (data.state === 'lobby') {
      renderFinalLobby(data);
      return;
    }
    if (data.state === 'final_question') {
      renderFinalQuestion(data);
      return;
    }
    if (data.state !== 'main_round') renderFinalOutcome(data);
  }

  async function submitFinalAnswer() {
    const validation = app.querySelector('.final-question-validation');
    const button = app.querySelector('.final-answer-button');
    const value = state.finalAnswer;
    if (Array.isArray(value) ? value.length === 0 : !String(value || '').trim()) {
      validation.textContent = 'Выберите ответ';
      return;
    }
    button.disabled = true;
    validation.textContent = '';
    try {
      await jsonRequest('/api/quiz/final-table/answer', {
        campaign,
        question_index: state.finalQuestionIndex,
        answer: value,
      });
      button.hidden = true;
      app.querySelector('.final-answer-wait').hidden = false;
      app.querySelectorAll('.final-question-options input, .final-question-options textarea').forEach((field) => {
        field.disabled = true;
      });
    } catch (error) {
      validation.textContent = errorText(error.message);
      button.disabled = false;
    }
  }

  async function copyShareLink() {
    if (!state.shareUrl) return;
    const status = app.querySelector('.quiz-share-status');
    try {
      await navigator.clipboard.writeText(state.shareUrl);
      status.textContent = 'Ссылка скопирована';
    } catch (_) {
      window.prompt('Скопируй ссылку', state.shareUrl);
    }
  }

  async function shareQuiz() {
    if (!state.shareUrl) return;
    if (navigator.share) {
      try {
        await navigator.share({ title: state.meta?.title || 'Квиз Hi, Jack!', text: 'Пройди квиз Hi, Jack!', url: state.shareUrl });
        return;
      } catch (error) {
        if (error.name === 'AbortError') return;
      }
    }
    await copyShareLink();
  }

  function formatTime(milliseconds) { const totalSeconds = Math.max(0, Math.ceil(milliseconds / 1000)); return `${Math.floor(totalSeconds / 60)}:${String(totalSeconds % 60).padStart(2, '0')}`; }
  function formatPreciseTime(milliseconds) {
    const value = Math.max(0, Number(milliseconds || 0));
    const minutes = Math.floor(value / 60000);
    const seconds = Math.floor((value % 60000) / 1000);
    const tenths = Math.floor((value % 1000) / 100);
    return `${minutes}:${String(seconds).padStart(2, '0')}.${tenths}`;
  }
  function startTimer() {
    stopTimer(); const timer = app.querySelector('.quiz-timer');
    if (!state.deadline || state.timeLimit <= 0) { timer.hidden = true; return; }
    timer.hidden = false; const number = timer.querySelector('strong'); const bar = timer.querySelector('span');
    const tick = async () => {
      const remaining = Math.max(0, state.deadline - Date.now()); number.textContent = formatTime(remaining); bar.style.width = `${Math.min(100, (remaining / (state.timeLimit * 1000)) * 100)}%`; timer.classList.toggle('urgent', remaining <= 15000);
      if (remaining <= 0 && !state.finishing) { stopTimer(); try { await saveCurrentAnswer(); } catch (_) { /* server controls deadline */ } await finishQuestions(); }
    };
    tick(); state.timer = window.setInterval(tick, 250);
  }
  function stopTimer() { if (state.timer) window.clearInterval(state.timer); state.timer = null; }

  function launchConfetti() {
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    const layer = document.createElement('div'); layer.className = 'confetti-layer'; layer.setAttribute('aria-hidden', 'true'); const colors = ['#006985', '#d5a547', '#00a184', '#ffffff'];
    for (let index = 0; index < 60; index += 1) { const piece = document.createElement('i'); piece.style.setProperty('--x', `${Math.random() * 100}vw`); piece.style.setProperty('--delay', `${Math.random() * .8}s`); piece.style.setProperty('--duration', `${2.2 + Math.random() * 1.8}s`); piece.style.setProperty('--rotate', `${Math.random() * 720 - 360}deg`); piece.style.setProperty('--drift', `${Math.random() * 180 - 90}px`); piece.style.background = colors[index % colors.length]; layer.append(piece); }
    document.body.append(layer); window.setTimeout(() => layer.remove(), 5000);
  }

  app.querySelector('[data-action="identify"]').addEventListener('click', () => {
    if (isDaily414) {
      setFlow('prize');
      show('daily-prize');
    } else showIdentityMethods();
  });
  app.querySelector('[data-action="remembered-confirm"]').addEventListener('click', confirmRememberedIdentity);
  app.querySelector('[data-action="remembered-forget"]').addEventListener('click', forgetRememberedIdentity);
  app.querySelector('[data-action="phone-identity"]').addEventListener('click', showPhoneIdentity);
  app.querySelector('[data-action="identity-methods"]').addEventListener('click', showIdentityMethods);
  if (identityForm) identityForm.addEventListener('submit', (event) => { event.preventDefault(); startQuiz(); });
  app.querySelector('[data-action="back"]').addEventListener('click', () => navigate('back'));
  app.querySelector('[data-action="next"]').addEventListener('click', () => navigate('next'));
  app.querySelector('[data-action="retry"]').addEventListener('click', loadMeta);
  app.querySelector('[data-action="new-attempt"]').addEventListener('click', () => { state.finishing = false; startQuiz(); });
  app.querySelector('[data-action="share"]').addEventListener('click', shareQuiz);
  app.querySelector('[data-action="copy-share"]').addEventListener('click', copyShareLink);
  app.querySelector('[data-action="daily-prize-next"]')?.addEventListener('click', () => {
    setFlow('jackcoin');
    show('daily-jackcoin');
  });
  app.querySelector('[data-action="daily-start"]')?.addEventListener('click', requestDailySeat);
  app.querySelector('[data-action="river-open"]')?.addEventListener('click', renderQuestion);
  app.querySelector('[data-action="final-answer"]')?.addEventListener('click', submitFinalAnswer);
  window.addEventListener('beforeunload', () => {
    stopTimer();
    stopFinalFlow();
    if (state.scheduleTimer) window.clearInterval(state.scheduleTimer);
  });
  initialize();
})();
