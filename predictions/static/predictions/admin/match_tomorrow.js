// Match admin: add a "Tomorrow" link after "Today" next to each date field
// (Start time and Prediction deadline), giving "Today | Tomorrow | [calendar]".
// Django builds the Today link in its own window "load" handler, so this runs
// on "load" too, deferred so it works whichever handler was registered first.
// The click reuses Django's own quick-link handler with an offset of +1 day.
(function () {
  "use strict";

  function label(text) {
    return typeof gettext === "function" ? gettext(text) : text;
  }

  function addTomorrowLink(inputId) {
    var input = document.getElementById(inputId);
    var shortcuts = input && input.nextElementSibling;
    var shortcutsClass = "datetimeshortcuts";
    if (!shortcuts || !shortcuts.classList.contains(shortcutsClass)) {
      return;
    }
    if (shortcuts.querySelector("a[data-tomorrow]")) {
      return;
    }
    var today = shortcuts.querySelector("a");
    var api = window.DateTimeShortcuts;
    if (!today || !api || !api.calendarInputs) {
      return;
    }
    var num = api.calendarInputs.indexOf(input);
    if (num === -1) {
      return;
    }

    var tomorrow = document.createElement("a");
    tomorrow.href = "#";
    tomorrow.setAttribute("role", "button");
    tomorrow.setAttribute("data-tomorrow", "");
    tomorrow.textContent = label("Tomorrow");
    tomorrow.addEventListener("click", function (e) {
      e.preventDefault();
      api.handleCalendarQuickLink(num, 1);
    });

    var separator = document.createTextNode(" | ");
    today.parentNode.insertBefore(separator, today.nextSibling);
    today.parentNode.insertBefore(tomorrow, separator.nextSibling);
  }

  window.addEventListener("load", function () {
    setTimeout(function () {
      addTomorrowLink("id_start_time_0");
      addTomorrowLink("id_prediction_deadline_0");
    }, 0);
  });
})();
