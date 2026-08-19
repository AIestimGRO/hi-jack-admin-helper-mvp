(() => {
  const page = document.querySelector('.member-app-page[data-performance-render="server"]');
  if (!page) return;

  function mountProfile() {
    if (page.dataset.accountTab !== 'profile') return;

    const grid = page.querySelector('.profile-emblem-grid');
    grid?.querySelectorAll('.profile-emblem-card').forEach((details) => {
      details.addEventListener('toggle', () => {
        if (!details.open) return;
        grid.querySelectorAll('.profile-emblem-card[open]').forEach((other) => {
          if (other !== details) other.open = false;
        });
        if (!window.matchMedia('(max-width: 680px)').matches) return;
        window.requestAnimationFrame(() => {
          details.scrollIntoView({
            behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth',
            block: 'nearest',
            inline: 'center',
          });
        });
      });
    });

    document.querySelectorAll('[data-referral-copy]').forEach((button) => {
      button.addEventListener('click', async () => {
        const selector = button.getAttribute('data-referral-copy');
        const input = selector ? document.querySelector(selector) : null;
        if (!input) return;
        const original = button.textContent;
        try {
          await navigator.clipboard.writeText(input.value);
        } catch (_) {
          input.focus();
          input.select();
          document.execCommand('copy');
        }
        button.textContent = 'Скопировано';
        window.setTimeout(() => { button.textContent = original; }, 1400);
      });
    });
  }

  function mountVault() {
    if (page.dataset.accountTab !== 'vault') return;
    const grid = page.querySelector('[data-vault-catalog]');
    const pager = page.querySelector('[data-vault-pager]');
    const button = pager?.querySelector('[data-vault-load-more]');
    const status = pager?.querySelector('[data-vault-load-status]');
    const sentinel = pager?.querySelector('[data-vault-load-sentinel]');
    if (!grid || !pager || !button) return;

    let loading = false;
    let finished = false;

    const total = Math.max(0, Number(grid.dataset.total || 0));
    const pageSize = Math.max(1, Number(grid.dataset.pageSize || 6));

    const setStatus = (loaded) => {
      if (status) status.textContent = `${loaded} из ${total}`;
    };

    const loadMore = async () => {
      if (loading || finished) return;
      const offset = Math.max(0, Number(grid.dataset.nextOffset || 0));
      if (offset >= total) {
        finished = true;
        pager.hidden = true;
        return;
      }

      loading = true;
      button.disabled = true;
      button.textContent = 'Загружаем…';
      try {
        const response = await fetch(
          `/api/account/vault-catalog-page?offset=${encodeURIComponent(offset)}&limit=${encodeURIComponent(pageSize)}`,
          { credentials: 'same-origin', headers: { Accept: 'application/json' } },
        );
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const payload = await response.json();
        const html = String(payload.html || '').trim();
        if (html) grid.insertAdjacentHTML('beforeend', html);
        const nextOffset = Math.max(offset, Number(payload.next_offset || offset));
        grid.dataset.nextOffset = String(nextOffset);
        setStatus(nextOffset);
        window.HJCRewardAnimations?.mount?.(grid);
        finished = !payload.has_more || nextOffset >= total;
        if (finished) pager.hidden = true;
      } catch (_) {
        button.textContent = 'Повторить загрузку';
        button.disabled = false;
        loading = false;
        return;
      }

      loading = false;
      button.disabled = false;
      button.textContent = 'Показать ещё';
    };

    button.addEventListener('click', loadMore);
    if ('IntersectionObserver' in window && sentinel) {
      const observer = new IntersectionObserver((entries) => {
        if (entries.some((entry) => entry.isIntersecting)) loadMore();
      }, { rootMargin: '320px 0px' });
      observer.observe(sentinel);
    }
  }

  mountProfile();
  mountVault();
})();
