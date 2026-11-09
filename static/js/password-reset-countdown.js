(function () {
  "use strict";

  document.querySelectorAll("[data-resend-countdown]").forEach(function (button) {
    var remainingMs = Number(button.getAttribute("data-remaining-ms"));
    var idleLabel = button.getAttribute("data-idle-label") || "Resend Email";
    if (!Number.isFinite(remainingMs) || remainingMs <= 0) {
      button.disabled = false;
      button.textContent = idleLabel;
      return;
    }

    button.disabled = true;
    var deadline = performance.now() + remainingMs;

    function updateCountdown() {
      var millisecondsLeft = Math.max(0, deadline - performance.now());
      if (millisecondsLeft === 0) {
        button.disabled = false;
        button.textContent = idleLabel;
        window.clearInterval(timer);
        return;
      }

      var seconds = Math.max(0, Math.ceil(millisecondsLeft / 1000) - 1);
      var minutesText = String(Math.floor(seconds / 60)).padStart(2, "0");
      var secondsText = String(seconds % 60).padStart(2, "0");
      button.textContent = "Resend Email in " + minutesText + ":" + secondsText;
    }

    updateCountdown();
    var timer = window.setInterval(updateCountdown, 1000);
  });
})();
