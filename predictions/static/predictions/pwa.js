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

// Match alerts (see predictions/push.py): the number on the app icon, and the
// "Match alerts" menu button that turns on notifications for this device.
(function () {
  var script = document.currentScript;
  if (!script || !("serviceWorker" in navigator)) return;
  var data = script.dataset;
  var TAG = "new-matches";  // same as push.NOTIFICATION_TAG
  var SYNCED_KEY = "pushSynced";

  // The icon number: open matches this user hasn't predicted, refreshed on
  // every page so it drops as they predict.
  if (data.badgeCount !== undefined) {
    var count = Number(data.badgeCount) || 0;
    if (navigator.setAppBadge) {
      (count ? navigator.setAppBadge(count) : navigator.clearAppBadge()).catch(function () {});
    }
    if (!count) {
      // Nothing left to predict: clear the alert too (on Android the
      // notification is what puts the dot on the icon).
      navigator.serviceWorker.ready
        .then(function (reg) { return reg.getNotifications({ tag: TAG }); })
        .then(function (list) { list.forEach(function (n) { n.close(); }); })
        .catch(function () {});
    }
  }

  // iPhone only offers push inside the installed app (iOS 16.4+), so there
  // PushManager is missing in a normal Safari tab and the button stays hidden.
  if (!data.vapidKey || !("PushManager" in window) || !("Notification" in window)) return;

  // Which menu button to show: "enable" (the site can ask), "blocked" (the
  // user said no, so only the phone's settings can undo it) or null (on).
  function showButton(which) {
    document.querySelectorAll("[data-push-item]").forEach(function (el) {
      el.hidden = which !== "enable";
    });
    document.querySelectorAll("[data-push-blocked-item]").forEach(function (el) {
      el.hidden = which !== "blocked";
    });
  }

  function platform() {
    var ua = navigator.userAgent;
    if (/iphone|ipad|ipod/i.test(ua) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1)) {
      return "ios";
    }
    return /android/i.test(ua) ? "android" : "desktop";
  }

  function showBlockedHelp() {
    var modal = document.getElementById("push-blocked-modal");
    if (!modal || !window.bootstrap) return;
    var device = platform();
    modal.querySelectorAll("[data-push-help]").forEach(function (el) {
      el.hidden = el.dataset.pushHelp !== device;
    });
    bootstrap.Modal.getOrCreateInstance(modal).show();
  }

  function keyBytes(base64url) {
    var padded = (base64url + "===".slice((base64url.length + 3) % 4))
      .replace(/-/g, "+").replace(/_/g, "/");
    var raw = atob(padded);
    var bytes = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
    return bytes;
  }

  // Tell the server this device belongs to the logged-in user. Only sent
  // again when the device or the user changes.
  function save(subscription, force) {
    document.querySelectorAll("[data-push-endpoint]").forEach(function (input) {
      input.value = subscription.endpoint;
    });
    var marker = data.user + "|" + subscription.endpoint;
    try {
      if (!force && localStorage.getItem(SYNCED_KEY) === marker) return Promise.resolve();
    } catch (e) {}
    return fetch(data.subscribeUrl, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRFToken": data.csrf },
      body: JSON.stringify(subscription),
    }).then(function (response) {
      if (!response.ok) return;
      try { localStorage.setItem(SYNCED_KEY, marker); } catch (e) {}
    });
  }

  var ready = navigator.serviceWorker.ready;

  function subscribe() {
    return ready
      .then(function (reg) {
        return reg.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: keyBytes(data.vapidKey),
        });
      })
      .then(function (subscription) { return save(subscription, true); })
      .then(function () { showButton(null); })
      .catch(function () { showButton("enable"); });
  }

  function refresh() {
    ready
      .then(function (reg) { return reg.pushManager.getSubscription(); })
      .then(function (subscription) {
        var permission = Notification.permission;
        if (permission === "granted") {
          // Allowed -- possibly just now, in the phone's settings after
          // blocking: make sure this device is subscribed.
          showButton(null);
          return subscription ? save(subscription, false) : subscribe();
        }
        showButton(permission === "denied" ? "blocked" : "enable");
      })
      .catch(function () {});
  }

  refresh();
  // Coming back to the app, e.g. from the phone's settings: check again.
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") refresh();
  });

  document.addEventListener("click", function (event) {
    if (event.target.closest("[data-push-blocked]")) {
      showBlockedHelp();
      return;
    }
    if (!event.target.closest("[data-push-enable]")) return;
    Notification.requestPermission().then(function (permission) {
      if (permission === "granted") return subscribe();
      showButton(permission === "denied" ? "blocked" : "enable");
    }).catch(function () {});
  });

  // Logging out removes this device on the server (see signals.py), so it
  // must be sent again at the next login.
  document.addEventListener("submit", function (event) {
    if (event.target.querySelector("[data-push-endpoint]")) {
      try { localStorage.removeItem(SYNCED_KEY); } catch (e) {}
    }
  });
})();
