(() => {
  const STICKER_ACCEPT = ".png,.webp,.jpg,.jpeg,.gif,image/png,image/webp,image/jpeg,image/gif";

  function replaceDirectText(label, value) {
    const textNode = Array.from(label.childNodes).find(
      (node) => node.nodeType === Node.TEXT_NODE && node.textContent.trim()
    );
    if (textNode) textNode.textContent = `${value}\n        `;
  }

  function prepareStickerFieldset(fieldset) {
    fieldset.classList.add("reward-sticker-fieldset");

    const legend = fieldset.querySelector("legend");
    if (legend) legend.textContent = "Стикер награды";

    const hint = fieldset.querySelector(":scope > .muted");
    if (hint) {
      hint.textContent = "Загрузите готовый стикер. Он появится в каталоге, на главной и в активной JACK CARD.";
    }

    const picker = fieldset.querySelector(".reward-animation-picker");
    if (picker) {
      picker.querySelectorAll(".reward-animation-option").forEach((option) => {
        const input = option.querySelector('input[name="animation_choice"]');
        if (!input) return;

        if (input.value && input.value !== "__keep_upload__") {
          option.remove();
          return;
        }

        if (input.value === "") {
          const none = option.querySelector(".animation-none");
          if (none) none.textContent = "Без стикера";
        }

        if (input.value === "__keep_upload__") {
          const title = option.querySelector("strong");
          const note = option.querySelector("small");
          if (title) title.textContent = "Текущий стикер";
          if (note) note.textContent = "Оставить без изменений";
        }
      });
    }

    const upload = fieldset.querySelector('input[type="file"][name="animation_file"]');
    if (!upload) return;

    upload.accept = STICKER_ACCEPT;
    const label = upload.closest("label");
    if (!label) return;

    replaceDirectText(label, fieldset.classList.contains("compact") ? "Заменить стикер" : "Загрузить стикер");
    const note = label.querySelector("small");
    if (note) {
      note.textContent = "PNG, WebP, JPEG или GIF · лучше 512×512 · прозрачный фон · PNG до 2,5 МБ";
    }
  }

  function fixAccuracyHeading() {
    const heading = document.querySelector(".rating-overview-copy .member-eyebrow");
    if (!heading) return;
    heading.textContent = "Точность ответов";
    heading.style.whiteSpace = "nowrap";
  }

  function initRewardStickers() {
    document.querySelectorAll(".reward-animation-fieldset").forEach(prepareStickerFieldset);
    fixAccuracyHeading();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initRewardStickers, { once: true });
  } else {
    initRewardStickers();
  }
})();
