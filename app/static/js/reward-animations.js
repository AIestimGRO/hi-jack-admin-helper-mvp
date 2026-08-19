let dotlottieModulePromise = null;

function isStaticSticker(source) {
  try {
    const pathname = new URL(source, window.location.origin).pathname.toLowerCase();
    return /\.(?:png|webp|gif|jpe?g)$/.test(pathname);
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

function dotlottieModule() {
  if (!dotlottieModulePromise) {
    dotlottieModulePromise = import("./vendor/dotlottie/dotlottie-wc.js").then((module) => {
      module.setWasmUrl("/static/js/vendor/dotlottie/dotlottie-player.wasm");
      return module;
    });
  }
  return dotlottieModulePromise;
}

function mountRewardAnimations(root = document) {
  const hosts = Array.from(root.querySelectorAll("[data-reward-animation-src]"))
    .filter((host) => !host.dataset.rewardAnimationReady);
  const animated = [];

  hosts.forEach((host) => {
    const source = host.dataset.rewardAnimationSrc;
    if (!source) return;
    if (isStaticSticker(source)) {
      renderStaticSticker(host, source);
      return;
    }
    animated.push(host);
  });

  if (!animated.length) return;
  dotlottieModule().then(() => {
    animated.forEach((host) => {
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
    animated.forEach((host) => host.classList.add("reward-animation-error"));
  });
}

window.HJCRewardAnimations = { mount: mountRewardAnimations };
mountRewardAnimations(document);
