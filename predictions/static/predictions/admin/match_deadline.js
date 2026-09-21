// Match admin: copy Start time into Prediction deadline as it is entered.
// The admin splits each datetime into a date input (_0) and a time input (_1).
// A deadline part follows the start part only while it is empty or still equal
// to the start's previous value, so a deadline the admin has edited by hand is
// left alone. Every field stays editable.
(function () {
  "use strict";

  document.addEventListener("DOMContentLoaded", function () {
    var startParts = [
      document.getElementById("id_start_time_0"),
      document.getElementById("id_start_time_1"),
    ];
    var deadlineParts = [
      document.getElementById("id_prediction_deadline_0"),
      document.getElementById("id_prediction_deadline_1"),
    ];
    if (startParts.concat(deadlineParts).some(function (el) { return !el; })) {
      return;
    }

    var previous = startParts.map(function (el) { return el.value; });

    function syncDeadline() {
      startParts.forEach(function (start, i) {
        var deadline = deadlineParts[i];
        if (deadline.value === "" || deadline.value === previous[i]) {
          deadline.value = start.value;
        }
        previous[i] = start.value;
      });
    }

    startParts.forEach(function (start) {
      // "input"/"change" cover typing. The admin's calendar and clock pickers
      // set .value directly (no event) and then call .focus() on the input, so
      // "focus" catches a picked value; "blur" catches leaving the field.
      ["input", "change", "focus", "blur"].forEach(function (type) {
        start.addEventListener(type, syncDeadline);
      });
    });
  });
})();
