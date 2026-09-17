---
phase: 30-appliance-layer
plan: 21
subsystem: testing
tags: [health-checks, sane-net, socket, getaddrinfo, ipv6, asvs-v7, tdd]

# Dependency graph
requires:
  - phase: 30-appliance-layer
    provides: "checks.py's five-row registry, the saned pre-probe, CheckState's three-state rule (D-01) and the D-02 shared-vocabulary contract"
provides:
  - "A Scanner row whose worst answer for a stale or powered-down network host is amber, so a machine with a working local scanner never goes red and `doctor` exits 0"
  - "_saned_host_setting: the probe reads SANE_NET_HOSTS before scanner.host, the same precedence _ensure_initialised uses, so the row can no longer describe a host SANE is not dialling"
  - "_looks_like_a_host_name: a narrow plausibility test that makes _saned_hosts refuse an IPv6 literal instead of inventing host/port pairs from it"
  - "A one-address-per-host probe, and a PROBE_CONNECT_SECONDS comment that states what is and is not inside the bound"
affects: [appliance-layer, health-checks, doctor-exit-codes, sane-net-deployments]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Read the value libsane reads (os.environ) rather than the value the config file holds, wherever the two can differ"
    - "A parser that cannot read its input returns nothing rather than guessing, and the caller's documented fallback absorbs it"
    - "A probe reports what it observed (this host did not answer), not what it would like to conclude (there is no scanner)"

key-files:
  created: []
  modified:
    - src/saneless/checks.py
    - tests/test_checks.py

key-decisions:
  - "CR-02 fixed by downgrading the verdict, not by removing or threading the pre-probe: the short circuit and its 2 s bound stay, but the row it returns is WARN"
  - "Rejected 'probe degrades cost only' (always enumerate): the refresher holds worker.scanner_gate across run_checks, so a ~127 s enumeration starves ScanWorker._scan_job (WR-03) -- a display defect turned into a scan-starvation defect"
  - "Rejected 'bound the enumeration on a worker thread': sane_get_devices is uninterruptible, so a thread past its deadline is still inside libsane, breaking scanner_gate's one-caller invariant and A-7's shutdown rule"
  - "Rejected the review's third option, 'gate it on the backend being net-only': ScannerSettings carries only host, nothing inspects dll.conf, so there is no data to decide it from"
  - "Accepted cost: an appliance whose only scanner is an unreachable net host now reports amber and doctor exits 0 for it (D-01 keys gates on red alone)"
  - "The probe reads SANE_NET_HOSTS first, because _ensure_initialised honours a pre-existing variable over scanner.host and logs that it is ignoring the config"
  - "An empty SANE_NET_HOSTS is not a configured host, so `or` rather than a two-argument get"
  - "Resolution is deliberately left outside the connect budget rather than moved onto a thread, for the same reason the enumeration is not threaded"

patterns-established:
  - "Refuse-to-guess parsing: no entries means no probe, which means the caller behaves exactly as it did before the probe existed"
  - "Amber for a true statement about a deployment that still works, extending the D-22 read-only-config precedent to the unanswered-host case"

requirements-completed: [APPL-01, APPL-02]

# Metrics
duration: 14min
completed: 2026-09-17
---

# Phase 30 Plan 21: Scanner Pre-Probe Verdict, Parser and Bound Summary

**The saned pre-probe stops claiming "there is no scanner" when all it observed was "the configured host did not answer": the row is amber, the parser refuses IPv6 literals instead of inventing host/port pairs, the probe dials the host `SANE_NET_HOSTS` names, and one configured host now costs one connect budget instead of one per resolved address.**

## Performance

- **Duration:** 14 min
- **Started:** 2026-09-17T03:20:16Z
- **Completed:** 2026-09-17T03:34:08Z
- **Tasks:** 3 (all TDD, 6 commits)
- **Files modified:** 2

## Accomplishments

- **CR-02 closed.** A machine with a working local scanner and a stale `scanner.host` reports `WARN`, not `FAIL`; no row is `FAIL`, so `saneless doctor` exits 0. `CheckState`'s own rule -- a healthy appliance must never go red -- now holds for this shape.
- **CR-02's second half closed.** The probe reads `SANE_NET_HOSTS` before `scanner.host`, matching `_ensure_initialised`'s precedence (`sane_backend.py:876-883`), so the row can no longer describe a host SANE is not using. An exported-but-empty variable falls back to the setting.
- **WR-01 closed.** `fe80::1`, `[fe80::1]:6566` and `::1` produce no probe at all instead of three junk dials, so the input that fed CR-02's false red is gone at the source.
- **WR-02 closed.** A name resolving to three addresses costs one construction, one `connect` and one budget. `PROBE_CONNECT_SECONDS`' comment no longer claims to be "the whole of success criterion 2 on the SANE side"; it states the real shape, including that name resolution runs before the budget and is not inside it.
- **23 new tests**, suite at 2913 passing (was 2890), no test removed, `tests/` `time.sleep` count unchanged at 17.

## Task Commits

1. **Task 1: the host parser refuses to guess** — `0f42050` (test, RED), `efa0b05` (fix, GREEN)
2. **Task 2: the probe dials what SANE dials, and its failure is amber** — `13fe181` (test, RED), `052b403` (fix, GREEN)
3. **Task 3: the probe's documented bound becomes the bound it holds** — `75a9ac1` (test, RED), `4872756` (fix, GREEN)

No REFACTOR commit was needed: each GREEN landed clean under ruff, ruff-format, ty and pyrefly.

### TDD Gate Compliance

All three tasks ran RED before GREEN, and every RED failed for the reason the plan predicted, with the values the review recorded:

| Task | RED evidence observed |
|------|------------------------|
| 1 | `_saned_hosts("fe80::1")` → `(('fe80', 1),)`; `_saned_hosts("[fe80::1]:6566")` → `(('[fe80', 6566), ('1]', 6566), ('6566', 6566))`; `_saned_hosts("::1")` → `(('1', 6566),)` |
| 2 | Scanner row `FAIL` `'Not reachable.'` with a working local device present; `worst_state([row])` → `FAIL` |
| 3 | `len(recorder.constructions)` → `3` (expected 1); `recorder.timeouts` → `[0.5, 0.5, 0.5]` (expected `[0.5]`) |

**Before → after for the CR-02 reproducing scenario** (a closed loopback port as `scanner.host`, with a backend reporting a working local device):

| | Scanner row state | Scanner row message |
|---|---|---|
| Parent commit `3199941` | `FAIL` | `Not reachable.` |
| This plan `4872756` | `WARN` | `The configured scanner host is not answering, so the scanner could not be checked.` |

`worst_state([scanner_row])` went from `FAIL` to `WARN`. `cli.py:1186` exits non-zero only when `worst_state(results) is CheckState.FAIL`, so this is the exit-code change.

## Files Created/Modified

- `src/saneless/checks.py`
  - `_saned_host_setting(settings)` — new; `os.environ.get("SANE_NET_HOSTS") or settings.scanner.host`
  - `_looks_like_a_host_name(segment)` — new; non-empty, ASCII letters/digits/hyphen/dot, no leading or trailing hyphen or dot
  - `_scanner_host_unanswered()` — new; the amber Scanner row, `skipped=False`, naming no host, address or port
  - `_saned_hosts` — refuses outright when the setting has more than one colon and any present segment is implausible or a blank segment sits anywhere but the first or last position
  - `_saned_reachable` — resolves once with `getaddrinfo`, dials the first result inside a `with socket.socket(...)` after applying the budget
  - `_check_scanner` — pre-probe now parses `_saned_host_setting(...)` and short-circuits to `_scanner_host_unanswered()`
  - `PROBE_CONNECT_SECONDS`, `_saned_reachable`, `_saned_hosts`, `_check_scanner` docstrings/comments corrected
  - `import os`, `from string import ascii_letters, digits`, `_HOST_NAME_CHARACTERS`
- `tests/test_checks.py`
  - `TestSanedHostParsing` +10 cases (3 refusals, 5 unchanged readings, 2 IPv4 literals)
  - `TestScannerCheck` +9 cases (3 env-precedence, amber shape, health-gate, 3-way parametrised ASVS guard, 2 enumeration-still-red, unparseable-host-reaches-backend), and 1 existing case updated FAIL→WARN
  - `TestSanedProbeBound` +4 cases, with `_ProbeRecorder` / `_RecordingSocket` doubles
  - `_recording_dialler`, `_healthy_settings`, and a module-wide autouse `_no_ambient_sane_net_hosts` fixture

## Decisions Made

### CR-02: the fix approach, and the rejections, restated for a future phase

The plan's `<decision_record>` is reproduced here in full substance, because a later phase that wants the red row back will need it and should not have to re-derive it.

**Chosen: downgrade the verdict, keep the bound.** The pre-probe keeps its short circuit, so the 2 s bound and phase success criterion 2 are untouched and no thread, deadline or shutdown machinery is added -- but it stops claiming a fact it cannot know. `SANE_NET_HOSTS` **adds** net devices; it does not replace local backend enumeration (`sane_backend.py:876` only sets the variable, the dll backend still loads every local backend), so "every configured sane-net host refuses TCP" never implied "there is no scanner". What the probe observed is *"the configured scanner host did not answer"*, which is precisely D-01's definition of amber: a true statement about a deployment that still works. It is the same shape as D-22's read-only-config row.

**Rejected: "probe degrades cost only"** — always call `get_devices()` and drop the short circuit. Smallest diff, correct verdict, but the regression is not page latency (that is owned by D-04's cache and background refresher, not by the probe). The refresher tick holds `worker.scanner_gate` across `run_checks`, so a ~127 s enumeration parks `ScanWorker._scan_job` behind it with the job row already written `SCANNING` (WR-03) -- a display defect turned into a scan-starvation defect. It would also make `saneless doctor` take two minutes on exactly the appliance whose operator is running it to find out what is wrong.

**Rejected: "bound the enumeration"** — run `get_devices()` on a worker thread with a deadline. The better answer in a codebase where the call can be abandoned; here it cannot. `sane_get_devices` is inside libsane and nothing can interrupt it, so a helper thread that misses its deadline is *still inside SANE* after the caller has returned and released the gate. That breaks the one invariant `scanner_gate` exists to hold -- one caller inside libsane at a time (Pitfall 2) -- and defeats Amendment A-7's shutdown rule, because `scanner.close()` then runs `sane_exit()` with a SANE call outstanding, which `sane_backend.shutdown()` documents as a segfault risk. Making it safe means the helper owns the gate itself *and* is visible to `refresher.stop()`, i.e. a second thread lifecycle. The net portion cannot be skipped selectively instead: the sane-net backend reads `SANE_NET_HOSTS` at `sane_init` and never again (`sane_backend.py:834-837`), so there is no per-call way to enumerate locals only. **A future phase that wants the red back needs this thread, and should read this paragraph before building it.**

**Rejected: the review's third suggestion, "gate it on the backend actually being net-only."** Not buildable with what saneless models today. `ScannerSettings` carries only `host: str`; nothing in the tree inspects `/etc/sane.d/dll.conf` or otherwise knows whether local backends are enabled. Implementing it would mean new host-introspection plumbing, larger than the defect warrants, and would itself need a fallback for the case where the introspection fails.

**The cost, recorded deliberately.** An appliance whose *only* scanner is an unreachable net host now reports amber, so `doctor` exits 0 for it. Accepted: D-01 already states a scripted gate keys on red only, and the alternative is a red row on a machine that scans perfectly -- the failure mode `CheckState`'s own docstring forbids outright. The row remains visible, names the scanner host as the thing that did not answer, and carries the same next step, so a human loses nothing.

### Parser refusal shape

The plan's stated guard -- "more than one `:` and any *present* segment implausible" -- does not by itself reject `fe80::1`, whose present segments (`fe80`, `1`) are both plausible; the signal is the *blank* segment the split produces, which the plan's own predicate description calls out ("deliberately rejecting `[`, `]` and the empty segments an IPv6 literal produces"). Checking every raw segment for emptiness would however have broken the existing, and explicitly-preserved, `" : host-a : "` case. The implemented rule keeps both: with more than one colon, refuse when any present segment is implausible **or** a blank segment sits anywhere but the first or last position. Stray colons at the edges stay tolerated; `fe80::1`, `::1` and `[fe80::1]:6566` are all refused.

### The OverflowError note was already inaccurate

While rewriting `_saned_reachable`, the `OverflowError` claim in `_saned_hosts`' docstring was checked against the platform rather than carried forward. `socket.create_connection(("127.0.0.1", 99999))` does **not** raise `OverflowError` -- `getaddrinfo` truncates the port modulo 65536 and the dial is merely refused on port 34463. `OverflowError` is raised only when a raw out-of-range port reaches `connect` directly. Both failure modes are wrong answers and the range guard prevents both, so the guard is unweakened, but the docstring (and the matching test docstring) now say the true thing.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 2 - Missing Critical] Module-wide autouse fixture isolating the suite from an ambient `SANE_NET_HOSTS`**
- **Found during:** Task 2 (the probe dials what SANE dials)
- **Issue:** Making the scanner check read the environment before `scanner.host` is correct, and it also makes every pre-probe assertion in `tests/test_checks.py` depend on the machine the suite runs on. A developer or CI image exporting `SANE_NET_HOSTS` would silently redirect every probe and change the verdicts, with no test naming the cause.
- **Fix:** Added an autouse `_no_ambient_sane_net_hosts` fixture that `delenv`s the variable for every test in the file; the cases that want it set say so with `monkeypatch.setenv` in their own body.
- **Files modified:** `tests/test_checks.py`
- **Verification:** `uv run pytest tests/test_checks.py -q` passes (116) both with and without the variable exported.
- **Committed in:** `13fe181` (Task 2 RED commit)

**2. [Rule 1 - Bug] Corrected two docstrings that asserted `OverflowError` behaviour the platform does not have**
- **Found during:** Task 3 (the probe's documented bound)
- **Issue:** Task 3 removes `socket.create_connection`, and its acceptance criteria require no reference to it survive in `src/`. Checking the claim before rewording it showed it was never true on this platform: `create_connection` with port 99999 raises `ConnectionRefusedError`, not `OverflowError`, because `getaddrinfo` truncates the port modulo 65536 first. The matching test docstring in `test_an_out_of_range_port_is_not_a_port` repeated the same false claim.
- **Fix:** Both docstrings now state both real failure modes -- `connect` raises `OverflowError` (not an `OSError`, so not caught), `getaddrinfo` truncates modulo 65536 so `host-a:99999` would quietly dial port 34463 -- and note that the range guard prevents both. The guard itself is unchanged and its test still passes.
- **Files modified:** `src/saneless/checks.py`, `tests/test_checks.py`
- **Verification:** Behaviour verified directly against CPython 3.14.2 before rewording; `uv run pytest tests/test_checks.py -q -k SanedHostParsing` passes (18).
- **Committed in:** `4872756` (Task 3 GREEN commit)

---

**Total deviations:** 2 auto-fixed (1 missing critical, 1 bug)
**Impact on plan:** Both are correctness work inside the plan's own files and scope. No scope creep; no new dependency; `pyproject.toml` untouched.

## Issues Encountered

**One acceptance criterion in the plan is arithmetically wrong.** Task 2 asks that `grep -v '^ *#' src/saneless/checks.py | grep -c "_scanner_unreachable()"` be `4`, described as "one definition plus the three post-enumeration returns". The parent commit had one definition and **three** returns totalling 4, but one of those three *was* the short circuit this task moves. After the move there are one definition and two post-enumeration returns (the raising backend, and the backend reporting no devices) -- the observed count is **3**. The criterion's intent, "confirming the short-circuit no longer uses it", is satisfied: `_scanner_unreachable` now appears only at `checks.py:659` (definition), `:796` (enumeration raised) and `:798` (enumeration found nothing). Recorded here rather than silently satisfied, so the verifier does not read 3-vs-4 as a miss.

**Worktree base correction.** The worktree was checked out at `ed2d620`, a commit whose tree predates `.planning/` entirely, so the plan file was not present. The mandated base assertion caught it and `git reset --hard 31999416c2b044803e409e7227ff1979facca735` corrected it before any work began. No content was lost (the tree was clean and had no untracked files).

## Verification

All of the plan's `<verification>` block, run at `4872756`:

| Check | Result |
|-------|--------|
| `uv run pytest -q` | 2913 passed (parent: 2890 -- 23 added, none removed) |
| `uv run ruff check .` | No issues found |
| `uv run ruff format --check .` | 63 files already formatted |
| `uv run ty check` | All checks passed |
| `uv run pyrefly check src tests` | 0 errors |
| `grep -nE "^(from\|import) saneless\.(web\|cli)" src/saneless/checks.py` | no matches -- the leaf import rule survives `import os` |
| `time.sleep` count across `tests/` | 17, unchanged |

Per-task acceptance criteria:

| Criterion | Observed |
|-----------|----------|
| `h('fe80::1')==() and h('[fe80::1]:6566')==() and h('::1')==()` | exits 0 |
| `h('a:b')==(('a',6566),('b',6566)) and h('scanner.local:6566')==(('scanner.local',6566),)` | exits 0 |
| `grep -c "def _looks_like_a_host_name("` | 1 |
| `pytest -k SanedHostParsing` | 18 selected, all pass (criterion: ≥12) |
| `grep -c "def _scanner_host_unanswered("` | 1 |
| `grep -c "def _saned_host_setting("` | 1 |
| `grep -c "SANE_NET_HOSTS" src/saneless/checks.py` | 5 (criterion: ≥1) |
| `_scanner_host_unanswered()` is WARN, not skipped, has a next step, no `:` in message | exits 0 |
| `grep -c "getaddrinfo"` / `grep -c "create_connection"` | 4 / 0 |
| `grep -c "settimeout" src/saneless/checks.py` | 1 |
| `grep -c "the whole of success criterion 2"` | 0 (removed from comment as well as code) |
| `pytest -k SanedProbeBound` | 4 selected, all pass (criterion: ≥4) |

Success criteria:

- A healthy appliance with a stale `scanner.host` reports amber and `doctor` exits 0 — **met** (`test_an_unanswered_host_is_amber_and_says_what_to_do`, `test_an_unanswered_host_does_not_turn_a_health_gate_red`; `cli.py:1186` exits non-zero on `FAIL` alone)
- The probe dials the host named by `SANE_NET_HOSTS` when that is set — **met** (`test_the_environment_variable_wins_over_the_configured_host`, plus the unset and empty-string cases)
- `fe80::1` and `[fe80::1]:6566` produce no probe rather than junk dials — **met** (three refusal cases, plus `test_an_unparseable_host_still_reaches_the_backend` showing the fallback path)
- One configured host costs one connect timeout, and the constant's comment says what is and is not inside the bound — **met** (`TestSanedProbeBound`, and the rewritten `PROBE_CONNECT_SECONDS` comment)

No stubs, no placeholders, no deferred items.

## Threat Model Outcomes

| Threat ID | Disposition | Outcome |
|-----------|-------------|---------|
| T-30-21-01 | mitigate | `_scanner_host_unanswered`'s message and next step are developer constants; `test_the_unanswered_row_names_no_host_address_or_port` asserts over three env/setting shapes that no `:`, no configured name and no port appears in either string |
| T-30-21-02 | mitigate | The probe reads the same source, in the same precedence, `_ensure_initialised` reads. No new trust in the variable -- it is already the value libsane uses |
| T-30-21-03 | mitigate | One resolved address per host removes the `budget x addresses` multiplier; the refusing parser removes the three-junk-dials amplification. Residual: resolution stays unbounded, now stated in the constant's comment rather than claimed away |
| T-30-21-04 | accept | `doctor` exits 0 for an appliance whose only scanner is an unreachable net host. Deliberate; see Decisions Made |
| T-30-21-SC | n/a | No package installed; `os`, `socket` and `string` are stdlib and `pyproject.toml` is untouched |

No new security-relevant surface beyond the register: no endpoint, no auth path, no file access and no schema change.

## User Setup Required

None — no external service configuration required.

## Next Phase Readiness

- CR-02, WR-01 and WR-02 are closed; requirements APPL-01 and APPL-02 are satisfied for this plan's slice.
- Nothing is blocked. The one open item this plan deliberately leaves is the bounded-enumeration thread that would let the Scanner row be red again for a net-only appliance; the reasoning against building it now, and what building it would require, is under "Decisions Made".
- `tests/test_doctor.py` was not touched, per the plan's wave note that plan 30-20 owns it. A `CliRunner` exit-code case for the amber-scanner shape would sit naturally there if 30-20 wants it; the behaviour it would assert is already pinned here via `worst_state`.

## Self-Check: PASSED

- `src/saneless/checks.py` — present
- `tests/test_checks.py` — present
- `.planning/phases/30-appliance-layer/30-21-SUMMARY.md` — present
- Commits `0f42050`, `efa0b05`, `13fe181`, `052b403`, `75a9ac1`, `4872756`, `fef042a` — all present in `git log 3199941..HEAD`

---
*Phase: 30-appliance-layer*
*Completed: 2026-09-17*
