(() => {
  const openButton = document.querySelector('[data-member-identity-qr-open]');
  const dialog = document.querySelector('[data-member-identity-qr-dialog]');
  const closeButton = dialog?.querySelector('[data-member-identity-qr-close]');

  if (!openButton || !dialog || !closeButton) return;

  function setOpenState(open) {
    document.documentElement.classList.toggle('member-identity-qr-open', open);
    document.body.classList.toggle('member-identity-qr-open', open);
  }

  function openDialog() {
    if (typeof dialog.showModal === 'function') {
      if (!dialog.open) dialog.showModal();
    } else {
      dialog.setAttribute('open', '');
    }
    setOpenState(true);
  }

  function closeDialog() {
    if (typeof dialog.close === 'function' && dialog.open) {
      dialog.close();
    } else {
      dialog.removeAttribute('open');
    }
    setOpenState(false);
  }

  openButton.addEventListener('click', openDialog);
  closeButton.addEventListener('click', closeDialog);
  dialog.addEventListener('cancel', () => setOpenState(false));
  dialog.addEventListener('close', () => setOpenState(false));
  dialog.addEventListener('click', (event) => {
    if (event.target === dialog) closeDialog();
  });
  window.addEventListener('pagehide', () => setOpenState(false));
})();
