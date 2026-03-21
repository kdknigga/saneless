# Phase 7: Tech Debt Cleanup - Context

**Gathered:** 2026-03-21
**Status:** Ready for planning

<domain>
## Phase Boundary

Address 5 of 6 tech debt items from v1.0 milestone audit: python-sane packaging, Starlette TemplateResponse deprecation, error typing in worker, flip flow timing gap, and Playwright browser rendering tests. The 6th item (physical scanner verification) is deferred — requires real hardware.

No new features, no UI changes, no pipeline restructuring. This is cleanup and hardening of existing code.

</domain>

<decisions>
## Implementation Decisions

### python-sane packaging
- `python-sane` is a mandatory dependency — the application cannot function without it
- Add `"python-sane>=2.9.1"` to the main `dependencies` list in pyproject.toml
- Dockerfile already has `libsane` in runtime stage — `python-sane` compiles during `pip install`
- Lazy import pattern in `sane_backend.py` remains for dev convenience (test collection without C extension)
- README documents that `libsane-dev` (Debian) or `sane-backends-devel` (RHEL/Fedora) system headers are required before `pip install`

### Starlette TemplateResponse deprecation
- Fix all 10 call sites in `src/saneless/web/routes.py` to use the new Starlette signature
- New signature: `TemplateResponse(name, context, request=request)` — name as first positional arg, request as keyword
- Mechanical find-and-replace — no behavioral changes
- Verify zero deprecation warnings in test output after fix

### Error category design
- Add `ErrorCategory` enum to `job.py`: `FEEDER`, `CONFIG`, `SCANNER`, `UPLOAD`, `UNKNOWN`
- Add `error_category` optional field to `Job` dataclass (default `None`, set on error)
- Worker's `except Exception` block catches specific exception types first:
  - `FeederEmptyError` → `ErrorCategory.FEEDER`
  - `ConfigError` → `ErrorCategory.CONFIG`
  - `ScanError` → `ErrorCategory.SCANNER`
  - `PaperlessError` → `ErrorCategory.UPLOAD`
  - `Exception` → `ErrorCategory.UNKNOWN`
- `JobStore.update_state()` accepts optional `error_category` parameter
- Error message text (`str(exc)`) still stored for display — category is for programmatic use

### Flip timing synchronization
- Use `threading.Event` for synchronization: worker signals when it transitions out of `AWAITING_FLIP`
- Route `POST /api/flip/continue` calls `worker.continue_flip()` then waits on the event with a timeout (e.g., 2s)
- If timeout expires, return current state anyway (graceful degradation — polling will catch up)
- Worker sets the event after it transitions to SCANNING state in the flip callback handler
- No busy-wait or polling loop — clean event-based synchronization

### Playwright browser tests
- New test file `tests/test_browser.py` with Playwright fixtures
- Test scenarios:
  - PicoCSS renders correctly (page loads, semantic HTML styled, dark/light mode)
  - HTMX polling works (status area updates without full page reload)
  - Flip prompt UI renders with Continue/Cancel buttons
  - Form elements functional (profile dropdown, metadata fields, scan button)
- Tests use the existing `create_app()` factory with `StubScanner` — no real scanner needed
- Run against a local uvicorn server started in a fixture
- Mark with `@pytest.mark.playwright` or similar for optional test selection

### Claude's Discretion
- Exact TemplateResponse signature migration (whether to use positional or keyword args for name)
- Playwright test fixture design (session-scoped server vs function-scoped)
- Whether ErrorCategory needs to be persisted to SQLite job_store or kept in-memory only
- Uvicorn server startup approach in Playwright fixtures (subprocess vs threading)

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Tech debt source
- `.planning/v1.0-MILESTONE-AUDIT.md` — Tech debt section defines all 6 items, their origin phases, and severity

### Error handling
- `src/saneless/exceptions.py` — Exception hierarchy: SanelessError → ConfigError, ScanError → FeederEmptyError, PaperlessError
- `src/saneless/worker.py` lines 159-165 — Bare `except Exception` that needs typed error categories
- `src/saneless/job.py` lines 24-33 — `JobState` enum, `Job` dataclass

### TemplateResponse deprecation
- `src/saneless/web/routes.py` — 10 `TemplateResponse` call sites with deprecated arg order (request first, name second)

### Flip timing
- `src/saneless/web/routes.py` lines 288-304 — `continue_flip()` route that returns immediately
- `src/saneless/worker.py` — `continue_flip()` method and `_flip_event` threading.Event

### Packaging
- `pyproject.toml` lines 23-33 — Dependencies (python-sane is mandatory, requires libsane-dev headers)
- `Dockerfile` — Two-stage build, runtime has `libsane` for python-sane compilation
- `src/saneless/scanner/sane_backend.py` — Lazy import of `sane` module

### Existing test patterns
- `tests/conftest.py` — Shared fixtures, StubScanner
- `tests/test_web.py` — httpx-based web tests (model for Playwright tests)

### Prior phase context
- `.planning/phases/03-web-ui/03-CONTEXT.md` — Web UI architecture, PicoCSS/HTMX/Jinja2 decisions
- `.planning/phases/04-packaging-and-deployment/04-CONTEXT.md` — Dockerfile design, packaging approach

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `src/saneless/exceptions.py`: Clean exception hierarchy — `SanelessError` base, `ScanError`, `FeederEmptyError`, `ConfigError`, `PaperlessError` — ready for typed catch blocks
- `tests/conftest.py`: `StubScanner` concrete class for testing without real SANE — reusable in Playwright fixtures
- `src/saneless/web/app.py`: `create_app(settings, scanner)` factory — can spin up full app for browser tests
- `src/saneless/worker.py`: `_flip_event` already uses `threading.Event` for flip signaling — extend pattern for transition notification

### Established Patterns
- Exception hierarchy: All custom exceptions inherit from `SanelessError` — new `ErrorCategory` fits alongside
- Worker state machine: `_status_cb` maps pipeline status strings to `JobState` values — error categorization follows same pattern
- Web tests: httpx `TestClient` with `create_app()` — Playwright tests would use same app factory but with real browser
- System dependency documentation: README must note libsane-dev/sane-backends-devel requirement

### Integration Points
- `JobStore.update_state()` — needs `error_category` parameter added
- `worker._run()` except block — needs typed exception catches
- `routes.py` TemplateResponse calls — mechanical arg reorder
- `Dockerfile` pip install line — needs `[sane]` extra added
- New `tests/test_browser.py` — new file, no existing code to modify

</code_context>

<specifics>
## Specific Ideas

- The TemplateResponse fix is the simplest item — pure mechanical find-and-replace across 10 call sites
- Error categorization should match the exception hierarchy: each exception class maps to exactly one category
- Flip timing fix extends the existing `_flip_event` threading.Event pattern — not a new mechanism
- Playwright tests should use CDN-loaded PicoCSS and HTMX (same as production) to catch real rendering issues
- python-sane is mandatory — a scanning app that installs without error but can't scan is worse than a loud build failure prompting the user to install libsane-dev

</specifics>

<deferred>
## Deferred Ideas

- Physical scanner verification (tech debt item 6) — requires real SANE hardware, cannot be automated
- Nyquist compliance for phases 1, 4, 5, 6 — separate `/gsd:validate-phase` runs

</deferred>

---

*Phase: 07-tech-debt-cleanup*
*Context gathered: 2026-03-21*
