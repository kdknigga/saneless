# Phase 29: Geometry, Memory, and Timeouts - Pattern Map

**Mapped:** 2026-09-15
**Files analyzed:** 22 (2 new source/test files, 9 modified source files, 11 modified test files; docs excluded)
**Analogs found:** 20 / 22

This phase is a refactor of code that already exists. Almost every new function has a direct
predecessor in the same file, so the dominant instruction to the planner is *"rewrite this named
thing, keeping its docstring density, its error-translation shape and its comment register"* — not
*"write something new in this style"*.

---

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|-------------------|------|-----------|----------------|---------------|
| `src/saneless/spool.py` **(NEW)** | utility module, pipeline-owned | file-I/O + transform | `src/saneless/atomic_write.py` (module shape, OSError→SanelessError) + `src/saneless/pages.py` (Pillow stats) + `ScanBatch` in `scanner/base.py` (frozen record) | composite — no single exact |
| `src/saneless/scanner/base.py` | contract / model | contract | itself: `ScanBatch` (`:194-230`), `ScannerBackend` (`:233-278`); `FlipCoordinator` (`pipeline.py:118-178`) for the ABC-vs-Protocol rule | exact (in-file) |
| `src/saneless/scanner/sane_backend.py` — `_acquire_with_timeout`, `_Slot`, `_cancel_and_settle` | service / device adapter | streaming, blocking-call bounded | `_next_page_with_timeout` (`:662-698`) — the function being replaced | exact |
| `src/saneless/scanner/sane_backend.py` — wedge flag + module init guard + `shutdown()` | module-global lifecycle | process lifecycle | `_ensure_sane` / `require_sane` (`:50-92`) — the only existing module-global mutable state here | role-match |
| `src/saneless/scanner/sane_backend.py` — `_acquire_pages` → sink, flatbed timeout | service | streaming | `_acquire_pages` (`:701-827`), `_snap_flatbed` (`:1095-1128`) | exact |
| `src/saneless/pdf.py` — `assemble_pdf(records, …)` | service | batch transform, file-I/O | `assemble_pdf` (`:128-239`) — same function, same `except Exception → PdfError` boundary | exact |
| `src/saneless/pipeline.py` — partial-scan preservation | orchestration | request-response + failure path | `_preserving` (`:473-560`) + `_handle_duplex_mismatch` (`:773-882`) | exact |
| `src/saneless/pipeline.py` — `_interleave_duplex`, `_drop_empty_pages`, `_DuplexMismatch`, spool lifetime | orchestration | batch transform | the same four symbols today (`:959-988`, `:563-598`, `:663-685`, `:1286-1404`) | exact |
| `src/saneless/pipeline.py` — `_warn_if_failed_dir_growing` counts directories | utility | file-I/O, never-raises | itself (`:429-470`) | exact |
| `src/saneless/pages.py` — stats-based `is_empty_page`, cheaper thumbnail | utility | transform | itself (`:28-63`, `:93-127`) | exact |
| `src/saneless/cli.py` — 4 × backend close | entry point | lifecycle | `cli.py:761-765` (the one `SaneBackend(...)` already wrapped in error handling) | role-match |
| `src/saneless/web/app.py` — lifespan close | app factory | lifecycle | `lifespan` shutdown branch (`:119-136`) | exact |
| `pyproject.toml` — declare `pikepdf` | config | — | the `dependencies` list (`img2pdf>=0.6.3` line) | exact |
| `tests/test_spool.py` **(NEW)** | test | unit | `tests/test_pages.py` / `tests/test_pdf.py` structure; `tests/test_atomic_write.py` for OSError-path tests | role-match |
| `tests/fake_sane.py` — Event-gated blocking read | test double | event-driven | `set_page_delay` (`:779-796`) + `start`/`snap` (`:1013-1073`) | exact |
| `tests/conftest.py` — `spooling()` factories, `StubScannerBackend` base | test seam | factory | `scan_batch()` (`:165-192`) + `AlwaysContinueFlipCoordinator` (`:195-224`) | exact |
| `tests/test_scanner.py` (105 sites) | test | unit | `TestSaneBackendPerPageTimeout` (`:1426-1454`), fixtures (`:270-305`) | exact |
| `tests/test_pipeline.py` (~50 sites, 36 mocks) | test | unit | `MagicMock(spec=ScannerBackend)` blocks (`:94`, `:360`, `:391`, …) | exact |
| `tests/test_worker.py`, `test_cli.py`, `test_browser.py` stub scanners | test double | gated | `_PassBGatedScanner` (`test_worker.py:1111-1167`) | exact |
| `tests/test_web.py`, `test_web_errors.py`, `test_cross_origin.py`, `test_web_state_rendering.py`, `test_app_lifespan.py` stubs | test double | one-page stub | `_StubScanner` (`test_app_lifespan.py:57-76`) | exact |
| Subprocess process-exit proof (in `tests/test_scanner.py`) | test | subprocess | `_NAMESPACE_SCRIPT` + `_run_in_mount_namespace` (`test_atomic_write.py:~645-703`) | role-match |
| `docs/**` (6 files) | docs | prose | — | **no analog** (see last section) |

---

## Pattern Assignments

### `src/saneless/spool.py` (NEW — utility module, file-I/O + transform)

No single analog. Copy its three halves from three places.

**A. Module shape and docstring register — from `src/saneless/atomic_write.py:1-31`**

This is the repo's closest existing "small module that owns one filesystem primitive" and shows the
exact header order: prose docstring naming the finding/decision IDs, `from __future__`, stdlib
imports, relative `from .exceptions import …`, `__all__`, `logger`.

```python
"""
Durable, all-or-nothing replacement of a config file (M-10, CFG-08).

``replace_file_atomically`` is the one primitive saneless uses to rewrite a
file the operator owns. It writes the new text beside the real file, fsyncs
it, and renames it into place, so a crash, a full disk, or a killed process
leaves either the old file or the new one -- never a truncated config
(D-05). ...

The module knows nothing about TOML: callers produce the text, this module
only makes the write durable.
"""

from __future__ import annotations

import contextlib
import errno
import logging
import os
...
from .exceptions import ConfigError

__all__ = ["replace_file_atomically"]

logger = logging.getLogger(__name__)
```

The closing sentence is the pattern to imitate for `spool.py`: *"the module knows nothing about
TOML"* → *"the spool knows nothing about SANE: the backend hands it one image at a time, this module
only decides where it lands and measures it."* That sentence is what encodes D-01's ownership split.
Also record PNG level 1 as the throughput fallback here (D-03), as a docstring line, not a config key.

**B. `PageRecord` — from `ScanBatch` in `src/saneless/scanner/base.py:194-230`**

The only frozen dataclass in the codebase, and its docstring states *why* it is frozen rather than
inheriting the choice. `PageRecord` is the same kind of object (a report of what already happened) and
should state the same, plus D-02's "facts, never verdicts".

```python
@dataclass(frozen=True)
class ScanBatch:
    """
    The pages one acquisition produced, and what the device actually did.
    ...
    ``frozen=True`` is a departure from the plain ``@dataclass`` used by
    ``DeviceInfo``, ``DeviceCapabilities``, ``ScanSettings`` and
    ``pipeline.ScanResult``. There is no frozen precedent in this codebase, so
    the choice is stated rather than inherited: this is a report of what a
    device has already done, and nothing downstream has any business rewriting
    it afterwards.

    Attributes:
        pages: The acquired pages, in the order the device produced them.
        actual_resolution: ...
        pages_rejected: ...
    """

    pages: list[Image.Image]
    actual_resolution: int
    pages_rejected: int
```

Note the second frozen-dataclass precedent for a record with a *named-field-beats-tuple* rationale —
`_DuplexMismatch` (`pipeline.py:663-685`): *"A named record rather than the bare ``(fronts, backs)``
tuple this used to be … a four-element tuple would make every call site remember an order."*

**C. Greyscale stats and thumbnail — from `src/saneless/pages.py:52-55` and `:114-119`**

The exact two lines that move to spool time (D-02/D-06), and the `.info.pop("exif", None)` call that
must survive the move:

```python
# pages.py:52-55 -- this convert+Stat pair moves to spool time, once per page
gray = image.convert("L")
stats = Stat(gray)
mean_val = stats.mean[0]
stddev_val = stats.stddev[0]
```

```python
# pages.py:114-119 -- the 26 MB copy research Finding "Don't Hand-Roll" replaces
# with ImageOps.contain(image, (300, 300), Resampling.LANCZOS)
thumb = image.copy()
# Strip EXIF to avoid img2pdf/viewer orientation issues (Pitfall #5)
thumb.info.pop("exif", None)
thumb.thumbnail((max_edge, max_edge), Resampling.LANCZOS)
buf = io.BytesIO()
thumb.save(buf, format="JPEG", quality=quality)
encoded = base64.b64encode(buf.getvalue()).decode("ascii")
```

**D. Per-page free-space check (D-07) — from `pipeline._check_disk_space` (`:342-366`)**

Reuse the message shape verbatim, including the `(configure min_free_space_mb to adjust)` tail, so the
per-page shortfall reads like the up-front one:

```python
usage = shutil.disk_usage(path)
free_mb = usage.free // (1024 * 1024)
if free_mb < min_free_mb:
    msg = (
        f"Insufficient disk space: {free_mb} MB free in {path}, "
        f"{min_free_mb} MB required (configure min_free_space_mb to adjust)"
    )
    raise ScanError(msg)
```

**E. `OSError` must not escape (D-07)** — `atomic_write.py:274-280` is the in-repo shape:

```python
except OSError as exc:
    raise ConfigError(_single_file_mount_message(target)) from exc
```

For the spool the target type is `ScanError`, and the message names the page number and the spool path.

---

### `src/saneless/scanner/base.py` (contract / model)

**Analog:** itself, plus one cross-file rule.

**⚠️ ABC-vs-Protocol conflict the planner must settle explicitly.** The repo states its own rule, in
prose, inside `pipeline.FlipCoordinator` (`pipeline.py:127-132`):

```python
    This is an ``ABC`` and not a ``typing.Protocol`` on purpose, and the rule is
    observable in the tree: ``Protocol`` describes shapes this project does not
    own (``SaneDevice`` for python-sane's handle, ``_SettingsFactory`` for
    pydantic's constructor), while ``ABC`` defines seams the project implements
    itself (``ScannerBackend``).  DPLX-04's lowercase "protocol" means
    "contract", not ``typing.Protocol``.
```

`spool.SpooledPageSink` is a seam **this project implements itself**, so the stated rule points at
`ABC`, while D-01 / RESEARCH.md's Standard Stack row point at `Protocol` ("implemented structurally").
CONTEXT.md leaves the choice to the planner ("whether the sink is a `Protocol` or an ABC"). Whichever
is chosen, the `FlipCoordinator` docstring above is either the rule being followed or the rule being
amended — and if amended, that docstring is a doc-truth site that has to change with it.

**The Protocol shape, if chosen — `SaneDevice` (`sane_backend.py:1131-1157`):**

```python
class SaneDevice(Protocol):
    """Protocol describing the SANE device handle interface."""

    mode: str
    resolution: float
    ...
    def get_options(self) -> list: ...
    def start(self) -> None: ...
    def snap(self) -> Image.Image: ...
```

**The ABC shape, if chosen — `ScannerBackend` (`base.py:233-278`)**, which is also where
`close()` is declared (D-18). Copy `get_capabilities`'s docstring shape for it — `Args:`/`Returns:`
sections, one blank line before the closing `"""`:

```python
class ScannerBackend(ABC):
    @abstractmethod
    def get_capabilities(self, device_id: str) -> DeviceCapabilities:
        """
        Query device capabilities and available options.

        Args:
            device_id: SANE device identifier string.

        Returns:
            Device capabilities including available sources,
            resolutions, and modes.

        """
```

**`scan_pages`'s existing docstring (`base.py:259-278`) is the one to rewrite, not delete.** It
currently argues *why not a generator*; the new one must keep that argument and add *why a sink*
(D-01's rejected alternatives). The `ScanBatch` docstring (`:196-206`) explicitly reserves this
change — *"Phase 29's HARD-01 owns the ordered per-page record design"* — and that reservation
sentence is itself a doc-truth site to replace (Finding 11).

---

### `src/saneless/scanner/sane_backend.py` — `_acquire_with_timeout` (service, bounded blocking call)

**Analog:** `_next_page_with_timeout` (`:662-698`) — the function it replaces.

**The whole current implementation, for the shape of what is kept** (message text, logger call,
`raise ScanError(msg) from exc`) **and what goes** (the executor):

```python
def _next_page_with_timeout(
    executor: ThreadPoolExecutor,
    iterator: Iterator[Image.Image],
    page_num: int,
    timeout_per_page: float,
) -> Image.Image:
    """
    Acquire one page from the ADF iterator under a wall-clock timeout.

    ``signal.alarm`` is not safe in a non-main thread, so the blocking
    ``next(iterator)`` call is submitted to a single-worker executor and
    waited on with a timeout instead.
    ...
    """
    future = executor.submit(next, iterator)
    try:
        return _as_image(future.result(timeout=timeout_per_page))
    except FuturesTimeoutError as timeout_exc:
        logger.error(
            "Page %d timed out after %.0fs",
            page_num + 1,
            timeout_per_page,
        )
        timeout_msg = f"Page {page_num + 1} timed out after {timeout_per_page:.0f}s"
        raise ScanError(timeout_msg) from timeout_exc
```

Keep: `_as_image(...)` normalisation (`:180`), the `Page N timed out after 120s` wording (SC3's test
matches on `"timed out"`), `logger.error` before the raise, `msg`-in-a-variable then
`raise … from …`. Replace: the `ThreadPoolExecutor` parameter with the daemon-thread + `_Slot` +
`Event` of RESEARCH.md Code Example 1, and the docstring's `signal.alarm` paragraph with the
D-11/D-12 rationale (daemon vs non-daemon at interpreter exit; cancel on its own thread).

**The `finally` this replaces — `_acquire_pages` (`:807-811`):**

```python
    finally:
        # Shut down the timeout executor
        executor.shutdown(wait=False)
        # Delete iterator before cancel to avoid __del__ issues (Pitfall #1)
        del iterator
```

`del iterator` is now a **hazard**, not a cleanup: Pitfall 3 says a wedged handle and its iterator must
stay strongly referenced (`_SaneIterator.__del__` calls `device.cancel()`). The wedge record owns those
references on the wedge path, and this `finally` must branch on wedge state.

**Imports to delete** (`:19-20`), which is the grep-able proof the executor is gone:

```python
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
```

**Exception-translation ladder to preserve — `_acquire_pages` (`:771-781`):** the ordering here is
load-bearing and the new sink-based loop must keep it exactly.

```python
            except StopIteration:
                break
            except ScanError:
                # FeederEmptyError subclasses ScanError, so saneless's own
                # errors -- including the timeout path's -- propagate here.
                raise
            except Exception as exc:
                scan_error_msg = (
                    f"Scanner error on page {page_num + 1}: {describe(exc)}"
                )
                raise ScanError(scan_error_msg) from exc
```

That `Scanner error on page N: …` string is the literal prefix CONTEXT.md's D-09 partial-scan message
is built on (`Scanner error on page 40: Document feeder jammed. The 39 page(s) …`), so it must not be
reworded.

**Flatbed (`_snap_flatbed`, `:1121-1128`) — the two calls that go inside the same helper (D-14):**

```python
    try:
        dev.start()
        return dev.snap()
    except Exception as exc:
        if str(exc) == "Document feeder out of documents":
            raise FeederEmptyError(_FEEDER_EMPTY_MESSAGE) from exc
        snap_msg = f"Scanner error on {device_id}: {describe(exc)}"
        raise ScanError(snap_msg) from exc
```

Wrap `lambda: (dev.start(), dev.snap())[1]`-equivalent work in `_acquire_with_timeout` while keeping
this exact-string feeder test and both message shapes.

**Byte-count cheap win (D-06) — replace `_validate_page_image:654`:**

```python
    # Check 2: Minimum file size (raw pixel data)
    raw_size = len(page_image.tobytes())      # <- the 26 MB copy M-08 measured
```
with `page_image.size[0] * page_image.size[1] * len(page_image.getbands())`, and add the mode-`"1"`
caveat (Pitfall 7) to the docstring's existing "Two integrity checks, and only two" paragraph
(`:626-636`), which is the right place because it already explains what the checks are *not* for.

**Constants block (`:100-136`)** is where `_CANCEL_GRACE_SECONDS = 10.0` goes, next to
`_DEFAULT_PAGE_TIMEOUT_SECONDS`. `_MAX_ADF_PAGES`'s comment is the template for a constant that
justifies its own value, and its `WHAT IT DOES NOT BOUND: memory … Bounding memory is Phase 29's
HARD-01/HARD-02` paragraph (`:124-127`) is a doc-truth site this phase must rewrite.

---

### `src/saneless/scanner/sane_backend.py` — init guard, wedge flag, `shutdown()`, `close()`

**Analog:** `_ensure_sane` / `require_sane` (`:50-92`) — the only existing module-global mutable state
in this module, and the seam every test monkeypatches.

```python
sane: Any = None


def _ensure_sane() -> None:
    """Import the real sane module on first use."""
    global sane  # noqa: PLW0603
    if sane is None:
        import sane as _sane  # noqa: PLC0415

        sane = _sane
```

**⚠️ Lint trap.** `PL` is in ruff's `select` (pyproject `[tool.ruff.lint]`), so `global` triggers
`PLW0603` — the existing line carries a `# noqa`, and CLAUDE.md forbids *adding* new ones. Do not copy
the `global` rebinding. Hold the init flag, the host that was used, the lock and the wedge record in a
**module-level mutable container** (a module-level dataclass instance or dict, mutated rather than
rebound), which needs no `global` statement and therefore no suppression.

**`require_sane`'s docstring (`:59-82`) is the template for the guard's docstring:** it explains the
"why once, why here, why this exception type", names the measured failure shapes, and ends with a
`Raises:` section. `shutdown()` needs the same treatment for its two skip conditions (worker not
stopped; backend wedged).

**`SaneBackend.__init__` (`:1182-1202`) — the body that moves behind the guard**, including the
already-existing "host was ignored" logging that D-17 upgrades from INFO to a WARNING naming both hosts:

```python
        require_sane()
        if host and "SANE_NET_HOSTS" not in os.environ:
            os.environ["SANE_NET_HOSTS"] = host
            logger.info("SANE net host discovery configured: %s", host)
        elif host and "SANE_NET_HOSTS" in os.environ:
            logger.info(
                "SANE_NET_HOSTS already set externally (%s), ignoring scanner.host config",
                os.environ["SANE_NET_HOSTS"],
            )
        try:
            self._sane_version = sane.init()
        except Exception as exc:
            # python-sane raises _sane.error, RuntimeError or AttributeError,
            # with no shared base, so the boundary catches Exception (D-08).
            init_msg = f"Could not initialise SANE: {describe(exc)}"
            raise ScanError(init_msg) from exc
        logger.info("SANE initialized, version %s", self._sane_version)
```

The module docstring's line 7 — `- sane.init() called exactly once at construction time (Pitfall #1)`
— and the class docstring's `Calls sane.init() exactly once at construction` (`:1164`) are the two
sentences D-17 makes false.

**`_open_device`'s `finally` (`:1228-1239`) — the one that must learn about the wedge (D-12):**

```python
        try:
            yield dev
        finally:
            with contextlib.suppress(Exception):
                dev.cancel()
            try:
                dev.close()
            except Exception:
                # Logged, never raised: an exception from close() here would
                # replace the one that ended the scan, which is the error the
                # operator needs to see (T-28-16).
                logger.warning("Could not close scanner %s", device_id, exc_info=True)
```

Two things to carry forward: the `logged, never raised` rule is exactly the rule `close()` needs
(Finding 10: "close() must never raise … a raise from a close callback would replace the real error"),
and this is the block that must *skip* both `cancel()` and `close()` when the read never returned.

---

### `src/saneless/pdf.py` — `assemble_pdf(records, …)` (service, batch transform)

**Analog:** `assemble_pdf` itself (`:128-239`).

**The block D-03 deletes (`:208-214`) — the second PNG encode:**

```python
        with tempfile.TemporaryDirectory(dir=str(output_dir)) as tmp_dir:
            image_paths: list[str] = []
            for i, img in enumerate(images):
                img_path = Path(tmp_dir) / f"page_{i:04d}.png"
                img.save(str(img_path), format="PNG")
                image_paths.append(str(img_path))
                logger.debug("Saved page %d to %s", i, img_path)
```

The `TemporaryDirectory(dir=str(output_dir))` **stays** — it becomes the scratch directory for the
one-page PDFs in RESEARCH.md Code Example 2. Same `{i:04d}` naming, `.pdf` instead of `.png`.

**The call that changes (`:216-227`) — keep the comment, it is still true:**

```python
            # The argument is an (x_dpi, y_dpi) 2-tuple, not a scalar:
            # default_layout_fun unpacks it, and an int silently yields wrong
            # geometry.  Without it img2pdf lays pages out at its default_dpi
            # of 96, turning an A4 page at 300 DPI into a 1860 x 2631 pt monster.
            pdf_bytes = img2pdf.convert(
                image_paths,
                layout_fun=img2pdf.get_fixed_dpi_layout_fun((dpi, dpi)),
            )
            if pdf_bytes is None:
                msg = "img2pdf.convert returned None"
                raise PdfError(msg)
            pdf_path.write_bytes(pdf_bytes)
```

Note the `pdf_bytes is None` guard disappears with `outputstream=` (convert returns `None` by
design when streaming) — the planner must not keep it as-is, or every page becomes a `PdfError`.

**The error boundary that covers the new `pikepdf.Job.run()` unchanged (`:229-237`):**

```python
    except PdfError:
        # Already the boundary's own type: wrapping it again would only
        # repeat the message.
        raise
    except Exception as exc:
        msg = (
            f"Could not assemble {len(images)} page(s) into {pdf_path}: {describe(exc)}"
        )
        raise PdfError(msg) from exc
```

The docstring paragraph at `:166-176` justifies that `except Exception` at length ("narrow in *span* …
broad in *type*"); extend that list with pikepdf/qpdf rather than replacing the paragraph. The empty
guard (`:197-202`, `"Could not assemble a PDF: no pages were given"`) and the `dpi`-is-supplied-by-the-
caller paragraph (`:149-158`) both survive verbatim.

**`build_pdf_filename` (`:86-125`)** is the analog for both new names D-10 needs — the
`… (partial).pdf` name and the `failed/<job-keyed-name>/` **directory**. Its uniqueness argument is
exactly why the directory must be job-keyed too:

```python
    timestamp = datetime.now(tz=UTC).strftime("%Y%m%d-%H%M%S")
    job_segment = sanitise_title_for_filename(job_id)[:_JOB_ID_LENGTH].strip("-")
    segments = [
        part
        for part in (timestamp, job_segment, sanitise_title_for_filename(title))
        if part
    ]
    return "-".join(segments) + ".pdf"
```

`_handle_duplex_mismatch` already demonstrates the suffix convention with
`f"{request.title} (fronts)"` (`pipeline.py:828`), which is the precedent for `(partial)`.

---

### `src/saneless/pipeline.py` — partial-scan preservation (orchestration, failure path)

**Analog:** `_preserving` (`:473-560`) — but per Finding 8 the new guard **reuses its move+message
shape without wrapping acquisition in it**, because `_preserving` turns a non-`SanelessError` into a
`PaperlessError`, which would be a lie on the acquisition path.

**The move + message + never-lose-the-original body to copy (`:531-560`):**

```python
    try:
        yield
    except Exception as exc:
        destinations: list[Path] = []
        try:
            failed_dir.mkdir(parents=True, exist_ok=True)
            for pdf_path in pdf_paths:
                if not pdf_path.exists():
                    continue
                destination = failed_dir / pdf_path.name
                shutil.move(pdf_path, destination)
                destinations.append(destination)
            # Once, after the loop, so the count reflects the finished state.
            if destinations:
                _warn_if_failed_dir_growing(failed_dir)
        except OSError as move_exc:
            msg = f"{exc}. The scan could NOT be preserved to {failed_dir}: {move_exc}"
            raise PaperlessError(msg) from exc
        if not destinations:
            raise
        preserved = ", ".join(str(destination) for destination in destinations)
        msg = f"{exc}. The scan was preserved at {preserved}"
        if isinstance(exc, SanelessError):
            raise type(exc)(msg) from exc
        raise PaperlessError(msg) from exc
```

Four things the planner must carry across verbatim:
- `raise type(exc)(msg) from exc` — this is what keeps the original exception type, hence
  `classify_error` → exit code 1 / `ErrorCategory.SCANNER` (Phase 28 D-07, D-09's "original exception
  type and so its original exit code").
- `shutil.move` to an **explicit destination path**, never a bare directory — the docstring at
  `:496-506` explains why (`shutil.Error` is not an `OSError` and would escape the handler). D-10's
  page **directory** move inherits that constraint.
- `if not destinations: raise` — nothing preserved means the original goes through untouched, which is
  exactly D-10's "zero pages spooled means today's messages are unchanged".
- Both failures reported when preservation itself fails.

**`ScanCancelledError` must be re-raised before this branch** (Anti-patterns / Pitfall in RESEARCH.md).
The in-repo precedent for that distinction is `exceptions.py:38-45`:

```python
class ScanCancelledError(SanelessError):
    """
    The operator deliberately stopped the scan at the flip prompt (N-08, D-01).

    A cancel is not a failure.  This is deliberately not a ``ScanError``, so no
    ``except ScanError`` anywhere can absorb it and report the operator's
    decision as a broken scanner.
    """
```
— note it is *not* a `ScanError` but *is* an `Exception`, so only an explicit re-raise excludes it.

**`_warn_if_failed_dir_growing` (`:454-462`) — the never-raise guard D-10 extends to directories:**

```python
    try:
        preserved = list(failed_dir.glob("*.pdf"))
        if len(preserved) < FAILED_DIR_WARN_THRESHOLD:
            return
        total_bytes = sum(pdf.stat().st_size for pdf in preserved)
    except OSError:
        # Deliberately silent, per the docstring: a bookkeeping failure must
        # not displace the delivery failure the guard is about to re-raise.
        return
```
Directory sizes go inside the same `try`, so a `rglob` on a directory that vanishes still returns
silently. Its docstring's **"This function must never raise"** paragraph (`:441-446`) stays.

**`_handle_duplex_mismatch` (`:825-848`) — the two-PDF naming and the single shared guard D-10's
pass-B rule reuses:**

```python
    fronts_pdf = assemble_pdf(
        fronts,
        tmp_path / "fronts",
        filename=build_pdf_filename(request.job_id, f"{request.title} (fronts)"),
        dpi=delivery.dpi,
    )
    backs_pdf = assemble_pdf(...)
    ...
    # One guard over both halves: they are a single document between them, so
    # a failure on either one has to keep both (D-08).
    with _preserving([fronts_pdf, backs_pdf], delivery.failed_dir):
```

**The gap being closed (`:1076-1088`) — the comment that must be deleted, not just amended:**

```python
    back_batch = scanner.scan_pages(device_id, scan_settings)
    # Before the count comparison, so no half is assembled from an empty list.
    # Not _require_pages: "No pages were scanned" is false once pass A fed the
    # fronts, so the message names the pass and what pass A scanned (IN-01).
    # The fronts are still lost here; keeping them needs Phase 29's spooling.
    if not back_batch.pages:
        msg = (
            "No back pages were scanned in pass B "
            f"(pass A scanned {len(front_pages)} front page(s))"
        )
        raise ScanError(msg)
```

**The workspace lifetime trap (Finding 8) — `run_pipeline` (`:1282-1287`, `:1404`):**

```python
    workspace = _open_workspace(
        settings.output.tmp_dir, settings.output.min_free_space_mb
    )

    with workspace as tmp_dir:
        tmp_path = Path(tmp_dir)
        ...
    return result
```
The spool lives inside this `with`; anything preserved must be moved out **before** it exits. The
`_preserving` docstring (`:478-484`) already names this trap — *"a guard placed outside would run
after the scan was already gone, which is the trap this whole phase is named after"* — and the same
sentence is now also true of the page directory.

---

### `src/saneless/pipeline.py` — records everywhere (orchestration, batch transform)

**`_interleave_duplex` (`:980-988`)** — the body is type-only change; the reversal comment stays:

```python
    if len(fronts) != len(backs):
        msg = f"Page count mismatch: {len(fronts)} fronts, {len(backs)} backs"
        raise ScanError(msg)
    backs_reversed = list(reversed(backs))
    result: list[Image.Image] = []
    for front, back in zip(fronts, backs_reversed, strict=True):
        result.append(front)
        result.append(back)
    return result
```

**`_drop_empty_pages` (`:584-598`)** — the call that changes from images to stored stats:

```python
    if not profile.enable_empty_page_detection:
        logger.info("Empty page detection disabled for profile")
        return images

    filtered = filter_empty_pages(
        images,
        mean_threshold=profile.empty_page_mean_threshold,
        stddev_threshold=profile.empty_page_stddev_threshold,
    )
    if len(filtered) < len(images):
        logger.info("Empty page filter: %d -> %d pages", len(images), len(filtered))
    if not filtered:
        msg = "All pages were blank"
        raise ScanError(msg)
```

**`_scan_simplex` (`:1167-1176`)** — where the thumbnail call moves *out of* (D-05 moves it into the
spool, during pass A):

```python
    batch = scanner.scan_pages(device_id, scan_settings)
    _require_pages(batch)
    logger.info("Scanned %d page(s)", len(batch.pages))

    # Generate thumbnail from first page
    if batch.pages and request.thumbnail_callback:
        thumb = generate_thumbnail(batch.pages[0])
        request.thumbnail_callback(thumb)
```
The same three lines exist again at `:1041-1043` in `_scan_manual_duplex`; both go.

**`run_pipeline:1329-1331`** — the per-image EXIF strip that becomes dead once the spool owns the
write:

```python
        # Step 1.5: Strip EXIF from all images (Pitfall #5)
        for img in images:
            img.info.pop("exif", None)
```

**Total `match` + `assert_never` — the mandated dispatch shape for any new variant set**
(`run_pipeline:1313-1323`, and `_scan_manual_duplex:1055-1072`):

```python
            match duplex_result:
                case ScanBatch():
                    batch = duplex_result
                case _DuplexMismatch():
                    return _finish_duplex_mismatch(...)
                case _:
                    assert_never(duplex_result)
```
with the comment explaining *why* a match and not a dict/isinstance chain — "a variant missing from a
dict draws no diagnostic from either ty or pyrefly".

---

### `src/saneless/cli.py` — four backend closes (entry point, lifecycle)

**Analog:** the four construction sites themselves, all one line:

```python
src/saneless/cli.py:543:    scanner = SaneBackend(host=settings.scanner.host)      # scan
src/saneless/cli.py:640:    scanner = SaneBackend(host=_settings.scanner.host)     # devices
src/saneless/cli.py:761:        scanner = SaneBackend(host=settings.scanner.host)  # serve (closed by lifespan)
src/saneless/cli.py:861:    scanner = SaneBackend(host=settings.scanner.host)      # auto-profiles
```

`serve` (`:760-765`) is the one already wrapped, and shows the established "a failure to start is a
ConfigError, not a ScanError" translation that D-18 must not disturb:

```python
    try:
        scanner = SaneBackend(host=settings.scanner.host)
    except ScanError as exc:
        msg = f"The web server could not start: {exc}"
        raise ConfigError(msg) from exc
    app = create_app(settings, scanner)
```

Per Finding 10 the other three take `ctx.call_on_close(scanner.close)` on the line after construction.
Note `scan` already uses `ctx.exit(ExitCode.CONFIG)` before the backend exists (`:534-541`), which is
the early-exit path a `try`/`finally` would have to be careful about and `call_on_close` handles for free.

---

### `src/saneless/web/app.py` — lifespan close (app factory, lifecycle)

**Analog:** the shutdown branch it extends (`:119-136`):

```python
        yield
        # worker.stop() blocks the event loop for at most STOP_JOIN_SECONDS,
        # during lifespan shutdown, after uvicorn has stopped serving (D-08).
        if not worker.stop():
            # D-09: the store closes only after a confirmed stop, so a stuck
            # thread never hits "Cannot operate on a closed database".  D-07:
            # the abandoned job is not written here -- that would race its own
            # final write; the next startup's recovery records it.
            logger.warning(
                "Scan worker did not stop within %s s (job %s still running); "
                "leaving the job store and Paperless client open for process exit",
                STOP_JOIN_SECONDS,
                worker.current_job_id,
            )
            return
        paperless.close()
        job_store.close()
        logger.info("App shutdown complete")
```

`scanner.close()` goes after `job_store.close()`; the `return` at `:133` must stay a `return` (the
"a thread may still be inside SANE" rule is the same rule already written there). The lifespan
docstring's closing line (`:92-93`) — *"Shutdown stops the worker first and closes the Paperless
client and the job store only when the worker confirms it stopped (D-09)"* — names the two clients and
needs the third.

---

### `tests/fake_sane.py` — Event-gated blocking read (test double, event-driven)

**Analog:** `set_page_delay` (`:779-796`) — the sleep-based knob D-16 replaces, whose docstring already
argues *why the knob belongs on the one shared device rather than in a bespoke double*. Keep that
argument, change the mechanism:

```python
    def set_page_delay(self, seconds: float) -> None:
        """
        Make each page take time to arrive, so a timeout can be exercised.

        A scan that is merely slow is a real condition, and the backend's
        per-page timeout exists precisely for it, so the delay belongs in the
        device rather than in a bespoke blocking iterator written per test.  A
        hand-rolled blocking double is what this replaces, and it modelled a
        device handle -- exactly what D-17 leaves only one of.

        A method rather than a constructor keyword for the usual reason:
        ``__init__`` already carries ruff's maximum of five arguments.
        ...
        """
        self.__dict__["_page_delay"] = seconds
```

**⚠️ `__init__` is at ruff's `PLR0913` ceiling (5 args, `:645-653`), so every new knob is a method**
that writes through `self.__dict__[...]` — required, because `__setattr__` (`:696-729`) is overridden
to model SANE option validation. State is declared as a bare annotation in the class body
(`:626-643`) with no assigned value, so `__getattr__` still runs.

**The two methods that gain the gate (`:1013-1073`)** — note `start()` is where the current delay
lives and `snap()` is where the truncated-page-on-cancel return must be modelled (Open Question 2):

```python
    def start(self) -> None:
        self.calls.append("start")
        if self._start_error is not None and self._page_index == self._start_error_page:
            raise self._start_error
        if self._page_index >= self._pages:
            # No delay on this path: the end-of-feed probe is not a page being
            # scanned.  Charging it one made the wall-clock arithmetic in the
            # per-page timeout tests wrong by a whole delay.
            raise FakeSaneError(_FEEDER_EMPTY_MESSAGE)
        if self._page_delay:
            time.sleep(self._page_delay)

    def snap(self, *, no_cancel: bool = False) -> Image.Image:
        self.calls.append("snap")
        error = self._call_errors.get("snap")
        if error is not None:
            raise error
        loaded = self._page_images
        index = self._page_index
        page = (
            loaded[index] if index < len(loaded) else _page_image(index, self._page_size)
        )
        self._page_index += 1
        return page
```

**The counters D-16's assertions read (`:1088-1104`)** — `close_while_blocked` / `exit_while_blocked`
are new flags set here and in `FakeSaneModule.exit`:

```python
    def cancel(self) -> None:
        """Record that the device was cancelled."""
        self.cancel_calls += 1

    def close(self) -> None:
        self.close_calls += 1
        error = self._call_errors.get("close")
        if error is not None:
            raise error
```

**`FakeSaneModule` (`:1107-1201`)** already has `init_call_count` / `exit_call_count`, so D-19's
assertions need no new counters — only `exit()` must learn to record whether a read was blocked:

```python
    def init(self) -> tuple[int, int, int]:
        self.init_call_count += 1
        if self._init_error is not None:
            raise self._init_error
        return (1, 0, 3)

    def exit(self) -> None:
        """Record the shutdown call."""
        self.exit_call_count += 1
```

**`_page_image` (`:523-540`)** is where the D-08 weakref issue log hooks in — one RGB page per index,
already distinguishable by `index`, which is also what SC1's "each page carries its distinct per-page
content in order 1..12" read-back needs:

```python
def _page_image(index: int, size: tuple[int, int] = _DEFAULT_PAGE_SIZE) -> Image.Image:
    width, height = size
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((10, 10, width - 10, height - 10), fill="black")
    draw.ellipse((30, 30 + index, width - 30, height - 30 + index), fill="white")
    return image
```

---

### `tests/conftest.py` — the migration lever (test seam, factory)

**Analog:** `scan_batch()` (`:165-192`) — the helper 36 of the 50 `test_pipeline.py` sites already go
through. Its docstring is also a model for why the new factory exists:

```python
def scan_batch(
    pages: list[Image.Image],
    *,
    resolution: int = 300,
    rejected: int = 0,
) -> ScanBatch:
    """
    Build a ScanBatch for a stubbed scanner.

    ``scan_pages`` returns a record rather than yielding, so a stub handing back
    ``iter([...])`` no longer models the backend at all -- and because a
    ``MagicMock`` will return whatever it is given, that mismatch surfaces as a
    confusing failure deep in the pipeline rather than at the stub.
    ...
    """
    return ScanBatch(pages=pages, actual_resolution=resolution, pages_rejected=rejected)
```

That last sentence is the precedent for Pitfall 5 (a `side_effect` **list** of callables is returned,
not called): the same class of "a MagicMock will return whatever it is given" trap, already named once
in this file.

**`AlwaysContinueFlipCoordinator` (`:195-224`) is the model for `StubScannerBackend`** — a shared
conftest base subclassing the ABC, with the import rule in its own docstring:

```python
class AlwaysContinueFlipCoordinator(FlipCoordinator):
    """
    A flip coordinator whose operator flips the stack the instant it is asked.
    ...
    Subclasses the ABC rather than duck-typing it, for the reason Phase 24's
    WR-08 measured and ``tests/test_cli.py``'s ``MockSaneBackend`` records:
    every stub that subclassed was caught by the type checkers when its
    contract changed, and the ones that did not were missed.

    Import it as ``from tests.conftest import AlwaysContinueFlipCoordinator``;
    the bare ``conftest`` form raises ``ModuleNotFoundError`` under pytest 9's
    importlib mode.
    """
```

Both sentences are load-bearing for this phase: the first is the argument for replacing
`MagicMock(spec=ScannerBackend)` sites with a real subclass, and the second is the import form the
seven stub classes must use.

**`mock_scanner` (`:227-235`)** is the fixture whose one line changes from `return_value` to
`side_effect`:

```python
@pytest.fixture
def mock_scanner() -> MagicMock:
    """Return a mock ScannerBackend returning a single image with content."""
    scanner = MagicMock(spec=ScannerBackend)
    img = Image.new("RGB", (100, 100), "white")
    draw = ImageDraw.Draw(img)
    draw.rectangle([10, 10, 90, 90], fill="black")
    scanner.scan_pages.return_value = scan_batch([img])
    return scanner
```

**`wait_for_state` + `_wait_for_state_fixture` (`:275-343`)** is the established pattern for exposing a
conftest *function* to test modules under pytest 9 importlib mode (a fixture that returns the
function uncalled) — reuse it if `spooling()` needs to be reachable as a helper rather than imported.

---

### Test stub classes (7 files) — one-line `scan_pages` override

**Analog for the one-page stubs:** `_StubScanner` (`tests/test_app_lifespan.py:57-76`):

```python
class _StubScanner(ScannerBackend):
    """Concrete scanner stub; these tests never run a scan."""

    def get_devices(self) -> list[DeviceInfo]:
        """Return an empty device list."""
        return []

    def get_capabilities(self, device_id: str) -> DeviceCapabilities:
        """Return default capabilities."""
        return DeviceCapabilities(
            sources=["Flatbed"], resolutions=[300], modes=["color"]
        )

    def scan_pages(self, device_id: str, settings: ScanSettings) -> ScanBatch:
        """Return a batch holding a single white test image."""
        return ScanBatch(
            pages=[Image.new("RGB", (100, 100), "white")],
            actual_resolution=settings.resolution,
            pages_rejected=0,
        )
```
All five web/cross-origin stubs are near-copies of this; a conftest `StubScannerBackend` base collapses
`get_devices` + `get_capabilities` and leaves each file a `scan_pages` override.

**Analog for the gated stubs:** `_PassBGatedScanner` (`tests/test_worker.py:1111-1167`) — already
`threading.Event`-based, already argues against `MagicMock`, and is the template for D-16's gated fake
at the *backend* level (as opposed to the device level):

```python
    def __init__(self) -> None:
        """Start with pass B held."""
        self.release_pass_b = threading.Event()
        self.scan_calls = 0

    def scan_pages(self, device_id: str, settings: ScanSettings) -> ScanBatch:
        self.scan_calls += 1
        if self.scan_calls >= 2:
            self.release_pass_b.wait(_PASS_B_GATE_CEILING)
        return scan_batch([_inked_page()])
```
The `scan_calls`-counter dispatch here is also the in-repo answer to Pitfall 5 — one object that
tracks its own call count, rather than a `side_effect` list.

---

### Scanner tests — timeout, fixtures, and the subprocess proof

**Analog for the timeout tests (D-14 says the flatbed test mirrors it) —
`tests/test_scanner.py:1426-1454`:**

```python
class TestSaneBackendPerPageTimeout:
    """Per-page timeout tests."""

    def test_page_timeout_raises_scan_error(
        self, fake_sane_module: FakeSaneModule
    ) -> None:
        """A page that takes too long raises ScanError naming the timeout."""
        mock_dev = fake_sane_module.open(_TEST_DEVICE)
        mock_dev.set_page_delay(2.0)

        backend = SaneBackend()

        with pytest.raises(ScanError, match="timed out"):
            backend._scan_adf_pages(mock_dev, timeout_per_page=0.1)
```
`match="timed out"` is why the timeout wording must not change. The `set_page_delay(2.0)` /
`timeout_per_page=0.1` pair becomes an Event arm / `Event.wait` under D-16 — TEST-02 forbids extending
the sleep, and this existing 2.0 s sleep is the one the phase is allowed to leave alone.

**Fixtures (`tests/test_scanner.py:270-305`)** — the module-level `sane` monkeypatch seam every test
depends on, and the thing the D-17 init guard must not break:

```python
@pytest.fixture
def fake_sane_module(monkeypatch: pytest.MonkeyPatch) -> FakeSaneModule:
    """
    Patch the one shared fake into sane_backend's module-level ``sane`` name.

    ``_ensure_sane()`` leaves that name None until first use, which is the seam
    that makes the whole approach work; it is kept exactly as it was.
    ...
    """

@pytest.fixture
def sane_backend(fake_sane_module: FakeSaneModule) -> SaneBackend:
    """Create a SaneBackend over the shared fake."""
    _ = fake_sane_module  # side-effect: patches the sane module
    return SaneBackend()
```
**⚠️** With a process-level init guard, `SaneBackend()` in one test will suppress `sane.init()` in the
next unless the guard's state is reset. These fixtures are where that reset belongs (or an autouse
sibling), and D-17's "after `sane.exit()` the guard resets" is the mechanism.

**Analog for the subprocess process-exit proof (Finding 9) — `tests/test_atomic_write.py:~645-703`,
the only existing child-process test in the suite:**

```python
_NAMESPACE_SCRIPT = """
import sys
from pathlib import Path
from saneless.atomic_write import replace_file_atomically
from saneless.exceptions import ConfigError

try:
    replace_file_atomically(Path(sys.argv[1]), "new = 1\\n")
except ConfigError as exc:
    print(exc)
    sys.exit(3)
print("replaced")
"""
```
```python
    return subprocess.run(
        [...],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
```
Module-level source constant, `sys.executable`, `capture_output=True, text=True, check=False`, explicit
`timeout=` — copy all four. Note this file also shows the `pytest.skip`-on-unavailable-host fixture
pattern, which the D-12 hardware test may want.

---

## Shared Patterns

### Error translation at a module boundary
**Source:** `sane_backend.py:777-781`, `pdf.py:233-237`, `pipeline.py:551-553`, `atomic_write.py:274-280`
**Apply to:** every new raise in `spool.py`, `sane_backend.py`, `pdf.py`, `pipeline.py`

```python
except Exception as exc:
    scan_error_msg = f"Scanner error on page {page_num + 1}: {describe(exc)}"
    raise ScanError(scan_error_msg) from exc
```
Always: message into a named variable first (ruff `EM`), always `from exc`, always through
`describe()` (which collapses multi-line third-party messages to one line — `exceptions.py:76-98`),
always naming the identifiers (page number, path, device) per Phase 28 D-08.

### Never let a cleanup failure replace the real failure
**Source:** `sane_backend.py:1233-1239`, `pipeline.py:441-446` + `:459-462`, `pdf.py:229-232`
**Apply to:** `ScannerBackend.close()`, `shutdown()`, the extended growth check, the preservation guard

```python
            try:
                dev.close()
            except Exception:
                # Logged, never raised: an exception from close() here would
                # replace the one that ended the scan, which is the error the
                # operator needs to see (T-28-16).
                logger.warning("Could not close scanner %s", device_id, exc_info=True)
```

### Docstrings answer "why", cite decision IDs, and record rejected alternatives
**Source:** `base.py:55-113` (`classify_source`), `pdf.py:45-83`, `sane_backend.py:701-751`
**Apply to:** every new/rewritten symbol in this phase

The register is: numbered branch rationale, `Measured, not assumed:` before a figure, explicit
`Rejected:` / `DECLINED` paragraphs, and a closing "this is a settled answer, do not re-open it".
`classify_source` (`base.py:96-113`) is the canonical example. Every new decision this phase makes
(the live-image bound of 2, `_CANCEL_GRACE_SECONDS = 10.0`, PNG level 6, per-page convert + qpdf merge)
has a measured number behind it in RESEARCH.md and should carry it in the docstring.

### Constants with a justified value, not a bare number
**Source:** `sane_backend.py:100-139` (`_DEFAULT_PAGE_TIMEOUT_SECONDS`, `_MIN_PAGE_BYTES`, `_MAX_ADF_PAGES`)
**Apply to:** `_CANCEL_GRACE_SECONDS`, the spool's PNG level note, the live-image bound

```python
# Minimum raw image data size in bytes. Catches a corrupt or truncated buffer.
#
# This is now one of only two surviving checks, so its value is worth
# justifying rather than asserting. Measured against the SANE `test` backend,
# the smallest legitimate real page is 69,620 bytes ...
_MIN_PAGE_BYTES: int = 10_000  # 10 KB
```
Module constants over config keys is the standing rule (Phase 24 D-04, restated in CONTEXT.md).

### Total `match` + `assert_never` for variant sets
**Source:** `pipeline.py:1313-1323`, `pipeline.py:1055-1072`, `base.py:42-49`
**Apply to:** any new enum/variant this phase introduces (e.g. a wedge/settle outcome)

### Lint constraints that shape the code (all verified in `pyproject.toml`)
| Rule | Constraint | Consequence here |
|---|---|---|
| `PL` selected | `PLW0603` flags `global` | init guard/wedge must mutate a module-level container, **not** rebind a global (no new `# noqa`) |
| `PLR0913` | max 5 args | `_acquire_with_timeout(dev, work, page_label, timeout, grace)` is exactly 5; `FakeSaneDev.__init__` is full — new knobs are methods |
| `PLR0915` | statement cap | `_finish_duplex_mismatch` exists only because of it (`pipeline.py:895-897`); new pipeline branches need the same extraction |
| `EM`, `RSE`, `G` | no literal in `raise`, no f-string in `logger` | message-in-a-variable; `logger.warning("... %s", x)` |
| `D` | docstrings everywhere, blank line before closing `"""` | every new function and test |
| `ANN`, `FBT` | annotations, no positional bool | `snap(self, *, no_cancel: bool = False)` (`fake_sane.py:1039`) is the in-repo example |
| tests per-file-ignores | only `S101`, `ARG`, `S104/5/6` | `subprocess` in tests needs a non-shell, absolute-path invocation — see `test_atomic_write.py:684` |
| `BLE`/`TRY` **not** selected | — | the reader thread's `except BaseException as exc:` needs no suppression (RESEARCH.md verified) |

---

## No Analog Found

| File | Role | Data Flow | Reason |
|------|------|-----------|--------|
| `docs/explanation/architecture.md`, `docs/how-to/troubleshoot-a-failed-scan.md`, `docs/how-to/set-up-adf-duplex.md`, `docs/explanation/consume-directory-fallback.md`, `docs/reference/configuration.md`, `docs/reference/environment-variables.md`, `docs/reference/docker.md` | docs | prose | Prose, not code. RESEARCH.md Finding 11 already enumerates every sentence with a file:line, which is more precise than any analog would be. The one code-adjacent analog is `tests/test_deployment_config.py:238`, which imports `ARCHITECTURE` and is the only existing doc-truth mechanism — copy that one assertion's shape for the new "Memory, disk and timeouts" subsection. |
| `pikepdf.Job` merge in `pdf.py` | service | batch transform | No in-repo use of `pikepdf` outside `tests/test_pdf.py` read-backs. Use RESEARCH.md Code Example 2 verbatim; the surrounding `TemporaryDirectory` + `except Exception → PdfError` frame is the analog. |

**Partial-analog note:** `spool.py` has no single analog because nothing in the tree both writes files
and measures images. Its three halves are covered above; the composite is deliberate, not a gap.

---

## Metadata

**Analog search scope:** `src/saneless/**`, `tests/**`, `pyproject.toml`
**Files read:** `scanner/base.py`, `scanner/sane_backend.py` (targeted ranges), `pipeline.py` (targeted
ranges), `pdf.py`, `pages.py`, `paper_sizes.py`, `exceptions.py`, `atomic_write.py` (header + grep),
`web/app.py`, `cli.py` (3 ranges), `tests/conftest.py`, `tests/fake_sane.py` (targeted ranges),
`tests/test_app_lifespan.py`, `tests/test_worker.py`, `tests/test_scanner.py`, `tests/test_atomic_write.py`,
`pyproject.toml`
**Pattern extraction date:** 2026-09-15
