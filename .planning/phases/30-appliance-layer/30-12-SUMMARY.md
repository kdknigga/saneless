---
phase: 30-appliance-layer
plan: 12
subsystem: ui
tags: [jinja2, htmx, picocss, accessibility, error-handling, infoleak, wcag]

# Dependency graph
requires:
  - phase: 30-appliance-layer
    provides: "30-01's error_advice/ErrorAdvice, page_counts/PageCounted and local_time in vocabulary.py"
  - phase: 30-appliance-layer
    provides: "30-09's registration of error_message, error_next_step, page_counts and local_time as Jinja filters"
  - phase: 30-appliance-layer
    provides: "30-11's partials/terminal_reload.html, included unchanged by the rewritten ERROR branch"
provides:
  - "partials/status.html ERROR branch -- one role='alert' div over the sentence and the next step, with a collapsed details.tech-details outside it"
  - "partials/error.html -- the request-error slot's two-fact disclosure"
  - "the .page-counts line, in the status area and in the history Title cell"
  - "the history Time cell rendered through local_time, naming its zone"
  - "RequestRejected.job_id / render_error(job_id=...) -- one argument where refresh_history and an id would have been two"
  - "_SLOT_MESSAGE in tests/test_browser.py -- the slot's message paragraph, named so the disclosure is never measured by mistake"
affects: [30-13, 30-16, 30-17, browser-verification]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Collapsed <details> as the place untrusted or technical text goes, with the plain-language message outside it"
    - "A template comment must not contain the literal attribute it explains: the plan's grep gates count comment prose too"
    - "Content-box probing, not text-edge probing, when a <summary> is one of the compared elements"

key-files:
  created: []
  modified:
    - src/saneless/web/templates/partials/status.html
    - src/saneless/web/templates/partials/error.html
    - src/saneless/web/templates/partials/history.html
    - src/saneless/web/static/app.css
    - src/saneless/web/errors.py
    - src/saneless/web/routes.py
    - tests/test_web_state_rendering.py
    - tests/test_web_errors.py
    - tests/test_cross_origin.py
    - tests/test_browser.py

key-decisions:
  - "RequestRejected and render_error take one job_id where the plan said to add job_id alongside refresh_history: the sixth parameter would have tripped PLR0913, which this project forbids suppressing, and D-05 already ties the history reload to the row's existence"
  - "_record_refused_submit and _reject_created_job return str | None rather than bool, so the caller cannot hold a 'yes it was written' without holding which row"
  - "_job_in_state now records an ErrorCategory by default, because production never writes an ERROR row without one; the NULL row is an explicit opt-in and gets its own test"
  - "The hex-literal pin already in tests/test_web_checks.py is relied on rather than duplicated into test_web_state_rendering.py"
  - "The disclosure inherits the slot's inset: the rule names both of #status-message's children, because a disclosure a rem to the left of its sentence reads as another element's"

patterns-established:
  - "Pattern C holds through the disclosure: every sentence in both error surfaces comes from a filter or from Python; the templates carry only structure and comments"
  - "Browser assertions about what a user reads name the message paragraph, not the container, now that the container also holds a debugging aid"

requirements-completed: [APPL-03, APPL-04, APPL-12]

# Metrics
duration: 38min
completed: 2026-09-16
---

# Phase 30 Plan 12: Plain-Language Errors and Page Counts Summary

**Both error surfaces now read as a sentence plus a next step with the raw detail one tap away in a collapsed disclosure that carries no path, and every terminal job that recorded counts shows them — in the status area and as a second line in the history Title cell, beside a timestamp that finally names its zone.**

## Performance

- **Duration:** ~38 min
- **Completed:** 2026-09-16
- **Tasks:** 3 (7 commits: RED → GREEN per task, plus one browser fix)
- **Files modified:** 10 (0 created)

## Accomplishments

- **The alert covers the next step, not just the sentence (APPL-04).** `role="alert"` moved from the `<p>` to a wrapping `<div>`, so a screen-reader user hears what to do and not only what broke. Parametrised over all seven `ErrorCategory` members, with `text.count('role="alert"') == 1` asserted on the same response so nothing nests a second one.
- **The specific message was relocated, never deleted.** `job.error` is still rendered — inside the disclosure, beside the category and the job id. `vocabulary.error_message`'s docstring warns that swapping it for a category sentence is a regression; a test reads the disclosure body and asserts all three facts are there.
- **No log path can reach the page (D-13, T-30-52).** Two assertions, deliberately different in kind: the configured `log_file` (given a distinctive name in the fixture, so the check means something) is absent from both the status poll and `GET /`, and a source scan over `web/templates/**.html` finds no template that references `log_file`, `log_path` or `logfile` at all. The second is the durable half — it proves no template *could* leak one.
- **The pre-Phase-21 row still renders today's line byte for byte.** Substituting `UNKNOWN` would print "Something went wrong." over a row that still holds a truthful specific message, so the NULL-category branch is unchanged and pinned as a literal.
- **The request-error slot's disclosure carries exactly two facts, and says so.** Status code always; the refused submit's own job id when one was written. A parametrised test over every `RequestRejection` asserts the body holds one `<p>` when no row exists, and a second test asserts exactly two when one does — so a third fact cannot arrive unnoticed. Phase 26's twelve slot messages are untouched.
- **A measured `0` renders as `0` (D-32, Pitfall 4).** The guard is the `page_counts` filter returning `None`, never truthiness. A six-case parametrisation over the terminal cases UI-SPEC S3 enumerates asserts `.page-counts` is present for exactly the two that record counts and **absent entirely** for the other four — including a pre-Phase-23 `DONE` row, which proves the absence is the filter's doing and not the branch's. A source assertion forbids `if job.pages_` in any template.
- **Four columns, still (Pitfall 11 designed out).** The counts are a `<span class="page-counts">` inside the Title cell. The four `<th>`s and the single `colspan="4"` are both asserted, and `display: block` is what makes the span its own line in a `<td>` and in the status area alike — one class, one rule, no new colour literal (the stylesheet's three hex literals are unchanged, and `tests/test_web_checks.py`'s ordered pin still passes).
- **Every history timestamp names its zone (APPL-12).** `strftime` is gone from the template; `local_time` — the same object `cli.py` imports — renders the cell, driven under a pinned `TZ` with `time.tzset()`.
- **The browser suite grew from 68 to 70 and stayed green.** It caught a real visual defect this plan introduced; see deviation 2.

## Task Commits

1. **Task 1: the status-area ERROR branch and the technical-details disclosure** — `fc5a819` (test), `2275785` (feat)
2. **Task 2: the request-error slot's short disclosure** — `677b4e4` (test), `bec2409` (feat)
3. **Task 3: page counts and the local Time cell** — `4001079` (test), `2bd5586` (feat)
4. **Browser regression and alignment fix** — `97142c1` (fix)

## The three UI-SPEC-mandated test rewrites

All three were done deliberately, as "could not have passed before" rewrites, not as accidents:

| Test | Was | Now |
|---|---|---|
| `tests/test_web_state_rendering.py:247` (`test_status_area_prose`) | pinned `<p role="alert" class="status-error">&#10007; Error: disk on fire</p>` | pins `<p class="status-error">&#10007; {escaped error_message(category)}</p>`, plus an explicit assertion that the old `Error: ` prefix is gone |
| `tests/test_web_errors.py:206` (`_assert_htmx_error`, via `_error_paragraph`) | asserted the slot body **equals** the single `<p>` | `_error_body(rejection, status, job_id=None)` is the new exact expectation; `_error_paragraph` survives as its first line, so the message half is still pinned byte for byte. This one helper backs 14 call sites, which is why 43 tests went red at once |
| `tests/test_cross_origin.py:356` | same exact-body assertion on the 403 path | the same body plus a status-only disclosure, spelled out inline with a comment on why no job is named |

## Decisions Made

- **One `job_id` instead of `refresh_history` plus `job_id`.** The plan said to add `job_id` beside the existing flag. That would have put `render_error` at six parameters and tripped ruff's `PLR0913`, which CLAUDE.md forbids suppressing. Collapsing is also the truer model: D-05 says history reloads *because the refused attempt wrote a row*, which is exactly when there is an id to name. `render_error` computes `refresh_history = job_id is not None` and hands it to the template, so the partial keeps no rule of its own and its `refresh_history` branch is untouched. The `_reject_created_job` failure path returns `None`: the row exists but is still PENDING and owed to the worker, so naming it would point the reader at a row that does not yet say it was rejected.
- **The two refusal helpers return `str | None`, not `bool`.** A caller can no longer hold "yes, a row was written" without holding *which* row.
- **`_job_in_state` records a category by default.** Production never writes an ERROR row without one — the worker always classifies — so a rendering-contract test that drove ERROR with a NULL category was exercising the legacy path as though it were the main one. The legacy row is now an explicit `error_category=None` with its own test.
- **The hex-literal pin was not duplicated.** `tests/test_web_checks.py` already pins app.css's colour literals as an ordered list; this plan adds no literal, and a second copy of that list in another file would be two things to update. Verified it still passes rather than restating it.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] `render_error` could not take a sixth parameter**

- **Found during:** Task 2 GREEN
- **Issue:** The plan's literal instruction — add `job_id` to the error context alongside `refresh_history` — puts `render_error` at six non-self parameters. Ruff's `PLR0913` caps it at five, and CLAUDE.md forbids `# noqa` and rule disabling.
- **Fix:** Collapsed the two into one `job_id`, with `refresh_history` derived in Python. See Decisions above for why this is also the better model rather than merely the legal one.
- **Files modified:** `src/saneless/web/errors.py`, `src/saneless/web/routes.py`, `tests/test_web_errors.py`
- **Commit:** `bec2409`

**2. [Rule 1 - Bug] The disclosure rendered a rem to the left of the message it explains**

- **Found during:** post-task browser validation (CLAUDE.md mandates it; the plan's verification block does not run `-m browser`)
- **Issue:** `app.css`'s inset rule was `#status-message > p`. Giving the slot a second child meant the `<details>` got no inset, so "Technical details" hung outside the left edge of the sentence above it. Confirmed in chromium at both 1280 px and 375 px before the fix — not inferred from the CSS.
- **Fix:** The rule now names both of the slot's children. A new parametrised browser test measures the two elements' **content boxes**, not their text edges, because a `<summary>`'s disclosure marker makes the text edge the wrong thing to compare.
- **Files modified:** `src/saneless/web/static/app.css`, `tests/test_browser.py`, and `tests/test_web_errors.py`'s `_CSS_RULE` regex pin
- **Commit:** `97142c1`

**3. [Rule 1 - Bug] Six browser tests read the slot's whole text**

- **Found during:** post-task browser validation
- **Issue:** `TestRequestErrorSlot` asserted `to_have_text` on `#status-message` and probed `#status-message p`. Both now include the disclosure, so what the tests measured was no longer what the user reads as the message.
- **Fix:** One new `_SLOT_MESSAGE = "#status-message p.status-error"` constant, used by the text, colour, left-edge and line-count assertions. These were not in the UI-SPEC's rewrite budget; they are a consequence of the same change and were rewritten to say what they meant.
- **Files modified:** `tests/test_browser.py`
- **Commit:** `97142c1`

**4. [Rule 1 - Bug] Test expectations compared unescaped copy**

- **Found during:** Task 1 GREEN
- **Issue:** `ASSEMBLY`'s next step contains an apostrophe, which Jinja renders as `&#39;`. The first draft of the assertion compared the raw constant, which would have exempted exactly the copy most likely to carry punctuation.
- **Fix:** The expectations go through `markupsafe.escape`, the convention already used in `test_web_errors.py` and `test_cross_origin.py`.
- **Commit:** `2275785`

### Notes, not deviations

- **Template comments count toward the plan's grep gates.** Three acceptance criteria failed on first run purely because a comment explaining `role="alert"`, `colspan="4"`, `local_time` or `page_counts` contained the literal string being counted. Every such comment was reworded to describe the thing rather than quote it, so the counts now measure real attributes and expressions only. Worth knowing for the rest of the phase.
- **`tests/test_browser.py`'s colour parametrisation was *not* extended with `page-counts`.** The UI-SPEC's rewrite table lists it together with `check-ok` / `check-warn` / `check-fail`, none of which 30-11 added either, so that row belongs to whichever plan takes the strip's colour coverage. `.page-counts` reads `var(--pico-muted-color)` — the identical computed colour as `.status-cancelled`, which the existing parametrisation already proves meets AA in both schemes across all three contexts — so nothing is unmeasured, only un-restated. Flagged here rather than half-done.

## Threat Flags

None. The two information-disclosure threats this plan owns are both mitigated and tested:

| Threat | Mitigation | Test |
|---|---|---|
| T-30-52 status disclosure | `job.error`, category, job id only; no log path | rendered-page assertion + template source scan |
| T-30-53 slot disclosure | status code, and job id only when a row was written | exact-body equality at 14 call sites + a `<p>`-count ceiling |
| T-30-54 XSS via `job.error` | autoescape, no `\|safe` introduced | a `<script>` payload is asserted escaped inside the disclosure; `grep -rn "\|safe" templates/` is empty |
| T-30-55 rendering `0` for NULL | the filter returns `None`; no truthiness guard | six-case parametrisation + source scan for `if job.pages_` |
| T-30-56 next step outside the alert | the alert wraps both | the alert body is captured and both strings asserted inside it; `<details` asserted outside |

## Known Stubs

None.

## Verification

- `uv run pytest tests/test_web_state_rendering.py tests/test_web_errors.py tests/test_cross_origin.py -q` — **269 passed**
- `uv run pytest -m "not browser and not sane_hardware" -q` — **2615 passed, 76 deselected**
- `uv run pytest -m browser -q` — **70 passed** (was 68 before this plan)
- `grep -rniE "log_file|log_path|logfile" src/saneless/web/templates/` — empty
- `grep -rn "|safe" src/saneless/web/templates/` — empty
- `uv run ruff check .` / `ruff format --check .` / `uv run ty check` / `uv run pyrefly check src tests` — all clean
- Acceptance greps: `details class="tech-details"` = 1 in each of status.html and error.html; `role="alert"` = 2 in status.html and 0 in error.html; ` open` = 0; `error_next_step` = 1; `page_counts` = 2 in status.html and 1 in history.html; `if job.pages_` = 0; `colspan="4"` = 1; `<th>` = 4; `strftime` = 0; `.page-counts` = 1 in app.css; six-digit hex literals still 3.

## Self-Check: PASSED
