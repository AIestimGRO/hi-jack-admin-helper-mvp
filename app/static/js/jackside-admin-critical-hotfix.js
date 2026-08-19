(() => {
  const builder = document.querySelector('[data-quiz-builder]');
  if (!builder || builder.dataset.campaignType !== 'daily_414') return;

  builder.querySelectorAll('.delete-question-form').forEach((form) => {
    if (form.dataset.hjDeleteInstalled === '1') return;
    form.dataset.hjDeleteInstalled = '1';

    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      event.stopImmediatePropagation();

      const title = form.dataset.questionTitle || 'этот вопрос';
      if (!window.confirm(`Удалить вопрос «${title}»? Старые результаты участников сохранятся.`)) return;

      const button = form.querySelector('button[type="submit"]');
      const original = button?.textContent || 'Удалить';
      if (button) {
        button.disabled = true;
        button.textContent = 'Удаляю…';
      }

      try {
        const response = await fetch(form.action, {
          method: 'POST',
          body: new FormData(form),
          credentials: 'same-origin',
          redirect: 'follow',
        });
        if (!response.ok) throw new Error(`Ошибка ${response.status}`);

        const card = form.closest('[data-question-card]');
        card?.remove();
        if (window.HJAdminToast) window.HJAdminToast('Вопрос удалён');
      } catch (error) {
        if (window.HJAdminToast) {
          window.HJAdminToast(error?.message || 'Не удалось удалить вопрос', 'error');
        }
        if (button) {
          button.disabled = false;
          button.textContent = original;
        }
      }
    }, true);
  });
})();
