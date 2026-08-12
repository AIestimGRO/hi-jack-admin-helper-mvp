document.querySelectorAll('.quick-values').forEach((group) => {
  group.querySelectorAll('button').forEach((button) => button.addEventListener('click', () => {
    document.getElementById(group.dataset.target).value = button.dataset.value;
  }));
});

document.querySelectorAll('.confirm-spend').forEach((button) => {
  button.addEventListener('click', (event) => {
    const amount = button.closest('form').querySelector('[name="amount"]').value;
    if (!window.confirm(`Списать ${amount} «${button.dataset.title}» у ${button.dataset.client}?`)) event.preventDefault();
  });
});

const dialog = document.getElementById('qr-dialog');
const qrButton = document.querySelector('.qr-open');
if (dialog && qrButton) {
  qrButton.addEventListener('click', () => {
    dialog.querySelector('img').src = qrButton.dataset.qrUrl;
    dialog.querySelector('strong').textContent = qrButton.dataset.phone;
    dialog.showModal();
  });
  dialog.querySelector('.dialog-close').addEventListener('click', () => dialog.close());
  dialog.addEventListener('click', (event) => { if (event.target === dialog) dialog.close(); });
}

const masterTabs = document.querySelectorAll('[data-master-tab]');
masterTabs.forEach((tab) => tab.addEventListener('click', () => {
  masterTabs.forEach((item) => item.classList.toggle('active', item === tab));
  document.querySelectorAll('[data-master-panel]').forEach((panel) => {
    panel.hidden = panel.dataset.masterPanel !== tab.dataset.masterTab;
  });
}));

const campaignTabs = [...document.querySelectorAll('[data-campaign-tab]')];
if (campaignTabs.length) {
  const campaignKinds = new Set(campaignTabs.map((tab) => tab.dataset.campaignTab));
  let selectedCampaignKind = 'classic';
  try {
    const saved = window.localStorage.getItem('hj-master-campaign-tab');
    if (campaignKinds.has(saved)) selectedCampaignKind = saved;
  } catch (_) {
    // The campaign switcher still works when browser storage is unavailable.
  }
  const showCampaignKind = (kind) => {
    campaignTabs.forEach((tab) => {
      const active = tab.dataset.campaignTab === kind;
      tab.classList.toggle('active', active);
      tab.setAttribute('aria-selected', active ? 'true' : 'false');
    });
    document.querySelectorAll('[data-campaign-kind]').forEach((item) => {
      item.hidden = item.dataset.campaignKind !== kind;
    });
    try {
      window.localStorage.setItem('hj-master-campaign-tab', kind);
    } catch (_) {
      // Ignore storage restrictions without breaking the switcher.
    }
  };
  campaignTabs.forEach((tab) => {
    tab.addEventListener('click', () => showCampaignKind(tab.dataset.campaignTab));
  });
  showCampaignKind(selectedCampaignKind);
}

document.querySelectorAll('.campaign-create').forEach((form) => {
  const typeSelect = form.elements.campaign_type;
  const jackcoinFields = form.querySelector('.campaign-jackcoin-fields');
  if (!typeSelect || !jackcoinFields) return;
  const syncCampaignType = () => {
    jackcoinFields.hidden = typeSelect.value !== 'daily_414';
  };
  typeSelect.addEventListener('change', syncCampaignType);
  syncCampaignType();
});

document.querySelectorAll('[data-final-prize-settings]').forEach((settings) => {
  const typeSelect = settings.querySelector('[name="final_prize_type"]');
  const cardField = settings.querySelector('[data-final-prize-card]');
  const jackcoinField = settings.querySelector('[data-final-prize-jackcoin]');
  if (!typeSelect || !cardField || !jackcoinField) return;
  const syncFinalPrize = () => {
    cardField.hidden = typeSelect.value !== 'reward_card';
    jackcoinField.hidden = typeSelect.value !== 'jackcoin';
  };
  typeSelect.addEventListener('change', syncFinalPrize);
  syncFinalPrize();
});

document.querySelectorAll('[data-row-href]').forEach((row) => {
  const openRow = () => window.location.assign(row.dataset.rowHref);
  row.addEventListener('click', (event) => {
    if (!event.target.closest('a, button, input, select, textarea')) openRow();
  });
  row.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      openRow();
    }
  });
});

const adminMenu = document.querySelector('[data-admin-menu]');
const adminMenuToggles = document.querySelectorAll('[data-admin-menu-toggle]');
const adminMenuClose = document.querySelector('[data-admin-menu-close]');
if (adminMenu && adminMenuToggles.length) {
  const setAdminMenuOpen = (open) => {
    adminMenu.hidden = !open;
    document.body.classList.toggle('admin-menu-open', open);
    adminMenuToggles.forEach((button) => button.setAttribute('aria-expanded', open ? 'true' : 'false'));
  };
  adminMenuToggles.forEach((button) => button.addEventListener('click', () => {
    setAdminMenuOpen(adminMenu.hidden);
  }));
  adminMenuClose?.addEventListener('click', () => setAdminMenuOpen(false));
  adminMenu.addEventListener('click', (event) => {
    if (event.target === adminMenu) setAdminMenuOpen(false);
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && !adminMenu.hidden) setAdminMenuOpen(false);
  });
}

const quizBuilder = document.querySelector('[data-quiz-builder]');
if (quizBuilder) {
  const campaignId = quizBuilder.dataset.campaignId;
  const defaultFinalQuestionSeconds = Number(quizBuilder.dataset.finalDefaultSeconds || 30);
  const csrfToken = quizBuilder.dataset.csrfToken;

  async function builderRequest(url, payload) {
    const response = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify({ csrf_token: csrfToken, ...payload }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || data.error || 'Не удалось сохранить изменения');
    return data;
  }

  function setFormBusy(form, busy) {
    form.querySelectorAll('button').forEach((button) => { button.disabled = busy; });
  }

  function finishBuilderAction(message, form) {
    const status = form?.querySelector('.form-status');
    if (status) {
      status.textContent = message;
      status.classList.remove('error');
      status.classList.add('success');
    }
    if (form) setFormBusy(form, false);
    if (window.HJAdminToast) window.HJAdminToast(message);
  }

  function optionLetter(index) {
    return index < 26 ? String.fromCharCode(65 + index) : `${index + 1}`;
  }

  function setupQuestionEditor(form, optionsBox, questionId = null) {
    const typeSelect = form.elements.question_type;
    const addOptionButton = form.querySelector('[data-action="add-option"]');
    const radioName = `correct-${questionId || 'new'}`;
    const visualSelect = form.elements.visual_type;
    const mediaInput = form.elements.image;
    const existingImage = form.elements.image_path?.value || '';
    const roundSelect = form.elements.game_round;
    const finalTimeField = form.querySelector('[data-final-question-time]');
    const finalTimeInput = form.elements.time_limit_seconds;

    function syncRound() {
      if (!roundSelect || !finalTimeField || !finalTimeInput) return;
      const isFinal = roundSelect.value === 'final';
      finalTimeField.hidden = !isFinal;
      finalTimeInput.required = isFinal;
      if (isFinal && Number(finalTimeInput.value || 0) < 5) {
        finalTimeInput.value = String(defaultFinalQuestionSeconds);
      }
    }
    roundSelect?.addEventListener('change', syncRound);
    syncRound();

    function syncVisual() {
      const needsImage = visualSelect.value !== 'standard';
      form.querySelector('[data-question-media]').hidden = !needsImage;
      if (needsImage) mediaInput.setAttribute('aria-required', existingImage ? 'false' : 'true');
    }
    visualSelect.addEventListener('change', syncVisual);
    syncVisual();

    const seeds = [...optionsBox.querySelectorAll('.hj-option-seed')].map((seed) => ({
      optionId: seed.dataset.optionId || '',
      optionCode: seed.dataset.optionCode || '',
      text: seed.dataset.text || '',
      correct: seed.dataset.correct === 'true',
    }));
    optionsBox.replaceChildren();

    function syncOptionLetters() {
      optionsBox.querySelectorAll('.quick-option-row').forEach((row, index) => {
        row.querySelector('.quick-option-letter').textContent = optionLetter(index);
      });
    }

    function addOption(text = '', correct = false, skipSync = false, optionId = '', optionCode = '') {
      const row = document.createElement('div');
      row.className = 'quick-option-row';
      row.dataset.optionId = optionId;
      row.dataset.optionCode = optionCode;
      const letter = document.createElement('span');
      letter.className = 'quick-option-letter';
      letter.setAttribute('aria-hidden', 'true');
      const correctLabel = document.createElement('label');
      correctLabel.className = 'quick-correct';
      correctLabel.title = 'Правильный ответ';
      const correctInput = document.createElement('input');
      correctInput.checked = correct;
      const correctMark = document.createElement('span');
      correctLabel.append(correctInput, correctMark);
      const textInput = document.createElement('input');
      textInput.type = 'text';
      textInput.maxLength = 300;
      textInput.placeholder = 'Вариант ответа';
      textInput.value = text;
      textInput.setAttribute('aria-label', 'Текст варианта ответа');
      textInput.addEventListener('keydown', (event) => {
        if (event.key !== 'Enter') return;
        event.preventDefault();
        const rows = [...optionsBox.querySelectorAll('.quick-option-row')];
        if (rows.at(-1) === row && textInput.value.trim()) addOption('', false);
        row.nextElementSibling?.querySelector('input[type="text"]')?.focus();
      });
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.className = 'remove-option-button';
      remove.textContent = '×';
      remove.title = 'Удалить вариант';
      remove.addEventListener('click', () => {
        if (optionsBox.children.length <= 2) {
          textInput.value = '';
          correctInput.checked = false;
        } else {
          row.remove();
        }
        syncOptionLetters();
        syncType();
      });
      row.append(letter, correctLabel, textInput, remove);
      optionsBox.append(row);
      syncOptionLetters();
      if (!skipSync) syncType();
    }

    function syncType() {
      const type = typeSelect.value;
      const isText = type === 'text';
      optionsBox.hidden = isText;
      addOptionButton.hidden = isText;
      form.querySelector('[data-text-answers]').hidden = !isText;
      form.querySelector('.correct-help').textContent = type === 'multi_choice'
        ? 'Можно отметить несколько правильных ответов.'
        : type === 'text' ? 'Укажите один или несколько допустимых правильных ответов.' : 'Выберите правильный вариант.';
      if (!isText && optionsBox.children.length < 2) {
        while (optionsBox.children.length < 2) addOption('', false, true);
      }
      let foundChecked = false;
      optionsBox.querySelectorAll('.quick-option-row').forEach((row) => {
        const input = row.querySelector('.quick-correct input');
        input.type = type === 'multi_choice' ? 'checkbox' : 'radio';
        input.name = radioName;
        if (type === 'single_choice' && input.checked) {
          if (foundChecked) input.checked = false;
          foundChecked = true;
        }
      });
    }

    (seeds.length ? seeds : [
      { text: '', correct: true }, { text: '', correct: false },
      { text: '', correct: false }, { text: '', correct: false },
    ]).forEach((item) => addOption(
      item.text, item.correct, true, item.optionId || '', item.optionCode || '',
    ));
    syncType();
    typeSelect.addEventListener('change', syncType);
    addOptionButton.addEventListener('click', () => {
      addOption('', false);
      optionsBox.lastElementChild.querySelector('input[type="text"]').focus();
    });

    function resetNewQuestionEditor() {
      form.reset();
      if (form.elements.image_path) form.elements.image_path.value = '';
      mediaInput.value = '';
      form.querySelector('[data-question-media] img')?.remove();
      optionsBox.replaceChildren();
      [true, false, false, false].forEach((correct) => addOption('', correct, true));
      syncType();
      syncRound();
      syncVisual();
    }

    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const status = form.querySelector('.form-status');
      status.textContent = '';
      status.classList.remove('error', 'success');
      const options = [...optionsBox.querySelectorAll('.quick-option-row')].map((row) => ({
        option_id: row.dataset.optionId || null,
        option_code: row.dataset.optionCode || null,
        text: row.querySelector('input[type="text"]').value,
        is_correct: row.querySelector('.quick-correct input').checked,
      }));
      const url = questionId
        ? `/api/master/quiz-questions/${questionId}/update-complete`
        : `/api/master/quiz-campaigns/${campaignId}/questions/create-complete`;
      setFormBusy(form, true);
      try {
        let imagePath = form.elements.image_path?.value || '';
        if (mediaInput.files[0]) {
          const mediaData = new FormData();
          mediaData.append('image', mediaInput.files[0]);
          mediaData.append('csrf_token', csrfToken);
          const response = await fetch(`/api/master/quiz-campaigns/${campaignId}/media`, {
            method: 'POST', body: mediaData, credentials: 'same-origin',
          });
          const uploaded = await response.json().catch(() => ({}));
          if (!response.ok) throw new Error(uploaded.detail || 'Не удалось загрузить изображение');
          imagePath = uploaded.path;
        }
        const gameRound = roundSelect?.value || 'main';
        const payload = {
          title: form.elements.title.value,
          question_type: typeSelect.value,
          game_round: gameRound,
          visual_type: visualSelect.value,
          image_path: imagePath,
          section_id: form.elements.section_id.value,
          options,
          accepted_text_answers: form.elements.accepted_text_answers.value,
          points: form.elements.points.value,
          time_limit_seconds: gameRound === 'final' ? finalTimeInput?.value : 0,
          required: form.elements.required.checked,
          placeholder: form.elements.placeholder.value,
        };
        const data = await builderRequest(url, payload);

        if (!questionId) {
          resetNewQuestionEditor();
          finishBuilderAction(data.message, form);
          return;
        }

        (data.options || []).forEach((option, index) => {
          const row = optionsBox.children[index];
          if (!row) return;
          row.dataset.optionId = option.db_id;
          row.dataset.optionCode = option.id;
        });
        if (imagePath) {
          form.elements.image_path.value = imagePath;
          let preview = form.querySelector('[data-question-media] img');
          if (!preview) {
            preview = document.createElement('img');
            preview.alt = 'Текущее изображение';
            form.querySelector('[data-question-media]').append(preview);
          }
          preview.src = imagePath;
        }
        mediaInput.value = '';
        form.dataset.gameRound = payload.game_round;
        const card = form.closest('[data-question-card]');
        const summary = card?.querySelector('.hj-question-summary strong');
        if (summary) summary.textContent = payload.title.trim();
        finishBuilderAction(data.message, form);
      } catch (error) {
        status.textContent = error.message;
        status.classList.add('error');
        setFormBusy(form, false);
        if (window.HJAdminToast) window.HJAdminToast(error.message, 'error');
      }
    });
  }

  const quickForm = quizBuilder.querySelector('#quick-question-form');
  setupQuestionEditor(quickForm, quickForm.querySelector('.quick-options'));
  quizBuilder.querySelectorAll('[data-existing-question-form]').forEach((form) => {
    setupQuestionEditor(form, form.querySelector('[data-existing-options]'), form.dataset.questionId);
  });

  const bulkForm = quizBuilder.querySelector('#bulk-question-form');
  bulkForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    const status = bulkForm.querySelector('.form-status');
    status.textContent = '';
    status.classList.remove('error', 'success');
    setFormBusy(bulkForm, true);
    try {
      const data = await builderRequest(`/api/master/quiz-campaigns/${campaignId}/questions/bulk-create`, {
        text: bulkForm.elements.bulk_text.value,
        game_round: bulkForm.elements.game_round?.value || 'main',
        points: 1,
        time_limit_seconds: 0,
      });
      bulkForm.elements.bulk_text.value = '';
      finishBuilderAction(data.message, bulkForm);
    } catch (error) {
      status.textContent = error.message;
      status.classList.add('error');
      setFormBusy(bulkForm, false);
      if (window.HJAdminToast) window.HJAdminToast(error.message, 'error');
    }
  });

  const questionCards = [...quizBuilder.querySelectorAll('[data-question-card]')];
  questionCards.forEach((card) => card.addEventListener('toggle', (event) => {
    if (event.target !== card || !card.open) return;
    questionCards.forEach((other) => { if (other !== card) other.open = false; });
  }));

  quizBuilder.querySelectorAll('.delete-question-form').forEach((form) => {
    form.addEventListener('submit', (event) => {
      if (!window.confirm(`Удалить вопрос «${form.dataset.questionTitle}»? Старые результаты участников сохранятся.`)) event.preventDefault();
    });
  });
}
