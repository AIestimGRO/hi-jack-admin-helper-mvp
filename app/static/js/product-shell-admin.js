(() => {
  function renameVaultCopy() {
    document.querySelectorAll('.admin-primary-nav a, .admin-menu-panel strong, .admin-bottom-nav small').forEach((node) => {
      const text = node.textContent.trim();
      if (text === 'THE VAULT' || text === 'Хранилище' || text === 'Vault') {
        node.textContent = text === 'Vault' ? 'Store' : 'Hi, Store';
      }
    });
    if (window.location.pathname.startsWith('/admin/vault')) {
      document.querySelectorAll('h1, h2, .eyebrow, .muted').forEach((node) => {
        const text = node.textContent.trim();
        if (text === 'THE VAULT' || text === 'Хранилище') node.textContent = 'Hi, Store';
      });
    }
  }

  function addEngagementIconManagerLink() {
    const panel = document.querySelector('[data-master-panel="engagement"]');
    if (!panel || panel.querySelector('[data-engagement-icons-link]')) return;
    const link = document.createElement('a');
    link.className = 'button';
    link.dataset.engagementIconsLink = '1';
    link.href = '/master/engagement-icons';
    link.textContent = 'Иконки званий и достижений';
    const head = panel.querySelector('.section-head');
    if (head) head.insertAdjacentElement('afterend', link);
    else panel.prepend(link);
  }

  renameVaultCopy();
  addEngagementIconManagerLink();
})();
