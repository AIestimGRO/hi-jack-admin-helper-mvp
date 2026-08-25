(() => {
  const QR_DECODER_URL = '/static/vendor/jsqr/dist/jsQR.js';
  let decoderLoadPromise = null;

  function loadDecoderScript() {
    return new Promise((resolve, reject) => {
      const existing = document.querySelector('script[data-vault-qr-decoder]');
      if (existing) {
        if (typeof window.jsQR === 'function') {
          resolve(true);
          return;
        }
        existing.addEventListener('load', () => resolve(typeof window.jsQR === 'function'), { once: true });
        existing.addEventListener('error', () => reject(new Error('decoder_load_failed')), { once: true });
        return;
      }

      const script = document.createElement('script');
      script.src = QR_DECODER_URL;
      script.async = true;
      script.dataset.vaultQrDecoder = '1';
      script.onload = () => {
        if (typeof window.jsQR === 'function') resolve(true);
        else reject(new Error('decoder_not_exposed'));
      };
      script.onerror = () => reject(new Error('decoder_load_failed'));
      document.head.appendChild(script);
    });
  }

  async function ensureJsQR() {
    if (typeof window.jsQR === 'function') return true;
    if (decoderLoadPromise) return decoderLoadPromise;

    decoderLoadPromise = loadDecoderScript()
      .then(() => typeof window.jsQR === 'function')
      .catch(() => false);

    const ready = await decoderLoadPromise;
    if (!ready) decoderLoadPromise = null;
    return ready;
  }

  function createScanner(root, options) {
    if (!root) return null;
    const startButton = options.startButton || root.querySelector('[data-vault-scan-start]');
    const stopButton = root.querySelector('[data-vault-scan-stop]');
    const panel = root.querySelector('[data-vault-scan-panel]');
    const video = root.querySelector('[data-vault-scan-video]');
    const canvas = root.querySelector('[data-vault-scan-canvas]');
    const status = root.querySelector('[data-vault-scan-status]');
    if (!startButton || !stopButton || !panel || !video || !canvas || !status) return null;

    const context = canvas.getContext('2d', { willReadFrequently: true });
    let stream = null;
    let running = false;
    let frameRequest = 0;
    let detector = null;
    let detectorFailed = false;
    let lastDecodeAt = 0;

    function setStatus(message, state = '') {
      status.hidden = false;
      status.textContent = message;
      status.dataset.state = state;
    }

    function setFullscreenOpen(open) {
      root.classList.toggle('is-open', open);
      document.documentElement.classList.toggle('vault-scanner-open', open);
      document.body.classList.toggle('vault-scanner-open', open);
    }

    function stopScanner({ keepStatus = true } = {}) {
      running = false;
      if (frameRequest) window.cancelAnimationFrame(frameRequest);
      frameRequest = 0;
      if (stream) stream.getTracks().forEach((track) => track.stop());
      stream = null;
      video.pause();
      video.srcObject = null;
      setFullscreenOpen(false);
      panel.hidden = true;
      startButton.disabled = false;
      stopButton.hidden = true;
      if (!keepStatus) setStatus(options.idleMessage || 'Нажмите «Сканировать QR» и разрешите доступ к камере.');
    }

    function acceptScan(rawValue) {
      return Boolean(options.onDecoded(rawValue, { stopScanner, setStatus }));
    }

    async function decodeFrame(timestamp) {
      if (!running) return;
      frameRequest = window.requestAnimationFrame(decodeFrame);
      if (timestamp - lastDecodeAt < 160 || video.readyState < 2) return;
      lastDecodeAt = timestamp;

      try {
        if (detector && !detectorFailed) {
          try {
            const codes = await detector.detect(video);
            if (codes.length && acceptScan(codes[0].rawValue)) return;
            return;
          } catch (_) {
            detectorFailed = true;
            const fallbackReady = await ensureJsQR();
            if (!fallbackReady) {
              stopScanner({ keepStatus: true });
              setStatus('Локальный модуль распознавания QR недоступен. Обновите страницу и повторите попытку.', 'error');
              return;
            }
          }
        }

        if (typeof window.jsQR !== 'function' || !context) return;
        const sourceWidth = video.videoWidth || 0;
        const sourceHeight = video.videoHeight || 0;
        if (!sourceWidth || !sourceHeight) return;

        const maxWidth = 720;
        const scale = Math.min(1, maxWidth / sourceWidth);
        canvas.width = Math.max(1, Math.round(sourceWidth * scale));
        canvas.height = Math.max(1, Math.round(sourceHeight * scale));
        context.drawImage(video, 0, 0, canvas.width, canvas.height);
        const pixels = context.getImageData(0, 0, canvas.width, canvas.height);
        const result = window.jsQR(pixels.data, pixels.width, pixels.height, {
          inversionAttempts: 'dontInvert',
        });
        if (result?.data) acceptScan(result.data);
      } catch (_) {
        setStatus('Не удалось прочитать кадр. Попробуйте приблизить QR и держать телефон неподвижно.', 'error');
      }
    }

    async function startScanner() {
      if (running) return;
      if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
        setStatus('Камера недоступна. Откройте админку по HTTPS.', 'error');
        return;
      }

      startButton.disabled = true;
      setStatus('Подготавливаем распознавание QR…');
      detector = null;
      detectorFailed = false;
      if ('BarcodeDetector' in window) {
        try {
          detector = new window.BarcodeDetector({ formats: ['qr_code'] });
        } catch (_) {
          detector = null;
        }
      }

      if (!detector) {
        const fallbackReady = await ensureJsQR();
        if (!fallbackReady) {
          startButton.disabled = false;
          setStatus('Локальный модуль распознавания QR недоступен. Обновите страницу и повторите попытку.', 'error');
          return;
        }
      }

      setStatus('Запрашиваем доступ к камере…');
      try {
        stream = await navigator.mediaDevices.getUserMedia({
          audio: false,
          video: {
            facingMode: { ideal: 'environment' },
            width: { ideal: 1280 },
            height: { ideal: 720 },
          },
        });
        video.srcObject = stream;
        video.setAttribute('playsinline', '');
        video.muted = true;
        await video.play();

        panel.hidden = false;
        stopButton.hidden = false;
        setFullscreenOpen(true);
        running = true;
        lastDecodeAt = 0;
        setStatus(options.scanningMessage || 'Наведите камеру на QR.', 'scanning');
        frameRequest = window.requestAnimationFrame(decodeFrame);
      } catch (error) {
        stopScanner({ keepStatus: true });
        const denied = error?.name === 'NotAllowedError' || error?.name === 'SecurityError';
        setStatus(
          denied
            ? 'Нет доступа к камере. Разрешите камеру для сайта в настройках браузера.'
            : 'Не удалось открыть камеру. Проверьте, что другая программа её не использует.',
          'error',
        );
      }
    }

    startButton.addEventListener('click', startScanner);
    stopButton.addEventListener('click', () => stopScanner({ keepStatus: false }));
    document.addEventListener('visibilitychange', () => {
      if (document.hidden && running) stopScanner({ keepStatus: false });
    });
    window.addEventListener('pagehide', () => stopScanner({ keepStatus: true }));
    return { startScanner, stopScanner, setStatus };
  }

  function extractVaultCode(rawValue) {
    const raw = String(rawValue || '').trim();
    if (!raw) return '';
    if (/^(?:https?:\/\/|\/)/i.test(raw)) {
      try {
        const parsed = new URL(raw, window.location.origin);
        if (parsed.pathname.replace(/\/+$/, '') !== '/admin/vault') return '';
        return String(parsed.searchParams.get('code') || '').trim();
      } catch (_) {
        return '';
      }
    }
    return /^[a-z0-9_-]{4,64}$/i.test(raw) ? raw : '';
  }

  function installVaultScanner() {
    const root = document.querySelector('[data-vault-scanner]');
    if (!root) return;
    const form = document.querySelector('[data-vault-redeem-form]');
    const codeInput = form?.querySelector('input[name="code"]');
    const burnButton = form?.querySelector('.vault-burn-button');
    if (!form || !codeInput || !burnButton) return;

    let manualCodeEntry = false;
    const lockCodeInput = () => {
      if (manualCodeEntry) return;
      codeInput.readOnly = true;
      if (document.activeElement === codeInput) codeInput.blur();
    };
    const unlockCodeInput = () => {
      manualCodeEntry = true;
      codeInput.readOnly = false;
    };
    codeInput.readOnly = true;
    codeInput.addEventListener('pointerdown', unlockCodeInput);
    codeInput.addEventListener('keydown', unlockCodeInput);
    codeInput.addEventListener('blur', () => {
      manualCodeEntry = false;
      codeInput.readOnly = true;
    });
    window.addEventListener('pageshow', lockCodeInput);
    window.requestAnimationFrame(lockCodeInput);

    const scanner = createScanner(root, {
      idleMessage: 'Нажмите «Сканировать QR» и разрешите доступ к камере.',
      scanningMessage: 'Наведите камеру на QR активированной JACK CARD.',
      onDecoded(rawValue, api) {
        const code = extractVaultCode(rawValue);
        if (!code) {
          api.setStatus('Это не QR активированной JACK CARD. Наведите камеру на QR карты.', 'error');
          return false;
        }
        manualCodeEntry = false;
        codeInput.readOnly = true;
        codeInput.value = code;
        codeInput.dispatchEvent(new Event('input', { bubbles: true }));
        api.stopScanner({ keepStatus: true });
        api.setStatus(`QR распознан · код ${code}. Нажмите «Сжечь JACK CARD», чтобы продолжить.`, 'success');
        if (navigator.vibrate) navigator.vibrate(70);
        window.setTimeout(() => {
          burnButton.focus({ preventScroll: true });
          burnButton.scrollIntoView({ block: 'center', behavior: 'smooth' });
        }, 0);
        return true;
      },
    });
    if (!scanner) return;

    if (codeInput.value.trim()) {
      scanner.setStatus('Код уже подставлен. Проверьте его и нажмите «Сжечь JACK CARD».');
    } else {
      scanner.setStatus('Нажмите «Сканировать QR» и разрешите доступ к камере.');
    }
  }

  function installClientScanner() {
    const root = document.querySelector('[data-client-scanner]');
    const startButton = document.querySelector('[data-client-scan-start]');
    if (!root || !startButton) return;
    const csrfToken = root.dataset.clientScanCsrf || '';
    const result = document.querySelector('[data-client-scan-card-result]');
    const resultTitle = result?.querySelector('[data-client-scan-card-title]');
    const resultMeta = result?.querySelector('[data-client-scan-card-meta]');
    const redeemForm = result?.querySelector('[data-client-scan-redeem]');
    const redeemCode = redeemForm?.querySelector('input[name="code"]');
    const redeemButton = redeemForm?.querySelector('button[type="submit"]');

    async function resolveScan(rawValue, api) {
      api.stopScanner({ keepStatus: true });
      api.setStatus('QR распознан. Определяю клиента или JACK CARD…');
      if (navigator.vibrate) navigator.vibrate(70);
      try {
        const body = new FormData();
        body.append('raw_value', String(rawValue || ''));
        body.append('csrf_token', csrfToken);
        const response = await fetch('/api/master/qr/resolve', {
          method: 'POST',
          body,
          credentials: 'same-origin',
          headers: { Accept: 'application/json' },
        });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(payload.error || `Ошибка ${response.status}`);

        if (payload.kind === 'client' || payload.kind === 'client_search') {
          api.setStatus(
            payload.kind === 'client' ? `Клиент найден: ${payload.name || ''}` : 'Найдено несколько клиентов. Открываю поиск…',
            'success',
          );
          window.location.assign(payload.url);
          return;
        }

        if (payload.kind !== 'card' || !result || !redeemForm || !redeemCode || !redeemButton) {
          throw new Error('qr_not_recognized');
        }

        redeemCode.value = payload.redeem_code || '';
        if (resultTitle) resultTitle.textContent = payload.title || 'JACK CARD';
        if (resultMeta) {
          const client = payload.client_name || `Клиент #${payload.client_id || ''}`;
          resultMeta.textContent = payload.redeemable
            ? `${client} · карта активна и готова к сжиганию.`
            : `${client} · карта сейчас не готова к сжиганию.`;
        }
        redeemButton.disabled = !payload.redeemable;
        result.hidden = false;
        api.setStatus(
          payload.redeemable
            ? 'JACK CARD распознана. Карта не списана — нажмите кнопку ниже, если хотите её сжечь.'
            : 'JACK CARD распознана, но она не активирована или уже недоступна.',
          payload.redeemable ? 'success' : 'error',
        );
        result.scrollIntoView({ block: 'center', behavior: 'smooth' });
      } catch (error) {
        api.setStatus(
          error?.message === 'qr_not_recognized'
            ? 'QR не распознан как клиент или JACK CARD.'
            : `Не удалось обработать QR: ${error?.message || 'неизвестная ошибка'}`,
          'error',
        );
      }
    }

    const scanner = createScanner(root, {
      startButton,
      idleMessage: 'Нажмите «Сканер», чтобы идентифицировать клиента или JACK CARD.',
      scanningMessage: 'Наведите камеру на QR клиента или активированной JACK CARD.',
      onDecoded(rawValue, api) {
        resolveScan(rawValue, api);
        return true;
      },
    });
    if (!scanner) return;

    redeemForm?.addEventListener('submit', async (event) => {
      event.preventDefault();
      if (!redeemCode?.value || !redeemButton) return;
      if (!window.confirm('Сжечь эту JACK CARD? Повторное использование будет невозможно.')) return;

      const originalText = redeemButton.textContent;
      redeemButton.disabled = true;
      redeemButton.textContent = 'Сжигаю…';
      try {
        const response = await fetch(redeemForm.action, {
          method: 'POST',
          body: new FormData(redeemForm),
          credentials: 'same-origin',
          redirect: 'follow',
        });
        const finalUrl = new URL(response.url || window.location.href, window.location.href);
        const error = finalUrl.searchParams.get('error');
        if (!response.ok || error) throw new Error(error || `Ошибка ${response.status}`);

        scanner.setStatus('JACK CARD успешно сожжена.', 'success');
        if (resultMeta) resultMeta.textContent = 'Карта сожжена. Повторное использование невозможно.';
        redeemCode.value = '';
        redeemButton.textContent = 'JACK CARD сожжена';
        redeemButton.disabled = true;
      } catch (error) {
        scanner.setStatus(`Не удалось сжечь JACK CARD: ${error?.message || 'неизвестная ошибка'}`, 'error');
        redeemButton.disabled = false;
        redeemButton.textContent = originalText;
      }
    });
  }

  installVaultScanner();
  installClientScanner();
})();
