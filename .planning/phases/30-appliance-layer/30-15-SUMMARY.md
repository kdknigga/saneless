---
phase: 30-appliance-layer
plan: 15
subsystem: web
tags: [profile-select, htmx, aria-describedby, classify-source, pico, apple-05]

# Dependency graph
requires:
  - phase: 30-appliance-layer
    provides: "plan 30-02's ProfileConfig.label / .description fields"
  - phase: 30-appliance-layer
    provides: "plan 30-05's generated label and description text, and the A-3 backfill the how-to documents"
  - phase: 24-source-classification
    provides: "classify_source and SourceKind.uses_feeder -- the one source-classification rule the ordering consumes"
  - phase: 26-error-vocabulary
    provides: "RequestRejected / RequestRejection.UNKNOWN_PROFILE, which is already a 422"
provides:
  - "GET /api/profiles/description -- the sentence for one profile, text alone, 422-guarded"
  - "partials/profile_description.html -- a byte-empty body when there is no description"
  - "_ProfileOption and _profile_options(worker) -- the ordered option list index renders"
  - "the D-21 render-time reading of has_flatbed: no profile reporting a flatbed source"
  - "the S4 select markup, the #profile-description live slot and its :empty rule"
affects: [30-16, appliance-layer-form-help-text]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "a text-only htmx partial with Jinja whitespace control, so an empty value is a byte-empty body and CSS :empty can match"
    - "validate-by-lookup on a query parameter as the traversal-free alternative to a path parameter"
    - "a stable sort keyed on a boolean as the way to regroup without reordering inside a group"
    - "a browser test that asserts element identity after an htmx swap, which is what distinguishes innerHTML from outerHTML"

key-files:
  created:
    - src/saneless/web/templates/partials/profile_description.html
  modified:
    - src/saneless/web/routes.py
    - src/saneless/web/templates/index.html
    - src/saneless/web/static/app.css
    - tests/test_web.py
    - tests/test_app_lifespan.py
    - tests/test_browser.py

key-decisions:
  - "The description route reads ProfileConfig.description from the loaded config rather than calling _profile_description, so an operator who took a profile over sees their own wording (30-05's direct handoff)"
  - "has_flatbed is read at render time as 'no profile in the set reports a flatbed source', via classify_source -- no new probe, no new config key (D-21)"
  - "index passes `selected` alongside `selected_description`, so the highlighted option and the sentence beneath it cannot disagree once D-21 reorders the list"
  - "index.html's option line moved in task 1's GREEN commit, not task 2's, so no commit ever renders a dataclass repr into the page"

patterns-established:
  - "Pattern: a partial whose whole body is one value uses `-#}` whitespace control, because a lone whitespace text node is a child and defeats :empty"
  - "Pattern: an ordering rule consumes classify_source's answer and never the source string, so the exact-match auto rule is inherited rather than re-litigated"
  - "Pattern: an htmx swap contract is proven in Chromium by asserting the target element's identity and attributes survive the swap"

requirements-completed: [APPL-05]

# Metrics
duration: 17min
completed: 2026-09-16
---

# Phase 30 Plan 15: Profile Select with a Live Description Summary

**The Profile dropdown now reads in plain words, explains the selected profile in a sentence swapped in place by htmx, and puts the feeder first when the device has no glass.**

## Performance

- **Duration:** 17 min
- **Started:** 2026-09-16T15:14-05:00
- **Completed:** 2026-09-16T15:30-05:00
- **Tasks:** 2 (4 commits — TDD RED/GREEN per task)
- **Files created:** 1 · **Files modified:** 6

## Accomplishments

- `GET /api/profiles/description?profile=<name>` answers with the profile's sentence and nothing around it. The name is a **query parameter, not a path segment**, validated by one locked `worker.get_profile` lookup that both checks it and yields the profile — the same shape `start_scan` uses, so there is no check-then-read gap and no path is ever built from client input (T-30-69). An unknown name and an absent parameter are both 422.
- **The route reads `ProfileConfig.description` from the loaded config, not `_profile_description`.** This is 30-05's direct handoff, honoured exactly: an operator who took a profile over by removing `auto_generated` and writing their own wording sees their sentence, not the generated one.
- `partials/profile_description.html` is the text alone. Jinja whitespace control (`-#}`) makes an empty description a **byte-empty body** — a lone newline would be a text node, and CSS `:empty` would stop matching, leaving Pico's help-text margins behind as exactly the stray gap the rule exists to remove.
- `_profile_options` builds one `_ProfileOption` per configured profile. The `value` stays the profile name — the wire contract `POST /api/scan` already takes — and only the text changes. A blank human name falls back to the profile name (Amendment A-3), because a deployed config whose profiles predate this phase carries none until `saneless auto-profiles --force` is run.
- **D-21's `has_flatbed` is read at render time as "no profile in the set reports a flatbed source"**, through `classify_source`. No new device probe, no new config key — the generated profile set already mirrors the device's sources, and that is the server's evidence. Recorded here as the plan asked.
- The ordering consumes `classify_source`'s answer and never re-derives it. That is what keeps `"Automatic Document Feeder"` — whose first four letters are the whole of the automatic rule — in the feeder group; a dedicated test row asserts it.
- The regroup is a **stable sort keyed on a boolean**, so configuration order survives inside each group and the only thing that changes is which group leads.
- The select is UI-SPEC S4's markup: `aria-describedby`, `hx-get`, `hx-trigger="change"`, `hx-target="#profile-description"` and `hx-swap="innerHTML"` — never `outerHTML`, because the slot carries the id `aria-describedby` points at, the polite live region and Pico's `select + small` adjacency, and replacing the element would break all three at once.
- **Nothing was added to the `<form hx-post="/api/scan">` element.** The C-10 inheritance fix is byte-identical, asserted against a literal copy of the element rather than trusted to a diff.
- Two Chromium tests prove the swap for real: the GET fires on `change`, the select's own value rides it, the slot keeps its identity, its id and its live region across the swap, and it remains the select's adjacent sibling — which is both Pico's styling hook and the "one help line under Profile" contract (APPL-10).

## Task Commits

1. **Task 1: the description route, the partial, and feeder-first ordering**
   - `5926ea4` (test — RED gate)
   - `3912626` (feat — GREEN gate)
2. **Task 2: the select markup and the live description slot**
   - `83b5154` (test — RED gate)
   - `c1bfae3` (feat — GREEN gate)

No `refactor` commit was needed; neither GREEN left code worth cleaning up.

## Files Created/Modified

- `src/saneless/web/templates/partials/profile_description.html` *(created)* — one expression, whitespace-controlled, with a comment explaining why the slot must survive the swap and why the body must be byte-empty. Contains no `<` character at all.
- `src/saneless/web/routes.py` — imported `SourceKind` / `classify_source`; added the frozen slotted `_ProfileOption` and `_profile_options(worker)` above `index`; added `get_profile_description` beside the other GET-partial routes; `index` now passes the ordered option tuple plus `selected` and `selected_description`.
- `src/saneless/web/templates/index.html` — the S4 Profile block, with a comment answering the three questions a reader will stop on. The `<form>` element is untouched.
- `src/saneless/web/static/app.css` — one `#profile-description:empty` rule and its UI-SPEC comment. No new colour literal.
- `tests/test_web.py` — `TestProfileDescriptionRoute` (6), `TestProfileOrdering` (8) and `TestProfileSelectMarkup` (8). 105 tests in this file, up from 83.
- `tests/test_app_lifespan.py` — the new route registered in `_ROUTE_CALLS` (see deviation 1).
- `tests/test_browser.py` — `TestProfileDescriptionSwap` (2) and two descriptions on the shared browser settings (see deviation 2).

## Decisions Made

- **`index` passes `selected` as well as `selected_description`.** UI-SPEC S4's markup reads a `selected` variable, and the plan's action text named only `selected_description`. Leaving `selected` undefined would have rendered S4's conditional as permanently falsy — harmless but dead. It is set to the first option's name, which is what a browser selects when no option is marked, so the highlighted option and the sentence beneath it cannot disagree on first paint. That matters precisely because D-21's regrouping means the first option is no longer necessarily the first profile in the config file.
- **The `profile` query parameter is not length-bounded.** ROBU-08 bounds inputs that reach storage or rendering; this one reaches a dict lookup and is rejected before any work if it is not a configured name. A bound would be a second rule saying the same thing less precisely. T-30-71 already accepts that the route exposes no name the select does not.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 — Blocking] The new route failed `tests/test_app_lifespan.py`'s route-coverage tripwire**

- **Found during:** Task 2 (GREEN gate, full-suite run)
- **Issue:** `test_sane_lifecycle_across_startup_every_route_and_shutdown` asserts that every route the app serves is either named in `_ROUTE_CALLS` or excused in `_ROUTE_SKIPS`. That is deliberate — it is what makes a route added later covered on the day it lands — and `/api/profiles/description` was neither, so the HARD-05 proof failed.
- **Fix:** Registered the route in `_ROUTE_CALLS`, driven with `params={"profile": "default"}` so the handler runs its locked lookup and renders rather than short-circuiting on the 422 an unknown name would get. Commented with that reason.
- **Files modified:** `tests/test_app_lifespan.py`
- **Verification:** `uv run pytest tests/test_app_lifespan.py -q` → 19 passed.
- **Committed in:** `c1bfae3`.

**2. [Rule 2 — Missing critical validation] No test proved the htmx swap actually happens**

- **Found during:** Task 2 (GREEN gate)
- **Issue:** The plan's tests are all server-side. They prove the attributes are in the markup and that the route answers, but nothing proved htmx issues the GET on `change`, that the select's own value rides it, or — the whole point of `innerHTML` over `outerHTML` — that the slot survives the swap with its id and live region intact. `CLAUDE.md` requires browser-based validation of htmx interactivity and forbids deferring it to a human.
- **Fix:** Added `TestProfileDescriptionSwap` to `tests/test_browser.py`: one test selects the second profile and asserts the text changes *and* `#profile-description` still exists exactly once with `aria-live="polite"` and the select still points at it; one asserts `#profile-select + small` resolves to exactly that slot, which is Pico's own help-text selector and the APPL-10 "one help line" contract in the same assertion. The shared browser settings gained a description on each profile and **no** label, so `test_profile_dropdown_has_options` still reads the profile name as the option text.
- **Files modified:** `tests/test_browser.py`
- **Verification:** `uv run pytest -m browser -q` → 80 passed (78 before).
- **Committed in:** `c1bfae3`.

**3. [Rule 1 — Bug] Task 1's GREEN would have rendered a dataclass repr into the page**

- **Found during:** Task 1 (GREEN gate, planning the edit)
- **Issue:** The plan gives `index.html` to task 2 but has task 1 replace the context's bare name list with option objects. Followed literally, the commit at the end of task 1 renders `<option value="_ProfileOption(name='default', ...)">` — a real, shipped regression for the length of one commit, in a repository where every commit is expected to be sound.
- **Fix:** Moved the single option line to its S4 form (`value="{{ p.name }}"`, text `{{ p.label or p.name }}`, plus the `selected` conditional) in task 1's GREEN commit. Task 2's RED still failed on five of its eight assertions, so the gate stayed honest; the three that passed are the option line and the two regression guards that were always meant to pass.
- **Files modified:** `src/saneless/web/templates/index.html`
- **Verification:** `uv run pytest tests/test_web.py -q` → 97 passed at task 1 GREEN, with the page rendering real option text throughout.
- **Committed in:** `3912626`.

### Acceptance-criterion deviations (no code impact)

**`grep -n "hx-disabled-elt" src/saneless/web/templates/index.html` matches 6 lines, not the 1 the criterion predicts, and did so before this plan started.** Five of the six are prose in the file's existing comments (the section-0 placement note and the C-10 note) and the sixth is `hx-disinherit="hx-disabled-elt"`; only one line *sets* the attribute. The criterion is unsatisfiable as literally written against the base tree, so the contract it is reaching for is asserted behaviourally instead: `test_the_profile_select_adds_no_attribute_to_the_scan_form` pins the `<form>` element against a byte-for-byte literal, asserts `hx-disabled-elt="` occurs exactly once in the file, and asserts the rendered select tag carries none. This plan added **zero** new matching lines — the new comment names the attribute in prose rather than quoting it, precisely so the count did not move.

**`cat partials/profile_description.html | grep -c "<"` is 0, not "only the Jinja comment delimiters".** Jinja comment delimiters are `{#` and `#}` and contain no `<` at all, so the criterion's own arithmetic cannot be satisfied. Its stated intent — "the file contains no HTML element" — is met in the strongest available form: the file contains no angle bracket whatsoever.

**`uv run pytest tests/test_web.py -k "description or ordering"` selects 14, above the 9 required; `-k profile_select` selects 8, above the 7 required.**

---

**Total deviations:** 3 auto-fixed (1 blocking, 1 missing validation, 1 bug) + 3 acceptance-criterion notes.
**Impact on plan:** No scope creep in `src/`. Two of the three fixes are additional test coverage the project's own rules demanded; the third moved one template line one commit earlier than the plan's file split implied.

## Out-of-scope note

The brief handed to this executor carried a success criterion reading *"The read-only config mount is reported on the strip."* That belongs to plans **30-04** and **30-06**, not 30-15 — the string appears in neither this plan nor UI-SPEC S4 — and it is already delivered: `src/saneless/checks.py:789` carries the amber "the config location is read-only, so they are lost on restart" row, with the precedence rule at `:751`. Nothing was changed for it here, and nothing needed to be. Recording it so the mismatch is visible rather than silently dropped.

## Issues Encountered

- **The worktree started on the wrong base**, at `a87b3dd` rather than `632c483`. The tree was clean, so the startup guard's `git reset --hard` corrected it before any work began. Ninth consecutive wave; the guard remains load-bearing.
- **`tests/test_scanner.py::TestPeakPageMemory::test_live_page_images_falls_as_pages_are_released` failed once** in the first full-suite run, with a `PytestUnraisableExceptionWarning` about an unclosed sqlite connection. It was collateral: the lifespan test aborted at its assertion before closing its store, and the warning surfaced in a later test. It passed alone and passes in every run after deviation 1 was fixed.
- **The plan's `read_first` line numbers for `routes.py` are stale** (it points at `index` around line 186; it is at 555, and the validate-by-lookup precedent is at 808 rather than 408). The named symbols were found by search instead. Worth noting for the remaining wave-10 plans that quote the same file.

## Known Stubs

None. Every new code path returns real values: the route renders the configured description, `_profile_options` returns one entry per configured profile, and the template renders both.

## Threat Flags

None. The plan adds one GET route whose only input is a name checked against the configured profile set, and no schema change.

| Threat | Mitigation as built |
|---|---|
| T-30-69 (path traversal via `profile`) | A query parameter, never a path segment; one locked `get_profile` lookup validates it and yields the profile in the same call; a traversal-shaped name is asserted to get a 422 |
| T-30-70 (XSS via `label` / `description`) | Jinja autoescape, nothing marked `\|safe`; both fields bounded by plan 30-02's `max_length`; `test_a_description_containing_markup_is_escaped_not_rendered` asserts the escaped output |
| T-30-71 (name enumeration) | Accepted per the register: the select already renders every profile name on an unauthenticated `GET /` |
| T-30-72 (one lock acquisition per profile per render) | Accepted per the register: the profile set is one entry per device source and each lookup is a dict read under a short lock |
| T-30-73 (blank option after an upgrade) | The label falls back to the profile name; `test_a_blank_label_falls_back_to_the_profile_name` asserts it |
| T-30-SC (package installs) | Nothing was installed |

## Verification Results

All plan gates pass:

- `uv run pytest tests/test_web.py -q` → **105 passed**
- `uv run pytest tests/test_web.py tests/test_web_state_rendering.py tests/test_web_checks.py -q` → **348 passed**
- `uv run pytest -m "not browser and not sane_hardware" -q` → **2715 passed, 86 deselected**
- `uv run pytest -m browser -q` → **80 passed** (Chromium, offline egress gate)
- `uv run ruff check .` → All checks passed
- `uv run ruff format --check .` → 63 files already formatted
- `uv run ty check` → All checks passed
- `uv run pyrefly check src tests` → 0 errors

Acceptance greps:

- `@router.get("/api/profiles/description")` → 1 match
- `classify_source(` in `routes.py` → 1 match (≥1 required)
- `profile.label or name` → 1 match
- `async def` in `routes.py` → 0 matches
- `<` in `partials/profile_description.html` → 0 (see the criterion note above)
- `aria-describedby="profile-description"` in `index.html` → 1 match
- `hx-swap="innerHTML"` in `index.html` → 5 matches (≥1 required; four predate this plan)
- `id="profile-description"` in `index.html` → 1 match
- `#profile-description:empty` in `app.css` → 1 match
- `#[0-9a-fA-F]{6}` in `app.css` → 3, unchanged from before this plan
- `git diff 632c483 -- index.html` touches no line of the `<form hx-post="/api/scan"` element
- `pytest -k "description or ordering"` → 14 selected, all passing (≥9 required)
- `pytest -k profile_select` → 8 selected, all passing (≥7 required)

TDD gate sequence in `git log`: `test(30-15)` → `feat(30-15)` → `test(30-15)` → `feat(30-15)`.

## Next Phase Readiness

Ready for the help-text plan (APPL-10, UI-SPEC S6):

- Profile's help line is **done and must not be duplicated** — the live description *is* it. The remaining controls (Title, Tags, Filter tags, Correspondent) still need theirs, and `#profile-select + small` is the adjacency pattern to copy.
- `app.css` now has one `:empty` rule. The other help lines are static and need none.
- `_ProfileOption` is the only object the Profile block reads. A plan that wants to add a field to the dropdown adds it there and to `_profile_options`, not to the template's loop variable.

No blockers.

## Self-Check: PASSED

- `src/saneless/web/templates/partials/profile_description.html` exists on disk; all six modified files exist.
- All four commits (`5926ea4`, `3912626`, `83b5154`, `c1bfae3`) are present in `git log`.
- `git diff --diff-filter=D` over the plan's commit range shows no file deletions.
- Working tree clean; `.planning/STATE.md` and `.planning/ROADMAP.md` are untouched by this executor.
