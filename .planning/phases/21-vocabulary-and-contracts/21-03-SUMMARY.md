---
phase: 21-vocabulary-and-contracts
plan: 03
subsystem: web-presentation
tags: [jinja2, templates, refactor, vocabulary, aria, htmx]
requires:
  - saneless.vocabulary.JobState
  - saneless.vocabulary.state_label
  - saneless.vocabulary.progress_label
  - saneless.job.Job.is_active
  - saneless.job.Job.is_busy
provides:
  - "Jinja filter: state_label"
  - "Jinja filter: progress_label"
  - "Jinja global: JobState"
affects:
  - src/saneless/web/app.py
  - src/saneless/web/templates/index.html
  - src/saneless/web/templates/partials/status.html
  - src/saneless/web/templates/partials/history.html
  - tests/test_web.py
tech-stack:
  added: []
  patterns:
    - "template state comparisons go through a Jinja global holding the enum class, never a string literal"
    - "template state classification goes through a property on the domain object, never a template-local list"
    - "byte-for-byte render equivalence proven by rendering the pre-change templates and the post-change templates side by side over every enum member"
key-files:
  created:
    - tests/test_web_state_rendering.py
  modified:
    - src/saneless/web/app.py
    - src/saneless/web/templates/index.html
    - src/saneless/web/templates/partials/status.html
    - src/saneless/web/templates/partials/history.html
    - tests/test_web.py
decisions:
  - "The JobState Jinja global is handed over through an explicitly `Any`-typed local because Jinja2 3.1.6 builds `Environment.globals` from the unannotated `DEFAULT_NAMESPACE`, so ty infers its value type as the union of the six built-in helpers. Annotating the mapping instead does not work: ty narrows a local to the assigned value's type and ignores the wider declared annotation."
  - "Per-state render coverage went into a NEW file, tests/test_web_state_rendering.py, so that tests/test_web.py stays literally unedited by task 2 as the plan requires, while the D-04 distinction still gets a permanent regression pin."
  - "The scan button keeps two separate conditions rather than one: `disabled` follows is_active, `aria-busy` follows the narrower is_busy, so AWAITING_FLIP is disabled without announcing itself as busy."
requirements: [CTR-01]
metrics:
  duration: 41m
  completed: 2026-09-10
---

# Phase 21 Plan 03: Web Vocabulary Wiring Summary

The web layer no longer owns any job vocabulary: `web/app.py`'s private
`_STATE_LABELS` map and `humanize_state` are deleted, `state_label` and
`progress_label` are Jinja filters and `JobState` a Jinja global sourced from
`saneless.vocabulary`, and all three hand-written state lists plus the
re-spelled progress prose are gone from the templates -- with every rendered
line proven identical to before for all seven states.

## What Shipped

**`src/saneless/web/app.py`**

- `_STATE_LABELS` (the fourth copy of the label table, and the only one keyed by
  raw `str`) and `humanize_state` deleted outright -- not aliased, not wrapped.
- `__all__` narrowed to `["create_app"]`.
- One runtime first-party import added:
  `from saneless.vocabulary import JobState, progress_label, state_label`,
  placed in the absolute-import block between `saneless.paperless` and
  `saneless.worker`. Imported from `vocabulary`, not from `job` -- the
  presentation layer does not ask the persistence module what a state is called.
- Registration immediately after the `Jinja2Templates` construction, before any
  template is loaded:

  ```python
  app.state.templates.env.filters["state_label"] = state_label
  app.state.templates.env.filters["progress_label"] = progress_label
  job_state_global: Any = JobState
  app.state.templates.env.globals["JobState"] = job_state_global
  ```

  `env.globals` is a new mechanism in this codebase; there was no `env.globals`
  or `env.tests` anywhere before.

**`partials/history.html`** -- `{{ job.state.value | humanize_state }}` became
`{{ job.state | state_label }}`, passing the enum rather than a `str` that only
happened to work because `StrEnum` members compare equal to their own values.
The CSS-class conditional compares against `JobState.DONE` / `JobState.ERROR`.

**`partials/status.html`** -- the `{% set active_states = [...] %}` line is gone;
the poll gate reads `job.is_active`; the four `aria-busy` prose arms collapse to
one `{% if job.is_busy %}<p aria-busy="true">{{ job.state | progress_label }}</p>`
branch. AWAITING_FLIP, DONE and ERROR keep their own arms and their exact
markup, including `role="alert"`, `&#10003;`, `&#10007;`, `{{ job.error }}`
verbatim, and both `hx-get="/api/jobs/history"` refresh divs. Branch order puts
`is_busy` first, since the other three states are all outside `BUSY_STATES`.

**`index.html`** -- the Scan button's two literal lists became
`{% if job and job.is_active %}disabled{% if job.is_busy %} aria-busy="true"{% endif %}{% endif %}`
and the caption chain `JobState.AWAITING_FLIP` -> `Waiting for flip&#8230;`,
`job.is_busy` -> `Scanning&#8230;`, else `Scan`. `type="submit"`, `id="scan-btn"`
and both `&#8230;` entities are untouched.

**`tests/test_web.py`** -- `humanize_state` dropped from the import;
`test_humanize_state_filter_unit` deleted per D-10. Not reimplemented anywhere:
its four label assertions live in `tests/test_vocabulary.py` from plan 21-01,
and its fifth (`humanize_state("UNKNOWN") == "UNKNOWN"`) is the assertion that
pinned the drift, replaced there by a raising assertion.

**`tests/test_web_state_rendering.py`** (new) -- 43 tests, all six parametrised
cases driven by `list(JobState)` rather than a hand-written name list.

## User-Visible Strings: Confirmed Unchanged

Every string below is byte identical to the pre-plan render. This was not
eyeballed -- it was measured (see "Render Equivalence Evidence").

| Surface | Strings preserved |
|---|---|
| History table | `Pending`, `Scanning`, `Waiting for flip`, `Assembling`, `Uploading`, `Complete`, `Failed` |
| Status prose | `Starting scan...`, `Scanning...`, `Assembling PDF...`, `Uploading to paperless-ngx...` -- all with the literal three-ASCII-period ellipsis, not U+2026 |
| Status DONE | `&#10003; Done: {{ job.title }}` (HTML entity, unescaped-by-source, not `| safe`) |
| Status ERROR | `&#10007; Error: {{ job.error }}` -- `job.error` still verbatim |
| Status idle | `Ready to scan.` |
| Scan button | `Waiting for flip&#8230;`, `Scanning&#8230;`, `Scan` |

`&#8230;` appears twice in `index.html` and `&#10003;` / `&#10007;` once each in
`status.html`, exactly as before.

## Render Equivalence Evidence

A harness rendered the templates as they exist at the plan's base commit
(`08adfad`, with the old `humanize_state` filter) and as they exist now (with
the new filters and the `JobState` global) into the same Jinja `Environment`
settings, then compared the output. Coverage: `history.html`, `status.html` and
`index.html`, each over all seven `JobState` members plus the `job = None` case,
plus a thumbnail case and an empty-history case -- 26 renders.

Result: **26 renders compared, 0 differing.**

- `history.html` (8 renders): exactly byte identical.
- `status.html` and `index.html` (18 renders): every **non-blank line
  identical**, differing only by exactly one blank line.

That one blank line is the trailing newline of the deleted
`{% set active_states = [...] %}` tag. Jinja runs with `trim_blocks=False`, so
the tag emitted nothing but kept its newline; removing the line -- which the
plan explicitly instructs -- removes that newline. It is leading whitespace in
an HTML fragment, changes no user-visible string, and htmx swaps `outerHTML`
regardless. Flagged here rather than buried because the plan's wording was
"byte identical".

## Test Coverage Added, and Proof It Discriminates

The plan's grep gates prove the *literals are gone*. They do not prove *which
state produces which markup*, and the 8 browser tests only ever load the idle
page -- they never drive a job into PENDING, AWAITING_FLIP or ERROR. Once the
lists moved behind `is_active` / `is_busy`, nothing mechanical pinned the
mapping on the one surface neither type checker can see. That gap is what
`tests/test_web_state_rendering.py` closes:

| Test | Pins |
|---|---|
| `test_history_cell_shows_the_shared_label` | the `<td>` renders `state_label(state)` |
| `test_history_cell_css_class` | `status-done` / `status-error` only for those two states |
| `test_status_area_polls_only_while_active` | `hx-trigger="every 1s"` present iff `state in ACTIVE_STATES` |
| `test_status_area_prose` | the busy line iff `state in BUSY_STATES`; flip prompt only for AWAITING_FLIP; no `aria-busy` outside BUSY_STATES; exact DONE/ERROR markup; history-refresh hook only on terminal states |
| `test_scan_button_disabled_and_busy_split` | `disabled` iff active, `aria-busy` iff busy -- the D-04 distinction |
| `test_scan_button_text` | the three exact captions including `&#8230;` |
| `test_idle_page_button_and_status` | the no-job page |

New tests passing on first run is weak evidence, so each assertion was checked
by mutating the implementation and confirming the expected red:

| Mutation | Observed failure |
|---|---|
| `index.html` aria-busy gate `is_busy` -> `is_active` | `test_scan_button_disabled_and_busy_split[AWAITING_FLIP]` |
| `status.html` prose gate `is_busy` -> `is_active` | `test_status_area_prose[AWAITING_FLIP]`, plus pre-existing `test_flip_prompt` and `test_flip_abort_label` |
| `status.html` poll gate `is_active` -> `is_busy` | `test_status_area_polls_only_while_active[AWAITING_FLIP]` |
| `history.html` filter `state_label` -> `progress_label` | `test_history_cell_shows_the_shared_label` for 5 states |

All four mutations were reverted; the harness re-confirmed 0 differing renders
afterwards.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] `ty` rejects `env.globals["JobState"] = JobState`**

- **Found during:** Task 1, first verification run.
- **Issue:** Jinja2 3.1.6 sets `self.globals = DEFAULT_NAMESPACE.copy()` and
  `DEFAULT_NAMESPACE` carries no annotation (unlike its neighbour
  `DEFAULT_POLICIES`, which is `t.Dict[str, t.Any]`). `ty` therefore infers
  `Environment.globals` as
  `dict[str, type[range] | type[dict] | ... ]` and rejects any value that is not
  one of Jinja's six built-in helpers:
  `error[invalid-assignment] ... Expected value of type '<class 'range'> | ...', got '<class 'JobState'>'`.
  `pyrefly` accepted the same line. The plan's instruction could not be followed
  literally without a suppression, which CLAUDE.md forbids.
- **Fix:** hand the enum over through an explicitly `Any`-typed local:
  `job_state_global: Any = JobState`, then the plain
  `app.state.templates.env.globals["JobState"] = job_state_global`. A comment
  records why. Nothing is suppressed and the key type is still checked.
- **Alternative rejected, with evidence:** widening the mapping instead --
  `a_globals: MutableMapping[str, Any] = t.env.globals` -- was tried first in a
  scratch probe and **`ty` still rejected the subscript assignment**, reporting
  the inferred `dict[...]` type rather than the declared annotation, because it
  narrows a local to the assigned value's type. `pyrefly` accepted both variants.
  The probe file was deleted.
- **Files modified:** `src/saneless/web/app.py`
- **Commit:** `7fb918e`

**2. [Rule 2 - Missing coverage] No permanent test pinned the D-04 split**

- **Found during:** Task 2, while verifying the acceptance criteria.
- **Issue:** `command grep -n 'aria-busy\|AWAITING_FLIP\|scan-btn' tests/` showed
  the existing suite asserts only that `id="scan-btn"` and the word `disabled`
  appear somewhere on the page during a SCANNING job. Nothing asserted that
  AWAITING_FLIP is disabled *without* `aria-busy`, nothing asserted the button
  captions, and nothing asserted the per-state status prose. The browser suite
  never leaves the idle page. So the single behavioural distinction this plan
  encodes was, after the refactor, invisible to every automated check.
- **Fix:** added `tests/test_web_state_rendering.py` (43 tests) and verified by
  mutation that each assertion discriminates, as tabulated above.
- **Why a new file rather than editing `tests/test_web.py`:** the plan's task-2
  acceptance criteria require `tests/test_web.py` to pass *unedited by that
  task*. A new module satisfies that literally while still closing the gap.
- **Files modified:** none; one file created.
- **Commit:** `6c0184f`

**3. [Rule 3 - Blocking] pyrefly project mode cannot see a worktree under a dot-directory**

- **Found during:** Task 1, first commit. Same issue plan 21-01 hit and recorded.
- **Issue:** the worktree lives under `.claude/worktrees/`, which the repo
  `.gitignore` excludes, so pyrefly's project-mode include resolution skipped
  every file (`No Python files matched patterns`) and exited 1, failing the
  `pyrefly-checker` prek hook for reasons unrelated to the code.
- **Fix:** an **untracked, worktree-local** `pyrefly.toml` naming the include
  roots explicitly. The shared `pyproject.toml` was not touched. Even so pyrefly
  still skips the `tests` tree in this worktree, so every commit was
  additionally gated on explicit paths
  (`uv run pyrefly check src/saneless/web/app.py tests/test_web.py tests/test_web_state_rendering.py`
  -> `0 errors`). **The file was deleted before returning**, so the worktree has
  no untracked leftovers.
- **Files modified:** none tracked.

### Notes, not deviations

- **Test count moves 445 -> 487, not "never below 445".** The executor prompt
  set a floor of 445, but the plan itself (D-10) mandates deleting
  `test_humanize_state_filter_unit`, taking the suite to 444; the 43 new tests
  then take it to 487. The plan's own acceptance criterion is "0 failed", which
  holds at every commit. The deleted assertions were not lost -- four of them
  are in `tests/test_vocabulary.py` and the fifth was deliberately replaced
  there by a raising assertion.
- **One acceptance criterion counts occurrences, not lines.** The plan expects
  `command grep -c '&#8230;' src/saneless/web/templates/index.html` to return 2,
  but `grep -c` counts matching *lines* and both entities sit on the same line
  (the button caption chain). It returns 1 now and returned 1 at the base commit
  `08adfad` too; `grep -o ... | wc -l` returns 2 in both. No regression -- the
  criterion was miscounted. Verified both ways against the base commit.
- **Playwright MCP was not available in this executor's toolset**, so real-browser
  validation is the repo's own playwright-driven suite,
  `uv run pytest -m browser` -> **8 passed**. This is not a "manual-only" carve
  out: the per-state UI behaviour that a browser check would look for is covered
  automatically by the 43 new render tests, and since every rendered line is
  proven identical to the pre-plan output, no browser-visible change is possible.
- **`worker._categorize_error` still exists** (noted by 21-01) and `error_message()`
  is still wired nowhere -- both out of this plan's scope and D-12 respectively.

## Threat Model Dispositions Honoured

| Threat ID | Disposition | How |
|---|---|---|
| T-21-02 | mitigate | No `| safe` anywhere under `src/saneless/web/templates/` (grep returns nothing). Every label is a developer-authored constant from `vocabulary.py`; autoescape stays in force. |
| T-21-05 | mitigate | The store-boundary guard is untouched: `Job.state` is still only built via `JobState(row[3])` at `job.py:187`/`:262`, so a raising filter cannot be reached from a stored row. The new `test_status_area_polls_only_while_active` additionally proves the 1s poll is attached for exactly the active states. |
| T-21-06 | accept | Unchanged by design. `{{ job.error }}` still renders verbatim; `error_message` appears nowhere under `src/saneless/web/` (grep returns 0 in every file). |
| T-21-SC | mitigate | Zero packages installed. `pyproject.toml` and `uv.lock` are byte identical to the base commit. |

## Verification Results

At the final commit:

```
uv run ruff check .                            -> All checks passed!
uv run ruff format --check .                   -> 40 files already formatted
uv run ty check                                -> All checks passed!
uv run pyrefly check                           -> 0 errors  (src; see deviation 3)
uv run pyrefly check src/saneless/web/app.py \
     tests/test_web.py \
     tests/test_web_state_rendering.py         -> 0 errors
uv run pytest -m "not browser"                 -> 487 passed, 8 deselected
uv run pytest -m browser                       -> 8 passed
render-equivalence harness                     -> 26 renders compared, 0 differing
```

Structural gates, all passing:

```
command grep -rn 'humanize_state' src/ tests/                                  -> no match
command grep -rn '_STATE_LABELS' src/                                          -> no match
command grep -c '__all__ = ["create_app"]' src/saneless/web/app.py             -> 1
command grep -c 'env.filters["state_label"]' src/saneless/web/app.py           -> 1
command grep -c 'env.filters["progress_label"]' src/saneless/web/app.py        -> 1
command grep -c 'env.globals["JobState"]' src/saneless/web/app.py              -> 1
command grep -c 'job.state | state_label' .../partials/history.html            -> 1
command grep -rnE '("|'"'"')(PENDING|SCANNING|AWAITING_FLIP|ASSEMBLING|UPLOADING|DONE|ERROR)("|'"'"')' src/saneless/web/
                                                                               -> no match
command grep -rn 'active_states' src/saneless/web/templates/                   -> no match
command grep -rn 'state.value'   src/saneless/web/templates/                   -> no match
command grep -rn '| safe'        src/saneless/web/templates/                   -> no match
command grep -c 'job.is_busy'   .../partials/status.html                       -> 1
command grep -c 'job.is_active' .../index.html                                 -> 1
command grep -c 'job.is_busy'   .../index.html                                 -> 2
command grep -c '&#10003;'      .../partials/status.html                       -> 1
command grep -c '&#10007;'      .../partials/status.html                       -> 1
command grep -c '{{ job.error }}' .../partials/status.html                     -> 1
command grep -o '&#8230;' .../index.html | wc -l                               -> 2  (see note above)
command grep -n 'noqa|type: ignore' src/saneless/web/app.py \
     tests/test_web_state_rendering.py                                         -> no match
```

The one `# noqa` remaining in `tests/test_web.py` (line 406, `ANN202`) is
pre-existing and untouched -- confirmed against `git show 08adfad:tests/test_web.py`.

## Known Stubs

None.

## Commits

| Task | Commit | Description |
|---|---|---|
| 1 | `7fb918e` | delete `_STATE_LABELS` / `humanize_state`, register the filters and the JobState global, re-back `history.html` |
| 2 | `6c0184f` | drive `status.html` and the Scan button from `is_active` / `is_busy`, add the 43 per-state render tests |

## What the Next Plan Can Rely On

- Every template state comparison goes through the `JobState` Jinja global and
  every label through `state_label` / `progress_label`. Adding an eighth
  `JobState` member is now a two-file change (`vocabulary.py` plus whichever
  classification set it joins) and the templates need no edit at all -- but
  `tests/test_web_state_rendering.py` will fail loudly until the new member's
  rendering is decided, because every case is parametrised over `list(JobState)`.
- `Job.is_active` / `Job.is_busy` are the only classification seam the
  presentation layer uses; the frozensets were deliberately NOT pushed into the
  template namespace.
- `error_message()` remains unwired, ready for the Phase 30 plain-language work.

## Self-Check: PASSED

Claimed files exist on disk:

- `src/saneless/web/app.py` FOUND
- `src/saneless/web/templates/index.html` FOUND
- `src/saneless/web/templates/partials/status.html` FOUND
- `src/saneless/web/templates/partials/history.html` FOUND
- `tests/test_web.py` FOUND
- `tests/test_web_state_rendering.py` FOUND

Claimed commits exist in `git log`: `7fb918e` FOUND, `6c0184f` FOUND, on
`worktree-agent-ab0ea8b27f02d36b8` atop base `08adfad`.

`STATE.md` and `ROADMAP.md` were not modified; the orchestrator owns those.
