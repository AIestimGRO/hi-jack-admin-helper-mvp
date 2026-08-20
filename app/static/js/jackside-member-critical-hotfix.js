(() => {
  function isJacksideQuizLink(link) {
    const href = link?.getAttribute('href') || '';
    return href.startsWith('/quiz?') && href.includes('campaign=jackside_');
  }

  function ensureRulesAction(card, primary) {
    if (card.querySelector('a[href^="/jackside/rules"]')) return;
    const rules = document.createElement('a');
    rules.className = 'app-secondary-action';
    rules.href = '/jackside/rules';
    rules.textContent = 'Полные правила';

    const existingActions = primary.closest('.quiz-feature-actions');
    if (existingActions) {
      existingActions.append(rules);
      return;
    }

    const actions = document.createElement('div');
    actions.className = 'quiz-feature-actions';
    primary.replaceWith(actions);
    actions.append(primary, rules);
  }

  function updateJacksideCard(card) {
    const primary = [...card.querySelectorAll('a.app-primary-action')].find(isJacksideQuizLink);
    if (!primary) return;

    const current = (primary.textContent || '').trim().toLowerCase();
    if (!current.startsWith('продолж')) {
      primary.textContent = card.classList.contains('active') ? 'Участвовать' : 'Лобби';
    }
    ensureRulesAction(card, primary);
  }

  document.querySelectorAll('.quiz-feature-card, .campaign-card').forEach(updateJacksideCard);
})();
