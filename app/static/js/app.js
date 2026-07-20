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

const quizBuilder = document.querySelector('[data-quiz-builder]');
if (quizBuilder) {
  const campaignId = quizBuilder.dataset.campaignId;
  const csrfToken = quizBuilder.dataset.csrfToken;

  async function builderRequest(url, payload) {
    const response = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify({ csrf_token: csrfToken, ...payload }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Не удалось сохранить изменения');
    return data;
  }

  function setFormBusy(form, busy) {
    form.querySelectorAll('button').forEach((button) => { button.disabled = busy; });
  }

  function finishBuilderAction(message) {
    window.location.assign(`${window.location.pathname}?ok=${encodeURIComponent(message)}`);
  }

  function setupQuestionEditor(form, optionsBox, questionId = null) {
    const typeSelect = form.elements.question_type;
    const addOptionButton = form.querySelector('[data-action="add-option"]');
    const radioName = `correct-${questionId || 'new'}`;

    const seeds = [...optionsBox.querySelectorAll('.hj-option-seed')].map((seed) => ({
      text: seed.dataset.text || '',
      correct: seed.dataset.correct === 'true',
    }));
    optionsBox.replaceChildren();

    function addOption(text = '', correct = false, skipSync = false) {
      const row = document.createElement('div');
      row.className = 'quick-option-row';
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
        syncType();
      });
      row.append(correctLabel, textInput, remove);
      optionsBox.append(row);
      if (!skipSync) syncType();
    }

    function syncType() {
      const type = typeSelect.value;
      const isText = type === 'text';
      optionsBox.hidden = isText;
      addOptionButton.hidden = isText;
      form.querySelector('.correct-help').textContent = type === 'multi_choice'
        ? 'Можно отметить несколько правильных ответов.'
        : type === 'text' ? 'Участник введёт ответ текстом.' : 'Выберите правильный вариант.';
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
    ]).forEach((item) => addOption(item.text, item.correct, true));
    syncType();
    typeSelect.addEventListener('change', syncType);
    addOptionButton.addEventListener('click', () => {
      addOption('', false);
      optionsBox.lastElementChild.querySelector('input[type="text"]').focus();
    });

    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const status = form.querySelector('.form-status');
      status.textContent = '';
      status.classList.remove('error');
      const options = [...optionsBox.querySelectorAll('.quick-option-row')].map((row) => ({
        text: row.querySelector('input[type="text"]').value,
        is_correct: row.querySelector('.quick-correct input').checked,
      }));
      const payload = {
        title: form.elements.title.value,
        question_type: typeSelect.value,
        options,
        points: form.elements.points.value,
        time_limit_seconds: 0,
        required: form.elements.required.checked,
        placeholder: form.elements.placeholder.value,
      };
      const url = questionId
        ? `/api/master/quiz-questions/${questionId}/update-complete`
        : `/api/master/quiz-campaigns/${campaignId}/questions/create-complete`;
      setFormBusy(form, true);
      try {
        const data = await builderRequest(url, payload);
        finishBuilderAction(data.message);
      } catch (error) {
        status.textContent = error.message;
        status.classList.add('error');
        setFormBusy(form, false);
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
    setFormBusy(bulkForm, true);
    try {
      const data = await builderRequest(`/api/master/quiz-campaigns/${campaignId}/questions/bulk-create`, {
        text: bulkForm.elements.bulk_text.value,
        points: 1,
        time_limit_seconds: 0,
      });
      finishBuilderAction(data.message);
    } catch (error) {
      status.textContent = error.message;
      status.classList.add('error');
      setFormBusy(bulkForm, false);
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
