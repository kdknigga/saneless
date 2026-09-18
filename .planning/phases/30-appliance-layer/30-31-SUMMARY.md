---
phase: 30-appliance-layer
plan: 31
subsystem: testing
tags: [saned, health-checks, host-parsing, getaddrinfo, ipaddress, unicode, dos, tdd]

# Dependency graph
requires:
  - phase: 30-appliance-layer
    provides: "the saned pre-probe and its host parser (plans 30-21 and 30-28), and the round-3 review that found the parser could raise and could still be talked into dialling loopback (30-REVIEW.md)"
provides:
  - "a port branch that reads ASCII decimal only, so no string a scanner.host or SANE_NET_HOSTS value can hold makes _saned_hosts raise"
  - "_segment_is_a_numeric_address_shorthand, which refuses every numeric form glibc resolves as an address while still accepting a legal dotted quad"
  - "_MAX_PROBE_HOSTS, a cap on the dial list that bounds the duration of one POST /api/checks/refresh rather than only its rate"
  - "a _saned_hosts docstring with a test behind every behavioural sentence, including the three round 3 found false"
affects: [30-appliance-layer verification, any future change to the scanner check's pre-probe or its host parser]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "ASCII-decimal character-set tests instead of str.isdigit() wherever a string is about to reach int()"
    - "ask the stdlib (ipaddress.IPv4Address) to separate a configured address from a numeric shorthand, rather than hand-rolling glibc's inet_aton grammar"
    - "cap a fan-out list at the parser, after filtering, so the bound counts work that would really happen"

key-files:
  created: []
  modified:
    - src/saneless/checks.py
    - tests/test_checks.py

key-decisions:
  - "_saned_hosts('host:U+00B2') returns (('host', SANED_PORT),) rather than (), because the port branch declining and the final filter keeping the host is exactly the reading this module already gives host:99999 -- returning () would make one spelling of an unparseable port refuse the whole setting while another keeps the host"
  - "The wider numeric refusal is a new named helper rather than a widening of the isdigit() line; the isdigit() line is kept because it only ever rejects, is cheaper, and the new rule subsumes rather than replaces it"
  - "_part_is_a_number_to_glibc was split out of _segment_is_a_numeric_address_shorthand so the per-part grammar (decimal run, or ASCII 0x/0X plus at least one hex digit) is one readable function rather than a loop with three returns inside the stdlib call"
  - "_MAX_PROBE_HOSTS = 4 is chosen against the deployment rather than the clock -- a household appliance bridging scanners to paperless-ngx does not have five network scanners -- and the cost of being wrong is the module's usual one, a lost pre-probe rather than a lost check"
  - "The root dot (scanner.local.) is documented and pinned as refused rather than fixed: widening the accept surface for a spelling absent from this project's config examples buys nothing, and the cost is one latency saving"

patterns-established:
  - "Each mutation named in a plan's acceptance criteria is actually applied, run and reverted before the task is called done, and the failure counts are recorded in the summary"
  - "A docstring sentence that a review falsified is removed rather than qualified in place, and the removal is checked by a grep in the plan's acceptance criteria"

requirements-completed: [APPL-01, APPL-02]

# Metrics
duration: 25min
completed: 2026-09-17
---

# Phase 30 Plan 31: Host Parser Crash and Numeric-Shorthand Hardening Summary

**`_saned_hosts` can no longer raise on any string an operator or a compose file can put in `scanner.host` or `SANE_NET_HOSTS`, no segment glibc reads as a numeric IPv4 address reaches the dial list while a legal dotted quad still does, the list is capped at four entries, and every behavioural sentence of the parser's docstring now has a test behind it.**

## Performance

- **Duration:** ~25 min
- **Started:** 2026-09-18T01:12:00Z (approx; first commit 01:25:23Z)
- **Completed:** 2026-09-18T01:40:00Z
- **Tasks:** 3
- **Files modified:** 2

## Accomplishments

- **R3-CR-01 closed.** `str.isdigit()` is True for Unicode category `No` characters `int()` refuses, so `_saned_hosts("host:²")` raised `ValueError` before any socket existed — past `_saned_hosts` (catches nothing), past `_scanner_preflight` (catches nothing), past the probe's `except OSError` (never reached), into `run_checks`' generic per-check handler, which rendered a FAIL Scanner row reading "This check could not be completed." and made `saneless doctor` exit 2 on an appliance that scans fine. The port branch now tests `set(maybe_port) <= _ASCII_DIGITS`, so the raise is unreachable and `host:١٢٣٤` is never read as port 1234 either — a number libsane's C-side parsing would not derive from that string.
- **R3-WR-01 closed.** `_segment_is_a_numeric_address_shorthand` refuses every numeric form glibc resolves: `0.0`, `0x0.0` (both → `0.0.0.0`), `0x7f.1`, `127.1` (both → `127.0.0.1`), `6566.0`, `0xdeadbeef` and `01.02.03.04`. A `connect()` to `0.0.0.0` reaches loopback on Linux, so before this any unrelated local listener on 6566 could make `any(...)` true, suppress `_scanner_host_unanswered` and let the check fall through to the ~127 s uninterruptible `get_devices()` the pre-probe exists to avoid. `_saned_hosts("host:0.0")` no longer *invents* such a segment from a stray `.` in a port.
- **A legal dotted quad still gets its pre-probe.** `192.0.2.10` and `192.0.2.10:6566` are unchanged, and `box-x1.lan` is not mistaken for a hex literal, so the ordinary static-IP scanner keeps the saving the whole feature exists for.
- **R3-IN-05 closed.** `_MAX_PROBE_HOSTS = 4` caps the dial list after filtering. `_scanner_preflight`'s `any(...)` walk pays an unbounded `getaddrinfo` plus `PROBE_CONNECT_SECONDS` per entry inside the `POST /api/checks/refresh` request thread; the manual-refresh floor bounds how often that request may be made and says so, not how long one takes.
- **R3-IN-04 closed.** Three false docstring sentences corrected, each with a test named after the behaviour it pins. The falsified "has only ever meant one host" sentence is gone rather than qualified.

## Task Commits

Each task committed atomically; both TDD tasks ran RED before GREEN.

1. **Task 1: ASCII-decimal ports, and no numeric-address shorthand in the dial list**
   - `e0d08ed` (test) — pin the Unicode-digit port and numeric-shorthand cases
   - `9b5d1c0` (feat) — read ports as ASCII decimal and refuse numeric-address shorthands
2. **Task 2: bound the dial list**
   - `e7b6780` (test) — pin the dial-list cap
   - `be8c494` (feat) — cap the dial list at `_MAX_PROBE_HOSTS` entries
3. **Task 3: make every sentence of the parser's docstring a pinned fact**
   - `6aa7562` (docs) — three corrected sentences and four pinning tests, no production behaviour change

**Plan metadata:** see the `docs(30-31)` commit that carries this file.

No REFACTOR commits: neither GREEN left anything to clean up, and a no-op commit to complete the triple would be ceremony.

## TDD Gate Compliance

Both TDD tasks have a `test(...)` commit before their `feat(...)` commit, and both REDs failed for the intended reason:

- **Task 1 RED** (`e0d08ed`, before implementation): 17 selected, 5 passed, 12 failed. The two Unicode `No` cases failed with `ValueError` raised inside `_saned_hosts`; the Arabic-Indic case failed on `(('host', 1234),) != (('host', 6566),)`; the seven shorthand cases and `host:0.0` failed with the junk entry present; the end-to-end case failed on the Scanner row carrying `_CHECK_FAILED_MESSAGE`. The 5 that passed are the deliberate controls (ASCII ports, legal dotted quads, `box-x1.lan`).
- **Task 2 RED** (`e7b6780`, before implementation): 4 selected, 2 passed, 2 failed — `AttributeError: module 'saneless.checks' has no attribute '_MAX_PROBE_HOSTS'` and the order case seeing all forty entries. The constant is referenced as `checks._MAX_PROBE_HOSTS` rather than imported at module scope precisely so the RED is two failing tests instead of a collection error that hides the rest of the file.

Gate sequence in `git log`: `test(30-31)` → `feat(30-31)` → `test(30-31)` → `feat(30-31)` → `docs(30-31)`.

## Mutation Evidence

Every mutation the plan names was applied to the working tree, run, and reverted. None left the new tests green.

| Mutation | Command run | Result |
|---|---|---|
| Restore `maybe_port.isdigit()` in the port branch | `pytest -k "TestUnicodeDigitPorts or unicode_digit_port"` | 2 passed, **4 failed** — `ValueError` from `checks.py` on both `No` cases, wrong port on the Arabic-Indic case, `_CHECK_FAILED_MESSAGE` on the end-to-end case |
| `_segment_is_a_numeric_address_shorthand` returns `False` unconditionally | `pytest -k TestNumericAddressShorthand` | 3 passed, **8 failed** — e.g. `assert (('0.0', 6566),) == ()`; only the dotted-quad and `box-x1.lan` controls survive |
| Remove the `[:_MAX_PROBE_HOSTS]` slice | `pytest -k TestProbeHostCap` | 2 passed, **2 failed** — `assert 40 == 4`, plus the order case seeing `h40` |
| Delete the `len(segments) > 2` refusal | `pytest -k stray_colon` | 0 passed, **2 failed** — `assert (('localhost', 6566),) == ()` |

After each revert the full `tests/test_checks.py` was re-run green, so no mutation was left behind.

## Files Created/Modified

- `src/saneless/checks.py`
  - `from string import ascii_letters, digits, hexdigits` — `hexdigits` added; stdlib, nothing installed
  - `_ASCII_DIGITS` / `_ASCII_HEX_DIGITS`: **new** constants beside `_HOST_NAME_CHARACTERS`, with the `str.isdigit()` trap written down where the constant is defined
  - `_MAX_PROBE_HOSTS`: **new**, with the request-thread reasoning and the choice of 4 in its comment
  - `_part_is_a_number_to_glibc`: **new**, the per-part grammar (non-empty, ASCII, decimal run or `0x`/`0X` + ≥1 hex digit)
  - `_segment_is_a_numeric_address_shorthand`: **new**, two-step rule — any non-numeric part means "name", otherwise `ipaddress.IPv4Address` decides between a configured literal and a shorthand
  - `_looks_like_a_host_name`: new rejection after the `isdigit()` line; the paragraph arguing "an all-digit segment is rejected for that reason" rewritten to state the wider rule, name the five measured forms, and say explicitly that a legal dotted quad is accepted because dialling it is what the operator configured
  - `_saned_hosts`: port branch reads ASCII decimal (with an explicit non-empty test, since `set("") <= _ASCII_DIGITS` is True where `"".isdigit()` was False); final return sliced to `_MAX_PROBE_HOSTS`; docstring gains **The cap** paragraph, the leading-zero-port paragraph and the root-dot paragraph, and loses the falsified stray-colon sentence
- `tests/test_checks.py`
  - `TestUnicodeDigitPorts`: **new**, 5 cases (U+00B2, U+2460, Arabic-Indic 1234, and two ASCII controls)
  - `TestNumericAddressShorthand`: **new**, 11 cases (7 parametrised shorthands, the invented `host:0.0` segment, two dotted-quad controls, `box-x1.lan`)
  - `TestProbeHostCap`: **new**, 4 cases
  - `TestTheParserDocstringIsTrue`: **new**, 4 cases named after the sentences they pin
  - `TestScannerCheck.test_a_unicode_digit_port_does_not_redden_the_whole_run`: **new**, end to end through `run_checks` with `SANE_NET_HOSTS` set; `_recording_dialler` replaces `checks._saned_reachable`, so the test resolves no name and opens no socket
  - `tests/test_checks.py` went from 153 to 165 collected cases; the full suite is 3090 passed

No existing test's asserted value changed. The only pre-existing behaviour touched is the one the plan's `decision_record` settled deliberately, and it kept its old reading.

## Decisions Made

- **`host:²` keeps the host rather than refusing the setting.** Once the port branch's condition is false, control falls through to the filtered return, which is the same reading `host:99999` already has and already has a test for. Refusing the whole setting would have meant changing `host:99999` too — a behaviour change to a pinned, documented case with no defect behind it, inside a plan whose job is to stop a crash. The property the gap is about is pinned explicitly either way: no raise, no generic-failure row, no FAIL verdict.
- **Two helpers rather than one.** `_part_is_a_number_to_glibc` holds the per-part grammar and `_segment_is_a_numeric_address_shorthand` holds the two-step rule. Inlining the grammar put four `return`s and a `try` in one function for no gain in either readability or the acceptance greps.
- **The `isascii()` guard is explicit even though it is currently redundant.** Both character sets happen to be ASCII-only, so the guard cannot fire today. It is written anyway, with a comment saying so, because "a non-ASCII digit is never a number here" is the rule of the function, and a rule that holds only because of how a constant was spelled is one edit from not holding.
- **The cap goes after the filter.** `tuple(...)[:_MAX_PROBE_HOSTS]` counts entries that would really be dialled, not segments already dropped, so a setting with thirty junk segments and two names still probes both names.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 2 - Missing critical functionality] The port branch needed an explicit non-empty test the plan assumed already existed**

- **Found during:** Task 1 (GREEN)
- **Issue:** The plan's action says to keep "the existing `maybe_port` non-empty test". There is no such test in the branch — emptiness was carried implicitly by `"".isdigit()` being `False`. Swapping to `set(maybe_port) <= _ASCII_DIGITS` silently loses that, because `set("") <= _ASCII_DIGITS` is `True`, and `int("")` raises `ValueError` — i.e. the literal edit would have re-introduced the exact crash class the plan exists to remove, on a different input.
- **Fix:** Added `and maybe_port` before the character-set test, with a comment saying why it is not redundant. (`present` filters blank segments today, so the path is unreachable from `_saned_hosts` — but the branch's own guard no longer depends on that being true elsewhere.)
- **Files modified:** `src/saneless/checks.py`
- **Verification:** `uv run pytest tests/test_checks.py -q` — 165 passed; the mutation table above shows the port gate is still load-bearing.
- **Committed in:** `9b5d1c0`

**2. [Rule 3 - Blocking] `_MAX_PROBE_HOSTS` referenced as a module attribute in the test, not imported**

- **Found during:** Task 2 (RED)
- **Issue:** Importing a not-yet-existing constant in the module-level `from saneless.checks import (...)` block makes the RED an `ImportError` at collection, so **no** test in `tests/test_checks.py` runs at the RED commit. That hides whether the rest of the file is still green at that commit, which is the one thing a RED commit is supposed to leave visible.
- **Fix:** The cap test reads `checks._MAX_PROBE_HOSTS`, matching how the end-to-end test reads `checks._CHECK_FAILED_MESSAGE`. RED became 2 failing tests in a fully collected file.
- **Files modified:** `tests/test_checks.py`
- **Verification:** RED run at `e7b6780`: 4 selected, 2 passed, 2 failed, no collection error.
- **Committed in:** `e7b6780`

**3. [Rule 2 - Missing critical functionality] Two further stale docstring claims corrected alongside the three the plan named**

- **Found during:** Task 3
- **Issue:** `_saned_hosts`' opening reading-paragraph still said a port segment is "all digits" and that "neither reading is applied to a segment that is all digits". After tasks 1 and 2 both clauses are false — the rule is ASCII decimal, and the segment refusal is much wider than all-digit. Leaving them would have recreated, in the same function, the anti-pattern this task exists to remove.
- **Fix:** Reworded to "ASCII decimal digits within 1..65535" and "a segment glibc would read as a number (`_segment_is_a_numeric_address_shorthand`)". Both are covered by tests already added in tasks 1 and 2.
- **Files modified:** `src/saneless/checks.py`
- **Verification:** Read the whole docstring against the body sentence by sentence; every behavioural claim now maps to a named test.
- **Committed in:** `6aa7562`

**4. [Rule 3 - Blocking] Stale worktree reset to the plan's base**

- **Found during:** Setup, before Task 1
- **Issue:** The worktree was spawned with a merge-base of `a87b3dd` against the orchestrator's stated base `2b3cced` — an older commit carrying no `.planning/` directory, so the plan file did not exist on disk. Same failure mode as round 2 recorded in `30-28-SUMMARY.md`.
- **Fix:** The HEAD assertion ran first and passed (branch `worktree-agent-aa86d14ed0ecb8425`, in the per-agent namespace, not a protected ref), which is what makes the reset safe; then `git reset --hard 2b3cced`. No `git update-ref` on any protected branch, no `git clean`, no `git stash`.
- **Files modified:** none (working tree only)
- **Verification:** `git rev-parse HEAD` → `2b3cced`; the plan file read successfully.
- **Committed in:** n/a

---

**Total deviations:** 4 auto-fixed (2 missing critical functionality, 2 blocking). No Rule 4 checkpoints, no architectural questions, no packages installed.
**Impact on plan:** Deviation 1 was required for the plan's own must-have to hold — the literal edit would have moved the crash rather than removed it. Deviation 3 is the plan's own anti-pattern applied consistently. Deviations 2 and 4 are mechanics. No scope creep: `_saned_reachable`, `_scanner_preflight`, the 2 s short-circuit and the FAIL → WARN downgrade are untouched, as the plan's out-of-scope section required.

## Threat Model Disposition

| Threat ID | Disposition | Mitigation | Pinned by |
|---|---|---|---|
| T-30-31-01 | mitigate | Port segment read as ASCII decimal only; no `ValueError` can leave `_saned_hosts`, so the Scanner row cannot become the generic-failure row and `doctor` cannot exit 2 on a healthy appliance | `TestUnicodeDigitPorts` (5 cases) and `test_a_unicode_digit_port_does_not_redden_the_whole_run` |
| T-30-31-02 | mitigate | Every numeric-address shorthand glibc resolves is refused, so no unrelated local listener on 6566 can make the configured host look reachable | `TestNumericAddressShorthand` (11 cases) |
| T-30-31-03 | mitigate | `_MAX_PROBE_HOSTS` caps the dial list, so one `POST /api/checks/refresh` cannot hold a request thread for minutes | `TestProbeHostCap` (4 cases) |
| T-30-31-04 | accept | No row added or changed renders a host, port, path, URL or exception text; the generic-failure row this plan stops producing was itself developer-authored (ASVS V7) | reviewed by inspection; `test_the_unanswered_row_names_no_host_address_or_port` still passes |
| T-30-31-SC | accept | No packages installed; `hexdigits` comes from the stdlib `string` module already imported here | `uv.lock` unchanged; `git diff` touches two source files only |

## Verification

All gates run from the worktree after the last task commit:

- `uv run pytest tests/test_checks.py -q` → 165 passed
- `uv run pytest -q` → **3090 passed**
- `uv run ruff check .` → clean
- `uv run ruff format --check .` → 63 files already formatted
- `uv run ty check` → All checks passed
- `uv run pyrefly check src tests` → 0 errors
- `uv run prek run --all-files` → every hook Passed or Skipped
- No `# type: ignore` and no `# noqa` in the diff (`git diff -- src tests | grep -c "type: ignore\|noqa"` → 0)

## Known Stubs

None. No placeholder value, empty collection or "coming soon" string was introduced.

## Threat Flags

None. No new network endpoint, auth path, file access pattern or schema change at a trust boundary; the change is strictly a narrowing of what the existing pre-probe will dial.

## Self-Check: PASSED

- Files claimed modified exist on disk: `src/saneless/checks.py`, `tests/test_checks.py`, `.planning/phases/30-appliance-layer/30-31-SUMMARY.md` — all present.
- Commits claimed exist in `git log`: `e0d08ed`, `9b5d1c0`, `e7b6780`, `be8c494`, `6aa7562` — all present on `worktree-agent-aa86d14ed0ecb8425`, rooted at the orchestrator's base `2b3cced`.
- `git diff --diff-filter=D 2b3cced HEAD` — no file deleted by any commit in this plan.
- Working tree clean after the summary commit; `STATE.md` and `ROADMAP.md` untouched (orchestrator-owned).
