(() => {
  const target = document.querySelector('.member-profile-button');
  if (!target) return;

  fetch('/api/account/product-shell', {
    credentials: 'same-origin',
    headers: { Accept: 'application/json' },
  }).then((response) => response.ok ? response.json() : null).then((state) => {
    const url = state?.profile?.avatar_url;
    if (!url) return;
    let image = target.querySelector('img[data-product-avatar]');
    if (!image) {
      image = document.createElement('img');
      image.dataset.productAvatar = '1';
      image.alt = '';
      target.prepend(image);
    }
    image.classList.toggle('is-sticker', state?.profile?.avatar_kind === 'sticker');
    image.src = `${url}?v=${Date.now()}`;
    image.addEventListener('error', () => {
      image.remove();
      target.classList.remove('has-product-avatar');
    }, { once: true });
    target.classList.add('has-product-avatar');
  }).catch(() => {});
})();
