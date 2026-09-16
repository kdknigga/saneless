---
phase: 30-appliance-layer
verified: 2026-09-16T22:14:16Z
status: passed
score: 5/5 must-haves verified
overrides_applied: 0
---

# Phase 30: Appliance Layer Verification Report

**Phase Goal:** A non-technical household member can tell at a glance whether the appliance is
healthy and what a failure means — one shared check list behind both `saneless doctor` and a
cached status strip, page counts on every terminal job, plain-language errors with a next step,
human profile labels, queue position, and an owner-only flip prompt — with help text and the docs
for each new surface written in-phase.

**Verified:** 2026-09-16T22:14:16Z
**Status:** passed
**Re-verification:** No — initial verification

## Method

This report is based on direct inspection of the tree at HEAD `449b030` (branch `autodev`):
reading the actual bodies of `vocabulary.py`, `config.py`, `job.py`, `checks.py`, `cli.py`,
`web/app.py`, `web/routes.py`, `web/checks_cache.py`, `web/refresher.py`, every touched Jinja
partial, `docker-compose.yml`, and the corrected docs; grepping for wiring (imports, filter
registration, route decorators, template includes); independently re-running `uv run pytest -q`
(2888 passed, matches the orchestrator's count) and `uv run ruff check .` (clean); and checking
that every `# noqa` present in phase-touched files predates this phase (`git log -S`). SUMMARY.md
claims were treated as hypotheses to falsify, not evidence.

## Goal Achievement

### Observable Truths (Success Criteria, ROADMAP-derived)

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | `saneless doctor` runs the shared checks and exits non-zero on a placeholder token or any red check; the index page shows the same checks, refreshed on load and by a button | ✓ VERIFIED | `src/saneless/checks.py` `run_checks()`/`CheckKey`/`worst_state` is the one registry (`checks.py:938`). `cli.py:1132` `doctor` calls `run_checks(...)` and `ctx.exit(ExitCode.CONFIG)` when `worst_state(results) is CheckState.FAIL` (`cli.py:1186-1187`). A placeholder token is caught pre-network by `is_placeholder_token` (`config.py:266`), consumed inside the Paperless check. `web/routes.py:1180` `GET /api/checks` and `:1200` `POST /api/checks/refresh` read/refresh the same `run_checks` output via `app.state.checks`/`app.state.refresher`, wired in `web/app.py`. `tests/test_doctor.py` (21 tests), `tests/test_checks.py` (75 tests), `tests/test_web_checks.py` (57 tests) all pass. |
| 2 | The status strip stays fast with the scanner host unplugged and is skipped entirely while a scan is active | ✓ VERIFIED | `checks.py` `_saned_reachable()` uses a bounded `socket.create_connection(timeout=...)` pre-probe before ever calling into libsane, which has no timeout (documented rationale at `checks.py:462-507`). `run_checks(..., skip_scanner=True)` returns `_scanner_skipped()` without entering the backend (`checks.py:582-601`). `CheckCache` (`web/checks_cache.py`) takes an injectable clock and never blocks a request — `GET /` reads from cache only; a background `CheckRefresher` (`web/refresher.py`) is the only prober, following the `ScanWorker` daemon-thread/stop-Event/bounded-join precedent. `plan 30-07`'s tests assert no `time.sleep` was added (0 found in `test_checks_cache.py`/`test_refresher.py`; module-wide `tests/` sleep count held at 17, confirmed by grep). |
| 3 | Every terminal job shows pages scanned/removed/uploaded; manual duplex shows front/back counts during pass B; a queued job shows the wait line | ✓ VERIFIED | `vocabulary.page_counts()` (`vocabulary.py:624-656`) returns `None` unless all three counts are non-`None`, renders a true `0` as `0`. Wired into `partials/status.html` (DONE/FALLBACK branches) and `partials/history.html` Title cell. `vocabulary.busy_line()` (`vocabulary.py:545-596`) implements the queue/front-count/plain-progress precedence exactly, including "next in line" instead of "(0 ahead of you)" — pinned at `tests/test_vocabulary.py:680-736`. `ScanWorker.front_pages`/`pass_count_callback` wiring confirmed in plan 30-04's artifacts. |
| 4 | Every user-facing error shows a plain-language message and next step, raw detail in a collapsed disclosure | ✓ VERIFIED | `vocabulary.error_advice()` (`vocabulary.py:731-821`) is the single `match`/`assert_never` over `ErrorCategory`, with `error_message`/`error_next_step` as one-line accessors (confirmed no second `match` exists). `partials/status.html` ERROR branch wraps the sentence + next step in one `role="alert"` div, with `job.error`/category/job id inside a collapsed `<details class="tech-details">` outside the alert (read in full — matches D-13 exactly). Grep confirms no template under `web/templates/` references a log path. CLI: `cli.py`'s `Try: ` line printed after Phase 28's unchanged failure line (confirmed pattern in cli.py's scan error path). |
| 5 | Two browser contexts show owner Continue/Abort (with Abort confirm) and non-owner "Waiting for the stack to be flipped"; profile dropdowns show human labels/descriptions, feeder-first, and read-only-mount note on the strip | ✓ VERIFIED | `web/routes.py:1097-1103` mints an `HttpOnly`, `SameSite=Lax`, no-`Max-Age` cookie (`OWNER_COOKIE = "saneless_owner"`) on first submit only (`if presented is None`). `partials/status.html` AWAITING_FLIP branch: owner sees `partials/flip.html` (Continue/Abort with `hx-confirm` on Abort — `flip.html:60`), non-owner sees the literal `Waiting for the stack to be flipped` line, server-side (no CSS hiding). `tests/test_browser.py:3606` `test_the_owner_is_offered_the_flip_and_the_second_browser_is_not` drives two real `browser.new_context()` cookie jars, asserts `httpOnly`, `sameSite == "Lax"`, `expires == -1` (session cookie), zero flip controls in the non-owner DOM, and D-26's absence guard (no third override control) on both pages. Profile select: `_profile_label`/`_profile_description` (plan 30-05) feed `partials/profile_description.html`; `D-21` feeder-first ordering confirmed via `classify_source` reference in `routes.py`. `ProfileStorage.IN_MEMORY_UNWRITABLE` WARN row confirmed in `checks.py`. |

**Score:** 5/5 truths verified.

### Specific Items Scrutinised (per orchestrator request)

| # | Item | Verdict | Reasoning |
|---|------|---------|-----------|
| 1 | APPL-11 consume mount ships COMMENTED in `docker-compose.yml` | **Accepted, not a gap** | Read the file in full. The mount is a two-line-explained commented block (`# - /srv/paperless/consume:/consume`), consistent with the file's existing pattern of commented optional integrations (the scanner-host lines are already commented the same way, and there is no local `paperless` service defined in this compose file for a live mount to connect to). REQUIREMENTS.md's APPL-11 text ("includes the consume-directory mount with a two-line explanation") does not require it to be live, and CONTEXT's D-17 precedent for the credential block establishes "commented with an explanation, the operator opts in" as this file's idiom. This is a defensible, documented scope decision, not a shortcut. |
| 2 | The bounded saned pre-probe / self-contradiction resolution in 30-06 | **Coherent, criterion 2 holds** | Read `_saned_hosts`, `_saned_reachable`, and `_check_scanner` in full. The logic is: no configured/parseable host → no probe → falls through to `get_devices()` (today's behaviour, cannot produce a false FAIL). A host is configured and every entry refuses TCP → `FAIL` without entering the backend (avoids the ~127s hang `get_devices()` would otherwise incur). This is a coherent relocation of the "no false FAIL" guarantee to "the probe could not be run," not a contradiction — both docstrings state the same invariant consistently and the two-minute-hang case is exactly the one criterion 2 requires to be avoided. |
| 3 | D-15 Scan button: a viewer following their own terminal job gets an enabled button while another job runs | **Does not violate a locked decision** | Read the full reasoning recorded in `30-14-SUMMARY.md` and the pinned test `TestScanButtonFollowsTheRenderedJob` at `tests/test_web_state_rendering.py:1890`. D-25 (status area follows the browser's own job) was locked in 30-CONTEXT; this is a necessary, explicitly-decided consequence rather than a silent regression — the alternative (button keyed on a different job than the one the status area reports) would make the page self-contradictory. Pinned by 4 test cases plus a browser test. |
| 4 | D-29 implemented as a fix (not per original description) | **Fix matches D-29's intent** | `web/routes.py:1001-1011` applies `found.default_tags`/`found.default_correspondent` as a fallback (`tags = tags or found.default_tags`; `if correspondent is None: correspondent = found.default_correspondent`) — a fallback, not an override, exactly mirroring the existing blank-title-falls-back-to-profile-title precedent D-29 cites. `cli.py:654-655` already did this; the web layer previously did not, confirmed by grep (only these two call sites exist). The fix is correctly scoped and tested. |
| 5 | `doctor --json` deliberately not shipped | **Legitimate scope decision** | Confirmed: `doctor` has no `--json` option (only `jobs --json` exists, at `cli.py:782`, a pre-existing machine contract). `cli.py:1125-1131` documents the reasoning (no consumer found in docs/tests/Dockerfile/compose; REQUIREMENTS' Out-of-Scope table already forbids a `HEALTHCHECK` calling `doctor`). `30-08-SUMMARY.md` records an explicit guard test asserting `doctor --json` is undocumented in both reference docs. Not a gap. |
| 6 | Acceptance greps replaced with behavioural assertions | **Spot-checked, genuinely proved** | Checked `TestTagFilterInChromium` (tap-target ≥44px via `bounding_box()`, tick-preservation across a filter swap, Enter-in-filter issuing GET not POST) and the two-context owner-cookie test (`cookie["httpOnly"]`, `cookie["sameSite"]`, `cookie["expires"] == -1` measured via Playwright's real cookie jar, not string-matched). These are genuine behavioural assertions against a running browser, not weakened placeholders. |

### Required Artifacts (representative sample — 19 plans, all files read or grepped)

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `src/saneless/vocabulary.py` | `ErrorAdvice`, `error_advice`, `local_time`, `page_counts`, `busy_line`, `ProfileStorage`, `RequestRejection.TOKEN_UNSET` | ✓ VERIFIED | All present, substantive, docstring-justified; single `match`/`assert_never` confirmed for `error_advice` |
| `src/saneless/config.py` | `is_placeholder_token`, `ProfileConfig.label/.description`, `WebConfig` | ✓ VERIFIED | All present; `Settings.web: WebConfig = Field(default_factory=WebConfig)` confirmed |
| `src/saneless/job.py` | `create_job(..., owner_token=...)`, `queue_position()` | ✓ VERIFIED | `owner_token` column has its first writer; `queue_position` at `job.py:1011` |
| `src/saneless/checks.py` | `CheckKey`, `run_checks`, `worst_state`, five checks | ✓ VERIFIED | Full registry read; imports nothing from `web/` or `cli.py` (confirmed by AST inspection) |
| `src/saneless/cli.py` | `doctor` command | ✓ VERIFIED | Reads shared registry, prints ordered rows with indented next steps, exits `ExitCode.CONFIG` on FAIL, does not call `require_sane()` |
| `src/saneless/web/checks_cache.py`, `refresher.py` | `CheckCache`, `CheckRefresher` | ✓ VERIFIED | Exist, injectable clock, daemon-thread/stop-Event/bounded-join pattern confirmed in `app.py` wiring |
| `src/saneless/web/app.py` | 8+ filters, `app.state.checks`/`.refresher`, dual-thread shutdown | ✓ VERIFIED | All filters registered; shutdown sets both stop events before either join (A-7), read in full |
| `src/saneless/web/routes.py` | `/api/checks`, `/api/checks/refresh`, owner cookie mint, `/api/profiles/description`, tag filter route | ✓ VERIFIED | All routes present and wired to vocabulary/checks/job modules |
| Templates (`checks.html`, `status.html`, `flip.html`, `tags.html`, `profile_description.html`, `scan_button.html`) | New surfaces render server-computed vocabulary only | ✓ VERIFIED | Read in full; templates own no vocabulary (all classes/glyphs/labels via Jinja filters); no log path anywhere |
| `docker-compose.yml` | Commented credential block, consume mount, TZ line | ✓ VERIFIED | Read in full; matches D-17 and the accepted APPL-11 scope decision above |
| 9 documentation files | Corrected per plan 30-18 | ✓ VERIFIED | All exist, all modified at phase-consistent timestamps (2026-09-16), spot-checked `cli-commands.md` for the `doctor` section and "six commands" wording |
| `tests/test_browser.py` | `_make_gate`, P1-P17 assertions | ✓ VERIFIED | Module-level gate factory confirmed reused by both hand-made contexts in the two-context owner test; all named UI-SPEC checkpoints (P1-P17) traced to concrete test methods, including P11-P13 which are covered by `TestTagFilterInChromium` without literal "(P11)" comment labels |

### Key Link Verification

| From | To | Via | Status |
|------|-----|-----|--------|
| `vocabulary.error_message`/`error_next_step` | `error_advice` | one-line accessors | ✓ WIRED — confirmed no second `match` |
| `cli.py doctor` | `checks.run_checks` | shared registry call | ✓ WIRED |
| `web/routes.py index`/`/api/checks` | `app.state.checks` (cache) | cache read, never a probe inside a request | ✓ WIRED — `GET /` never calls `run_checks` directly |
| `web/app.py lifespan` | `CheckRefresher.stop()` | bounded join before any resource close | ✓ WIRED — A-7 overlapping-join gate read in full |
| `web/routes.py start_scan` | `RequestRejection.TOKEN_UNSET` | `RequestRejected`, guard before `create_job` | ✓ WIRED |
| `web/templates/partials/scan_button.html` | `scan_blocked` context | OR'd disabled source, independent of job state | ✓ WIRED |
| `web/routes.py continue_flip`/`abort_flip` | owner-token comparison | `_owner_answers`/`_is_owner`, `secrets.compare_digest` used | ✓ WIRED |
| `web/templates/index.html` profile select | `/api/profiles/description` | `hx-get` on change, `hx-target="#profile-description"` | ✓ WIRED |
| `web/templates/partials/tags.html` filter | `#tags-list` | `hx-include`, pinned-ticked-tags loop | ✓ WIRED |
| `docker-compose.yml` | `./config/config.toml` | read-write mount, commented credential override | ✓ WIRED |

### Requirements Coverage

All 12 requirement IDs (APPL-01 through APPL-12) are claimed by at least one of the 19 plans'
`requirements:` frontmatter (cross-referenced against REQUIREMENTS.md lines 129-140). No orphaned
requirements found — every APPL-* ID in REQUIREMENTS.md's phase-30 mapping table (lines 316-327)
appears in at least one plan.

| Requirement | Status | Evidence |
|---|---|---|
| APPL-01 | ✓ SATISFIED | `doctor` + shared registry (plans 30-06, 30-08, 30-11) |
| APPL-02 | ✓ SATISFIED | Status strip, cache, refresher, skip-while-scanning (30-04, 30-06, 30-07, 30-09, 30-11, 30-17) |
| APPL-03 | ✓ SATISFIED | `page_counts`, `busy_line`, front-count channel (30-01, 30-04, 30-09, 30-12, 30-13, 30-17) |
| APPL-04 | ✓ SATISFIED | `error_advice`, ERROR branch rewrite, CLI `Try:` line (30-01, 30-09, 30-10, 30-12, 30-17) |
| APPL-05 | ✓ SATISFIED | Profile label/description generation and rendering (30-02, 30-05, 30-15, 30-17, 30-18) |
| APPL-06 | ✓ SATISFIED | `ProfileStorage`, amber WARN row (30-01, 30-04, 30-06, 30-11) |
| APPL-07 | ✓ SATISFIED | `is_placeholder_token`, doctor/scan/web refusal (30-01, 30-02, 30-06, 30-08, 30-10, 30-14, 30-18, 30-19) |
| APPL-08 | ✓ SATISFIED | `queue_position`, `busy_line` queue branch (30-01, 30-03, 30-13) |
| APPL-09 | ✓ SATISFIED | Owner cookie, gated flip prompt, Abort confirm (30-03, 30-13, 30-19) |
| APPL-10 | ✓ SATISFIED | Help text, checkbox tag list, `[web]` show_tags/show_correspondent (30-02, 30-15, 30-16, 30-18, 30-19) |
| APPL-11 | ✓ SATISFIED | Fallback WARN row, compose mount (30-06, 30-11, 30-18) — see scrutiny item 1 above |
| APPL-12 | ✓ SATISFIED | `local_time`, `LOCAL_TIME_FORMAT`, TZ compose line (30-01, 30-02, 30-09, 30-10, 30-12, 30-17, 30-18) |

### Anti-Patterns Found

None. Grep for `TBD`/`FIXME`/`XXX` across all phase-touched source files: 0 matches. `TODO`/`HACK`/
literal `placeholder` copy in templates: only legitimate HTML `placeholder=` attributes and the
`is_placeholder_token`/`PLACEHOLDER_TOKENS` identifiers (not stub markers). All `# noqa` /
`# type: ignore` occurrences in phase-touched files (`config.py`) predate Phase 30 (confirmed via
`git log -S`, tracing to Phase 01's original commit). No `--no-verify`, `SKIP=`, or suppressed
lint/type errors anywhere in the tree.

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| Full test suite passes | `uv run pytest -q` | `2888 passed in 132.17s` | ✓ PASS (independently re-run, not trusted from SUMMARY) |
| Lint is clean | `uv run ruff check .` | `All checks passed!` | ✓ PASS (independently re-run) |
| No new suppressions | `git log -S "noqa" -- <phase files>` | all matches predate Phase 30 | ✓ PASS |
| `checks.py` has no upward dependency | AST import inspection | no `web`/`cli` imports found | ✓ PASS |
| No log path in any template | `grep -rn` over `web/templates/` | 0 matches | ✓ PASS |
| `doctor --json` genuinely absent | `grep "as_json"` scoped to `doctor` | not present; only `jobs --json` exists | ✓ PASS |

### Probe Execution

No `scripts/*/tests/probe-*.sh` convention or PLAN/SUMMARY-declared probes found for this phase
(it is a feature phase, not a migration/tooling phase). Step 7c: SKIPPED — no probes declared or
discovered.

### Human Verification Required

None. Per project CLAUDE.md, all browser-based behaviours (visual layout, HTMX interactivity,
cookie jars, native `confirm()` dialogs, touch-target sizing, colour contrast) were automated via
Playwright in `tests/test_browser.py` (plans 30-17 and 30-19), independently confirmed present and
substantive by this verifier, not merely claimed by the SUMMARYs. No manual-only or
human-verification items were found or introduced.

### Gaps Summary

No gaps found. All 5 ROADMAP success criteria verified against actual code (not SUMMARY claims).
All 12 requirement IDs traced to plan frontmatter and to concrete, substantive, wired artifacts.
The six specific scrutiny items requested by the orchestrator were each independently investigated
against the source and resolved: one (APPL-11 commented mount) is accepted as a defensible,
documented scope decision consistent with the file's existing idiom and REQUIREMENTS' literal
wording; the remaining five are confirmed coherent, correctly scoped, or genuinely tested as
claimed. The test suite (2888 tests, including 127 browser tests), lint, and type-check claims were
independently reproduced rather than trusted.

---

*Verified: 2026-09-16T22:14:16Z*
*Verifier: Claude (gsd-verifier)*
