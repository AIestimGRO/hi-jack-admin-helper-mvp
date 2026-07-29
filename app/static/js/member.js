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

document.querySelectorAll("[data-registration-form]").forEach((form) => {
  const submit = form.querySelector("[data-registration-submit]");
  const password = form.querySelector("[data-registration-password]");
  const confirmation = form.querySelector(
    "[data-registration-password-confirmation]"
  );
  const mismatch = form.querySelector("[data-password-match-message]");
  if (!submit || submit.disabled || !password || !confirmation) return;

  const sync = () => {
    const passwordsMatch =
      !confirmation.value || password.value === confirmation.value;
    const passwordMeetsRules =
      password.value.length >= 10 &&
      password.value.length <= 128 &&
      /\p{L}/u.test(password.value) &&
      /\d/.test(password.value);
    submit.disabled =
      !form.checkValidity() || !passwordsMatch || !passwordMeetsRules;
    if (mismatch) {
      mismatch.hidden = passwordsMatch;
    }
  };

  form.addEventListener("input", sync);
  form.addEventListener("change", sync);
  sync();
});
