(() => {
  const centralizeCampaignEconomy = () => {
    if (location.pathname !== '/master') return;
    document.querySelectorAll('.campaign-jackcoin-fields').forEach((fieldset) => {
      ['jackcoin_per_correct', 'jackcoin_completion_bonus', 'jackcoin_perfect_bonus'].forEach((name) => {
        const input = fieldset.querySelector(`[name="${name}"]`);
        const label = input?.closest('label');
        if (label) label.hidden = true;
      });
      if (!fieldset.querySelector('.central-economy-note')) {
        const note = document.createElement('p');
        note.className = 'muted central-economy-note';
        note.innerHTML = 'Базовые начисления за ответ, завершение и 10/10 задаются централизованно в <a href="/master/economy">Экономике JACKCOIN</a>. Здесь остаются только настройки конкретной игры и её дополнительного приза.';
        const legend = fieldset.querySelector('legend');
        if (legend) legend.insertAdjacentElement('afterend', note);
        else fieldset.prepend(note);
      }
    });
  };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', centralizeCampaignEconomy, { once: true });
  else centralizeCampaignEconomy();
})();
