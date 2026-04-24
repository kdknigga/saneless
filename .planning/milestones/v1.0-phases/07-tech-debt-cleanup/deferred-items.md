# Deferred Items - Phase 07

## UI Bug: Scan button disabled by HTMX before-request bubbling

**Found during:** 07-02 Task 1
**Description:** The `hx-on::before-request` handler on the scan form disables the scan button for ALL HTMX requests originating within the form, including the `hx-trigger="load"` requests for tags and correspondents. This causes the scan button to be disabled after page load completes.
**Impact:** Low -- button still works after the initial HTMX requests settle, but it's a cosmetic issue.
**Suggested fix:** Scope the before-request handler to only fire for the form's own submit request, not child element requests. For example, check `event.detail.elt === this` in the handler.
