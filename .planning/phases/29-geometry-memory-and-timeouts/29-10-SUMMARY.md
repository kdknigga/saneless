---
phase: 29-geometry-memory-and-timeouts
plan: 10
subsystem: scanner-lifecycle
tags: [HARD-05, N-04, D-17, D-18, D-19, sane, lifecycle, cli, web]
requires:
  - "29-07: the _WEDGE record and _wedged_by(), which shutdown() consults"
  - "29-06: ScannerBackend.close(), the declared no-op this overrides"
  - "tests/fake_sane.py: FakeSaneModule.init_call_count / exit_call_count / exit_while_blocked"
provides:
  - "sane_backend.shutdown(): idempotent, wedge-aware, never-raising process shutdown"
  - "sane_backend._ensure_initialised(host): the thread-safe, once-per-process init guard"
  - "SaneBackend.close(): the abstraction an entry point shuts SANE down through"
  - "a behavioural proof that no request path reaches sane.init() or sane.exit()"
affects:
  - "src/saneless/cli.py: scan, devices and auto-profiles now close their backend"
  - "src/saneless/web/app.py: the lifespan closes the scanner after a confirmed worker stop"
  - "tests/test_scanner.py: an autouse fixture re-arms the init guard around every test"
tech-stack:
  added: []
  patterns:
    - "module-level mutable container instead of a global rebind, to stay clear of ruff PLW0603"
    - "click ctx.call_on_close for teardown that survives success, ctx.exit() and a raise alike"
    - "route-table-derived coverage: a route neither driven nor skipped-with-a-reason fails the test"
key-files:
  created:
    - ".planning/phases/29-geometry-memory-and-timeouts/deferred-items.md"
  modified:
    - "src/saneless/scanner/sane_backend.py"
    - "src/saneless/cli.py"
    - "src/saneless/web/app.py"
    - "tests/test_scanner.py"
    - "tests/test_cli.py"
    - "tests/test_app_lifespan.py"
    - ".planning/phases/29-geometry-memory-and-timeouts/29-VALIDATION.md"
decisions:
  - "D-17 implemented as _Init beside _Wedge, so the module has one home for process-global state"
  - "shutdown() re-arms the guard even when sane.exit() raised, rather than stranding the process"
  - "the D-19 proof skips /openapi.json, naming a pre-existing 500 as the reason"
  - "the five duck-typed CLI scanner stubs now subclass StubScannerBackend"
metrics:
  duration: "~70 min"
  completed: "2026-09-15"
  tasks: 3
  commits: 5
  tests_added: 22
---

# Phase 29 Plan 10: SANE Lifecycle (HARD-05) Summary

`sane.init()` now runs once per process behind a thread-safe module-level guard, a second
scanner host is reported instead of silently ignored, and `sane.exit()` runs explicitly at
every entry point's shutdown — never from an interpreter-exit hook, and never while a read is
still inside SANE.

## What Changed

### Task 1 — the init guard (`bbf461b` test, `c1d3d6c` feat)

`SaneBackend.__init__` no longer calls `sane.init()`. It calls `_ensure_initialised(host)`,
which takes `_INIT_LOCK` and, on the first call in the process, sets `SANE_NET_HOSTS` (Phase
14's external-env-wins rule unchanged), calls `sane.init()` and records the flag, the host and
the version in `_INIT` — a module-level `@dataclass` instance that is **mutated, never
rebound**, so no new `global` statement and therefore no new `PLW0603` suppression. It sits
directly beside `_WEDGE`, so the module has one place for process-global state rather than two.

On a later call the guard returns at once, except that a non-empty host differing from the
recorded one logs a WARNING naming **both** hosts and the reason — SANE reads `SANE_NET_HOSTS`
only at the first initialisation, so the operator's second host does nothing at all (N-04).
The existing `ScanError` translation, its `Could not initialise SANE:` message and its D-08
comment are carried over verbatim; a failed init records nothing, so a retry runs.

`SaneBackend` stays freely constructible: no singleton, no factory. `shutdown()` re-arms the
guard, so a later construction initialises again.

Both doc-truth sentences were rewritten: the module docstring's
`sane.init() called exactly once at construction time` and the class docstring's
`Calls sane.init() exactly once at construction` now state the process-level truth.

### Task 2 — explicit shutdown at every entry point (`05ba925` test, `13e9c65` feat)

`shutdown()` is module-level, idempotent and never raises. Under `_INIT_LOCK` it returns early
(at DEBUG) when SANE was never initialised, and returns early (at WARNING) when
`_read_outstanding()` reports that a reader is still inside SANE — because `sane_exit` closes
every open handle by specification and `PySane_exit` runs holding the GIL, which is
`_open_device`'s close-while-reading refusal applied to every handle at once. Otherwise it
calls `sane.exit()` inside a `try`, logs any failure with `exc_info=True`, and re-arms the
guard either way, so a failed exit cannot strand the process.

`SaneBackend.close()` overrides `ScannerBackend`'s declared no-op and delegates.

- `cli.py`: `ctx.call_on_close(scanner.close)` on the line after `SaneBackend(...)` in `scan`
  (:543), `devices` (:640) and `auto-profiles` (:861). click 8.3.1 runs those callbacks when
  the subcommand's `Context` leaves its `with` block — on success, on `ctx.exit()` and while an
  exception propagates — which is before `_GuardedGroup.invoke`'s handlers, so SANE is already
  down when the error line is printed. `serve` gained a comment saying why it deliberately has
  none: its backend outlives the command body.
- `web/app.py`: `scanner.close()` after `job_store.close()`, in the branch that runs only once
  `worker.stop()` returned True. The worker-did-not-stop branch still `return`s, and its
  warning now names the scanner alongside the store and the client. The lifespan docstring's
  closing line names all three.

### Task 3 — the D-19 reachability proof (`68ab16c`)

`test_sane_lifecycle_across_startup_every_route_and_shutdown` builds the real app over a real
`SaneBackend` backed by `FakeSaneModule`, enters `TestClient`, drives every route derived from
`app.routes`, and exits. It asserts `init_call_count == 1` **after each route** (not once at
the end, which could not say which handler moved it), `exit_call_count == 0` throughout,
`exit_call_count == 1` after shutdown, and `exit_while_blocked is False`.

Two extra assertions keep it from passing vacuously: any path in the app's route table that is
neither in `_ROUTE_CALLS` nor in `_ROUTE_SKIPS` fails the test by name, so a route added later
is covered the day it lands; and the submitted scan is waited to a terminal state and the fake
device asserted to have been opened, so the counters cannot be satisfied by a scan that never
started. Removing `scanner.close()` from the lifespan was verified to turn this test red.

## Routes the D-19 proof skipped, and why

Both skips are recorded in `_ROUTE_SKIPS` in `tests/test_app_lifespan.py` with their reason in
the test file itself, so the coverage is auditable rather than implicit.

| Path | Reason |
|------|--------|
| `/static` | A `StaticFiles` **mount**, not an endpoint: Starlette serves bytes off disk and no saneless code runs. |
| `/openapi.json` | FastAPI cannot generate this app's schema — the route handlers annotate returns as `Response` under postponed evaluation and pydantic raises `PydanticUserError` rather than resolving the forward reference, so the request 500s. It runs no saneless handler, opens no scanner, and the failure reproduces at this plan's base commit. Recorded in `deferred-items.md`. |

Everything else is driven: `/docs`, `/docs/oauth2-redirect`, `/redoc`, `/`, `/health`,
`/api/paperless/test`, `/api/scan` (a real form submission, carried to a terminal state),
`/api/jobs/current/status`, `/api/tags`, `/api/correspondents`, `/api/cache/invalidate`,
`/api/jobs/history`, `/api/flip/continue` and `/api/flip/abort`.

## Deviations from Plan

**1. [Rule 3 — blocking] Five duck-typed CLI scanner stubs had no `close()`**

- **Found during:** Task 2, the moment `ctx.call_on_close(scanner.close)` landed.
- **Issue:** `_RangeScanner`, two local `MockSaneBackend` classes, `_AutoScanner` and
  `LongNameScanner` in `tests/test_cli.py` duck-typed the backend instead of subclassing it,
  so 14 pre-existing tests died with `AttributeError: ... has no attribute 'close'`.
- **Fix:** Each now subclasses `tests.conftest.StubScannerBackend` and inherits the base
  `close()`, which is precisely what that class's own docstring argues for ("every stub that
  subclassed was caught by the type checkers when its contract changed"). Their
  `get_capabilities(self, _device_id)` parameter was renamed to `device_id` to match the
  override.
- **Files modified:** `tests/test_cli.py`
- **Commit:** `13e9c65`

**2. [Lesson 2] `grep -c 'atexit' src/saneless/scanner/sane_backend.py` cannot be 0**

- **Found during:** Task 2.
- **Issue:** The task's `<action>` mandates prose rejecting `atexit`, and its
  `<acceptance_criteria>` greps for the token's absence — the seventh instance of this clash.
  Worse, the token is *already* in the file at `:1226`, in 29-07's essential explanation of why
  a `concurrent.futures` pool (whose `_python_exit` hook is registered with
  `threading._register_atexit`) made a stuck reader unsurvivable. Deleting that would vandalise
  prior-wave rationale; keeping it makes the grep non-zero whatever this plan writes.
- **Resolution:** `<action>` prose is normative, so the rationale is kept but worded without
  the bare token — `shutdown()`'s docstring says "never from an interpreter-exit hook". The
  intent is then proven *directly and more strongly than a grep could*:
  `test_no_entry_point_installs_an_interpreter_exit_hook` parses each of the three entry-point
  modules with `ast` and asserts none imports `atexit` and none calls anything whose name
  contains `register`. `cli.py` and `app.py` do return 0 for the literal grep.
- **Files modified:** `src/saneless/scanner/sane_backend.py`, `tests/test_scanner.py`

**3. [Lesson 2] `grep -c 'def close(self) -> None'` returns 2, not 1**

- **Found during:** Task 2 verification.
- **Issue:** The second match is the pre-existing `SaneDevice` **Protocol**'s
  `def close(self) -> None: ...` at `:1862`, which declares the python-sane *handle* interface
  and has nothing to do with the backend. The criterion's intent — `SaneBackend` defines a
  `close()` override — is met, and `test_close_overrides_the_base_no_op` asserts it directly
  rather than by counting lines.
- **No code change made.** Recorded so a verifier reading the criterion literally is not
  surprised.

**4. [Plan clarification] `scan`'s early `ctx.exit(ExitCode.CONFIG)` precedes the backend**

- **Found during:** Task 2 test design.
- **Issue:** The task's `<behavior>` asks for the close to be proven "including the early
  `ctx.exit(ExitCode.CONFIG)` path in `scan`". Both such paths in `scan` (`:524` unknown
  profile, `:541` manual duplex without a TTY) run *before* `SaneBackend(...)` at `:543`, so
  there is no backend in existence to close.
- **Resolution:** The underlying claim — a `ctx.exit()` taken mid-command does not skip the
  close — is proven where it genuinely applies:
  `test_auto_profiles_closes_the_backend_on_the_early_config_exit` drives `auto-profiles`'
  unwritable-config `ctx.exit(ExitCode.CONFIG)` at `:889`, which *is* after its construction.
  `scan` is covered on its success path and on a raising-pipeline path.

**5. [Task boundary] `shutdown()` was implemented in Task 1, not Task 2**

Task 1's `<behavior>` requires "after `shutdown()` has run, a subsequent `SaneBackend()`
construction calls `sane.init()` again", which cannot be tested without `shutdown()` existing.
It was therefore written whole in Task 1's GREEN commit (`c1d3d6c`), including the wedge skip,
and Task 2 added its behavioural tests, `SaneBackend.close()` and the entry-point wiring. Both
tasks' acceptance greps are satisfied.

**6. [Plan instruction vs. files_modified] `29-VALIDATION.md` and `deferred-items.md`**

Task 3's `<action>` and `<acceptance_criteria>` both require updating
`29-VALIDATION.md`'s SC5 rows, which the plan's `files_modified` list does not name. The action
is normative, so the three SC5 rows were updated (they already named 29-10 T1/T2/T3; the status
columns moved from `❌ W0 | ⬜ pending` to `✅ exists | ✅ passing`). `deferred-items.md` was
created per the executor's own scope-boundary rule for the `/openapi.json` finding.

**7. [Test hygiene] One over-claiming scanner test deleted**

`test_sane_backend_init_calls_sane_init_exactly_once` claimed "exactly once" while constructing
a single backend — it could never have caught a per-instance init. CONTEXT.md allows deleting
such a test when this plan's real one replaces it;
`test_init_once_for_two_backends_in_one_process` does.

## Authentication Gates

None.

## Verification

All run from the worktree at `68ab16c`:

| Check | Result |
|-------|--------|
| `uv run pytest -m "not browser and not sane_hardware"` | **2032 passed**, 74 deselected (base: 2004) |
| `uv run pytest -m browser` | 68 passed |
| `uv run pytest -m sane_hardware` | 6 passed |
| `uv run pytest tests/test_scanner.py -k init_once -x` | 6 passed |
| `uv run pytest tests/test_cli.py -k closes_the_backend -x` | 6 passed |
| `uv run pytest tests/test_app_lifespan.py -k sane_lifecycle -x` | 1 passed |
| `uv run ruff check .` / `ruff format --check .` | clean |
| `uv run ty check` / `uv run pyrefly check src tests` | clean, 0 errors |
| `uv run prek run --all-files` | exit 0 |
| `uv run prek run --stage pre-push --all-files` | exit 0 |

Acceptance greps: `PLW0603` 1 (unchanged), `^\s*global ` 1 (unchanged), `threading.Lock()` 2,
`exactly once at construction` 0, `Could not initialise SANE` 1, `call_on_close` 3,
`scanner.close()` 1 (after `job_store.close()`), `def shutdown` 1, `atexit` 0 in `cli.py` and
`app.py`, `init_call_count` 4 / `exit_call_count` 4 / `.routes` 2 in `test_app_lifespan.py`.
The two literal-grep criteria that cannot hold are explained under Deviations 2 and 3.

No suppressions were added: no new `# noqa`, no `# type: ignore`, no disabled rules, no
`--no-verify`, no `SKIP=`. Every commit ran the hooks.

## Known Stubs

None.

## For the downstream waves / 29-11

- `shutdown()` and `SaneBackend.close()` are the names to reference; `shutdown` is in
  `__all__`.
- The module docstring line 7 and the `SaneBackend` class docstring were rewritten by this
  plan — do not re-reword them. The `docs/` tree is untouched and remains 29-11's.
- `deferred-items.md` now exists in the phase directory with the `/openapi.json` finding. It is
  a finding, not a doc-truth correction, so it is not 29-11's to fix either; it wants its own
  decision about whether saneless should serve generated API docs at all.
- `tests/test_scanner.py` now has a module-wide autouse `sane_init_guard` fixture that calls
  `shutdown()` before and after every test in the file. Any new scanner test asserting
  `init_call_count` relies on it; any new test module that builds a real `SaneBackend` over a
  fake should do the same, or it will inherit whichever guard state the previous module left.

## Self-Check: PASSED
