(() => {
  const CONFIRM_TEXT = [
    'Активировать JACK CARD?',
    '',
    'После подтверждения карта будет активна 15 минут.',
    'За это время покажите QR или код администратору или бармену и получите награду.',
    'Если карту не погасить за 15 минут, она автоматически будет считаться использованной. Повторная активация будет невозможна.',
  ].join('\n');

  function mountActivationConfirmations() {
    document.querySelectorAll('form[action*="/account/rewards/"][action$="/activate"]').forEach((form) => {
      if (form.dataset.irreversibleActivationInstalled === '1') return;
      form.dataset.irreversibleActivationInstalled = '1';
      form.addEventListener('submit', (event) => {
        if (!window.confirm(CONFIRM_TEXT)) event.preventDefault();
      });

      const card = form.closest('.jack-card');
      const note = card?.querySelector('.jack-card-main + p');
      if (note) {
        note.textContent = 'Активируй карту только когда готов получить награду: после подтверждения у тебя будет 15 минут. Затем карта автоматически считается использованной.';
      }
    });
  }

  function buildStableCountdown(node) {
    node.replaceChildren();
    node.setAttribute('aria-live', 'off');

    const prefix = document.createElement('span');
    prefix.textContent = 'Карта активна ';

    const value = document.createElement('strong');
    value.dataset.rewardCountdownValue = '1';
    value.className = 'reward-countdown-value';
    value.textContent = '15:00';

    const suffix = document.createElement('span');
    suffix.textContent = ' · затем будет использована автоматически';

    node.append(prefix, value, suffix);
    return value;
  }

  function mountCountdowns() {
    document.querySelectorAll('[data-reward-activation-countdown]').forEach((node) => {
      if (node.dataset.irreversibleCountdownInstalled === '1') return;
      node.dataset.irreversibleCountdownInstalled = '1';
      const raw = node.dataset.rewardActivationCountdown || '';
      const expiresAt = Date.parse(raw);
      if (!Number.isFinite(expiresAt)) return;

      const value = buildStableCountdown(node);
      let reloadQueued = false;
      let intervalId = null;

      const tick = () => {
        const remaining = Math.max(0, expiresAt - Date.now());
        const totalSeconds = Math.ceil(remaining / 1000);
        const minutes = Math.floor(totalSeconds / 60);
        const seconds = totalSeconds % 60;
        if (remaining > 0) {
          value.textContent = `${minutes}:${String(seconds).padStart(2, '0')}`;
          return;
        }

        if (intervalId !== null) {
          window.clearInterval(intervalId);
          intervalId = null;
        }
        node.setAttribute('aria-live', 'polite');
        node.textContent = '15 минут истекли · карта считается использованной';
        if (!reloadQueued) {
          reloadQueued = true;
          window.setTimeout(() => window.location.reload(), 900);
        }
      };

      tick();
      if (!reloadQueued) intervalId = window.setInterval(tick, 1000);
    });
  }

  mountActivationConfirmations();
  mountCountdowns();
})();
