---
phase: 30-appliance-layer
verified: 2026-09-17T00:00:00Z
status: passed
score: 5/5 must-haves verified
overrides_applied: 0
re_verification:
  previous_status: passed
  previous_score: 5/5
  gaps_closed:
    - "CR-01: web Profiles row lied on every config-file deployment, contradicting doctor"
    - "CR-02: saned pre-probe short-circuited get_devices(), producing a false FAIL/exit 2 on a working scanner"
    - "WR-01: _saned_hosts mis-parsed IPv6/bracketed addresses into bogus host/port pairs"
    - "WR-02: saned probe's documented 2s bound was neither the wall-clock bound nor DNS-inclusive"
    - "WR-03: scanner gate held across work that never touches SANE, stalling scan starts"
    - "WR-04: gate contention between two checkers misreported as 'a scan is running'"
    - "WR-05: POST /api/checks/refresh was an unauthenticated, unbounded probe amplifier"
    - "WR-06: a user could no longer submit a scan with no tags / no correspondent (profile-default override bug)"
    - "WR-07: shutdown's worst case was two join bounds, not the one STOP_JOIN_SECONDS the comment claimed"
    - "IN-01: index page fetched Paperless metadata it would never render"
    - "IN-02: paperless_test logged the raw exception object (ASVS V7 outlier)"
    - "IN-03: _note_pass_count interpolated a user-supplied title with %s, not %r"
    - "IN-04: _profile_storage was cross-thread state with no lock and no documented discipline"
    - "IN-05: PLACEHOLDER_TOKENS cited a doc line the same phase deleted"
    - "IN-06: local_time could emit a trailing space into a generated document title"
    - "IN-07: the cold-start strip polled every 2s forever if the cache was never filled"
  gaps_remaining: []
  regressions: []
---

# Phase 30: Appliance Layer Verification Report (Re-verification after gap closure)

**Phase Goal:** Status strip and `saneless doctor` from one source of truth, so the web UI and the
CLI cannot disagree about the health of the same appliance — with page counts on every terminal
job, plain-language errors with a next step, human profile labels, queue position, and an
owner-only flip prompt, help text and docs for each new surface written in-phase.

**Verified:** 2026-09-17T00:00:00Z
**Status:** passed
**Re-verification:** Yes — after gap closure (plans 30-20..30-27 closing all 16 findings from `30-REVIEW.md`)

## Method

This is a re-verification. The prior VERIFICATION.md (dated 2026-09-16, pre-gap-closure) passed
5/5 ROADMAP success criteria on plans 30-01..30-19, but that pass was taken *before* a deep code
review (`30-REVIEW.md`) found 2 critical and 7 warning defects, several of which directly attack
the phase goal's central claim — that the CLI and the web UI cannot disagree about the same
appliance (CR-01 broke this outright; CR-02 broke `doctor`'s exit code truthfulness).

For this pass I did not trust any SUMMARY.md claim. For all 16 findings (CR-01, CR-02, WR-01..07,
IN-01..07) I read the actual current source at HEAD `8976d1a` (branch `autodev`) — `checks.py`,
`config.py`, `worker.py`, `cli.py`, `web/app.py`, `web/refresher.py`, `web/checks_cache.py`,
`web/routes.py`, `pipeline.py`, `vocabulary.py`, `docker-compose.yml`, `docs/reference/web-api.md`
— and confirmed each fix is present, matches the finding's own proposed remedy in substance, and is
pinned by a test that exercises the actual code path (not just a result that happens to match).
I independently re-ran `uv run pytest -q` (3025 passed, matches the stated HEAD), `uv run ruff
check .` / `ruff format --check .` (clean), `uv run ty check` (all passed), and
`uv run pyrefly check src tests` (0 errors) rather than trusting the orchestrator's reported gate
state.

## Goal Achievement

### Observable Truths (Success Criteria, ROADMAP-derived)

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | `saneless doctor` and the web status strip derive every check from one shared source, and cannot disagree about the same appliance | ✓ VERIFIED | The phase's one prior structural gap here (CR-01) is closed: `config.profile_storage_for_loaded(settings)` (`config.py:673-709`) is now the single derivation of "nothing was written, so where do the profiles live?", called by both `cli.py`'s `doctor` (`cli.py:1154`, passed straight into `CheckContext.profile_storage`) and `ScanWorker._generate_startup_profiles`'s two early-return branches (`worker.py:911`, `worker.py:917`). No third copy of the rule exists anywhere in the tree (`grep -rn "config_path is not None" src/` finds only this function's own line and two unrelated call sites in `config.py`'s loader). `tests/test_doctor.py`'s `_recording` monkeypatch on `saneless.cli.profile_storage_for_loaded` pins that `doctor` genuinely *calls* the shared function rather than merely producing a matching answer by coincidence; `tests/test_worker.py` has 10 `test_profile_storage_*` cases covering every branch including the non-bare-config-file path CR-01 broke. For the web side, `web/app.py:150`'s `build_check_context()` reads `worker.profile_storage` live at *call time* (documented as deliberate: "the worker records it after `create_app` has already returned"), so the refresher's probe and `doctor`'s one-shot probe read from the same live fact, not a snapshot. |
| 2 | `saneless doctor` exits non-zero on a real problem but never on a healthy, working appliance (CR-02 closed) | ✓ VERIFIED | The saned pre-probe no longer returns `_scanner_unreachable()` (FAIL) when a configured net host refuses TCP; it returns the new `_scanner_host_unanswered()` (WARN) instead (`checks.py:744-789`), and only when `_saned_hosts` actually yielded entries to probe — a machine with a working local/USB scanner and a stale net-host setting now reports amber, not red, and `doctor` exits 0. Pinned directly: `tests/test_checks.py::test_a_closed_saned_port_skips_the_backend` asserts `row.state is CheckState.WARN` with the exact new message. The probe also now reads `os.environ.get("SANE_NET_HOSTS") or settings.scanner.host` (`checks.py:469-496`, `_saned_host_setting`) rather than the config value alone, closing the second divergence CR-02 documented (dialling a host SANE was not using). |
| 3 | The IPv6/bracketed-address parser and the connect-timeout bound are correct (WR-01, WR-02 closed) | ✓ VERIFIED | `_saned_hosts` (`checks.py:532-596`) now refuses (`return ()`) any setting with more than one colon unless every segment passes `_looks_like_a_host_name` — an IPv6 literal like `fe80::1` or `[fe80::1]:6566` no longer fans out into bogus host/port dials; falls through to `get_devices()` exactly as the module's stated fallback discipline promises. Pinned by `TestSanedHostParsing::test_a_bare_ipv6_literal_produces_no_entries_to_probe`, `test_a_bracketed_ipv6_literal_produces_no_entries_to_probe`, `test_the_ipv6_loopback_produces_no_entries_to_probe`. `_saned_reachable` (`checks.py:599-669`) now resolves once and dials only the first address (`socket.getaddrinfo(...)[0]`), so the connect budget is one `PROBE_CONNECT_SECONDS` per configured host, not per resolved address; the docstring states plainly that DNS resolution itself remains outside the bound (an honest limitation, not silently claimed away). |
| 4 | The scanner gate is held only around the scanner check, and gate contention is not misreported as "a scan is running" (WR-03, WR-04 closed) | ✓ VERIFIED | `run_checks(context, *, scanner_gate=None)` (`checks.py:1186-1246`) now takes the gate as a parameter and only `_scanner_result` (`checks.py:1154-1183`) acquires it, non-blocking, around the single `_dispatch(CheckKey.SCANNER, ...)` call — the Paperless HTTP probe and the two filesystem writes no longer sit behind the scanner gate. `CheckRefresher._probe_and_store` (`refresher.py:239-282`) derives `skip_scanner` from `self._scan_active()` — `worker.current_job_id is not None`, the same fact `_checks_context` renders in the template — not from whether the gate happened to be free; a probe that loses the gate to the worker no longer renders "Not checked while a scan is running" on an idle appliance. Both `probe_now()` (the Refresh button's path) and `_tick()` funnel through the one `_probe_and_store` implementation, closing the "drifted into two probe implementations" root cause the review named. |
| 5 | `POST /api/checks/refresh` cannot be used as an unbounded probe amplifier (WR-05 closed) | ✓ VERIFIED | `CheckCache.claim_manual_refresh(min_interval=MIN_MANUAL_REFRESH_SECONDS)` (`checks_cache.py:26,198`, `MIN_MANUAL_REFRESH_SECONDS = 2.0`) grants a probe at most once per 2 seconds under the cache's own lock; `routes.py:1342`'s `refresh_checks` handler only calls `probe_now()` on a granted claim, otherwise re-renders the current strip with the same status/body. `docs/reference/web-api.md:151` documents the floor next to the GET's no-traffic guarantee. |
| 6 | Shutdown's stated bound (`STOP_JOIN_SECONDS`, not double it) is what the code delivers (WR-07 closed) | ✓ VERIFIED | `_stop_threads` (`app.py:167-201`) takes one `deadline = time.monotonic() + STOP_JOIN_SECONDS` before either join and hands the refresher `max(0.0, deadline - time.monotonic())`; `CheckRefresher.stop(timeout=...)` (`refresher.py:173-205`) honours the caller-supplied bound, clamped at zero. The docstrings (`refresher.py:152-171`) were corrected to say what the code now does rather than repeat the disproved overlap claim. |
| 7 | Every terminal job shows pages scanned/removed/uploaded; manual duplex shows front/back counts; a queued job shows the wait line | ✓ VERIFIED (unchanged from prior pass, re-confirmed) | `vocabulary.page_counts()` / `busy_line()` unchanged by gap closure except for the `local_time` rstrip fix (IN-06, below); wiring into `partials/status.html` / `partials/history.html` re-confirmed present. |
| 8 | Every user-facing error shows a plain-language message and next step, raw detail in a collapsed disclosure | ✓ VERIFIED (unchanged, re-confirmed) | `vocabulary.error_advice()` and the ERROR branch in `partials/status.html` re-read; unaffected by gap closure. |
| 9 | Two browser contexts show owner Continue/Abort (with Abort confirm) and non-owner "Waiting for the stack to be flipped"; profile dropdowns show human labels/descriptions | ✓ VERIFIED (unchanged, re-confirmed) | `tests/test_browser.py::test_the_owner_is_offered_the_flip_and_the_second_browser_is_not` (line 3774) re-read in full: two real `browser.new_context()` cookie jars, `HttpOnly`/`SameSite=Lax`/session-cookie assertions, zero flip controls in the non-owner DOM. Genuine Playwright automation against real Chromium, not a manual-only item. |

**Score:** 9/9 truths verified (5 ROADMAP success criteria + 4 additional truths specific to the
D-02 single-source-of-truth claim the orchestrator asked me to scrutinise).

### Gap-Closure Finding Traceability (all 16 from `30-REVIEW.md`)

| Finding | Severity | Plan | Fix location | Test pinning it | Status |
|---|---|---|---|---|---|
| CR-01 | Critical | 30-20 | `config.py:673` `profile_storage_for_loaded`; `worker.py:911,917` | `test_config.py::TestProfileStorageForLoaded`, `test_worker.py` (10 cases), `test_doctor.py` monkeypatch-call test | ✓ Closed |
| CR-02 | Critical | 30-21 | `checks.py:744` `_scanner_host_unanswered`, `checks.py:850-854` | `test_checks.py::test_a_closed_saned_port_skips_the_backend` | ✓ Closed |
| WR-01 | Warning | 30-21 | `checks.py:499` `_looks_like_a_host_name`, `checks.py:583-591` | `TestSanedHostParsing` (3 IPv6/bracket cases) | ✓ Closed |
| WR-02 | Warning | 30-21 | `checks.py:656-663` (one address, `getaddrinfo()[0]`) | `TestSanedProbeBound` | ✓ Closed |
| WR-03 | Warning | 30-25 | `checks.py:1154-1183,1186-1246` `run_checks(scanner_gate=...)` | `test_checks.py`, `test_refresher.py` (gate-sampling doubles) | ✓ Closed |
| WR-04 | Warning | 30-25 | `refresher.py:273` `skip_scanner=self._scan_active()` | `test_web_checks.py` (real job started, not gate held) | ✓ Closed |
| WR-05 | Warning | 30-26 | `checks_cache.py:26,198` `claim_manual_refresh`; `routes.py:1342` | `test_checks_cache.py`, `test_web_checks.py` (mock-transport request-count assertion) | ✓ Closed |
| WR-06 | Warning | 30-23 | `routes.py:1073-1076` (gate on `show_tags`/`show_correspondent`, not submitted value) | `test_web.py` (empty-tags-not-overridden case) | ✓ Closed |
| WR-07 | Warning | 30-24 | `app.py:167-201` `_stop_threads`; `refresher.py:173` `stop(timeout=...)` | `test_app_lifespan.py`, `test_refresher.py` (bound-by-value) | ✓ Closed |
| IN-01 | Info | 30-23 | `routes.py:401` (`_tag_list_context` flag-gated), `routes.py:789-791` | `test_web.py` (request-count assertion) | ✓ Closed |
| IN-02 | Info | 30-23 | `routes.py:874` `type(exc).__name__` | ast-parsed source guard in `test_web.py` | ✓ Closed |
| IN-03 | Info | 30-22 | `pipeline.py:1572,2216` `%r` | `test_pipeline.py` (caplog, `record.getMessage()`) | ✓ Closed |
| IN-04 | Info | 30-20 | `worker.py:760-786` property docstring (deliberately unlocked, documented) | Covered by CR-01's tests exercising the property | ✓ Closed |
| IN-05 | Info | 30-20/23 | `config.py:239-241` citation replaced with rationale, no stale line reference | — (doc-only) | ✓ Closed |
| IN-06 | Info | 30-22 | `vocabulary.py:632` `.rstrip()` on `local_time` | `test_vocabulary.py` (monkeypatched empty-`%Z` property test) | ✓ Closed |
| IN-07 | Info | 30-27 | `checks.py:144` `POLL_ATTEMPT_CAP = 10`; `routes.py:1248,306` | `test_web_checks.py`, `test_browser.py` (real request-count property) | ✓ Closed |

No regressions found in previously-passing must-haves: page counts, busy line, error advice, owner
cookie/flip gating, profile labels, and help text are all unaffected by the gap-closure diffs
(confirmed by re-reading `vocabulary.py`, `partials/status.html`, `partials/flip.html`).

### Required Artifacts (gap-closure diffs, all read in full)

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `src/saneless/config.py` | `profile_storage_for_loaded` | ✓ VERIFIED | Present, pure, documented, deliberately cannot return `IN_MEMORY_UNWRITABLE` |
| `src/saneless/worker.py` | Both early-return branches of `_generate_startup_profiles` call the shared function | ✓ VERIFIED | Lines 906-918 |
| `src/saneless/cli.py` | `doctor` calls `profile_storage_for_loaded` | ✓ VERIFIED | Line 1154, with a comment explaining why this keeps D-02 |
| `src/saneless/checks.py` | `_scanner_host_unanswered`, `_saned_host_setting`, `_looks_like_a_host_name`, `run_checks(scanner_gate=...)`, `POLL_ATTEMPT_CAP` | ✓ VERIFIED | All present, substantive, documented, exercised by dedicated test classes |
| `src/saneless/web/refresher.py` | `probe_now`, `_probe_and_store` (single implementation), `stop(timeout=...)` | ✓ VERIFIED | Both public entry points delegate to one private implementation; no re-entrant duplication |
| `src/saneless/web/checks_cache.py` | `claim_manual_refresh`, `MIN_MANUAL_REFRESH_SECONDS` | ✓ VERIFIED | Present, under the cache's existing lock |
| `src/saneless/web/app.py` | `_stop_threads` with a shared deadline | ✓ VERIFIED | Extracted helper, deadline computed once |
| `src/saneless/web/routes.py` | Flag-gated metadata fetch, `type(exc).__name__` logging, form-shape profile-default gating, `attempt` query param | ✓ VERIFIED | All present |
| `src/saneless/pipeline.py` | `%r` for all user-title interpolations | ✓ VERIFIED | 2 call sites confirmed (`pipeline.py:1572,2216`); a third pre-existing profile/device line also converted |
| `src/saneless/vocabulary.py` | `local_time().rstrip()` | ✓ VERIFIED | Line 632 |
| `docs/reference/web-api.md` | Refresh floor and single-flight documented | ✓ VERIFIED | Lines 147-151 |

### Key Link Verification

| From | To | Via | Status |
|------|-----|-----|--------|
| `cli.py doctor` | `config.profile_storage_for_loaded` | direct call, passed into `CheckContext` | ✓ WIRED (test pins the call itself, not just the result) |
| `worker.py _generate_startup_profiles` (both early returns) | `config.profile_storage_for_loaded` | direct call | ✓ WIRED |
| `web/app.py build_check_context` | `worker.profile_storage` | read live at call time, not captured at `create_app` | ✓ WIRED |
| `checks.py _check_scanner` | `_scanner_host_unanswered` (WARN, not FAIL) | returned when `_saned_hosts` yields entries and none answer | ✓ WIRED |
| `checks.py run_checks` | `scanner_gate` parameter | held only around `_scanner_result`, not the whole registry | ✓ WIRED |
| `web/refresher.py _probe_and_store` | `worker.current_job_id` (via `scan_active` callable) | `skip_scanner` derived from the fact, not gate contention | ✓ WIRED |
| `web/routes.py refresh_checks` | `CheckCache.claim_manual_refresh` | gates every `probe_now()` call | ✓ WIRED |
| `web/app.py _stop_threads` | `CheckRefresher.stop(timeout=...)` | one shared `time.monotonic()` deadline | ✓ WIRED |

### Requirements Coverage

All 12 requirement IDs (APPL-01 through APPL-12) remain traced to concrete, substantive, wired
artifacts; the gap-closure plans additionally claim APPL-01, APPL-02, APPL-03, APPL-04, APPL-06,
APPL-10, APPL-12 in their frontmatter, which is consistent — the fixes tighten the correctness of
checks already covering those IDs rather than introducing new surface area. No orphaned
requirements found.

One documentation-hygiene item, not a code gap: `.planning/REQUIREMENTS.md`'s checkbox column for
APPL-01..12 (lines 129-140) and the phase-mapping table (lines 316-327) still read `[ ]` /
"Pending" rather than checked/"Done". This is a tracking-file bookkeeping step, not evidence the
requirements are unmet — every APPL-* ID is independently confirmed satisfied by the artifact and
key-link evidence above. Flagged for the phase-closure step to update, not as a gap blocking this
verification.

### Anti-Patterns Found

None in gap-closure-touched files. `grep -rn "TBD\|FIXME\|XXX"` across every file touched by
30-20..30-27 (`checks.py`, `worker.py`, `config.py`, `cli.py`, `web/app.py`, `web/refresher.py`,
`web/checks_cache.py`, `web/routes.py`, `pipeline.py`, `vocabulary.py`, `docker-compose.yml`,
`docs/reference/web-api.md`): 0 matches. `TODO\|HACK`: 0 matches. No new `# noqa` or
`# type: ignore` suppressions introduced.

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| Full test suite passes at current HEAD | `uv run pytest -q` | `3025 passed in 192.08s` | ✓ PASS (independently re-run) |
| Lint is clean | `uv run ruff check .` | `No issues found` | ✓ PASS (independently re-run) |
| Format is clean | `uv run ruff format --check .` | `63 files already formatted` | ✓ PASS (independently re-run) |
| `ty` type check clean | `uv run ty check` | `All checks passed!` | ✓ PASS (independently re-run) |
| `pyrefly` type check clean | `uv run pyrefly check src tests` | `0 errors` | ✓ PASS (independently re-run) |
| CR-02 scanner row is WARN not FAIL on an unanswered configured host | `test_a_closed_saned_port_skips_the_backend` | asserts `CheckState.WARN` | ✓ PASS (read the assertion directly) |
| CR-01 doctor genuinely calls the shared function | `test_doctor.py` monkeypatch-recording test | `seen == [settings]`, `storage is ProfileStorage.PERSISTED` | ✓ PASS (read the assertion directly) |

### Probe Execution

No `scripts/*/tests/probe-*.sh` convention or PLAN/SUMMARY-declared probes for this phase (feature
phase, not migration/tooling). Step 7c: SKIPPED — no probes declared or discovered.

### Human Verification Required

None. Per project CLAUDE.md, browser-based behaviours (two-context cookie jars, HttpOnly/SameSite
assertions, DOM absence of flip controls for the non-owner, the cold-start poll's real request
count) are all automated via Playwright against real Chromium in `tests/test_browser.py`, confirmed
present and substantive by direct reading, not merely claimed by SUMMARYs. No manual-only or
physical-hardware items exist in this phase's scope.

### Gaps Summary

No gaps found. This re-verification independently confirmed that all 16 findings from
`30-REVIEW.md` (2 critical, 7 warning, 7 info) are genuinely fixed in the current source, each
backed by a test that exercises the real code path rather than merely asserting a matching output.
The phase goal's core claim — that the web UI and the CLI cannot disagree about the health of the
same appliance — was specifically re-verified for the Profiles row (CR-01, the exact defect that
broke it): `config.profile_storage_for_loaded` is now the sole derivation, called by both `doctor`
and the worker, with a test pinning the call itself rather than just the coincidence of matching
answers, and no third copy of the derivation exists anywhere in the tree. `saneless doctor`'s exit
code is now truthful for a working scanner behind a stale net-host setting (CR-02). All gate
commands (pytest, ruff, ty, pyrefly) were independently re-run at HEAD and are clean. The one
documentation-hygiene item (`REQUIREMENTS.md` checkboxes not yet flipped) does not affect the
codebase's satisfaction of the requirements and is noted for phase-closure bookkeeping, not as a
gap.

---

*Verified: 2026-09-17T00:00:00Z*
*Verifier: Claude (gsd-verifier)*
