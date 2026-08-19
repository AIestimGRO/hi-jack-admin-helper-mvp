(() => {
  const page = document.querySelector('.member-app-page[data-account-tab="vault"]');
  if (!page) return;

  const returnKey = 'hj-store-return-tab';

  try {
    const pendingTab = window.sessionStorage.getItem(returnKey);
    if (pendingTab === 'cards') {
      window.sessionStorage.removeItem(returnKey);
      const url = new URL(window.location.href);
      if (url.searchParams.get('store') !== 'cards') {
        url.searchParams.set('store', 'cards');
        window.history.replaceState(window.history.state, '', url);
      }
    }
  } catch (_) {
    // Storage restrictions must never break card activation or HI STORE.
  }

  document.addEventListener('submit', (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) return;

    const action = new URL(form.action || window.location.href, window.location.href);
    if (action.origin !== window.location.origin) return;
    if (!/^\/account\/rewards\/\d+\/activate$/.test(action.pathname)) return;

    try {
      window.sessionStorage.setItem(returnKey, 'cards');
    } catch (_) {
      // The activation itself remains a normal server-side form submission.
    }
  }, true);
})();
