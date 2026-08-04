const players = document.querySelectorAll("[data-reward-animation-src]");

if (players.length) {
  import("./vendor/dotlottie/dotlottie-wc.js").then(({ setWasmUrl }) => {
    setWasmUrl("/static/js/vendor/dotlottie/dotlottie-player.wasm");
    players.forEach((host) => {
      const source = host.dataset.rewardAnimationSrc;
      if (!source || host.dataset.rewardAnimationReady) return;
      const player = document.createElement("dotlottie-wc");
      player.setAttribute("src", source);
      player.setAttribute("autoplay", "");
      player.setAttribute("loop", "");
      player.setAttribute("renderconfig", JSON.stringify({
        freezeOnOffscreen: true,
        devicePixelRatio: Math.min(window.devicePixelRatio || 1, 2),
      }));
      host.dataset.rewardAnimationReady = "true";
      host.replaceChildren(player);
    });
  }).catch(() => {
    players.forEach((host) => host.classList.add("reward-animation-error"));
  });
}
