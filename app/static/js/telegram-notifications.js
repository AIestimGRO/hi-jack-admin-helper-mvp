(() => {
  const root = document.querySelector("[data-telegram-admin]");
  if (!root) return;

  const composer = root.querySelector("[data-telegram-composer]");
  if (!composer) return;

  const message = composer.querySelector("[data-telegram-message]");
  const count = composer.querySelector("[data-telegram-count]");
  const previewText = root.querySelector("[data-telegram-preview-text]");
  const buttonText = composer.querySelector("[data-telegram-button-text]");
  const buttonUrl = composer.querySelector("[data-telegram-button-url]");
  const previewButton = root.querySelector("[data-telegram-preview-button]");
  const category = composer.querySelector("[data-telegram-category]");
  const audience = root.querySelector("[data-telegram-audience]");

  const refreshPreview = () => {
    const text = (message?.value || "").trim();
    if (previewText) previewText.textContent = text || "Текст сообщения появится здесь.";
    if (count) count.textContent = String(message?.value.length || 0);

    const label = (buttonText?.value || "").trim();
    const href = (buttonUrl?.value || "").trim();
    if (previewButton) {
      previewButton.textContent = label || "Кнопка";
      previewButton.href = href || "#";
      previewButton.hidden = !label;
    }
  };

  const refreshAudience = async () => {
    if (!audience || !category) return;
    try {
      const response = await fetch(
        `/api/master/telegram/audience-preview?category=${encodeURIComponent(category.value)}`,
        { credentials: "same-origin", headers: { Accept: "application/json" } },
      );
      if (!response.ok) return;
      const payload = await response.json();
      audience.textContent = String(payload.count ?? "—");
    } catch (_) {
      audience.textContent = "—";
    }
  };

  [message, buttonText, buttonUrl].forEach((node) => {
    node?.addEventListener("input", refreshPreview);
  });
  category?.addEventListener("change", refreshAudience);
  refreshPreview();
})();
