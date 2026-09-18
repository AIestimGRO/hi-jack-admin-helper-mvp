(() => {
  const app = document.getElementById('quiz-app');
  if (!app || app.dataset.campaignType !== 'daily_414') return;
  if (window.HJJacksideErrorReviewInstalled) return;
  window.HJJacksideErrorReviewInstalled = true;

  const campaign = app.dataset.campaign || 'default';
  let statusState = null;
  let reviewState = null;
  let reviewIndex = 0;
  let statusLoading = false;

  async function readJson(response) {
    const type = response.headers.get('content-type') || '';
    if (!type.includes('application/json')) {
      throw new Error('Не удалось проверить доступ к разбору');
    }
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || data.error || `Ошибка ${response.status}`);
    return data;
  }

  async function fetchStatus() {
    const response = await fetch(
      `/api/quiz/error-review/status?campaign=${encodeURIComponent(campaign)}`,
      { headers: { Accept: 'application/json' }, credentials: 'same-origin' },
    );
    return readJson(response);
  }

  function installStyle() {
    if (document.getElementById('jackside-error-review-style')) return;
    const style = document.createElement('style');
    style.id = 'jackside-error-review-style';
    style.textContent = `
      .jackside-review-offer { margin-top: 18px; padding: 18px; border: 1px solid rgba(244,213,107,.38); border-radius: 18px; background: linear-gradient(145deg, rgba(244,213,107,.12), rgba(8,28,25,.88)); text-align: left; }
      .jackside-review-offer h3 { margin: 4px 0 8px; font-size: 1.18rem; }
      .jackside-review-offer p { margin: 0 0 10px; color: rgba(255,255,255,.76); }
      .jackside-review-offer .jackside-review-meta { display: flex; gap: 10px; flex-wrap: wrap; margin: 12px 0; }
      .jackside-review-offer .jackside-review-meta span { padding: 7px 10px; border-radius: 999px; background: rgba(255,255,255,.08); font-size: .9rem; }
      .jackside-review-offer .quiz-validation { margin-top: 10px; }
      .jackside-review-shell { width: 100%; }
      .jackside-review-progress { margin: 0 0 14px; color: rgba(255,255,255,.62); font-size: .9rem; }
      .jackside-review-card { width: 100%; padding: 0; text-align: left; }
      .jackside-review-image { display: block; width: 100%; max-height: 310px; object-fit: contain; border-radius: 16px; margin: 14px 0; background: rgba(0,0,0,.2); }
      .jackside-review-options { display: grid; gap: 10px; margin: 18px 0; }
      .jackside-review-option { display: grid; grid-template-columns: 26px 1fr; gap: 10px; align-items: start; padding: 13px 14px; border: 1px solid rgba(255,255,255,.12); border-radius: 14px; background: rgba(255,255,255,.045); }
      .jackside-review-option strong { display: block; }
      .jackside-review-option small { display: block; margin-top: 4px; color: rgba(255,255,255,.66); }
      .jackside-review-option.is-correct { border-color: rgba(72,210,151,.68); background: rgba(40,169,112,.16); }
      .jackside-review-option.is-wrong { border-color: rgba(245,92,92,.66); background: rgba(207,54,54,.16); }
      .jackside-review-option .jackside-review-mark { font-weight: 800; line-height: 1.3; }
      .jackside-review-option.is-correct .jackside-review-mark { color: #70e7ae; }
      .jackside-review-option.is-wrong .jackside-review-mark { color: #ff8080; }
      .jackside-review-explanation { margin-top: 16px; padding: 15px 16px; border-radius: 16px; background: rgba(244,213,107,.1); border: 1px solid rgba(244,213,107,.25); }
      .jackside-review-explanation strong { display: block; margin-bottom: 7px; color: #f4d56b; }
      .jackside-review-explanation p { margin: 0; white-space: pre-wrap; line-height: 1.5; }
      .jackside-review-actions { display: flex; gap: 10px; margin-top: 18px; }
      .jackside-review-actions button, .jackside-review-actions a { flex: 1; }
      @media (max-width: 520px) { .jackside-review-actions { flex-direction: column; } }
    `;
    document.head.append(style);
  }

  function successScreen() {
    return app.querySelector('[data-screen="success"]');
  }

  function removeOffer() {
    app.querySelectorAll('[data-error-review-offer]').forEach((node) => node.remove());
  }

  function offerHost() {
    const success = successScreen();
    if (success?.classList.contains('active')) return success;
    const ended = app.querySelector('[data-screen="ended"]');
    if (ended?.classList.contains('active')) return ended;
    return null;
  }

  function renderOffer(status) {
    removeOffer();
    const host = offerHost();
    if (!host || !status?.eligible) return;
    const box = document.createElement('section');
    box.className = 'jackside-review-offer';
    box.dataset.errorReviewOffer = '1';
    const wrongLabel = status.wrong_count === 1 ? '1 ошибка' : `${status.wrong_count} ошибок`;
    const buttonText = status.purchased
      ? 'Открыть разбор ошибок'
      : `Разобрать ошибки — ${status.price_jc} JC`;
    box.innerHTML = `
      <p class="quiz-kicker">После игры</p>
      <h3>Разбор неправильных ответов</h3>
      <p>Посмотри свои ошибки, правильные решения и комментарии к каждому вопросу.</p>
      <div class="jackside-review-meta">
        <span>${wrongLabel}</span>
        <span>Баланс: ${status.balance_jc} JC</span>
      </div>
      <button class="quiz-primary" type="button" data-error-review-open>${buttonText}</button>
      <p class="quiz-validation" data-error-review-message role="alert"></p>`;
    host.append(box);

    const button = box.querySelector('[data-error-review-open]');
    const message = box.querySelector('[data-error-review-message]');
    if (!status.purchased && !status.can_purchase) {
      button.disabled = true;
      message.textContent = `Для разбора нужно ${status.price_jc} JC. Сейчас доступно ${status.balance_jc} JC.`;
    }
    button.addEventListener('click', async () => {
      button.disabled = true;
      message.textContent = '';
      try {
        if (!statusState.purchased) {
          const purchased = await readJson(await fetch('/api/quiz/error-review/purchase', {
            method: 'POST',
            credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
            body: JSON.stringify({
              campaign,
              submission_id: statusState.submission_id,
            }),
          }));
          statusState = {
            ...statusState,
            purchased: true,
            can_purchase: false,
            balance_jc: purchased.balance_jc,
          };
        }
        await openReview();
      } catch (error) {
        message.textContent = error.message || 'Не удалось открыть разбор';
        button.disabled = false;
      }
    });
  }

  function ensureReviewScreen() {
    let screen = app.querySelector('[data-screen="error-review"]');
    if (screen) return screen;
    screen = document.createElement('section');
    screen.className = 'quiz-screen daily-414-screen jackside-review-shell';
    screen.dataset.screen = 'error-review';
    screen.innerHTML = `
      <p class="quiz-kicker">Разбор ошибок</p>
      <p class="jackside-review-progress" data-review-progress></p>
      <div class="jackside-review-card" data-review-card></div>
      <div class="jackside-review-actions">
        <button class="quiz-secondary" type="button" data-review-prev>← Назад</button>
        <button class="quiz-primary" type="button" data-review-next>Следующая ошибка</button>
      </div>`;
    app.append(screen);
    screen.querySelector('[data-review-prev]').addEventListener('click', () => {
      if (reviewIndex > 0) {
        reviewIndex -= 1;
        renderReviewQuestion();
      }
    });
    screen.querySelector('[data-review-next]').addEventListener('click', () => {
      if (!reviewState?.questions?.length) return;
      if (reviewIndex < reviewState.questions.length - 1) {
        reviewIndex += 1;
        renderReviewQuestion();
        return;
      }
      window.location.assign('/account?tab=stats');
    });
    return screen;
  }

  function optionHtml(option) {
    const classes = ['jackside-review-option'];
    let note = '';
    let mark = '•';
    if (option.correct) {
      classes.push('is-correct');
      mark = '✓';
      note = option.selected ? 'Правильный ответ · ваш выбор' : 'Правильный ответ';
    } else if (option.selected) {
      classes.push('is-wrong');
      mark = '×';
      note = 'Ваш ответ';
    }
    const holder = document.createElement('div');
    holder.className = classes.join(' ');
    const marker = document.createElement('span');
    marker.className = 'jackside-review-mark';
    marker.textContent = mark;
    const copy = document.createElement('div');
    const strong = document.createElement('strong');
    strong.textContent = option.text || '—';
    copy.append(strong);
    if (note) {
      const small = document.createElement('small');
      small.textContent = note;
      copy.append(small);
    }
    holder.append(marker, copy);
    return holder;
  }

  function renderReviewQuestion() {
    const screen = ensureReviewScreen();
    const questions = reviewState?.questions || [];
    const question = questions[reviewIndex];
    if (!question) return;
    screen.querySelector('[data-review-progress]').textContent = `Ошибка ${reviewIndex + 1} из ${questions.length}`;
    const card = screen.querySelector('[data-review-card]');
    card.replaceChildren();

    const title = document.createElement('h2');
    title.textContent = question.title || 'Вопрос';
    card.append(title);
    if (question.image_path) {
      const image = document.createElement('img');
      image.className = 'jackside-review-image';
      image.src = question.image_path;
      image.alt = question.title || '';
      card.append(image);
    }

    const options = document.createElement('div');
    options.className = 'jackside-review-options';
    if (question.type === 'text') {
      options.append(optionHtml({ text: question.user_answer || 'Нет ответа', selected: true, correct: false }));
      (question.correct_answers || []).forEach((answer) => {
        options.append(optionHtml({ text: answer, selected: false, correct: true }));
      });
    } else {
      (question.options || []).forEach((option) => options.append(optionHtml(option)));
    }
    card.append(options);

    const explanation = document.createElement('section');
    explanation.className = 'jackside-review-explanation';
    const heading = document.createElement('strong');
    heading.textContent = 'Почему так?';
    const text = document.createElement('p');
    text.textContent = question.explanation || 'Комментарий к этому вопросу пока не добавлен.';
    explanation.append(heading, text);
    card.append(explanation);

    const prev = screen.querySelector('[data-review-prev]');
    const next = screen.querySelector('[data-review-next]');
    prev.disabled = reviewIndex === 0;
    next.textContent = reviewIndex === questions.length - 1 ? 'Завершить разбор' : 'Следующая ошибка';
  }

  async function openReview() {
    const submissionId = statusState?.submission_id;
    if (!submissionId) throw new Error('Не найден результат для разбора');
    const response = await fetch(
      `/api/quiz/error-review?campaign=${encodeURIComponent(campaign)}&submission_id=${encodeURIComponent(submissionId)}`,
      { headers: { Accept: 'application/json' }, credentials: 'same-origin' },
    );
    reviewState = await readJson(response);
    if (!reviewState.questions?.length) throw new Error('Неправильных ответов для разбора не найдено');
    reviewIndex = 0;
    app.querySelectorAll('[data-screen]').forEach((screen) => screen.classList.remove('active'));
    const screen = ensureReviewScreen();
    screen.classList.add('active');
    renderReviewQuestion();
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  async function refreshOffer() {
    if (statusLoading || !offerHost()) return;
    statusLoading = true;
    try {
      const status = await fetchStatus();
      statusState = status;
      renderOffer(status);
    } catch (_) {
      // The quiz itself must stay usable even when the optional review status fails.
    } finally {
      statusLoading = false;
    }
  }

  function watchScreens() {
    const observer = new MutationObserver(() => {
      if (offerHost()) void refreshOffer();
    });
    app.querySelectorAll('[data-screen]').forEach((screen) => {
      observer.observe(screen, { attributes: true, attributeFilter: ['class'] });
    });
    if (offerHost()) void refreshOffer();
  }

  installStyle();
  watchScreens();
})();
