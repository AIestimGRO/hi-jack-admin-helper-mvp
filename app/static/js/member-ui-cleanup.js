(() => {
  const page = document.querySelector('.member-app-page');
  if (!page) return;

  const tab = page.dataset.accountTab || 'home';
  document.body.classList.add(`member-tab-${tab}`);

  function directChild(parent, selector) {
    if (!parent) return null;
    return Array.from(parent.children).find((child) => child.matches(selector)) || null;
  }

  function cleanHome() {
    if (tab !== 'home') return;

    const homeSections = Array.from(page.querySelectorAll(':scope > .home-section'));
    const nearest = homeSections.find((section) => {
      const head = section.querySelector(':scope > .app-section-head');
      const eyebrow = head?.querySelector('.member-eyebrow')?.textContent.trim();
      const heading = head?.querySelector('h2')?.textContent.trim();
      return eyebrow === 'Игра' || heading === 'Ближайшая игра' || heading === 'Сейчас в клубе';
    });
    if (nearest) {
      nearest.classList.add('home-nearest-clean');
      const head = nearest.querySelector(':scope > .app-section-head');
      const eyebrow = head?.querySelector('.member-eyebrow');
      if (eyebrow) eyebrow.textContent = 'Ближайшая игра';
      head?.querySelector('h2')?.remove();
      directChild(head, 'a')?.remove();
    }

    const jackCards = homeSections.find((section) => {
      const eyebrow = section.querySelector(':scope > .app-section-head .member-eyebrow');
      return eyebrow?.textContent.trim() === 'JACK CARDS';
    });
    if (jackCards) {
      jackCards.classList.add('home-jack-cards-clean');
      const head = jackCards.querySelector(':scope > .app-section-head');
      const eyebrow = head?.querySelector('.member-eyebrow');
      if (eyebrow && eyebrow.tagName !== 'A') {
        const link = document.createElement('a');
        link.className = 'member-eyebrow home-jack-cards-link';
        link.href = '/account?tab=vault';
        link.textContent = 'JACK CARDS';
        eyebrow.replaceWith(link);
      }
      head?.querySelector('h2')?.remove();
      directChild(head, 'a:not(.home-jack-cards-link)')?.remove();
    }
  }

  function cleanStore() {
    if (tab !== 'vault') return;
    const heading = page.querySelector('.vault-page-heading');
    if (!heading) return;
    const balance = heading.querySelector('.mini-balance');
    if (!balance) return;
    balance.classList.add('store-balance-chip');
    balance.querySelector(':scope > span')?.remove();
  }

  function cleanRating() {
    if (tab !== 'rating') return;

    const hero = page.querySelector('.rating-hub-hero');
    if (hero) {
      const title = hero.querySelector('h1');
      if (title) title.textContent = 'Рейтинг';
      hero.querySelector('.member-muted')?.remove();
    }

    const sectionParam = new URL(window.location.href).searchParams.get('section');
    if (sectionParam === 'club') return;

    const section = page.querySelector('.jackside-rating-section');
    if (!section) return;
    section.classList.add('is-clean-rating-list');
    directChild(section, '.app-section-head')?.remove();
    directChild(section, '.jackside-rating-note')?.remove();
  }

  function cleanProfile() {
    if (tab !== 'profile') return;

    page.querySelectorAll('.profile-rich-section').forEach((section) => {
      const heading = section.querySelector('h2');
      const text = heading?.textContent.trim();
      if (text === 'Главное в цифрах') {
        heading.remove();
        section.classList.add('profile-stat-clean-head');
      }
      if (text === 'Рефералы') {
        heading.remove();
        section.classList.add('profile-referral-clean-head');
      }
    });

    const card = page.querySelector('.profile-referral-card');
    if (!card) return;

    const updateTreeStats = () => {
      const stats = card.querySelector('.referral-tree-stats');
      if (!stats) return false;
      const values = Array.from(stats.querySelectorAll('b')).map((node) => Number(node.textContent.trim()) || 0);
      stats.classList.toggle('is-empty-referral-stats', values.length > 0 && values.every((value) => value === 0));
      return true;
    };

    if (updateTreeStats()) return;
    const observer = new MutationObserver(() => {
      if (updateTreeStats()) observer.disconnect();
    });
    observer.observe(card, { childList: true, subtree: true });
  }

  function installStableChat() {
    window.setTimeout(() => {
      const legacyLauncher = document.querySelector('[data-chat-launcher]');
      if (!legacyLauncher || legacyLauncher.dataset.stableChatReplacement === '1') return;

      const launcher = legacyLauncher.cloneNode(true);
      launcher.dataset.stableChatReplacement = '1';
      launcher.classList.remove('is-obstructed');
      launcher.classList.add('is-stable-chat');
      legacyLauncher.replaceWith(launcher);

      const syncUnread = () => {
        launcher.classList.toggle('has-unread', legacyLauncher.classList.contains('has-unread'));
        const sourceBadge = legacyLauncher.querySelector('.member-chat-unread');
        const targetBadge = launcher.querySelector('.member-chat-unread');
        if (sourceBadge && targetBadge) targetBadge.textContent = sourceBadge.textContent;
      };
      syncUnread();
      const legacySync = new MutationObserver(syncUnread);
      legacySync.observe(legacyLauncher, { attributes: true, childList: true, subtree: true, characterData: true });
      window.setTimeout(() => legacySync.disconnect(), 5000);

      let pinnedOpen = false;
      let frame = 0;
      const importantSelectors = [
        '.app-primary-action',
        '.vault-buy-button',
        '.reward-activate-button',
        '.profile-settings-link',
        '.store-tabs button',
        '.schedule-tabs button',
        '.rating-period-tabs a',
        '.rating-section-tabs a',
        '[data-chat-avoid]',
      ];

      const overlaps = (a, b) => !(
        a.right <= b.left ||
        a.left >= b.right ||
        a.bottom <= b.top ||
        a.top >= b.bottom
      );

      const expandedRect = () => {
        const nav = document.querySelector('.member-bottom-nav');
        const navRect = nav?.getBoundingClientRect();
        const size = window.innerWidth <= 720 ? 48 : 52;
        const rightGap = window.innerWidth <= 720 ? 12 : 14;
        const bottom = navRect && navRect.height > 0 ? navRect.top - 14 : window.innerHeight - 18;
        return {
          left: window.innerWidth - rightGap - size,
          right: window.innerWidth - rightGap,
          top: bottom - size,
          bottom,
        };
      };

      const refresh = () => {
        if (launcher.classList.contains('is-hidden') || pinnedOpen) {
          launcher.classList.remove('chat-collapsed');
          return;
        }
        const bubble = expandedRect();
        const important = Array.from(document.querySelectorAll(importantSelectors.join(','))).filter((node) => {
          if (node.closest('.member-bottom-nav') || node === launcher || launcher.contains(node)) return false;
          const style = getComputedStyle(node);
          if (style.display === 'none' || style.visibility === 'hidden') return false;
          const rect = node.getBoundingClientRect();
          return rect.width > 0 && rect.height > 0 && rect.bottom > 0 && rect.top < window.innerHeight;
        });
        launcher.classList.toggle('chat-collapsed', important.some((node) => overlaps(bubble, node.getBoundingClientRect())));
      };

      const schedule = () => {
        cancelAnimationFrame(frame);
        frame = requestAnimationFrame(refresh);
      };

      launcher.addEventListener('click', (event) => {
        if (!launcher.classList.contains('chat-collapsed')) return;
        event.preventDefault();
        event.stopPropagation();
        pinnedOpen = true;
        launcher.classList.remove('chat-collapsed');
        launcher.focus({ preventScroll: true });
      }, true);

      window.addEventListener('resize', schedule, { passive: true });
      window.addEventListener('scroll', schedule, { passive: true });
      schedule();
      window.setTimeout(schedule, 500);
    }, 300);
  }

  cleanHome();
  cleanStore();
  cleanRating();
  cleanProfile();
  installStableChat();
})();
