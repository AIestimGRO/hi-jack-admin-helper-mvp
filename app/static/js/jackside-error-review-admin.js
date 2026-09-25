(() => {
  const builder = document.querySelector('[data-quiz-builder]');
  if (!builder || builder.dataset.campaignType !== 'daily_414') return;
  if (window.HJJacksideErrorReviewAdminInstalled) return;
  window.HJJacksideErrorReviewAdminInstalled = true;

  const campaignId = builder.dataset.campaignId;
  const csrfToken = builder.dataset.csrfToken || '';
  const configUrl = `/api/master/quiz-campaigns/${encodeURIComponent(campaignId)}/error-review-config`;
  let config = null;
  let pendingNewQuestion = null;

  const toast = (message, kind = 'success') => {
    if (window.HJAdminToast) window.HJAdminToast(message, kind);
  };

  async function readJson(response) {
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || data.error || `Ошибка ${response.status}`);
    return data;
  }

  async function loadConfig() {
    config = await readJson(await fetch(configUrl, {
      headers: { Accept: 'application/json' },
      credentials: 'same-origin',
    }));
    return config;
  }

  async function postJson(url, payload) {
    return readJson(await fetch(url, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify({ csrf_token: csrfToken, ...payload }),
    }));
  }

  function addPriceCard() {
    if (builder.querySelector('[data-error-review-price-card]')) return;
    const hero = builder.querySelector('.hj-builder-hero');
    if (!hero) return;
    const section = document.createElement('section');
    section.className = 'card jackside-error-review-admin-card';
    section.dataset.errorReviewPriceCard = '1';
    section.innerHTML = `
      <div>
        <p class="eyebrow">После основного раунда</p>
        <h2>Разбор ошибок за JACKCOIN</h2>
        <p class="muted">Игрок, который не попал за финальный стол, сможет открыть свои неправильные ответы, правильные решения и комментарии мастера.</p>
      </div>
      <form data-error-review-price-form>
        <label>Стоимость разбора, JC
          <input name="price_jc" type="number" min="0" max="100000" step="1" inputmode="numeric" required>
          <small>По умолчанию 30 JC. Цена фиксируется в момент завершения основной части.</small>
        </label>
        <button class="button" type="submit">Сохранить стоимость</button>
        <span class="form-status" role="status"></span>
      </form>`;
    hero.insertAdjacentElement('afterend', section);

    const form = section.querySelector('[data-error-review-price-form]');
    const input = form.elements.price_jc;
    input.value = String(config?.price_jc ?? 30);
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const status = form.querySelector('.form-status');
      const button = form.querySelector('button');
      const value = Number(input.value);
      if (!Number.isInteger(value) || value < 0 || value > 100000) {
        status.textContent = 'Укажите целое значение от 0 до 100000 JC';
        status.className = 'form-status error';
        return;
      }
      button.disabled = true;
      status.textContent = 'Сохраняем…';
      status.className = 'form-status';
      try {
        const data = await postJson(
          `/api/master/quiz-campaigns/${encodeURIComponent(campaignId)}/error-review-price`,
          { price_jc: value },
        );
        config.price_jc = data.price_jc;
        input.value = String(data.price_jc);
        status.textContent = `Стоимость: ${data.price_jc} JC`;
        status.className = 'form-status success';
        toast(`Стоимость разбора сохранена: ${data.price_jc} JC`);
      } catch (error) {
        status.textContent = error.message;
        status.className = 'form-status error';
        toast(error.message, 'error');
      } finally {
        button.disabled = false;
      }
    });
  }

  function questionConfig(questionId) {
    return (config?.questions || []).find((item) => Number(item.id) === Number(questionId));
  }

  function addExplanationField(form, questionId = null) {
    if (form.querySelector('[data-error-review-explanation]')) return;
    const status = form.querySelector('.form-status');
    if (!status) return;
    const wrap = document.createElement('label');
    wrap.className = 'wide jackside-error-review-explanation';
    wrap.dataset.errorReviewExplanation = '1';
    wrap.innerHTML = `Комментарий к правильному ответу
      <textarea name="error_review_explanation" maxlength="4000" rows="4" placeholder="Объясните, почему правильный ответ именно такой"></textarea>
      <small>Игрок увидит этот текст только после покупки разбора ошибок. Для финального стола комментарий не используется.</small>`;
    status.insertAdjacentElement('beforebegin', wrap);
    const textarea = wrap.querySelector('textarea');
    if (questionId) textarea.value = questionConfig(questionId)?.explanation || '';

    const round = form.elements.game_round;
    const syncRound = () => {
      wrap.hidden = Boolean(round && round.value !== 'main');
    };
    round?.addEventListener('change', syncRound);
    syncRound();
  }

  async function saveExplanation(questionId, explanation) {
    return postJson(
      `/api/master/quiz-questions/${encodeURIComponent(questionId)}/error-review-explanation`,
      { explanation },
    );
  }

  function watchExistingForm(form) {
    const questionId = Number(form.dataset.questionId || form.closest('[data-question-id]')?.dataset.questionId || 0);
    if (!questionId) return;
    addExplanationField(form, questionId);
    let pending = null;
    let savingExplanation = false;

    form.addEventListener('submit', () => {
      if (form.elements.game_round && form.elements.game_round.value !== 'main') {
        pending = null;
        return;
      }
      pending = form.elements.error_review_explanation?.value.trim() || '';
    }, true);

    const status = form.querySelector('.form-status');
    const observer = new MutationObserver(async () => {
      if (savingExplanation || pending === null || !status.classList.contains('success')) return;
      const explanation = pending;
      pending = null;
      savingExplanation = true;
      try {
        await saveExplanation(questionId, explanation);
        const item = questionConfig(questionId);
        if (item) item.explanation = explanation;
        toast('Вопрос и комментарий для разбора сохранены');
      } catch (error) {
        status.textContent = `Вопрос сохранён, но комментарий не сохранён: ${error.message}`;
        status.classList.remove('success');
        status.classList.add('error');
        toast(error.message, 'error');
      } finally {
        savingExplanation = false;
      }
    });
    observer.observe(status, { childList: true, characterData: true, subtree: true, attributes: true });
  }

  function watchNewForm(form) {
    addExplanationField(form, null);
    const status = form.querySelector('.form-status');
    let resolving = false;

    form.addEventListener('submit', () => {
      const round = form.elements.game_round?.value || 'main';
      if (round !== 'main') {
        pendingNewQuestion = null;
        return;
      }
      pendingNewQuestion = {
        title: form.elements.title.value.trim(),
        explanation: form.elements.error_review_explanation?.value.trim() || '',
        knownIds: new Set((config?.questions || []).map((item) => Number(item.id))),
      };
    }, true);

    const observer = new MutationObserver(async () => {
      if (resolving || !pendingNewQuestion || !status.classList.contains('success')) return;
      resolving = true;
      const pending = pendingNewQuestion;
      pendingNewQuestion = null;
      try {
        const fresh = await loadConfig();
        const candidates = (fresh.questions || []).filter(
          (item) => !pending.knownIds.has(Number(item.id)) && item.game_round === 'main',
        );
        const exact = candidates.filter((item) => String(item.title || '').trim() === pending.title);
        const created = (exact.length ? exact : candidates).sort((a, b) => Number(b.id) - Number(a.id))[0];
        if (!created) throw new Error('Новый вопрос сохранён, но не удалось определить его для комментария');
        await saveExplanation(created.id, pending.explanation);
        const item = questionConfig(created.id);
        if (item) item.explanation = pending.explanation;
        toast('Вопрос и комментарий для разбора сохранены');
      } catch (error) {
        status.textContent = `Вопрос сохранён, но комментарий не сохранён: ${error.message}`;
        status.classList.remove('success');
        status.classList.add('error');
        toast(error.message, 'error');
      } finally {
        resolving = false;
      }
    });
    observer.observe(status, { childList: true, characterData: true, subtree: true, attributes: true });
  }

  function installStyle() {
    if (document.getElementById('jackside-error-review-admin-style')) return;
    const style = document.createElement('style');
    style.id = 'jackside-error-review-admin-style';
    style.textContent = `
      .jackside-error-review-admin-card { margin: 18px 0; display: grid; gap: 16px; }
      .jackside-error-review-admin-card h2 { margin: 4px 0 8px; }
      .jackside-error-review-admin-card form { display: flex; gap: 12px; align-items: end; flex-wrap: wrap; }
      .jackside-error-review-admin-card label { min-width: min(320px, 100%); flex: 1; }
      .jackside-error-review-admin-card input { width: 100%; }
      .jackside-error-review-explanation { margin-top: 10px; }
      .jackside-error-review-explanation textarea { width: 100%; resize: vertical; min-height: 92px; }
      .jackside-error-review-explanation[hidden] { display: none !important; }
    `;
    document.head.append(style);
  }

  async function init() {
    try {
      await loadConfig();
      installStyle();
      addPriceCard();
      builder.querySelectorAll('[data-existing-question-form]').forEach(watchExistingForm);
      const newForm = builder.querySelector('#quick-question-form');
      if (newForm) watchNewForm(newForm);
    } catch (error) {
      toast(`Не удалось загрузить настройки разбора ошибок: ${error.message}`, 'error');
    }
  }

  void init();
})();
