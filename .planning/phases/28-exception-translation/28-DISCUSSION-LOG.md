# Phase 28: Exception Translation - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-09-15
**Phase:** 28-exception-translation
**Areas discussed:** Cancelled jobs, PdfError and exit codes, Wrapped message shape, Troubleshooting docs

---

## Cancelled jobs

| Option | Description | Selected |
|--------|-------------|----------|
| New CANCELLED state (Recommended) | Terminal JobState, "Cancelled" label, neutral styling; FALLBACK precedent; ScanCancelledError carries it | ✓ |
| ERROR + CANCELLED category | Smaller change; history still reads "Error" unless templates branch on category | |

**User's choice:** New CANCELLED state

| Option | Description | Selected |
|--------|-------------|----------|
| Only an explicit abort (Recommended) | Web Abort, `n`, Ctrl-C/Ctrl-D at the prompt; timeout, prompt read failure, shutdown stay ERROR | ✓ |
| Abort + flip timeout | Timeout also CANCELLED | |

**User's choice:** Only an explicit abort

| Option | Description | Selected |
|--------|-------------|----------|
| 130 for every cancel (Recommended) | Shell SIGINT convention; one rule for Ctrl-C anywhere in the CLI | ✓ |
| Keep exit 1 | Same as a scan error | |
| 0, it's not an error | Scripts can't tell uploaded from nothing happened | |

**User's choice:** 130 for every cancel
**Notes:** INFO log level, neutral styling and wording left to Claude.

---

## PdfError and exit codes

| Option | Description | Selected |
|--------|-------------|----------|
| Own branch, new category, exit 1 (Recommended) | Sibling of ScanError, new ErrorCategory, exit-code table unchanged | |
| Own branch, new category, exit 4 | Same, plus a distinct exit code for PDF assembly | ✓ |
| PdfError(ScanError) | Exit 1, SCANNER category; still blames the scanner | |

**User's choice:** Own branch, new category, exit 4

| Option | Description | Selected |
|--------|-------------|----------|
| Every SANE command refuses, exit 2 (Recommended) | scan/devices/auto-profiles/serve refuse with install hint | ✓ |
| Refuse, exit 1 | Same under runtime-error code | |
| serve starts, scans fail | Web UI up, each scan fails with the hint | |

**User's choice:** Every SANE command refuses, exit 2

| Option | Description | Selected |
|--------|-------------|----------|
| One line, traceback to log, exit 1 (Recommended) | Last-resort handler; `-v` mirrors traceback | |
| Same, distinct exit code | Script can tell a saneless bug from a scan failure | ✓ |
| Let bugs show tracebacks | Only known failures translated | |

**User's choice:** Same, distinct exit code

| Option | Description | Selected |
|--------|-------------|----------|
| 5 (Recommended) | Continues the small-integer scheme | ✓ |
| 70 (EX_SOFTWARE) | sysexits.h meaning | |

**User's choice:** 5
**Notes:** Same table applies to every command; `jobs` on a fresh install must not traceback (Claude's discretion).

---

## Wrapped message shape

| Option | Description | Selected |
|--------|-------------|----------|
| Context: original (Recommended) | What saneless was doing + identifiers, then original text; class name when str() is empty | ✓ |
| Context: original (Class) | Plus third-party class name in brackets | |
| Original message only | Context only in logged traceback | |

**User's choice:** Context: original

| Option | Description | Selected |
|--------|-------------|----------|
| JSON detail, else ~200 chars (Recommended) | DRF detail / first field error, else collapsed and truncated; full body at DEBUG | ✓ |
| Status line only | Body only in DEBUG log | |

**User's choice:** JSON detail, else ~200 chars

| Option | Description | Selected |
|--------|-------------|----------|
| All transient transport errors (Recommended) | Every TransportError retries then falls back; bad URL fails at once; duplicate message warns | ✓ |
| Only 'never reached Paperless' | Connect-phase only; narrows today's ReadTimeout retry | |
| Wrap only, no retry change | Narrowest EXC-01 reading | |

**User's choice:** All transient transport errors
**Notes:** User accepted two derived decisions: `poll_task` keeps polling through transport errors until the deadline; TOML syntax errors become ConfigError under Phase 27's header with line/column.

---

## Troubleshooting docs

| Option | Description | Selected |
|--------|-------------|----------|
| New how-to by symptom (Recommended) | `docs/how-to/troubleshoot-a-failed-scan.md` by exit code then symptom; tables gain 4/5/130; doc-truth test | ✓ |
| Reference catalogue of messages | `docs/reference/errors.md`; churns when Phase 30 rewords | |
| No new page | Update tables and existing sections in place | |

**User's choice:** New how-to by symptom

---

## Claude's Discretion

- ScanCancelledError hierarchy placement
- Name of the PDF ErrorCategory and its user_message sentence
- Location of the python-sane import check helper
- CANCELLED styling tokens (23.1 convention)
- Exact wording of new messages
- `saneless jobs` on a fresh install
- Test shape for the per-library parametrised boundary tests

## Deferred Ideas

- Keeping pages when PDF assembly fails (HARD-01, Phase 29)
- `paperless.url` load-time validation (no requirement; milestone audit)
- Plain-language messages (APPL-04, Phase 30)
- Safe device cancel on Ctrl-C mid-read (HARD-03, Phase 29)
- Metadata-fetch cause logging (SWP-04, Phase 32)
- Reference catalogue of error messages (rejected)

---

## Post-research decisions (2026-09-15, during plan-phase)

| Question | Options | Selected |
|----------|---------|----------|
| D-10 premise corrected: paperless-ngx v3 consumes a duplicate as a second document by default. Keep widened retry? | Keep wide retry, accept copies (Recommended) / Retry only before sending / Wide retry but no fallback after send | Keep wide retry, accept copies |
| `serve` exit codes outside D-07 (bind failure = 1, uvicorn startup failure = 3) | Document as serve-specific (Recommended) / Remap to fit D-07 | Remap to fit D-07 (both exit 2) |
