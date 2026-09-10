# Phase 21: Vocabulary and Contracts - Pattern Map

**Mapped:** 2026-09-10
**Files analyzed:** 12 (2 created, 10 modified)
**Analogs found:** 11 / 12 (one new-to-codebase pattern — see § No Analog Found)

All line numbers below were read from the working tree on 2026-09-10 and are exact.

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|-------------------|------|-----------|----------------|---------------|
| `src/saneless/vocabulary.py` **(new)** | domain constants / leaf module | transform (pure lookup) | `src/saneless/paper_sizes.py` + `src/saneless/exceptions.py` | exact (structure), role-match (content) |
| `tests/test_vocabulary.py` **(new)** | test | transform | `tests/test_paper_sizes.py` | role-match (parametrize is new — see below) |
| `src/saneless/job.py` | model + persistence | CRUD | itself (`job.py:19`, `:24-43`, `:46-74`) | self |
| `src/saneless/pipeline.py` | service / orchestrator | request-response | itself (`pipeline.py:37-45`, `:51-62`) | self |
| `src/saneless/paperless.py` | service client | request-response | `pipeline.py:51-62` (`PipelineRequest`) for the new `UploadResult` | exact |
| `src/saneless/worker.py` | service / background thread | event-driven | itself (`worker.py:120-128`, `:221-233`) | self |
| `src/saneless/cli.py` | controller (CLI) | event-driven | itself (`cli.py:110-122`) | self |
| `src/saneless/web/app.py` | config / app factory | request-response | itself (`web/app.py:28`, `:35-48`, `:101-102`) | self |
| `src/saneless/web/templates/partials/status.html` | template | request-response | `partials/history.html:7-8` (filter-pipe idiom) | partial |
| `src/saneless/web/templates/index.html` | template | request-response | `partials/status.html:1-7` | partial |
| `src/saneless/scanner/base.py` | model / ABC | transform | `auto_profiles.py:32-60` (`source_to_slug`) | role-match |
| `src/saneless/scanner/sane_backend.py` | service backend | streaming | itself (`sane_backend.py:158-160`, `:496-505`) | self |
| `src/saneless/auto_profiles.py` | utility | transform | itself (`auto_profiles.py:47-58`) | self |
| `docs/explanation/consume-directory-fallback.md` | docs | — | same file, `:62` | exact |

---

## Pattern Assignments

### `src/saneless/vocabulary.py` (NEW — leaf module, constants + total lookup functions)

**Analog for module skeleton:** `src/saneless/paper_sizes.py` — the closest leaf module (no intra-package imports, `__all__`, constants with attached docstrings, one pure function with a full Args/Returns docstring). `src/saneless/exceptions.py` is the analog for the *multi-line* module docstring and the multi-line sorted `__all__` list.

**Module header + `__all__`** — `paper_sizes.py:1-25`:
```python
"""Paper size dimension lookup and crop utility for scan area control."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from PIL import Image

__all__ = ["PAPER_SIZES_MM", "PaperSize", "crop_to_paper_size"]

PaperSize = Literal["full", "a3", "a4", "a5", "letter", "legal"]
"""Valid paper size names for scan area constraint."""

PAPER_SIZES_MM: dict[str, tuple[float, float]] = {
    ...
}
"""Width x height in millimeters for standard paper sizes.

``"full"`` is intentionally absent -- it means no constraint (scan full bed).
"""
```

Load-bearing conventions to copy:
- `from __future__ import annotations` is the first import in **every** module in `src/saneless/` (verified: `job.py:9`, `pipeline.py:8`, `worker.py:9`, `cli.py:8`, `paperless.py:10`, `scanner/base.py:9`, `auto_profiles.py:9`, `web/app.py:3`). No exception.
- `__all__` sits **after** the imports and **before** any definition, sorted (ruff `RUF022` is active via the `RUF` selector).
- Module-level constants carry a **string literal directly beneath** them as documentation (`paper_sizes.py:13`, `:22-25`). This is the established way to document `ACTIVE_STATES` / `TERMINAL_STATES` / `BUSY_STATES`.
- `logger = logging.getLogger(__name__)` goes immediately after `__all__` when the module logs (`job.py:21`, `pipeline.py:48`, `worker.py:34`). `vocabulary.py` needs no logger.
- Prose in docstrings uses `--` (double hyphen), not an em dash (`paper_sizes.py:24`, `exceptions.py:30`, `auto_profiles.py:55`).

**Multi-line module docstring + multi-line `__all__`** — `exceptions.py:1-14` (the form to use if the `vocabulary.py` docstring runs past one line, which it should):
```python
"""
Custom exception hierarchy for saneless.

All saneless-specific exceptions inherit from SanelessError,
allowing callers to catch broad or narrow exception types.
"""

__all__ = [
    "ConfigError",
    "FeederEmptyError",
    "PaperlessError",
    "SanelessError",
    "ScanError",
]
```
Note the opening `"""` alone on its own line — `D212` is in the ignore list (`pyproject.toml:92`), so multi-line docstrings start on the *second* line throughout this codebase.

**StrEnum declaration** — `job.py:24-43` (copy verbatim in style):
```python
class JobState(StrEnum):
    """States in the scan job lifecycle."""

    PENDING = "PENDING"
    SCANNING = "SCANNING"
    AWAITING_FLIP = "AWAITING_FLIP"
    ASSEMBLING = "ASSEMBLING"
    UPLOADING = "UPLOADING"
    DONE = "DONE"
    ERROR = "ERROR"


class ErrorCategory(StrEnum):
    """Categories of errors for programmatic handling."""

    FEEDER = "FEEDER"
    CONFIG = "CONFIG"
    SCANNER = "SCANNER"
    UPLOAD = "UPLOAD"
    UNKNOWN = "UNKNOWN"
```
Conventions: value string is **identical to the member name**; one-line class docstring; blank line after the docstring; **no per-member comments or docstrings**; members are in **lifecycle/semantic order, not alphabetical**. `PipelineEvent` (`pipeline.py:37-45`) follows the same rules. `ScanOutcome` must follow them too.

Import line for the enum base: `from enum import StrEnum` (`job.py:17`, `pipeline.py:15`) — plain, not `import enum`.

**Function docstring shape for `state_label()` / `progress_label()` / `error_message()`** — `paper_sizes.py:33-48` is the analog (Google-style `Args:`/`Returns:`, with a **blank line before the closing `"""`**, which is required here because `D203` is ignored but the trailing-blank-line style is universal in this tree):
```python
def crop_to_paper_size(
    image: Image.Image,
    paper_size: str,
    dpi: int,
) -> Image.Image:
    """
    Crop an image to the given paper size at the specified DPI.

    Returns the image unchanged when *paper_size* is ``"full"`` or not
    recognised.  Otherwise crops top-left aligned to the calculated
    pixel dimensions, clamping to the actual image size.

    Args:
        image: Source PIL image to crop.
        paper_size: Paper size key (e.g. ``"a4"``, ``"letter"``).
        dpi: Scan resolution in dots per inch.

    Returns:
        Cropped image, or the original if no constraint applies.

    """
```
Note: `Raises:` sections exist in this codebase (`pipeline.py:78`, `pipeline.py:327-331`, `paperless.py:90-92`) and are the place to document `AssertionError` from `assert_never`.

**Sole existing `assert_never` / `match` precedent:** none. `grep` finds no `assert_never` and no `match` statement anywhere in `src/`. The exhaustiveness pattern in RESEARCH.md § Pattern 1 is the specification; there is no in-tree analog to imitate. The nearest existing shape is the `is`-identity chain at `worker.py:223-233` (see below), which RESEARCH.md's measured table confirms is also checker-valid.

---

### `src/saneless/job.py` (model, CRUD — re-exports + two new properties)

**Re-export pattern — VERIFIED, no `# noqa` needed.** `job.py:19` already declares:
```python
__all__ = ["ErrorCategory", "Job", "JobState", "JobStore"]
```
Ruff treats membership in `__all__` as usage, so a plain re-export import satisfies `F401` under this project's exact config. Confirmed by running the real gate:
```
$ printf '"""Probe."""\n\nfrom __future__ import annotations\n\nfrom saneless.vocabulary import ErrorCategory, JobState\n\n__all__ = ["ErrorCategory", "JobState"]\n' \
    | uv run ruff check --stdin-filename src/saneless/job.py -
All checks passed!
```
So the edit is: delete `job.py:24-43`, add `from saneless.vocabulary import ErrorCategory, JobState` to the import block (**after** the stdlib block, separated by a blank line — see `pipeline.py:19-22` and `web/app.py:14-17` for the first-party import group), and leave `__all__` at `:19` untouched. Do **not** use `import X as X`, do **not** add `# noqa`.

**Only existing re-export in the tree** is a package `__init__.py`, not a module — `scanner/__init__.py:9-17`:
```python
from .base import DeviceCapabilities, DeviceInfo, ScannerBackend, ScanSettings

__all__ = [
    "DeviceCapabilities",
    "DeviceInfo",
    "SaneBackend",
    "ScanSettings",
    "ScannerBackend",
]
```
Same mechanism (import + `__all__`), so `job.py` is establishing nothing novel.

**Dataclass + `Attributes:` docstring** — `job.py:46-74`, the analog the planner must extend:
```python
@dataclass
class Job:
    """
    A scan job with metadata and state tracking.

    Attributes:
        id: Unique job identifier (UUID).
        profile: Name of the scan profile to use.
        title: Document title for paperless-ngx.
        state: Current job lifecycle state.
        ...

    """

    id: str
    profile: str
    title: str
    state: JobState = JobState.PENDING
    ...
```
**No dataclass in this codebase uses `frozen=`, `slots=`, `kw_only=`, or `eq=`** — verified across all five existing value types (`job.py:46`, `pipeline.py:51`, `scanner/base.py:23`, `:33`, `:43`). Every one is a bare `@dataclass`. If the planner adopts RESEARCH.md's mild preference for `frozen=True` on the two *new* result types, that is a deliberate departure and should be stated as such in the plan.

**Property-on-a-dataclass:** no existing example — `Job` has no methods at all. The closest analog for the docstring form is `ScanWorker`'s properties, `worker.py:120-128`:
```python
    @property
    def is_alive(self) -> bool:
        """Whether the worker thread is currently running."""
        return self._thread.is_alive()

    @property
    def current_job_id(self) -> str | None:
        """ID of the currently processing job, or None."""
        return self._current_job_id
```
Copy this exactly for `Job.is_active` / `Job.is_busy`: `@property`, explicit return annotation, **one-line** docstring beginning "Whether …", single-expression body. Place them after the field block, before `class JobStore`.

---

### `src/saneless/pipeline.py` (service, request-response — `job_state` property, `ScanResult`, `-> dict` deleted)

**Enum + dataclass pair to extend** — `pipeline.py:34-62`:
```python
__all__ = ["PipelineEvent", "PipelineRequest", "run_pipeline"]


class PipelineEvent(StrEnum):
    """Events emitted by the scan pipeline to report progress."""

    SCANNING = "SCANNING"
    AWAITING_FLIP = "AWAITING_FLIP"
    SCANNING_REVERSE = "SCANNING_REVERSE"
    ASSEMBLING = "ASSEMBLING"
    UPLOADING = "UPLOADING"
    DONE = "DONE"


logger = logging.getLogger(__name__)


@dataclass
class PipelineRequest:
    """Parameters for a scan pipeline run."""

    profile_name: str
    title: str
    tags: list[int] | None = None
    correspondent: int | None = None
    status_callback: Callable[[PipelineEvent], None] | None = None
    thumbnail_callback: Callable[[str], None] | None = None
    flip_event: threading.Event | None = None
    abort_event: threading.Event | None = None
```
`ScanResult` copies this exactly: bare `@dataclass`, **one-line** docstring (`PipelineRequest` does *not* use an `Attributes:` block — the symmetric result type should not either), required fields first, `| None = None` optionals last. Add `"ScanResult"` to `__all__` (sorted position: after `PipelineRequest`).

**Import block to extend** — `pipeline.py:19-22` (first-party group, absolute `saneless.` form in this file):
```python
from saneless.exceptions import ConfigError, ScanError
from saneless.pages import filter_empty_pages, generate_thumbnail
from saneless.pdf import assemble_pdf
from saneless.scanner.base import ScanSettings
```
Note the split-import convention: `pipeline.py` uses absolute `saneless.x`, while `worker.py:16-24` and `cli.py:20-32` use relative `.x`. **Match the file you are editing**, do not normalise.

**The sites being replaced** — `pipeline.py:310` (`-> dict`), `:325-326` (the `Returns: Task result dict from paperless-ngx polling.` docstring line), `:386` (`return {"status": "DONE", "warning": warning}`), `:432-439`:
```python
        task_uuid = paperless.upload_document(...)

        # Step 5: Poll for result
        if task_uuid != "fallback":
            result = paperless.poll_task(
                task_uuid,
                timeout=settings.output.paperless_task_timeout,
            )
        else:
            result = {"status": "FALLBACK", "path": settings.paperless.consume_dir}

        notify(PipelineEvent.DONE)
        logger.info("Pipeline complete for '%s'", request.title)

    return result
```

---

### `src/saneless/paperless.py` (service client — `UploadResult`)

**Analog for the new dataclass:** `pipeline.py:51-62` (above). `paperless.py` currently defines **no** dataclass and imports no `dataclasses`; the planner adds `from dataclasses import dataclass` to the stdlib import group at `paperless.py:12-15` (currently `logging`, `shutil`, `time`, `pathlib`) and adds `"UploadResult"` to `__all__` at `:21` (currently `__all__ = ["PaperlessClient"]`, sorted → `["PaperlessClient", "UploadResult"]`).

**The signature and docstring being changed** — `paperless.py:63-94` and `:146-158`:
```python
    def upload_document(
        self,
        pdf_path: Path,
        title: str,
        tags: list[int] | None = None,
        correspondent: int | None = None,
        created: str | None = None,
    ) -> str:
        """
        Upload a PDF document to paperless-ngx.
        ...
        Returns:
            Task UUID string from paperless-ngx, or "fallback" if
            the file was copied to the consume directory.

        Raises:
            PaperlessError: If upload fails and no fallback is available,
                or if the server returns a 4xx error.

        """
```
```python
        # All retries exhausted
        if self._consume_dir:
            dest_dir = Path(self._consume_dir)
            ...
            logger.warning("All retries exhausted. Copied PDF to %s", dest)
            return "fallback"
```
Success return is `return str(task_id)` at `:116`.

**The `Returns:` docstring at `:86-88` must change with the signature** — a stale `Returns:` block is the exact kind of drift this phase exists to delete.

---

### `src/saneless/worker.py` (service, event-driven — `_status_cb` collapse, `_categorize_error` moves out)

**The chain to preserve the ordering of** — `worker.py:221-233`:
```python
        def _status_cb(event: PipelineEvent, _jid: str = job.id) -> None:
            logger.info("Pipeline event: %s", event.value)
            if event is PipelineEvent.AWAITING_FLIP:
                self._transition_event.clear()
                self._job_store.update_state(_jid, JobState.AWAITING_FLIP)
            elif event is PipelineEvent.ASSEMBLING:
                self._job_store.update_state(_jid, JobState.ASSEMBLING)
                self._transition_event.set()
            elif event is PipelineEvent.UPLOADING:
                self._job_store.update_state(_jid, JobState.UPLOADING)
                self._transition_event.set()
            elif event is PipelineEvent.SCANNING_REVERSE:
                self._transition_event.set()
```
Note this file already uses `is`-identity comparison against enum members — the same construct RESEARCH.md's measured table lists as checker-valid. Keep `logger.info("Pipeline event: %s", event.value)` as the first statement.

**The function moving to `vocabulary.py`** — `worker.py:130-149` (verbatim, including the docstring, which is the shape `classify_error` should keep minus `self`):
```python
    def _categorize_error(self, exc: Exception) -> ErrorCategory:
        """
        Map an exception to its error category.

        Args:
            exc: The caught exception.

        Returns:
            The appropriate ErrorCategory value.

        """
        if isinstance(exc, FeederEmptyError):
            return ErrorCategory.FEEDER
        ...
        return ErrorCategory.UNKNOWN
```
`grep -rn '_categorize_error' tests/` returns nothing — RESEARCH.md's finding reproduces; no compatibility shim is needed.

**Its one caller** — `worker.py:254-262`:
```python
        except Exception as exc:
            category = self._categorize_error(exc)
            self._job_store.update_state(
                job.id,
                JobState.ERROR,
                error=str(exc),
                error_category=category,
            )
            logger.error("Job %s failed (%s): %s", job.id, category.value.lower(), exc)
```
This is the log line D-12 offers as an optional real reader for `error_message()`.

**Import to update** — `worker.py:23`: `from .job import ErrorCategory, JobState` (relative form; keeps working unchanged thanks to the re-export, but `classify_error`/`ACTIVE_STATES`/`BUSY_STATES` come from `.vocabulary`).

---

### `src/saneless/cli.py` (controller, event-driven — `_event_labels` deleted)

**The map and its consumer** — `cli.py:110-122`:
```python
    _event_labels: dict[PipelineEvent, str] = {
        PipelineEvent.SCANNING: "Scanning...",
        PipelineEvent.AWAITING_FLIP: "Awaiting flip...",
        PipelineEvent.SCANNING_REVERSE: "Scanning reverse sides...",
        PipelineEvent.ASSEMBLING: "Assembling PDF...",
        PipelineEvent.UPLOADING: "Uploading to paperless-ngx...",
    }

    def status_callback(event: PipelineEvent) -> None:
        if event == PipelineEvent.DONE:
            click.echo(f"Done: {title}")
        else:
            click.echo(_event_labels.get(event, str(event)))
```
Two things the planner must not lose:
1. This map is keyed by **`PipelineEvent`** and contains `SCANNING_REVERSE: "Scanning reverse sides..."`, which has **no `JobState` twin**. `status.html:9-18` is keyed by **`JobState`** and contains `PENDING: "Starting scan..."`, which has **no `PipelineEvent` twin**. The two prose vocabularies are *not* the same set — `progress_label` cannot be a straight union unless the planner keeps the CLI's `SCANNING_REVERSE` case explicit (which CONTEXT.md § Deferred already anticipates).
2. Nested-function definitions inside a Click command are the local idiom; keep `status_callback` where it is.

---

### `src/saneless/web/app.py` (config / factory — `_STATE_LABELS` deleted, filter re-backed)

**Everything that must change, in one block** — `web/app.py:28`, `:35-48`, `:101-102`:
```python
__all__ = ["create_app", "humanize_state"]
```
```python
_STATE_LABELS: dict[str, str] = {
    "PENDING": "Pending",
    "SCANNING": "Scanning",
    "AWAITING_FLIP": "Waiting for flip",
    "ASSEMBLING": "Assembling",
    "UPLOADING": "Uploading",
    "DONE": "Complete",
    "ERROR": "Failed",
}


def humanize_state(value: str) -> str:
    """Convert a JobState enum value to a human-readable label."""
    return _STATE_LABELS.get(value, value)
```
```python
    app.state.templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
    app.state.templates.env.filters["humanize_state"] = humanize_state
```

**This is the only Jinja registration in the codebase** — `grep -n 'env\.' src/saneless/web/` returns exactly line 102. No `env.globals`, no `env.tests` exist today, so exposing `JobState` as a global (RESEARCH.md § Pattern 4) is a new mechanism on the same object, registered on the line directly below:
```python
    app.state.templates.env.filters["state_label"] = state_label
    app.state.templates.env.globals["JobState"] = JobState
```

**Two out-of-file couplings to `humanize_state`** if the filter is renamed:
- `tests/test_web.py:36` — `from saneless.web.app import create_app, humanize_state`
- `web/app.py:28` `__all__`

---

### `src/saneless/web/templates/partials/status.html` and `index.html` (templates)

**The literal state lists** — `status.html:1-7`:
```jinja
{% set active_states = ["PENDING", "SCANNING", "ASSEMBLING", "UPLOADING", "AWAITING_FLIP"] %}
<div id="status-area"
     {% if job and job.state.value in active_states %}
     hx-get="/api/jobs/current/status"
     hx-trigger="every 1s"
     hx-swap="outerHTML"
     {% endif %}>
```

**The prose chain** — `status.html:8-25` (the four `aria-busy` arms collapse; the `AWAITING_FLIP` include and the `DONE`/`ERROR` markup are structurally different and stay):
```jinja
  {% if job %}
    {% if job.state.value == "PENDING" %}
      <p aria-busy="true">Starting scan...</p>
    {% elif job.state.value == "SCANNING" %}
      <p aria-busy="true">Scanning...</p>
    {% elif job.state.value == "AWAITING_FLIP" %}
      {% include "partials/flip.html" %}
    {% elif job.state.value == "ASSEMBLING" %}
      <p aria-busy="true">Assembling PDF...</p>
    {% elif job.state.value == "UPLOADING" %}
      <p aria-busy="true">Uploading to paperless-ngx...</p>
    {% elif job.state.value == "DONE" %}
      <p class="status-done">&#10003; Done: {{ job.title }}</p>
      <div hx-get="/api/jobs/history" ... class="htmx-hidden"></div>
    {% elif job.state.value == "ERROR" %}
      <p role="alert" class="status-error">&#10007; Error: {{ job.error }}</p>
```

**The three-way button spelling** — `index.html:57-60`:
```jinja
        <button type="submit" id="scan-btn"
                {% if job and job.state.value in ["PENDING", "SCANNING", "AWAITING_FLIP", "ASSEMBLING", "UPLOADING"] %}disabled{% if job.state.value != "AWAITING_FLIP" %} aria-busy="true"{% endif %}{% endif %}>
            {% if job and job.state.value == "AWAITING_FLIP" %}Waiting for flip&#8230;{% elif job and job.state.value in ["PENDING", "SCANNING", "ASSEMBLING", "UPLOADING"] %}Scanning&#8230;{% else %}Scan{% endif %}
        </button>
```
`index.html:58` is the `ACTIVE_STATES` spelling; `:59`'s second list is the `BUSY_STATES` spelling — the concrete evidence for D-04.

**The filter-pipe idiom to imitate** — `partials/history.html:7-8` (the *only* filter call site in the tree):
```jinja
  <td class="{% if job.state.value == 'DONE' %}status-done{% elif job.state.value == 'ERROR' %}status-error{% endif %}">
    {{ job.state.value | humanize_state }}
  </td>
```

**Template context contract** — the `job` object reaches every one of these templates as a `Job` instance, from `routes.py:90-100` (`index.html`) and `routes.py:172-176`, `:194-198`, `:301-305`, `:320-324` (`partials/status.html`):
```python
    return state.templates.TemplateResponse(
        request,
        "partials/status.html",
        {"job": job},
    )
```
Nothing else is passed, which is why `Job.is_active` / `Job.is_busy` (properties) is the right seam and a frozenset global is not.

---

### `src/saneless/scanner/base.py` (`SourceKind` + `classify_source()`)

**`base.py` today has zero enums and zero module-level functions**, so two analogs combine:

*For the enum:* `job.py:24-33` (above) — same StrEnum conventions.

*For the classifier function:* `auto_profiles.py:32-60` — the codebase's existing `str -> classification` function, and the one whose rule set `classify_source` must subsume:
```python
def source_to_slug(source: str) -> str:
    """
    Convert a SANE source name to a profile slug.

    Maps common scanner source names to descriptive, URL-safe slugs.
    Handles flatbed, ADF simplex, ADF duplex, and falls back to a
    basic slugification for unknown source types.

    Args:
        source: SANE source name string (e.g., "Flatbed", "ADF Duplex").

    Returns:
        A lowercase hyphenated slug string.

    """
    lower = source.lower()
    if lower == "auto":
        return "auto-scan"
    if "flatbed" in lower:
        return "flatbed-scan"
    if "duplex" in lower:
        return "adf-duplex"
    if "back" in lower:
        # ADF Back is not simplex or duplex -- use fallback slugification
        return lower.replace(" ", "-").replace("_", "-")
    if "adf" in lower or "document feeder" in lower or "feeder" in lower:
        return "adf-simplex"
    # Fallback: slugify the source name
    return lower.replace(" ", "-").replace("_", "-")
```
Copy: `lower = source.lower()` computed once at the top; **`lower == "auto"` as an exact match, checked first** (this is exactly the guard RESEARCH.md flags as the `"Automatic Document Feeder"` pitfall — the existing code already gets it right); guard-clause `if`-returns; inline `# ...` comment for the non-obvious arm; trailing fallback with a comment.

`__all__` at `base.py:20` is `["DeviceCapabilities", "DeviceInfo", "ScanSettings", "ScannerBackend"]` and must gain `"SourceKind"` and `"classify_source"` in sorted position. `scanner/__init__.py:9-17` re-exports from `.base` and would also need updating if the new names should be importable as `saneless.scanner.SourceKind` — the existing list is the template.

---

### `src/saneless/scanner/sane_backend.py` (delegate to the classifier)

**The rule being deleted** — `sane_backend.py:158-160`:
```python
def _is_adf_source(source: str) -> bool:
    """Check if the source string indicates an ADF source."""
    return "adf" in source.lower()
```

**Its single call site and the D-11 blast point** — `sane_backend.py:496-510`:
```python
            use_adf = _is_adf_source(effective_source)

            # D-04: Override for "Auto" source using config-driven routing
            if effective_source == "Auto":
                use_adf = settings.auto_source_mode == "adf"
                logger.info(
                    "Auto source routing: auto_source_mode='%s', use_adf=%s",
                    settings.auto_source_mode,
                    use_adf,
                )

            if use_adf:
                # ADF/duplex: use multi_scan() for multi-page acquisition
                for page in self._scan_adf_pages(dev):
```
Note `sane_backend.py` imports from `base` with the absolute multi-line form (`:28-33`) — extend that block, don't add a new import line style.

---

### `src/saneless/auto_profiles.py` (build on the classifier)

Same excerpt as `source_to_slug` above (`:32-60`). Its 9 existing tests are `tests/test_auto_profiles.py:23-61` (`TestSourceToSlug`), all of which must stay green — they pin `"Flatbed"→flatbed-scan`, `"ADF"→adf-simplex`, `"Automatic Document Feeder"→adf-simplex`, `"ADF Duplex"→adf-duplex`, `"Adf-duplex"→adf-duplex`, `"ADF Front"→adf-simplex`, `"Auto"`/`"auto"`→`auto-scan`, and `"ADF Back"` → neither simplex nor duplex.

---

### `tests/test_vocabulary.py` (NEW — test module)

**Structural analog:** `tests/test_paper_sizes.py` — the test module for the closest leaf module. Header + class grouping, `test_paper_sizes.py:1-50`:
```python
"""Tests for paper size dimension lookup, crop utility, and config integration."""

from __future__ import annotations

from typing import get_args

import pytest
from PIL import Image
from pydantic import ValidationError

from saneless.config import ProfileConfig
from saneless.paper_sizes import PAPER_SIZES_MM, PaperSize, crop_to_paper_size
from saneless.scanner.base import ScanSettings


class TestPaperSizeLiteral:
    """PaperSize Literal type tests."""

    def test_paper_size_literal_includes_all_sizes(self) -> None:
        """PaperSize Literal includes full, a3, a4, a5, letter, legal."""
        args = get_args(PaperSize)
        assert set(args) == {"full", "a3", "a4", "a5", "letter", "legal"}


class TestPaperSizesMM:
    """PAPER_SIZES_MM constant tests."""

    def test_a4_dimensions(self) -> None:
        """PAPER_SIZES_MM['a4'] is (210.0, 297.0)."""
        assert PAPER_SIZES_MM["a4"] == (210.0, 297.0)
```
Conventions: one-line module docstring; `from __future__ import annotations`; class per subject with a short noun-phrase docstring; `-> None` on every test (ruff `ANN` is on); the test docstring **restates the assertion as a sentence**; `assert` used bare (`S101` is per-file-ignored at `pyproject.toml:129-133`).

**Requirement-ID convention in test docstrings** — the current spelling is a parenthesised ID at the **end of the docstring sentence**. Confirmed live: `tests/test_web.py:304` `"""Job history shows human-readable state labels instead of raw enum values (P12-01)."""`, `:315` `(P12-04)`, `:373` `(P12-01)`, `tests/test_cache.py:13` `(PLSS-05)`, `tests/test_worker.py:770` `(SCAN-11)`, `tests/test_job.py:21` `(UI-05)`. Both the phase-scoped form `(P12-01)` and the requirement-code form `(CTR-01)` are in use; this phase's requirements are `CTR-01`..`CTR-05`, so use those.

**Module-level "Covers requirements:" header** — used where a file spans several requirements, `tests/test_web.py:1-6`:
```python
"""
Web endpoint tests for the saneless FastAPI application.

Covers requirements: UI-01 through UI-08, PROF-03, PLSS-04,
HLTH-01, HLTH-02, LOG-03.
"""
```
`tests/test_job.py:1-5` uses the same header. `test_vocabulary.py` should carry `Covers requirements: CTR-01, CTR-02, CTR-05.`

**The test being rewritten by D-10** — `tests/test_web.py:372-378`:
```python
def test_humanize_state_filter_unit() -> None:
    """humanize_state converts enum values to human-readable labels (P12-01)."""
    assert humanize_state("DONE") == "Complete"
    assert humanize_state("ERROR") == "Failed"
    assert humanize_state("SCANNING") == "Scanning"
    assert humanize_state("AWAITING_FLIP") == "Waiting for flip"
    assert humanize_state("UNKNOWN") == "UNKNOWN"
```
Note `tests/test_web.py` is **module-level functions, not classes** — the two grouping styles coexist in this suite (`test_paperless.py`, `test_auto_profiles.py`, `test_paper_sizes.py` use classes; `test_web.py`, `test_job.py` use bare functions). Match the file being edited.

**Sentinel assertions to update** — `tests/test_paperless.py:230-231` is the representative:
```python
        result = client.upload_document(sample_pdf, title="Fallback")
        assert result == "fallback"
```
plus `:424` and `:451`. The single highest-leverage stub is `tests/conftest.py:132-138`, which every pipeline test inherits:
```python
@pytest.fixture
def mock_paperless() -> MagicMock:
    """Return a mock PaperlessClient that succeeds."""
    paperless = MagicMock()
    paperless.upload_document.return_value = "mock-task-uuid"
    paperless.poll_task.return_value = {"status": "SUCCESS"}
    return paperless
```
The other four independent stub sites are `tests/test_cli.py:142` and `:253` (hand-rolled classes with `-> str` / `-> None` annotations), `tests/test_pipeline.py:153` (`return_value = "task-uuid-123"`), and the direct `PaperlessClient` calls throughout `tests/test_paperless.py`.

---

### `docs/explanation/consume-directory-fallback.md` (line 66 only)

**The sentence to rewrite and the true statement four lines above it that models the target voice** — `:62` and `:66`:
```markdown
**Metadata is not preserved.** When using the consume directory fallback, only the PDF file is saved. Title, tags, and correspondent metadata specified for the scan job are lost. Paperless-ngx applies its default processing rules (matching rules, ASN assignment, OCR) when it picks up the file from the consume directory.
```
```markdown
**The job status shows FALLBACK.** When the consume directory fallback is used, the scan job's final status is `FALLBACK` rather than `DONE`, so users can identify which documents may need metadata corrections in paperless-ngx.
```
Convention: bold lead-in sentence, then one explanatory paragraph, single-sentence-per-idea, backticked identifiers. Keep the shape; change only the claim.

---

## Shared Patterns

### Import ordering
**Source:** `pipeline.py:8-32`, `worker.py:9-30`, `web/app.py:3-26`
**Apply to:** every `.py` file touched
```python
from __future__ import annotations

import logging          # stdlib block
import shutil
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from saneless.exceptions import ConfigError, ScanError   # first-party block
from saneless.pages import filter_empty_pages, generate_thumbnail

if TYPE_CHECKING:                                        # type-only block LAST
    import threading
    from collections.abc import Callable

    from saneless.config import Settings

__all__ = [...]
```
`TCH`/`TC` rules are enabled (`pyproject.toml:91`), so any import used **only** in annotations must live in the `if TYPE_CHECKING:` block — but re-exported names must **not**, because they are runtime exports.

### Docstring style
**Source:** `paper_sizes.py:33-48`, `job.py:121-134`, `pipeline.py:311-332`
**Apply to:** every new module, class, function, and property
Google sections `Args:` / `Returns:` / `Raises:` / `Yields:` / `Attributes:`; multi-line docstrings open with `"""` alone on its line (`D212` ignored); a **blank line before the closing `"""`**; one-line docstrings for trivial properties and nested callbacks.

### Logging
**Source:** `job.py:21`, `worker.py:34`, `pipeline.py:48`
**Apply to:** any modified module
`logger = logging.getLogger(__name__)` at module scope; `%s` lazy formatting always (`G` rules are on — f-strings inside logging calls are a lint error); `logger.warning(...)` for recoverable degradation (`paperless.py:151`, `:154`).

### Enum comparison
**Source:** `worker.py:223-232` (`event is PipelineEvent.AWAITING_FLIP`), `cli.py:119` (`event == PipelineEvent.DONE`), `sane_backend.py:499` (`effective_source == "Auto"`)
Both `is` and `==` are in use against enum members. RESEARCH.md's measured table says both narrow under `ty` and `pyrefly`. Prefer `is` (the dominant form in `worker.py`) for identity against members.

### `__all__` maintenance
**Source:** every module in `src/saneless/`
Every module declares `__all__`, sorted, immediately after imports. Adding `ScanResult`, `UploadResult`, `SourceKind`, `classify_source`, `state_label`, etc. means editing the corresponding `__all__` in the same commit — `RUF022` enforces the sort order.

---

## No Analog Found

| File / Construct | Role | Data Flow | Reason |
|------------------|------|-----------|--------|
| `@pytest.mark.parametrize` in `tests/test_vocabulary.py` | test | transform | **There is not a single `@pytest.mark.parametrize` anywhere in the 4,500-line test suite** (`grep -c parametrize tests/*.py` → 0 for all 17 files). D-09's parametrised completeness tests are a new pattern for this codebase. Use RESEARCH.md § D-09 for the shape; use `tests/test_paper_sizes.py` for the surrounding module/class/docstring conventions. `pytest` is already imported in 8 test files, `--strict-markers` does not affect built-in marks, and `parametrize` needs no `markers=` entry in `pyproject.toml`. |
| `match` / `typing.assert_never` | any | transform | Neither construct appears anywhere in `src/` or `tests/`. RESEARCH.md § Pattern 1 is the specification; there is no in-tree code to copy from. The nearest in-tree relative is the `is`-identity `if/elif` chain at `worker.py:223-233`. |
| `@property` on a `@dataclass` | model | — | No dataclass in this codebase has any method. `worker.py:120-128` (properties on a plain class) is the docstring/annotation analog; the dataclass interaction itself is new. |
| `env.globals` / `env.tests` on the Jinja environment | config | — | Only `env.filters` is used (`web/app.py:102`). If the planner exposes `JobState` as a global, that mechanism is new — register it on the line below the existing filter assignment inside `create_app`. |
| `frozen=True` / `slots=True` dataclasses | model | — | Zero occurrences across all five existing value types. Adopting it on `ScanResult`/`UploadResult` is a deliberate departure, not a pattern copy. |

## Metadata

**Analog search scope:** `src/saneless/` (all 20 modules), `src/saneless/web/templates/`, `tests/` (all 17 modules), `docs/explanation/`, `pyproject.toml`
**Files scanned:** 24 read, 17 grepped
**Verification performed:** the re-export lint question was settled by executing `uv run ruff check --stdin-filename src/saneless/job.py -` against the project's real config (result: `All checks passed!`), not inferred.
**Pattern extraction date:** 2026-09-10
