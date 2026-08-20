(() => {
  const app = document.getElementById('quiz-app');
  if (!app || app.dataset.campaignType !== 'daily_414') return;

  function cleanCandidates() {
    const candidates = app.querySelector('.final-lobby-candidates');
    if (!candidates) return;
    const current = candidates.textContent || '';
    const cleaned = current.replace(/\s*\(нужно минимум\s+\d+\)\s*$/i, '');
    if (cleaned !== current) candidates.textContent = cleaned;
  }

  function hideLobbyMessage() {
    const message = app.querySelector('.final-lobby-message');
    if (!message) return;
    if (message.textContent) message.textContent = '';
    message.hidden = true;
  }

  function cleanLobbyCopy() {
    hideLobbyMessage();
    cleanCandidates();
  }

  cleanLobbyCopy();

  const candidates = app.querySelector('.final-lobby-candidates');
  if (candidates) {
    const observer = new MutationObserver(() => cleanCandidates());
    observer.observe(candidates, {
      childList: true,
      characterData: true,
      subtree: true,
    });
  }

  const message = app.querySelector('.final-lobby-message');
  if (message) {
    const observer = new MutationObserver(() => hideLobbyMessage());
    observer.observe(message, {
      childList: true,
      characterData: true,
      subtree: true,
      attributes: true,
      attributeFilter: ['hidden'],
    });
  }
})();
