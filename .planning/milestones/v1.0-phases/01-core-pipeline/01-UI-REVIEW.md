# Phase 1 — UI Review

**Audited:** 2026-03-22
**Baseline:** Abstract 6-pillar standards (no UI-SPEC.md for this phase)
**Screenshots:** Not captured — no dev server detected; code-only audit
**UI Surface:** CLI terminal interface only (Click commands, click.echo output, error messages, help text)

---

## Scope Note

Phase 1 (core-pipeline) produces a pure CLI application with no web frontend, no HTML, no CSS, and no JavaScript. The "UI" is entirely the terminal: help text, status lines, table output, error messages, and exit codes. All 6 pillars are scored against this CLI surface using abstract standards adapted for terminal UX.

---

## Pillar Scores

| Pillar | Score | Key Finding |
|--------|-------|-------------|
| 1. Copywriting | 4/4 | Status messages are concise, action-oriented, and contextual |
| 2. Visuals | 3/4 | Table output is well-structured but column widths are hardcoded and may truncate long device names |
| 3. Color | 4/4 | No color/styling used; consistent with plain-text terminal tool expectations |
| 4. Typography | 4/4 | No font system; text hierarchy achieved through consistent capitalization and indentation |
| 5. Spacing | 3/4 | Table formatting uses fixed-width columns; no padding normalisation between command output blocks |
| 6. Experience Design | 4/4 | All error states covered with correct exit codes, graceful degradation, and actionable messages |

**Overall: 22/24**

---

## Top 3 Priority Fixes

1. **Hardcoded table column widths in `devices` and `jobs` commands** — Long scanner device names (>30 chars) and long document titles (>30 chars) will be silently truncated with no indication to the user. Fix by computing column widths dynamically from actual data: `max(len(d.name) for d in device_list)` then pad to that width plus a minimum gutter. Files: `src/saneless/cli.py:167-171` and `src/saneless/cli.py:216-223`.

2. **`devices` command emits "Discovering scanners..." before knowing whether `--json` mode suppresses it, but the empty-list case is inconsistent** — When no scanners are found and `--json` is set, the code returns `"[]"` (a string) instead of a JSON array printed via `json.dumps`. This breaks the `--json` contract (caller cannot `json.loads` on `"[]"` from click.echo which adds no newline issues, but the string literal is inconsistent with the array format used for non-empty results). Fix: replace `click.echo("No scanners found." if not as_json else "[]")` with `click.echo(json.dumps([]))` in the json branch. File: `src/saneless/cli.py:151`.

3. **`jobs` command table truncates titles at 30 chars with no ellipsis** — A document titled "Important 2026 Tax Return Documents" would display as "Important 2026 Tax Return Docu" with no visible indicator of truncation. This causes silent data loss in the display. Fix: add an ellipsis truncation helper: if `len(title) > 28` then `title[:27] + "…"`, or dynamically size the column. File: `src/saneless/cli.py:220-223`.

---

## Detailed Findings

### Pillar 1: Copywriting (4/4)

The CLI copy is well-crafted throughout.

**Status messages** (`src/saneless/pipeline.py:234, 269, 274, 293`) follow the design-spec pattern exactly:
- `"Scanning..."` — active progressive form
- `"Assembling PDF..."` — specific and informative
- `"Uploading to paperless-ngx..."` — identifies destination, avoids vague "uploading..."
- `"Done: {title}"` — confirms success with document identity

**Error messages** are actionable:
- `"Configuration error: {exc}"` (cli.py:59) — routes the user to config rather than a raw stack trace
- `"Unknown profile: {profile}"` (cli.py:91) — names the bad value
- `"Scan error: {exc}"` / `"Paperless error: {exc}"` (cli.py:119, 122) — distinguishes error domain
- `"No scanners found."` (cli.py:151) — clear empty state
- `"No new profiles written (use --force to overwrite)."` (cli.py:288) — tells user what to do

**Help text** (cli.py:53, 87, 141, 192, 232, 267) is terse but complete. All required options are documented.

**No generic labels found** — no "Submit", "Click Here", "OK", or "Cancel" patterns. The one `Submit` match (worker.py:82) is a docstring word, not user-facing.

No issues.

---

### Pillar 2: Visuals (3/4)

**Table structure** is present for both `devices` and `jobs` commands and uses fixed-column formatting with a header separator (cli.py:167-171, 216-223).

**Column width issue** — Device name column is `{d.name:<30}`, vendor `{d.vendor:<15}`, model `{d.model:<20}`. SANE device name strings such as `"net:192.168.1.50:fujitsu:ScanSnap iX500:1"` are 43 characters and will overflow into the adjacent column or be truncated by f-string formatting (the `<30` format simply truncates if the string exceeds width). This destroys table alignment with no user feedback.

**`capabilities` output** (cli.py:177-185) uses consistent two-space indentation. The section header `"Capabilities for {d.name}:"` provides clear visual grouping via blank line separator above (cli.py:174).

**JSON output** (`--json` flag) uses `indent=2` (cli.py:164, 213) giving readable structured output — good choice.

**Minor gap**: No terminal width awareness. Wide outputs have no soft-wrap protection. Acceptable for v1 but worth noting.

---

### Pillar 3: Color (4/4)

No ANSI color, no Tailwind, no CSS. The application produces plain text output consistently across all commands. This is appropriate for a tool intended for scripting and daemon use (`--json` flags confirm machine-readable output is a first-class concern).

No hardcoded color sequences found. No accent overuse (there is no accent). The visual distinction between errors and normal output is correctly achieved via stderr vs stdout routing (`err=True` on all error echoes), which is the correct terminal equivalent of color differentiation.

No issues.

---

### Pillar 4: Typography (4/4)

No font system applies. Terminal text hierarchy is established through consistent conventions:

- **ALL_CAPS** used for job states (`PENDING`, `SCANNING`, `DONE`, `ERROR`) — consistent, machine-readable, visually distinct
- **Title Case** for table headers (`Name`, `Vendor`, `Model`, `Type`, `Timestamp`, `Profile`, `Title`, `Status`)
- **Sentence case** for status messages (`"Scanning..."`, `"Assembling PDF..."`)
- **Consistent indentation**: two-space indent for capabilities sub-items (cli.py:178-185)

No weight/size inconsistency exists because there is no font system. The terminal hierarchy conventions are applied uniformly.

No issues.

---

### Pillar 5: Spacing (3/4)

**Table column widths** are hardcoded integers that create implicit spacing decisions:
- `devices` table: `{d.name:<30} {d.vendor:<15} {d.model:<20} {d.device_type}` (cli.py:171) — single space between columns
- `jobs` table: `{j.created_at.strftime(...):<22} {j.profile:<15} {j.title:<30} {j.state.value}` (cli.py:221-223) — single space between columns

**Inconsistency**: The `devices` table uses `"-" * len(header)` for the separator (cli.py:169) but `header` includes trailing spaces from the last column (`{d.device_type}` has no padding spec, so header width varies by terminal). The separator length is non-deterministic based on the string value.

**`capabilities` output block** has a blank line separator before each device (cli.py:174: `click.echo()`), but the `devices` table output has no trailing blank line before capabilities begin. This causes the table and capabilities sections to visually merge.

**`jobs` output** has no blank line between the table and any following output — acceptable for the current single-output-mode design.

No arbitrary spacing values (no hardcoded pixel values — not applicable to CLI).

---

### Pillar 6: Experience Design (4/4)

This pillar translates well to CLI:

**Error state coverage:**
- Config errors: caught at CLI group level, exit 2, actionable message (cli.py:56-60)
- Profile not found: explicit check before pipeline, exit 2 (cli.py:90-92)
- Scan errors: caught, exit 1, domain-labeled message (cli.py:118-120)
- Paperless errors: caught, exit 3, domain-labeled message (cli.py:121-123)
- Worker errors: categorized into FEEDER, CONFIG, SCANNER, UPLOAD, UNKNOWN (worker.py:130-149)

**Loading/progress states:**
- Pipeline progress is signaled via status callbacks: "Scanning...", "Assembling PDF...", "Uploading to paperless-ngx..." (pipeline.py:234, 269, 274)
- `devices` command signals discovery start: "Discovering scanners..." (cli.py:146)
- Job state machine (PENDING → SCANNING → ASSEMBLING → UPLOADING → DONE) provides programmatic visibility via `saneless jobs`

**Empty states:**
- No scanners found: "No scanners found." (cli.py:151)
- No jobs in history: `jobs` command outputs empty table (header + separator, no rows) — acceptable
- All pages empty after filter: raises `ScanError("All pages were detected as empty")` (pipeline.py:265-266) — surfaces clearly

**Graceful degradation:**
- Log directory unwritable: falls back to stderr-only logging (logging_config.py:56-60)
- Upload failure with consume_dir configured: falls back to file copy, returns "fallback" (paperless.py:156-164)
- ADF feeder empty: raises `FeederEmptyError` distinct from generic `ScanError`, enabling targeted web UI handling

**Exit code contract** matches the spec exactly: 0 success, 1 scan error, 2 config error, 3 paperless error (cli.py:60, 92, 120, 123).

No issues.

---

## Registry Safety

No `components.json` found. Shadcn not initialized. Registry audit skipped.

---

## Files Audited

- `/home/kris/git/saneless/src/saneless/cli.py`
- `/home/kris/git/saneless/src/saneless/pipeline.py`
- `/home/kris/git/saneless/src/saneless/worker.py`
- `/home/kris/git/saneless/src/saneless/job.py`
- `/home/kris/git/saneless/src/saneless/config.py`
- `/home/kris/git/saneless/src/saneless/exceptions.py`
- `/home/kris/git/saneless/src/saneless/logging_config.py`
- `/home/kris/git/saneless/src/saneless/paperless.py`
- `/home/kris/git/saneless/src/saneless/scanner/base.py`
- `/home/kris/git/saneless/.planning/phases/01-core-pipeline/01-CONTEXT.md`
- `/home/kris/git/saneless/.planning/phases/01-core-pipeline/01-01-SUMMARY.md` through `01-05-SUMMARY.md`
