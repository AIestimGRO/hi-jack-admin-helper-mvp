(() => {
  const app = document.getElementById('quiz-app');
  if (!app || app.dataset.campaignType !== 'daily_414') return;

  const campaign = app.dataset.campaign || '';
  let totalRequested = false;

  function cleanLobbyCopy() {
    const message = app.querySelector('.final-lobby-message');
    if (message) {
      message.textContent = '';
      message.hidden = true;
    }

    const candidates = app.querySelector('.final-lobby-candidates');
    if (candidates && candidates.textContent) {
      const cleaned = candidates.textContent.replace(/\s*\(нужно минимум\s+\d+\)\s*$/i, '');
      if (cleaned !== candidates.textContent) candidates.textContent = cleaned;
    }
  }

  function ensureIssueTotalBox() {
    const screen = app.querySelector('[data-screen="final-outcome"]');
    if (!screen) return null;
    let box = screen.querySelector('.jackside-issue-jc');
    if (box) return box;
    box = document.createElement('div');
    box.className = 'jackside-issue-jc';
    box.hidden = true;
    const actions = screen.querySelector('.final-outcome-actions, .final-account-link');
    if (actions) actions.before(box);
    else screen.append(box);
    return box;
  }

  function renderIssueTotal(data) {
    const box = ensureIssueTotalBox();
    if (!box || !data || data.issue_jackcoin_total == null) return;
    const total = Number(data.issue_jackcoin_total || 0);
    const breakdown = data.issue_jackcoin_breakdown || {};
    const parts = [
      ['main', 'Основная часть'],
      ['final_correct', 'Ответы финала'],
      ['final_win', 'Победа в финале'],
      ['final_prize', 'Главный приз'],
    ].map(([key, label]) => {
      const amount = Number(breakdown[key] || 0);
      return amount > 0 ? `${label}: +${amount} JC` : '';
    }).filter(Boolean);
    box.innerHTML = `<small>За этот выпуск</small><strong>+${total} JC</strong>${parts.length ? `<span>${parts.join(' · ')}</span>` : ''}`;
    box.hidden = false;
  }

  async function loadIssueTotal() {
    if (totalRequested || !campaign) return;
    const screen = app.querySelector('[data-screen="final-outcome"]');
    if (!screen?.classList.contains('active')) return;
    totalRequested = true;
    try {
      const response = await fetch(`/api/jackside/final-result?campaign=${encodeURIComponent(campaign)}`, {
        headers: { Accept: 'application/json' },
        cache: 'no-store',
      });
      if (!response.ok) return;
      renderIssueTotal(await response.json());
    } catch (_) {
      totalRequested = false;
    }
  }

  function tick() {
    cleanLobbyCopy();
    void loadIssueTotal();
  }

  tick();
  window.setInterval(tick, 300);
})();
