(() => {
  const app = document.getElementById('quiz-app');
  if (!app) return;

  const campaign = app.dataset.campaign || 'default';
  const state = { meta: null, verifiedIdentity: null, rememberedIdentity: null, attemptToken: null, questions: [], answers: {}, index: 0, deadline: null, timeLimit: 0, timer: null, scheduleTimer: null, serverOffset: 0, submitting: false, finishing: false, shareUrl: null };
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
    if (!response.ok) throw new Error(data.error || 'Не удалось выполнить действие');
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

  function startCountdown() {
    const start = Date.parse(app.dataset.activeFrom || '');
    if (!Number.isFinite(start)) {
      loadMeta();
      return;
    }
    const output = {
      days: app.querySelector('[data-countdown-days]'),
      hours: app.querySelector('[data-countdown-hours]'),
      minutes: app.querySelector('[data-countdown-minutes]'),
      seconds: app.querySelector('[data-countdown-seconds]'),
    };
    app.querySelector('.quiz-schedule-time').textContent = `Старт: ${new Date(start).toLocaleString('ru-RU')}`;
    const tick = () => {
      const remaining = Math.max(0, start - scheduledNow());
      const totalSeconds = Math.ceil(remaining / 1000);
      output.days.textContent = String(Math.floor(totalSeconds / 86400)).padStart(2, '0');
      output.hours.textContent = String(Math.floor((totalSeconds % 86400) / 3600)).padStart(2, '0');
      output.minutes.textContent = String(Math.floor((totalSeconds % 3600) / 60)).padStart(2, '0');
      output.seconds.textContent = String(totalSeconds % 60).padStart(2, '0');
      if (remaining <= 0) {
        window.clearInterval(state.scheduleTimer);
        state.scheduleTimer = null;
        app.dataset.scheduleState = 'active';
        loadMeta();
      }
    };
    show('countdown');
    tick();
    state.scheduleTimer = window.setInterval(tick, 250);
  }

  function initialize() {
    const serverNow = Date.parse(app.dataset.serverNow || '');
    if (Number.isFinite(serverNow)) state.serverOffset = serverNow - Date.now();
    setBackground(app.dataset.campaignBackground || '');
    if (app.dataset.scheduleState === 'upcoming') startCountdown();
    else if (app.dataset.scheduleState === 'ended') showEnded();
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
      if (!response.ok) throw new Error(data.error || 'Не удалось загрузить квиз');
      state.meta = data;
      const countLabel = data.questions_count === 1 ? '1 вопрос' : `${data.questions_count} вопросов`;
      const timeLabel = data.time_limit_seconds > 0 ? ` · общее время ${formatTime(data.time_limit_seconds * 1000)}` : ' · без ограничения времени';
      app.querySelector('.quiz-welcome-meta').textContent = `${countLabel}${timeLabel} · попыток: ${data.max_attempts}`;
      app.querySelector('[data-content="welcome-kicker"]').textContent = data.content.welcome_kicker;
      app.querySelector('[data-content="welcome-text"]').textContent = data.content.welcome_text;
      app.querySelector('[data-content="identity-text"]').textContent = data.content.identity_text;
      app.querySelector('[data-action="identify"]').textContent = data.content.start_button_text;
      const identityResponse = await fetch(`/api/quiz/identity?campaign=${encodeURIComponent(campaign)}`, { headers: { Accept: 'application/json' } });
      const identity = await readJson(identityResponse, 'Не удалось проверить сохранённый вход.');
      if (!identityResponse.ok) throw new Error(identity.error || 'Не удалось проверить сохранённый вход');
      if (identity.remembered) {
        showRemembered(identity);
        return;
      }
      if (identity.verified) showVerified(identity);
      if (identity.verified && identity.method === 'telegram' && app.dataset.telegramVerified === '1') {
        await startQuiz();
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
        startCountdown();
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

  async function startQuiz() {
    if (state.submitting) return;
    const activeUntil = Date.parse(app.dataset.activeUntil || '');
    if (Number.isFinite(activeUntil) && scheduledNow() >= activeUntil) {
      showEnded();
      return;
    }
    const values = identityValues();
    try { validateIdentity(values); } catch (error) { setIdentityError(error.message); return; }
    state.submitting = true;
    const button = identityForm.querySelector('[data-action="start"]');
    button.disabled = true;
    button.textContent = 'Открываем…';
    setIdentityError('');
    try {
      const data = await jsonRequest('/api/quiz/start', { campaign, referrer_id: app.dataset.referrer, source: app.dataset.source, ...values });
      state.attemptToken = data.attempt_token;
      state.questions = data.questions || [];
      state.answers = data.answers || {};
      state.index = Number(data.current_index || 0);
      state.deadline = data.deadline_at ? new Date(data.deadline_at).getTime() : null;
      state.timeLimit = Number(data.time_limit_seconds || 0);
      if (!state.questions.length) throw new Error('В квизе пока нет опубликованных вопросов');
      if (data.resumed) {
        const box = app.querySelector('.quiz-verified');
        box.textContent = 'Продолжаем ранее начатую попытку';
        box.hidden = false;
      }
      clearTelegramVerifiedFlag();
      renderQuestion();
      startTimer();
    } catch (error) {
      setIdentityError(error.message);
      if (/Квиз завершён/.test(error.message)) {
        showEnded();
      } else if (/Квиз начнётся/.test(error.message)) {
        app.dataset.scheduleState = 'upcoming';
        startCountdown();
      } else if (/исчерпан|успешно пройден/.test(error.message)) {
        app.querySelector('.quiz-error-message').textContent = errorText(error.message);
        show('error');
      } else {
        showIdentityMethods();
      }
    } finally {
      state.submitting = false;
      button.disabled = false;
      button.textContent = 'Продолжить';
    }
  }

  function currentQuestion() { return state.questions[state.index]; }
  function emptyAnswer(question) { return question.type === 'multi_choice' ? [] : ''; }
  function hasAnswer(question, value = state.answers[question.id]) { return Array.isArray(value) ? value.length > 0 : Boolean(String(value || '').trim()); }

  function renderQuestion() {
    const question = currentQuestion();
    const savedAnswer = state.answers[question.id] ?? emptyAnswer(question);
    app.querySelector('.quiz-step').textContent = `Вопрос ${state.index + 1} из ${state.questions.length}`;
    app.querySelector('.quiz-progress span').style.width = `${((state.index + 1) / state.questions.length) * 100}%`;
    const screen = app.querySelector('[data-screen="question"]');
    const section = question.section || {};
    screen.dataset.theme = section.theme || 'theory';
    const questionBackground = section.background_image || app.dataset.campaignBackground || '';
    setBackground(questionBackground);
    if (questionBackground) screen.style.setProperty('--section-background', `url("${questionBackground}")`);
    else screen.style.removeProperty('--section-background');
    const sectionLabel = app.querySelector('.quiz-section-label');
    sectionLabel.hidden = !section.title;
    sectionLabel.textContent = section.title || '';
    const questionImage = app.querySelector('.quiz-question-image');
    questionImage.hidden = !question.image_path;
    questionImage.src = question.image_path || '';
    questionImage.alt = question.image_path ? question.title : '';
    app.querySelector('.quiz-question-title').textContent = question.title;
    app.querySelector('[data-screen="question"] .quiz-validation').textContent = '';
    const options = app.querySelector('.quiz-options');
    options.replaceChildren();
    if (question.type === 'text') {
      const textarea = document.createElement('textarea');
      textarea.maxLength = 1000;
      textarea.placeholder = question.placeholder || 'Напиши свой ответ';
      textarea.value = savedAnswer;
      textarea.addEventListener('input', () => { state.answers[question.id] = textarea.value; });
      options.append(textarea);
    } else {
      (question.options || []).forEach((option) => {
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
    app.querySelector('[data-action="back"]').disabled = state.index === 0;
    app.querySelector('[data-action="next"]').textContent = state.index === state.questions.length - 1 ? 'Завершить тест' : 'Далее';
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
    state.submitting = true;
    const validation = app.querySelector('[data-screen="question"] .quiz-validation');
    app.querySelectorAll('[data-screen="question"] .quiz-actions button').forEach((button) => { button.disabled = true; });
    try {
      await saveCurrentAnswer({ requireAnswer: direction === 'next' });
      if (direction === 'back') state.index = Math.max(0, state.index - 1);
      else if (state.index < state.questions.length - 1) state.index += 1;
      else { await finishQuestions(); return; }
      renderQuestion();
    } catch (error) { validation.textContent = errorText(error.message); }
    finally { state.submitting = false; if (!state.finishing) { app.querySelector('[data-action="back"]').disabled = state.index === 0; app.querySelector('[data-action="next"]').disabled = false; } }
  }

  async function finishQuestions() {
    if (state.finishing) return;
    state.finishing = true; stopTimer();
    try { showResult(await jsonRequest('/api/quiz/finish', { attempt_token: state.attemptToken })); }
    catch (error) { app.querySelector('[data-screen="question"] .quiz-validation').textContent = errorText(error.message); state.finishing = false; startTimer(); }
  }

  function showResult(data) {
    const title = app.querySelector('.quiz-success-title'); const mark = app.querySelector('.quiz-success-mark');
    app.classList.remove('quiz-winner', 'quiz-not-won'); title.textContent = data.title || 'Спасибо!';
    if (data.outcome === 'won') { mark.textContent = '★'; app.classList.add('quiz-winner'); launchConfetti(); }
    else if (data.outcome === 'not_won') { mark.textContent = '♥'; app.classList.add('quiz-not-won'); }
    else mark.textContent = '✓';
    app.querySelector('.quiz-success-message').textContent = data.message || 'Твои ответы сохранены.';
    const score = app.querySelector('.quiz-score-message');
    if (data.max_correct_count > 0) { score.textContent = `Правильных ответов: ${data.correct_count} из ${data.max_correct_count}. Баллы: ${data.score} из ${data.max_score}.`; score.hidden = false; score.classList.toggle('passed', Boolean(data.passed)); } else score.hidden = true;
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

  app.querySelector('[data-action="identify"]').addEventListener('click', showIdentityMethods);
  app.querySelector('[data-action="remembered-confirm"]').addEventListener('click', confirmRememberedIdentity);
  app.querySelector('[data-action="remembered-forget"]').addEventListener('click', forgetRememberedIdentity);
  app.querySelector('[data-action="phone-identity"]').addEventListener('click', showPhoneIdentity);
  app.querySelector('[data-action="identity-methods"]').addEventListener('click', showIdentityMethods);
  identityForm.addEventListener('submit', (event) => { event.preventDefault(); startQuiz(); });
  app.querySelector('[data-action="back"]').addEventListener('click', () => navigate('back'));
  app.querySelector('[data-action="next"]').addEventListener('click', () => navigate('next'));
  app.querySelector('[data-action="retry"]').addEventListener('click', loadMeta);
  app.querySelector('[data-action="new-attempt"]').addEventListener('click', () => { state.finishing = false; startQuiz(); });
  app.querySelector('[data-action="share"]').addEventListener('click', shareQuiz);
  app.querySelector('[data-action="copy-share"]').addEventListener('click', copyShareLink);
  window.addEventListener('beforeunload', () => {
    stopTimer();
    if (state.scheduleTimer) window.clearInterval(state.scheduleTimer);
  });
  initialize();
})();
