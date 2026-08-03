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
      password.value.length >= 6 &&
      password.value.length <= 128 &&
      /\p{L}/u.test(password.value);
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

document.querySelectorAll("[data-member-countdown]").forEach((output) => {
  const target = Date.parse(output.dataset.memberCountdown || "");
  if (!Number.isFinite(target)) return;

  const render = () => {
    const remaining = Math.max(0, target - Date.now());
    if (remaining <= 0) {
      output.textContent = "Можно играть";
      return false;
    }
    const totalSeconds = Math.floor(remaining / 1000);
    const days = Math.floor(totalSeconds / 86400);
    const hours = Math.floor((totalSeconds % 86400) / 3600);
    const minutes = Math.floor((totalSeconds % 3600) / 60);
    const seconds = totalSeconds % 60;
    const clock = [hours, minutes, seconds]
      .map((value) => String(value).padStart(2, "0"))
      .join(":");
    output.textContent = days ? `${days} дн. · ${clock}` : clock;
    return true;
  };

  if (!render()) return;
  const timer = window.setInterval(() => {
    if (!render()) window.clearInterval(timer);
  }, 1000);
});

document.querySelectorAll("[data-reward-activation-countdown]").forEach((output) => {
  const target = Date.parse(output.dataset.rewardActivationCountdown || "");
  const card = output.closest("[data-reward-activation-card]");
  if (!Number.isFinite(target) || !card) return;

  const render = () => {
    const remaining = Math.max(0, target - Date.now());
    if (remaining <= 0) {
      output.textContent = "Код закрывается…";
      const rewardId = card.dataset.rewardId || "";
      window.location.replace(`/account?tab=vault#card-${encodeURIComponent(rewardId)}`);
      return false;
    }
    const totalSeconds = Math.ceil(remaining / 1000);
    const minutes = Math.floor(totalSeconds / 60);
    const seconds = totalSeconds % 60;
    output.textContent = `Код активен ${minutes}:${String(seconds).padStart(2, "0")}`;
    return true;
  };

  if (!render()) return;
  const timer = window.setInterval(() => {
    if (!render()) window.clearInterval(timer);
  }, 1000);
});
