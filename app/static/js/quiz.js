(() => {
  const app = document.getElementById('quiz-app');
  if (!app) return;

  const campaign = app.dataset.campaign || 'default';
  const state = { meta: null, verifiedIdentity: null, attemptToken: null, questions: [], answers: {}, index: 0, deadline: null, timeLimit: 0, timer: null, submitting: false, finishing: false };
  const screens = [...app.querySelectorAll('[data-screen]')];
  const show = (name) => screens.forEach((screen) => screen.classList.toggle('active', screen.dataset.screen === name));
  const errorText = (message) => typeof message === 'string' ? message : 'Попробуй ещё раз чуть позже.';
  const identityForm = app.querySelector('.quiz-identity');

  async function jsonRequest(url, body) {
    const response = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'application/json' }, body: JSON.stringify(body) });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Не удалось выполнить действие');
    return data;
  }

  function identityValues() {
    const values = Object.fromEntries(new FormData(identityForm));
    return { phone: (values.phone || '').trim(), username: (values.username || '').trim(), name: (values.name || '').trim(), nickname: (values.nickname || '').trim() };
  }

  function validateIdentity(values) {
    if (state.verifiedIdentity?.verified && state.verifiedIdentity.method === 'telegram') return;
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
      const data = await response.json();
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
      const identity = await identityResponse.json();
      if (identity.verified) showVerified(identity);
      if (identity.verified && identity.method === 'telegram' && app.dataset.telegramVerified === '1') {
        await startQuiz();
        return;
      }
      show('welcome');
    } catch (error) {
      app.querySelector('.quiz-error-message').textContent = errorText(error.message);
      show('error');
    }
  }

  function showVerified(identity) {
    state.verifiedIdentity = identity;
    const box = app.querySelector('.quiz-verified');
    const label = identity.method === 'telegram' ? `Telegram подтверждён${identity.username ? `: @${identity.username}` : ''}` : `Email подтверждён${identity.email ? `: ${identity.email}` : ''}`;
    box.textContent = `✓ ${label}`;
    box.hidden = false;
  }

  function setIdentityError(message) { app.querySelector('.quiz-identity-error').textContent = message ? errorText(message) : ''; }

  function clearTelegramVerifiedFlag() {
    if (app.dataset.telegramVerified !== '1') return;
    app.dataset.telegramVerified = '0';
    const url = new URL(window.location.href);
    url.searchParams.delete('telegram_verified');
    window.history.replaceState({}, '', `${url.pathname}${url.search}${url.hash}`);
  }

  async function startQuiz() {
    if (state.submitting) return;
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
      if (/исчерпан|успешно пройден/.test(error.message)) {
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
    show('success');
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
  app.querySelector('[data-action="phone-identity"]').addEventListener('click', showPhoneIdentity);
  app.querySelector('[data-action="identity-methods"]').addEventListener('click', showIdentityMethods);
  identityForm.addEventListener('submit', (event) => { event.preventDefault(); startQuiz(); });
  app.querySelector('[data-action="back"]').addEventListener('click', () => navigate('back'));
  app.querySelector('[data-action="next"]').addEventListener('click', () => navigate('next'));
  app.querySelector('[data-action="retry"]').addEventListener('click', loadMeta);
  app.querySelector('[data-action="new-attempt"]').addEventListener('click', () => { state.finishing = false; startQuiz(); });
  window.addEventListener('beforeunload', stopTimer);
  loadMeta();
})();
