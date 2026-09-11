/* saneless -- Scan button state management via HTMX lifecycle events. */

(function () {
    "use strict";

    function resetScanButton() {
        var btn = document.getElementById("scan-btn");
        if (btn) {
            btn.disabled = false;
            btn.setAttribute("aria-busy", "false");
            btn.textContent = "Scan";
        }
    }

    function disableScanButton() {
        var btn = document.getElementById("scan-btn");
        if (btn) {
            btn.disabled = true;
            btn.setAttribute("aria-busy", "true");
            btn.textContent = "Scanning\u2026";
        }
    }

    document.addEventListener("htmx:afterSwap", function (evt) {
        var target = evt.detail.target;
        if (!target || target.id !== "status-area") return;
        // All three terminal states re-enable the button. Omitting the
        // consume-directory fallback would leave a successful-but-degraded
        // scan looking like a locked-up app until the user reloaded the page.
        if (target.querySelector(".status-done") ||
            target.querySelector(".status-error") ||
            target.querySelector(".status-fallback")) {
            resetScanButton();
        }
    });

    document.addEventListener("htmx:beforeRequest", function (evt) {
        var elt = evt.detail.elt;
        if (elt && elt.matches && elt.matches('form[hx-post="/api/scan"]')) {
            disableScanButton();
        }
    });
})();
