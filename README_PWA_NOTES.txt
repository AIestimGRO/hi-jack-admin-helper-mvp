PWA implementation notes

- Manifest start URL: /account
- Browser display mode: standalone
- Service worker intentionally does not cache pages, API responses, or personal data.
- Icons are generated at runtime from app/static/img/brand/hi-jack-mark.webp.
- Chromium-required 192x192 and 512x512 PNG sizes are served exactly.
- The install button is only displayed after the browser fires beforeinstallprompt.
