const players = Array.from(document.querySelectorAll("[data-reward-animation-src]"));

function isStaticSticker(source) {
  try {
    const pathname = new URL(source, window.location.origin).pathname.toLowerCase();
    return /\.(?:png|webp|gif)$/.test(pathname);
  } catch (_error) {
    return false;
  }
}

function renderStaticSticker(host, source) {
  if (host.dataset.rewardAnimationReady) return;

  const image = document.createElement("img");
  image.src = source;
  image.alt = "";
  image.loading = "lazy";
  image.decoding = "async";
  image.draggable = false;
  image.className = "reward-sticker-image";
  image.style.width = "100%";
  image.style.height = "100%";
  image.style.display = "block";
  image.style.objectFit = "contain";

  host.dataset.rewardAnimationReady = "true";
  host.classList.add("reward-sticker-host");
  host.replaceChildren(image);
}

const animationPlayers = players.filter((host) => {
  const source = host.dataset.rewardAnimationSrc;
  if (!source) return false;
  if (!isStaticSticker(source)) return true;
  renderStaticSticker(host, source);
  return false;
});

if (animationPlayers.length) {
  import("./vendor/dotlottie/dotlottie-wc.js").then(({ setWasmUrl }) => {
    setWasmUrl("/static/js/vendor/dotlottie/dotlottie-player.wasm");
    animationPlayers.forEach((host) => {
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
    animationPlayers.forEach((host) => host.classList.add("reward-animation-error"));
  });
}
