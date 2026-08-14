(() => {
  const page = document.querySelector('.member-app-page');
  if (!page) return;

  const tab = page.dataset.accountTab || 'home';

  function polishWallet() {
    if (tab !== 'home') return;
    const copy = page.querySelector('.jc-wallet-bottom p');
    if (copy) copy.textContent = 'Играй. Копи. Меняй.';
  }

  function polishHiJackRating() {
    if (tab !== 'rating') return;
    const section = new URL(window.location.href).searchParams.get('section');
    if (section !== 'club') return;

    let meObserver = null;
    const normalizeHub = () => {
      const hub = page.querySelector('.hijack-rating-hub');
      if (!hub) return false;

      const head = hub.querySelector('.hijack-rating-head');
      const me = hub.querySelector('[data-hijack-me]');
      if (!head || !me) return false;

      head.classList.add('hijack-rating-head-compact');
      const intro = Array.from(head.children).find((node) => node !== me);
      intro?.remove();

      const normalizeMe = () => {
        const label = me.querySelector('small');
        if (label) label.textContent = 'Твоё место в рейтинге';
        me.querySelector('span')?.remove();
      };

      normalizeMe();
      if (!me.dataset.finalPolishObserver) {
        me.dataset.finalPolishObserver = '1';
        meObserver = new MutationObserver(normalizeMe);
        meObserver.observe(me, { childList: true });
      }

      hub.querySelector('.hijack-rating-caption')?.setAttribute('aria-hidden', 'true');
      return true;
    };

    if (normalizeHub()) return;
    const observer = new MutationObserver(() => {
      if (normalizeHub()) observer.disconnect();
    });
    observer.observe(page, { childList: true, subtree: true });
    window.setTimeout(() => observer.disconnect(), 8000);
  }

  polishWallet();
  polishHiJackRating();
})();
