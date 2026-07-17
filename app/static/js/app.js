document.querySelectorAll('.quick-values').forEach((group) => {
  group.querySelectorAll('button').forEach((button) => button.addEventListener('click', () => {
    document.getElementById(group.dataset.target).value = button.dataset.value;
  }));
});

document.querySelectorAll('.confirm-spend').forEach((button) => {
  button.addEventListener('click', (event) => {
    const amount = button.closest('form').querySelector('[name="amount"]').value;
    if (!window.confirm(`Списать ${amount} «${button.dataset.title}» у ${button.dataset.client}?`)) event.preventDefault();
  });
});

const dialog = document.getElementById('qr-dialog');
const qrButton = document.querySelector('.qr-open');
if (dialog && qrButton) {
  qrButton.addEventListener('click', () => {
    dialog.querySelector('img').src = qrButton.dataset.qrUrl;
    dialog.querySelector('strong').textContent = qrButton.dataset.phone;
    dialog.showModal();
  });
  dialog.querySelector('.dialog-close').addEventListener('click', () => dialog.close());
  dialog.addEventListener('click', (event) => { if (event.target === dialog) dialog.close(); });
}

const masterTabs = document.querySelectorAll('[data-master-tab]');
masterTabs.forEach((tab) => tab.addEventListener('click', () => {
  masterTabs.forEach((item) => item.classList.toggle('active', item === tab));
  document.querySelectorAll('[data-master-panel]').forEach((panel) => {
    panel.hidden = panel.dataset.masterPanel !== tab.dataset.masterTab;
  });
}));
