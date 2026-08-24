(() => {
  const root = document.querySelector('[data-vault-scanner]');
  if (!root) return;

  const form = document.querySelector('[data-vault-redeem-form]');
  const codeInput = form?.querySelector('input[name="code"]');
  const startButton = root.querySelector('[data-vault-scan-start]');
  const stopButton = root.querySelector('[data-vault-scan-stop]');
  const panel = root.querySelector('[data-vault-scan-panel]');
  const video = root.querySelector('[data-vault-scan-video]');
  const canvas = root.querySelector('[data-vault-scan-canvas]');
  const status = root.querySelector('[data-vault-scan-status]');

  if (!form || !codeInput || !startButton || !stopButton || !panel || !video || !canvas || !status) return;

  const context = canvas.getContext('2d', { willReadFrequently: true });
  const QR_DECODER_URLS = [
    'https://unpkg.com/jsqr@1.4.0/dist/jsQR.js',
    'https://cdn.jsdelivr.net/npm/jsqr@1.4.0/dist/jsQR.min.js',
  ];

  let stream = null;
  let running = false;
  let frameRequest = 0;
  let detector = null;
  let detectorFailed = false;
  let lastDecodeAt = 0;
  let decoderLoadPromise = null;

  function setStatus(message, state = '') {
    status.textContent = message;
    status.dataset.state = state;
  }

  function extractCode(rawValue) {
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

  function loadDecoderScript(url) {
    return new Promise((resolve, reject) => {
      const script = document.createElement('script');
      let settled = false;
      const timeout = window.setTimeout(() => {
        if (settled) return;
        settled = true;
        script.remove();
        reject(new Error('decoder_load_timeout'));
      }, 6000);

      script.src = url;
      script.async = true;
      script.referrerPolicy = 'no-referrer';
      script.onload = () => {
        if (settled) return;
        settled = true;
        window.clearTimeout(timeout);
        if (typeof window.jsQR === 'function') {
          resolve(true);
        } else {
          script.remove();
          reject(new Error('decoder_not_exposed'));
        }
      };
      script.onerror = () => {
        if (settled) return;
        settled = true;
        window.clearTimeout(timeout);
        script.remove();
        reject(new Error('decoder_load_failed'));
      };
      document.head.appendChild(script);
    });
  }

  async function ensureJsQR() {
    if (typeof window.jsQR === 'function') return true;
    if (decoderLoadPromise) return decoderLoadPromise;

    decoderLoadPromise = (async () => {
      for (const url of QR_DECODER_URLS) {
        try {
          await loadDecoderScript(url);
          if (typeof window.jsQR === 'function') return true;
        } catch (_) {
          // Try the next mirror. Safari/iOS does not expose BarcodeDetector by
          // default, so keeping an independent fallback mirror matters here.
        }
      }
      return false;
    })();

    const ready = await decoderLoadPromise;
    if (!ready) decoderLoadPromise = null;
    return ready;
  }

  function stopScanner({ keepStatus = true } = {}) {
    running = false;
    if (frameRequest) window.cancelAnimationFrame(frameRequest);
    frameRequest = 0;
    if (stream) stream.getTracks().forEach((track) => track.stop());
    stream = null;
    video.pause();
    video.srcObject = null;
    panel.hidden = true;
    startButton.disabled = false;
    stopButton.hidden = true;
    if (!keepStatus) setStatus('Нажмите «Сканировать QR» и разрешите доступ к камере.');
  }

  function acceptScan(rawValue) {
    const code = extractCode(rawValue);
    if (!code) {
      setStatus('Это не QR активированной JACK CARD. Наведите камеру на QR карты.', 'error');
      return false;
    }

    codeInput.value = code;
    codeInput.dispatchEvent(new Event('input', { bubbles: true }));
    stopScanner({ keepStatus: true });
    setStatus(`QR распознан · код ${code}. Подтвердите сжигание JACK CARD.`, 'success');
    if (navigator.vibrate) navigator.vibrate(70);
    root.scrollIntoView({ block: 'nearest', behavior: 'smooth' });

    // requestSubmit keeps native validation and the form's explicit admin
    // confirmation. The QR itself never burns a card without confirmation.
    window.setTimeout(() => form.requestSubmit(), 0);
    return true;
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
            setStatus('Не удалось загрузить распознавание QR. Проверьте интернет и повторите попытку или введите код вручную.', 'error');
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
      setStatus('Камера недоступна. Откройте админку по HTTPS или введите код вручную.', 'error');
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
        setStatus('Не удалось загрузить распознавание QR. Проверьте интернет и повторите попытку или введите код вручную.', 'error');
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
      running = true;
      lastDecodeAt = 0;
      setStatus('Наведите камеру на QR активированной JACK CARD.', 'scanning');
      frameRequest = window.requestAnimationFrame(decodeFrame);
    } catch (error) {
      stopScanner({ keepStatus: true });
      const denied = error?.name === 'NotAllowedError' || error?.name === 'SecurityError';
      setStatus(
        denied
          ? 'Нет доступа к камере. Разрешите камеру для сайта в настройках браузера или введите код вручную.'
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

  if (codeInput.value.trim()) {
    setStatus('Код уже подставлен. Проверьте его и нажмите «Сжечь JACK CARD».');
  } else {
    setStatus('Нажмите «Сканировать QR» и разрешите доступ к камере.');
  }
})();
