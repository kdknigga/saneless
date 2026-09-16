---
phase: 30-appliance-layer
plan: 16
subsystem: web
tags: [tag-picker, htmx, form-owner, touch-targets, help-text, apple-10]

# Dependency graph
requires:
  - phase: 30-appliance-layer
    provides: "plan 30-02's [web] show_tags / show_correspondent config keys"
  - phase: 30-appliance-layer
    provides: "plan 30-15's live profile description, which is Profile's one help line"
  - phase: 30-appliance-layer
    provides: "plan 30-14's pinned scan_button.html opening attributes"
provides:
  - "GET /api/tags?q=&tags= -- the filtered, selection-preserving checkbox list"
  - "partials/tags.html -- the whole swap target, wrapper and both loops"
  - "_tag_list_context(state, q=, selected=, on_page_load=) -- one context, three callers"
  - "the four S6 help sentences and their aria-describedby wiring"
  - "#tag-filter-form -- the empty sibling form that owns the filter input"
  - "label.tag-option / .tag-list -- the 44 px tap target and the scrolling list"
  - "start_scan's D-29 fallback to the profile's default_tags / default_correspondent"
affects: [30-17, appliance-layer-browser-colour]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "a partial that renders its own wrapper, because the swap is outerHTML"
    - "a load trigger rendered only on the full page render, so a swapped-in wrapper cannot re-trigger itself"
    - "the HTML form-owner attribute as the way to keep a nested input out of its DOM ancestor's submit"
    - "a request-counting httpx.MockTransport as the proof that a filter costs zero upstream calls"
    - "a Chromium measurement as the proof of a cascade outcome a source assertion cannot reach"

key-files:
  created: []
  modified:
    - src/saneless/web/routes.py
    - src/saneless/web/templates/partials/tags.html
    - src/saneless/web/templates/index.html
    - src/saneless/web/static/app.css
    - tests/test_web.py
    - tests/test_web_state_rendering.py
    - tests/test_browser.py

key-decisions:
  - "partials/tags.html renders the #tags-list wrapper itself, because an outerHTML swap replaces its target -- UI-SPEC S6 showed the wrapper in index.html as well, which would have doubled it"
  - "the wrapper's hx-get/hx-trigger=load attributes render only on a full page render; a swapped-in wrapper that still asked to load itself looped for ever (caught in Chromium)"
  - "the tag context key is selected_tags, not selected, because index already uses selected for the profile the page opens on and an include shares its parent's context"
  - "start_scan gained the D-29 fallback: the web layer had never applied default_tags or default_correspondent, so 'the defaults still apply' was a claim about code that did not exist"
  - "index.html mentions show_tags twice, not once: the filter form is a sibling of the scan form and cannot share the fieldset's {% if %}"

patterns-established:
  - "Pattern: a partial that owns its own swap target renders its request attributes conditionally, or the swap re-arms itself"
  - "Pattern: an input that must not be submitted with its DOM-ancestor form names a different form owner, rather than the ancestor filtering it out -- the ancestor form element stays untouched"
  - "Pattern: a filter over cached data is a Python-side substring test, never a parameter forwarded upstream, and never rendered back"

requirements-completed: [APPL-10]

# Metrics
duration: 42min
completed: 2026-09-16
---

# Phase 30 Plan 16: Form Help Text and the Thumb-Friendly Tag Picker Summary

**Tags are a filterable checkbox list with 44 px rows that cannot silently lose a tick, every control explains itself in one plain line, and the owner can reduce the form to Profile, Title, Scan without changing what a scan does.**

## Performance

- **Duration:** 42 min
- **Tasks:** 3 (6 commits — TDD RED/GREEN per task)
- **Files created:** 0 · **Files modified:** 7

## Accomplishments

- **The multi-select is gone.** `GET /api/tags` renders `<div id="tags-list" class="tag-list">` full of `<label class="tag-option"><input type="checkbox" name="tags" …>` rows. One control, one partial, one set of tests (D-30).
- **A filter that cannot drop a tick (A-5).** `hx-include="#tags-list"` carries the currently ticked ids with every filter request — htmx gathers only *checked* boxes — and `_tag_list_context` splits the cached list into `pinned` (ticked, but excluded by the filter) and `tags` (the filtered list). The pinned rows render **above** the filtered ones, so a tick can never leave the DOM and therefore can never be dropped from the next submit. A tag that is both ticked and matched is rendered by the filtered loop alone, so it appears once.
- **`q` never leaves the process and never comes back.** It is a Python-side case-insensitive `in` over the already-cached `MetadataCache` list. A request-counting `httpx.MockTransport` proves a warm-cache filter issues **zero** upstream requests, and that a cold-cache one fetches the whole list with no `q` parameter on the wire. It is absent from the render context, so the partial cannot echo it — asserted against both the raw and the escaped script string. `Query(max_length=100)` rejects an over-long value with 422 before the handler body runs, and the project's own 422 handler reads only each error's `loc` and `type`, so even the refusal echoes nothing.
- **Enter filters; it never scans (A-6).** The filter input's HTML **form owner** is `#tag-filter-form` — an empty form with no submit button, a sibling of the scan form inside the Scan article. A Chromium test presses Enter and asserts a `/api/tags` GET fires and **no** POST is issued at all. A second Chromium test intercepts the scan submit and reads the wire: it carries both ticked tags (including the filtered-out one) and no `q` field.
- **Nothing was added to the `<form hx-post="/api/scan">` element.** Asserted attribute-for-attribute against `_SCAN_FORM_ATTRS`, plus `hx-confirm` absent from the whole template. That is the decisive advantage of the form-owner attribute over `hx-params="not q"` on the form, which would have needed the `hx-disinherit` list extended and the C-10 trap re-opened (Pitfall 8).
- **Five help slots, in order, and no more.** `profile-description`, `title-help`, `tag-filter-help`, `tags-help`, `correspondent-help` — asserted as a whole ordered list rather than by membership, so **a second line under Profile fails the test**. 30-15's handoff is honoured exactly: the live description *is* Profile's help line and none was added. Each slot is its control's adjacent sibling, which is both Pico's `+small` styling hook and the announce-with-the-control contract.
- **44 px rows, measured in Chromium.** `label.tag-option` is flex, full width, `min-height: 2.75rem`. The selector is `label.tag-option` and not the bare class because Pico's `label:has([type=checkbox])` carries the same weight — the tie is what makes this win, and only because app.css loads second. A source assertion would pass with that order reversed, so a browser test measures every row's bounding box instead. No new colour literal: the count is still 3.
- **`show_tags` / `show_correspondent` remove markup, not pixels (D-28).** With the Tags block off there is no `#tags-list`, no `#tag-filter`, no `#tag-filter-form`, no `tags-help` and no tag refresh button; a test asserts the page contains no `display: none` achieving it. What is not in the markup cannot be re-shown in devtools, read by a screen reader or tabbed into.
- **Hiding a control changes the form, never the scan (D-29)** — see deviation 2; this needed code that did not exist.

## Task Commits

1. **Task 1: the filtered, selection-preserving tags route and partial**
   - `8ab5b3a` (test — RED gate; 13 tests, 8 failing)
   - `92f224e` (feat — GREEN gate)
2. **Task 2: the form markup — help text, the tags fieldset, the separate filter form**
   - `7f6875c` (test — RED gate; 10 markup tests, 7 failing)
   - `9b1282f` (feat — GREEN gate)
3. **Task 3: touch targets and the simpler form**
   - `a2bfea7` (test — RED gate; 11 + 1 browser tests, 8 failing)
   - `c43c8fa` (feat — GREEN gate)

No `refactor` commit was needed.

## Files Created/Modified

- `src/saneless/web/routes.py` — `TAG_FILTER_MAX_LENGTH`, `_TAGS_QUERY_DEFAULT`, `_tag_list_context`; `get_tags` and `invalidate_cache` gained `q`/`tags`; `index` passes the tag context and the two form-shape flags; `start_scan` gained the D-29 fallback.
- `src/saneless/web/templates/partials/tags.html` — rewritten: the wrapper, the pinned loop, the filtered loop, the two empty states.
- `src/saneless/web/templates/index.html` — the S6 Tags fieldset, the filter input, four help lines, the `#tag-filter-form` sibling, and the two `{% if %}` guards. The scan form element is byte-identical.
- `src/saneless/web/static/app.css` — `label.tag-option` and `.tag-list`, with the UI-SPEC comment and both deliberate limits recorded.
- `tests/test_web.py` — `TestTagFilter` (14) and `TestSimpleForm` (11).
- `tests/test_web_state_rendering.py` — `TestFormHelpTextAndTagPicker` (10).
- `tests/test_browser.py` — `TestTagFilterInChromium` (4) and the `tagged_server` fixture; `_RECORD_LOAD_REQUESTS` rewritten (deviation 3).

## The UI-SPEC-mandated test rewrite (named, as the brief asked)

The brief assigned this plan the fifth of the phase's mandated test rewrites: *anything asserting `<select name="tags" multiple>`*. What existed and what was done with it:

| Location | What it asserted | What it is now |
|---|---|---|
| `tests/test_browser.py:672` `_RECORD_LOAD_REQUESTS` | counted `htmx:afterRequest` on elements with id `tags-select` / `correspondent-select` | counts `htmx:afterSettle` on `tags-list` / `correspondent-select` — see deviation 3 |
| `tests/test_web.py::test_page_loads` | `'name="tags"' in response.text` | unchanged, and still true: the checkboxes carry `name="tags"` |

No test asserted the literal string `name="tags" multiple`. The rewrite was therefore **one** test helper, not a suite — and a new test now *forbids* the multi-select in any template, so the deletion is pinned rather than merely done. The two C-10 browser tests that depend on the helper both pass.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 — Bug] The swapped-in wrapper re-fired its own load trigger, for ever**

- **Found during:** Task 2 (GREEN gate, first Chromium run)
- **Issue:** The partial renders `#tags-list` itself (it must — the swap is `outerHTML`), and UI-SPEC S6 puts `hx-get="/api/tags" hx-trigger="load"` on that wrapper. htmx processes swapped-in content and fires `load` on it, so every response produced another request, for ever. In Chromium this looked like checkboxes detaching from under the cursor: `page.check()` retried for 30 s and timed out. No server-side test could see it.
- **Fix:** The request attributes now render only when `_tag_list_context` is called with `on_page_load=True`, which only `index` does. Nothing is lost — the filter, the refresh button and the filter form each carry their own request. Commented in both the route and the partial.
- **Files modified:** `src/saneless/web/routes.py`, `src/saneless/web/templates/partials/tags.html`
- **Verification:** `uv run pytest -m browser -k TagFilterInChromium` → 3 passed (was 2 failed).
- **Committed in:** `9b1282f`.

**2. [Rule 2 — Missing critical functionality] D-29's "the defaults still apply" described code that did not exist**

- **Found during:** Task 3 (RED gate)
- **Issue:** The plan, `config.py:557` and UI-SPEC S6 all state that hiding a control leaves the profile's `default_tags` / `default_correspondent` applying. `grep` found those keys read in exactly one place in the whole tree: `cli.py:654`. The web layer had **never** applied them, so with `show_tags = false` a scan would have been filed with no tags at all — the precise silent behaviour change D-29 exists to forbid.
- **Fix:** `start_scan` now falls back to the profile's defaults when the submit carries none, on the line after `resolve_job_title` does the same for the title. It is a fallback and not an override; a dedicated test asserts a submitted tag still wins.
- **Files modified:** `src/saneless/web/routes.py`
- **Verification:** `test_simple_form_without_tags_still_applies_the_profile_default_tags`, `…_correspondent_still_applies_its_default` and `…_lets_a_visible_control_override_the_default`.
- **Committed in:** `c43c8fa`.

**3. [Rule 3 — Blocking] The C-10 browser recorder counted an event that no longer reaches the document**

- **Found during:** Task 1 (GREEN gate)
- **Issue:** `_RECORD_LOAD_REQUESTS` listened for `htmx:afterRequest` on `document` and matched `elt.id === "tags-select"`. That id is gone, and the deeper problem is that htmx 2.0.8 fires `afterRequest` on the requesting element *after* the swap — for an `outerHTML` swap the element is already detached, so the event never bubbles to a document-level listener. Two C-10 regression tests would have hung on `wait_for_function`.
- **Fix:** The recorder counts `htmx:afterSettle` on `tags-list` / `correspondent-select`. Settle runs on elements that are in the document, on htmx's 20 ms default settle delay — later than the `disabled`-stripping the tests are watching for, so it remains a sound "the trap has had its chance" signal. Both the reason and the ordering are in the comment.
- **Files modified:** `tests/test_browser.py`
- **Verification:** `uv run pytest -m browser -q` → 80 passed at that commit.
- **Committed in:** `92f224e`.

**4. [Rule 1 — Bug] Task 1's GREEN would have swapped a `<div>` into a `<select>`**

- **Found during:** Task 1 (GREEN gate, planning the edit)
- **Issue:** The plan gives `index.html` to Task 2 but has Task 1 change what `/api/tags` answers with. Followed literally, the commit at the end of Task 1 leaves a `<select … hx-swap="innerHTML">` receiving a `<div>` full of labels — markup a browser discards, so the tag picker would be broken for the length of one commit. Same class as 30-15's deviation 3.
- **Fix:** Task 1's GREEN converted the Tags block to the fieldset around `{% include "partials/tags.html" %}`. Task 2's RED still failed 7 of its 10 assertions, so the gate stayed honest; the three that passed are the scan-form and no-multi-select regression guards, which were always meant to pass.
- **Files modified:** `src/saneless/web/templates/index.html`
- **Committed in:** `92f224e`.

### Design deviations from UI-SPEC S6 (deliberate, with reasons)

**`partials/tags.html` renders the `#tags-list` wrapper; `index.html` does not.** S6's markup block shows the wrapper in `index.html` *and* shows the partial rendering only the two loops. Those cannot both hold: the swap is `outerHTML`, so a response of bare rows would replace the wrapper with them and lose the id, the class and the styling hook on the first refresh. The plan's own Task 1 action text ("Rewrite `partials/tags.html` to render the whole swap target: the `<div id="tags-list" …>` wrapper, the `pinned` loop, the `tags` loop, and the two empty-state sentences") is the version that is internally consistent, and it is what was built. Task 2's acceptance greps do not ask for the wrapper in `index.html`, and its behaviour clause — "`#tags-list` carries `hx-get` / `hx-trigger="load"` / `hx-target="this"` / `hx-swap="outerHTML"`" — is about the rendered page and is asserted there.

**The template variable is `selected_tags`, not S6's `selected`.** `index`'s context already binds `selected` to the profile the page opens on (30-15), and a Jinja `{% include %}` shares its parent's context, so S6's `{% if tag.id in selected %}` would have tested a tag id against a profile name on every full-page render. Renaming is the only correct resolution.

### Acceptance-criterion deviations (no code impact)

**`grep -c "show_tags" src/saneless/web/templates/index.html` is 2, not the 1 the criterion predicts, and cannot be 1.** The criterion contradicts the plan's own behaviour clause in the same task: "`show_tags=false` removes the whole Tags fieldset, both help lines **and the filter form** from the rendered page." The fieldset lives inside `<form hx-post="/api/scan">` and the filter form is a **sibling after** it — that separation is the whole of A-6 and HTML forbids nesting them — so one `{% if %}` cannot span both. The only way to make the count 1 would be to bind a second context name to the same boolean, which is worse code written for a grep. The contract is asserted behaviourally instead: `test_simple_form_without_tags_renders_no_part_of_the_tag_block` requires `id="tag-filter-form"` to be absent from the page. `show_correspondent` is 1 as specified.

**`grep -c "min-height: 2.75rem" src/saneless/web/static/app.css` is 3, not the "at least 2" the criterion names.** The criterion predicts the tag option plus the `details.tech-details > summary` rule from 30-12; `.check-refresh` (Phase 30's `Check again` button) also carries it and predates this plan. The criterion is satisfied — it asks for at least 2 — and this plan added exactly one.

**`uv run pytest tests/test_web.py -k tag_filter` selects 14, above the 10 required; `-k simple_form` selects 11, above the 8 required; `tests/test_web_state_rendering.py -k "help or tag"` selects 10, above the 9 required.**

---

**Total deviations:** 4 auto-fixed (2 bugs, 1 missing functionality, 1 blocking) + 2 design deviations + 3 acceptance-criterion notes.
**Impact on plan:** No scope creep. Deviation 2 is the only one that adds behaviour to `src/`, and it adds the behaviour three separate documents already claimed existed.

## Issues Encountered

- **The worktree started on the wrong base**, at `a87b3dd` rather than `add0df6`. The tree was clean, so the startup guard's `git reset --hard` corrected it before any work began. **Tenth consecutive wave**; the guard remains load-bearing.
- **The infinite-swap loop (deviation 1) is the strongest argument yet for CLAUDE.md's browser rule.** Every server-side test passed with the bug in place — the loop only exists once htmx parses the response and re-arms the trigger. It was found by a `page.check()` that timed out, not by anything a `TestClient` could see.

## Known Stubs

None. Every new path returns real values: the route filters real cached data, the partial renders both loops and both empty states, and both `[web]` flags are read from `Settings`.

## Threat Flags

None. The plan adds no route and no schema change; it extends one existing GET and one existing POST with two bounded parameters each.

| Threat | Mitigation as built |
|---|---|
| T-30-74 (XSS via `q`) | `q` is absent from the render context; a `<script>` filter produces a response containing neither the raw nor the escaped string, nor `alert(1)`. The 422 handler reads only `loc` and `type`, so the over-long path echoes nothing either |
| T-30-75 (SSRF / injection via `q`) | A Python-side `in` over the cached list. A counting `MockTransport` asserts a warm-cache filter issues zero upstream requests and that no request carries a `q` parameter |
| T-30-76 (unbounded `q`) | `Query(max_length=TAG_FILTER_MAX_LENGTH)` — 422 at the boundary. Pinned from both sides: a value at the cap must be accepted |
| T-30-77 (a filter swap dropping a tick) | `hx-include` carries the selection, the route re-renders it checked and pins filter-excluded ticks above the list. Proven in Chromium: two ticks, one filtered out, both on the wire at submit |
| T-30-78 (Enter starting a scan) | The input's form owner is `#tag-filter-form`. A Chromium test presses Enter and records **no** POST; a server-side test proves a stray `q` on a scan is ignored rather than refused |
| T-30-79 (hiding a control with CSS) | The controls are absent from the markup; a test asserts no `display: none` and no `hidden` attribute achieves it |
| T-30-80 (a hidden control changing the scan) | `start_scan` falls back to the profile's defaults — the behaviour deviation 2 had to add. Three tests: both defaults apply, and a submitted value still wins |
| V7 (upstream tag names) | Jinja autoescape; nothing marked `\|safe`; the partial's comment says so |
| T-30-SC (package installs) | Nothing was installed |

## Verification Results

All plan gates pass:

- `uv run pytest tests/test_web.py tests/test_web_state_rendering.py -q` → **310 passed**
- `uv run pytest tests/test_web.py tests/test_web_state_rendering.py tests/test_web_checks.py tests/test_web_errors.py -q` → **502 passed**
- `uv run pytest -m "not browser and not sane_hardware" -q` → **2750 passed, 90 deselected**
- `uv run pytest -m browser -q` → **84 passed** (Chromium, offline egress gate; 80 before)
- `uv run ruff check .` → All checks passed
- `uv run ruff format --check .` → 63 files already formatted
- `uv run ty check` → All checks passed
- `uv run pyrefly check src tests` → 0 errors

Acceptance greps:

| Criterion | Required | Actual |
|---|---|---|
| `type="checkbox"` in `partials/tags.html` | 2 | 2 |
| `<option` in `partials/tags.html` | 0 | 0 |
| `id="tags-list"` in `partials/tags.html` | 1 | 1 |
| `{{ q }}` in `partials/tags.html` | 0 | 0 |
| `No tags match that filter.` in `partials/tags.html` | 1 | 1 |
| `form="tag-filter-form"` in `index.html` | 1 | 1 |
| `id="tag-filter-form"` in `index.html`, after the scan form's close | 1 | 1 (position asserted by test) |
| `name="tags" multiple` under `src/` | 0 | 0 |
| `<small` in `index.html` | ≥ 4 | 7 |
| `aria-describedby` in `index.html` | ≥ 4 | 8 |
| `keyup changed delay:300ms` in `index.html` | 1 | 1 |
| `git diff index.html \| grep '^[-+].*hx-post="/api/scan"'` | 0 | 0 |
| `label.tag-option {` in `app.css` | 1 | 1 |
| `min-height: 2.75rem` in `app.css` | ≥ 2 | 3 (see note) |
| `max-height: 17.5rem` in `app.css` | 1 | 1 |
| `#[0-9a-fA-F]{6}` in `app.css` | unchanged | 3, unchanged |
| `show_tags` in `index.html` | 1 | 2 (see note) |
| `show_correspondent` in `index.html` | 1 | 1 |
| `display: none` in `index.html` | 0 | 0 |
| `pytest -k tag_filter` | ≥ 10 | 14, all passing |
| `pytest -k simple_form` | ≥ 8 | 11, all passing |
| `pytest -k "help or tag"` (state rendering) | ≥ 9 | 10, all passing |

TDD gate sequence in `git log`: `test(30-16)` → `feat(30-16)` → `test(30-16)` → `feat(30-16)` → `test(30-16)` → `feat(30-16)`.

## Next Phase Readiness

For 30-17 (the browser colour test rewrite) and anything else touching this form:

- **`app.css` gained no colour literal.** The count is still 3 and `test_simple_form_touch_targets_add_no_colour_to_the_stylesheet` pins it, alongside 30-15's identical assertion.
- **The page now renders two new muted-text surfaces** — four `<small>` help lines and the tag rows. If a contrast parametrisation enumerates muted text on the card, `--pico-muted-color` on the card surface is already measured at 5.36:1 light / 4.53:1 dark (UI-SPEC S6) and no new token was introduced.
- **`tagged_server` in `tests/test_browser.py`** is the fixture to reuse for any browser test that needs tags on the page; the shared session server has none by design.
- **`#tags-list` is not present on every response.** A test that waits for it must either load the page or issue the filter request; a status poll does not carry it.
- **Do not put request attributes on `#tags-list` unconditionally.** Deviation 1 explains what happens; the flag is `tags_load_on_render`.

No blockers.

## Self-Check: PASSED

- All seven modified files exist on disk; no file was created and none deleted (`git diff --diff-filter=D add0df6 HEAD` is empty).
- All six commits (`8ab5b3a`, `92f224e`, `7f6875c`, `9b1282f`, `a2bfea7`, `c43c8fa`) are present in `git log`.
- Working tree clean before this SUMMARY; `.planning/STATE.md` and `.planning/ROADMAP.md` are untouched by this executor.
