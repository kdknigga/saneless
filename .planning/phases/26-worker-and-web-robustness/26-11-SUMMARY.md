---
phase: 26-worker-and-web-robustness
plan: 11
subsystem: web
tags: [htmx, jinja, oob-swap, scan-button, robustness]
requires:
  - phase: 26-05
    provides: "#status-message slot and single error renderer"
  - phase: 26-10
    provides: "_status_context and the four status-rendering routes"
provides:
  - "partials/scan_button.html -- the only Scan button markup, oob flag"
  - "partials/status_response.html -- status + OOB button + optional OOB message clear"
  - "form hx-disabled-elt/hx-disinherit wiring; title input maxlength from TITLE_MAX_LENGTH"
  - "app.js deleted; no application JavaScript remains"
affects: [26-12]
tech-stack:
  added: []
  patterns:
    - "Server-owned UI state via hx-swap-oob re-render from a single Jinja include"
key-files:
  created:
    - src/saneless/web/templates/partials/scan_button.html
    - src/saneless/web/templates/partials/status_response.html
  modified:
    - src/saneless/web/templates/index.html
    - src/saneless/web/templates/base.html
    - src/saneless/web/routes.py
    - tests/test_web_state_rendering.py
  deleted:
    - src/saneless/web/static/app.js
key-decisions:
  - "Only POST /api/scan success sets clear_message; poll and flip responses never clear #status-message (D-03)"
  - "title_max_length passed via index context so templates own no vocabulary"
requirements-completed: [ROBU-04, ROBU-08, ROBU-11]
duration: 15min
completed: 2026-09-14
---

# Phase 26 Plan 11: Server-Owned Scan Button Summary

**The Scan button is rendered from one Jinja partial, re-rendered out-of-band on every status response, guarded in flight by `hx-disabled-elt` with `hx-disinherit`, and `app.js` is gone.**

## Performance

- **Duration:** ~15 min
- **Completed:** 2026-09-14
- **Tasks:** 2 (each a RED + GREEN commit pair)
- **Files:** 2 created, 4 modified, 1 deleted

## Accomplishments

- `partials/scan_button.html` is the only copy of the button; `index.html` includes it inline, `partials/status_response.html` includes it with `oob = true`.
- `start_scan` (success), `current_job_status`, `continue_flip`, `abort_flip` render `partials/status_response.html`; only `start_scan` passes `clear_message: True`.
- Scan form carries `hx-disabled-elt="#scan-btn"` and `hx-disinherit="hx-disabled-elt"` with the 2.0.8 inheritance trap documented in a Jinja comment.
- `#title-input` carries `maxlength="{{ title_max_length }}"`, fed from `TITLE_MAX_LENGTH` in the `index` route.
- `app.js` and its `<script>` tag removed; `/static/app.js` is a 404 through the 26-05 renderer.

## Task Commits

1. **Task 1 RED:** `059361a` test(26-11): add failing tests for the server-owned OOB Scan button (22 failing)
2. **Task 1 GREEN:** `c6b1b82` feat(26-11): render the Scan button from one partial, OOB on every status response
3. **Task 2 RED:** `726d24c` test(26-11): add failing tests for form wiring, title cap and app.js removal (5 failing)
4. **Task 2 GREEN:** `dd305c6` feat(26-11): wire hx-disabled-elt, cap the title input, delete app.js

## Tests added (tests/test_web_state_rendering.py)

- T1 `test_page_renders_one_inline_scan_button`
- T2 `test_poll_scan_button_matches_the_page_button[<9 JobStates>]` (byte identity minus ` hx-swap-oob="true"`)
- T2 `test_poll_scan_button_follows_the_state_table[<9 JobStates>]` (disabled / aria-busy / label; no `aria-busy="false"`)
- T3 `test_scan_success_carries_button_status_and_message_clear`, `test_poll_never_clears_the_status_message`, `test_flip_responses_never_clear_the_status_message[continue|abort]`
- Regression: `test_scan_error_response_carries_no_button` (422 unknown profile); idle button now also asserts no `aria-busy`
- Task 2: `test_scan_form_disables_the_button_without_inheritance`, `test_title_input_is_capped_at_the_server_limit`, `test_title_input_cap_comes_from_the_route_context` (monkeypatched to 99), `test_page_loads_no_app_script_and_no_remote_url`, `test_app_script_is_gone`

## Mutation self-checks (not committed)

- Appended `{% with oob = true %}{% include "partials/scan_button.html" %}{% endwith %}` to `partials/status.html`: `test_page_renders_one_inline_scan_button` failed with `assert 2 == 1` (duplicate `id="scan-btn"`). Reverted.
- Added `"clear_message": True` to the `current_job_status` context: `test_poll_never_clears_the_status_message` failed on `assert "status-message" not in text`. Reverted.

## Verification

- `uv run pytest tests/test_web_state_rendering.py tests/test_web.py tests/test_vendor_assets.py tests/test_web_errors.py -q`: 217 passed
- `uv run pytest -m "not browser and not sane_hardware" -q`: 1386 passed
- `ruff check`, `ruff format --check`, `ty check`, `pyrefly check src tests` (0 errors), `prek run --all-files`: all clean
- Acceptance greps: `id="scan-btn"` only in `partials/scan_button.html`; no `scan_button` in `status.html`; 4 `partials/status_response.html` in routes.py; one `"clear_message": True`; no `app.js` anywhere under `src/saneless`; `app.js` file absent; each form attribute count is 1; one `maxlength="{{ title_max_length }}"`

## Deviations from Plan

None in behaviour. One wording change: the `index.html` form comment says "the deleted app script" rather than "app.js", because acceptance criterion `grep -rn "app.js" src/saneless` must return nothing.

## Known follow-up (by plan design)

- `tests/test_browser.py` still has the app.js-mimicking test (~line 347) and an `aria-busy == "false"` assertion (~line 683). Both break now that app.js is gone. The plan said not to touch them here; plan 26-12 rewrites them and adds browser proofs B3, B5, B6, B7. The browser suite was not run for this plan.

## TDD Gate Compliance

`test(26-11)` commits (059361a, 726d24c) each come before their `feat(26-11)` commits (c6b1b82, dd305c6). Each RED run failed only on the new behaviours.

## Self-Check: PASSED

- FOUND: src/saneless/web/templates/partials/scan_button.html
- FOUND: src/saneless/web/templates/partials/status_response.html
- ABSENT (intended): src/saneless/web/static/app.js
- FOUND commits: 059361a, c6b1b82, 726d24c, dd305c6
