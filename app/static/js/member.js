document.querySelectorAll("[data-consent-form]").forEach((form) => {
  const checkbox = form.querySelector("[data-consent-checkbox]");
  const submit = form.querySelector("[data-consent-submit]");
  if (!checkbox || !submit) return;
  const sync = () => {
    submit.disabled = !checkbox.checked;
  };
  checkbox.addEventListener("change", sync);
  sync();
});
