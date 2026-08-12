(() => {
  const isStandalone = window.matchMedia?.('(display-mode: standalone)').matches
    || window.navigator.standalone === true;

  if ('serviceWorker' in navigator) {
    window.addEventListener('load', () => {
      navigator.serviceWorker.register('/service-worker.js', { scope: '/' }).catch(() => {});
    }, { once: true });
  }

  if (isStandalone) return;

  let deferredPrompt = null;
  let installButton = null;

  const removeInstallButton = () => {
    installButton?.remove();
    installButton = null;
  };

  const ensureInstallButton = () => {
    if (installButton) return installButton;

    const style = document.createElement('style');
    style.textContent = `
      .hj-pwa-install {
        position: fixed;
        right: 16px;
        bottom: calc(86px + env(safe-area-inset-bottom));
        z-index: 1200;
        border: 1px solid rgba(255,255,255,.16);
        border-radius: 999px;
        padding: 10px 15px;
        background: rgba(8,24,20,.94);
        color: #fff;
        box-shadow: 0 10px 30px rgba(0,0,0,.3);
        font: 700 12px/1.1 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        cursor: pointer;
        backdrop-filter: blur(14px);
      }
      .hj-pwa-install:hover { transform: translateY(-1px); }
      @media (min-width: 900px) {
        .hj-pwa-install { bottom: 22px; right: 22px; }
      }
    `;
    document.head.append(style);

    installButton = document.createElement('button');
    installButton.type = 'button';
    installButton.className = 'hj-pwa-install';
    installButton.textContent = 'Установить приложение';
    installButton.setAttribute('aria-label', 'Установить Hi, Jack Club как приложение');
    installButton.addEventListener('click', async () => {
      if (!deferredPrompt) return;
      installButton.disabled = true;
      try {
        await deferredPrompt.prompt();
        await deferredPrompt.userChoice;
      } finally {
        deferredPrompt = null;
        removeInstallButton();
      }
    });
    document.body.append(installButton);
    return installButton;
  };

  window.addEventListener('beforeinstallprompt', (event) => {
    event.preventDefault();
    deferredPrompt = event;
    ensureInstallButton();
  });

  window.addEventListener('appinstalled', () => {
    deferredPrompt = null;
    removeInstallButton();
  });
})();
