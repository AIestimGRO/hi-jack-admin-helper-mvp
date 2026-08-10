(() => {
  const page = document.querySelector('.member-app-page[data-account-tab="rating"]');
  if (!page) return;

  const update = () => {
    const hub = page.querySelector('.hijack-rating-hub');
    if (!hub) return;
    const active = hub.querySelector('[data-hijack-period="year"].active');
    const caption = hub.querySelector('[data-hijack-caption]');
    if (active && caption) {
      caption.textContent = 'Накопленный глобальный рейтинг + все последующие турниры';
    }
  };

  page.addEventListener('click', (event) => {
    if (event.target.closest('[data-hijack-period]')) window.requestAnimationFrame(update);
  });
  new MutationObserver(update).observe(page, { subtree: true, childList: true, attributes: true, attributeFilter: ['class'] });
  update();
})();
