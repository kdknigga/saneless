# Phase 25: Manual Duplex - Pattern Map

**Mapped:** 2026-09-13
**Files analyzed:** 21 source/test files + 9 documentation files
**Analogs found:** 18 exact / 3 role-match / 3 no-analog

> **How to read this file.** Phase 25 creates almost nothing genuinely new. Its value is in
> naming the **existing in-tree analog** for each change so the executor copies house style
> instead of inventing it. Every excerpt below was read from the tree this session; line numbers
> are current as of `ea0f534`.
>
> **Three things that need no analog because they need no edit:**
> `tests/test_web_state_rendering.py` (all six `list(JobState)` parametrisations build their
> expectations from `state_label` / `progress_label` / `ACTIVE_STATES` / `BUSY_STATES`, so
> `SCANNING_REVERSE` flows through automatically), `tests/fake_sane.py` (its `report_sources`
> seam already exists and its `_DEFAULT_SOURCES` already lacks a plain `"ADF"`), and
> `src/saneless/web/templates/partials/status.html` (the busy branch absorbs the ninth state the
> moment it joins `ACTIVE_STATES` — adding a branch would *break* D-16).

---

## File Classification

### Production

| Modified File | Role | Data Flow | Closest Analog | Match Quality |
|---|---|---|---|---|
| `src/saneless/config.py` (`duplex` field + before-validator) | model / config | transform (validation) | `ProfileConfig.auto_source_mode` (`config.py:71`) + `Settings.validate_default_profile` (`:177-187`) | exact |
| `src/saneless/config.py` (`flip_timeout_seconds`) | config | — | `OutputConfig.paperless_task_timeout` (`config.py:97`) | exact |
| `src/saneless/config.py` (module logger + deprecation warning) | config | event (log) | `worker.py:40` + `auto_profiles.py:508-512` | exact |
| `src/saneless/vocabulary.py` (`JobState.SCANNING_REVERSE`) | vocabulary | transform (total lookup) | `JobState.FALLBACK`, Phase 23 — every touch site traced below | exact |
| `src/saneless/vocabulary.py` (`FlipOutcome`) | vocabulary | transform (total lookup) | `ScanOutcome` (`:62-74`) + `job_state_for` (`:235-268`) | exact |
| `src/saneless/pipeline.py` (`FlipCoordinator` ABC) | service seam | event-driven | `ScannerBackend` ABC (`scanner/base.py:220-265`) | exact |
| `src/saneless/pipeline.py` (`PipelineRequest` field swap) | model | request-response | `PipelineRequest` itself (`:109-134`) | exact |
| `src/saneless/pipeline.py` (`_scan_manual_duplex` rewrite) | service | request-response | `_handle_duplex_mismatch`'s derive-`notify` trick (`:574`) | exact |
| `src/saneless/pipeline.py` (D-07 `match` dispatch) | service | transform | `job_state_for` / `SourceKind.uses_feeder` | exact |
| `src/saneless/pipeline.py` (refusal guard) | service | request-response | `run_pipeline`'s own profile guard (`:833-835`) | exact |
| `src/saneless/pipeline.py` (`PipelineEvent.job_state` narrowing) | vocabulary projection | transform | the property itself (`:54-93`) | exact |
| `src/saneless/worker.py` (`WorkerFlipCoordinator`) | worker / provider | event-driven | `continue_flip`/`abort_flip` (`:97-109`) — the *defect* being replaced | role-match |
| `src/saneless/worker.py` (delete `_transition_event`) | worker | event-driven | *(deletion — touch sites mapped, no pattern to copy)* | n/a |
| `src/saneless/cli.py` (`ClickFlipCoordinator` + exit-2 refusal) | controller (CLI) | request-response (interactive) | `scan`'s exit-2 unknown-profile guard (`:110-112`) | role-match |
| `src/saneless/cli.py` (`_stdin_is_interactive` seam) | utility | — | `_STATUS_COL_WIDTH` (`:43-47`) — module-level derived seam with a why-comment | partial |
| `src/saneless/web/routes.py` (shared job lookup, **4** call sites) | route helper | request-response | `_get_cached_or_fetch` (`routes.py:27-62`) | exact |
| `src/saneless/web/routes.py` (`continue_flip` / `abort_flip`) | controller (route) | request-response | `current_job_status` (`:179-198`) | exact |
| `src/saneless/scanner/base.py` (`ScanSettings` feeder signal) | model | — | `ScanSettings.auto_source_mode` (`:177`) | exact |
| `src/saneless/scanner/sane_backend.py` (`_resolve_source` feeder branch) | adapter | transform | `_resolve_source` itself (`:792-846`) + `classify_source` (`base.py:55-113`) | exact |
| `src/saneless/auto_profiles.py` (`duplex = "hardware"`) | generator | file-I/O | `auto_source_mode` write-when-non-default (`:523-524`) | exact |

### Tests

| Modified/New Test | Role | Data Flow | Closest Analog | Match Quality |
|---|---|---|---|---|
| `tests/conftest.py` — always-continue `FlipCoordinator` stub | test fixture | — | `_StubScanner` (`test_web_state_rendering.py:60-86`) | exact |
| `tests/test_config.py` — translation, no-overwrite, caplog warning | unit | transform | `TestDefaultProfile` (`:60-81`) + `test_collision_warning_names_both_sources` (`test_auto_profiles.py:613-627`) | exact |
| `tests/test_vocabulary.py` — 4 hand-written rosters | unit, parametrised | — | the rosters themselves (`:46-55`, `:123-136`, `:162-177`, `:190-202`) | exact |
| `tests/test_pipeline.py` — coordinator migration (~10 sites) | unit | request-response | `TestIsManualDuplex` (`:237-259`), integration case (`:797-852`) | exact |
| `tests/test_worker.py` — delete flip-timing class, add blocking stub | unit | event-driven | `TestWorkerFlipTiming` (`:726-818`) to delete; `wait_for_state` usage (`:1416-1436`) to copy | exact |
| `tests/test_cli.py` — prompt + non-TTY refusal | unit (CliRunner) | request-response | `_patch_cli` (`:68-171`) + `test_scan_status_output` (`:217-223`) | exact |
| `tests/test_outcomes_e2e.py` — `_Case.flip_timeout` + flip-timeout case | e2e, parametrised | request-response | `_Case` + `_TIMEOUT_BUDGET` (`:104-115`, `:296-402`) | exact |
| `tests/test_auto_profiles.py` — 3-way round trip | unit, parametrised | file-I/O | `test_a_feeder_only_config_round_trips_through_load_settings` (`:548-566`) | exact |
| `tests/test_web.py` — flip route behaviour | unit (TestClient) | request-response | `test_flip_continue` / `test_flip_abort` (`:244-253`) | exact |
| `tests/test_web_state_rendering.py` | — | — | **ZERO EDITS** — see §"Files That Must Not Be Touched" | n/a |
| `tests/fake_sane.py` | — | — | **ZERO EDITS** — `report_sources` already exists (`:787-801`) | n/a |

---

## Pattern Assignments

### 1. `FlipCoordinator` — new ABC, two implementations (pipeline.py, worker.py, cli.py)

**Analog:** `ScannerBackend` in `src/saneless/scanner/base.py:220-265`

**The ABC shape to copy** (`scanner/base.py:220-247`):

```python
class ScannerBackend(ABC):
    """
    Abstract base class for scanner backends.

    All scanner operations go through this interface, allowing
    different implementations (real SANE, mock for testing, etc.).
    """

    @abstractmethod
    def get_devices(self) -> list[DeviceInfo]:
        """Enumerate available scanning devices."""

    @abstractmethod
    def scan_pages(self, device_id: str, settings: ScanSettings) -> ScanBatch:
        """
        Acquire pages from scanner.
        ...
        Args:
            device_id: SANE device identifier string.
            settings: Scan settings (source, resolution, mode).

        Returns:
            A ScanBatch carrying the pages, ...
        """
```

Note the house details: `from abc import ABC, abstractmethod` (`base.py:11`), an abstract method
whose body is **only** its docstring (no `...`, no `pass`), full `Args:`/`Returns:` sections
because ruff's `D` rules are on, and the ABC exported through `__all__` (`base.py:19-27`).

**The deliberate ABC-vs-Protocol split, both halves observable.** `Protocol` is used only for
shapes this project does **not** own — `config.py:193-204`:

```python
class _SettingsFactory(Protocol):
    """
    Callable view of ``Settings`` that accepts the private ``_toml_file`` kwarg.

    ``_toml_file`` is not a declared field on ``Settings``; it is a private init
    kwarg popped out of ``init_kwargs`` by ``settings_customise_sources``. This
    protocol describes the constructor signature that mechanism really provides.
    """

    def __call__(self, *, _toml_file: Path) -> Settings:
        """Construct ``Settings`` from an explicit TOML file path."""
        ...
```

`FlipCoordinator` is a seam **this project implements**, so it is an ABC. The docstring should
say so, because D-09 says the rule is observable in the tree and a later reader will otherwise
see two conventions rather than one.

**Concrete subclassing, not duck typing — Phase 24's WR-08.** Both CLI-side stubs were moved to
subclassing for a measured reason, recorded verbatim at `tests/test_cli.py:94-102`:

```python
class MockSaneBackend(ScannerBackend):
    """
    Mock scanner backend for CLI tests.

    Subclasses the ABC so the type checkers can see the contract at
    all. This was one of the two CLI stubs that would *not* have
    failed when ScanBatch replaced the generator in this phase --
    every stub that does subclass was caught by the checkers.
    """
```

The conftest flip-coordinator stub (Wave 0) must subclass `FlipCoordinator` for the same reason.
`tests/test_web_state_rendering.py:60-86`'s `_StubScanner` is the tersest example of the form.

**The `assert` trap.** `S101` is in `select` (`pyproject.toml:91`) and is relaxed **only** for
`tests/**` (`:129-133`). A bare `assert answer is not None` inside `wait_for_flip` is a lint
failure the project forbids suppressing. Structure `_resolve()` to *return* the claimed outcome so
the type narrows without an assertion.

---

### 2. `FlipOutcome` — new total enum (vocabulary.py)

**Analog:** `ScanOutcome` (`vocabulary.py:62-74`) and `job_state_for` (`vocabulary.py:235-268`)

**The enum + its "why these members and no others" docstring** (`vocabulary.py:62-74`):

```python
class ScanOutcome(StrEnum):
    """
    How a scan attempt resolved.

    There is no ``FAILED`` member, and there is not going to be one: a failure
    raises.  The ``outcome`` column therefore stays ``NULL`` on the error path
    and ``JobState.ERROR`` carries the failure on its own.  A returned
    ``FAILED`` would record the same fact in a second column, and two columns
    about a job with exactly one fate are two columns that can disagree.
    """

    SUCCESS = "SUCCESS"
    FALLBACK = "FALLBACK"
```

**The canonical total lookup** — copy this comment paragraph almost verbatim when consuming
`FlipOutcome` (`vocabulary.py:235-268`):

```python
def job_state_for(outcome: ScanOutcome) -> JobState:
    """
    Return the terminal job state a resolved scan outcome implies.
    ...
    This is a ``match`` with ``assert_never`` and not a
    ``dict[ScanOutcome, JobState]`` on purpose.  A dict missing a member draws
    no diagnostic from either ``ty`` or ``pyrefly``; the same enum in a match
    is caught by both, at edit time, before a third outcome can fall silently
    through an ``else``.

    Args:
        outcome: The outcome the pipeline resolved to.

    Returns:
        The terminal JobState to persist for that outcome.

    Raises:
        AssertionError: If the value is not a ScanOutcome member.

    """
    match outcome:
        case ScanOutcome.SUCCESS:
            state = JobState.DONE
        case ScanOutcome.FALLBACK:
            state = JobState.FALLBACK
        case _:
            assert_never(outcome)
    return state
```

Note the assign-then-`return` shape (never `return` inside each arm) and the
`Raises: AssertionError` docstring line — both are uniform across all five lookups in the module.

**Second example of the same shape, inside a property** — this is the one `PipelineEvent.job_state`
already follows and is the model for `SourceKind.uses_feeder` too (`scanner/base.py:39-49`):

```python
    @property
    def uses_feeder(self) -> bool:
        """Whether this kind of source feeds a stack of sheets."""
        match self:
            case SourceKind.FEEDER | SourceKind.FEEDER_DUPLEX:
                feeds = True
            case SourceKind.FLATBED | SourceKind.AUTO | SourceKind.UNKNOWN:
                feeds = False
            case _:
                assert_never(self)
        return feeds
```

**Placement:** `vocabulary.py`'s module docstring (`:1-8`) forbids importing from `job.py`,
`pipeline.py`, `worker.py`, `cli.py` or `web/`. `FlipOutcome` imports nothing, so it belongs
there; add it to `__all__` (`:22-36`), which is **alphabetically sorted** (ruff `RUF022`).

---

### 3. `ProfileConfig.model_validator(mode="before")` — no validator exists on `ProfileConfig` today

**Analog:** `Settings.validate_default_profile` (`config.py:177-187`) — the nearest in-tree
validator, and the one D-03 splits the roles against.

```python
    @field_validator("profiles")
    @classmethod
    def validate_default_profile(
        cls,
        v: dict[str, ProfileConfig],
    ) -> dict[str, ProfileConfig]:
        """Ensure a default profile is always defined."""
        if "default" not in v:
            msg = "A 'default' profile must be defined in config"
            raise ValueError(msg)
        return v
```

House details to carry over: `@field_validator` / `@model_validator` stacked **above**
`@classmethod`; the parameter is named `v`; the message is bound to a local `msg` before raising
(ruff `EM101` forbids a string literal in the raise); one-line docstring for a validator.

**The field itself** — copy the `Literal` form already beside it (`config.py:71-72`):

```python
    auto_source_mode: Literal["flatbed", "adf"] = "flatbed"
    paper_size: Literal["full", "a3", "a4", "a5", "letter", "legal"] = "full"
```

`from typing import ... Literal` is already imported at `config.py:14`.

**The timeout field** — `OutputConfig` (`config.py:97-98`) already holds the two timeouts D-10
says `flip_timeout_seconds` sits beside:

```python
    paperless_task_timeout: int = 300
    paperless_cache_ttl_seconds: int = 60
```

Plain `int`, no `Field(...)`, no docstring on the attribute. Note the `int` typing is what makes
`flip_timeout_seconds = 0` the zero-cost test seam (see §7).

**`config.py` has NO module logger today** — verified by reading the whole file. Wave 0 adds one.
Copy `worker.py:40` / `routes.py:20` exactly:

```python
logger = logging.getLogger(__name__)
```

placed after `__all__` and before the first class. `caplog` then targets `logger="saneless.config"`.

**The warning call itself** must be `%`-style lazy (ruff's `G` rules are in `select`). Two in-tree
examples, both of which also show the house habit of a multi-line implicit-concat message:

`auto_profiles.py:508-512`:
```python
        logger.info(
            "Removing auto-generated profile %r: the scanner's sources no "
            "longer produce that name.",
            name,
        )
```

`pipeline.py:462-468`:
```python
        logger.warning(
            "Manual duplex passes disagree on resolution: pass A reports %s dpi, "
            "pass B reports %s dpi; assembling at %s dpi",
            front.actual_resolution,
            back.actual_resolution,
            front.actual_resolution,
        )
```

D-18 makes this message load-bearing: it is the only migration instruction that will exist
anywhere. Name the profile, the replacement key **and** its value, and the command that produces
a real `source`.

---

### 4. The ninth `JobState` — trace the `FALLBACK` precedent

**Analog:** `JobState.FALLBACK`, added in Phase 23. Every site it had to touch is the template
for `SCANNING_REVERSE`. All six production sites, read this session:

| # | Site | Current text | What `SCANNING_REVERSE` needs |
|---|---|---|---|
| 1 | `vocabulary.py:42-49` | `FALLBACK = "FALLBACK"` in the enum body | a ninth member, value == name |
| 2 | `vocabulary.py:114-122` | `ACTIVE_STATES` frozenset literal | add the member (`BUSY_STATES` at `:138` is **derived** and follows for free) |
| 3 | `vocabulary.py:182-183` | `case JobState.FALLBACK: label = "Saved to folder"` | `case JobState.SCANNING_REVERSE: label = "Scanning backs"` |
| 4 | `vocabulary.py:228-229` | `case JobState.FALLBACK: label = "Saved to folder"` | `case JobState.SCANNING_REVERSE: label = "Scanning reverse sides..."` |
| 5 | `pipeline.py:82-84` | `case PipelineEvent.SCANNING_REVERSE: state = None` + the reserving comment | the answer, and the property's `JobState \| None` narrows to `JobState` |
| 6 | `cli.py:121-133` and `worker.py:215-220` | the two `state is None` branches | both collapse; see below |

**The seam being filled** (`pipeline.py:54-93`, the comment at `:82-84`):

```python
            case PipelineEvent.SCANNING_REVERSE:
                # No JobState twin: progress prose only, nothing persisted.
                state = None
```

and its docstring paragraph at `:56-64` (`"``None`` means this event changes no persisted
state. Today that is exactly ``SCANNING_REVERSE``…"`) — **replace the paragraph with the answer**,
do not leave it beside a filled-in arm.

**Both `state is None` branches carry a comment that predicts their own deletion.** `cli.py:121-133`:

```python
    def status_callback(event: PipelineEvent) -> None:
        state = event.job_state
        if event is PipelineEvent.DONE:
            click.echo(f"Done: {title}")
        elif state is None:
            # SCANNING_REVERSE is the only event that persists no state, so it
            # is the only one with no progress_label to read.  When it gains a
            # JobState twin this branch collapses into the general one below.
            # Branching on `state is None` rather than on the member name is
            # also what lets the type checkers accept progress_label(state).
            click.echo("Scanning reverse sides...")
        else:
            click.echo(progress_label(state))
```

D-13's `"Scanning reverse sides..."` is byte-identical to line 131, so the collapse is invisible
to users. `worker.py:215-220` is the twin:

```python
            state = event.job_state
            if state is None:
                # SCANNING_REVERSE carries no persisted state, but the pass
                # boundary is a transition a flip waiter must observe.
                self._transition_event.set()
                return
```

That one dies together with `_transition_event` (§5).

**No migration.** Re-verified: `src/saneless/job.py:289` is `state TEXT NOT NULL` with no `CHECK`
constraint, and the `PRAGMA user_version` ladder at `:350-364` governs *columns*, not values.

**The four test rosters that a count guard will not catch** — excerpts to edit, all in
`tests/test_vocabulary.py`:

```python
# :46-55  -- a count guard whose own docstring explains what should break instead
    def test_job_state_has_exactly_eight_members(self) -> None:
        """
        JobState declares exactly eight lifecycle members (CTR-01).

        A count guard, not a name list: adding a member should fail the
        parametrised completeness tests below -- which force a label and a
        classification decision -- rather than a hand-written roster that only
        records what the enum happened to contain when it was written.
        """
        assert len(list(JobState)) == 8
```

```python
# :123-136 -- explicit five-member frozenset
    def test_active_states_membership(self) -> None:
        """ACTIVE_STATES is the five in-flight lifecycle states (CTR-01)."""
        assert (
            frozenset(
                {
                    JobState.PENDING,
                    JobState.SCANNING,
                    JobState.AWAITING_FLIP,
                    JobState.ASSEMBLING,
                    JobState.UPLOADING,
                }
            )
            == ACTIVE_STATES
        )
```

```python
# :162-177 -- eight-row literal-text parametrize (deliberately hand-written)
    @pytest.mark.parametrize(
        ("state", "expected"),
        [
            (JobState.PENDING, "Pending"),
            ...
            (JobState.FALLBACK, "Saved to folder"),
        ],
    )
    def test_state_label_strings(self, state: JobState, expected: str) -> None:
```

```python
# :190-202 -- five-row literal-text parametrize
            (JobState.UPLOADING, "Uploading to paperless-ngx..."),
```

The method name and docstring wording must be updated with the count (`..._exactly_nine_members`,
"the six in-flight lifecycle states"). The `list(JobState)` completeness tests at `:57`, `:110`,
`:179`, `:204` need **no** edit — that is Phase 21 D-09 working as designed.

---

### 5. Deleting the `_transition_event` protocol — every touch site

Not a pattern to copy. This is the complete map so nothing is missed; each line was read.

| File:line | Text |
|---|---|
| `worker.py:71` | `self._transition_event = threading.Event()` (constructor) |
| `worker.py:111-124` | `def wait_transition(self, timeout: float = 2.0) -> bool:` — the whole method, including its `.wait()` + `.clear()` body |
| `worker.py:189` | `self._transition_event.set()` (after the initial `SCANNING` write) |
| `worker.py:219` | `self._transition_event.set()` (inside the `state is None` branch — dies with §4) |
| `worker.py:231` | `self._transition_event.set()` (busy-state write) |
| `worker.py:236` | `self._transition_event.clear()` (AWAITING_FLIP, clear-before-write) |
| `worker.py:276` | `self._transition_event.set()` (after `finish_job`) |
| `routes.py:297` | `state.worker.wait_transition(timeout=2.0)` — **the only external reader** |
| `tests/test_worker.py:726-818` | `class TestWorkerFlipTiming` — all three tests (`:729`, `:758`, `:805`) |
| `tests/test_worker.py:1400` | `worker._transition_event.clear()` inside `clearing_pipeline` |

**`tests/test_worker.py:758` documents the defect from the inside** — its docstring
(`:765-777`) explains why the event must be clear during the flip window, i.e. it pins the
current design rather than guarding a requirement. D-14 says explicitly: do not treat these as a
safety net.

**The replacement for the four orphaned tests already exists.** `wait_for_state` at
`tests/conftest.py:194-244`, handed over by the fixture at `:247-261`. The house usage, from
`tests/test_worker.py:1416-1436`:

```python
    def test_finish_prunes_history_after_a_fallback_outcome(
        self,
        ...
        wait_for_state: Callable[..., Job],
    ) -> None:
        """The finally block still runs on the new terminal path (STOR-05)."""
        ...
            wait_for_state(store, job.id, TERMINAL_STATES)
            worker.stop()
            assert worker.current_job_id is None
```

and the version with an explicit budget plus the reason, from `tests/test_outcomes_e2e.py:597-607`:

```python
            if case.awaits_flip:
                # Observing the persisted AWAITING_FLIP first removes the race
                # against the worker creating its flip event: continue_flip()
                # is a no-op if it arrives before _process_job has made one.
                wait_for_state(store, job.id, JobState.AWAITING_FLIP, 2.0)
                worker.continue_flip()
            # A 2 s budget, far below pytest-timeout's 60 s SIGALRM.  The
            # signal method delivers to the MAIN thread whichever thread is
            # stuck, so letting this run to the global ceiling would print a
            # traceback for this wait loop rather than for the stuck worker.
            finished = wait_for_state(store, job.id, TERMINAL_STATES, 2.0)
```

The tests being deleted currently use `for _ in range(50): time.sleep(0.05)` polling loops
(`test_worker.py:747-750`, `:789-792`) — do **not** carry that forward; ROBU-03 (Phase 26) owns
converting the remaining ones, but the four this phase touches should land on `wait_for_state`.

**The worker's own coordinator construction** replaces `worker.py:191-201`, and the `finally`
block at `:289-292` (`self._flip_event = None; self._abort_event = None`) becomes one assignment.
`worker.py:240-254` is where the two `PipelineRequest` fields become one.

---

### 6. The shared "current job, else most recent" route helper — **four** copies

**Analog / source of truth:** `routes.py:186-192` inside `current_job_status`:

```python
    state = request.app.state
    job = None
    if state.worker.current_job_id:
        job = state.job_store.get_job(state.worker.current_job_id)
    if job is None:
        recent = state.job_store.list_recent(limit=1)
        job = recent[0] if recent else None
```

**All four call sites, read this session:**

| Site | Lines | Current behaviour |
|---|---|---|
| `index` | `routes.py:81-87` | full copy (current job **and** recent fallback) — variable named `current_job` |
| `current_job_status` | `routes.py:186-192` | full copy — the correct one D-17 extracts |
| `continue_flip` | `routes.py:298-300` | **partial** — current job only, no recent fallback |
| `abort_flip` | `routes.py:317-319` | **partial** — current job only, no recent fallback |

The two partial copies are M-02's defect: `worker.py:290` clears `_current_job_id` in its
`finally`, so an Abort arriving as the job ends renders `"Ready to scan."` (`status.html:29`).

**The extraction shape to copy** — `routes.py` already has exactly one private module-level
helper, and it is the form to follow (`routes.py:27-46`):

```python
def _get_cached_or_fetch(
    cache: MetadataCache,
    paperless: PaperlessClient,
    resource: str,
) -> list[dict[str, object]]:
    """
    Retrieve metadata from cache or fetch from paperless-ngx.

    Falls back to an empty list if the paperless API is unreachable,
    ensuring the UI always loads even when paperless-ngx is down.

    Args:
        cache: Metadata cache instance.
        paperless: Paperless-ngx API client.
        resource: Resource name ('tags' or 'correspondents').

    Returns:
        List of metadata dicts, or empty list on error.

    """
```

Module-level `def` (not a method), leading underscore, takes the collaborators it needs rather than
`request`, full `Args:`/`Returns:` docstring, defined above the routes. `TYPE_CHECKING` imports at
`routes.py:12-16` are where any new type-only import goes.

**RESEARCH flags `index` as widening D-17's stated scope.** Leaving it behind preserves the exact
duplication D-17 exists to remove; the plan should record the call either way.

**The route response shape is unchanged** — every handler ends with the same three-line
`TemplateResponse` (`routes.py:301-305`), and `partials/status.html` is the target for all three
flip/status routes.

---

### 7. `_scan_manual_duplex` — at the `PLR0913` ceiling

**The blocker:** `pipeline.py:668-673` has exactly five parameters and ruff's default `max-args`
is 5. `pyproject.toml` sets no `[tool.ruff.lint.pylint]` override, `PLR0913` is not in `ignore`
(`:92`), and CLAUDE.md forbids both raising the limit and `# noqa`.

**The in-tree fix, with the comment that justifies it** — `_handle_duplex_mismatch` already does
this (`pipeline.py:572-575`):

```python
    fronts, backs = passes
    # Derived rather than passed: it is exactly what the caller would hand us,
    # and the caller is already handing us the request it comes from.
    notify = request.status_callback or _noop_callback
```

`run_pipeline` itself derives it the same way at `:831`. Drop `notify`, derive it, spend the freed
slot on `timeout: float`.

**Two more precedents for the same ceiling**, if bundling is ever preferred to dropping:
`_DeliveryContext` (`pipeline.py:388-412`) and `JobResult` (`job.py:423-429`), both frozen
dataclasses whose docstrings name `PLR0913` as the reason they exist. `tests/fake_sane.py:755-758`
and `:778-779` record the same constraint on the test side.

**Do not add a parameter to `_handle_duplex_mismatch`** — it is also at five (`:524-530`).

**The wait, and the D-15 raise.** The code being replaced is `pipeline.py:704-712`:

```python
    # Signal awaiting flip
    if request.flip_event is not None:
        notify(PipelineEvent.AWAITING_FLIP)
        request.flip_event.wait()

        # Check if aborted
        if request.abort_event is not None and request.abort_event.is_set():
            msg = "Manual duplex scan cancelled by user"
            raise ScanError(msg)
```

The `msg = ...` / `raise ScanError(msg)` two-step is mandatory house style (ruff `EM101`), used
identically at `pipeline.py:341-343`, `:658-659`, `:789-790` and `sane_backend.py:840-844`.

**The D-07 dispatch** replaces `pipeline.py:871`'s `if isinstance(duplex_result, _DuplexMismatch):`
with a `match` whose `case _: assert_never(duplex_result)` makes a third variant a type-gate
failure. `assert_never` is already imported at `pipeline.py:18`.

**The refusal guard's placement** — it goes between these two existing lines (`pipeline.py:837-838`):

```python
    profile = settings.profiles[request.profile_name]
    device_id = _resolve_device(scanner, settings)
```

because `_resolve_device` (`:769-792`) calls `scanner.get_devices()` when `settings.scanner.device`
is empty. The error shape to copy is two lines above it (`:833-835`):

```python
    if request.profile_name not in settings.profiles:
        msg = f"Unknown profile: {request.profile_name}"
        raise ConfigError(msg)
```

---

### 8. Feeder resolution in `_resolve_source` (D-02)

**Analog:** `_resolve_source` itself, `sane_backend.py:792-846`. The existing validate-or-substitute
path and, critically, the message shape the no-feeder refusal must reuse (`:840-844`):

```python
        else:
            msg = (
                f"Device does not support source '{effective_source}'. "
                f"Available: {available_sources}"
            )
            raise ScanError(msg)
```

**The `Auto` substitution that must become unreachable for manual duplex** (`:831-838`) — its
WARNING text is itself a description of C-01's mechanism:

```python
        if "Auto" in available_sources:
            logger.warning(
                "Source '%s' not available; falling back to 'Auto', whose "
                "routing is decided by the profile's auto_source_mode and may "
                "not be multi-page -- a whole stack can come back as one page",
                effective_source,
            )
            effective_source = "Auto"
```

**The classifier is the only rule** — `classify_source` (`scanner/base.py:55-113`) and
`SourceKind.uses_feeder` (`:39-49`). Its docstring (`:59-62`) states the contract the new branch
must honour:

> This is the ONLY source-classification rule in the codebase. Every question of the form "is this
> source a feeder?" must be answered by calling this function and inspecting the returned member;
> do not re-derive the answer from the source string anywhere else.

**The signal reaching the backend** — `ScanSettings` (`scanner/base.py:170-178`):

```python
@dataclass
class ScanSettings:
    """Settings for a scan operation."""

    source: str
    resolution: int
    mode: str
    auto_source_mode: str = "flatbed"
    paper_size: str = "full"
```

`auto_source_mode` is the exact precedent for "a scan-strategy flag carried in settings and read by
the backend": it is typed as a plain `str` here (not the config `Literal`), defaulted, and consumed
at `sane_backend.py:1135-1141`. Note **Phase 21 D-03 holds** — `grep -rn "vocabulary|JobState|
ScanOutcome" src/saneless/scanner/` is empty; whatever signal is added must keep it that way.

**The consumption site** (`sane_backend.py:1093-1122`) shows why the backend is the right tier:

```python
        with self._open_device(device_id) as dev:
            # Fetched once and passed on: _resolve_source reads the source
            # constraint from it and _set_geometry reads the scan-area options,
            # and a second get_options() call would be a second device round
            # trip for a list that cannot have changed in between.
            raw_options = dev.get_options()

            # Validate source option against device capabilities
            effective_source, has_source_option = _resolve_source(
                raw_options, settings.source
            )
            ...
            source_kind = classify_source(effective_source)
            use_adf = source_kind.uses_feeder
```

`ScanSettings` construction in the pipeline is at `pipeline.py:840-846` — one new keyword there.

---

### 9. The CLI prompt and the non-TTY refusal

**Analog for the exit-2 refusal:** `cli.py:110-112`, inside `scan`:

```python
    if profile not in settings.profiles:
        click.echo(f"Unknown profile: {profile}", err=True)
        sys.exit(2)
```

`err=True` + `sys.exit(N)` is the uniform CLI failure form; the other codes are at `:149-154`
(`ScanError` → 1, `PaperlessError` → 3) and `:79-80` (config → 2).

**Analog for the one-line module seam:** `cli.py:43-47` is the only module-level derived value in
the file, and its comment style is what `_stdin_is_interactive()` should imitate:

```python
# Width of the Status column in `saneless jobs`, derived rather than written
# down: the humanised labels are longer than the raw enum values they replaced,
# and a ninth JobState member must not be able to overflow an 80-column
# terminal without anyone noticing.
_STATUS_COL_WIDTH = max(len(state_label(state)) for state in JobState)
```

(Incidentally: `_STATUS_COL_WIDTH` self-updates for the ninth state. No edit needed.)

**No analog for the interactive read.** Verified by reading all 386 lines of `cli.py`: there is no
`isatty`, no `click.confirm`, no `click.prompt` and no `input()`. Every interaction is one-way
`click.echo`. Use RESEARCH.md Code Examples 4 and 5 (both executed against click 8.3.1) rather
than an in-tree pattern. `sys` is already imported (`cli.py:14`).

**Where the coordinator prompts:** `cli.py:121-133`'s `status_callback` is a closure over `title`
defined inside `scan` — the same construction the `ClickFlipCoordinator` can use, and the reason
prompting from it is correct (`run_pipeline` is synchronous, same thread).

**The test harness:** `tests/test_cli.py:68-171`'s `_patch_cli` returns `(CliRunner(), settings)`
and patches `saneless.cli.load_settings`, `configure_logging`, `SaneBackend`, `PaperlessClient`.
Output assertions follow `:217-223`:

```python
    def test_scan_status_output(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Scan shows progress messages during pipeline execution."""
        runner, _ = _patch_cli(monkeypatch)
        result = runner.invoke(cli, ["scan", "--title", "Test"])
        assert "Scanning..." in result.output
        assert "Assembling PDF..." in result.output
        assert "Uploading to paperless-ngx..." in result.output
```

C-02's ordering assertion ("the prompt appears between `Scanning...` and `Scanning reverse
sides...`") is this test plus `result.output.index(...)` comparisons.

---

### 10. `auto-profiles` emits `duplex = "hardware"` (D-06)

**Analog:** the `auto_source_mode` write-when-non-default line, `auto_profiles.py:519-527`:

```python
        profile_table = tomlkit.table()
        profile_table.add("source", profile.source)
        profile_table.add("resolution", profile.resolution)
        profile_table.add("mode", profile.mode)
        if profile.auto_source_mode != "flatbed":
            profile_table.add("auto_source_mode", profile.auto_source_mode)
        auto_generated_flag = True
        profile_table.add("auto_generated", auto_generated_flag)
```

One `if profile.duplex != "none":` in the same place. Phase 16's "only write when non-default".

**The generation side** — `generate_profiles` (`auto_profiles.py:294-382`) already routes every
source question through `classify_source`, and says so at `:311-317`:

> Every question this function asks about a source name is answered by ``classify_source``
> (D-02, Q9). It previously carried three rules of its own … which disagreed with the classifier
> at the edges…

The new `duplex=` argument goes beside `auto_source_mode=` in **both** construction sites —
the loop at `:361-367` and the default profile at `:370-380`. `_auto_source_mode`
(`:289-291`) is the one-expression helper pattern if a helper is preferred to an inline
conditional.

**DPLX-07's round-trip generalisation** — `tests/test_auto_profiles.py:548-566` is the existing
one of three:

```python
    def test_a_feeder_only_config_round_trips_through_load_settings(
        self, tmp_path: Path
    ) -> None:
        """
        The written file loads back, which is the guarantee that matters.

        "default is present" is a proxy; a config saneless can actually load
        after auto-profiles has run is the user-visible promise.
        """
        caps = DeviceCapabilities(
            sources=["Automatic Document Feeder", "ADF Duplex"],
            resolutions=[300],
            modes=["Color"],
        )
        config_file = tmp_path / "config.toml"
        write_profiles_to_config(config_file, generate_profiles(caps))

        settings = load_settings(str(config_file))
        assert settings.profiles["default"].source == "Automatic Document Feeder"
```

RESEARCH confirms the emission logic is **already correct** (`generate_profiles:303-309` reserves
`"default"` and names `Settings.validate_default_profile` as the reason). DPLX-07 reduces to
parametrising this test three ways — do not rebuild the generator.

---

### 11. Test harnesses to reuse

**The e2e case record** — `tests/test_outcomes_e2e.py:296-345` is a frozen dataclass whose
`label` doubles as the pytest id, with a field-by-field `Attributes:` docstring. Add
`flip_timeout: int = _PRODUCTION_TIMEOUT`-style field and a new `_Case` entry at `:347-402`.
The existing duplex case is the template:

```python
    _Case(
        label="duplex-mismatch",
        handler_factory=_accepting_handler,
        expected_state=JobState.DONE,
        expected_outcome=ScanOutcome.SUCCESS,
        expected_pages=(5, 0, 5),
        expected_failed_pdfs=0,
        expected_consume_pdfs=0,
        source=_DUPLEX_SOURCE,
        scan_passes=(3, 2),
        awaits_flip=True,
        warning_contains="Page count mismatch: 3 fronts, 2 backs",
    ),
```

`_DUPLEX_SOURCE = "Manual Duplex"` (`:97`) becomes the new two-key form; `_build_settings`
(`:457-500`) is where `flip_timeout_seconds` is threaded into `OutputConfig`.

**The zero-cost timeout seam** — copy `_TIMEOUT_BUDGET`'s comment wholesale (`:104-115`), because
it already explains why the value is `0` and not `0.05` for an `int`-typed pydantic field:

```python
# Zero, not the 0.05 s the plan suggested.  OutputConfig.paperless_task_timeout
# is typed ``int``, so pydantic rejects 0.05 outright (a float with a fractional
# part is not a lax-mode int) and assigning it afterwards would be a type lie
# that ty and pyrefly are right to reject. ...
_TIMEOUT_BUDGET = 0
```

**Per-pass page counts** — `_build_scanner` (`:430-454`); note its docstring already describes
flip ownership and will need one sentence updated:

```python
    scanner = MagicMock(spec=ScannerBackend)
    scanner.scan_pages.side_effect = [
        scan_batch(_pages(count)) for count in scan_passes
    ]
```

`scan_batch` is the conftest helper (`tests/conftest.py:116-143`); the import form is
**`from tests.conftest import scan_batch`** (`test_outcomes_e2e.py:72`) — the bare `conftest`
form raises `ModuleNotFoundError` under pytest 9's importlib mode, documented at
`conftest.py:249-255` and `pyproject.toml:140-148`.

**The real-backend integration test** — `tests/test_pipeline.py:797-852`. Two changes, both
flagged by RESEARCH:

```python
        dev = FakeSaneDev()
        dev.report_sources(["Flatbed", "ADF Manual Duplex"])   # :813 -- MUST become realistic
        ...
        flip = threading.Event()

        def _reload_the_stack(event: PipelineEvent) -> None:
            """Put the flipped stack back when the pipeline asks for it."""
            if event is PipelineEvent.AWAITING_FLIP:
                dev.load_feeder(backs)
                flip.set()

        request = PipelineRequest(
            profile_name="default",
            title="Duplex over the shared fake",
            status_callback=_reload_the_stack,
            flip_event=flip,                                    # :831 -- becomes the coordinator
        )
```

`tests/fake_sane.py:787-801`'s `report_sources` is the seam to use (its docstring explicitly says
to narrow the constraint here rather than subclass the fake), and `_DEFAULT_SOURCES` at `:109` is
`["Flatbed", "Automatic Document Feeder", "ADF Duplex"]` — **no plain `"ADF"`**, which is what
makes this test an automatic regression guard against C-01's declined hardcoded-`"ADF"`
suggestion. `load_feeder` (`:740-766`) rewinds the feeder, which is what makes a two-pass run
drivable through one device handle.

**The `TestIsManualDuplex` relocation** — `tests/test_pipeline.py:237-259`, six cases, moves to
`tests/test_config.py` retargeted at the new private predicate. Do not delete: the rule still
exists and still deserves its edge cases.

```python
class TestIsManualDuplex:
    """Tests for _is_manual_duplex helper."""

    def test_adf_manual_duplex(self) -> None:
        """Source 'ADF Manual Duplex' is manual duplex."""
        assert _is_manual_duplex("ADF Manual Duplex") is True
    ...
    def test_hardware_duplex_not_manual(self) -> None:
        """Hardware duplex without 'manual' is not manual duplex."""
        assert _is_manual_duplex("ADF Duplex") is False
```

**The `caplog` assertion shape** — `tests/test_auto_profiles.py:613-627`:

```python
    def test_collision_warning_names_both_sources(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The WARNING names both colliding source strings and the new slug."""
        with caplog.at_level(logging.WARNING, logger="saneless.auto_profiles"):
            generate_profiles(self._caps())

        message = "\n".join(
            record.getMessage()
            for record in caplog.records
            if record.levelno == logging.WARNING
        )
        assert "ADF-Front" in message
```

Retarget to `logger="saneless.config"` and assert the profile name, `duplex`, `"manual"` and the
`devices --capabilities` pointer are all present.

**The config-validator test shape** — `tests/test_config.py:63-75` (inline TOML heredoc string,
`config_file.write_text`, `load_settings(config_path=str(...))`, `pytest.raises(...)`).

**The web route test shape** — `tests/test_web.py:244-253` are the two flip tests that currently
assert only `status_code == 200`; `:188-199` (`test_flip_prompt`) shows the
`_app(client).state.worker._current_job_id = job.id` reach-through used to stage a state.
`tests/test_web_state_rendering.py:122-135` documents that reach-through as deliberate.

---

## Shared Patterns

### Totality: `match` + `assert_never`, never a dict
**Source:** `vocabulary.py:235-268` (`job_state_for`), `scanner/base.py:39-49`
(`SourceKind.uses_feeder`), `pipeline.py:77-93` (`PipelineEvent.job_state`)
**Apply to:** `FlipOutcome` consumption, the `_DuplexMismatch` dispatch, both new `JobState` arms
Assign to a local, `case _: assert_never(x)`, `return` once at the end. Docstring carries
`Raises: AssertionError:`. Phase 21 D-08 measured that a dict draws no diagnostic from either
checker; the match is caught by both at edit time.

### Error raising: bind the message first
**Source:** `pipeline.py:341-343`, `:658-659`, `:789-790`; `sane_backend.py:840-844`;
`config.py:185-186`
**Apply to:** every new `ScanError` / `ConfigError` in this phase
```python
        msg = f"Device does not support source '{effective_source}'. ..."
        raise ScanError(msg)
```
Ruff `EM101`/`EM102` forbid the literal or f-string inline in the `raise`.

### Logging: `%`-style lazy args, never f-strings
**Source:** `pipeline.py:462-468`, `auto_profiles.py:508-512`, `worker.py:288`
**Apply to:** the D-04 deprecation warning, any new coordinator/timeout log
Ruff's `G` family is in `select` (`pyproject.toml:91`). This is also the log-injection control
for interpolating a user-supplied profile name.

### Argument ceiling: five, and it is never suppressed
**Source:** `pipeline.py:572-575` (derive), `pipeline.py:388-412` + `job.py:423-429` (bundle),
`tests/fake_sane.py:755-758` (method instead of ctor kwarg)
**Apply to:** `_scan_manual_duplex` (drop `notify`, add `timeout`)
Three distinct in-tree escapes from `PLR0913` exist. Raising the limit and `# noqa` are both
forbidden by CLAUDE.md.

### No bare `assert` in `src/`
**Source:** `pyproject.toml:91` (`S101` selected) vs `:129-133` (relaxed for `tests/**` only)
**Apply to:** `WorkerFlipCoordinator.wait_for_flip` — restructure so the type narrows by return
value, not by assertion.

### Unused-but-required parameters
**Source:** `pyproject.toml:99` `dummy-variable-rgx = "^(_+|(_+[a-zA-Z0-9_]*[a-zA-Z0-9]+?))$"`;
in-tree: `pipeline._noop_callback(_event)` (`:148-149`), `cli.devices(_settings)` (`:211`),
`config.py:155-156`'s two `# noqa: ARG003` for a signature pydantic-settings mandates
**Apply to:** ~~the CLI coordinator's `_timeout` parameter (Open Question 1)~~ — **SUPERSEDED by
CONTEXT.md D-19 (2026-09-14).** The CLI coordinator now *honours* its timeout via a daemon thread, so
the parameter is named `timeout`, is genuinely used, and needs no `dummy-variable-rgx` treatment.
**Do not write `_timeout` in `ClickFlipCoordinator`** — plan 25-07 asserts `grep -c "_timeout"` is 0.
The pattern below still applies to genuinely unused parameters elsewhere: the leading-underscore form
is preferred; `# noqa: ARG` appears exactly twice in `src/` and only where a third-party signature
forces it.

### Docstrings: `D` rules are on, with `Args:` / `Returns:` / `Raises:`
**Source:** any public function in `pipeline.py` or `vocabulary.py`; `scanner/base.py:247-265`
for the abstract-method form
**Apply to:** every new module-level function, class, method and ABC member. `D203`/`D212` are
ignored (`pyproject.toml:92`), i.e. no blank line before class, summary on the second line for
multi-line docstrings.

### Comments that record a *declined* alternative
**Source:** `scanner/base.py:96-113` (the `UNKNOWN` policy, ending "This is a settled answer. Do
not re-open it as an unmade decision."), `vocabulary.py:63-71` (no `FAILED` member),
`pipeline.py:234-242` (`_preserving` catching `Exception`), `pipeline.py:948-956` ("Do not re-add
a status check")
**Apply to:** D-05 (`"hardware"` has no reader — say so), D-08 (the mismatch path deliberately
bypasses `_drop_empty_pages`), D-07 (this dispatch satisfies N-07). This codebase consistently
writes down why the *other* branch was not taken; a bare implementation will read as an oversight.

### Enum completeness tests parametrise over `list(EnumType)`
**Source:** `tests/test_vocabulary.py:57`, `:110`, `:179`, `:204`;
`tests/test_web_state_rendering.py:167`, `:185`, `:195`, `:205`, `:244`, `:258`
**Apply to:** any new `FlipOutcome` test. Literal-text tests stay hand-written by design (Phase 21
D-09) — that split is why exactly four rosters need a manual edit and ten parametrisations do not.

### Template vocabulary lives in Python, not Jinja
**Source:** `web/app.py:89-102` — `state_label` / `progress_label` registered as filters, `JobState`
as a global, with the comment "The templates own no vocabulary of their own"
**Apply to:** nothing. This is *why* `status.html`, `history.html` and `index.html` need no edit
for the ninth state. `Job.is_active` / `Job.is_busy` (`job.py:412-420`) are thin wrappers over
`ACTIVE_STATES` / `BUSY_STATES`, so both follow automatically.

---

## Files That Must Not Be Touched

Recorded so the executor does not "helpfully" edit them and the verifier does not read their
absence from the diff as an omission.

| File | Why no edit |
|---|---|
| `tests/test_web_state_rendering.py` | All six `list(JobState)` cases build expectations from `state_label` / `progress_label` / `ACTIVE_STATES` / `BUSY_STATES`. `test_status_area_prose` (`:206-241`) already asserts `("flip-prompt" in text) is (state is JobState.AWAITING_FLIP)` for **every** state — so it proves D-16's "Continue and Abort vanish during pass B" with zero new test code, the moment `SCANNING_REVERSE` joins `ACTIVE_STATES`. |
| `src/saneless/web/templates/partials/status.html` | The busy branch (`:8-9`) absorbs the new state via `job.is_busy`. Adding a `SCANNING_REVERSE` branch would give pass B a presentation of its own and **break** D-16. |
| `src/saneless/web/templates/partials/flip.html` | Stays bound to the `AWAITING_FLIP` branch only (`status.html:10-11`). That binding is the whole mechanism of D-16. (The doc claim that its buttons are "Continue and **Cancel**" is wrong — `:42-43` say `Continue` / `Abort scan` — but that is a *docs* fix, not a template fix.) |
| `tests/fake_sane.py` | `report_sources` (`:787-801`) and `load_feeder` (`:740-766`) already provide every seam Wave E needs, and `_DEFAULT_SOURCES` (`:109`) already omits a plain `"ADF"`. |
| `src/saneless/job.py` | `state TEXT NOT NULL` (`:289`) has no `CHECK` constraint. **No migration, no `PRAGMA user_version` step.** `is_active` / `is_busy` (`:412-420`) are derived. |
| `src/saneless/scanner/base.py::classify_source` | Phase 24 D-01: the only classification rule, settled. Consume it; do not extend or re-derive it. |
| `cli.py:43-47` (`_STATUS_COL_WIDTH`) | Derived from `max(... for state in JobState)`; self-updates for the ninth member. |

---

## No Analog Found

Files/changes with no close match in the codebase. Use RESEARCH.md's executed examples instead.

| Change | Role | Data Flow | Reason |
|---|---|---|---|
| `cli._stdin_is_interactive()` + `click.confirm` prompt | controller (CLI) | request-response (interactive) | Verified by reading all 386 lines: `cli.py` has **no** `isatty`, `click.confirm`, `click.prompt` or `input()`. Every interaction is one-way `click.echo`. This is the CLI's first interactive read. Nearest structural analog is the module-level seam at `cli.py:43-47`; for behaviour use RESEARCH.md Code Examples 4 and 5 (executed against click 8.3.1, including the measured `CliRunner` non-TTY result and `click.pause`'s no-op). |
| `config.py` module logger | config | event (log) | `config.py` defines no logger today. Copy the one-liner from `worker.py:40` / `routes.py:20`; the novelty is only that this module has never logged. |
| `pydantic model_validator(mode="before")` | model | transform | The codebase has `field_validator` (`config.py:177`) but **no** `model_validator` anywhere. Use RESEARCH.md Code Example 1, which was executed against this project's pydantic 2.12.5 / pydantic-settings 2.13.1 and covers Pitfall 3 (do not overwrite an explicit `duplex`). |

---

## Documentation Patterns

The phase rule is "correct what you changed", and D-18 makes the ADF how-to load-bearing rather
than cosmetic.

**Analog:** `docs/how-to/set-up-adf-duplex.md` rewrites itself — its own §"ADF Simplex" and
§"ADF Hardware Duplex" (`:14-42`) are the house form the Manual Duplex section should match: a
one-paragraph explanation, a fenced ```toml``` profile block, a fenced ```bash``` invocation.

Sentences that are now false, each read this session:

| Line(s) | Current text | Why it is false after this phase |
|---|---|---|
| `:48-52` | admonition: *"A source name containing 'duplex' is treated as a feeder source"* | That is a `classify_source` statement, not a strategy statement; as written it reads as the old rule |
| `:56-67` | the `source = "Manual Duplex"` block | D-18 removes the legacy form from the docs entirely |
| `:60` | *"press Enter (CLI)"* | D-11 uses `click.confirm`, and the roadmap criterion needs amending with it |
| `:64` | *"set the source to any string containing both 'manual' and 'duplex'"* | the substring rule is gone from the strategy path |
| `:77` | *"Continue and **Cancel** buttons"* | `flip.html:43` says `Abort scan` (same defect as doc row 24) |
| `:100-105` | § Page count mismatch | needs D-08's note: blank-page removal is deliberately skipped here, because a blank back is *evidence* |

The seven other doc files and `.planning/UI-SPEC.md` are enumerated with line numbers in
RESEARCH.md § "Documentation Scope"; six rows there are *new* findings the CONTEXT does not list.
Note the UI-SPEC re-check the CONTEXT asked for has been done: `:197` claims "Nine presentations"
while the table at `:238-247` lists **eight** and omits `AWAITING_FLIP`, so the summary already
disagrees with its own table.

---

## Metadata

**Analog search scope:** `src/saneless/` (all 9 modules + `scanner/` + `web/` + templates),
`tests/` (12 modules), `docs/how-to/`, `pyproject.toml`, `.planning/phases/25-manual-duplex/`
**Files read this session:** 32 (14 source, 13 test, 3 template, 1 doc, 1 config)
**Pattern extraction date:** 2026-09-13
**Gates every excerpt above must survive:** `uv run ruff check .`, `uv run ruff format --check .`,
`uv run ty check`, `uv run pyrefly check src tests` (paths always named — 23.1 D-10)
