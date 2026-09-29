// Installable app: registers the service worker and drives the "Install app"
// menu item and the dismissible phone banner (see templates/base.html).
(function () {
  var script = document.currentScript;
  if ("serviceWorker" in navigator && script && script.dataset.swUrl) {
    window.addEventListener("load", function () {
      navigator.serviceWorker.register(script.dataset.swUrl).catch(function () {});
    });
  }

  var standalone = window.matchMedia("(display-mode: standalone)").matches ||
    window.navigator.standalone === true;
  if (standalone) return;  // already running as the installed app

  var DISMISS_KEY = "pwaBannerDismissedAt";
  var DISMISS_DAYS = 30;
  var isIos = /iphone|ipad|ipod/i.test(navigator.userAgent) ||
    (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  var deferredPrompt = null;

  function dismissedRecently() {
    try {
      var at = Number(localStorage.getItem(DISMISS_KEY));
      return at && Date.now() - at < DISMISS_DAYS * 24 * 60 * 60 * 1000;
    } catch (e) {
      return false;
    }
  }

  function banner() {
    return document.getElementById("pwa-banner");
  }

  function showInstall() {
    document.querySelectorAll("[data-pwa-install-item]").forEach(function (el) {
      el.hidden = false;
    });
    if (banner() && !dismissedRecently()) {
      banner().hidden = false;
      document.body.classList.add("pwa-banner-open");
    }
  }

  function hideBanner() {
    if (banner()) banner().hidden = true;
    document.body.classList.remove("pwa-banner-open");
  }

  function hideInstall() {
    document.querySelectorAll("[data-pwa-install-item]").forEach(function (el) {
      el.hidden = true;
    });
    hideBanner();
  }

  function install() {
    if (deferredPrompt) {
      deferredPrompt.prompt();
      // The prompt can only be used once; the browser fires
      // beforeinstallprompt again later if the user said no.
      deferredPrompt.userChoice.then(hideInstall);
      deferredPrompt = null;
    } else if (isIos && window.bootstrap) {
      bootstrap.Modal.getOrCreateInstance(document.getElementById("pwa-ios-modal")).show();
    }
  }

  // Chrome/Edge/Samsung Internet (Android and desktop) fire this when the
  // site can be installed; keep it so our own button can open the prompt.
  window.addEventListener("beforeinstallprompt", function (event) {
    event.preventDefault();
    deferredPrompt = event;
    showInstall();
  });
  window.addEventListener("appinstalled", hideInstall);

  document.addEventListener("click", function (event) {
    if (event.target.closest("[data-pwa-install]")) {
      install();
    } else if (event.target.closest("[data-pwa-dismiss]")) {
      try {
        localStorage.setItem(DISMISS_KEY, String(Date.now()));
      } catch (e) {}
      hideBanner();
    }
  });

  // iPhone/iPad have no install prompt; offer the manual steps instead.
  if (isIos) {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", showInstall);
    } else {
      showInstall();
    }
  }
})();
