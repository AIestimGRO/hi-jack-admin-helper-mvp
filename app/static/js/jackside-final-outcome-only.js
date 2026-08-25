(() => {
  const app = document.getElementById('quiz-app');
  if (!app || app.dataset.campaignType !== 'daily_414') return;

  const campaign = app.dataset.campaign || '';
  let loading = false;
  let renderedKey = '';

  function outcomeScreen() {
    return app.querySelector('[data-screen="final-outcome"]');
  }

  function ensureActions(screen) {
    let actions = screen.querySelector('.final-outcome-actions');
    if (!actions) {
      actions = document.createElement('div');
      actions.className = 'final-outcome-actions';
      screen.append(actions);
    }
    actions.innerHTML = '';

    const back = document.createElement('a');
    back.href = '/account';
    back.className = 'jackside-final-action jackside-final-action-primary';
    back.textContent = 'Вернуться в JACKSIDE';

    const rating = document.createElement('a');
    rating.href = '/account?tab=rating';
    rating.className = 'jackside-final-action jackside-final-action-secondary';
    rating.textContent = 'Открыть рейтинг';

    actions.append(back, rating);
  }

  function renderBreakdown(screen, data) {
    let box = screen.querySelector('.jackside-final-jc-summary');
    if (!box) {
      box = document.createElement('section');
      box.className = 'jackside-final-jc-summary';
      const message = screen.querySelector('.final-outcome-message');
      if (message) message.after(box);
      else screen.append(box);
    }

    const breakdown = data.issue_jackcoin_breakdown || {};
    const rows = [
      ['main', 'Основная часть'],
      ['final_correct', 'Ответы финала'],
      ['final_win', 'Победа в финале'],
      ['final_prize', 'Доп. приз выпуска'],
    ].map(([key, label]) => {
      const amount = Number(breakdown[key] || 0);
      if (amount <= 0) return '';
      return `<div class="jackside-final-jc-row"><span>${label}</span><b>+${amount} JC</b></div>`;
    }).filter(Boolean).join('');

    const total = Number(data.issue_jackcoin_total || 0);
    box.innerHTML = `
      <div class="jackside-final-jc-head">
        <span>JACKCOIN за выпуск</span>
        <strong>+${total} JC</strong>
      </div>
      ${rows ? `<div class="jackside-final-jc-rows">${rows}</div>` : ''}
    `;
    box.hidden = false;
  }

  function renderSuperprize(screen, data) {
    const existing = screen.querySelector('.jackside-final-superprize');
    const prize = data.state === 'winner' ? data.superprize : null;
    if (!prize) {
      existing?.remove();
      return;
    }

    const card = existing || document.createElement('section');
    card.className = 'jackside-final-superprize';
    card.replaceChildren();

    const kicker = document.createElement('div');
    kicker.className = 'jackside-final-superprize-kicker';
    kicker.textContent = 'СУПЕРПРИЗ ВЫПУСКА';

    const title = document.createElement('strong');
    title.className = 'jackside-final-superprize-title';
    title.textContent = String(prize.title || 'JACK CARD');

    const caption = document.createElement('div');
    caption.className = 'jackside-final-superprize-caption';
    caption.textContent = 'JACK CARD добавлена в My Cards';

    const link = document.createElement('a');
    link.className = 'jackside-final-superprize-link';
    link.href = String(prize.my_cards_url || '/account?tab=vault&store=cards');
    link.textContent = 'Открыть My Cards';

    card.append(kicker, title, caption, link);
    if (!existing) {
      const summary = screen.querySelector('.jackside-final-jc-summary');
      if (summary) summary.after(card);
      else screen.append(card);
    }
  }

  function enhanceOutcome(data) {
    const screen = outcomeScreen();
    if (!screen || !screen.classList.contains('active')) return;
    renderBreakdown(screen, data);
    renderSuperprize(screen, data);
    ensureActions(screen);
  }

  async function loadOutcome() {
    const screen = outcomeScreen();
    if (!campaign || loading || !screen?.classList.contains('active')) return;

    loading = true;
    try {
      const response = await fetch(`/api/jackside/final-outcome?campaign=${encodeURIComponent(campaign)}`, {
        headers: { Accept: 'application/json' },
        cache: 'no-store',
      });
      if (!response.ok) return;
      const data = await response.json();
      if (!data || data.state === 'pending') return;
      const prizeKey = data.superprize
        ? `${data.superprize.member_reward_id}:${data.superprize.status}:${data.superprize.title}`
        : '';
      const key = `${data.state}:${data.issue_jackcoin_total}:${JSON.stringify(data.issue_jackcoin_breakdown || {})}:${prizeKey}`;
      if (key === renderedKey && screen.querySelector('.jackside-final-jc-summary')) return;
      renderedKey = key;
      enhanceOutcome(data);
    } catch (_) {
      // The base final screen remains untouched if the enhancement cannot load.
    } finally {
      loading = false;
    }
  }

  const observer = new MutationObserver(() => {
    void loadOutcome();
  });
  observer.observe(app, {
    subtree: true,
    childList: true,
    attributes: true,
    attributeFilter: ['class'],
  });

  void loadOutcome();
  window.setInterval(loadOutcome, 1000);
})();
