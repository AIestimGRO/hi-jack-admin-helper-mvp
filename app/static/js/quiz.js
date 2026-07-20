(() => {
  const app = document.getElementById('quiz-app');
  if (!app) return;

  const campaign = app.dataset.campaign || 'default';
  const state = {
    attemptToken: null,
    questions: [],
    answers: {},
    index: 0,
    deadline: null,
    timeLimit: 0,
    timer: null,
    submitting: false,
    finishing: false,
  };
  const screens = [...app.querySelectorAll('[data-screen]')];
  const show = (name) => screens.forEach((screen) => screen.classList.toggle('active', screen.dataset.screen === name));
  const errorText = (message) => typeof message === 'string' ? message : 'Попробуй ещё раз чуть позже.';

  async function jsonRequest(url, body) {
    const response = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Не удалось выполнить действие');
    return data;
  }

  async function loadMeta() {
    stopTimer();
    try {
      const response = await fetch(`/api/quiz/questions?campaign=${encodeURIComponent(campaign)}`, { headers: { Accept: 'application/json' } });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'Не удалось загрузить квиз');
      const countLabel = data.questions_count === 1 ? '1 вопрос' : `${data.questions_count} вопросов`;
      const timeLabel = data.time_limit_seconds > 0 ? ` · общее время ${formatTime(data.time_limit_seconds * 1000)}` : ' · без ограничения времени';
      app.querySelector('.quiz-welcome-meta').textContent = countLabel + timeLabel;
      show('welcome');
    } catch (error) {
      app.querySelector('.quiz-error-message').textContent = errorText(error.message);
      show('error');
    }
  }

  async function startQuiz() {
    const button = app.querySelector('[data-action="start"]');
    button.disabled = true;
    button.textContent = 'Начинаем…';
    try {
      const data = await jsonRequest('/api/quiz/start', { campaign });
      state.attemptToken = data.attempt_token;
      state.questions = data.questions || [];
      state.answers = {};
      state.index = 0;
      state.deadline = data.deadline_at ? new Date(data.deadline_at).getTime() : null;
      state.timeLimit = Number(data.time_limit_seconds || 0);
      if (!state.questions.length) throw new Error('В квизе пока нет опубликованных вопросов');
      renderQuestion();
      startTimer();
    } catch (error) {
      app.querySelector('.quiz-error-message').textContent = errorText(error.message);
      show('error');
    } finally {
      button.disabled = false;
      button.textContent = 'Начать';
    }
  }

  function currentQuestion() {
    return state.questions[state.index];
  }

  function emptyAnswer(question) {
    return question.type === 'multi_choice' ? [] : '';
  }

  function hasAnswer(question, value = state.answers[question.id]) {
    return Array.isArray(value) ? value.length > 0 : Boolean(String(value || '').trim());
  }

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
        const label = document.createElement('label');
        label.className = 'quiz-option';
        const input = document.createElement('input');
        input.type = question.type === 'multi_choice' ? 'checkbox' : 'radio';
        input.name = question.id;
        input.value = option.id;
        input.checked = Array.isArray(savedAnswer) ? savedAnswer.includes(option.id) : savedAnswer === option.id;
        label.classList.toggle('selected', input.checked);
        input.addEventListener('change', () => {
          if (question.type === 'multi_choice') {
            state.answers[question.id] = [...options.querySelectorAll('input:checked')].map((item) => item.value);
          } else {
            state.answers[question.id] = option.id;
          }
          options.querySelectorAll('.quiz-option').forEach((item) => item.classList.toggle('selected', item.querySelector('input').checked));
        });
        const marker = document.createElement('span');
        const text = document.createElement('strong');
        text.textContent = option.text;
        label.append(input, marker, text);
        options.append(label);
      });
    }
    const back = app.querySelector('[data-action="back"]');
    const next = app.querySelector('[data-action="next"]');
    back.disabled = state.index === 0;
    next.textContent = state.index === state.questions.length - 1 ? 'Завершить тест' : 'Далее';
    show('question');
  }

  async function saveCurrentAnswer({ requireAnswer = false } = {}) {
    const question = currentQuestion();
    const answer = state.answers[question.id] ?? emptyAnswer(question);
    if (!hasAnswer(question, answer)) {
      if (requireAnswer && question.required) throw new Error('Выбери ответ, чтобы продолжить');
      if (!Object.prototype.hasOwnProperty.call(state.answers, question.id)) return;
    }
    await jsonRequest('/api/quiz/answer', {
      attempt_token: state.attemptToken,
      question_id: question.id,
      answer,
    });
  }

  async function navigate(direction) {
    if (state.submitting || state.finishing) return;
    state.submitting = true;
    const validation = app.querySelector('[data-screen="question"] .quiz-validation');
    const buttons = app.querySelectorAll('[data-screen="question"] .quiz-actions button');
    buttons.forEach((button) => { button.disabled = true; });
    try {
      await saveCurrentAnswer({ requireAnswer: direction === 'next' });
      if (direction === 'back') {
        state.index = Math.max(0, state.index - 1);
        renderQuestion();
      } else if (state.index < state.questions.length - 1) {
        state.index += 1;
        renderQuestion();
      } else {
        await finishQuestions();
      }
    } catch (error) {
      validation.textContent = errorText(error.message);
    } finally {
      state.submitting = false;
      if (!state.finishing) {
        app.querySelector('[data-action="back"]').disabled = state.index === 0;
        app.querySelector('[data-action="next"]').disabled = false;
      }
    }
  }

  async function finishQuestions() {
    if (state.finishing) return;
    state.finishing = true;
    stopTimer();
    try {
      await jsonRequest('/api/quiz/finish', { attempt_token: state.attemptToken });
      show('contact');
    } catch (error) {
      app.querySelector('[data-screen="question"] .quiz-validation').textContent = errorText(error.message);
      state.finishing = false;
      startTimer();
    }
  }

  function formatTime(milliseconds) {
    const totalSeconds = Math.max(0, Math.ceil(milliseconds / 1000));
    const minutes = Math.floor(totalSeconds / 60);
    const seconds = String(totalSeconds % 60).padStart(2, '0');
    return `${minutes}:${seconds}`;
  }

  function startTimer() {
    stopTimer();
    const timer = app.querySelector('.quiz-timer');
    if (!state.deadline || state.timeLimit <= 0) {
      timer.hidden = true;
      return;
    }
    timer.hidden = false;
    const number = timer.querySelector('strong');
    const bar = timer.querySelector('span');
    const tick = async () => {
      const remaining = Math.max(0, state.deadline - Date.now());
      number.textContent = formatTime(remaining);
      bar.style.width = `${Math.min(100, (remaining / (state.timeLimit * 1000)) * 100)}%`;
      timer.classList.toggle('urgent', remaining <= 15000);
      if (remaining <= 0 && !state.finishing) {
        stopTimer();
        try { await saveCurrentAnswer(); } catch (_) { /* deadline may already be closed */ }
        await finishQuestions();
      }
    };
    tick();
    state.timer = window.setInterval(tick, 200);
  }

  function stopTimer() {
    if (state.timer) window.clearInterval(state.timer);
    state.timer = null;
  }

  function launchConfetti() {
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    const layer = document.createElement('div');
    layer.className = 'confetti-layer';
    layer.setAttribute('aria-hidden', 'true');
    const colors = ['#e62c45', '#ffcf5a', '#2fce85', '#ffffff', '#a77bff'];
    for (let index = 0; index < 70; index += 1) {
      const piece = document.createElement('i');
      piece.style.setProperty('--x', `${Math.random() * 100}vw`);
      piece.style.setProperty('--delay', `${Math.random() * 0.8}s`);
      piece.style.setProperty('--duration', `${2.2 + Math.random() * 1.8}s`);
      piece.style.setProperty('--rotate', `${Math.random() * 720 - 360}deg`);
      piece.style.setProperty('--drift', `${Math.random() * 180 - 90}px`);
      piece.style.background = colors[index % colors.length];
      layer.append(piece);
    }
    document.body.append(layer);
    window.setTimeout(() => layer.remove(), 5000);
  }

  app.querySelector('[data-action="start"]').addEventListener('click', startQuiz);
  app.querySelector('[data-action="back"]').addEventListener('click', () => navigate('back'));
  app.querySelector('[data-action="next"]').addEventListener('click', () => navigate('next'));
  app.querySelector('[data-action="retry"]').addEventListener('click', () => { show('loading'); loadMeta(); });

  app.querySelector('.quiz-contact').addEventListener('submit', async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const validation = form.querySelector('.quiz-validation');
    const submit = form.querySelector('[type="submit"]');
    const values = Object.fromEntries(new FormData(form));
    if (!values.phone.trim()) { validation.textContent = 'Укажи номер телефона'; return; }
    if (![values.name, values.nickname, values.username].some((value) => value.trim())) { validation.textContent = 'Укажи имя, никнейм или Telegram username'; return; }
    validation.textContent = '';
    submit.disabled = true;
    submit.textContent = 'Сохраняем…';
    try {
      const data = await jsonRequest('/api/quiz/submit', {
        attempt_token: state.attemptToken,
        referrer_id: app.dataset.referrer,
        source: app.dataset.source,
        ...values,
      });
      const title = app.querySelector('.quiz-success-title');
      const mark = app.querySelector('.quiz-success-mark');
      app.classList.remove('quiz-winner', 'quiz-not-won');
      if (data.outcome === 'won') {
        title.textContent = 'Поздравляем!';
        mark.textContent = '★';
        app.classList.add('quiz-winner');
        launchConfetti();
      } else if (data.outcome === 'not_won') {
        title.textContent = 'Не расстраивайтесь';
        mark.textContent = '♥';
        app.classList.add('quiz-not-won');
      } else {
        title.textContent = 'Спасибо!';
        mark.textContent = '✓';
      }
      app.querySelector('.quiz-success-message').textContent = data.message || 'Твои ответы сохранены.';
      const score = app.querySelector('.quiz-score-message');
      if (data.max_correct_count > 0) {
        score.textContent = `Правильных ответов: ${data.correct_count} из ${data.max_correct_count}. Баллы: ${data.score} из ${data.max_score}.`;
        score.hidden = false;
        score.classList.toggle('passed', Boolean(data.passed));
      } else {
        score.hidden = true;
      }
      const bonus = app.querySelector('.quiz-bonus-message');
      bonus.textContent = data.bonus_message || '';
      bonus.hidden = !data.bonus_message;
      show('success');
    } catch (error) {
      validation.textContent = errorText(error.message);
      submit.disabled = false;
      submit.textContent = 'Отправить';
    }
  });

  window.addEventListener('beforeunload', stopTimer);
  loadMeta();
})();
