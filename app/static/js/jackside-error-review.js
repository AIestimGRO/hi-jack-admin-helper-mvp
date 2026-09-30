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
      .jackside-review-offer [data-error-review-open] { display: block; width: min(100%, 360px); margin: 18px auto 0; }
      .jackside-review-offer .quiz-validation { margin-top: 10px; text-align: center; }

      .quiz-screen[data-screen="error-review"] { justify-content: flex-start; padding: 28px 28px 88px; border: 1px solid #173e3a; border-radius: 24px; background: linear-gradient(155deg, rgba(10,27,24,.48), rgba(4,11,10,.66)); backdrop-filter: blur(2px); box-shadow: 0 24px 60px rgba(0,0,0,.24); }
      .quiz-screen[data-screen="error-review"] .quiz-progress { margin-bottom: 20px; }
      .quiz-screen[data-screen="error-review"] .quiz-question-title { font-size: clamp(19px,5.3vw,28px); line-height: 1.22; letter-spacing: -.4px; margin-bottom: 14px; }
      .quiz-screen[data-screen="error-review"] .quiz-section-label { align-self: center; text-align: center; }
      .jackside-review-image { display: block; width: auto; max-width: 100%; height: auto; max-height: min(42vh,320px); margin: 0; padding: 0; border: 0; border-radius: 10px; background: transparent; box-shadow: none; object-fit: contain; }
      .jackside-review-options { display: grid; gap: 10px; margin-bottom: 8px; }
      .jackside-review-option { cursor: default; }
      .jackside-review-option:hover { border-color: #3b6c65; background: rgba(7,17,15,.42); }
      .jackside-review-option > div { min-width: 0; }
      .jackside-review-option strong { display: block; overflow-wrap: anywhere; }
      .jackside-review-option small { display: block; margin-top: 4px; font-size: 12px; font-weight: 750; }
      .jackside-review-option.is-wrong { border-color: rgba(255,93,111,.88); background: rgba(173,31,50,.28); box-shadow: inset 0 0 0 1px rgba(255,93,111,.12); }
      .jackside-review-option.is-wrong:hover { border-color: rgba(255,93,111,.88); background: rgba(173,31,50,.28); }
      .jackside-review-option.is-wrong > span { border-color: #ff6577; background: #e62c45; box-shadow: inset 0 0 0 5px #e62c45; }
      .jackside-review-option.is-wrong small { color: #ff9aa7; }
      .jackside-review-option.is-correct { border-color: rgba(67,222,160,.82); background: rgba(24,139,96,.25); box-shadow: inset 0 0 0 1px rgba(67,222,160,.1); }
      .jackside-review-option.is-correct:hover { border-color: rgba(67,222,160,.82); background: rgba(24,139,96,.25); }
      .jackside-review-option.is-correct > span { border-color: #55dfa8; background: #2fce85; box-shadow: inset 0 0 0 5px #2fce85; }
      .jackside-review-option.is-correct small { color: #8aebba; }
      .jackside-review-option:not(.is-wrong):not(.is-correct) { opacity: .68; }
      .jackside-review-explanation { margin-top: 18px; padding: 16px; border: 1px solid rgba(82,198,201,.35); border-radius: 14px; background: rgba(0,105,133,.12); }
      .jackside-review-explanation strong { display: block; margin-bottom: 7px; color: #8de4e4; font-size: 13px; letter-spacing: .25px; }
      .jackside-review-explanation p { margin: 0; color: #e7f5f3; white-space: pre-wrap; line-height: 1.5; }
      .jackside-review-actions { display: grid; grid-template-columns: auto 1fr; gap: 10px; margin-top: 28px; }
      .jackside-review-actions .quiz-secondary { min-width: 112px; }
      @media (max-width: 420px) {
        .quiz-screen[data-screen="error-review"] { width: 100%; min-height: calc(100dvh - 92px); padding: 20px 15px 82px; border-radius: 18px; }
        .jackside-review-actions { grid-template-columns: 1fr 1.45fr; }
        .jackside-review-actions .quiz-secondary { min-width: 0; }
      }
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
    screen.className = 'quiz-screen jackside-review-shell';
    screen.dataset.screen = 'error-review';
    screen.innerHTML = `
      <div class="quiz-progress"><span data-review-progress-bar></span></div>
      <p class="quiz-step" data-review-progress></p>
      <p class="quiz-section-label">Разбор ошибок</p>
      <figure class="quiz-question-media" data-review-media hidden>
        <img class="quiz-question-image jackside-review-image" data-review-image alt="">
      </figure>
      <h2 class="quiz-question-title" data-review-title></h2>
      <div class="quiz-options jackside-review-options" data-review-options></div>
      <section class="jackside-review-explanation" data-review-explanation>
        <strong>Комментарий к правильному ответу</strong>
        <p></p>
      </section>
      <div class="quiz-actions jackside-review-actions">
        <button class="quiz-secondary" type="button" data-review-prev>Назад</button>
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

  function optionNode(option) {
    const holder = document.createElement('div');
    holder.className = 'quiz-option jackside-review-option';
    if (option.correct) holder.classList.add('is-correct');
    else if (option.selected) holder.classList.add('is-wrong');

    const marker = document.createElement('span');
    const copy = document.createElement('div');
    const strong = document.createElement('strong');
    strong.textContent = option.text || '—';
    copy.append(strong);

    if (option.correct || option.selected) {
      const note = document.createElement('small');
      note.textContent = option.correct ? 'Правильный ответ' : 'Ваш ответ';
      copy.append(note);
    }

    holder.append(marker, copy);
    return holder;
  }

  function renderReviewQuestion() {
    const screen = ensureReviewScreen();
    const questions = reviewState?.questions || [];
    const question = questions[reviewIndex];
    if (!question) return;

    const progress = screen.querySelector('[data-review-progress]');
    progress.textContent = `Ошибка ${reviewIndex + 1} из ${questions.length}`;
    const progressBar = screen.querySelector('[data-review-progress-bar]');
    progressBar.style.width = `${((reviewIndex + 1) / questions.length) * 100}%`;

    const media = screen.querySelector('[data-review-media]');
    const image = screen.querySelector('[data-review-image]');
    media.hidden = !question.image_path;
    image.src = question.image_path || '';
    image.alt = question.image_path ? (question.title || '') : '';

    screen.querySelector('[data-review-title]').textContent = question.title || 'Вопрос';
    const options = screen.querySelector('[data-review-options]');
    options.replaceChildren();

    if (question.type === 'text') {
      options.append(optionNode({
        text: question.user_answer || 'Нет ответа',
        selected: true,
        correct: false,
      }));
      (question.correct_answers || []).forEach((answer) => {
        options.append(optionNode({ text: answer, selected: false, correct: true }));
      });
    } else {
      (question.options || []).forEach((option) => options.append(optionNode(option)));
    }

    const explanation = screen.querySelector('[data-review-explanation] p');
    explanation.textContent = question.explanation || 'Комментарий к этому вопросу пока не добавлен.';

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
