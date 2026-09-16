---
phase: 30-appliance-layer
plan: 06
subsystem: appliance-health
tags: [check-registry, strenum, assert-never, socket-probe, httpx-timeout, asvs-v7, tdd]

# Dependency graph
requires:
  - phase: 30-appliance-layer
    provides: ProfileStorage (plan 30-01), is_placeholder_token and ProfileConfig.label (plan 30-02), ScanWorker.scanner_gate and ScanWorker.profile_storage (plan 30-04)
  - phase: 27-profile-generation
    provides: ProfileConfig.auto_generated, and D-09's "a write is the only honest writability probe"
  - phase: 23-paperless-contract
    provides: ConnectionStatus, connection_status_message, PaperlessClient.test_connection
provides:
  - src/saneless/checks.py — the one health-check registry both surfaces read
  - CheckKey (five members) and CheckState (three members), the D-02 and D-01 contracts as types
  - CheckResult, frozen and slotted, carrying message, next_step and skipped
  - CheckContext, the injection point for settings, scanner, Paperless client and profile storage
  - run_checks(context) -> tuple[CheckResult, ...], total over CheckKey and unable to raise
  - worst_state(results), the FAIL > WARN > OK collapse doctor's exit code is built on
  - check_name / check_state_label / check_state_class / check_state_glyph, the four presentation lookups templates must not re-derive
  - CHECKING_GLYPH / CHECKING_STATE_CLASS / CHECKING_MESSAGE, the cold-start row's vocabulary
  - _saned_hosts and _saned_reachable, the only bounded SANE reachability probe in the tree
  - PaperlessClient.test_connection(timeout=...), a per-request bound that leaves every existing caller alone
affects: [30-07, 30-08, 30-11, 30-12]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "shared registry with an explicit anti-import rule: a module consumed by two surfaces states in its docstring that it may import neither, and a test reads the source to enforce it"
    - "dependency injection through a frozen context dataclass, so doctor (no worker, possibly no python-sane) and the web app build the same registry from different pieces"
    - "bounded pre-probe before an unbounded call: socket.create_connection with a timeout against the same address the C library would dial"
    - "per-request timeout override rather than a second client, so one caller can be fast without changing another caller's budget"
    - "a registry that cannot raise: per-check exceptions are caught and rendered as a developer-constant FAIL row"

key-files:
  created:
    - src/saneless/checks.py
    - tests/test_checks.py
  modified:
    - src/saneless/paperless.py
    - tests/test_paperless.py

key-decisions:
  - "A closed saned port produces FAIL with zero backend calls, rather than falling back to get_devices(). The plan's Task 2 docstring text and Task 3 behaviour block disagreed on this; Task 3 wins because a pre-probe that falls back to the unbounded call on failure provides no protection at all, which would void T-30-22"
  - "The no-false-FAIL fallback is instead 'the probe could not be run': when _saned_hosts yields no entry to dial, no probe happens and the check behaves exactly as it did before this module existed. Only a probe that ran and was refused on every configured entry goes red"
  - "_saned_hosts refuses a trailing number outside 1..65535 as a port. socket.create_connection raises OverflowError for an out-of-range port, and OverflowError is not an OSError, so accepting one would put an exception the probe does not catch inside the probe"
  - "The Scanner row names vendor and model, never DeviceInfo.name. The SANE device id is net:<host>:<backend>:... for the net backend, so rendering it would publish a LAN address on a LAN-visible page — the same reason the fallback row omits the folder path"
  - "The skipped Scanner row carries state OK, not WARN. 'We did not look' is a fact about the probe, not a verdict about the appliance, and a scan in flight is evidence the scanner worked moments ago; a scripted health gate must not go red for the duration of every scan"
  - "paperless=None maps to the NOT_FOUND row rather than a sixth sentence. The only way PaperlessClient.__init__ refuses is a URL httpx will not parse, which is exactly 'the API was not found at that URL'"
  - "skip_scanner is honoured in run_checks, not inside _check_scanner, so the 'no backend call' guarantee is visible at the one place both surfaces call"
  - "test_connection's timeout is keyword-only and defaults to httpx.USE_CLIENT_DEFAULT, so GET /api/paperless/test keeps today's flat 30 s with no edit to routes.py"
  - "The two D-22 profile sentences are written as single over-length literals (E501 is off in this project) so a grep for the verbatim copy finds it on one line"

patterns-established:
  - "Import-hygiene test: read the module's own source via Path(module.__file__) and assert no line starts with a forbidden import prefix"
  - "Timeout assertions read request.extensions['timeout'] from an httpx.MockTransport handler instead of measuring wall-clock, so the suite's no-sleep rule holds and nothing is flaky"
  - "Socket probe tests bind 127.0.0.1 port 0 for the reachable case and re-use a released port for the refused case; scanner.invalid (RFC 2606) covers the unresolvable case with no network"
  - "_CountingBackend / _RequestCounter: stubs that count calls, so 'zero SANE calls' and 'zero HTTP requests' are measured rather than asserted by inspection"

requirements-completed: [APPL-01, APPL-02, APPL-06, APPL-07, APPL-11]

# Metrics
duration: 17min
completed: 2026-09-16
---

# Phase 30 Plan 06: The Check Registry Summary

**One `checks.py` registry — five keys, three states, injected dependencies — plus the only bounded SANE reachability probe in the tree and a per-request bound on the Paperless probe.**

## Performance

- **Duration:** 17 min
- **Started:** 2026-09-16T17:05:11Z
- **Completed:** 2026-09-16T17:21:42Z
- **Tasks:** 3 (all TDD, six commits)
- **Files modified:** 4 (2 created, 2 modified)

## Accomplishments

- `src/saneless/checks.py` exists and is the single definition of what "healthy" means. `CheckKey` has five members and `run_checks` iterates it, so neither `saneless doctor` nor the status strip is *able* to hold a check the other does not have.
- The saned pre-probe, the one file in this phase with no analog anywhere in the tree, is written and unit-tested against real loopback sockets. An unplugged sane-net host now answers in about two seconds instead of roughly 127, and it does so without entering `get_devices()` at all.
- `PaperlessClient.test_connection` takes a per-request bound. The strip and `doctor` pass ~2 s; `GET /api/paperless/test` still sends the client's flat 30 s, with no edit to `routes.py`.
- Every one of the twenty UI-SPEC S1 message/next-step pairs is a developer constant in one place, and a parametrised guard test proves no row can carry a path, a URL, a token value or a traceback.
- A placeholder token fails the Paperless check with **zero** HTTP requests, and `skip_scanner` produces the paused row with **zero** backend calls. Both are counted in tests, not assumed.

## Task Commits

1. **Task 1: a bounded Paperless probe** — `a59c41d` (test, RED) → `935da4a` (feat, GREEN)
2. **Task 2: checks.py types, presentation lookups, saned pre-probe** — `9df732e` (test, RED) → `ef4a493` (feat, GREEN)
3. **Task 3: the five checks and run_checks** — `35406e9` (test, RED) → `d5bb5d7` (feat, GREEN)

No REFACTOR commits were needed; each GREEN landed clean under all four gates.

## Files Created/Modified

- `src/saneless/checks.py` (new, ~1000 lines incl. docstrings) — `CheckState`, `CheckKey`, `CheckResult`, `CheckContext`, the four presentation lookups, `worst_state`, `_saned_hosts`, `_saned_reachable`, `_directory_accepts_a_write`, `_device_label`, the five private check functions, `_dispatch` and `run_checks`.
- `tests/test_checks.py` (new, 91 tests) — vocabulary completeness, worst-state collapse, host parsing, socket probing, import hygiene, one class per check, and the `run_checks` completeness/ordering/safety/leak guards.
- `src/saneless/paperless.py` — `test_connection` grew a keyword-only `timeout: httpx.Timeout | None = None`; the constructor's flat 30 s, the status-code chain and the exception arms are untouched.
- `tests/test_paperless.py` — new `TestConnectionTimeout` class (13 tests with parametrisation).

## Decisions Made

See `key-decisions` in the frontmatter. The two that later plans most need to know about:

1. **A closed saned port is authoritative.** `30-07`'s cache and `30-08`'s `doctor` will see a red Scanner row within ~2 s of a host being unplugged, with no SANE call made. The documented fallback that cannot go red is narrower than the plan's Task 2 prose suggested — see Deviations.
2. **The Scanner row never renders `DeviceInfo.name`.** `30-11`'s template can render `c.message` directly without any further redaction.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 — Contradiction between two task specifications] The saned pre-probe's failure policy**

- **Found during:** Task 2 (writing `_saned_reachable`'s docstring) and confirmed in Task 3.
- **Issue:** Task 2's `<action>` says a failed pre-probe "falls back to calling `get_devices()` rather than reporting `FAIL` outright". Task 3's `<behavior>` says a configured host "whose saned port is closed yields `FAIL` without entering the backend at all (assert zero `get_devices` calls)". These cannot both hold. A pre-probe that falls back to the unbounded call whenever it fails provides exactly zero protection, because failing *is* the condition it exists to short-circuit — it would void T-30-22 and success criterion 2 while keeping the code that looks like a mitigation.
- **Fix:** Task 3's reading implemented. A probe that *ran* and was refused on every configured entry produces `FAIL` with no backend call. The no-false-FAIL fallback is relocated to the case the plan was really guarding against — a probe that *could not be run*: when `_saned_hosts` yields no entry to dial (no host configured, or a setting this module will not guess at), no probe happens and the check falls through to `get_devices()`, i.e. exactly today's behaviour. This is documented at length on `_saned_reachable` and asserted by `test_no_configured_host_is_never_pre_probed`. RESEARCH assumption A1 is settled by the plan's own `/etc/services` verification, so the "wrong port" scenario the original prose feared is not live.
- **Files modified:** `src/saneless/checks.py`, `tests/test_checks.py`
- **Verification:** `test_a_closed_saned_port_skips_the_backend` (FAIL, zero calls), `test_a_listening_saned_port_reaches_the_backend` (one call), `test_no_configured_host_is_never_pre_probed` (one call, OK).
- **Committed in:** `ef4a493` and `d5bb5d7`

**2. [Rule 2 — Missing critical: information disclosure] The Scanner row must not render the SANE device id**

- **Found during:** Task 3 (writing the "device is ready" row).
- **Issue:** UI-SPEC S1 says `{device} is ready.`, and the obvious field is `DeviceInfo.name`. For the `net` backend that value is `net:<host>:<backend>:...` — it embeds the scanner's LAN address. Rendering it on the index page publishes an internal hostname to everyone who can load the page, which is precisely what T-30-21 and the fallback row's omission of the folder path exist to prevent, and the plan's own guard test (no `/`, no `http`) would not have caught it.
- **Fix:** Added `_device_label`, which composes the row from `vendor` and `model` — what is printed on the machine's lid, and therefore what a household member can actually match against the device in front of them — and never from `name`. The label is whitespace-collapsed and bounded at 60 characters, because nothing else bounds what a scanner can call itself and the row sits in a fixed-width column (ROBU-08).
- **Files modified:** `src/saneless/checks.py`, `tests/test_checks.py`
- **Verification:** `test_the_sane_device_id_is_never_rendered` asserts neither `scanbox.lan` nor `net:` appears in the message.
- **Committed in:** `d5bb5d7`

**3. [Rule 3 — Blocking] `_saned_hosts` had to reject an out-of-range port**

- **Found during:** Task 2.
- **Issue:** `socket.create_connection` raises `OverflowError` for a port outside 0..65535, and `OverflowError` is **not** an `OSError`. A setting of `host:99999` would therefore have parsed to a port and then raised straight out of `_saned_reachable`, past the one `except` arm, and out of the scanner check — turning a typo in a config file into an exception the strip would have to catch generically.
- **Fix:** The two-segment port rule now requires the trailing segment to be all digits **and** in 1..65535. Anything else is read as a list of host names, which degrades to a name that does not resolve and one refused connect.
- **Files modified:** `src/saneless/checks.py`, `tests/test_checks.py`
- **Verification:** `test_an_out_of_range_port_is_not_a_port`.
- **Committed in:** `ef4a493`

**4. [Rule 1 — Bug in own test] `test_a_skipped_scanner_is_not_red` asserted the wrong thing**

- **Found during:** Task 3 GREEN.
- **Issue:** The RED test asserted `worst_state(results) is not CheckState.FAIL` over the whole run, but that context also had `paperless=None`, which is legitimately a red row. The assertion measured an unrelated check.
- **Fix:** Scoped the assertion to the scanner row itself: `worst_state([scanner_row]) is CheckState.OK`.
- **Files modified:** `tests/test_checks.py`
- **Committed in:** `d5bb5d7`

### Small additions beyond the literal task text

- **`CHECKING_GLYPH` / `CHECKING_STATE_CLASS` / `CHECKING_MESSAGE`.** Task 2 named U+00B7 "for the cold-start marker" and required the glyphs to live here "so the template never owns them". The class and the `Checking…` copy belong with it for the same reason; `30-11` would otherwise have had to author two of the three in a template.
- **`_CHECK_FAILED_MESSAGE` / `_CHECK_FAILED_NEXT_STEP`.** Task 3 required a raising check to render as "a `FAIL` row with a developer-constant message" but did not supply the copy. `"This check could not be completed."` / `"Restart saneless, then press Check again."` — both path-free, URL-free and consistent with the S1 voice. **`30-11` and `30-08` should confirm this copy against the UI-SPEC author's intent** if a canonical sentence exists elsewhere.
- **The glyphs are written as Python escapes / literal characters, not HTML entities.** Task 2's phrase "written as the escaped HTML entities the UI-SPEC names" cannot be taken literally: Jinja's autoescape would render `&#8230;` as `&amp;#8230;`. The code points named are the ones shipped.

---

**Total deviations:** 4 auto-fixed (2 × Rule 1, 1 × Rule 2, 1 × Rule 3), plus 3 small documented additions.
**Impact on plan:** No scope creep. Deviation 1 is the only one that changes plan-visible behaviour, and it resolves a contradiction *within* the plan in favour of the reading that makes the phase's second success criterion true.

## Issues Encountered

- **The worktree spawned on the wrong base.** `git merge-base HEAD 46b5ce2` returned `a87b3dd`, so the startup guard's `git reset --hard 46b5ce2` fired for real — and it mattered: before the reset the worktree had no `.planning/`, no `CLAUDE.md` and a stale `pyproject.toml`. This is the second wave running in which this guard has been load-bearing; it should not be dropped.
- **`ruff format` reflows string escapes into literal glyphs** in some files and leaves them as `\uXXXX` in others. Harmless, but it means a grep for `✓` in a test file will miss.
- **`PLR0913`** capped the test `_settings` helper at five arguments; `profiles` moved to a separate `_with_profiles` helper using `Settings.model_copy`.

## Verification

All plan gates pass from the worktree:

- `uv run pytest tests/test_checks.py -q` — 91 passed
- `uv run pytest tests/test_paperless.py -q` — 155 passed, the existing five-outcome tests unchanged
- `uv run pytest -m "not browser and not sane_hardware" -q` — **2385 passed, 74 deselected**
- `uv run ruff check .` / `uv run ruff format --check .` / `uv run ty check` / `uv run pyrefly check src tests` — all clean, zero suppressions added
- `grep -nE "^(from|import) saneless\.(web|cli)" src/saneless/checks.py` — empty
- Acceptance greps: `run_checks` signature ×1, `connection_status_message(` ×1, `is_placeholder_token(` ×1, APPL-11 fallback sentence ×1, D-22 read-only sentence ×1, `SANED_PORT: Final = 6566` ×1, `assert_never` ×6, `timeout: httpx.Timeout | None = None` ×1, `httpx.USE_CLIENT_DEFAULT` ×1, `"timeout": 30.0` ×1 (unchanged)

## Known Stubs

None. Every function in `checks.py` is wired to real settings, a real backend and a real client; nothing returns a hardcoded empty value. `checks.py` has no consumer yet — that is `30-07`, `30-08` and `30-11`'s job — but the module itself is complete and exercised.

## Threat Flags

None. Every surface this plan touches is already in the plan's threat register. The one finding not anticipated by it — `DeviceInfo.name` embedding a LAN host — is an instance of the registered T-30-21 (information disclosure in a check message) and is mitigated above, not a new surface.

## Next Phase Readiness

Ready for the three plans that consume this:

- **30-07 (cache):** cache `tuple[CheckResult, ...]`. The tuple is frozen all the way down, so a cached value cannot be mutated by a renderer.
- **30-08 (`saneless doctor`):** build a `CheckContext` with `scanner=None` where python-sane is absent, iterate `CheckKey` for ordering, use `check_name` / `check_state_label`, and map `worst_state(results) is CheckState.FAIL` to `ExitCode.CONFIG` (2). Do **not** add an `ExitCode` member — `tests/test_deployment_config.py:393,403` pin the enum.
- **30-11 (status strip):** register `check_name`, `check_state_class`, `check_state_glyph`, `check_state_label` as Jinja filters beside `state_label` in `web/app.py`; the cold-start row's three constants are `CHECKING_*` in this module. The template needs no conditional on state and no redaction of any message.

One open question for `30-08`/`30-11`: the fallback copy for a check that raised (`"This check could not be completed."`) was authored here because the plan required the row but not the words.

## Self-Check: PASSED

- `src/saneless/checks.py` — FOUND (976 lines)
- `tests/test_checks.py` — FOUND (1465 lines)
- `.planning/phases/30-appliance-layer/30-06-SUMMARY.md` — FOUND
- Commits `a59c41d`, `935da4a`, `9df732e`, `ef4a493`, `35406e9`, `d5bb5d7` — all FOUND in `git log`
- RED precedes GREEN in every task; no `--no-verify`, no `SKIP=`, no suppression comment added anywhere
- `STATE.md` and `ROADMAP.md` not modified (orchestrator owns those writes)

---
*Phase: 30-appliance-layer*
*Plan: 06*
*Completed: 2026-09-16*
