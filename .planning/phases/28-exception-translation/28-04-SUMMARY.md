---
phase: 28-exception-translation
plan: 04
subsystem: pdf
tags: [exceptions, img2pdf, pillow, boundary, tdd]
requires:
  - "saneless.exceptions.PdfError (28-01)"
  - "saneless.exceptions.describe (28-01)"
provides:
  - "assemble_pdf raises only PdfError"
  - "assemble_pdf refuses an empty page list"
affects: [28-09]
tech-stack:
  added: []
  patterns:
    - "Narrow-span, broad-type boundary catch: except PdfError: raise, then except Exception as exc: raise PdfError(msg) from exc"
key-files:
  created: []
  modified:
    - src/saneless/pdf.py
    - tests/test_pdf.py
decisions:
  - "assemble_pdf catches Exception, not a tuple: img2pdf's seven classes share no base, and img2pdf and Pillow also raise Exception, TypeError, ValueError, OSError and SystemError (D-04)"
  - "The try covers mkdir through the end of the TemporaryDirectory block, so temp-dir cleanup failures are translated too"
  - "An empty page list raises PdfError before any filesystem work, keeping img2pdf's empty-list ValueError unreachable (N-06)"
metrics:
  duration: 9min
  completed: 2026-09-15
  tasks: 1
  files: 2
requirements: [EXC-01, EXC-03]
---

# Phase 28 Plan 04: img2pdf Boundary Summary

`assemble_pdf` now raises only `PdfError`. Every img2pdf error class, img2pdf's untyped raises, Pillow's page-save failures and the "convert returned None" guard become `PdfError`. The message is `Could not assemble N page(s) into <pdf path>: <original text>`, and the original exception is on `__cause__`. An empty page list is refused before anything touches the filesystem.

## What Was Built

### Task 1: every assembly failure becomes PdfError
- **`src/saneless/pdf.py`**
  - Imports `PdfError` and `describe` from `saneless.exceptions`.
  - `if not images:` raises `PdfError("Could not assemble a PDF: no pages were given")`. A comment points at the pipeline's `_require_pages` and N-06.
  - `pdf_path` is computed before the `try`. The `try` covers `output_dir.mkdir`, the PNG saves, `img2pdf.convert`, `write_bytes` and the `TemporaryDirectory` exit.
  - The None guard raises `PdfError` rather than `RuntimeError`.
  - Clauses run in this order: `except PdfError: raise` first, so the None guard is not wrapped twice, then `except Exception as exc:`, which raises `PdfError(msg) from exc`.
  - The docstring explains why the catch is `Exception`, in the same way `_preserving` does: seven classes with no shared base, plus the untyped raises. It also says the span is narrow, the original is always chained, and `KeyboardInterrupt` and `SystemExit` pass through. A `Raises:` section names `PdfError`.
- **`tests/test_pdf.py`**: new `TestPdfBoundary` class (16 tests; its docstring cites M-17, D-04 and success criterion 1):
  - One parametrised row for each of the seven real `img2pdf` error classes. Each checks the message, `__cause__ is original`, and that the error is not an instance of the img2pdf class.
  - Rows for bare `Exception`, `TypeError` and `ValueError`.
  - Real Pillow failures: a CMYK page gives `OSError` as the cause, and a 0x0 page gives `SystemError`.
  - `convert` returning `None`.
  - The message shape: it starts with the page count and path prefix and ends with the original text.
  - An empty list raises and `convert` is never called.
  - `KeyboardInterrupt` passes through.
  - `test_temp_files_cleaned_on_error` now expects `PdfError`, and it still checks that no PNG files are left behind.

## Verification

- `uv run pytest tests/test_pdf.py -q`: 80 passed. `-k Boundary` selects 16 tests, all passing.
- `uv run pytest tests/test_pipeline.py tests/test_outcomes_e2e.py tests/test_pdf.py -q`: 184 passed.
- `uv run pytest -m "not browser and not sane_hardware" -q`: 1739 passed.
- `uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check` and `uv run pyrefly check src tests` all report 0 errors. pyrefly's 4 warnings were already there, in `pipeline.py`, `sane_backend.py` and `fake_sane.py`, none of which this plan touches.
- Acceptance greps:
  - `raise RuntimeError` is gone.
  - `raise PdfError(msg) from exc` appears once, at line 237.
  - `except PdfError:` (229) comes before `except Exception as exc:` (233).

## Deviations from Plan

None. The plan was executed as written. Two small notes:
- **KeyboardInterrupt row:** it passed during RED, as expected. It guards behaviour that already existed (the new `except Exception` must not swallow it), so it is not a sign the feature was already there. The other 15 boundary rows and the updated cleanup test all failed during RED.
- **Test typing:** `Callable` is imported inside a `TYPE_CHECKING` block in the tests, to satisfy ruff TC003.

## TDD Gate Compliance

| Task | RED commit | GREEN commit |
|------|------------|--------------|
| 1 | bdf7205 | c62909a |

No refactor commit was needed.

## Threat Mitigations

- T-28-12: the boundary catch covers assembly only, and every failure kind has a test.
- T-28-14: `KeyboardInterrupt` is not caught, and a test asserts that it propagates.

## Known Stubs

None.

## Self-Check: PASSED

- FOUND: src/saneless/pdf.py (PdfError boundary, empty-list refusal)
- FOUND: tests/test_pdf.py (TestPdfBoundary)
- FOUND commits: bdf7205, c62909a
