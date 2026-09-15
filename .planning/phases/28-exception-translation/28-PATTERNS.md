# Phase 28: Exception Translation - Pattern Map

**Mapped:** 2026-09-15
**Files analyzed:** 38 (14 source/template/style, 11 docs + nav + UI-SPEC, 13 tests/fixtures)
**Analogs found:** 36 / 38 (two have no in-repo analog: uvicorn startup-failure capture, the new how-to's content)

All line numbers below were read from the working tree at commit `6cd5a6a`. Every analog is
in this repo; this phase adds no package. Read CONTEXT.md D-07's amendment: `serve`'s bind
failure and uvicorn's startup failure both exit **2** (RESEARCH.md Open Question 2 is superseded).

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|---|---|---|---|---|
| `src/saneless/exceptions.py` | model (exception hierarchy) | n/a | itself: `FeederEmptyError(ScanError)`, `PaperlessTimeoutError(PaperlessError)` (lines 20-45) | exact |
| `src/saneless/vocabulary.py` (`JobState.CANCELLED`, `ErrorCategory` PDF member, `ExitCode`, `exit_code_for`) | model / total lookup | transform | `JobState.FALLBACK` ripple (52-63, 253-255, 274-363); `rejection_status_code` (621-658) for enum→int; `GeometryUnit(IntEnum)` (`scanner/sane_backend.py:269-294`) | exact |
| `src/saneless/cli.py` (Group guard, `ClickFlipCoordinator` cause, per-command catches, `require_sane` calls, serve exit 2) | controller (CLI) | request-response | `_load_cli_settings` (193-246), `scan` catches (300-324), `serve` bind `ClickException` (499-507) | exact (catch shape), role-match (Group subclass is new) |
| `src/saneless/worker.py` (three job endings, `exc_info`) | service | event-driven | `_scan_job` except (1313-1336), `_failure_record` (941-965), `abort_for_shutdown` lock (242-266) | exact |
| `src/saneless/pipeline.py` (flip match → cancel vs prompt failure, `_require_pages`, "All pages were blank", `FlipCoordinator.abort_cause`) | service (orchestration) | request-response | flip `match` (947-966); `_check_disk_space` precondition (315-337); `_drop_empty_pages` (474-507); `_flip_context` refusal (992-1025) | exact |
| `src/saneless/pdf.py` (boundary catch → `PdfError`, empty-list refusal) | utility | file-I/O / transform | `_preserving` broad-type narrow-span catch (`pipeline.py:442-471`) | role-match |
| `src/saneless/paperless.py` (ctor `InvalidURL`, retry set, one body renderer, poll continuation, get_tags/correspondents, duplicate wording, consume-dir wrap) | service (HTTP client) | request-response | upload loop (269-329), `test_connection` `TransportError` catch (490-498), `_failure_message` (97-123), `_truncated_body` (126-142) | exact |
| `src/saneless/scanner/sane_backend.py` (`require_sane`, SANE call-site wrapping) | adapter (device) | request-response / streaming | `_acquire_pages` translate-and-chain (737-743); `_ensure_sane` (43-56) | exact |
| `src/saneless/config.py` (`TOMLDecodeError` / `OSError` / `UnicodeDecodeError` → `ConfigError` under header) | config loader | file-I/O | `_build_settings` renderer (974-1025), `_escape_name` (593-608) | exact |
| `src/saneless/auto_profiles.py` (`tomlkit.parse` → `ConfigError`) | config writer | file-I/O | `_read_config` UTF-8 catch (644-649), `_render_checked` (718-732) | exact |
| `src/saneless/logging_config.py` (report whether file handler attached, for D-06 "Full details in <log_file>") | config | n/a | itself (55-68) | exact |
| `src/saneless/web/templates/partials/status.html` | component | request-response | FALLBACK branch (21-26) | exact |
| `src/saneless/web/templates/partials/history.html` | component | request-response | line 7 class chain | exact |
| `src/saneless/web/static/app.css` | style | n/a | `.status-done` / `.status-error` using Pico tokens directly (20-26) | exact |
| `docs/how-to/troubleshoot-a-failed-scan.md` (new) | doc (how-to) | n/a | `docs/how-to/scanner-host-discovery.md` § Troubleshooting (70-95) for bold-symptom format | partial |
| `mkdocs.yml` | config (nav) | n/a | How-To Guides block (44-50) | exact |
| `docs/how-to/cli-scripting.md`, `docs/reference/cli-commands.md` (exit tables) | doc (reference) | n/a | existing tables (`cli-scripting.md` "## Exit codes" table; `cli-commands.md:31-38`) | exact |
| `docs/reference/web-api.md`, `docs/how-to/set-up-adf-duplex.md`, `docs/explanation/{architecture,consume-directory-fallback,empty-page-detection}.md`, `docs/how-to/{install-bare-metal,scanner-host-discovery}.md` | doc | n/a | RESEARCH.md § "Doc Surfaces to Change (exact)" line list | exact |
| `.planning/UI-SPEC.md` | spec | n/a | "Measured status contrast" table (152-163), convention rules (136-183) | exact |
| `tests/test_vocabulary.py` | test | n/a | itself (59-68, 154-159, 175-233, 708-751) | exact |
| `tests/test_cli.py` | test (CliRunner) | request-response | `_patch_cli` (81-192), `test_scan_scan_error` (346-380), `TestManualDuplexPrompt` (538-752) | exact |
| `tests/test_worker.py` | test | event-driven | abort tests (1201-1229, 1840-1873), `_worker_records` (1988-1998) | exact |
| `tests/test_pipeline.py` | test | request-response | `test_all_pages_empty_raises` (406-421), `_FixedFlipCoordinator` (530-552), abort test (895-921) | exact |
| `tests/test_pdf.py` | test | file-I/O | `test_temp_files_cleaned_on_error` (96-128) | exact |
| `tests/test_paperless.py` | test (MockTransport) | request-response | `_make_transport` (33-37), `_CONNECTION_EXCEPTION_CASES` (141-145), retry tests (287-374), `test_non_200_body_is_truncated` (505-518) | exact |
| `tests/test_scanner.py` + `tests/fake_sane.py` | test + fixture | streaming | `test_first_page_fault_keeps_the_original_as_cause` (656-667), `_backend_with` (612-624), `FakeSaneError` (543-551), `FakeSaneModule` (1017-1085) | exact (fake needs error seams for `init`/`open`/`get_devices`) |
| `tests/test_config.py`, `tests/test_auto_profiles.py` | test | file-I/O | `TestInvalidToml` (384-391), `_load_error` (573+), `_assert_value_absent` (761-766) | exact |
| `tests/test_deployment_config.py` (doc-truth exit-code table) | test (doc-truth) | n/a | `_read` (187-189), `_table_row` (259-263), `test_scripting_exit_code_two_examples` (323-330) | exact |
| `tests/test_web_state_rendering.py` | test | request-response | `test_history_cell_css_class` (209-216), `test_status_area_prose` (229-265) | exact |
| `tests/test_browser.py` | test (Playwright) | n/a | `fallback_page` fixture (1028-1072), `_PROBE_STATUS_COLOURS` (774-787), AA test (1214-1240), forced dark (1242-1255) | exact |

## Pattern Assignments

### `src/saneless/exceptions.py` (model)

**Analog:** itself, lines 8-45.

**Subclass + `__all__` pattern** (lines 8-17, 32-41). Keep `__all__` sorted, one-line docstring per class, no
constructor (Pitfall 9: `_preserving` rebuilds `type(exc)(msg)` at `pipeline.py:469-470`, so every new class
must stay single-message):
```python
__all__ = [
    "ConfigError",
    "FeederEmptyError",
    "PaperlessError",
    "PaperlessTimeoutError",
    "SanelessError",
    "ScanError",
    "StorageError",
]
...
class FeederEmptyError(ScanError):
    """ADF feeder is empty -- no paper detected."""
...
class PaperlessTimeoutError(PaperlessError):
    """Paperless-ngx did not resolve a consume task before the deadline."""
```
Add `PdfError(SanelessError)` and `ScanCancelledError(SanelessError)` (RESEARCH recommendation: direct
`SanelessError` subclass, so no `except ScanError` site at `cli.py:317`, `sane_backend.py:737`,
`vocabulary.py:682` can absorb a cancel). The `describe(exc)` helper (`str(exc) or type(exc).__name__`) can
live here too; `exceptions.py` is a leaf, so every translating module can import it.

---

### `src/saneless/vocabulary.py` (model / total lookup)

**Analog:** the FALLBACK ripple in the same file.

**Import rule** (lines 1-20): leaf module, may import only `saneless.exceptions`. `ExitCode` needs
`from enum import IntEnum, StrEnum`. The import block is currently:
```python
from enum import StrEnum
from typing import Final, assert_never

from saneless.exceptions import (
    ConfigError,
    FeederEmptyError,
    PaperlessError,
    ScanError,
)
```
Add `PdfError`. `__all__` (22-49) is alphabetised: insert `"ExitCode"` and `"exit_code_for"`.

**Enum member** (52-63): add `CANCELLED = "CANCELLED"` after `FALLBACK`. `ErrorCategory` (66-83) gains the PDF
member; its docstring explains a non-obvious member (REJECTED), so give the new one a sentence.

**Terminal set** (253-260):
```python
TERMINAL_STATES: frozenset[JobState] = frozenset(
    {JobState.DONE, JobState.ERROR, JobState.FALLBACK}
)
```

**Total match arm** (`state_label`, 291-312; `progress_label`, 342-363, whose docstring at 327-330 names
"``DONE``, ``ERROR`` and ``FALLBACK``" and must be updated):
```python
        case JobState.FALLBACK:
            label = "Saved to folder"
        case _:
            assert_never(state)
    return label
```
`error_message` (438-482; CONTEXT calls it `user_message`, RESEARCH Pitfall 11) gains only the PDF arm.

**Enum → integer total mapping to copy for `exit_code_for`** (`rejection_status_code`, 635-658). Note the
`A | B` arm and the assign-then-return-once shape (avoids PLR0911):
```python
    match rejection:
        case RequestRejection.QUEUE_FULL:
            status_code = 429
        case RequestRejection.WORKER_DOWN | RequestRejection.WORKER_DEGRADED:
            status_code = 503
        ...
        case _:
            assert_never(rejection)
    return status_code
```

**IntEnum with explanatory docstring** (`scanner/sane_backend.py:269-294`, `class GeometryUnit(IntEnum)`): the
docstring states why dispatch is a `match` and never a mapping. Mirror that for `ExitCode`.

**Ordered isinstance chain** (661-686), gains the PDF branch; the docstring already explains
narrower-first ordering:
```python
    if isinstance(exc, FeederEmptyError):
        return ErrorCategory.FEEDER
    if isinstance(exc, ConfigError):
        return ErrorCategory.CONFIG
    if isinstance(exc, ScanError):
        return ErrorCategory.SCANNER
    if isinstance(exc, PaperlessError):
        return ErrorCategory.UPLOAD
    return ErrorCategory.UNKNOWN
```
Five `return`s today (no `max-returns` override in `pyproject.toml`, so ruff's default of 6 applies). The PDF
branch makes six, exactly at the limit. A seventh branch (for example a cancel arm) fails PLR0911, so convert
to assign-once in that case (RESEARCH Pitfall 10).

---

### `src/saneless/cli.py` (controller, request-response)

**Analog:** `_load_cli_settings` and `scan` in the same file.

**Imports** (20-48): relative imports (`from .exceptions import ...`, `from .vocabulary import ...`). Add
`SanelessError`, `ScanCancelledError`, `PdfError`, `ExitCode`, `exit_code_for`, `classify_error`, and
`from .scanner.sane_backend import SaneBackend, require_sane` (module-global name, so tests patch
`saneless.cli.require_sane`, same as `saneless.cli.SaneBackend` at test line 112).

**Group decorator to replace** (168-190). The guard goes on as `@click.group(cls=_GuardedGroup)`. The callback
comment at 184-187 explains why nothing loads here (CFG-10); keep it:
```python
@click.group()
@click.option("--config", "config_path", default=None, help="Path to config file.")
@click.option("-v", "--verbose", is_flag=True, help="...")
@click.pass_context
def cli(ctx: click.Context, config_path: str | None, *, verbose: bool) -> None:
    """Saneless -- SANE scanner to paperless-ngx bridge."""
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path
    ctx.obj["verbose"] = verbose
```
`ctx.obj` is where "logging is configured" must be recorded (RESEARCH Pitfall 3: `logging.lastResort` prints a
traceback before setup, breaking "one line").

**Current one-line + exit pattern to centralise** (`_load_cli_settings` 218-238, `scan` 300-324):
```python
    except ConfigError as exc:
        # The loader's renderer already wrote its own "Configuration error in
        # <file>:" header; a second prefix would double it (D-10).
        click.echo(str(exc), err=True)
        sys.exit(2)
    except Exception as exc:
        ...
        click.echo(f"Configuration error: {exc}", err=True)
        sys.exit(2)
```
```python
    except ScanError as exc:
        click.echo(f"Scan error: {exc}", err=True)
        sys.exit(1)
    except PaperlessError as exc:
        click.echo(f"Paperless error: {exc}", err=True)
        sys.exit(3)
    finally:
        paperless.close()
```
Keep the prefixes `Scan error:` / `Paperless error:` (quoted in `docs/how-to/set-up-adf-duplex.md:95`), add
`PDF error:`, no prefix for `ConfigError`. Keep `finally: paperless.close()`. The generic `except Exception`
at 233-238 is deleted once `load_settings` raises one type. `PaperlessClient(...)` at 287-291 moves inside
the guarded region (its ctor will raise `PaperlessError` for `InvalidURL`).

**Guard shape (verified by research probe, RESEARCH.md Pattern 1):** click's `Exit`/`Abort` are
`RuntimeError`s and must be re-raised first. `ctx.exit(code)` is the single exit.

**Existing remaining catches to keep in the table** (`auto-profiles`, 585-595):
```python
    except OSError as exc:
        click.echo(f"Cannot write {config_path}: {exc.strerror or exc}", err=True)
        sys.exit(2)
```
Replace literals `1`/`2`/`3` with `ExitCode` members (CONTEXT: "codes live in one place"). Remaining literal
sites: 232, 238, 267, 284, 319, 322, 573, 592, 595.

**`serve` bind failure** (499-507), today a `ClickException` exit 1; D-07 amendment makes it exit 2:
```python
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((actual_host, actual_port))
    except OSError as e:
        msg = f"Cannot bind to {actual_host}:{actual_port}: {e}"
        raise click.ClickException(msg) from e
    finally:
        sock.close()
```
`ClickException` has an `exit_code` attribute; either raise a subclass with `exit_code = ExitCode.CONFIG` or
echo + `ctx.exit(ExitCode.CONFIG)`. `uvicorn.run(...)` (515-522) calls `sys.exit(3)` on startup failure; see
No Analog Found.

**WR-08 prompt failure: store cause on the coordinator** (`ClickFlipCoordinator._prompt`, 141-158):
```python
        except Exception:
            ...
            logger.exception("Flip prompt failed; treating it as an abort")
            self._slot.settle(FlipOutcome.ABORTED)
            return
```
Copy the claim-and-mark-under-one-lock shape from `WorkerFlipCoordinator.abort_for_shutdown`
(`worker.py:196-199, 255-266`):
```python
        # Held across a shutdown's claim and its marker, so the worker thread,
        # woken by that claim, cannot read the marker before it is set (WR-06).
        self._shutdown_lock = threading.Lock()
        self._aborted_by_shutdown = False
...
        with self._shutdown_lock:
            claimed = self._slot.offer(FlipOutcome.ABORTED)
            if claimed:
                self._aborted_by_shutdown = True
        return claimed

    @property
    def aborted_by_shutdown(self) -> bool:
        """Whether :meth:`abort_for_shutdown` claimed this coordinator's answer."""
        with self._shutdown_lock:
            return self._aborted_by_shutdown
```
Use `FlipAnswerSlot.offer` (`pipeline.py:184-202`, returns whether this call claimed), not `settle`, so a
Ctrl-C that claimed first is not misreported as a prompt failure.

---

### `src/saneless/worker.py` (service, event-driven)

**Analog:** `_scan_job`'s except block and `_failure_record`.

**Current block to replace** (1313-1336):
```python
        except Exception as exc:
            error, category = self._failure_record(exc, coordinator)
            # No result argument: outcome, warning and all three page counts
            # stay NULL. ...
            self._finish_or_owe(
                job.id,
                _OwedWrite(JobState.ERROR, error=error, category=category),
            )
            if category is None:
                # Only a shutdown records no category: stop() answered the
                # flip wait with Abort, so the operator did not end this job.
                logger.info("Job %s ended by shutdown: %s", job.id, exc)
            else:
                logger.error(
                    "Job %s failed (%s): %s", job.id, category.value.lower(), exc
                )
            return
```
Keep the long comment block (the WR-02 owed-write reasoning). Replace the `category is None` sentinel with
three explicit branches (shutdown first, then `ScanCancelledError`, then failure) per RESEARCH Pattern 5.
`logger.exception` must be called inside the `except` (it reads `sys.exc_info()`). G rule: no f-strings in
log calls.

**`_failure_record`** (941-965) is also called by `_best_effort_fail` (1003-1005); keep it working there:
```python
        if coordinator is not None and coordinator.aborted_by_shutdown:
            return RESTART_REASON, None
        return str(exc), classify_error(exc)
```

**`_OwedWrite`** (96-118) is frozen and state-agnostic; `_OwedWrite(JobState.CANCELLED, error=str(exc))` needs no
change. Its docstring attribute text "for an ERROR write" may widen to mention CANCELLED.

**Warning-with-traceback precedent** (932-939) for the `exc_info=True` form:
```python
        except Exception:
            logger.warning(
                "Could not write owed job records before stopping; the next "
                "startup's recovery ends those rows",
                exc_info=True,
            )
```

**Imports** (18-43): relative; add `ScanCancelledError` to `from .exceptions import ConfigError`.

---

### `src/saneless/pipeline.py` (service, orchestration)

**Analog:** same file.

**Flip match to split** (947-966). The comment at 949-952 names Phase 28 and must be rewritten:
```python
    notify(PipelineEvent.AWAITING_FLIP)
    outcome = flip.coordinator.wait_for_flip(flip.timeout)
    # A match with assert_never rather than an if-chain: a fourth FlipOutcome
    # member then fails ty and pyrefly at edit time instead of falling through
    # into pass B.  These are plain ScanErrors on purpose (D-15) -- classifying
    # an abort as a cancellation rather than a failure is Phase 28's EXC-04.
    match outcome:
        case FlipOutcome.CONTINUED:
            pass
        case FlipOutcome.ABORTED:
            msg = "Manual duplex scan aborted at the flip prompt"
            raise ScanError(msg)
        case FlipOutcome.TIMED_OUT:
            msg = (
                f"Manual duplex flip wait timed out after {flip.timeout:g} "
                "seconds: nobody confirmed the stack was flipped"
            )
            raise ScanError(msg)
        case _:
            assert_never(outcome)
```
`_scan_manual_duplex`'s docstring `Raises:` (926-930) must list `ScanCancelledError`.

**`FlipCoordinator` ABC** (116-151): add a concrete (non-abstract) property with a `None` default so
`WorkerFlipCoordinator`, `tests/test_pipeline.py::_FixedFlipCoordinator` (530) and other stubs need no change.
The ABC's docstring explains ABC vs Protocol; keep it.

**Precondition helper shape to copy for `_require_pages`** (`_check_disk_space`, 315-337):
```python
def _check_disk_space(tmp_dir: str, min_free_mb: int) -> None:
    """
    Raise ScanError if insufficient disk space in tmp_dir.

    Args:
        ...

    Raises:
        ScanError: If free space is below the required threshold.

    """
    ...
    if free_mb < min_free_mb:
        msg = (...)
        raise ScanError(msg)
```
Call sites: after `scanner.scan_pages` in `_scan_simplex` (1048), after pass A (938, before
`notify(PipelineEvent.AWAITING_FLIP)` at 947) and after pass B (970, before the count comparison at 979).

**Reword** (`_drop_empty_pages`, 504-506; docstring `Raises:` at 489-490):
```python
    if not filtered:
        msg = "All pages were detected as empty"
        raise ScanError(msg)
```

**Assembly call sites** (`_deliver_duplex_mismatch`, 734-745) stay; `assemble_pdf` now raises `PdfError`. The
`Raises:` sections at 723-726 and in `run_pipeline` should name `PdfError`.

**"No scanner found"** (`_resolve_device`, 1077-1080) stays a `ConfigError`; the CLI guard now maps it to 2.

**Imports** (22-31): absolute `from saneless.exceptions import (...)`; add `ScanCancelledError`.

---

### `src/saneless/pdf.py` (utility, file-I/O / transform)

**Analog:** `_preserving` in `pipeline.py:397-405, 442-471` (narrow span, broad type, always chained).

**Docstring justification to mirror** (`pipeline.py:397-405`):
```
    **The caught type is ``Exception``, deliberately.** The guard is narrow in
    *span* -- it covers the upload and the poll and nothing else -- and broad
    in *type*. ... nothing is masked, because the exception is
    always re-raised with the original chained on ``__cause__``.
    ``KeyboardInterrupt`` and ``SystemExit`` derive from ``BaseException`` and
    pass through untouched.
```
RESEARCH measured img2pdf raising bare `Exception`, `TypeError`, `ValueError` and Pillow raising `SystemError`,
so a tuple cannot satisfy D-04.

**Body to wrap** (177-202):
```python
    output_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(dir=str(output_dir)) as tmp_dir:
        image_paths: list[str] = []
        for i, img in enumerate(images):
            img_path = Path(tmp_dir) / f"page_{i:04d}.png"
            img.save(str(img_path), format="PNG")
            ...
        pdf_bytes = img2pdf.convert(
            image_paths,
            layout_fun=img2pdf.get_fixed_dpi_layout_fun((dpi, dpi)),
        )
        if pdf_bytes is None:
            msg = "img2pdf.convert returned None"
            raise RuntimeError(msg)
        pdf_path.write_bytes(pdf_bytes)
```
Empty-list refusal goes before the `try`. A `PdfError` raised inside the `try` would be re-wrapped, so raise it
outside or `except PdfError: raise` first. Add `Raises:` to the docstring (173-176 has none today).
Imports (12-20): add `from saneless.exceptions import PdfError, describe` (absolute, as `pipeline.py` does).

---

### `src/saneless/paperless.py` (service, HTTP request-response)

**Analog:** same file.

**Module imports** (12-22): relative `from .exceptions import PaperlessError, PaperlessTimeoutError`; add
`import json` if catching `json.JSONDecodeError` by name.

**Base-class catch precedent** (`test_connection`, 490-498):
```python
        try:
            response = self._client.get("/api/tags/", params={"page_size": 1})
        except httpx.TransportError:
            # The base class of ConnectError, ConnectTimeout and ReadTimeout.
            # Catching only ConnectError let the timeout siblings escape to
            # routes.py's blanket handler ...
            logger.warning("Paperless is unreachable")
            return ConnectionStatus.UNREACHABLE
```

**Upload retry loop to change** (269-329). Retry tuple at 289, 4xx body at 300-306, exhausted message 328:
```python
            except (httpx.ConnectError, httpx.TimeoutException) as exc:
                last_error = exc
                logger.warning(
                    "Upload attempt %d/%d failed: %s",
                    attempt + 1,
                    self._max_retries,
                    exc,
                )
                if attempt < self._max_retries - 1:
                    time.sleep(2**attempt)

            except httpx.HTTPStatusError as exc:
                if 400 <= exc.response.status_code < 500:
                    msg = (
                        f"Paperless rejected upload: "
                        f"{exc.response.status_code} {exc.response.text}"
                    )
                    raise PaperlessError(msg) from exc
                ...
        msg = f"Upload failed after {self._max_retries} retries"
        raise PaperlessError(msg) from last_error
```
Order: `except httpx.UnsupportedProtocol` before `except httpx.TransportError` (it is a subclass). The
duplicated warning+sleep block (291-298 and 308-315) is a candidate for a small helper. The fallback block
(318-326) stays the path for exhausted retries; `_deliver_to_consume_dir` (331-383) raises raw `OSError`
(docstring 368-371) and is wrapped in `upload_document`.

**Constructor** (203-224): wrap `self._client = httpx.Client(**client_kwargs)` (222) for `httpx.InvalidURL`.
Never include the token (the `Authorization` header is in `client_kwargs`, 214-217).

**Body renderers to reconcile into one** (126-142 and the poll use at 432-437):
```python
_MAX_ERROR_BODY_CHARS = 500
...
def _truncated_body(text: str) -> str:
    if len(text) <= _MAX_ERROR_BODY_CHARS:
        return text
    head = text[:_MAX_ERROR_BODY_CHARS]
    return f"{head}... [truncated, {len(text)} characters total]"
```
The module-constant + private-function style (`_NO_FAILURE_MESSAGE` at 48, `_failure_message` at 97-123) is
the shape for the new renderer and the duplicate-detection helper. `_failure_message` shows the v9/v10
both-shapes tolerance to reuse for `duplicate_of` in `result_data`:
```python
    result = task.get("result")
    if isinstance(result, str) and result:
        return result
    data = task.get("result_data")
    if isinstance(data, dict):
        for key in ("error_message", "reason"):
            ...
```

**Poll loop** (424-462): wrap only `self._client.get(...)` (428-431); fall through to the existing
`remaining`/sleep logic (452-462) so the monotonic deadline still governs; append the last transport error
to the `PaperlessTimeoutError` message at 455. `response.json()` at 439 needs a decode catch.

**get_tags / get_correspondents** (509-539): `raise_for_status()` + `.json()` unguarded today; wrap both with
`except httpx.HTTPError` and JSON decode → `PaperlessError`. `routes.py` has no typed catch for these
(SWP-04 is Phase 32).

---

### `src/saneless/scanner/sane_backend.py` (adapter, device I/O)

**Analog:** `_acquire_pages` translation (same file, 731-743).

**Translate-and-chain pattern** (737-743):
```python
            except ScanError:
                # FeederEmptyError subclasses ScanError, so saneless's own
                # errors -- including the timeout path's -- propagate here.
                raise
            except Exception as exc:
                scan_error_msg = f"Scanner error on page {page_num + 1}: {exc}"
                raise ScanError(scan_error_msg) from exc
```
Apply `describe(exc)` in place of `{exc}` only for the empty-message rule; the wording is Phase 24's (keep).

**Lazy import to wrap, not replace** (43-56). The existing `# noqa` lines are pre-existing; do not add new ones:
```python
sane: Any = None


def _ensure_sane() -> None:
    """Import the real sane module on first use."""
    global sane  # noqa: PLW0603
    if sane is None:
        import sane as _sane  # noqa: PLC0415

        sane = _sane
```
`require_sane()` sits next to it and catches `ImportError` (covers `ModuleNotFoundError`). `SaneBackend.__init__`
calls `_ensure_sane()` at 1047.

**Call sites to wrap (no try today):** `sane.init()` 1060; `sane.open(device_id)` 1078 in `_open_device`
(1063-1084, whose `finally` suppresses `cancel()` errors with `contextlib.suppress(Exception)` at 1082-1084;
keep `close()` from masking the body's exception); `sane.get_devices()` 1094; `dev.get_options()` 1123, 1203;
option assignments in `_configure_device` 992-997:
```python
    if has_source_option:
        dev.source = effective_source
    dev.mode = settings.mode
    dev.resolution = settings.resolution

    actual_resolution = int(dev.resolution)
```
One try per assignment so D-08 names the option. `_configure_device` does not receive the device name; watch
PLR0913 (the `_FlipContext` record at `pipeline.py:243-267` is the house fix for the argument limit).
Flatbed `dev.start()` / `dev.snap()` at 1257-1258. **Keep** `_set_geometry`'s broad catch (536-544, crop
fallback) and the feeder-empty/all-unreadable raises (775-787, Phase 24 D-03).

**Imports** (28): `from saneless.exceptions import FeederEmptyError, ScanError`; add `ConfigError`, `describe`.

---

### `src/saneless/config.py` (config loader, file-I/O)

**Analog:** `_build_settings` (974-1025).

**Header renderer** (1013-1025):
```python
    if settings is not None and not lines:
        return settings
    lines.sort()
    header = (
        f"Configuration error in {toml_file}:"
        if toml_file is not None
        else "Configuration error (defaults and environment):"
    )
    msg = "\n".join([header, *(f"  {line}" for line in lines)])
    # from None, and raised outside the except block: a chained ValidationError
    # would print its inputs -- possibly the token -- in any traceback
    # (Pitfall 1, D-14).
    raise ConfigError(msg) from None
```
The construction `try` (1006-1012) is where `except tomllib.TOMLDecodeError`, `except UnicodeDecodeError` and
`except OSError` go, each appending one line. `ValidationError` keeps `from None`; the three new causes can be
chained (RESEARCH Open Question 4) by storing the exception and raising `from cause`. Never render
`TOMLDecodeError.doc` (holds the token). The docstring sentence at 984-985 ("A TOML syntax error ... propagates
unchanged (Phase 28)") must be corrected. Order: `UnicodeDecodeError` is a `ValueError`, as is
`TOMLDecodeError`; neither subclasses the other, so order is free, but `OSError` must not be caught before
anything narrower that matters.

**Key escaping** (593-608): `_escape_name(name)` is `repr(name)[1:-1]`; use it for any key path in a tomllib
message.

**`load_settings` Raises** (1133-1136) gains the three causes. Import `tomllib` at the top (11-17 block).

---

### `src/saneless/auto_profiles.py` (config writer, file-I/O)

**Analog:** `_read_config` (624-649), `_render_checked` (718-732).

**Catch pattern to extend** (644-649):
```python
    try:
        text = config_path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        msg = f"{config_path} is not valid UTF-8; refusing to rewrite it"
        raise ConfigError(msg) from None
    return tomlkit.parse(text), text
```
Wrap `tomlkit.parse(text)` (649) with `except tomlkit.exceptions.ParseError as exc` using `exc.line`/`exc.col`,
message prefixed `Cannot update {config_path}:` as at 726-730. Docstring `Raises:` (635-636) gains the
parse case. `worker.py:830-856` already catches `ConfigError` from this path.

---

### `src/saneless/logging_config.py` (config)

**Analog:** itself (55-68). The fallback silently swaps the file handler for stderr, so
"Full details in <log_file>" (D-06) can be false:
```python
    try:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(...)
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)
    except OSError:
        stderr_handler = logging.StreamHandler(sys.stderr)
        ...
        root_logger.warning("Cannot write to %s, logging to stderr only", log_file)
```
If changed to return `bool`, `cli.py:221-227` stores it in `ctx.obj`, and `tests/test_cli.py` patches
`saneless.cli.configure_logging` with `lambda *_a, **_kw: None` in many places (105-109, 340); a `None` return
must stay tolerable or those stubs change.

---

### `src/saneless/web/templates/partials/status.html` (component)

**Analog:** FALLBACK branch, lines 21-26 (non-alert, with the history-reload div):
```html
    {% elif job.state == JobState.FALLBACK %}
      <p class="status-fallback">&#8594; Saved to folder: {{ job.title }}</p>
      {% if job.warning %}
      <p class="status-fallback">{{ job.warning }}</p>
      {% endif %}
      <div hx-get="/api/jobs/history" hx-target="#history-body" hx-swap="outerHTML" hx-trigger="load" class="htmx-hidden"></div>
```
The if-chain (8-30) has no `else`: without a CANCELLED branch a cancelled job renders nothing. No `role="alert"`
(a cancel is not a failure; ERROR at 28 carries it).

### `src/saneless/web/templates/partials/history.html` (component)

**Analog:** line 7:
```html
  <td class="{% if job.state == JobState.DONE %}status-done{% elif job.state == JobState.FALLBACK %}status-fallback{% elif job.state == JobState.ERROR %}status-error{% endif %}">
```

### `src/saneless/web/static/app.css` (style)

**Analog:** Pico tokens used directly (19-26); the app-owned pair (28-56) is only for colours Pico lacks:
```css
.status-done {
    color: var(--pico-ins-color, green);
}

.status-error {
    color: var(--pico-del-color, red);
}
```
RESEARCH recommends `.status-cancelled { color: var(--pico-muted-color); }`, no light/dark duplication
(UI-SPEC convention: Pico tokens that already swap per scheme are used directly). If an app-owned pair is
chosen instead, copy the three-block structure at 34-52 including the "Change one, change the other" comments.

---

### `docs/how-to/troubleshoot-a-failed-scan.md` (new doc) and `mkdocs.yml`

**Analog (format):** `docs/how-to/scanner-host-discovery.md` lines 1-3 (H1 + one-sentence purpose) and 70-95
(bold symptom headings with bullet causes and fenced commands):
```markdown
## Troubleshooting

**Scanner not found**

- Verify `saned` is running on the scanner host: `systemctl status saned.socket` (or `saned` service)
...
```
Structure per D-13: exit-code table first, then symptom sections. Written by symptom, not by exact message
string (Phase 30 rewords them).

**Nav** (`mkdocs.yml`, How-To Guides block, 44-50):
```yaml
  - How-To Guides:
    - Install on Bare Metal: how-to/install-bare-metal.md
    ...
    - Use the CLI for Scripting: how-to/cli-scripting.md
```

### Exit-code tables (`docs/how-to/cli-scripting.md`, `docs/reference/cli-commands.md`)

**Analog:** `cli-scripting.md` "## Exit codes" table:
```markdown
| Exit Code | Meaning | Example |
|---|---|---|
| 0 | Success | Scan completed and uploaded |
| 1 | Scan or runtime error | Scanner disconnected mid-scan, no pages scanned |
| 2 | Configuration or profile error | Unknown profile name, ... |
| 3 | Paperless upload error | paperless-ngx unreachable, invalid API token |
```
`cli-commands.md` uses a two-column `| Code | Meaning |` form per command at 31-38 (scan), 57-61 (devices),
100-104 (jobs), 121-126 (serve), 142-148 (auto-profiles). The warning admonition in `cli-scripting.md`
("exits with code 1" for an abort) and `cli-commands.md:36, 40` change to 130. Row format must match what the
doc-truth test parses (see `_table_row` below: rows start with `| <first cell> |`).

The remaining doc edits have exact file:line targets in RESEARCH.md § "Doc Surfaces to Change (exact)";
no further analog is needed.

### `.planning/UI-SPEC.md`

**Analog:** "Measured status contrast (Chromium-computed, both schemes)" table (152-163) and convention rule 5
(183: every app-owned colour carries a measured ratio for both schemes). Add `.status-cancelled` rows with the
Playwright-measured values. Status state table row for FALLBACK at 259 is the row format for CANCELLED.

---

### `tests/test_vocabulary.py` (test)

**Analog:** itself.
- Count guard to bump (59-68): `assert len(list(JobState)) == 9`.
- Terminal membership (154-159): `frozenset({JobState.DONE, JobState.ERROR, JobState.FALLBACK}) == TERMINAL_STATES`.
- ErrorCategory name set (87-94).
- `state_label` parametrised table (178-194), `progress_label` terminal prose (229-233).
- `classify_error` one-test-per-class plus ordering test (708-751):
```python
    def test_feeder_empty_wins_over_its_scan_error_base(self) -> None:
        """FeederEmptyError is checked before its ScanError base class (CTR-05)."""
        assert issubclass(FeederEmptyError, ScanError)
        assert classify_error(FeederEmptyError("no paper")) is ErrorCategory.FEEDER
```
- Unrecognised-value `AssertionError` tests (673-705) with `cast("JobState", "UNKNOWN")`: copy for
  `exit_code_for(cast("ErrorCategory", "UNRECOGNISED"))`.
- A total test over `ExitCode` / `ErrorCategory` should be `@pytest.mark.parametrize("category", list(ErrorCategory))`
  as at 266-269.

### `tests/test_cli.py` (test, CliRunner)

**Analog:** `_patch_cli` (81-192) and the scan error tests.

**Patch seams** (105-112, 165-168, 190): `saneless.cli.load_settings`, `saneless.cli.configure_logging`,
`saneless.cli.SaneBackend`, `saneless.cli.PaperlessClient`. Add `saneless.cli.require_sane` to `_patch_cli`
(hermetic; RESEARCH Pattern 3).

**Exit-code test shape** (346-380):
```python
        runner, _ = _patch_cli(monkeypatch, scanner_cls=FailScanner)

        result = runner.invoke(cli, ["scan", "--title", "Test"])
        assert result.exit_code == 1
        assert "Paper jam" in result.output
```
For "exactly one stderr line", read `result.stderr` (click 8.3 separates it).

**Tests this phase must rewrite (they pin today's behaviour):**
- 331-344 `test_scan_config_error`: raises bare `ValueError` from `load_settings` and expects exit 2
  "Configuration error"; under D-06 that is exit 5. Rewrite to raise `ConfigError` (2) and add a separate
  unexpected-error (5) test.
- 585-608, 610-638: abort tests assert `exit_code != 0` and slice from `"Scan error"`; become exit 130 and the
  cancel line.
- 686-725 WR-08: keep `FlipOutcome.ABORTED` + logged traceback (the `caplog` + `records[0].exc_info is not None`
  pattern at 708-725), add the cause assertion, and a CliRunner case asserting exit 1.
- Ctrl-C seam without killing pytest (640-674): make `coordinator._slot.wait` raise `KeyboardInterrupt`. For
  a command-level Ctrl-C, raise it inside the command via a stub scanner so it stays within `CliRunner.invoke`
  (RESEARCH Pitfall 5).
- `TestServeCommand` (1525+): `_capture_uvicorn` patches `saneless.cli.uvicorn.run` (1535-1553); the bind test
  patches `saneless.cli.socket.socket` (1532). Serve's bind exit becomes 2.

### `tests/test_worker.py` (test, event-driven)

**Analog:** abort tests and `_worker_records`.

**Abort test to flip to CANCELLED** (1201-1229):
```python
            wait_for_state(store, job.id, JobState.AWAITING_FLIP, _STATE_BUDGET)
            worker.abort_flip(job.id)
            finished = wait_for_state(store, job.id, TERMINAL_STATES, _STATE_BUDGET)
        finally:
            scanner.release_pass_b.set()
            worker.stop()
            store.close()

        assert finished.state is JobState.ERROR
        assert finished.error is not None
        assert "flip prompt" in finished.error
```
Also 1840-1873 (`operator abort while stopping`) asserts `JobState.ERROR` and `error_category is not None`;
becomes CANCELLED. 1709-1744 and 1748-1801 (shutdown → `RESTART_REASON` ERROR) must stay green.

**Log record filter helper** (1988-1998), reuse for EXC-05 `exc_info` assertions:
```python
def _worker_records(
    caplog: pytest.LogCaptureFixture, level: int, text: str
) -> list[logging.LogRecord]:
    """Return the ``saneless.worker`` records at ``level`` whose message has ``text``."""
    return [
        record
        for record in caplog.records
        if record.name == "saneless.worker"
        and record.levelno == level
        and text in record.getMessage()
    ]
```
Degraded-health tests (3194-3309) are the analog for "a cancel never counts toward degraded".

### `tests/test_pipeline.py` (test)

**Analog:** itself.
- 406-421 `test_all_pages_empty_raises`: `match="All pages were detected as empty"` → "All pages were blank".
  Its `MagicMock(spec=ScannerBackend)` + `scan_batch([...])` setup (415-418) is the shape for the empty-batch
  tests (`scan_batch([])`, detection on and off).
- 895-921 abort test: `pytest.raises(ScanError, match="flip prompt")` → `ScanCancelledError`; add an
  `abort_cause` variant expecting `ScanError`. `_FixedFlipCoordinator` (530-552) is the stub to extend with a
  cause.

### `tests/test_pdf.py` (test, file-I/O)

**Analog:** `test_temp_files_cleaned_on_error` (96-128), which must change from `RuntimeError` to `PdfError`:
```python
        monkeypatch.setattr(
            pdf_mod,
            "img2pdf",
            type(
                "FakeImg2Pdf",
                (),
                {
                    "convert": staticmethod(fake_convert),
                    "get_fixed_dpi_layout_fun": staticmethod(fake_layout_fun),
                },
            )(),
        )

        img = Image.new("RGB", (100, 100), "white")
        with pytest.raises(RuntimeError, match="fake img2pdf error"):
            pdf_mod.assemble_pdf([img], tmp_path, filename="boom.pdf", dpi=300)
```
Patching the whole `img2pdf` name replaces the exception classes too; for the seven parametrised rows, raise
instances of the real `img2pdf.<Class>` from a patched `pdf_mod.img2pdf.convert` (RESEARCH Code Examples) and
assert `__cause__ is original`. File has no `from __future__ import annotations` and no `TYPE_CHECKING` block
(1-16); keep its import style.

### `tests/test_paperless.py` (test, MockTransport)

**Analog:** itself.
- Handler + transport (33-37, 164-167, 287-309):
```python
        def handler(_request: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            if call_count["n"] <= 2:
                msg = "connection refused"
                raise httpx.ConnectError(msg)
            return httpx.Response(200, json="task-id-ok")
```
- Exception-class parametrisation (141-145):
```python
_CONNECTION_EXCEPTION_CASES = [
    pytest.param(httpx.ConnectError, id="connect-error"),
    pytest.param(httpx.ConnectTimeout, id="connect-timeout"),
    pytest.param(httpx.ReadTimeout, id="read-timeout"),
]
```
- Must change: 330-346 `match="retries"` → attempts wording; 505-518 asserts `"truncated"` and `< 1000` chars
  (new one-line ~200-char renderer).
- Retry tests really sleep (`time.sleep(2**attempt)`, ~3 s per exhausted test; no autouse sleep patch in
  `tests/conftest.py`). New multi-row retry parametrisations should patch `saneless.paperless.time.sleep` or
  use `max_retries=1`/`2` to keep the suite under the 60 s timeout.
- `_poll_client` (89-97) for poll continuation tests.

### `tests/test_scanner.py` + `tests/fake_sane.py` (test + fixture)

**Analog:** cause-chaining test (656-667):
```python
        original = FakeSaneError("Document feeder jammed")
        dev = FakeSaneDev(pages=5, start_error=original)
        backend = _backend_with(dev, monkeypatch)

        with pytest.raises(ScanError) as exc_info:
            backend.scan_pages("test:0", _feeder_settings())

        assert exc_info.value.__cause__ is original
```
- Must change: 392-407 expects raw `FakeSaneError` from a flatbed `start()`; becomes `ScanError` with `__cause__`.
- The option-assignment fidelity table (2176-2214) already records which option raises which type
  (`FakeSaneError`, `TypeError`, `AttributeError`); the wrapping test can parametrise over the same rows.
- `FakeSaneModule` (1017-1085) has no failure seams for `init()`, `open()` or `get_devices()`; add keyword
  arguments in the style of `FakeSaneDev(start_error=..., start_error_page=...)`. `FakeSaneError` (543-551)
  docstring explains why it subclasses `Exception` directly; keep that.

### `tests/test_config.py`, `tests/test_auto_profiles.py` (test)

**Analog:** `TestInvalidToml` (384-391), which must change (`ConfigError` is not a `ValueError`):
```python
    def test_invalid_toml(self, tmp_config_dir: Path) -> None:
        """Malformed TOML raises an exception during loading."""
        bad_file = tmp_config_dir / "bad.toml"
        bad_file.write_text("this is not [valid toml\n===broken===")
        with pytest.raises(ValueError, match=r"(?i)invalid|expected|toml"):
            load_settings(config_path=str(bad_file))
```
Token-absence helper (761-766) asserts `__cause__ is None`, which is correct for `ValidationError` only; a
TOML-syntax secret-absence test needs a variant that allows a `TOMLDecodeError` cause but asserts the token is
absent from `str(err)` and `repr(err)`. `_load_error(config_file, toml_content)` (573+) writes and loads in one
call. Produce `TOMLDecodeError` by parsing real bad text (RESEARCH Pitfall 4).

### `tests/test_deployment_config.py` (doc-truth test)

**Analog:** helpers and the exit-code section test.

Path constants (27-46), reader and row finder (187-189, 259-263):
```python
CLI_REFERENCE = DOCS_DIR / "reference" / "cli-commands.md"
CLI_SCRIPTING = DOCS_DIR / "how-to" / "cli-scripting.md"
...
def _read(path: Path) -> tuple[str, Path]:
    """Return a file's text and its repo-relative name for failure messages."""
    return path.read_text(encoding="utf-8"), path.relative_to(REPO_ROOT)
...
def _table_row(text: str, first_cell: str) -> str:
    """Return the Markdown table row whose first cell is ``first_cell``."""
    rows = [line for line in text.splitlines() if line.startswith(f"| {first_cell} |")]
    assert rows, f"no table row starting with {first_cell}"
    return rows[0]
```
Section-scoped assertion (323-330):
```python
def test_scripting_exit_code_two_examples() -> None:
    """The scripting how-to's exit-code section lists the new exit-2 causes."""
    text, name = _read(CLI_SCRIPTING)
    _, heading, rest = text.partition("## Exit codes")
    assert heading, f"{name} has no '## Exit codes' section"
    section = rest.split("\n## ", 1)[0]
    for needle in ("unknown config key", "SANELESS_"):
        assert needle in section, f"{name}: exit-code section lacks {needle!r}"
```
New test iterates `ExitCode` and asserts `_table_row(section, str(int(code)))` exists in both tables. Update the
module docstring (1-20), which lists the phases it pins. Add a constant for the new how-to and assert
`mkdocs.yml` names it.

### `tests/test_web_state_rendering.py` (test)

**Analog:** parametrised over `list(JobState)`, so CANCELLED is exercised automatically; the explicit
per-state assertions need a CANCELLED line.
- 209-216: add `assert ('<td class="status-cancelled">' in text) is (state is JobState.CANCELLED)`; docstring
  "Only the three terminal history cells" becomes four.
- 249-265: add the CANCELLED status-line assertion and a `role="alert"` absence check, copying the FALLBACK
  block at 256-262. The `hx-get="/api/jobs/history"` check at 265 already keys on `TERMINAL_STATES`.
- `_job_in_state` (179-188) writes `error="disk on fire"` via `update_state`; reuse it for CANCELLED.

### `tests/test_browser.py` (Playwright)

**Analog:** `TestFallbackRendering`.
- Fixture (1028-1072): create the job, `job_store.finish_job(job.id, JobState.FALLBACK, result=...)`, set
  `app.state.worker._current_job_id`, and on teardown clear the pointer **and** `job_store.delete_job(job.id)`
  (the comment at 1063-1070 explains why both). A `cancelled_page` fixture passes
  `JobState.CANCELLED, error=...` instead of a result.
- `_goto` (1074-1078) waits for `#status-area .status-fallback`.
- Probe (774-787) iterates `["status-done", "status-error", "status-fallback"]`; add `status-cancelled`. The
  distinctness assertion at 1111 (`len(set(colours.values())) == 3`) becomes 4.
- AA contrast parametrisation (1214-1240): extend the `cls` list and `Literal` type.
- Forced dark (1242-1255): add an assertion that `status-cancelled` resolves to Pico's dark muted value.
- Scan-button re-enable (1120-1144) and history refresh (1146-1166) tests are the templates for CANCELLED.

## Shared Patterns

### Build the message, then raise from the cause
**Source:** `src/saneless/scanner/sane_backend.py:741-743`, `src/saneless/paperless.py:300-306`,
`src/saneless/pipeline.py:462-471`
**Apply to:** every translation site in `sane_backend.py`, `paperless.py`, `pdf.py`, `config.py`,
`auto_profiles.py`, `pipeline.py`
```python
            except Exception as exc:
                scan_error_msg = f"Scanner error on page {page_num + 1}: {exc}"
                raise ScanError(scan_error_msg) from exc
```
Ruff `EM` is active (message in a variable). `TRY`/`BLE` are not selected. Use `describe(exc)` in place of
`{exc}` for D-08's empty-message rule. `from None` only where the cause embeds input values (Phase 27
`ValidationError`, `config.py:1022-1025`).

### Total enum dispatch
**Source:** `src/saneless/vocabulary.py:291-312` (state), `621-658` (enum → int), `402-435` docstring on why
`match` beats a dict
**Apply to:** `JobState.CANCELLED` consumers, new `ErrorCategory` member, `ExitCode` mapping
```python
    match outcome:
        case ScanOutcome.SUCCESS:
            state = JobState.DONE
        case ScanOutcome.FALLBACK:
            state = JobState.FALLBACK
        case _:
            assert_never(outcome)
    return state
```
Run `uv run ty check` and `uv run pyrefly check src tests` after adding members; they list every arm to add.

### Narrow-first ordering for subclass catches
**Source:** `src/saneless/vocabulary.py:665-669` docstring, `src/saneless/scanner/sane_backend.py:737-740`
**Apply to:** `classify_error`, the CLI guard (click exceptions before `Exception`; `ScanCancelledError` and
`KeyboardInterrupt` before `SanelessError`), `paperless.py` (`UnsupportedProtocol` before `TransportError`;
`HTTPStatusError` handled before a final `HTTPError`), `pdf.py` (`PdfError` re-raise before `Exception`).

### Coordinator marks why it answered, under the claim's lock
**Source:** `src/saneless/worker.py:196-199, 242-266`; `FlipAnswerSlot.offer` `src/saneless/pipeline.py:184-202`
**Apply to:** `ClickFlipCoordinator` prompt-failure cause, `FlipCoordinator` default property, pipeline flip match.

### Logging with the real exception, no f-strings
**Source:** `src/saneless/cli.py:155` (`logger.exception(...)` inside except), `src/saneless/worker.py:935-939`
(`exc_info=True`)
**Apply to:** worker failure ending (EXC-05), CLI guard exit 5, paperless DEBUG body logging. A cancel logs at
INFO with no `exc_info`. Before logging is configured, do not call `logger.exception` in the CLI (lastResort
prints the traceback).

### Docstrings cite finding IDs and explain why
**Source:** `src/saneless/worker.py:941-962`, `src/saneless/pipeline.py:397-405`, `src/saneless/cli.py:83-108`
**Apply to:** every new/changed function. Google style `Args:`/`Returns:`/`Raises:` with a trailing blank line
before the closing quotes (see `vocabulary.py:274-290`). D203/D212 ignored (summary on the line after `"""`).

### CANCELLED is terminal, non-alert, muted
**Source:** FALLBACK precedent across `vocabulary.py:253-255`, `status.html:21-26`, `history.html:7`,
`app.css:19-56`, `tests/test_browser.py:1028-1166`, `tests/test_web_state_rendering.py:209-265`
**Apply to:** every CANCELLED surface. `job.py` needs no change (`is_active`/`is_busy` are set membership;
`state TEXT` has no CHECK constraint per RESEARCH).

## No Analog Found

| File / concern | Role | Data Flow | Reason |
|---|---|---|---|
| `src/saneless/cli.py` `click.Group` subclass overriding `invoke` | controller | request-response | No custom click `Group`/`Command` class exists in the repo. Use RESEARCH.md Pattern 1 (verified against click 8.3.1 with CliRunner). |
| `src/saneless/cli.py` `serve`: uvicorn startup failure → exit 2 | controller | request-response | `uvicorn.run` calls `sys.exit(3)` (a `SystemExit`, invisible to `except Exception`). No repo code catches `SystemExit` or drives `uvicorn.Server` directly. Options: catch `SystemExit` around `uvicorn.run` and map a non-zero code to `ExitCode.CONFIG`, or build `uvicorn.Config`/`uvicorn.Server` and check `server.started`. Test by stubbing `saneless.cli.uvicorn.run` to `sys.exit(3)` (seam at `tests/test_cli.py:1553`). Ctrl-C already exits 0 (measured). |
| `docs/how-to/troubleshoot-a-failed-scan.md` content | doc | n/a | No symptom-by-exit-code page exists; only per-page `## Troubleshooting` sections. Format from `scanner-host-discovery.md:70-95`, content from D-07 and D-13. |

## Metadata

**Analog search scope:** `src/saneless/` (all modules, `scanner/`, `web/templates/partials/`, `web/static/`),
`tests/` (cli, worker, pipeline, pdf, paperless, scanner, fake_sane, config, vocabulary, deployment_config,
web_state_rendering, browser), `docs/how-to/`, `docs/reference/`, `mkdocs.yml`, `.planning/UI-SPEC.md`
**Files scanned:** 29
**Pattern extraction date:** 2026-09-15
