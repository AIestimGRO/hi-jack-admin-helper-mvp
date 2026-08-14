(() => {
  const page = document.querySelector('.member-app-page');
  if (!page) return;

  const tab = page.dataset.accountTab || '';

  function openMonthlyRatingByDefault() {
    if (tab !== 'rating') return false;
    const url = new URL(window.location.href);
    if (url.searchParams.has('section')) return false;
    url.searchParams.set('tab', 'rating');
    url.searchParams.set('section', 'month');
    window.location.replace(url.toString());
    return true;
  }

  function focusRatingPage() {
    if (tab !== 'rating') return;
    page.querySelectorAll('.personal-stat-groups, .engagement-panel').forEach((node) => node.remove());
    const heroTitle = page.querySelector('.rating-hub-hero h1');
    if (heroTitle) heroTitle.textContent = 'Рейтинг';
    const heroCopy = page.querySelector('.rating-hub-hero .member-muted');
    if (heroCopy) heroCopy.remove();
    const jacksideTab = page.querySelector('.rating-section-tabs a:first-child');
    if (jacksideTab) jacksideTab.href = '/account?tab=rating&section=month';
  }

  function numberLeaderboard(container) {
    if (!container) return;
    const rows = Array.from(container.children).filter((node) => node.matches('article'));
    rows.forEach((row, index) => {
      const position = index + 1;
      const place = row.querySelector('.leaderboard-place');
      if (place) {
        place.textContent = String(position);
        place.setAttribute('aria-label', `Место ${position}`);
      }
      row.classList.remove('podium-1', 'podium-2', 'podium-3');
      if (position <= 3) row.classList.add('podium', `podium-${position}`);
    });
  }

  function numberAllLeaderboards() {
    if (tab !== 'rating') return;
    numberLeaderboard(page.querySelector('.jackside-leaderboard'));
    numberLeaderboard(page.querySelector('.club-leaderboard:not(.jackside-leaderboard)'));
  }

  function centerOpenEmblem(details) {
    if (!details.open || !window.matchMedia('(max-width: 680px)').matches) return;
    window.requestAnimationFrame(() => {
      details.scrollIntoView({
        behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth',
        block: 'nearest',
        inline: 'center',
      });
    });
  }

  function mountProfileRichBlocks() {
    if (tab !== 'profile') return;
    const template = document.getElementById('profile-rich-blocks');
    const header = page.querySelector('.profile-heading');
    if (!template || !header) return;
    const fragment = template.content.cloneNode(true);
    header.insertAdjacentElement('afterend', document.createElement('div'));
    const mount = header.nextElementSibling;
    mount.className = 'profile-rich-stack';
    mount.append(fragment);
    page.querySelector('#activity')?.remove();
    mount.querySelectorAll('.profile-emblem-card').forEach((details) => {
      details.addEventListener('toggle', () => {
        if (!details.open) return;
        mount.querySelectorAll('.profile-emblem-card[open]').forEach((other) => {
          if (other !== details) other.open = false;
        });
        centerOpenEmblem(details);
      });
    });
  }

  function enhanceReferralCopy() {
    document.querySelectorAll('[data-referral-copy]').forEach((button) => {
      button.addEventListener('click', async () => {
        const selector = button.getAttribute('data-referral-copy');
        const input = selector ? document.querySelector(selector) : null;
        if (!input) return;
        try {
          await navigator.clipboard.writeText(input.value);
          const oldText = button.textContent;
          button.textContent = 'Скопировано';
          window.setTimeout(() => { button.textContent = oldText; }, 1400);
        } catch (_) {
          input.focus();
          input.select();
          document.execCommand('copy');
        }
      });
    });
  }

  function loadScriptOnce(src, marker) {
    if (document.querySelector(`script[${marker}]`)) return;
    const script = document.createElement('script');
    script.setAttribute(marker, '1');
    script.src = src;
    document.body.append(script);
  }

  function loadStylesheetOnce(href, marker) {
    if (document.querySelector(`link[${marker}]`)) return;
    const stylesheet = document.createElement('link');
    stylesheet.setAttribute(marker, '1');
    stylesheet.rel = 'stylesheet';
    stylesheet.href = href;
    document.head.append(stylesheet);
  }

  function loadHiJackExtension() {
    loadStylesheetOnce('/static/css/member-ui-cleanup.css?v=1', 'data-member-ui-cleanup');
    loadScriptOnce('/static/js/member-ui-cleanup.js?v=1', 'data-member-ui-cleanup');
    loadStylesheetOnce('/static/css/hijack-member.css?v=3', 'data-hijack-member-extension');
    loadScriptOnce('/static/js/member-avatar-global.js?v=1', 'data-member-avatar-global');

    if (tab === 'rating' || tab === 'profile') {
      loadScriptOnce('/static/js/hijack-member.js?v=3', 'data-hijack-member-extension');
    }

    if (tab === 'profile') {
      loadStylesheetOnce('/static/css/member-achievement-carousel.css?v=4', 'data-engagement-carousel');
      loadScriptOnce('/static/js/profile-experience-v2.js?v=3', 'data-profile-experience-v2');
      loadStylesheetOnce('/static/css/account-security.css?v=2', 'data-account-security');
      loadScriptOnce('/static/js/account-security.js?v=2', 'data-account-security');
    }

    // Compatibility first, final brand layer last. prelaunch-member.js sees the
    // marker and will not append an older turquoise copy after this bundle.
    loadStylesheetOnce('/static/css/prelaunch-ui-hotfix.css?v=2', 'data-prelaunch-ui-hotfix');
    loadStylesheetOnce('/static/css/member-final-polish.css?v=1', 'data-member-final-polish');
    loadScriptOnce('/static/js/member-final-polish.js?v=1', 'data-member-final-polish');
    loadStylesheetOnce('/static/css/member-brand-refinement.css?v=2', 'data-member-brand-refinement');
  }

  if (openMonthlyRatingByDefault()) return;
  focusRatingPage();
  mountProfileRichBlocks();
  numberAllLeaderboards();
  enhanceReferralCopy();
  loadHiJackExtension();
})();