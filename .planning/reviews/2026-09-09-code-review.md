# saneless — Comprehensive Code Review

**Date:** 2026-09-09 (usability section and naming decision added the same day)
**Commit reviewed:** e905f64 (branch `autodev`)
**Reviewer:** Claude Fable 5.1, six parallel review agents plus lead verification
**Framework:** Google Engineering Practices, ["What to look for in a code review"](https://google.github.io/eng-practices/review/reviewer/looking-for.html)
**Scope:** Entire repository, treated as a first-ever review: `src/saneless/` (about 4,000 lines), `tests/` (about 6,000 lines), packaging, Docker, CI workflows, and user documentation.

## How to read this review

This review is written for a developer early in their career. Each finding says **what** is wrong, **why** it matters (the concrete way it fails and who gets hurt), and **how** to fix it. Where a finding illustrates a general principle, the principle is spelled out so you can apply it next time without a reviewer.

Findings are graded on four levels:

| Severity | Meaning | What to do |
|---|---|---|
| **CRITICAL** | Data loss, a security hole, a crash or hang on a normal path, or a silently wrong result. | Fix before the next release. |
| **MAJOR** | A bug users will realistically hit, a resource leak, a race, a missing test on a critical path, or a design flaw that will make future change expensive. | Fix soon; schedule it. |
| **MINOR** | A correctness nit, a misleading name or comment, a weak test, a small inconsistency. | Fix when you touch the file. |
| **NIT** | Style or preference. Google's convention is to prefix these with "Nit:" so the author knows they are optional. | Your call. |

Every finding also carries a **confidence**: *Confirmed* means the reviewer executed code or the code path is unambiguous; *Likely* means a strong reading that was not executed. Treat *Likely* findings as things to verify first, not things to fix blindly.

Findings carry consolidated IDs: `C-` for critical, `M-` for major, `N-` for minor and nit, and `U-` for the usability findings in section 11, which were written from the home operator's perspective after the code pass. Each also lists the per-reviewer IDs it was merged from (`SCAN` scanner backend, `PIPE` pipeline and auto-profiles, `CORE` Paperless client, job store, config, and logging, `WEB` worker and web UI, `OPS` CLI, packaging, deployment, and docs, `XC` cross-cutting design and test quality). When several reviewers found the same defect independently, that is noted, because independent rediscovery is strong evidence.

**Contents.** 1 Verdict and executive summary · 2 Findings at a glance · 3 Critical findings · 4 Major findings · 5 Minor findings and nits · 6 What is done well · 7 Lessons that cut across the findings · 8 Documentation accuracy audit · 9 Test suite assessment · 10 Recommended remediation order · 11 Usability review: the appliance test · Appendix A Methodology and verification · Appendix B Reviewer finding IDs

## The Google checklist, in one paragraph each

Google's reviewer guide asks you to look at code through a fixed set of lenses, in roughly this order of importance. **Design** asks whether the pieces fit together and belong where they are. **Functionality** asks whether the code does what the author intended and what users need, with special attention to edge cases, concurrency, and error paths, because those are where bugs hide. **Complexity** asks whether a reader can understand the code quickly, and warns against building for imagined future needs. **Tests** asks not only whether tests exist but whether they would actually fail if the code broke. **Naming**, **Comments**, **Style**, and **Consistency** cover readability; the guide's rule of thumb is that comments explain *why*, because the code already says *what*. **Documentation** asks whether anything user-facing changed without its docs changing. Finally, the guide insists a reviewer read **every line** and consider the **context** of the whole system, and reminds reviewers to call out the **good things**, because positive examples teach as much as corrections do.
## 1. Verdict and executive summary

**Verdict: not ready to release, but close to being a good codebase.** The foundations are strong: the module graph is clean and acyclic, the scanner abstraction is a real seam that keeps python-sane out of the web and pipeline layers, every module declares its public API, logging is disciplined, the linters and both type checkers pass with zero findings, and 340 tests pass. That is a better starting point than most first projects. The problems are concentrated in one pattern: the modules agree on types but not on contracts. A value is returned that nobody reads, a string carries two meanings that two layers interpret differently, a heuristic is implemented four ways, a fake models an API that does not exist. Each of those gaps is small to fix, and each has produced a user-visible defect on a normal path.

The six reviewers produced 136 raw findings. After removing duplicates found independently by several reviewers, this report consolidates them to 89:

| Severity | Count | Fix before |
|---|---|---|
| CRITICAL | 10 | next release |
| MAJOR | 34 | soon; schedule them |
| MINOR and NIT | 45 | when touching the file |
| USABILITY (section 11) | 10 | alongside the critical fixes; see section 11's priority list |

Every CRITICAL finding and most MAJOR findings were demonstrated by executing code, not just reading it. The lead reviewer independently re-verified all ten critical findings.

**The ten things a user would notice first:**

1. **Manual duplex does not work on real ADF hardware** (C-01). The documented `source = "Manual Duplex"` string is handed to the SANE backend, which rejects it.
2. **The CLI never pauses for the flip** (C-02). Pass B starts immediately after pass A, and the docs promise a prompt that does not exist.
3. **Failures are reported as success** (C-03). A document Paperless rejects as a duplicate, a poll that times out, a file that only reached the consume directory, and a duplex mismatch all show a green "Complete".
4. **A failed upload deletes the scan** (C-04). Fifty pages fed through the ADF are discarded when one HTTP POST fails.
5. **Consume-directory fallbacks overwrite each other** (C-05). Every PDF is named `output.pdf`, so during an outage only the last scan survives.
6. **Common scanners get one page instead of a stack** (C-06). Feeders named "Automatic Document Feeder" (Epson, Canon, Brother) are routed to the single-page flatbed path.
7. **The database is shared between threads without a lock** (C-07). A two-thread stress test fails in every run, and a failure in the worker kills it for the life of the process.
8. **First run can brick the second run** (C-08). Auto-profiles for a sheet-fed scanner writes a config file without the mandatory default profile.
9. **A dead worker hangs the whole server** (C-09). The eleventh scan blocks the event loop forever, and shutdown blocks too.
10. **The Scan button never re-enables** (C-10). After every web scan the button stays "Scanning…" until the page is reloaded.

**Why the tests did not catch these.** Line coverage is 94 percent, which reads as well tested, but the uncovered six percent is exactly the integration seams: no test drives the worker through the real pipeline, no test runs manual duplex against the real backend contract, no test submits a scan in a browser, no test uses the job store from two threads, and no test checks what happens when Paperless says no. The test doubles are also more convenient than the truth: a `MagicMock` scanner accepts any source string, and the SANE fake raises where the real library silently succeeds. Section 9 covers this in detail.

**The operator's view.** Section 11 re-examines the project from the chair of a non-developer running the container at home for their family, which is the project's stated goal of feeling like an appliance. It adds ten `U-` findings, four of them new (the compose template's placeholder token overrides the config file, the UI never shows a page count, there is no status or test view, and the flip prompt is shared by every viewer) and ends with a prioritised definition of what appliance-grade would mean here.

**Repository naming.** The canonical remote is `git@github.com:kdknigga/saneless.git`. Every reference to `github.com/kris-knigga/saneless`, `ghcr.io/kris-knigga/saneless`, or `kris-knigga.github.io/saneless` in shipped files is wrong and must be updated to the `kdknigga/saneless` forms; M-27 lists all 24 lines in nine files.

**What to do first.** Section 10 gives an ordered remediation plan. The short version: fix the contract bugs in the pipeline and worker (C-01 through C-05, C-09) as one coherent change with end-to-end tests, then the scanner-classification and thread-safety bugs (C-06, C-07), then the two one-liners (C-08, C-10), then stand up CI (M-25) so the suite runs somewhere nobody can bypass.

## 2. Findings at a glance

Consolidated IDs are used throughout this report. The right-hand column lists the per-reviewer finding IDs that were merged into each row, so a reader who wants the original evidence can ask for the individual report.

### Critical

| ID | Title | Area | Merged from |
|---|---|---|---|
| C-01 | Documented manual-duplex setup cannot work on real ADF hardware | Pipeline, Scanner | PIPE-01, XC-02 |
| C-02 | CLI never waits for the flip | CLI, Pipeline | PIPE-02, OPS-01, XC-03 |
| C-03 | Pipeline outcome thrown away; failures display as Complete | Pipeline, Worker, CLI | PIPE-04, CORE-02, XC-01, OPS-11, WEB-04 |
| C-04 | Upload failure after a successful scan deletes the PDF | Pipeline | PIPE-03 |
| C-05 | Every PDF is `output.pdf`; fallbacks overwrite each other | PDF, Paperless | PIPE-05, CORE-03 |
| C-06 | Feeders not named "ADF" routed to the single-page flatbed path | Scanner | SCAN-01, XC-06 |
| C-07 | One SQLite connection shared by two threads with no lock | Job store, Worker | CORE-01 |
| C-08 | Auto-profiles for a sheet-fed scanner writes an unloadable config | Auto-profiles | PIPE-06 |
| C-09 | Worker dies silently; eleventh scan hangs the server and shutdown | Worker, Web | WEB-03, WEB-07 |
| C-10 | Scan button never re-enables after a job | Web (JavaScript) | WEB-01 |

### Major

| ID | Title | Area | Merged from |
|---|---|---|---|
| M-01 | Blocking I/O inside `async def` routes freezes the UI and `/health` | Web | WEB-02, XC-05 |
| M-02 | Job stays AWAITING_FLIP through pass B; Abort is a no-op | Worker, Web | WEB-05, PIPE-17 |
| M-03 | No crash recovery; interrupted jobs lock the UI; shutdown ordering | Web, Job store | WEB-06, CORE-09 |
| M-04 | Worker writes profiles to a self-resolved path, ignores `--config`; `is_bare_default` too narrow | Worker, Auto-profiles | WEB-08, XC-04, PIPE-11 |
| M-05 | Two state enums, three label maps, three active-state lists | Cross-cutting | XC-08, WEB-10 |
| M-06 | PDFs get 96 DPI page geometry | PDF | PIPE-07 |
| M-07 | Flip wait has no timeout | Pipeline | PIPE-08 |
| M-08 | Whole batch held in RAM; disk pre-check does not scale | Pipeline, Scanner | PIPE-09, SCAN-08 |
| M-09 | `--force` replaces hand-written profiles and drops unknown keys | Auto-profiles | PIPE-10, OPS-26 |
| M-10 | Config rewritten non-atomically, locale-dependent encoding | Auto-profiles | PIPE-12 |
| M-11 | First-page errors all reported as "No paper detected" | Scanner | SCAN-02 |
| M-12 | Timeout closes the device while a thread is inside `sane_read()` | Scanner | SCAN-03 |
| M-13 | Flatbed path has no timeout or validation | Scanner | SCAN-07 |
| M-14 | Scanner-level white-page check bypasses the toggle, breaks duplex parity | Scanner | SCAN-04 |
| M-15 | Crop fallback unreachable; real python-sane accepts unknown attributes | Scanner, Tests | SCAN-05 |
| M-16 | Crop maths uses requested DPI; option order lets source clamp resolution | Scanner | SCAN-06, SCAN-13 |
| M-17 | Third-party exceptions leak at every boundary; CLI tracebacks | Cross-cutting | XC-07, CORE-06, CORE-14, SCAN-11, OPS-09, PIPE-15 |
| M-18 | Misspelled keys inside config sections silently ignored | Config | CORE-04 |
| M-19 | Nonexistent `--config` path silently ignored | Config, CLI | CORE-05, OPS-08 |
| M-20 | `~` not expanded; XDG variables ignored | Config | CORE-07 |
| M-21 | `log_level` unvalidated; `-v` does not enable DEBUG | Config, Logging, CLI | CORE-08, OPS-14 |
| M-22 | `poll_task` swallows non-200; clock ignores request time | Paperless | CORE-10 |
| M-23 | All user-facing dates and times are UTC without saying so | Cross-cutting | XC-09, PIPE-18, WEB-11 |
| M-24 | Documented `title` profile key is never read | Config, Docs | XC-11, OPS-10 |
| M-25 | No CI on push or pull request | Delivery | OPS-02 |
| M-26 | Release workflow cannot succeed | Delivery | OPS-03 |
| M-27 | Published names differ from the actual remote; nothing published | Delivery, Docs | OPS-04 |
| M-28 | Docker logs invisible to `docker logs` | Delivery | OPS-05 |
| M-29 | Healthcheck port 8080 versus example config 8081 | Delivery | OPS-06 |
| M-30 | Read-only compose mount breaks auto-profiles; docs wrong about missing file | Delivery, Docs | OPS-07 |
| M-31 | No `.dockerignore`; secret config enters the build context | Delivery | OPS-12 |
| M-32 | Test doubles model a SANE API that does not exist | Tests | SCAN-09, XC-19 |
| M-33 | Data-loss paths have no tests; several assertions cannot fail | Tests | PIPE-13, CORE-11, XC-18, WEB-16, OPS-25 |
| M-34 | Suite is not hermetic | Tests | XC-10, OPS-13 |

### Minor and nit

Forty-five consolidated items, N-01 through N-45, are listed in section 5 with their source IDs.

### Usability (operator's perspective, section 11)

| ID | Title | Severity | Status |
|---|---|---|---|
| U-01 | Compose placeholder token silently overrides the token in `config.toml` | MAJOR | New |
| U-02 | UI never says how many pages were scanned or removed as blank | MAJOR | New |
| U-03 | No setup or status view; Paperless test route has no button; no `doctor` command | MAJOR | New |
| U-04 | Profiles shown as raw slugs; appear only after first scan and reload | MAJOR | Reframes M-04, M-30 |
| U-05 | Errors are raw exception text with no plain-language next step | MAJOR | Reframes M-11, M-17, N-14 |
| U-06 | Shared flip prompt with live Abort for every viewer; queued jobs unexplained | MINOR | New |
| U-07 | Paperless jargon without help; multi-select unusable on a phone | MINOR | New |
| U-08 | Recommended deployment has the consume-directory fallback switched off | MINOR | New |
| U-09 | Nobody is told there is no login and it listens on every interface | MINOR | Reframes N-22 |
| U-10 | Scanner-connection setup is the hardest step and the docs contradict themselves | MINOR | Reframes doc rows 29, 31 |
## 3. Critical findings

These eight findings each cause data loss, a silently wrong result, or a crash or hang on a path an ordinary user will take. Fix them before anything else. Each was found by at least one review agent and independently re-verified by the lead reviewer by reading the code and, where noted, by executing it.

### C-01 — The documented manual-duplex setup cannot work on real ADF hardware
- **Severity:** CRITICAL · **Dimension:** Design, Functionality, Documentation · **Confidence:** Confirmed (executed)
- **Found by:** PIPE-01, XC-02
- **Location:** `src/saneless/pipeline.py:94-96` and `:342-348`, `src/saneless/scanner/sane_backend.py:472-485`, `docs/how-to/set-up-adf-duplex.md:56-67`

**What.** The how-to tells users to enable manual duplex by writing `source = "Manual Duplex"` in a profile. The pipeline detects the mode by looking for the words "manual" and "duplex" in that string, but then builds `ScanSettings(source=profile.source, ...)` and hands the same string to the SANE backend. The backend validates the source against the device's real option list. No scanner has a source called "Manual Duplex", so one of two things happens: if the device has no "Auto" source, the scan fails with `ScanError: Device does not support source 'Manual Duplex'`; if it does have "Auto", the backend silently falls back to it and, because `auto_source_mode` defaults to `flatbed`, takes a single flatbed snapshot per pass. Either way the feature does not work as documented.

**Why it matters.** Manual duplex is the feature for people whose ADF cannot flip pages itself, which is most consumer ADFs. Those users follow the documentation and get either a failed job or a two-page PDF of the glass. Every manual-duplex test in the suite passes because the scanner is a `MagicMock(spec=ScannerBackend)` that accepts any source string, and the one test that does check source validation never uses a duplex profile. The lesson: one field must carry one meaning. Here `source` is simultaneously "which physical SANE input to open" and "which saneless scanning strategy to run", and the two layers that read it disagree about which meaning applies.

**How to fix.** Separate the strategy from the device value. Add a profile field such as `duplex: Literal["none", "hardware", "manual"] = "none"`, keep `source` as a pure SANE value (for example `"ADF"`), and branch on the new field in the pipeline. The inline copy of the detection rule in `worker.py:206-209` then becomes `profile.duplex == "manual"`. If you must keep the marker string for backward compatibility, translate it in exactly one place before building `ScanSettings`:

```python
manual = _is_manual_duplex(profile.source)
device_source = "ADF" if manual else profile.source
scan_settings = ScanSettings(source=device_source, ...)
```

Then add one integration test that runs the pipeline with a manual-duplex profile against the fake SANE device from `tests/test_scanner.py`, whose source list is `["Flatbed", "ADF", "ADF Duplex"]`. Update the how-to.

**Verification.** Lead re-read both code sites. The pipeline agent's scratch test reused the suite's own fake SANE module and showed `ScanSettings(source="Manual Duplex")` raises `ScanError` from `scan_pages`. The cross-cutting agent independently showed the "Auto" fallback path calls `snap()` once and `multi_scan()` never.

### C-02 — The CLI never waits for the flip, so a manual-duplex scan from the command line runs both passes back to back
- **Severity:** CRITICAL · **Dimension:** Functionality, Documentation · **Confidence:** Confirmed (executed)
- **Found by:** PIPE-02, OPS-01, XC-03
- **Location:** `src/saneless/cli.py:124-137`, `src/saneless/pipeline.py:225-237`, `docs/how-to/set-up-adf-duplex.md:52, 69`

**What.** The pipeline only emits `AWAITING_FLIP` and blocks on the flip event when the caller supplied one. The CLI's `scan` command builds its `PipelineRequest` without a flip event, so pass B starts the instant pass A finishes. The CLI even defines an "Awaiting flip..." status label that can never be printed. The documentation promises a prompt: "press Enter (CLI)".

**Why it matters.** Once C-01 is fixed this becomes a data-loss path. Pass A empties the feeder, pass B immediately calls `multi_scan()` on an empty tray, the backend raises `FeederEmptyError`, and the exception unwinds through the pipeline's `TemporaryDirectory`, deleting every front page that was just scanned. On a flatbed or "Auto" device today, pass B rescans the same face and the pipeline interleaves it, uploading a PDF in which every "back" duplicates its front, then prints `Done`. The lesson: an optional parameter that silently changes semantics ("wait" versus "do not wait") is a trap. When two entry points share a pipeline, both must supply the collaborators the pipeline relies on, or the pipeline must refuse to run without them.

**How to fix.** Give the CLI a real prompt. Because `run_pipeline` is synchronous and the status callback runs on the same thread, the simplest correct approach is to prompt inside the callback and set the event before returning:

```python
flip_event = threading.Event()
abort_event = threading.Event()

def status_callback(event: PipelineEvent) -> None:
    if event is PipelineEvent.AWAITING_FLIP:
        click.echo("Flip the stack and reload the feeder.")
        if click.confirm("Continue with the back sides?", default=True):
            flip_event.set()
        else:
            abort_event.set()
            flip_event.set()
        return
    click.echo(_event_labels.get(event, str(event)))

request = PipelineRequest(..., flip_event=flip_event, abort_event=abort_event)
```

Also make the pipeline refuse a manual-duplex request that has no flip event, raising `ConfigError`, so the same omission can never recur silently. Add a `CliRunner` test with `input="y\n"` asserting the prompt appears between "Scanning..." and "Scanning reverse sides...".

**Verification.** Lead read `pipeline.py:226` (`if request.flip_event is not None:`) and the CLI request construction. The ops agent's scratch `CliRunner` test showed `scan_pages` called twice back to back with no prompt and exit code 0.

### C-03 — The pipeline's outcome is thrown away, so rejected uploads, timeouts, consume-directory fallbacks, and duplex mismatches all display as "Complete"
- **Severity:** CRITICAL · **Dimension:** Design, Functionality · **Confidence:** Confirmed (executed)
- **Found by:** PIPE-04, CORE-02, XC-01, OPS-11
- **Location:** `src/saneless/pipeline.py:431-443`, `src/saneless/paperless.py:146-158` and `:189-198`, `src/saneless/worker.py:246-252`, `src/saneless/cli.py:132-137`, `docs/explanation/consume-directory-fallback.md:66`

**What.** `run_pipeline` reports four materially different outcomes only through its return dictionary: a paperless task in `SUCCESS`, `FAILURE`, or `TIMEOUT` state, a `FALLBACK` status when the PDF was copied to the consume directory, and a `DONE` with a `warning` when a duplex mismatch produced two partial PDFs. It then emits `PipelineEvent.DONE` unconditionally. Neither caller reads the dictionary. The worker writes `JobState.DONE`; the CLI prints `Done: <title>`. `upload_document` signals the fallback with the magic string `"fallback"` in place of a task id, and `poll_task` returns a status dictionary instead of raising.

**Why it matters.** paperless-ngx marks a task `FAILURE` when it refuses a document, and the most common reason is a duplicate ("Not consuming output.pdf: it is a duplicate of ..."). OCR crashes and pre-consume script failures do the same. In every one of those cases the user sees a green "Complete", the job history says the document was archived, and the document is not in paperless. The docs promise that a consume-directory fallback shows a distinct `FALLBACK` status "so users can identify which documents may need metadata corrections"; `JobState` has no such member. The lesson: a status the caller must remember to inspect is not a status. Failure must be impossible to ignore, which means raising an exception, and partial success must be visible, which means a distinct event or state.

**How to fix.**
1. In `poll_task`, raise `PaperlessError` carrying paperless's `result` text on `FAILURE`, and raise on timeout. Keep the return value only for success.
2. Replace the `"fallback"` sentinel with a small typed result, for example `UploadResult(task_id: str | None, fallback_path: Path | None)`, so the outcome is visible in the signature.
3. Add `JobState.FALLBACK` and `PipelineEvent.DONE_WITH_WARNING` (or similar), map them in the worker and CLI, and render them in the status partial.
4. Delete the untyped `-> dict` return from `run_pipeline` or replace it with a `PipelineResult` dataclass.
5. Add worker-level tests for each outcome that assert the persisted job state, not the dictionary.

**Verification.** Lead read all four sites; both callers discard the return value. The core agent drove the real `ScanWorker` and real `run_pipeline` with a stub client returning `FAILURE`, `TIMEOUT`, and `"fallback"`; all three ended with `state='DONE', error=None`. The cross-cutting agent reproduced the same result independently.

### C-04 — An upload failure after a successful scan deletes the scanned document
- **Severity:** CRITICAL · **Dimension:** Functionality · **Confidence:** Confirmed (executed)
- **Found by:** PIPE-03
- **Location:** `src/saneless/pipeline.py:354` and `:415-429`, `src/saneless/pipeline.py:133-149`, `src/saneless/paperless.py:129-135` and `:157-158`

**What.** The PDF is assembled inside `tempfile.TemporaryDirectory` and uploaded from there. `upload_document` raises `PaperlessError` on any 4xx response (an expired token, a bad tag id, a 413 for an oversized file) and after exhausting retries when no consume directory is configured. The exception exits the `with` block, which deletes the directory and the PDF with it. Nothing is preserved anywhere.

**Why it matters.** The expensive, irreplaceable part of the job, feeding fifty sheets through an ADF, succeeded. The cheap, retryable part, one HTTP POST, failed. The code throws away the expensive result. The user's only recourse is to physically re-scan. The duplex-mismatch recovery has the same flaw: if the first of its two uploads raises, both partial PDFs are gone. The lesson: once you hold data the user cannot regenerate for free, every later failure path must preserve it, and the error message should say where it is.

**How to fix.** Wrap the upload so that on failure the PDF is moved out of the temporary directory into a durable location and the error names it:

```python
failed_dir = Path(settings.output.tmp_dir) / "failed"
try:
    task_uuid = paperless.upload_document(pdf_path, ...)
except PaperlessError as exc:
    failed_dir.mkdir(parents=True, exist_ok=True)
    kept = failed_dir / f"{created}-{_slug(request.title)}.pdf"
    shutil.move(pdf_path, kept)
    msg = f"{exc} -- scanned PDF preserved at {kept}"
    raise PaperlessError(msg) from exc
```

Better still, make the consume directory the fallback for all upload failures rather than only network ones; today a 401 bypasses it entirely. Add a test: upload raises, PDF exists under `failed/`, message names it.

**Verification.** Lead read the `with` block and the raise sites. The pipeline agent's scratch test scanned five pages, made `upload_document` raise, and found `tmp_dir` empty afterwards.

### C-05 — Every PDF is named `output.pdf`, so consume-directory fallbacks overwrite each other
- **Severity:** CRITICAL · **Dimension:** Functionality · **Confidence:** Confirmed (executed)
- **Found by:** PIPE-05, CORE-03
- **Location:** `src/saneless/pdf.py:55`, `src/saneless/paperless.py:152-153`, `src/saneless/pipeline.py:125-126`

**What.** `assemble_pdf` always writes `output.pdf`. The consume-directory fallback copies it as `consume_dir/output.pdf` with `shutil.copy2`, which silently replaces an existing file. The duplex-mismatch recovery uploads `fronts/output.pdf` and `backs/output.pdf`; under fallback the second copy overwrites the first.

**Why it matters.** The fallback runs only while paperless is down, and while paperless is down its consumer is down too, so files accumulate rather than being picked up. During an outage only the last scan survives; every earlier one is silently gone, and the job history says all of them succeeded. paperless also records the upload's original filename, so every document shows `output.pdf`. The lesson: anything written into a shared directory needs a name you can prove is unique, and a copy into a watched directory should be atomic (write to a temporary name, then rename) so a consumer never sees a half-written file.

**How to fix.** Give `assemble_pdf` a caller-supplied filename derived from the date, a slug of the title, and a short unique suffix; pass `-fronts` and `-backs` variants from the mismatch path. Independently, make the fallback refuse to clobber:

```python
stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%S%f")
safe_title = re.sub(r"[^\w.-]+", "_", title).strip("_")[:80] or "scan"
dest = dest_dir / f"{safe_title}-{stamp}.pdf"
tmp = dest.with_suffix(".pdf.part")   # not a supported extension, so paperless ignores it
shutil.copyfile(pdf_path, tmp)
tmp.replace(dest)                     # atomic on the same filesystem
```

Add a test that two fallback scans leave two files.

**Verification.** Lead read `pdf.py:55` and the copy site. Two agents independently demonstrated a single surviving `output.pdf` containing the second document's bytes after two fallbacks.

### C-06 — Feeders not literally named "ADF" are routed to the single-page flatbed path
- **Severity:** CRITICAL · **Dimension:** Functionality, Consistency · **Confidence:** Confirmed (executed against the real SANE test backend)
- **Found by:** SCAN-01, XC-06
- **Location:** `src/saneless/scanner/sane_backend.py:158-160` and `:496-519`, compare `src/saneless/auto_profiles.py:47-60`

**What.** The backend decides between the multi-page `multi_scan()` loop and a single `start()`/`snap()` by asking whether the source string contains the substring "adf". Many SANE backends name the feeder "Automatic Document Feeder": Epson's `epson2` and `epsonds`, Canon's `pixma`, Brother's "Automatic Document Feeder(left aligned)", and SANE's own reference `test` backend. For all of them the code takes the flatbed branch and returns exactly one page. Meanwhile `auto_profiles.source_to_slug` in the same codebase already recognises "document feeder" and "feeder" as ADF names, so `saneless auto-profiles` will generate a profile whose source the backend then mis-routes.

**Why it matters.** A user loads a ten-page stack, saneless uploads a one-page PDF, and nothing is logged as an error. The scanner agent ran the real `SaneBackend` with real python-sane against the `test` backend: `multi_scan()` yields ten pages, `scan_pages(source="Automatic Document Feeder")` yielded one. The lesson: a classification rule that exists in more than one place is a bug waiting for the next scanner model. Two modules disagree today about what "an ADF source" is.

**How to fix.** Create a single classifier, for example `classify_source(name) -> Literal["flatbed", "adf", "adf_duplex", "auto", "unknown"]` in `scanner/base.py`, and use it from the backend, `auto_profiles`, and the pipeline. Consider the safer default: if the device exposes a `source` option, treat anything that is not the flatbed entry as multi-page, since `multi_scan()` on a flatbed simply yields one page. Add a parametrised test with the real-world names above.

**Verification.** Lead read `_is_adf_source` and its one call site. The scanner agent's experiment against `test:0` produced one page from a ten-page feeder.

### C-07 — One SQLite connection is shared by the web thread and the worker thread with no lock; a store error kills the worker and can hang the server
- **Severity:** CRITICAL · **Dimension:** Functionality (concurrency) · **Confidence:** Confirmed (executed)
- **Found by:** CORE-01
- **Location:** `src/saneless/job.py:87-111`, `src/saneless/worker.py:151-157` and `:201-204` and `:263-270`, `src/saneless/web/routes.py:170`

**What.** `JobStore` opens one `sqlite3` connection with `check_same_thread=False`. That flag only disables Python's safety check; nothing serialises access. The event-loop thread (page loads, the one-second HTMX status poll, form submits) and the worker thread (`update_state`, `update_thumbnail`, `prune`) call the same methods concurrently. Python's `sqlite3` keeps one implicit transaction per connection, so two threads interleaving `execute()` and `commit()` corrupt each other's transaction state.

**Why it matters.** The lead reran the core agent's two-thread stress test: ten of ten runs raised, with `OperationalError: cannot commit - no transaction is active`, `cannot start a transaction within a transaction`, and `SystemError: error return without exception set` from the C layer, for both in-memory and file-backed databases. The production window per job is small but it is open on every scan while a browser tab is polling. The consequences are asymmetric. In the web thread a failure is a 500 on one poll. In the worker thread, the `update_state` call that marks a job as scanning runs before the `try` block and `prune` runs in the `finally`, and the worker loop `_run` has no exception handling at all, so one failure ends the worker thread for the life of the process. After that `/health` reports 503 but the UI still accepts scans; `submit()` is a blocking `queue.put` on a ten-slot queue called from inside an `async def` route, so the eleventh submission blocks the event loop and the whole web server hangs. The lesson: `check_same_thread=False` is a promise you make to serialise access yourself. It is not a thread-safety feature.

**How to fix.** Add a `threading.RLock` to `JobStore`, take it in every public method, and use the connection as a context manager so commits and rollbacks are automatic:

```python
def __init__(self, db_path: str = ":memory:") -> None:
    self._lock = threading.RLock()
    self._conn = sqlite3.connect(db_path, check_same_thread=False)
    ...

def update_state(self, job_id, state, error=None, error_category=None) -> None:
    with self._lock, self._conn:
        self._conn.execute("UPDATE jobs SET state=?, error=?, error_category=? WHERE id=?", (...))
```

The core agent reran the stress test with a lock around each method: zero of ten runs raised. Separately, make the worker resilient: wrap the whole body of `_process_job`, including the pre-`try` `update_state` and the `finally` prune, so that a store failure is logged and the loop continues. Make `submit()` use `put_nowait` and return HTTP 429 when the queue is full. Add a two-thread test to `tests/test_job.py`; it is fast and deterministic enough to fail today.

**Verification.** Lead executed the stress script (10/10 failures both modes) and read the worker loop, the pre-try store call, and the blocking put in the async route.

### C-08 — Auto-profiles for a scanner without a flatbed writes a config file the application then refuses to load
- **Severity:** CRITICAL · **Dimension:** Functionality · **Confidence:** Confirmed (executed)
- **Found by:** PIPE-06
- **Location:** `src/saneless/auto_profiles.py:170-178` and `:230-255`, `src/saneless/config.py:146-156`, `src/saneless/worker.py:165-177`, `src/saneless/cli.py:313-325`

**What.** `generate_profiles` emits a `default` profile only when some source contains "flatbed". Sheet-fed scanners (Fujitsu ScanSnap, Brother ADS, Canon DR, a very common class for a paperless bridge) report only feeder sources. When no config file exists yet, which is the env-var or Docker case, `write_profiles_to_config` creates a new file containing only the feeder profiles. On the next start `load_settings` finds that file and `validate_default_profile` raises; the CLI prints "Configuration error: ... A 'default' profile must be defined" and exits 2. The worker triggers this automatically on the first scan.

**Why it matters.** The feature designed to make first-run easy bricks the second run, without the user asking, and leaves them to hand-edit a file they did not write. The lesson: a writer must produce output its own reader accepts. Round-trip it in a test.

**How to fix.** Always emit a `default`: prefer flatbed, else the first feeder source, else the first source. In `write_profiles_to_config`, if the document has no `[profiles.default]` after writing, add one or refuse to write with an explanation. Add a round-trip test: `write_profiles_to_config` then `load_settings`.

**Verification.** Lead ran `generate_profiles` with sources `["ADF", "ADF Duplex"]` and got `['adf-simplex', 'adf-duplex']` with no default. The pipeline agent's scratch test showed the written file fails `load_settings` with "default" in the message.

### C-09 — The worker thread can die silently, and once it has, the eleventh scan hangs the whole server and blocks shutdown
- **Severity:** CRITICAL · **Dimension:** Functionality (concurrency) · **Confidence:** Confirmed (executed)
- **Found by:** WEB-03, WEB-07, CORE-01
- **Location:** `src/saneless/worker.py:61` and `:74-89` and `:151-157` and `:201-204` and `:254-270`, `src/saneless/web/routes.py:170`

**What.** Three facts combine. First, the worker loop `_run` has no exception guard and `_process_job` guards only the pipeline call: the store call before the `try`, the store call inside the `except`, and `prune()` in the `finally` can all raise (`database is locked`, `disk I/O error`, `Cannot operate on a closed database`, a full disk). Any of those ends the thread; Python prints the traceback through `threading.excepthook`, not the application logger. Second, the job queue is bounded to ten entries but `submit()` calls a blocking `put`, and `stop()` enqueues its sentinel the same way. Third, `submit()` is called from an `async def` route, so the blocking put runs on the event loop.

**Why it matters.** After the worker dies, `/health` correctly returns 503 but `/api/scan` keeps accepting jobs. Each sits in `PENDING` showing "Starting scan..." forever. The eleventh submission blocks the event loop indefinitely, after which nothing answers, not even `/health`. Shutdown then calls `stop()`, whose `put(None)` also blocks, so `Ctrl-C` and `docker stop` cannot complete cleanly. The web agent reproduced the full chain: kill the worker, post ten scans (all 200), the eleventh never returns and a concurrent `/health` hangs too. The lesson: a long-lived worker must treat "one job failed unexpectedly" and "the loop is broken" as different events, and a request path must never wait unboundedly. Back-pressure has to become an HTTP error, not a hang.

**How to fix.** Guard the loop and make the queue non-blocking:

```python
def _run(self) -> None:
    while not self._stop.is_set():
        try:
            item = self._queue.get(timeout=0.5)
        except queue.Empty:
            continue
        try:
            self._process_job(item)
        except Exception:
            logger.exception("Worker crashed while processing job %s", item.id)
            with contextlib.suppress(Exception):
                self._job_store.update_state(item.id, JobState.ERROR, error="internal worker error", error_category=ErrorCategory.UNKNOWN)

def submit(self, job: Job) -> None:
    if not self.is_alive:
        raise WorkerUnavailableError("scan worker is not running")
    try:
        self._queue.put_nowait(job)
    except queue.Full:
        raise WorkerBusyError("scan queue is full") from None
```

Map those exceptions to 503 and 429 in `start_scan`, and create the job row only after the enqueue succeeds. Replace the sentinel with a stop flag so stopping never depends on queue capacity, check the result of `join()`, and move `prune()` out of the per-job `finally` because housekeeping must never fail the job path. Add tests: `prune` raises and the worker is still alive; ten jobs queued and the eleventh gets 429.

**Verification.** Lead read all cited lines. The web agent's scratch tests demonstrated the worker exiting after a raising `prune`, the eleventh POST never returning, and a `TestClient` lifespan exit that hung on shutdown.

### C-10 — The Scan button never re-enables after a job finishes
- **Severity:** CRITICAL · **Dimension:** Functionality · **Confidence:** Confirmed (executed in headless Chromium by two reviewers)
- **Found by:** WEB-01
- **Location:** `src/saneless/web/static/app.js:24-30`, `src/saneless/web/templates/partials/status.html:2-7`, `src/saneless/web/templates/index.html:57-60`

**What.** The `htmx:afterSwap` handler reads `evt.detail.target` and looks inside it for a `.status-done` or `.status-error` element. The status area is swapped with `hx-swap="outerHTML"`. For that swap style htmx fires the event on the new element but populates `detail.target` with the original element, which the swap has just removed from the document. The handler therefore inspects the previous poll's content, finds no terminal marker, and never calls `resetScanButton()`. Polling then stops because the DONE partial has no trigger, so nothing fires again.

**Why it matters.** This is the normal path of every scan through the web UI. After the job completes the button stays disabled with the text "Scanning…" until the user reloads the page. The lead reran the web agent's Playwright test against a real uvicorn server: the instrumentation shows, for the DONE swap, `detailTargetConnected: False`, `detailTargetHasDone: False`, `eventTargetHasDone: True`, and the button still disabled. The prior UI audit in `.planning/UI-REVIEW.md` had declared this lifecycle correct from reading alone. Two lessons. UI state logic must be verified in a browser, not by inspection. And the server already knows the button state (the index template renders it correctly on page load), so a second, client-side implementation of the same rule created two sources of truth, and the second one was wrong.

**How to fix.** Preferred: delete the JavaScript and let the server own the button. In `status.html` add an out-of-band swap so every status response also re-renders the button:

```html
<button type="submit" id="scan-btn" hx-swap-oob="true"
        {% if job and job.state.value in active_states %}disabled aria-busy="true"{% endif %}>
  {% if job and job.state.value == "AWAITING_FLIP" %}Waiting for flip&#8230;{% elif job and job.state.value in active_states %}Scanning&#8230;{% else %}Scan{% endif %}
</button>
```

Move the button markup into one shared include so it is rendered by exactly one template, and replace the `beforeRequest` handler with `hx-disabled-elt="#scan-btn"` on the form. The minimal one-line fix, if you keep the JavaScript, is to read `evt.target` or `document.getElementById("status-area")` instead of `evt.detail.target`. Either way, add a browser test that clicks Scan, waits for `.status-done`, and asserts the button is enabled.

**Verification.** Lead executed the scratch Playwright test (fails, demonstrating the bug) and read `app.js`. The web agent additionally read htmx 2.0.8's `swapOuterHTML` and `swap()` to confirm the event semantics.
## 4. Major findings

Each of these is a bug users will realistically hit, a resource or concurrency hazard, a missing test on a critical path, or a design flaw that will make the next change expensive. They are grouped by area. Within a group the order is roughly by impact.

### Web layer and worker

#### M-01 — Every route is `async def` but everything it calls blocks, so a slow Paperless freezes the whole UI including `/health`
- **Dimension:** Design, Functionality · **Confidence:** Confirmed (executed) · **Found by:** WEB-02, XC-05
- **Location:** `src/saneless/web/routes.py` (all handlers), `src/saneless/paperless.py:55`, `src/saneless/worker.py:105-118`

**What.** Every handler is declared `async def`, so FastAPI runs it directly on the single uvicorn event loop. Inside, every call is synchronous: `paperless.get_tags()` and `get_correspondents()` use a blocking `httpx.Client` with a 30-second timeout, `JobStore` is synchronous sqlite, and `continue_flip` calls `worker.wait_transition(timeout=2.0)`, a `threading.Event.wait`. While any of these blocks, no other request is served. Ruff's ASYNC rules cannot see this because the blocking call is behind a method on `self`.

**Why it matters.** Paperless being down is a supported scenario. When its host is unroutable rather than refusing connections (firewall, VPN down, machine asleep), `GET /` blocks for up to sixty seconds, the two `hx-trigger="load"` selects then issue two more blocking requests, and during all of it the one-second status poll, `/api/scan`, and `/health` cannot be answered, so an orchestrator will conclude the service is dead. Failed fetches are not cached, so every page load repeats the wait. Independently, a double-click on Continue blocks the loop for the full two seconds; the existing `test_flip_continue` takes exactly 2.00 s for this reason. Measured by both agents: a `/health` request issued 0.2 s after a slow `/api/tags` took 1.3 to 1.7 s. The principle: `async def` is a promise to the framework that you will not block. If a handler only calls synchronous code, declare it `def` and Starlette runs it in a threadpool.

**How to fix.** Change every handler from `async def` to `def`; no other code changes are needed. Add a comment on the router explaining the choice so nobody "fixes" it back. Then give `PaperlessClient` a short connect timeout separate from the upload timeout, for example `httpx.Timeout(connect=5.0, read=30.0)`, and cache the "unreachable" outcome briefly so a down Paperless costs one timeout per TTL rather than one per request.

#### M-02 — After Continue, the job stays in AWAITING_FLIP for all of pass B, so the flip prompt stays on screen while the scanner runs and Abort does nothing
- **Dimension:** Functionality · **Confidence:** Confirmed (executed) · **Found by:** WEB-05, PIPE-17
- **Location:** `src/saneless/worker.py:221-233`, `src/saneless/pipeline.py:230-236`, `src/saneless/web/routes.py:288-324`, `docs/reference/web-api.md:124-136`

**What.** The worker maps most pipeline events to job states, but on `SCANNING_REVERSE` it only sets its transition flag and leaves the persisted state at `AWAITING_FLIP`. The pipeline then scans the backs, possibly for minutes, before `ASSEMBLING` moves the state on. So `POST /api/flip/continue` waits, re-reads the job, and returns the same flip prompt, and the one-second poll keeps re-rendering Continue and Abort while pages are visibly feeding. Abort at that point sets an event the pipeline checked only before pass B, so it silently does nothing. `abort_flip` also reads `current_job_id`, which the worker may already have cleared, so it can answer "Ready to scan." with no error.

**Why it matters.** Confusing feedback during the most error-prone step of the workflow, plus a control that does nothing. The transition-event protocol is also more complex than it needs to be: the same event is set on five different transitions and cleared in two places, so `wait_transition` often returns immediately on a stale signal, which is why the unit test for it passes.

**How to fix.** Map `SCANNING_REVERSE` to `JobState.SCANNING`, or add a dedicated `SCANNING_BACKS` state with its own label. Then `continue_flip` can return the status partial immediately, which removes both `wait_transition` and the two-second event-loop block. Make `abort_flip` use the same "current job or most recent" lookup as `current_job_status`. If cancel during pass B is desired, check the abort event between pages, which needs the lazy consumption from M-08. Add a worker test that asserts the state is no longer `AWAITING_FLIP` once `SCANNING_REVERSE` has been emitted, and fix the API doc.

#### M-03 — No crash recovery: a job interrupted by a restart stays "Scanning..." forever and locks the UI; shutdown closes the database under a running worker
- **Dimension:** Functionality · **Confidence:** Confirmed (executed) · **Found by:** WEB-06, CORE-09
- **Location:** `src/saneless/web/app.py:78-92`, `src/saneless/worker.py:74-78`, `src/saneless/web/routes.py:81-86` and `:187-192`, `src/saneless/job.py:1-7`

**What.** Nothing reconciles the job table at startup. If the process dies or is stopped while a job is in an active state, the row keeps that state until `prune` removes it after seven days. On the next start, `index` and `current_job_status` fall back to "the most recent job", which is that row, so the page renders "Scanning..." with a one-second poll and a disabled Scan button, and every poll returns the same thing. The in-memory queue of pending jobs is lost too. On shutdown, `stop()` joins for at most five seconds and ignores the result; `lifespan` then closes the Paperless client and the store while the daemon worker thread may still be mid-scan, so its next store call raises `Cannot operate on a closed database`, which is precisely how a job gets stranded.

**Why it matters.** Restart-mid-job is routine for an appliance: a container update, an unplugged scanner, or C-09. The result is a UI that looks permanently busy with no recovery except deleting `saneless.db`. The `job.py` module docstring promises "SQLite-backed persistence for crash recovery" and no recovery code exists. The principle: any persisted "in progress" state needs an owner that can prove it is still in progress; on startup, anything the new worker did not create is by definition orphaned.

**How to fix.** Add `JobStore.fail_active_jobs(reason)` that sets every active row to `ERROR` with `error="Interrupted by restart"`, and call it in `lifespan` before `worker.start()`. Stop deriving the "current" job from `list_recent(1)` for active rendering; only `worker.current_job_id` should be able to make the UI busy. On shutdown, check `is_alive()` after the join, log a warning, and close the store and client only after the thread has exited. Also note that the database lives under `tmp_dir`, which many distributions clear on reboot; if history is meant to be durable, put it under the XDG state directory next to the log file (see N-39).

#### M-04 — The worker writes auto-generated profiles to a config path it resolves on its own, never the `--config` file; `is_bare_default` ignores most fields; the search list is duplicated
- **Dimension:** Design, Functionality · **Confidence:** Confirmed (executed) · **Found by:** WEB-08, XC-04, PIPE-11
- **Location:** `src/saneless/worker.py:159-191`, `src/saneless/auto_profiles.py:104-129` and `:183-206`, `src/saneless/config.py:229-259`, `src/saneless/web/app.py:51-76`

**What.** Three related problems. `load_settings` and `resolve_config_path` each hard-code the same three search paths; the worker calls `resolve_config_path()` with no argument, and `create_app` has no way to receive the `--config` path, so the worker cannot know which file was loaded and falls back to `./saneless.toml` in the daemon's current working directory. `is_bare_default` compares only `source`, `resolution`, `mode`, and `auto_generated`, so a default profile customised with `default_tags`, `paper_size`, `title`, or thresholds still counts as bare; the worker then replaces `settings.profiles["default"]` in memory with the generated one, and for the rest of the process the user's tags and paper size are gone. And that mutation of a dictionary the request handlers iterate happens from the worker thread with no lock.

**Why it matters.** `saneless --config /etc/saneless/prod.toml serve` under systemd with `WorkingDirectory=/` will try to create `/saneless.toml` on the first scan, fail with `PermissionError`, and log "Auto-profiles: scanner unreachable", a message that asserts a cause the code cannot know. The real config never gains the profiles, so the dance repeats on every restart. The existing worker tests monkeypatch `resolve_config_path` precisely to avoid writing into the repository root, which shows the authors met this behaviour and worked around it in the tests rather than the code. The principle: derived facts such as "which file we loaded" must travel with the data, not be re-derived by a different module with a different algorithm.

**How to fix.** Record the resolved path once, for example `Settings.config_path: Path | None` set in `load_settings`, and use it in both the worker and the CLI; delete the duplicated search list. Make `is_bare_default` compare the full model: `profile.model_dump() == ProfileConfig().model_dump()`. In the worker, merge only new names into `settings.profiles`, never replace an existing entry, and do it under a lock. Better still, move auto-generation out of the job loop into an explicit startup step in `serve` or `lifespan`, where the config path is in scope and the dropdown is correct on first load. Log the real exception class in the warning.

#### M-05 — Two parallel state enums, three hand-maintained label maps, and three copies of the "active states" list must all change together
- **Dimension:** Design, Complexity · **Confidence:** Confirmed · **Found by:** XC-08, WEB-10
- **Location:** `src/saneless/job.py:24-33`, `src/saneless/pipeline.py:37-45`, `src/saneless/worker.py:221-233`, `src/saneless/web/app.py:35-48`, `src/saneless/cli.py:110-116`, `partials/status.html:1`, `index.html:58-59`, `static/app.js`

**What.** `PipelineEvent` and `JobState` are near-identical string enums. The worker maps one to the other by hand; `humanize_state` keys a dictionary by raw strings and silently returns the raw value for anything unknown; the CLI has its own label table; the status partial lists the active states as string literals, the index template repeats the list twice, and the JavaScript implies it again. `JobState` itself has no notion of "active" or "terminal".

**Why it matters.** Adding one pipeline stage today requires edits in seven places across five files with no compiler or test telling you which one you missed. The failure mode is a stage that renders as its raw enum name, or a button that stays enabled. `SCANNING_REVERSE` already shows the drift (M-02), and C-10 is a direct result of the duplication. The existing test `test_humanize_state_filter_unit` asserts the fallback-to-raw behaviour, which pins the drift rather than preventing it.

**How to fix.** Use one enum, or give `PipelineEvent` a `job_state` property. Attach the label to the enum and add a test that every member has one. Expose `ACTIVE_STATES: frozenset[JobState]` and a `Job.is_active` property, pass it into the template context, and render the button from a single include with `hx-swap-oob`. Type the label map as `dict[JobState, str]` so ty and pyrefly flag a missing member.

### Pipeline, PDF, and auto-profiles

#### M-06 — Every PDF gets 96 DPI page geometry, so an A4 scan at 300 DPI becomes a 26 by 36 inch page
- **Dimension:** Functionality · **Confidence:** Confirmed (executed by lead) · **Found by:** PIPE-07
- **Location:** `src/saneless/pdf.py:49-56`

**What.** Pages are saved as PNG without `dpi=`, so the file has no physical-size metadata, and `img2pdf.convert` is called without a layout function, so it falls back to its default 96 DPI when computing the page box. The lead reproduced it: a 2480 by 3508 pixel image, A4 at 300 DPI, produced `MediaBox [0 0 1860 2631]`, about 25.8 by 36.5 inches. At 600 DPI the page is over fifty inches wide.

**Why it matters.** Pixel content is intact, but the physical page size is wrong in every PDF the project produces. Printing scales or tiles, viewers and Paperless display a poster-sized page, and tools that derive DPI from page size, such as ocrmypdf, see 96 DPI. The resolution the user configured never reaches the output. The principle: image DPI is metadata that must be carried explicitly through every hop, because libraries default to something plausible-looking when it is missing.

**How to fix.** Pass the resolution through: `assemble_pdf(images, output_dir, dpi=profile.resolution)` and either `img.save(path, format="PNG", dpi=(dpi, dpi))` or `img2pdf.convert(paths, layout_fun=img2pdf.get_fixed_dpi_layout_fun((dpi, dpi)))`. Read the actual DPI back from the device first (see M-16). Assert the MediaBox in a test: 595 by 842 points for A4 at 300 DPI.

#### M-07 — The flip wait has no timeout; a forgotten prompt parks the only worker thread forever
- **Dimension:** Functionality (concurrency) · **Confidence:** Confirmed (executed) · **Found by:** PIPE-08
- **Location:** `src/saneless/pipeline.py:226-228`, `src/saneless/worker.py:61` and `:88`

**What.** `request.flip_event.wait()` blocks indefinitely. The worker is a single thread; while it waits, every later job queues behind it.

**Why it matters.** A user starts a manual-duplex scan, walks away or closes the tab, and the bridge is stuck at "Waiting for flip" until the process restarts. After ten more submissions, C-09 turns it into a full server hang. The principle: never wait on an external actor without a bound. A timeout turns a hang into a reportable error.

**How to fix.** `if not request.flip_event.wait(timeout=settings.output.flip_timeout_seconds): raise ScanError("Timed out waiting for the stack to be flipped")` with a generous configurable default such as ten minutes, and treat the timeout like an abort, preserving the fronts per C-04.

#### M-08 — The whole batch is held in RAM as decoded pixels, about 1.3 GB for fifty colour pages at 300 DPI, and the disk pre-check does not scale with it
- **Dimension:** Design · **Confidence:** Confirmed (measured) · **Found by:** PIPE-09, SCAN-08
- **Location:** `src/saneless/pipeline.py:217` and `:237` and `:268`, `src/saneless/pdf.py:47-60`, `src/saneless/scanner/sane_backend.py:181-216`, `src/saneless/config.py:94`

**What.** `scan_pages` is a generator, which is the right contract, but every consumer immediately does `list(...)`, and `filter_empty_pages` takes and returns lists, so every decoded page stays in memory until the PDF is written. `assemble_pdf` then writes every page as PNG to disk, and `img2pdf.convert` returns the whole PDF as one `bytes` object. Measured for a realistic noisy A4 colour page at 300 DPI: 26 MB in RAM, 10.6 MB as PNG. A fifty-page batch is about 1.3 GB resident plus about 1 GB of peak disk while PNGs and PDF coexist, twice the 500 MB the pre-check demands. At 600 DPI multiply by four. During acquisition, `_validate_page_image` additionally calls `tobytes()` on each page only to measure its length, a full 26 MB copy, and converts to greyscale, which `is_empty_page` later does again.

**Why it matters.** Scanner bridges run on small always-on boxes. A batch that exceeds RAM is killed by the OOM killer mid-scan with all pages lost; one that exceeds the disk margin fails with an `OSError` that is not a `ScanError`, again losing the pages. The principle: a generator is only a streaming API if its consumer streams. Decide where the batch is materialised, which should be disk, and keep the in-memory working set to one page.

**How to fix.** Two cheap wins now: compute the size check from `page_image.size` and the band count instead of `tobytes()`, and do the greyscale conversion once. Structural fix: write each page to the job's temporary directory as it arrives, computing the thumbnail and the empty-page statistics on the fly, keep only paths, and pass `outputstream=` to `img2pdf.convert` so the PDF streams to disk. Run the disk check per page against a per-page estimate.

#### M-09 — `auto-profiles --force` replaces any same-named profile, including hand-written ones, and drops every key it does not know
- **Dimension:** Functionality, Documentation · **Confidence:** Confirmed (executed) · **Found by:** PIPE-10, OPS-26
- **Location:** `src/saneless/auto_profiles.py:241-253`, `docs/how-to/configure-scan-profiles.md:128-131`

**What.** With `force=True` the function assigns a brand-new table over the existing one. The user's `default_tags`, `default_correspondent`, `title`, `paper_size`, `enable_empty_page_detection`, thresholds, and any comments inside the table are gone. The `auto_generated` flag on disk is never consulted, so a hand-written `[profiles.default]` is treated exactly like a generated one, contrary to the how-to.

**Why it matters.** `--force` reads as "refresh the scanner-derived values"; a user with a customised default who runs it to pick up a new scanner loses their tagging setup silently. The generated slugs (`default`, `flatbed-scan`, `adf-simplex`, `adf-duplex`, `auto-scan`) are exactly the names users are most likely to have hand-written. The principle: an "overwrite" should be a merge of the keys you own, and a flag that says "I generated this" is only useful if the writer honours it.

**How to fix.** Under `force`, update in place on the existing tomlkit table (`existing["source"] = ...`), which preserves other keys and comments, and skip tables whose `auto_generated` is not true unless a stronger flag is given. Fix the doc sentence to match. Add a test asserting `default_tags` survives `--force`.

#### M-10 — The config file is rewritten non-atomically with locale-dependent encoding
- **Dimension:** Functionality · **Confidence:** Confirmed (reading; standard-library semantics) · **Found by:** PIPE-12
- **Location:** `src/saneless/auto_profiles.py:231` and `:255`

**What.** `Path.write_text` opens the file with mode `w`, truncating it to zero bytes before the new contents are written. A crash, power loss, or full disk between those two moments leaves an empty or partial TOML file. Neither the read nor the write passes `encoding="utf-8"`, so a comment with a non-ASCII character fails to parse on a non-UTF-8 locale.

**Why it matters.** This is the user's entire configuration, including the Paperless token, and the worker rewrites it in the background on a small device where unclean shutdowns are common. A truncated file means the application cannot start. The principle: durable config writes are always "write to a temporary file in the same directory, then `os.replace`".

**How to fix.**

```python
tmp = config_path.with_suffix(".toml.tmp")
tmp.write_text(tomlkit.dumps(doc), encoding="utf-8")
os.replace(tmp, config_path)
```

Read with `encoding="utf-8"` too, and preserve the original file's mode bits since it may contain a token.

### Scanner backend

#### M-11 — Any error on the first ADF page is reported as "No paper detected in feeder"
- **Dimension:** Functionality · **Confidence:** Confirmed (executed against the real SANE test backend) · **Found by:** SCAN-02, XC-07
- **Location:** `src/saneless/scanner/sane_backend.py:376-379` and `:407-414`

**What.** `except Exception as exc: if page_num == 0: raise FeederEmptyError(...)` converts every exception raised while acquiring the first page into a feeder-empty error. The real empty-feeder signal never reaches this branch anyway: python-sane's iterator already turns "Document feeder out of documents" into `StopIteration`, which the code handles correctly via `page_num == 0` at line 431. The guard around `dev.multi_scan()` is unreachable with the real API, because that method just returns an iterator object and cannot raise.

**Why it matters.** With the `test` backend configured to return real SANE statuses on the first read, saneless raised `FeederEmptyError('No paper detected in feeder')` for `Error during device I/O`, `Document feeder jammed`, `Scanner cover is open`, and `Device busy`. The worker then files the job under the FEEDER category and the UI tells the user to load paper when the real problem is a jam, an open lid, or another process holding the device. The principle: catch the specific condition you can recognise; never infer meaning from "it failed on iteration zero".

**How to fix.** Delete the `page_num == 0` special case and rely on `StopIteration` for feeder-empty. Translate other backend errors into a `ScanError` that carries the SANE message: `raise ScanError(f"Scanner error on page {page_num + 1}: {exc}") from exc`. If a dedicated category for jams is wanted, match on the message explicitly and test each case.

#### M-12 — On a per-page timeout the device is closed while a thread is still inside `sane_read()`, and that thread outlives the job
- **Dimension:** Functionality (concurrency) · **Confidence:** Confirmed for thread lifetime and close-while-reading; Likely for a crash on real backends · **Found by:** SCAN-03
- **Location:** `src/saneless/scanner/sane_backend.py:382-400` and `:425-429` and `:283-286`

**What.** On timeout the code raises `ScanError`, calls `executor.shutdown(wait=False)`, and `_open_device`'s `finally` immediately calls `dev.cancel()` then `dev.close()`. The executor thread is still executing `next(iterator)` inside `sane_read()` at that moment. The SANE standard says of cancellation that a frontend must not call any other operation until the cancelled operation has returned; `sane_close()` is such an operation. `ThreadPoolExecutor` workers are non-daemon threads that the interpreter joins at exit.

**Why it matters.** For backends where the read does not unblock promptly, a network read stuck on a TCP socket being the realistic case for a 120-second timeout to fire, `sane_close()` frees state the reader thread is still using, and a segfault takes the whole web server down. Even without a crash, every timeout leaks one thread and one device object for as long as the C call blocks, and `saneless serve` cannot exit until it returns. Measured: after the `ScanError`, the executor thread was still alive when `close()` returned, and a script with a blocked worker took four seconds to exit after its body finished. The principle: a timeout on a blocking C call does not stop the call. You need a cancellation mechanism and you must wait for the callee to acknowledge it before tearing down what it uses.

**How to fix.** After `sane_cancel`, wait for the future to finish before closing:

```python
except FuturesTimeoutError as timeout_exc:
    with contextlib.suppress(Exception):
        dev.cancel()
    done = concurrent.futures.wait([future], timeout=_CANCEL_GRACE_SECONDS)
    if not done.done:
        logger.critical("SANE read did not return after cancel; leaving handle open to avoid undefined behaviour")
    raise ScanError(...) from timeout_exc
```

Make `_open_device` skip `close()` in that state. Keep one long-lived executor per backend rather than one per call. Add a test asserting `close()` is not called while the fake read is still blocked.

#### M-13 — The flatbed path has no timeout and no page validation, so a hung device wedges the worker thread forever
- **Dimension:** Functionality, Consistency · **Confidence:** Likely · **Found by:** SCAN-07
- **Location:** `src/saneless/scanner/sane_backend.py:511-519`

**What.** ADF pages get a 120-second per-page timeout; the flatbed branch calls `dev.start()` and `dev.snap()` synchronously on the calling thread with no bound and skips `_validate_page_image`. The module docstring describes the safety measures as if they applied to all scanning.

**Why it matters.** In `saneless serve` the caller is the single worker thread. If a network scanner stops responding mid-read, the scenario documented in the project's own memory-overflow debug note, that thread never returns: the job stays in SCANNING forever and C-09 follows. The rationale the code gives for ADF timeouts applies identically to flatbed scans.

**How to fix.** Route both paths through one `_acquire_with_timeout(dev, fn, timeout)` helper built as described in M-12, apply the same integrity checks to flatbed pages, and add a test mirroring `test_page_timeout_raises_scan_error` for the flatbed path.

#### M-14 — Scanner-level "pure white" validation silently drops pages regardless of the profile's empty-page toggle and breaks manual-duplex page parity
- **Dimension:** Functionality, Design · **Confidence:** Confirmed (executed against the real SANE test backend) · **Found by:** SCAN-04
- **Location:** `src/saneless/scanner/sane_backend.py:186-216` and `:416-420`

**What.** `_validate_page_image` discards any page with mean above 254 and standard deviation below 1. python-sane expands 1-bit lineart to 0 and 255 bytes, so a clean blank page in Lineart mode is exactly mean 255.0 with deviation 0.0 and is dropped here with only a warning, before the pipeline ever sees it. `page_num` still increments, so `scan_pages` can yield zero pages without raising.

**Why it matters.** Three consequences. The empty-page explanation doc promises that `enable_empty_page_detection = false` keeps all pages, "useful when blank pages are intentional", and that is untrue for clean blanks. The pipeline deliberately compares raw counts before empty-page filtering for manual duplex, so a blank back dropped here makes fronts and backs mismatch and the user gets two separate partial PDFs instead of one interleaved document. And with ten blank sheets in the feeder, `scan_pages` returned an empty list without `FeederEmptyError`; with detection disabled the pipeline then calls `assemble_pdf([])`. The principle: a low-level layer should not make policy decisions that a higher layer exposes as a user setting. Validate integrity at the bottom; decide content policy at the top.

**How to fix.** Keep the zero-dimension and truncated-buffer checks; remove the white and black checks from the backend, since `filter_empty_pages` already covers blank pages under the user's thresholds and toggle. Raise, or return a distinct signal, when every fed page was rejected.

#### M-15 — The Pillow crop fallback can never trigger on a device that lacks geometry options, because the real python-sane silently accepts unknown attributes
- **Dimension:** Functionality, Tests · **Confidence:** Confirmed (lead read the installed `sane.py`) · **Found by:** SCAN-05
- **Location:** `src/saneless/scanner/sane_backend.py:94-132`, `tests/test_scanner.py:893-908`

**What.** `_set_geometry` assumes that assigning `dev.tl_x` on a device without that option raises. The real `SaneDev.__setattr__` does `if key not in self.opt: d[key] = value; return`: it stores a plain Python attribute and never touches the scanner. So the function returns `True`, `_maybe_crop` does nothing, and the user's `paper_size = "a4"` has no effect, with no log line. The test double `_NoGeometryDevice` raises `AttributeError`, which is why the suite is green. Additionally, the `except Exception` swallows the reason without logging it, so when the fallback does trigger nobody can tell why.

**Why it matters.** The profiles how-to promises "Otherwise, it crops the image after scanning". That sentence is currently unreachable for the case it describes. The principle: when you write a fake for a third-party API, copy the real semantics from its source, and when you swallow an exception, log it.

**How to fix.** Check presence before assignment using the option list already fetched: if `{"tl-x", "tl-y", "br-x", "br-y"}` is not a subset of the option names, log and return `False`. Log the exception in the `except`. Then fix `_NoGeometryDevice` to model the real behaviour so the existing fallback tests become meaningful.

#### M-16 — Crop maths uses the requested DPI although the device may silently substitute another
- **Dimension:** Functionality · **Confidence:** Confirmed (device substitution demonstrated) · **Found by:** SCAN-06, SCAN-13
- **Location:** `src/saneless/scanner/sane_backend.py:487-494` and `:153-154`, `src/saneless/paper_sizes.py:57-58`

**What.** SANE backends clamp or snap resolution to their constraint and report an "inexact" flag instead of failing. `dev.resolution = 5000` on the `test` backend raised nothing and the device reported 1200. Nothing reads the value back; `_maybe_crop` computes pixel dimensions from the value the user asked for. Options are also set in an order (mode, resolution, then source) that lets a source change reload the option descriptors and silently clamp the earlier values, because feeders often have a different resolution ceiling than the flatbed.

**Why it matters.** A profile says 300 DPI, the scanner offers only 200 and 400 and snaps to 400: A4 is now 3307 by 4677 pixels, but the fallback crops to 2480 by 3507, cutting off the bottom and right quarter of every page. The user also never learns which DPI was used, and the PDF (M-06) will carry the wrong value. The principle: after setting a device option, read it back; the device is the source of truth.

**How to fix.** Set `source` first, then `mode`, then `resolution`, then geometry. After assignment read `actual = int(dev.resolution)`, warn if it differs, and pass `actual` to the crop and to `assemble_pdf`. Consider validating the requested resolution and mode against the constraints already parsed in `get_capabilities`, so the user gets "device supports 200, 400" instead of a silent change.

### Error handling across module boundaries

#### M-17 — Third-party exceptions leak past every module boundary; broad catches hide causes; the CLI prints tracebacks for expected failures
- **Dimension:** Design, Functionality · **Confidence:** Confirmed (executed for the httpx and SANE cases) · **Found by:** XC-07, CORE-06, CORE-14, SCAN-11, OPS-09, PIPE-15
- **Location:** `src/saneless/exceptions.py`, `src/saneless/scanner/sane_backend.py:280` and `:488-491` and `:515-516`, `src/saneless/paperless.py:118-135` and `:160-198` and `:212-250`, `src/saneless/pdf.py:56-59`, `src/saneless/worker.py:130-149` and `:254-262`, `src/saneless/cli.py:64-69` and `:138-143` and `:167` and `:226`, `src/saneless/web/routes.py:56-61`

**What.** The project defines a clean exception hierarchy and then does not hold the line at its boundaries. The cross-cutting agent tabulated every `raise` and `except` in the package: `SanelessError` is never caught by name; `ConfigError` is raised by the pipeline for "no scanner found" but the CLI `scan` command catches only `ScanError` and `PaperlessError`, so a first-time user with no scanner attached gets a traceback and exit code 1 while the docs promise "Configuration error" and exit 2. Specific leaks, each verified:

- `paperless.py`: only `ConnectError`, `TimeoutException`, and `HTTPStatusError` are handled. `ReadError`, `RemoteProtocolError` (a reverse proxy closing the connection), and `WriteError` get one attempt and then escape raw. With `paperless.url` left at its default empty string, every call raises `httpx.UnsupportedProtocol` with a full traceback. `poll_task` has no error handling at all, so one `ReadTimeout` on the task endpoint after a successful upload fails the job and the user re-scans a document Paperless already accepted, which then fails as a duplicate, which C-03 records as done. `test_connection` catches only `ConnectError`, so a `ConnectTimeout`, the most common "unreachable" on a home LAN, raises instead of returning "unreachable". `get_tags` and `get_correspondents` are unguarded.
- `sane_backend.py`: `sane.open()` on a wrong or busy device, a rejected `mode` or `resolution`, and errors inside `start()` or `snap()` propagate as `_sane.error` with messages such as `Invalid argument`. Demonstrated: `dev.mode = "Lineart"` on the test backend raised `_sane.error('Invalid argument')`, which the CLI shows as a traceback with no hint which option was rejected.
- `pdf.py`: raises a bare `RuntimeError`, and img2pdf's own `ValueError` (empty list) and Pillow's `OSError` (disk full) pass through.
- `cli.py`: `python-sane` not importable (the README has a whole section on this) gives a `ModuleNotFoundError` traceback from four commands; `jobs` on a fresh install where `tmp_dir` does not yet exist gives a `sqlite3.OperationalError` traceback. Every 4xx from Paperless appends the entire response body to the error message: 5,044 characters of HTML in one test, which lands in `job.error`, the history table, and the terminal.
- Broad catches: nine `except Exception` sites. Five are justified boundary catches. Two in the backend convert every error into "feeder empty" (M-11). The worker's job-level catch logs `%s` of the exception with no `exc_info`, so the traceback of any unclassified failure is lost forever. The routes' metadata catch logs a warning without the exception at all, so an operator cannot tell a 401 from a DNS failure.

**Why it matters.** Callers were written against the docstring contract. Every leak becomes a traceback for CLI users and an `UNKNOWN` category with no diagnostic for web users. The `ErrorCategory` feature exists "for programmatic handling", yet the most common real failures, scanner unplugged, wrong `mode` string, Paperless returning 502 during polling, arrive as `UNKNOWN`. The principle: a module that wraps a third-party library must translate all of that library's failure modes into the project's own exceptions at the boundary, adding context (which device, which option, which value) at the point where it is known. The outermost catch must log with `exc_info` when it cannot classify.

**How to fix.**
1. `sane_backend.py`: wrap `sane.open`, each option assignment, and `start()`/`snap()` in `try/except Exception as exc: raise ScanError(f"...: {exc}") from exc`; map to `FeederEmptyError` only when the message matches the SANE feeder strings.
2. `paperless.py`: catch `httpx.TransportError` (the base of all connect, read, write, protocol, and pool errors) for the retry branch, and add a final `except httpx.HTTPError as exc: raise PaperlessError(...) from exc`. In `poll_task`, catch transport errors inside the loop and keep polling until the deadline. In `test_connection`, map `TransportError` to "unreachable". Validate `paperless.url` at load time with `AnyHttpUrl` or a validator. Truncate 4xx bodies to about 200 characters and prefer the JSON `detail` field. Rename `max_retries` to `max_attempts`, since three is the attempt count.
3. `pdf.py`: `except (ValueError, OSError, img2pdf.ImageOpenError) as exc: raise ScanError(f"PDF assembly failed: {exc}") from exc`.
4. `cli.py`: add `except ConfigError` with exit 2 to `scan`; add a helper that converts `ImportError` from the SANE import into "python-sane is not installed; see System Requirements" and use it in all four commands; `mkdir` the tmp dir before opening the store in `jobs`.
5. `worker.py`: use `logger.exception(...)` in the job-level catch, at least when the category is `UNKNOWN`.
6. `_build_settings`: convert any `ValidationError` and `TOMLDecodeError` to `ConfigError` with a friendly rendering, so `load_settings` raises one type as its docstring promises.
7. Add a test per boundary asserting the project exception type and a message that names the cause.

### Configuration

#### M-18 — Misspelled keys inside config sections are silently ignored
- **Dimension:** Functionality · **Confidence:** Confirmed (executed by lead) · **Found by:** CORE-04
- **Location:** `src/saneless/config.py:48-97` and `:162-189`

**What.** `Settings` inherits `extra="forbid"` from pydantic-settings, implicitly, so an unknown section is rejected with a helpful message. But the four nested models are plain `BaseModel`s whose default is `extra="ignore"`, so an unknown key inside a section is dropped without a word. The lead loaded a file containing `hostname = ...` under `[scanner]` and `resoluton = 600` under `[profiles.default]`: no error, `scanner.host` empty, resolution 300. The existing hint text is also misleading for the one case it catches: `[Paperless]` yields "Did you mean [profiles.Paperless]?".

**Why it matters.** A user who types `tokne`, `log_leve`, `web_prot`, or `resolutin` gets a later, unrelated failure (an empty token becomes "401 Invalid token") with no clue the config line was never read. Putting `web_port` under the wrong section is equally silent. The principle: configuration is user input; reject what you do not understand at the point of input.

**How to fix.** Add `model_config = ConfigDict(extra="forbid")` to the four nested models and write `extra="forbid"` explicitly on `Settings`. Extend `_build_settings` to render nested locations: for `loc == ("paperless", "tokne")` say `Unknown key 'tokne' in [paperless]; valid keys: url, token, consume_dir`, using `model_fields`, which also removes the hand-maintained `_VALID_SECTIONS`. Emit the `[profiles.X]` hint only when `X` is not a case-insensitive match of a real section. Add tests for a nested typo and a key in the wrong section.

#### M-19 — A nonexistent `--config` path is silently ignored
- **Dimension:** Functionality · **Confidence:** Confirmed (executed) · **Found by:** CORE-05, OPS-08
- **Location:** `src/saneless/config.py:245-246` and `:137-142`, `src/saneless/cli.py:46-69`, `docs/how-to/cli-scripting.md:62`

**What.** `load_settings("/definitely/not/here.toml")` returns default settings with no error, and so does a directory path, because pydantic-settings' TOML source treats a missing file as "no data". The auto-discovery loop checks `exists()`, so only the explicit branch is affected. The scripting guide promises exit code 2 for a missing config file.

**Why it matters.** `saneless --config /etc/saneless/confg.toml serve` starts with an empty Paperless URL and a bare default profile, and the first symptom is a scan failing with a traceback (M-17) or profiles being generated into the wrong file (M-04). An explicit path is the strongest statement of intent a user can make; ignoring it silently is the worst possible response.

**How to fix.** In `load_settings`: `if config_path: p = Path(config_path).expanduser(); if not p.is_file(): raise ConfigError(f"Config file not found: {p}")`. Consider logging which file auto-discovery chose; "which config am I running?" is a common support question. Add a test.

#### M-20 — `~` is not expanded in path settings, and the "XDG" locations ignore the XDG environment variables
- **Dimension:** Functionality · **Confidence:** Confirmed (executed) · **Found by:** CORE-07
- **Location:** `src/saneless/config.py:85-86` and `:207-226` and `:250`, `src/saneless/web/app.py:71`, `src/saneless/pipeline.py:351`, `src/saneless/logging_config.py:48`, `docs/reference/configuration.md:11, 44`

**What.** No path field goes through `expanduser()`. The docs show the log default as `~/.local/state/saneless/saneless.log`, inviting users to write `~/...` themselves. With `tmp_dir = "~/saneless-tmp"`, `validate_settings_dirs` passed (neither the literal `~/saneless-tmp` nor its "parent" `~` exists, so the check is skipped) and the app then created a directory literally named `~` in the current working directory. `$XDG_CONFIG_HOME` and `$XDG_STATE_HOME` are ignored even though the docs describe these paths as XDG locations.

**Why it matters.** Scans and logs quietly land in `./~/...` relative to wherever the service was started; the user looks in their home directory and finds nothing. The validation function's silence makes it worse: it exists to fail fast on bad directories and here it approves one. The principle: normalise user-supplied paths once, at the boundary, so every consumer sees an absolute path.

**How to fix.** Add an "after" validator on `tmp_dir`, `log_file`, and `consume_dir` that returns `str(Path(v).expanduser())` for non-empty values. Compute defaults from `XDG_STATE_HOME` and `XDG_CONFIG_HOME` with the usual fallbacks, or use `platformdirs`. In `validate_settings_dirs`, walk up to the nearest existing ancestor instead of checking only `parent`.

#### M-21 — `log_level` is not validated, so bad values crash after configuration "succeeded", and `-v` does not enable DEBUG despite the docs
- **Dimension:** Functionality, Documentation · **Confidence:** Confirmed (executed) · **Found by:** CORE-08, OPS-14
- **Location:** `src/saneless/config.py:87`, `src/saneless/logging_config.py:44-45` and `:62-65`, `src/saneless/cli.py:53-58` and `:71-77` and `:298`, `docs/reference/cli-commands.md:12`, `docs/explanation/empty-page-detection.md:54`

**What.** Any string is accepted for `log_level`. `TRACE` (documented by uvicorn, so a plausible guess) makes `configure_logging` raise `AttributeError` outside the `try` in the CLI group, printing a traceback. `warn` is accepted by the logging module but `saneless serve` then dies with `KeyError: 'warn'` inside uvicorn, whose table has only `warning`. Separately, `-v` is described as "enable debug output" but only mirrors the configured level to stderr; the root level stays INFO. The empty-page doc tells users to run with `--log-level DEBUG`, an option that does not exist.

**Why it matters.** The docs list four valid values; the code should enforce exactly those so the user gets "log_level must be one of ..." at startup, not a traceback from a library. And the only way to see the per-page mean and deviation the doc promises is an environment variable no page mentions. The principle: validate at the edge with the narrowest type that expresses the rule; `Literal[...]` does this for free in pydantic.

**How to fix.** `log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"` plus a "before" validator that upper-cases input. Use `logging.getLevelNamesMapping()` instead of `getattr`. Make `-v` raise the effective level to DEBUG and pass that level to uvicorn, or add a real `--log-level` option. Fix both doc lines. Move the `configure_logging` call inside the CLI's `try`.

### Paperless client

#### M-22 — `poll_task` swallows non-200 responses for the whole timeout, and its clock ignores request time
- **Dimension:** Functionality · **Confidence:** Confirmed (executed) · **Found by:** CORE-10
- **Location:** `src/saneless/paperless.py:179-198`

**What.** A response that is not 200 (401 after a token change, 404 on an older Paperless without the tasks endpoint, 5xx during a restart) is ignored and the loop sleeps and retries until the timeout, logging nothing. `elapsed` is advanced only by the sleep durations, so time spent inside each HTTP call, up to the 30-second client timeout, is not counted: with `timeout=1` and 0.3-second responses the call returned after 2.1 seconds, and with 30-second hangs a 300-second timeout can become many minutes.

**Why it matters.** The worker is single-threaded, so during those minutes no other scan can start and the UI shows "Uploading" with no indication anything is wrong; the eventual `TIMEOUT` is then recorded as done (C-03). The principle: use a monotonic deadline for "wait at most N seconds", and treat an unexpected status code as a signal, not as "not yet".

**How to fix.**

```python
deadline = time.monotonic() + timeout
while time.monotonic() < deadline:
    response = self._client.get("/api/tasks/", params={"task_id": task_id})
    if response.status_code in (401, 403, 404):
        raise PaperlessError(f"task lookup failed: {response.status_code}")
    if response.status_code != 200:
        logger.warning("Task poll returned %s, retrying", response.status_code)
    ...
    time.sleep(min(delay, max(0.0, deadline - time.monotonic())))
```

### Dates and times

#### M-23 — All user-facing dates and times are UTC with no indication, including the `created` date sent to Paperless
- **Dimension:** Consistency, Functionality · **Confidence:** Confirmed (reading; arithmetic is unambiguous) · **Found by:** XC-09, PIPE-18, WEB-11
- **Location:** `src/saneless/pipeline.py:134` and `:422`, `src/saneless/web/routes.py:161-162`, `src/saneless/cli.py:259`, `partials/history.html:4`, `src/saneless/job.py:71`

**What.** Storage is correct: aware UTC ISO-8601 in SQLite. But every presentation point formats the UTC value directly: the Paperless `created` date, duplicated in two places; the auto-generated title `Scan YYYY-MM-DD HH:MM`; the CLI jobs table; and the web history table. None converts to local time or appends a zone.

**Why it matters.** A user in New York scanning at 20:30 local gets a Paperless document dated tomorrow, a title with the wrong time, and a history table that disagrees with the wall clock by four or five hours with nothing saying "UTC". The `created` date is persisted document metadata that Paperless uses for sorting and matching rules; an off-by-one that only happens in the evening is hard to notice and impossible to explain later. Ruff's DTZ rules pushed the code to `now(tz=UTC)`, which is right for storage. Presentation needs a conversion step. The principle: "use UTC everywhere" is right for timestamps and wrong for calendar dates that describe a human event.

**How to fix.** Add one helper module with `local_now()` returning `datetime.now().astimezone()` and a `to_local` Jinja filter, and use them for `created`, the default title, the CLI table, and the history table. Hoist the duplicated `created` computation into `run_pipeline`. In Docker, `astimezone()` honours the `TZ` environment variable; document it.

### Documentation that describes features that do not exist

#### M-24 — The `title` profile key is documented in three places and the example config, and nothing reads it
- **Dimension:** Functionality, Documentation · **Confidence:** Confirmed · **Found by:** XC-11, OPS-10
- **Location:** `src/saneless/config.py:75`, `docs/reference/configuration.md:69, 115, 123`, `docs/how-to/configure-scan-profiles.md:59, 145`, `saneless.toml.example:21`

**What.** `ProfileConfig.default_title_template`, aliased `title`, is defined, documented as "Default title template", and round-trip tested, but no code path reads it. The web route generates `Scan <timestamp>` when the form title is empty; the CLI requires `--title`.

**Why it matters.** A user who sets `title = "Receipt"` exactly as the reference shows gets no effect and no warning. Documented-but-inert settings erode trust in the whole reference page. The principle: every configuration field must have a reader, or be removed.

**How to fix.** Implement it (`title = form_title or profile.default_title_template or f"Scan {...}"`, defining placeholders such as `{date}` if "template" is intended) or delete the field, its docs rows, and the example line.

### Delivery: CI, release, Docker, and repository identity

#### M-25 — No CI runs tests, lint, or type checks on push or pull request; the only gate is a local pre-commit hook
- **Dimension:** Tests, Design · **Confidence:** Confirmed · **Found by:** OPS-02
- **Location:** `.github/workflows/` (only `docs.yml` and `release.yml`), `.pre-commit-config.yaml:46-62`

**What.** No workflow is triggered on push or pull request. The only job that runs tests is the release workflow's `test` job, which fires solely on version tags, and even that runs ruff and pytest but never `ty check` or `pyrefly check`, the two type checkers the project's own rules declare mandatory. The pre-commit hooks can be skipped with `--no-verify` and do not run pytest at all.

**Why it matters.** Anyone can push code that fails the type checkers or the suite and nobody finds out until a release tag is cut, at which point the release itself fails (M-26). The principle: the quality gate must run on a machine nobody can bypass, before merge.

**How to fix.** Add `.github/workflows/ci.yml`:

```yaml
on: { push: { branches: [main] }, pull_request: {} }
permissions: { contents: read }
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
      - run: sudo apt-get update && sudo apt-get install -y libsane-dev
      - uses: astral-sh/setup-uv@v7
      - run: uv sync --locked
      - run: uv run ruff check . && uv run ruff format --check .
      - run: uv run ty check && uv run pyrefly check
      - run: uv run pytest -m "not browser"
```

Make the release workflow's test job reuse it via `workflow_call` so the gate is defined once.

#### M-26 — The release workflow cannot succeed: an unresolvable action ref, missing SANE headers, and browser tests without a browser
- **Dimension:** Functionality · **Confidence:** Confirmed for the action ref (lead checked `git ls-remote`); Likely for the other two · **Found by:** OPS-03
- **Location:** `.github/workflows/release.yml:12-16` and `:28`, `uv.lock` (python-sane is sdist-only), `tests/test_browser.py`

**What.** Three independent failures. `pypa/gh-action-pypi-publish@v1.12` does not exist: the upstream repository has tags `v1.12.0` through `v1.12.4` and a branch `release/v1.12`, but no `v1.12` ref, so GitHub Actions cannot resolve the step. `uv sync` on a bare runner must compile `python-sane` from source, which needs `libsane-dev`; the README documents this for users but the workflow never installs it. `uv run pytest` runs the browser-marked tests, which need Chromium; there is no install step and no deselection.

**Why it matters.** The first `git push --tags` will fail, and because both publish jobs depend on `test`, nothing is published. Debugging a release pipeline under time pressure is exactly when you do not want three unrelated failures stacked. The principle: exercise the release path end to end before you need it, for example with a release candidate tag against TestPyPI.

**How to fix.** Use `pypa/gh-action-pypi-publish@release/v1` or pin a full tag or SHA; install `libsane-dev` before `uv sync`; run `pytest -m "not browser"` or add `uv run playwright install --with-deps chromium`; add a job that asserts the tag equals `project.version` so a `v1.0.0` tag cannot publish a wheel labelled `0.1.0`.

#### M-27 — Every published name in the repository differs from the actual remote; update the docs to `kdknigga/saneless`
- **Dimension:** Documentation, Consistency · **Confidence:** Confirmed (lead checked `git remote -v`) · **Found by:** OPS-04
- **Decision (from the project owner, 2026-09-09):** the remote `git@github.com:kdknigga/saneless.git` is canonical. Documentation and configuration must be updated to match it; the remote is not being renamed.

**What.** Every user-facing reference says `kris-knigga/saneless`: the GitHub URLs in `pyproject.toml`, the image `ghcr.io/kris-knigga/saneless:latest`, the docs site `kris-knigga.github.io/saneless`, and the README's link text `saneless.github.io`. The release workflow is already correct: it publishes the image as `ghcr.io/${{ github.repository }}`, which resolves to `ghcr.io/kdknigga/saneless`, so after a release the compose file and docs would pull an image name that does not exist. Today every one of these URLs returns 404.

**Why it matters.** A reader of the README is told `pip install saneless` and `docker run ghcr.io/kris-knigga/saneless` as if they work. The principle: a name that appears in many files should be derived from, or verified against, the one source of truth, which is the remote.

**How to fix.** Replace every occurrence in the shipped files with the `kdknigga/saneless` forms. The PyPI distribution name `saneless` is independent of the repository name and can stay; only URLs and the image name change. The complete list in tracked, shipped files (the `.planning/` history and the generated `site/` are excluded; regenerate `site/` after the change):

| File | Lines | Current | Change to |
|---|---|---|---|
| `pyproject.toml` | 41, 42, 43 | `https://github.com/kris-knigga/saneless` (`Homepage`, `Repository`, `Issues`) | `https://github.com/kdknigga/saneless` (`/issues` for Issues) |
| `mkdocs.yml` | 3 | `site_url: https://kris-knigga.github.io/saneless/` | `https://kdknigga.github.io/saneless/` |
| `mkdocs.yml` | 4, 5 | `repo_url` and `repo_name` with `kris-knigga/saneless` | `https://github.com/kdknigga/saneless`, `kdknigga/saneless` |
| `docker-compose.yml` | 12 | `image: ghcr.io/kris-knigga/saneless:latest` | `ghcr.io/kdknigga/saneless:latest` |
| `docker-compose.yml` | 18 | `# See: https://github.com/kris-knigga/saneless#configuration` | `https://github.com/kdknigga/saneless#configuration` |
| `README.md` | 37 | `docker run ... ghcr.io/kris-knigga/saneless` | `ghcr.io/kdknigga/saneless` |
| `README.md` | 72 | link text `saneless.github.io`, URL `kris-knigga.github.io/saneless/` | text and URL `kdknigga.github.io/saneless/` |
| `README.md` | 74, 75, 76, 77 | four doc links under `kris-knigga.github.io/saneless/` (74 also points at a page that does not exist, N-29) | `kdknigga.github.io/saneless/...` |
| `docs/getting-started/quick-start.md` | 21 | `ghcr.io/kris-knigga/saneless:latest` | `ghcr.io/kdknigga/saneless:latest` |
| `docs/getting-started/first-cli-scan.md` | 38 | `ghcr.io/kris-knigga/saneless` | `ghcr.io/kdknigga/saneless` |
| `docs/how-to/deploy-docker-compose.md` | 48, 69 | `ghcr.io/kris-knigga/saneless:latest` | `ghcr.io/kdknigga/saneless:latest` |
| `docs/how-to/scanner-host-discovery.md` | 35 | `ghcr.io/kris-knigga/saneless:latest` | `ghcr.io/kdknigga/saneless:latest` |
| `docs/reference/docker.md` | 9, 51, 65, 83, 101 | `ghcr.io/kris-knigga/saneless:latest` | `ghcr.io/kdknigga/saneless:latest` |

One command finds them all and nothing else (24 lines in nine files today):

```bash
git grep -n -e "kris-knigga" -e "saneless\.github\.io" -- ':!.planning' ':!site'
```

After the edit it should print nothing. Add it as a CI step so the names cannot drift again, and until the first release is on PyPI and GHCR, add a "Not yet published; install from source" note to the README. Consider registering the PyPI name `saneless` now.

#### M-28 — Application logs are invisible to `docker logs`; they go only to a file inside the container's writable layer
- **Dimension:** Functionality (deployment) · **Confidence:** Confirmed (executed) · **Found by:** OPS-05
- **Location:** `Dockerfile:22-23`, `src/saneless/logging_config.py:44-65`, `src/saneless/cli.py:293-300`, `docker-compose.yml:20`

**What.** `configure_logging` attaches only a rotating file handler to the root logger; the stderr handler is added only with `-v`. The image's `CMD ["serve"]` has no `-v`, so every log line, scan progress, worker errors, and the access log, is written to a file under `/root/.local/state/` inside the container. `docker logs` shows exactly one line, the "Serving on" banner. The file is not on a volume, so it is lost on the documented update procedure.

**Why it matters.** The first thing an operator does when a scan fails is `docker logs`; they will see nothing and conclude the app is not logging. Every container platform expects logs on stdout or stderr. The principle: in a container, stdout and stderr are the log sink; a file-only logger is a bare-metal assumption.

**How to fix.** Cheapest: `CMD ["-v", "serve"]`. Better: always add a stderr handler when stderr is not a TTY or when `log_file` is empty, and document `SANELESS_OUTPUT__LOG_FILE=""` for containers. Decouple `-v` from "add a handler" (M-21).

#### M-29 — The Dockerfile healthcheck and `EXPOSE` hard-code port 8080 while the shipped example config sets `web_port = 8081`
- **Dimension:** Functionality (deployment), Documentation · **Confidence:** Confirmed · **Found by:** OPS-06
- **Location:** `Dockerfile:19-21`, `saneless.toml.example:11`, `docker-compose.yml:14`, `docs/reference/docker.md:42`

**What.** The healthcheck curls `localhost:8080/health` and the image exposes 8080. `saneless.toml.example`, the file users are told to copy, sets `web_port = 8081` with the comment "change if 8080 is taken", and the Docker reference lists `SANELESS_OUTPUT__WEB_PORT` under common variables.

**Why it matters.** A user who copies the example and uses the documented compose file gets a server on 8081, a port mapping to nothing, and a healthcheck that fails forever, so the container shows unhealthy and gets restart-looped by autoheal or Swarm. From inside the container everything "works". The principle: a healthcheck must read the same configuration the server does, or the port must be pinned in the image.

**How to fix.** Simplest and honest: use the real default in the example (`# web_port = 8080`), document that the container always listens on 8080 and the host port is changed via the compose `ports:` mapping, and remove `WEB_PORT` from the Docker "common" table.

#### M-30 — Compose mounts the config read-only, which silently breaks the server's auto-profile feature with a misleading warning; the guide also claims a missing file makes the container fail
- **Dimension:** Functionality (deployment), Documentation · **Confidence:** Confirmed (executed) · **Found by:** OPS-07
- **Location:** `docker-compose.yml:16-19`, `docs/how-to/deploy-docker-compose.md:27-28, 71`, `src/saneless/worker.py:159-191`, `src/saneless/config.py:137-142`

**What.** The compose guide says the minimum config is a bare `[profiles.default]`. That is exactly the condition under which the worker tries to auto-generate profiles on the first scan; it finds `/etc/saneless/config.toml`, the write fails on the `:ro` mount, the broad catch logs "scanner unreachable" (untrue), and because the in-memory update comes after the write, the generated profiles are discarded. Separately, the guide says the container "will fail to start if the file is missing". It will not: Docker creates a directory named `config.toml` on the host, `exists()` is true, pydantic-settings ignores non-files, and the server starts with defaults plus environment variables.

**Why it matters.** Users get a bare default profile forever, a log line blaming the scanner, and a doc that gave them the wrong mental model in both directions.

**How to fix.** Mount the directory rather than the file (`./saneless-config:/etc/saneless:ro`) to avoid the directory-creation trap, and say truthfully that a missing config is ignored. In the worker, update `settings.profiles` before attempting the write and make the warning describe the real exception. See also M-04.

#### M-31 — No `.dockerignore`: every local build ships the 526 MB `.venv`, `.git`, and the untracked `saneless.toml` containing a real API token into the build context
- **Dimension:** Functionality (build and security hygiene) · **Confidence:** Confirmed · **Found by:** OPS-12
- **Location:** `Dockerfile:5`, repository root

**What.** `COPY . .` in the builder stage copies the whole working tree. Measured: `.venv` 526 MB, `.git` 12 MB, `site` 3.3 MB. The working tree also contains the developer's git-ignored `saneless.toml` with a live Paperless token; it lands in the builder layer and the local build cache even though the final image receives only the wheel.

**Why it matters.** Slow builds are the visible symptom. The invisible one is that a secret file is copied into an image layer every time the author builds locally; anyone who later pushes the builder stage, exports the cache, or collapses the Dockerfile to one stage ships the token. The principle: a build context should contain only what the build needs, and secrets must never be under `COPY .`.

**How to fix.** Add an allow-list `.dockerignore` (`*`, then `!pyproject.toml`, `!uv.lock`, `!README.md`, `!LICENSE`, `!src/`) and copy the manifest files and `src` explicitly in the Dockerfile for better layer caching.

### Test suite

#### M-32 — The scanner test doubles model a SANE API that does not exist, and the pipeline's `MagicMock` scanner accepts any source; these fakes are why four shipped defects have green tests
- **Dimension:** Tests · **Confidence:** Confirmed (lead compared the fake with the installed `sane.py`) · **Found by:** SCAN-09, XC-19
- **Location:** `tests/test_scanner.py:47-136` and `:164-207` and `:893-933`, `tests/test_pipeline.py:438-439` and similar

**What.** Differences between the fakes and the real `sane.SaneDev` and its iterator, each of which hides a finding above: unknown option assignment silently succeeds on the real class but the fake raises (M-15); the real iterator calls `start()` and `snap()` per page and converts exactly one message to `StopIteration`, the fakes return `iter(list)`; `multi_scan()` cannot raise on the real class, yet `test_empty_feeder_out_of_documents_error` builds its whole scenario on it raising; real errors are `_sane.error` instances with real messages, never raised by any fake (M-11); the real resolution constraint is often a `(min, max, step)` tuple, every fake uses lists; real source names include "Automatic Document Feeder", every fake uses "ADF" (C-06). In the pipeline tests, `MagicMock(spec=ScannerBackend)` accepts any `ScanSettings`, which is how C-01 escaped.

**Why it matters.** Tests are a model of the world the code runs in. When the fake is more convenient than the truth, the suite verifies that the code matches the fake, and every bug above is precisely "the code matches the fake". The principle: derive fakes from the third-party source (sane.py is 420 lines), and back them with at least one test that runs the real library, even if it is optional.

**How to fix.** Rewrite the fakes to mirror `SaneDev.__setattr__`, `__getattr__`, and the iterator, raising a local error type with real message strings. Add an opt-in integration test module that points `SANE_CONFIG_DIR` at a temp dir containing `dll.conf` with just `test` and drives `SaneBackend` against `test:0`: ADF page count with the long feeder name, capabilities with a range constraint, first-page I/O error message, blank-page handling, and the timeout path using the backend's `read-delay` option. Everything needed is already installed on this machine; the scanner agent's scratch scripts show the recipe. In the pipeline tests, let at least one manual-duplex test use the fake SANE module from `test_scanner.py` instead of a `MagicMock`.

#### M-33 — The paths where data is lost or misreported have no tests at any level, and several existing tests cannot fail
- **Dimension:** Tests · **Confidence:** Confirmed (coverage run plus reading of every test) · **Found by:** PIPE-13, CORE-11, XC-18, WEB-16, OPS-25
- **Location:** listed per item

**What.** Line coverage is 94 percent, but the uncovered six percent sits at the integration seams where every critical finding lives. No test covers: a Paperless `FAILURE` or `TIMEOUT` reaching the job state (C-03); upload failure after a successful scan preserving anything (C-04); two consume-directory fallbacks in one directory (C-05); manual duplex through the real backend or the CLI (C-01, C-02); the worker plus the real pipeline plus a stub scanner end to end (every worker test monkeypatches `run_pipeline` away); `JobStore` from two threads (C-07); the worker surviving an unexpected exception, a full queue, or `stop()` during a job (C-09); startup with stale active jobs (M-03); a browser test that submits a scan and checks the button afterwards (C-10); `/api/tags`, `/api/correspondents`, the Paperless-down dropdown path, or a cache hit through HTTP; `/health` returning 503; `validate_settings_dirs` consume-dir branches; device auto-detection; unknown `--profile`; a nonexistent `--config`; a nested config typo; an invalid `log_level`; the PDF page geometry; and the generated config being loadable. Meanwhile several assertions cannot fail: `test_manual_duplex_flip_event_wait` pre-sets the event and contains no assertion; `test_run_pipeline_happy_path` asserts `result is not None` against a `MagicMock`; `test_nearest_when_no_exact` asserts `result in (150, 600)`, which any element of the input satisfies; `test_preserves_comments` contains a tautology; `test_cache_invalidate`, `test_flip_continue`, and `test_flip_abort` assert only a 200 with no active job; `test_pico_css_applied` asserts a bounding box exists while its comment describes a margin check it never performs; `test_flip_prompt_not_visible_on_idle` wraps its only assertion in `if count > 0`.

**Why it matters.** Every critical finding in this review would have been caught by a test on the corresponding branch. A green suite that skips the failure branches gives false confidence exactly where confidence matters most, and an assertion that cannot fail is worse than none because it looks like coverage. The principle: for every `raise`, `except`, and early `return` in orchestration code, ask "which test executes this line, and what would it assert if the line were wrong?"

**How to fix.** Add one worker-plus-pipeline-plus-stub-scanner-plus-`httpx.MockTransport` end-to-end test per outcome (success, failure, timeout, fallback, duplex mismatch). Add one browser test of a complete scan cycle and one of the flip prompt. Add the negative-path CLI and config tests listed above; most are ten to fifteen lines using existing helpers. Replace the tautological assertions with exact expectations. Give the pipeline's `mock_paperless` a `spec=PaperlessClient` so a renamed method is caught. The verification scripts written for this review are reusable starting points.

#### M-34 — The suite is not hermetic: a fixed `/tmp/saneless-test` path shared with real `serve` runs, tests that read the developer's private `saneless.toml`, a fixed port, and chmod checks that fail as root
- **Dimension:** Tests · **Confidence:** Confirmed · **Found by:** XC-10, OPS-13
- **Location:** `tests/conftest.py:20-21, 44, 91-93`, `tests/test_cli.py:33-34, 46-49, 575-583`, `tests/test_config.py:48-59, 337-351`, `tests/test_auto_profiles.py:140-143`, `tests/test_logging.py:113-132`

**What.** `default_settings` and `_make_settings` point `tmp_dir` at the fixed `$TMPDIR/saneless-test`; the four `serve` tests open a real SQLite database there, one file shared between every run, every parallel worker, and any developer who ever pointed a real `serve` at it (a leftover `saneless.db` exists on this machine now). `test_env_prefix` and `test_nested_env_delimiter` call `load_settings()` without changing directory, so from the repository root they read the developer's real `./saneless.toml`, which contains a live Paperless URL and token; they pass only because environment variables take priority. `test_serve_custom_host_port` forgets `_mock_socket` and really binds port 9090. Two tests rely on `chmod` denying access, which fails as root, the typical Docker CI user. The shared `mock_scanner` fixture returns one exhausted iterator, so a second `scan_pages` call yields nothing.

**Why it matters.** Hermetic tests are the difference between "red means a bug" and "red means someone else is running the suite". Reading a private config from tests is also a small security smell. The principle: a test must own everything it touches. Use `tmp_path`, patch `Path.home`, use port 0.

**How to fix.** Add an autouse fixture that does `monkeypatch.chdir(tmp_path)` and patches `Path.home()`. Make the settings builders take `tmp_path` and delete the fixed constants. Call `_mock_socket` in the port test. Mark the chmod tests `skipif(os.geteuid() == 0)`. Use `side_effect=lambda *a: iter([img])` in the scanner fixture.
## 5. Minor findings and nits

These are worth fixing when you touch the file. Each entry is deliberately short: the location, what is wrong, why it matters, and the fix. Confidence is Confirmed unless marked otherwise.

### Scanner backend

**N-01 (SCAN-10) — `get_capabilities` ignores range constraints.** `sane_backend.py:327-339` handles only list constraints, but python-sane documents three shapes: `None`, a `(min, max, step)` tuple, or a list. The `test` backend and many real devices report resolution as a range, so `saneless devices --capabilities` prints an empty resolutions line and auto-profiles silently falls back to 300 even when it is outside the device's range. The same tuple parsing is duplicated at `:464-470`. Write one `_constraint(raw_options, name)` helper and either expose the range on `DeviceCapabilities` or expand common DPIs that fall inside it.

**N-02 (SCAN-12) — A mid-batch failure discards every page already acquired.** After the first page, any exception (jam, cover open) is re-raised from the generator and the consumer's `list()` throws away the pages it collected (`sane_backend.py:411-414`, `pipeline.py:268`). A jam on sheet 40 of 50 costs 39 pages, while the project's own duplex-mismatch handling shows the intended philosophy is "never discard scanned data". Decide the generator's contract on error explicitly: attach partial pages to the exception or yield a final status object, and offer the "upload what we have with a warning" path. *Likely.*

**N-03 (SCAN-14) — Geometry values are assumed to be millimetres.** The SANE scan-area options may be in pixels or millimetres and the option tuple carries the unit at index 5, but `_set_geometry` writes `br_x = 210.0` unconditionally (`sane_backend.py:115-120`). On a pixel-unit backend an A4 profile produces a 210 by 297 pixel scan with no error. Read the unit and convert, or skip geometry and fall back to cropping. *Likely.*

**N-04 (SCAN-15) — "sane.init() exactly once" is only true per instance, and `sane.exit()` is never called.** Each `SaneBackend()` calls `init()` with no re-entry guard (`sane_backend.py:242-244`), and `SANE_NET_HOSTS` must be set before the first `init()`, so a second instance with a different host is silently ignored. Today every CLI command constructs exactly one backend, so this is latent, but the docstring states a process-level guarantee the code does not provide. Guard with a module-level flag or make the backend a singleton, expose a `close()` that calls `sane.exit()`, and reword the docstrings. A test constructing two backends showed two `init` calls.

**N-05 (SCAN-19) — Several scanner tests do not test what their names claim.** `test_sane_backend_init_calls_sane_init_exactly_once` constructs one backend and is identical to the test above it; `test_iterator_deleted_before_cancel` asserts only that cancel was called and its own comment admits the deletion "may or may not appear"; `test_empty_feeder_out_of_documents_error` exercises `multi_scan()` raising, which the real library cannot do; `test_geometry_failure_still_completes` asserts only a page count, not that the crop fallback was taken (`tests/test_scanner.py:350-355, 570-589, 840-885, 991-1003`). A test whose name promises more than it checks is worse than no test, because reviewers assume the property is covered. Make each assertion the thing in the name, or rename.

### Pipeline, PDF, auto-profiles

**N-06 (PIPE-14) — Zero scanned pages produce a misleading error or a bare `ValueError`.** With detection on, zero pages become "All pages were detected as empty" even though nothing was detected; with detection off, `assemble_pdf([])` lets img2pdf's `ValueError: Unable to process empty list` escape (`pipeline.py:396-413`, `pdf.py:56`). The first message sends the user to tune thresholds for a problem that is not threshold-related. Check the precondition at the boundary: `if not images: raise ScanError("Scanner returned no pages")`.

**N-07 (PIPE-16) — Manual-duplex detection is duplicated in the worker, and the two-outcome result is dispatched by `isinstance`.** `worker.py:206-209` re-implements the rule from `pipeline.py:94-96`; if one changes, the pipeline silently skips the wait (exactly the C-02 failure). `_scan_manual_duplex` returns either a list or a `(fronts, backs)` tuple and the caller type-switches on it (`pipeline.py:373`); the recovery path also silently bypasses empty-page filtering. A union return with `isinstance` dispatch is a classic sign that a small result type or an exception is missing. Export one rule (or add the `duplex` field from C-01) and replace the tuple with a `DuplexMismatchError(fronts, backs)` or a tiny result dataclass.

**N-08 (PIPE-19) — A user-initiated cancel is recorded as a scanner failure.** Cancelling at the flip prompt raises a generic `ScanError` (`pipeline.py:231-233`), so the job ends in `ERROR` with category `SCANNER` and the UI says "Failed". Anyone triaging errors later is misled. Add `ScanCancelledError(ScanError)` or a `CANCELLED` job state with a neutral label, and do not log it at ERROR level.

**N-09 (PIPE-20) — Slug collisions silently drop scanner sources.** `source_to_slug` maps any source containing "feeder" or "adf" to `adf-simplex` (`auto_profiles.py:32-60`), so scanners reporting two feeder variants (Canon's "left aligned" and "centrally aligned", Epson's "ADF Front" and "ADF Back") get one profile with the last source winning and no log line. Any mapping that is not injective needs a collision strategy: append a disambiguator or keep the first and warn.

**N-10 (PIPE-22, SCAN-18, XC-22) — `PIL.Image.MAX_IMAGE_PIXELS` is mutated at import time in three modules.** `pages.py:21`, `pdf.py:22`, and `sane_backend.py:57` each relax Pillow's decompression-bomb guard for the whole process as a side effect of being imported, and each also imports `PIL.Image` alongside `from PIL import Image` to do so. Global configuration belongs in exactly one place, at application start-up, not scattered across library modules. Set it once in the entry point with the comment it already has.

**N-11 (PIPE-23) — Small cleanups.** `auto_profiles.py:250-251` introduces `auto_generated_flag = True` only to dodge ruff's FBT003 and ignores `profile.auto_generated`; `pipeline.py:82-83` and `:351` create the same directory twice; `pipeline.py:310` returns `dict` with no type parameters; `tests/test_pdf.py:32-34` calls `assemble_pdf` twice with a redundant `mkdir` between; `tests/test_pipeline.py:806` pins `len(PipelineEvent) == 6`, so adding an event breaks a test for no behavioural reason; `tests/test_auto_profiles.py:140-143` leaves `tmp_path` unused and its result depends on whether `./saneless.toml` exists in the runner's working directory.

### Paperless client, job store, config, logging

**N-12 (CORE-12) — `test_connection` reports "connected" for 404 and 5xx.** Anything that is not 401 or 403 returns "connected" (`paperless.py:212-218`): a URL pointing at some other web server, nginx answering for a stopped Paperless, or Paperless mid-restart. The indicator exists to answer "will my upload work?" and for these cases the answer is no. Return "connected" only for 200, "unreachable" for transport errors, and a new "error" value otherwise; the route already has an "error" branch.

**N-13 (CORE-13) — Schema migration is ad hoc.** The only migration is `try: ALTER TABLE ... ADD COLUMN error_category; except OperationalError: pass` (`job.py:106-111`). That `except` also swallows "database is locked", "readonly database", and "no such table", and the `thumbnail` column added later never got a migration at all, so a database from the first release fails on every `create_job` with "table jobs has no column named thumbnail" (verified). Track a schema version with `PRAGMA user_version` and apply ordered migrations, or at minimum read `PRAGMA table_info(jobs)` and add each missing column deliberately. Cover with a test that opens a first-release schema.

**N-14 (CORE-15) — `error_category` is written but never read.** No template, route, CLI output, or doc consumes the category (`job.py:36-43`, `worker.py:130-149`); the docstring promises "programmatic handling" that does not exist. Meanwhile the mapping is by exception class only, so a full disk and a user pressing abort are both filed as scanner failures. This is speculative generality: a column, an enum, a migration, and eight tests for a feature nobody uses. Either give it a consumer (a badge in the history table, a retry hint such as "load paper and retry" for FEEDER) or remove it.

**N-15 (CORE-16) — The API token is a plain `str` on the settings model.** `repr(settings)` contains the token in clear text (the lead verified this). No current code prints the settings object, and httpx redacts the `Authorization` header in its own reprs, so there is no leak today; but `Settings` is stored on `app.state` and passed everywhere, and the first `logger.debug("settings=%s", settings)` added during a debugging session writes the token to a world-readable log file. Use `SecretStr` and call `get_secret_value()` at the two construction sites. Optionally open the log file with mode 0600.

**N-16 (CORE-17) — `configure_logging` is not idempotent and can attach two stderr handlers.** Each call appends handlers to the root logger, so two calls log every line twice (this is why every test in `test_logging.py` carries a manual cleanup). When the log directory is unwritable and `-v` is given, the fallback branch and the verbose branch each add a stderr handler, so every message prints twice on the console (verified). Remove handlers this function previously installed at the top, and create at most one stderr handler.

**N-17 (CORE-19) — `job.py` repeats the row mapping and column list three times; `prune()` derives its count from two `COUNT(*)` queries.** Two copies of the row-to-`Job` conversion and three copies of the column list are where the next column gets added in two of three places (N-13 shows this already happened). `prune` computes `before - after` across two statements on the shared connection; a concurrent `create_job` between them makes the result wrong, and a scratch test forced it to return -1. Use `cursor.rowcount`, a `_row_to_job` helper or `sqlite3.Row`, and expose the database path once instead of computing `tmp_dir / "saneless.db"` in both `cli.py:226` and `app.py:73`.

**N-18 (CORE-20) — Retry tests sleep for real.** Three tests in `test_paperless.py` take exactly 3.0 seconds each and two take 0.5 seconds because the production `time.sleep(2**attempt)` runs unmocked, and no test asserts the backoff schedule `[1, 2]`, so the backoff could silently become constant or zero. Patch `saneless.paperless.time.sleep` with a recorder and assert the recorded delays.

**N-19 (CORE-22) — API-shape nits.** `_transport` is an underscore-prefixed public constructor parameter that tests must pass (`paperless.py:49`); `consume_dir: str = ""` uses the empty string as "unset" rather than `Path | None`; `poll_task`'s `timeout=300` duplicates `OutputConfig.paperless_task_timeout`; `get_tags` and `get_correspondents` are copy-paste and `page_size=1000` silently truncates larger installs; the job store docstring says "state machine" but `update_state` accepts any transition.

### Web layer

**N-20 (WEB-09) — `POST /api/scan` and `POST /api/cache/invalidate` do not validate their inputs.** Any `profile` string is accepted (a job is created, then fails in the worker), a title of 100,000 characters is stored, and `resource=bogus` returns a 200 with the correspondents partial. The one real user-facing edge: a tag deleted in Paperless within the 60-second cache window is sent with the upload, Paperless answers 400, and C-04 loses the scan. Use `Literal["tags", "correspondents"]` for the resource (FastAPI returns 422 for free), check the profile exists before creating the job, cap the title with `Form(max_length=256)`, and validate tag and correspondent ids against the cache when it is warm.

**N-21 (WEB-12) — htmx and Pico load from a public CDN with no integrity hash, so the UI does not work on an offline LAN.** The product's only stated prerequisite is a browser on the same network as the host. Without Internet the page renders unstyled, the form does a native POST, and polling never happens; with Internet every page load trusts a third party, and `@picocss/pico@2` is a floating major. `/static` already exists, so vendor the two files, or at least add `integrity` and `crossorigin`.

**N-22 (WEB-13) — State-changing POST endpoints have no cross-site protection, and the server binds all interfaces by default.** A form on any website the user visits can `POST http://<scanner-host>:8080/api/scan` without a CORS preflight and start a scan with attacker-chosen title and tags. The docs cover network exposure ("trusted LAN, use a reverse proxy") but CSRF is a browser exposure a trusted LAN does not remove. Reject POSTs whose `Sec-Fetch-Site` header is present and not `same-origin`, and consider defaulting `web_host` to `127.0.0.1` with the Docker docs setting it to `0.0.0.0`. *Likely.*

**N-23 (WEB-14) — Metadata fetch failures are logged without the cause, and the cache has no stale-on-error behaviour.** `routes.py:56-61` catches everything and logs "Failed to fetch tags ... using empty list" with neither `exc_info` nor the exception text, so the most common support question ("why are my tags empty?") cannot be answered from the log. The cache discards expired entries on read, so once Paperless goes down the dropdowns switch to empty after at most 60 seconds even though tags change rarely. Log the exception, and let the cache serve the last good value when a refresh fails.

**N-24 (WEB-17) — Worker tests rely on 26 fixed sleeps that are slow and unnecessary.** Almost every worker test does `submit(job); time.sleep(0.5); worker.stop()`, but `stop()` enqueues its sentinel behind the job and joins the thread, so the job is already processed when `stop()` returns; the sleeps add nothing but about 12 seconds. `TestWorkerEnumDispatch::test_worker_status_cb_dispatches_on_pipeline_event` is byte-for-byte the same as `TestWorkerIntermediateStates::test_worker_assembling_state`; two docstrings at `:487` and `:533` still describe string matching that was replaced by the enum; `test_cache_ttl_expiry` sleeps 1.1 seconds instead of controlling the clock. Drop the sleeps, add a `wait_for_state` helper for the flip waits, delete the duplicate, and patch `time.monotonic` in the cache test.

### CLI, packaging, deployment

**N-25 (OPS-15) — Subcommand `--help` requires a valid configuration.** Click runs the group callback before building the subcommand context, so `saneless serve --help` first loads and validates settings and configures logging; with a bad environment variable the user gets "Configuration error" instead of help, and with a good one asking for help creates the log directory as a side effect. Help must always work. Store the options in `ctx.obj` and load settings lazily in each command, or return early when `ctx.resilient_parsing` is set.

**N-26 (OPS-16) — The container runs as root, base images are unpinned, and no `WORKDIR` makes the server write `/saneless.toml`.** `uv:latest` and an undigested `python:3.14-slim` mean two builds of the same commit can differ; with no `USER` the process runs as root; with no `WORKDIR` the auto-profile file lands at `/saneless.toml` and the log under `/root/.local/state/`, both lost on recreate. Pin by digest, add a non-root user, set `WORKDIR /data`, and point `tmp_dir` and `log_file` there via `ENV`.

**N-27 (OPS-17) — The `serve` pre-bind probe rejects IPv6 hosts, and `--port 0` falls back to config.** The probe socket is `AF_INET`, so `--host ::1` fails with "Address family not supported" even though uvicorn binds IPv6 fine; `port or settings.output.web_port` treats an explicit `--port 0` as not given. Pick the family from the host string, and test `port is not None`.

**N-28 (OPS-18) — `devices --json --capabilities` mixes JSON and free text on stdout**, so piping to `jq` fails. Include capabilities inside the JSON objects when `--json` is set, or make the flags mutually exclusive.

**N-29 (OPS-20, OPS-21) — README links to a tutorial page that does not exist, and the internal PRD is published to the site without being in the nav.** `README.md:74` links `tutorials/scan-your-first-document/`; there is no `docs/tutorials/`. `mkdocs build --strict` passes but reports `PRD.md` as not in the nav, meaning it is rendered and reachable by URL. The PRD is also stale: it still describes a device picker, a Test Connection button, count mismatches that fail the job, normalised 0 to 1 thresholds, and a `/api/jobs/{id}` route, none of which match the shipped code. Point the README at `getting-started/first-cli-scan/`, and either move the PRD out of `docs/` or add a "historical; see reference docs" banner.

**N-30 (OPS-22) — The wheel ships no LICENSE file.** With the legacy `license = {text = "MIT"}` table, the built wheel contains no `LICENSE` (verified with `unzip -l`), and the MIT licence requires the notice to accompany distributions. Use `license = "MIT"` and `license-files = ["LICENSE"]`. Also: `httpx` is listed in both runtime and dev dependencies; `[tool.ty.src]` and `[tool.pyrefly]` are empty tables.

**N-31 (OPS-23) — CI and hook hygiene.** Actions are pinned to floating major tags a compromised upstream can move; the release `test` job has no `permissions` block; `docs.yml` runs `pip install mkdocs-material` unpinned while the lock has 9.7.6; the `sort-simple-yaml` hook ships with `files: '^$'` and never runs; ty and pyrefly run whole-project on every commit including docs-only ones. Pin by SHA with Dependabot, add `permissions: { contents: read }`, build docs with `uv run` so the lock applies, delete the dead hook.

**N-32 (OPS-24) — `.gitignore` contains a blanket `*.png`** (line 307) that will silently exclude any future docs image, favicon, or test fixture, and because the wheel and Docker build take files from the tree, the author's machine works while everyone else's is missing the asset. Replace with the specific screenshot directories.

**N-33 (OPS-25) — `test_cli.py` gaps.** `test_auto_profiles_force_flag` never reads the file back; `test_jobs_exit_code_zero`'s docstring says "always exits 0", which is false (M-17); `test_verbose_flag` pins the `configure_logging(verbose=True)` call rather than observable behaviour; `test_scan_status_output` does not assert order, so a regression that printed "Uploading" before "Scanning" would pass. No tests exist for unknown `--profile`, `ConfigError` from the pipeline, a busy port, or a missing `--config`.

### Cross-cutting

**N-34 (XC-12, SCAN-16, PIPE-21) — Nineteen comments in `src/` cite planning artefacts that are not part of the shipped code.** "Pitfall #5", "D-13", "SCAN-07", "Phase 11", "RESEARCH.md Open Question 1", and "Per user decision" appear at `config.py:44, 197`, `pages.py:115`, `paperless.py:165`, `pipeline.py:240, 391, 395`, and eight places in `sane_backend.py`. The numbering is already inconsistent: "Pitfall #5" means "source validated against capabilities" at `sane_backend.py:10` but "invalid EXIF breaks img2pdf" at three other sites, and "Pitfall #1" means two different things in the same file. A new contributor cannot follow these references, and where the reference is load-bearing the actual reason is missing. Comments must be self-contained explanations of why; plan IDs belong in commit messages. Rewrite each as a standalone reason, for example: "python-sane's multi_scan iterator calls sane_cancel() in `__del__`; drop it before dev.cancel() so the device is not cancelled twice."

**N-35 (XC-13, SCAN-22, CORE-22) — The project's own rule bans `# noqa` and `# type: ignore`, but `src/` contains six and `tests/` four.** Some are avoidable: `scanner/__init__.py:23` sits in dead code (N-37); `tests/test_cli.py:550` imports FastAPI inside a closure for no reason; `tests/test_scanner.py:706, 723` assign lambdas to a method where a small subclass would do; `tests/test_web.py:406` needs only a return annotation. Others are genuinely forced by third-party signatures (`config.py:124-125, 168`, `sane_backend.py:47, 49`). A junior developer reading CLAUDE.md will either believe the rule and be confused by the code, or learn that rules are decorative. Remove the avoidable four, move the forced ones into `per-file-ignores` where possible, and amend the rule to "suppressions require a justification comment and must be listed in CLAUDE.md".

**N-36 (XC-14) — Duplicated logic inventory.** Beyond the cases already promoted to findings, the same concept is implemented in two or more places for: the job database path (`cli.py:226`, `app.py:73`); EXIF stripping (`sane_backend.py:423, 518`, `pipeline.py:392-393`, `pages.py:116`, where the pipeline re-strips what the backend already stripped); the paper-size type (a `PaperSize` Literal in `paper_sizes.py` that `config.py` retypes and `scanner/base.py` declares as `str`); the empty-page thresholds 250.0 and 5.0 (`config.py`, twice in `pages.py`); log rotation defaults (`config.py`, `logging_config.py`); the "current job, else most recent" lookup (twice in `routes.py`, and two more sites without the fallback); SANE option-tuple parsing for `source` (twice in `sane_backend.py`); `page_size=1000` (twice); the history limit 50 (twice, while the CLI default is 20). Every pair is a future instance of the bug class behind C-01, C-06, and M-04. Centralise each.

**N-37 (XC-15, SCAN-20) — Dead code.** `scanner/__init__.py:11-27` defines a module `__getattr__` to lazily import `SaneBackend`, but every importer, including `cli.py:31`, imports from `saneless.scanner.sane_backend` directly, and `sane_backend` already defers the C-extension import itself; coverage shows lines 22-27 never execute, and `__all__` lists a name that is not a real attribute. `PaperSize` is exported but only tests reference it. Delete the `__getattr__` (or make the one caller use it) and import `PaperSize` where the Literal is retyped.

**N-38 (XC-16) — Stringly-typed protocols make the type checkers blind exactly where modules meet.** `upload_document` returns a task id or the magic string `"fallback"`; `test_connection` returns one of three magic strings; `poll_task` and `run_pipeline` return dictionaries keyed by convention; tags and correspondents are `list[dict[str, object]]`; every route reads services through `request.app.state`, which is `Any`, so ty and pyrefly check none of the route bodies. The project pays for two type checkers, yet its most bug-prone seams are invisible to them: C-03 is literally a dictionary key nobody read. Use a `NamedTuple` for the upload outcome, a `StrEnum` for connection status, a `PipelineResult` dataclass, `TypedDict`s for the rows, and a typed `AppServices` object retrieved through one helper so route bodies are checked.

**N-39 (XC-17) — Job history lives in `tmp_dir`, which defaults to the system temp directory.** `saneless.db` sits next to scratch scan files under a path many distributions clear on reboot or by `systemd-tmpfiles`, while the docs promise seven-day retention and the module docstring promises crash recovery. State that must outlive a reboot belongs under the XDG state directory, where the log file already goes. Add `output.state_dir` and derive the database path from it in one place.

**N-40 (XC-19) — Low-value tests.** Five tests have no assertion at all; about 25 pin literal constants or pydantic field defaults and would only fail if someone edited the literal; two tests test the test double (`TestMockBackend`); five pairs are duplicates (`test_health_endpoint_no_auth` and `test_health_endpoint_ok`, `test_jobs_exit_code_zero` and `test_jobs_empty`, `test_flatbed_no_regression` and `test_flatbed`, plus the two in N-05 and N-24); three separate Settings builders exist across `conftest.py`, `test_cli.py`, and `test_web.py`; `default_settings.output.tmp_dir = str(tmp_path)` is repeated 23 times in `test_pipeline.py`; eight hand-written scanner stubs exist alongside `conftest.mock_scanner`; tests reach into private names 70 times. The test-to-source ratio of 1.5 to 1 is inflated by these. Delete or merge the duplicates and constant-pinning tests, move the shared builders and one `StubScanner` into `conftest.py`, and wrap resources in `yield` fixtures.

**N-41 (XC-20) — Logging consistency.** `sane_backend.py:391-400` logs at ERROR and raises with the same text, and the worker logs the same failure again, so one timeout produces two ERROR lines; `_handle_duplex_mismatch` logs the warning it also returns (which nobody reads); `worker.py:188` asserts "scanner unreachable" for any exception; `cli.py:138-143` prints errors to stderr but never logs them, so the log has no record of CLI failures. Log once, at the boundary that handles the exception, with `exc_info` when it is unexpected; raise without logging in between.

**N-42 (XC-21) — README examples do not run.** `saneless scan  # Scan a document` fails because `--title` is required; the README's `source = "flatbed"` is compared case-sensitively against the device list, where every SANE backend spells it `"Flatbed"`, so the example profile raises on any scanner that exposes a source option. Fix both, and consider matching sources case-insensitively in `scan_pages` and using the device's spelling.

**N-43 (XC-23) — Path settings are `str` and re-wrapped in `Path()` at nine call sites**, and function signatures mix `str` and `Path` (`configure_logging(log_file: str)` versus `assemble_pdf(output_dir: Path)`). Pydantic supports `Path` fields natively; convert once in the model.

**N-44 (XC-24, OPS-27, WEB-18) — Naming and template nits.** `cli.__all__` exports `_truncate` only so a test can import it; `paperless.py`'s `_transport` parameter is public but underscore-prefixed; `tests/test_web.py:70-87` has a fixture named `test_settings` that reads as a test; `ScanWorker.__init__`'s docstring says it starts a queue, which it does not; the CLI module docstring lists two of five commands and its `logger` is unused; "Discovering scanners..." goes to stdout, so `saneless devices | grep` is polluted. In the templates, `<html data-theme="auto">` is a no-op in Pico v2 (only `light` and `dark` exist), `<table role="grid">` is the Pico v1 idiom and tells screen readers the table is an interactive widget, the tag and correspondent selects are server-rendered and then immediately re-fetched by `hx-trigger="load"`, and the DONE partial re-fetches the history the page just rendered. `create_app` calls `mkdir` on `tmp_dir` before `lifespan` validates it, so the friendly "not writable" error can never fire from the app path. `_categorize_error` does not use `self`.

**N-45 (CORE-21, SCAN-17) — Two debug notes are stale.** `.planning/debug/paperless-token-test.md` still says "investigating" although the fix shipped in commit `a3b538c` and is tested. `.planning/debug/scanner-memory-overflow.md` says the flatbed path "now shells out to `scanimage`" with six new tests; `git log -S scanimage` finds no such code and the tree still uses `start()` and `snap()`. The next person debugging the memory report will look in the wrong place. Update or delete both.
## 6. What is done well

Google's guide asks reviewers to point out good practice, because a positive example teaches as clearly as a correction. These are specific things in this codebase to keep doing.

- **A real abstraction at the hardware boundary.** `ScannerBackend` in `scanner/base.py` has three methods, which is the right size. The web tests, browser tests, and CLI tests all implement it with typed stubs, and it keeps python-sane out of every layer above the backend, exactly as the architecture document claims.
- **A clean, acyclic module graph.** No core module imports the web layer, every one of the twenty modules declares `__all__`, every module uses `from __future__ import annotations` with `TYPE_CHECKING` blocks, and the two in-function imports that exist are deliberate lazy loads of heavy dependencies with a comment saying why.
- **Progress reported by enum identity, not string matching.** `PipelineEvent` is a `StrEnum` and both consumers compare with `is` or a lookup table. Adding a UI does not touch the pipeline. (M-05 asks for one enum instead of two, but the direction is right.)
- **Resource lifecycles handled on every exit path.** `_open_device` in `sane_backend.py:265-286` cancels then closes on success and on error and suppresses cancel failures so close still runs; `TemporaryDirectory` wraps scan scratch space in both the pipeline and `assemble_pdf`; the CLI closes the Paperless client in a `finally`. The tests for the device context manager genuinely exercise the error path.
- **A hard-won lesson pinned by a test.** Passing a progress callback to python-sane's `snap()` segfaults; the code avoids it and `test_sane_backend_no_progress_callback` makes sure it stays avoided. This is how you stop a painful discovery from regressing.
- **Per-page timeouts implemented the hard, correct way.** `signal.alarm` is unusable off the main thread, so the backend uses futures, and the reasoning is written down in the code. (M-12 asks for the teardown to wait for the thread, but the design choice is sound.)
- **The HTTP client is tested through a real transport.** `tests/test_paperless.py` uses `httpx.MockTransport`, so request building, multipart encoding, and status handling are all real. `test_form_fields_sent_as_data_not_files` is a model regression test: it pins the exact wire shape Paperless cares about.
- **Secrets do not leak.** The token never appears in exception text, client reprs, or logs; `saneless.toml` is git-ignored and was never committed; the example config uses placeholders; `/api/paperless/test` returns only the exception class name, and a test feeds it an IP and a token to prove it.
- **Retries that re-open the file.** `paperless.py:107-112` re-opens the PDF inside the retry loop so a retried upload sends the whole file rather than an exhausted file object, a classic retry bug avoided. The "task not visible yet" race after upload is handled deliberately and the backoff is capped.
- **The right clocks and the right timestamps.** `time.monotonic()` for cache TTLs, and timezone-aware UTC ISO-8601 in SQLite so ordering and pruning work as plain string comparisons. (M-23 is about presentation, not storage.)
- **A friendly config error.** `_build_settings` turns pydantic's `extra_forbidden` into a sentence that names the valid sections, and the `[default]` versus `[profiles.default]` mistake is covered by a test. M-18 asks for the same care one level down.
- **A test environment that cannot be polluted by the developer's shell.** The autouse `clean_env` fixture strips `SANELESS_*` variables before every test.
- **Warnings are errors.** `filterwarnings = ["error"]`, `--strict-markers`, `--strict-config`, and `xfail_strict` in `pyproject.toml`, with no local suppression anywhere in `tests/`. An unclosed connection fails the suite.
- **The browser test server binds port 0** and reads the assigned port back, with an accurate comment about uvicorn signal handling in a non-main thread. This is the one place the suite gets port handling right.
- **Web tests use a real `TestClient` with lifespan** and a real `ScannerBackend` subclass; worker tests drive a real thread with real events. The worker is never mocked out of its own tests.
- **Templates escape by default and nobody overrides it.** No `|safe`, no inline scripts, `role="alert"` on errors, `aria-busy` on busy states, `aria-label` plus screen-reader text on icon buttons, and polling attributes emitted only while a job is active so an idle page makes no requests.
- **Small pure functions where it counts.** The auto-profile mapping helpers are pure and named for what they do, which is why they were trivial to test and to reason about during this review.
- **Packaging basics that first projects often miss.** The wheel includes templates and static files (verified); the Dockerfile installs and purges the build toolchain in one layer, uses exec-form entrypoints so uvicorn is PID 1, and has a healthcheck; the release workflow uses PyPI trusted publishing and least-privilege token scopes; `sync-with-uv` keeps the ruff hook version equal to the lock.
- **Documentation that follows a structure.** The docs follow the Diátaxis split, `architecture.md` is a genuinely useful narrative, and the reference pages for configuration defaults and web routes were fully accurate against the code. The problems are drift in a few claims, not absence.
## 7. Lessons that cut across the findings

Most of the 89 findings are instances of about eight underlying habits. Learning the habit is worth more than fixing any one instance, because the next instance will not be on this list.

**1. Make failure impossible to ignore.** `run_pipeline` returns a dictionary that says whether the upload worked; `upload_document` returns the string `"fallback"` instead of a task id; `poll_task` returns `{"status": "FAILURE"}`. Every caller forgot to look (C-03). A return value the caller must remember to inspect is a bug waiting to happen. Raise an exception for failure so every caller is forced to decide; use a distinct type or event for partial success so it cannot be confused with full success. As a rule: if a function can fail, its signature should make that visible, and the type checkers you already run should be able to catch a caller who ignores it.

**2. One field, one meaning; one rule, one place.** The profile `source` string is both "which SANE input" and "which scanning strategy" (C-01). The question "is this an ADF?" is answered four different ways in four files (C-06). The config search path is listed twice (M-04). The list of active job states is spelled out three times (M-05). Each pair drifted, and each drift became a bug. When you find yourself writing a rule you have written before, stop and import it instead. When a string is being parsed by two modules, it is time for an enum or a dataclass.

**3. Preserve what the user cannot regenerate.** A scan is expensive and irreversible; an HTTP POST is cheap and retryable. The code deletes the scan when the POST fails (C-04), overwrites earlier scans during an outage (C-05), and discards 39 acquired pages when sheet 40 jams (N-02). Once you hold data the user paid for with physical effort, every subsequent failure path must keep it somewhere and tell the user where.

**4. Validate at the edge, with the narrowest type that expresses the rule.** Misspelled config keys are silently dropped (M-18), a nonexistent `--config` path is ignored (M-19), `log_level = "warn"` passes validation and crashes uvicorn later (M-21), a URL of `""` is accepted and produces a traceback mid-scan (M-17), any profile name is accepted by the API and fails later in the worker (N-20). In every case the failure surfaces far from its cause. Pydantic's `Literal`, `AnyHttpUrl`, `SecretStr`, and `extra="forbid"` are free; use them. An explicit argument that cannot be honoured is an error, never a default.

**5. Your test double must be at least as strict as the real thing.** The pipeline's scanner is a `MagicMock` that accepts any source string, so the manual-duplex tests pass against an impossible device (C-01). The SANE fake raises on unknown attributes where the real library silently succeeds, so the crop fallback tests pass against behaviour that cannot happen (M-15). The fake iterator never raises real errors, so the error mapping was tested only with `RuntimeError("hardware error")` (M-11). When a fake is more convenient than the truth, the suite verifies that the code matches the fake. Read the third-party source when you write a fake, and keep at least one optional test that runs the real library; SANE ships a `test` backend precisely for this.

**6. Concurrency primitives are promises, not features.** `check_same_thread=False` promises that you will serialise access yourself (C-07). `async def` promises that you will not block (M-01). A bounded queue with a blocking `put` promises that somebody is always consuming (C-09). A per-page timeout promises that the timed-out call has actually stopped (M-12). In each case the code made the promise and did not keep it. When you reach for one of these, write down what you are promising and where you keep it.

**7. Configure globals once, at the entry point.** Three modules set `PIL.Image.MAX_IMAGE_PIXELS` as an import side effect (N-10); the backend writes `os.environ` and calls `sane.init()` per instance while the docstring claims once per process (N-04); auto-profiles writes to the filesystem from inside the job loop (M-04). Library modules should be inert to import and side-effect-free to construct; startup code owns global state.

**8. Documentation is part of the contract, and comments must stand alone.** Fourteen documented claims were checked and found false (section 8), including a job state that does not exist, a CLI prompt that does not exist, a profile key nothing reads, and a flag that does not exist. Nineteen comments cite planning documents with numbers that already contradict each other (N-34). When behaviour changes, the sentence that described it is now a bug too. When you write a comment, write it for a reader who has only the code: the reason, not the ticket.

**A note on what these lessons are not.** None of them is "write more code". Most fixes in this report delete a duplicate, replace a dictionary with a dataclass, change `async def` to `def`, or add one validator. The codebase is not under-engineered; it is under-connected. The pieces are good and the joints need attention.
## 8. Documentation accuracy audit

Reviewers checked concrete, testable claims in `README.md` and `docs/` against the code. A claim counts as false only when a reviewer read or ran the code and found the documented behaviour absent. Rows marked *Likely* were not executed.

### Claims that are false

| # | Where | Claim | Reality |
|---|---|---|---|
| 1 | `docs/how-to/set-up-adf-duplex.md:52, 69` | "press Enter (CLI)"; "In the CLI, saneless prompts you to flip" | No prompt exists; pass B starts immediately (C-02) |
| 2 | `docs/how-to/set-up-adf-duplex.md:56-67` | Set `source = "Manual Duplex"` to enable manual duplex | The backend rejects that source (C-01) |
| 3 | `docs/explanation/consume-directory-fallback.md:66` | "the scan job's final status is FALLBACK" | No such `JobState`; the worker writes DONE (C-03) |
| 4 | `docs/getting-started/first-web-ui-scan.md:42` and `docs/reference/web-api.md` | "Done: the document has been successfully uploaded" | Also shown for FAILURE, TIMEOUT, and fallback (C-03) |
| 5 | `docs/reference/environment-variables.md:59` | "Profile fields cannot be set via environment variables" | `SANELESS_PROFILES__DEFAULT__RESOLUTION=600` works; the project's own test relies on it. The same page's first sentence says all config can be set via env vars |
| 6 | `docs/reference/configuration.md:69, 115, 123`, `configure-scan-profiles.md:59, 145`, `saneless.toml.example:21` | `title` profile key sets a default title | Nothing reads it (M-24) |
| 7 | `docs/how-to/configure-scan-profiles.md:128` | Auto-profiles creates `flatbed-color-300`, `adf-gray-150` | Actual slugs are `flatbed-scan`, `adf-simplex`, `adf-duplex`, `auto-scan`, `default` |
| 8 | `docs/how-to/configure-scan-profiles.md:128-131` | `--force` overwrites only auto-generated profiles | It overwrites any same-named profile and drops user keys (M-09) |
| 9 | `docs/how-to/configure-scan-profiles.md:115-116` | "Otherwise, it crops the image after scanning" | The crop fallback is unreachable on devices without geometry options (M-15) |
| 10 | `docs/how-to/configure-scan-profiles.md:164-172` | "lower the thresholds to detect faint pages as non-empty" (example mean 240) | Lowering the mean threshold makes more pages count as empty; `empty-page-detection.md:49-51` has it right |
| 11 | `docs/explanation/empty-page-detection.md:54` | Use `--log-level DEBUG` | No such option; `-v` does not enable DEBUG either (M-21) |
| 12 | `docs/explanation/empty-page-detection.md:56-68` | `enable_empty_page_detection = false` keeps all pages | The backend drops clean blank pages before the toggle applies (M-14) |
| 13 | `docs/reference/cli-commands.md:12` | `-v` "enables debug output" | Mirrors the configured level to stderr only (M-21) |
| 14 | `docs/how-to/cli-scripting.md:62` | Exit 2 for a missing config file | Missing file silently ignored (M-19) |
| 15 | `docs/how-to/install-bare-metal.md:47` | Run `saneless --version` | No `version_option`; exits 2 with "No such option" |
| 16 | `README.md:44` | `saneless scan  # Scan a document` | `--title` is required |
| 17 | `README.md:63` | `source = "flatbed"` | Compared case-sensitively; every SANE backend spells it `"Flatbed"` |
| 18 | `README.md:74` | Link to `tutorials/scan-your-first-document/` | No such page; the tutorial is `getting-started/first-cli-scan/` |
| 19 | `README.md:37, 72-77` and eight other files (24 lines) | `ghcr.io/kris-knigga/saneless`, `github.com/kris-knigga/saneless`, `kris-knigga.github.io/saneless` | Remote is `kdknigga/saneless`; all URLs 404. Decision: update every reference to the `kdknigga/saneless` forms (M-27 has the full list) |
| 20 | `docs/how-to/deploy-docker-compose.md:27-28` | Container "will fail to start if the file is missing" | Starts with defaults; Docker creates a directory at that path (M-30) |
| 21 | `docs/reference/docker.md:42`, `saneless.toml.example:11` | `web_port` is a common override for Docker; example sets 8081 | Healthcheck and EXPOSE are fixed at 8080 (M-29) |
| 22 | `docs/reference/web-api.md:124-136` | Flip endpoints return "HTML partial with updated job status" | Continue returns the unchanged flip prompt; abort may return "Ready to scan." (M-02) |
| 23 | `docs/reference/web-api.md:106-112` | `/api/scan` "returns immediately after queuing" | Not when the queue is full (C-09) |
| 24 | `docs/getting-started/first-web-ui-scan.md:29, 38` | Click **Cancel**; "place the pages back face-up" | Button is "Abort scan" (a test asserts "Cancel" is absent); the on-screen prompt says to flip the stack, contradicting the doc |
| 25 | `docs/explanation/architecture.md:43` | Fallback triggers on "network error, timeout, auth failure" | Auth failures (4xx) raise immediately without fallback; `consume-directory-fallback.md:54` has it right |
| 26 | `docs/explanation/architecture.md` | HTMX design "keeps the web layer fully responsive" | Every route blocks the event loop (M-01) |
| 27 | `docs/reference/configuration.md:11, 44` | "XDG config directory", "XDG state directory" | `~/.config` and `~/.local/state` are hard-coded; `$XDG_*` is ignored (M-20) |
| 28 | `docs/explanation/consume-directory-fallback.md:11` | "up to 3 attempts by default" | Not configurable; the count is fixed |
| 29 | `docs/getting-started/first-cli-scan.md:9, 67` | USB scanners must be connected to the `saned` host | Bare-metal libsane enumerates local USB directly; two other pages say the opposite |
| 30 | `docs/how-to/cli-scripting.md:45-50` | Job JSON shows `"id": "a1b2c3d4"`, `created_at` without offset | Ids are UUID4; timestamps carry `+00:00` (cosmetic) |
| 31 | `docs/getting-started/quick-start.md:18-21` | `docker run -v ./config.toml:...` | Docker Engine's `-v` historically rejects relative host paths; use `$(pwd)`. *Likely* |
| 32 | `docs/how-to/deploy-docker-compose.md:34-45` | A Paperless service with only `PAPERLESS_SECRET_KEY` | Paperless requires a Redis broker; the example stack will not come up. *Likely* |
| 33 | `src/saneless/job.py:1-7` (docstring) | "SQLite-backed persistence for crash recovery" | No recovery code exists (M-03) |
| 34 | `docs/PRD.md` | Device picker in the UI, Test Connection button, mismatches fail the job, 0 to 1 thresholds, `/api/jobs/{id}` | None match the shipped code; the PRD was never updated after v1.0 (N-29) |

### Claims that were checked and are correct

So the reader can trust them: every `[output]` and `[profiles]` default in `configuration.md` against `config.py`; the config search order; the env prefix and `__` delimiter; env-over-TOML-over-defaults precedence; the requirement for a `default` profile; the `auto_source_mode` and `paper_size` literals; all eleven routes and methods in `web-api.md`; the `/health` 200 and 503 bodies; the `/api/paperless/test` status values; the `/api/scan` form fields; `jobs --limit` default 20; `serve` defaults of `0.0.0.0:8080` and the no-auth note; `scan` exit codes 1, 2, and 3 for the cases that are caught; three upload attempts with `2**attempt` backoff; the `SANE_NET_HOSTS` precedence rule; PNG-then-img2pdf lossless assembly; and the dual-threshold empty-page rule in `empty-page-detection.md`.

**How to keep it this way.** Every documented claim should be pinned by a test or generated from the code. Cheap wins: add `@click.version_option(package_name="saneless")` so row 15 becomes true; a CI step that runs the README commands; and a habit of quoting UI copy from the templates rather than paraphrasing it.
## 9. Test suite assessment

**Numbers.** 340 tests, all passing; the 332 non-browser tests take about 27 seconds. About 6,050 lines of tests to about 4,050 lines of source. Line coverage 94 percent (1,354 statements, 83 missed). Ruff, ruff format, ty, and pyrefly all clean.

**Shape.** The suite has a good skeleton. The HTTP client is tested through a real transport. The web app is tested through a real `TestClient` with lifespan and a typed stub scanner. The worker is tested as a real thread with real events. The CLI tests stub only the hardware and network edges and let the real pipeline, PDF assembly, and empty-page filter run. The scanner tests model the SANE option-tuple protocol. Warnings are errors, and nothing in `tests/` suppresses them. Test names mostly state a behaviour and an outcome.

**Where it is thin, in order of importance.**

1. **The integration seams are untested.** Every worker test monkeypatches `run_pipeline` away, so the worker-to-pipeline callback contract is only ever exercised against a double written in the same test file. No test drives worker, pipeline, stub scanner, and mock transport together for any outcome. No browser test submits a scan. No test uses the job store from two threads. This is where C-01 through C-10 live (M-33).
2. **The fakes are more convenient than the truth.** The `MagicMock(spec=ScannerBackend)` accepts any source; the SANE fakes raise where the real library succeeds, return `iter(list)` where the real iterator calls `start()` per page, and never raise `_sane.error` (M-32).
3. **The suite is not hermetic.** A fixed shared `/tmp` path, tests that read the developer's private `saneless.toml`, a fixed port, chmod tests that fail as root (M-34).
4. **About 24 of the 27 seconds are `time.sleep`.** 26 fixed sleeps in the worker tests are unnecessary because `stop()` already synchronises; the Paperless retry tests run the production backoff unmocked (N-18, N-24).
5. **Around 35 tests add little.** Five have no assertion; about 25 pin literal constants or pydantic defaults; two test the test double; five pairs are duplicates; several assert only `status_code == 200` on a state-changing endpoint (N-40).
6. **Over-reach into private names.** 70 accesses of underscore attributes from tests, mostly `_current_job_id` and `_mock_dev`; one private helper is exported from `cli.__all__` only so a test can import it.

**The ten most important tests to add**, each of which would have caught a finding in this report:

| Test | Catches |
|---|---|
| Worker + real pipeline + stub scanner + `MockTransport`, parametrised over SUCCESS, FAILURE, TIMEOUT, fallback, duplex mismatch; assert the persisted job state | C-03 |
| Pipeline with a manual-duplex profile against the fake SANE module from `test_scanner.py` | C-01 |
| `CliRunner` manual-duplex scan with `input="y\n"`; assert the prompt appears between passes | C-02 |
| Upload raises after five pages scanned; assert a PDF exists under `failed/` and the message names it | C-04 |
| Two consume-directory fallbacks; assert two files survive | C-05 |
| Parametrised over "ADF", "Automatic Document Feeder", "ADF Front"; assert `multi_scan` is used | C-06 |
| `JobStore` hammered from two threads for 200 rounds; assert no exception | C-07 |
| `write_profiles_to_config` for sources `["ADF", "ADF Duplex"]`, then `load_settings` on the result | C-08 |
| `prune` raises; assert the worker is still alive and the next job completes; ten queued jobs, eleventh gets 429 | C-09 |
| Browser: click Scan, wait for `.status-done`, assert `#scan-btn` is enabled | C-10 |

The verification scripts written during this review (under the session scratchpad, listed in Appendix A) are working starting points for all ten.

## 10. Recommended remediation order

The order below groups fixes so that each step leaves the suite green and each step's tests protect the next. Estimated sizes are for a developer who knows the codebase.

**Step 1: Make outcomes honest (C-03, C-04, C-05, M-22; about a day).** Raise on Paperless FAILURE and TIMEOUT; replace the `"fallback"` sentinel with a typed result; add `JobState.FALLBACK` and a warning field; preserve the PDF on upload failure; give PDFs unique names and make the fallback copy atomic; fix `poll_task`'s clock and status handling. Add the end-to-end worker test parametrised over outcomes. This step alone removes the "silently wrong result" class.

**Step 2: Make manual duplex real (C-01, C-02, M-07, M-02, N-07; about a day).** Add a `duplex` profile field and stop overloading `source`; give the CLI a flip prompt and make the pipeline refuse to run manual duplex without one; add a flip timeout; map `SCANNING_REVERSE` to a visible state and drop `wait_transition`; collapse the duplicated detection rule. Add the pipeline-against-fake-SANE test and the CLI prompt test. Update the how-to.

**Step 3: Make the scanner layer truthful (C-06, M-11, M-14, M-15, M-16, M-32; one to two days).** One `classify_source` used everywhere; delete the first-page-equals-feeder-empty special case and wrap SANE errors as `ScanError` with the message; remove the white-page policy from the backend; check option presence before assigning geometry and read resolution back; set options in source-first order. Rewrite the fakes from `sane.py` and add the opt-in `test` backend integration module.

**Step 4: Make the worker and web layer robust (C-07, C-09, C-10, M-01, M-03, M-05; about a day).** Lock the job store; guard the worker loop and use `put_nowait` with 429; change routes from `async def` to `def`; reconcile active jobs at startup and fix shutdown ordering; let the server own the button with `hx-swap-oob` and delete the JavaScript; unify the state enums and label maps. Add the two-thread store test, the worker-survives-exception test, and the browser scan-cycle test.

**Step 5: Make configuration strict (C-08, M-04, M-09, M-10, M-18, M-19, M-20, M-21, M-24, N-15; about a day).** `extra="forbid"` on nested models with a nested-key error message; refuse a missing `--config`; expand `~` and honour XDG; validate `log_level` and make `-v` mean DEBUG; record the loaded config path and use it from the worker; always emit a default profile; merge under `--force`; write atomically; implement or delete `title`; use `SecretStr`.

**Step 6: Translate exceptions at every boundary (M-17, N-06, N-08; half a day).** Wrap SANE, httpx, and img2pdf errors; add `ConfigError` handling and a python-sane import guard to the CLI; log with `exc_info` in the worker; make `load_settings` raise one type.

**Step 7: Stand up delivery (M-25 through M-31; half a day).** A CI workflow that runs ruff, ty, pyrefly, and pytest on every push; fix the release workflow's three failures; update the 24 lines in nine files that name `kris-knigga/saneless` to `kdknigga/saneless` (M-27 lists them) and add the grep as a CI check; add `-v` to the container command or a stderr handler for containers; align the example config port with the healthcheck; mount the config directory rather than the file; add `.dockerignore`.

**Step 8: Fix the geometry, memory, and timeouts (M-06, M-08, M-12, M-13; one to two days).** Pass DPI through to the PDF; spool pages to disk as they arrive; wait for the cancelled read before closing; give the flatbed path the same timeout.

**Step 9: Clean the suite (M-33, M-34, N-18, N-24, N-40; about a day).** Hermetic fixtures; remove the sleeps; delete the vacuous and duplicate tests; add the remaining negative-path tests.

**Step 10: Everything else in section 5**, as the files are touched. The planning-reference comments (N-34) and the suppression rule (N-35) are good candidates for a single sweep.

**Step 11: Make it an appliance (section 11; two to three days).** Once steps 1 through 4 have made the pipeline honest, build the operator-facing layer on top: a status strip and `saneless doctor` (U-03); page counts and plain-language outcomes with technical detail behind a disclosure (U-02, U-05); profiles generated at startup with human labels and descriptions (U-04); the compose template with one place for the token, a placeholder check at startup, and the consume mount included (U-01, U-08); queue visibility and an owner-only flip prompt (U-06); help text and a thumb-friendly tag picker (U-07); one-sentence trust-model and "which setup do I have" pages in the docs (U-09, U-10). Several of these are small once the data exists: U-02 is two columns and two template lines after C-03 adds the result record.

## 11. Usability review: the appliance test

The sections above review the code as code. This section reviews it from the chair of the person the project is for: a home operator who is not a developer, spinning up the container so their family can scan things. The goal stated for the project is that it should feel like an appliance. An appliance discovers its surroundings, tells you plainly when something is wrong and what to do about it, never loses your work, survives being unplugged, and never shows you its internals unless you ask. Each finding below is a place where the current build falls short of that bar. Findings carry `U-` IDs; several are new to this pass, and the rest give an operator-facing framing to code findings above and say what the appliance-grade fix looks like.

### The journey, and where it breaks

**Day zero: getting it running.**

- The install commands do not work yet, and they name the wrong repository (M-27, resolved below with the decision).
- The quick start promises five minutes but its first prerequisite is a running `saned` on the machine with the scanner. For a home user that is a bigger project than saneless itself, and the docs disagree with each other about whether a USB scanner on the Docker host needs `saned` at all (U-10).
- The compose template quietly overrides the config file it tells you to create (U-01).
- Config mistakes are silent: a misspelled key inside a section is ignored (M-18), a missing config file is ignored and Docker creates a directory in its place (M-30), and the example config's port does not match the healthcheck so the container reports unhealthy forever (M-29).
- There is no way to check the setup from the UI, and when it fails, `docker logs` is empty (U-03, M-28).

**Day one: the first scan.**

- The profile dropdown shows raw slugs, and the useful profiles only appear after the first scan plus a page reload, if they appear at all (U-04).
- The form uses Paperless jargon with no help text, and the tag box is hard to use on a phone (U-07).
- The Scan button never comes back (C-10). Every family member's first experience ends with a page reload and "I think it's broken".
- Nothing says how many pages were captured, which is what makes C-06 invisible: a ten-page stack yields a one-page PDF and a green tick (U-02).
- Errors are raw exception text with no next step (U-05).

**Daily use by the family.**

- If Paperless is down, scans vanish, because the recommended compose deployment does not mount the consume directory that the fallback needs (U-08, C-04).
- One shared queue with no ownership: the flip prompt for one person's job is rendered for everyone, with live Continue and Abort buttons (U-06).
- Times are UTC (M-23). A container update mid-scan leaves "Scanning..." on screen until the operator deletes the database (M-03).
- The UI has no login and binds every interface, which is fine for a home LAN but is never said in plain words (U-09).

### Findings

#### U-01 — The compose template's placeholder token silently overrides the token in `config.toml`
- **Severity:** MAJOR · **Confidence:** Confirmed (lead read the settings source order) · **New in this pass**
- **Location:** `docker-compose.yml:16-23`, `src/saneless/config.py:137-142`

**What.** The compose file tells the operator to create `config.toml` "with at minimum `[profiles.default]` and `[paperless]` sections", mounts it read-only, and also ships `SANELESS_PAPERLESS__TOKEN=changeme` and `SANELESS_PAPERLESS__URL=http://paperless:8000` as environment variables. pydantic-settings resolves sources in the order the project returns them, `(init, env, toml)`, so environment beats file. An operator who puts the real token and URL in the file, as instructed, and leaves the placeholders in compose, as shipped, runs with token `changeme` and a URL that only resolves inside a compose network that also contains Paperless.

**Why it matters.** The symptom is a 401 on every upload, surfaced as "Upload failed after 3 retries" with no cause, after the scan has already happened. Nothing at startup says "your token is the placeholder". The operator has two places to put one secret and no indication which one wins. For an appliance, a secret lives in exactly one place, and a placeholder value is refused at startup.

**How to fix.** Ship the compose file with the environment block commented out and a one-line comment stating that environment variables override the file. Add a startup check: if `paperless.token` is empty or equals a known placeholder, log a clear error and show it on the status strip (U-03). Print, at startup and at INFO, which config file was loaded and which keys came from the environment.

#### U-02 — The UI never says how many pages were scanned or how many blanks were removed
- **Severity:** MAJOR · **Confidence:** Confirmed · **New in this pass**
- **Location:** `src/saneless/web/templates/partials/status.html:19-20`, `partials/history.html`, `src/saneless/job.py` (no page-count columns), `src/saneless/pipeline.py:396-413`

**What.** The DONE state renders "Done: title" and a thumbnail of the first page. The page count, the number of pages removed as blank, and the number uploaded are known to the pipeline and discarded. The history table shows time, profile, title, and state.

**Why it matters.** This is the difference between a defect the family notices and one they do not. C-06 uploads one page of a ten-page stack with a green tick; M-14 drops blank Lineart pages before the user's toggle applies; the duplex mismatch recovery uploads two half documents. In every case, "Scanned 10 pages, removed 2 blank, uploaded 8" would have made the problem visible on the spot, and "Scanned 1 page" would have made the user re-feed the stack instead of discovering the loss weeks later in Paperless. An appliance reports what it did.

**How to fix.** Add `pages_scanned` and `pages_uploaded` to the job record (and a `warning` text column, which C-03 also needs), set them from the pipeline result, and render them in the status partial and the history table. For manual duplex, show "fronts 10, backs 10" during pass B.

#### U-03 — There is no setup or status view: nothing tells the operator whether the scanner was found, whether Paperless accepts the token, or which profiles exist
- **Severity:** MAJOR · **Confidence:** Confirmed · **New in this pass**
- **Location:** `src/saneless/web/templates/index.html`, `src/saneless/web/routes.py:119-135` (`/api/paperless/test` exists; no template calls it), `src/saneless/cli.py`

**What.** The Paperless connection test route exists and distinguishes connected, token rejected, and unreachable, but no button or indicator in the UI uses it. There is no scanner status at all: the first sign that the scanner host is wrong is a failed scan. The CLI has `devices` but no single "check everything" command. The `docs/PRD.md` planned a Test Connection button; it was never built (N-29).

**Why it matters.** Every setup problem in this section (wrong host, placeholder token, wrong port, read-only config, no profiles) currently surfaces as a failed scan, after the family has already tried to use it. With `docker logs` empty (M-28), the operator has no diagnostic path short of reading the source. An appliance shows a green or red light for each thing it depends on.

**How to fix.** Add a status strip at the top of the index page, refreshed on load and by a button: Scanner (found "EPSON XP-7100" on host X, or "not found: check SANELESS_SCANNER__HOST"), Paperless (connected, token rejected, unreachable, with the URL), Profiles (N configured, or "using bare default; run auto-profiles"), and Fallback (consume directory mounted or not). Back it with a `saneless doctor` CLI command that prints the same checks and exits non-zero on any red, so a compose healthcheck or the docs can use it. Make the status strip the first thing the quick start tells the operator to look at.

#### U-04 — Profiles are shown as raw slugs with no description, and they appear only after the first scan and a page reload, if at all
- **Severity:** MAJOR · **Confidence:** Confirmed · **New framing of M-04 and M-30**
- **Location:** `src/saneless/web/templates/index.html:11-16`, `src/saneless/auto_profiles.py:32-60`, `src/saneless/worker.py:159-191`, `src/saneless/config.py:63-79`

**What.** The dropdown renders the profile key as both value and label: `default`, `adf-simplex`, `adf-duplex`, `flatbed-scan`, `auto-scan`. There is no description field on `ProfileConfig`. On a fresh install the list contains only `default`; the scanner-derived profiles are generated lazily inside the first job, so they appear only after that job completes and the page is reloaded, and under the recommended read-only compose mount the write fails and they are never persisted (M-30), so the dropdown is wrong again after every restart.

**Why it matters.** "adf-simplex" means nothing to a family member; "default" does not say whether it is the glass or the feeder. And the first person to scan sees a different form than the second, with no explanation. An appliance labels its buttons in the user's language and is fully configured before anyone touches it.

**How to fix.** Add `label` and `description` fields to `ProfileConfig` and have auto-generation fill them from the source: "Feeder, single-sided", "Feeder, both sides", "Glass (flatbed)". Render `label` in the dropdown with `description` as help text, and pick a sensible default order (feeder first for sheet-fed scanners). Generate profiles at startup, not in the first job (M-04), and when the config is read-only keep them in memory and say so on the status strip (U-03) rather than logging "scanner unreachable".

#### U-05 — Errors shown to family members are raw exception text with no plain-language explanation and no next step
- **Severity:** MAJOR · **Confidence:** Confirmed · **New framing of M-11, M-17, N-14**
- **Location:** `src/saneless/web/templates/partials/status.html:23` (`Error: {{ job.error }}`), `src/saneless/worker.py:254-262`, `src/saneless/job.py:36-43` (`ErrorCategory` exists and is never rendered)

**What.** The error partial prints `str(exc)` verbatim. Examples a family member can currently see: `Device does not support source 'Manual Duplex'. Available: ['Flatbed', 'ADF']`; `[Errno 111] Connection refused`; `Upload failed after 3 retries`; several kilobytes of HTML from a 4xx response (M-17); and `No paper detected in feeder` when the real problem is a jam, an open lid, or a busy device (M-11). The `ErrorCategory` that could drive a friendlier message is stored and never read (N-14).

**Why it matters.** The person at the scanner needs to know one thing: is this something I can fix right now, or do I tell the operator? "Connection refused" answers neither. An appliance says "Paperless is not responding; your scan has been kept and will need to be uploaded later" or "Check that the scanner lid is closed and the paper is loaded, then try again", and puts the technical detail behind a disclosure for the operator.

**How to fix.** Map each `ErrorCategory` (and the new outcomes from C-03) to a short user message and a suggested action, render that in the status partial, and put the raw text inside a collapsed `<details>` element labelled "Technical details". Fix the misclassification in M-11 first, or the friendly message will be confidently wrong. Log the raw exception with `exc_info` so the operator can find it.

#### U-06 — One shared queue with no ownership: one person's flip prompt is rendered for everyone, with live Continue and Abort buttons, and a queued job shows "Starting scan..." with no explanation
- **Severity:** MINOR · **Confidence:** Confirmed · **New in this pass**
- **Location:** `src/saneless/web/routes.py:81-86` and `:187-192` (current-or-most-recent job), `partials/status.html`, `partials/flip.html:43-45`, `src/saneless/worker.py:61`

**What.** Every browser renders the same status area for the current job. When one family member's manual-duplex scan reaches the flip prompt, every other open page also shows the prompt and the Continue and Abort buttons, and any of them can press either. When a second person submits while a job is running, their job sits in the queue and the status area shows the other person's job; the new job appears only as "Starting scan..." once it becomes current, with no indication that it waited.

**Why it matters.** In a household this is a real footgun: a child who sees "Abort scan" on a tablet can cancel a parent's fifty-page job, and the parent's fronts are lost (C-04). And "Starting scan..." for a job that is actually waiting behind another looks like a hang. An appliance shows whose turn it is.

**How to fix.** Show queued jobs explicitly: "Waiting for 'Tax return' to finish (1 ahead of you)". Render the flip prompt only for the page that submitted the job, identified by a token stored in the browser at submit time, and show other viewers "Waiting for the stack to be flipped". Confirm before Abort. Once C-09 adds a queue limit, show the position rather than a 429.

#### U-07 — The form speaks Paperless jargon with no help, and the multi-select tag box is hard to use on a phone
- **Severity:** MINOR · **Confidence:** Confirmed · **New in this pass**
- **Location:** `src/saneless/web/templates/index.html:18-56`

**What.** The labels are Profile, Title, Tags, and Correspondent, with no help text. "Correspondent" is Paperless's term for the sender of a document; a family member has never seen it. Tags are a native `<select multiple>`, which on mobile browsers is a modal list that requires knowing about multi-select gestures; the UI review screenshots show it is small on a phone. The refresh buttons are icon-only with a tooltip.

**Why it matters.** The family will not tag or attribute anything, which pushes all the metadata work to whoever curates Paperless. That is acceptable if it is a choice; today it is a consequence of the form. An appliance uses words its users already know and controls that work with a thumb.

**How to fix.** Add one line of help under each control ("Who sent this document? Optional."). Replace the multi-select with a checkbox list or a searchable picker; both work with htmx and no build step. Let the operator hide Tags and Correspondent entirely for a simpler family form, with per-profile defaults doing the work (the `default_tags` and `default_correspondent` fields already exist; M-24 is the missing `title` piece).

#### U-08 — The recommended deployment has the "documents are not lost" fallback switched off
- **Severity:** MINOR (MAJOR once C-04 is fixed, because the preserved-PDF directory needs a volume too) · **Confidence:** Confirmed · **New in this pass**
- **Location:** `docker-compose.yml:15-20`, `docs/how-to/deploy-docker-compose.md`, `src/saneless/paperless.py:146-155`

**What.** The consume-directory fallback only runs when `paperless.consume_dir` is set to a directory the container can write and Paperless can read. The compose example mentions this in a comment and mounts nothing; the how-to calls it optional. So in the deployment the docs recommend, three failed retries mean the scan is deleted (C-04).

**Why it matters.** Paperless being down is the one failure a home operator will definitely experience (updates, reboots, a full disk). Whether the family's scans survive it currently depends on an optional step that is easy to skip. An appliance keeps your work by default.

**How to fix.** Put a commented consume mount in the compose example with a two-line explanation of what it buys, and have the status strip (U-03) say "Fallback: not configured; scans cannot be kept if Paperless is down". When C-04 adds the `failed/` directory, make sure it lives on the `saneless-data` volume and is mentioned in the same place.

#### U-09 — Nobody is told that the UI has no login and listens on every interface
- **Severity:** MINOR · **Confidence:** Confirmed · **Operator framing of N-22**
- **Location:** `src/saneless/config.py:95`, `docs/reference/web-api.md:143` (the only mention), `docs/getting-started/*.md` (no mention)

**What.** The API reference says there is no authentication and suggests a reverse proxy. None of the getting-started or Docker pages say it, and the default bind is all interfaces.

**Why it matters.** For a family on a home network the absence of a login is probably the right default. The problem is that it is a decision the operator does not know they are making. An appliance states its trust model in one sentence where the operator will read it.

**How to fix.** One admonition in the quick start and the compose guide: "Anyone on your network can open this page and start a scan. There is no login. Keep it on your home network or put it behind a reverse proxy with authentication." Consider the `Sec-Fetch-Site` check from N-22 so a web page elsewhere cannot trigger scans through the family's browsers.

#### U-10 — The scanner-connection story is the hardest part of setup and the docs contradict themselves about it
- **Severity:** MINOR · **Confidence:** Confirmed · **New framing of doc rows 29 and 31**
- **Location:** `docs/getting-started/quick-start.md:5-9`, `docs/getting-started/first-cli-scan.md:9, 67`, `docs/how-to/install-bare-metal.md:9`, `docs/reference/configuration.md:24`, `docs/reference/docker.md:60`

**What.** The quick start's first prerequisite is `saned` running on the scanner's machine. Two other pages say a bare-metal install talks to USB directly. The Docker reference shows `/dev/bus/usb` passthrough but describes it as "managed by a local `saned`", which is not how passthrough works. There is no page that asks the operator the one question that decides everything: where is your scanner, and how does it connect?

**Why it matters.** A home operator has one of three setups: a USB scanner plugged into the Docker host, a scanner with its own network interface, or a scanner on another computer. Each needs a different two-line configuration, and today the operator has to infer which from pages that disagree. This is where the "five minutes" claim actually breaks.

**How to fix.** Add a "Which setup do I have?" page with three columns and the exact compose lines for each (USB on the host: `devices: [/dev/bus/usb]` and the group id; network scanner: `SANELESS_SCANNER__HOST`; `saned` elsewhere: the same variable plus a pointer to a `saned` setup guide). Link it from the quick start's prerequisites and make the status strip (U-03) confirm the outcome.

### What "appliance" would look like, in priority order

1. **A status strip and a `doctor` command** (U-03), so every setup mistake shows up before the first scan, with logs on stdout (M-28).
2. **Honest outcomes** (C-03, U-02, U-05): page counts, plain-language results with a next step, technical detail behind a disclosure.
3. **Never lose a scan** (C-04, C-05, U-08): preserve on failure, unique names, fallback mounted by default.
4. **Configured before first use** (U-04, M-04, U-01): profiles generated at startup with human labels, one place for the token, placeholder values refused.
5. **Survives restarts and outages** (M-03, C-07, C-09, M-01): stuck jobs cleared on start, worker that cannot die, UI that cannot freeze.
6. **Speaks the family's language** (U-07, M-23): help text, thumb-friendly controls, local time, a stated trust model (U-09).
7. **A setup page that asks the one question that matters** (U-10).

The ten most user-visible code defects (C-01 through C-10) are prerequisites for all of this; an appliance shell over a pipeline that reports failure as success would make things worse, not better.

## Appendix A. Methodology and verification

**Process.** Six review agents ran in parallel, each assigned a slice of the codebase and instructed to read every line of its files, apply all Google checklist dimensions, verify runtime claims by executing throwaway scripts outside the repository, and mark each finding Confirmed or Likely. Slices: scanner backend (`SCAN`), pipeline and auto-profiles (`PIPE`), Paperless client, job store, config, and logging (`CORE`), worker and web (`WEB`), CLI, packaging, deployment, and docs (`OPS`), and a cross-cutting pass over architecture, consistency, comments, and whole-suite test quality with a coverage run (`XC`). The lead reviewer then read all six reports, merged duplicates, re-verified every CRITICAL finding and the highest-impact MAJOR findings by reading the cited code and re-executing the agents' scripts, and wrote this document. The repository was not modified at any point; `git status` was clean throughout. After the code pass, the lead re-read the quick start, compose file, example config, and web templates from a non-developer operator's perspective and wrote section 11; the project owner supplied the repository-naming decision recorded in M-27.

**Baseline at commit e905f64.** `uv run ruff check .`: no issues. `uv run ruff format --check .`: 37 files already formatted. `uv run ty check`: all checks passed. `uv run pyrefly check`: 0 errors. `uv run pytest`: 340 passed, including browser tests.

**Lead re-verification, in addition to reading the cited lines.**
- Loaded a config with `hostname` under `[scanner]` and `resoluton = 600` under `[profiles.default]`: no error, resolution 300 (M-18). Confirmed the token appears in `repr(Settings)` and is not currently logged (N-15).
- Assembled a 2480 by 3508 image and read the PDF's MediaBox: `0 0 1860 2631` (M-06).
- Ran the two-thread `JobStore` stress script: 10 of 10 runs raised for both `:memory:` and file-backed databases (C-07).
- Ran `generate_profiles` with sources `["ADF", "ADF Duplex"]`: no `default` profile (C-08).
- Ran the Playwright scan-cycle test against a real uvicorn server: button still disabled after `.status-done` appeared, with the swap event's detail target disconnected (C-10).
- Read the installed `sane.py` `__setattr__`: unknown option names are stored silently (M-15).
- Ran `git remote -v` and `git ls-remote` against the PyPI publish action: remote is `kdknigga/saneless`; no `v1.12` ref exists (M-26, M-27).

**Agent verification artefacts** live in the session scratchpad at `/tmp/claude-1000/-home-kris-git-saneless/8fb87bbe-41f7-42a6-84dd-3ff208355f8f/scratchpad/` (session-specific; copy anything worth keeping) and are reusable as regression tests: `pipe/test_pipe_claims.py` (10 tests), `pipe/test_autoprof_claims.py` (5), `pipe/test_manual_source_claim.py` (2), `pipe/verify_pdf.py`, `core/threads_jobstore.py` and `threads_jobstore_locked.py`, `core/verify_config.py`, `core/verify_paperless.py`, `core/test_scratch_worker.py`, `core/test_scratch_core.py`, `exp1_caps_and_adf.py` through `exp9_exit_join.py` (real SANE `test` backend), `test_scratch_init.py`, `test_scratch_browser_button.py`, `test_scratch_web.py`, `test_scratch_worker2.py`, `test_ops_verify.py`, `xc_verify_test.py` (8 tests), `test_hygiene.py`, and `deadcode.py`. The SANE experiments use `SANE_CONFIG_DIR` pointing at a directory whose `dll.conf` contains only `test`, which works on this machine because `libsane-test.so` is installed.

**Limits of this review.** No real scanner hardware was used; scanner findings were verified against the SANE `test` backend and by reading python-sane's source, which is why a few are marked Likely. No live Paperless instance was used; the client was verified with mock transports. The Docker image was not built and run. Findings about third-party behaviour (htmx, python-sane, Docker's `-v` parsing, Paperless's Redis requirement) were checked against the installed or fetched sources where possible and are marked Likely where they were not.

## Appendix B. Reviewer finding IDs

For traceability, every per-reviewer ID and the consolidated finding it was merged into.

| Reviewer ID | Consolidated | Reviewer ID | Consolidated | Reviewer ID | Consolidated |
|---|---|---|---|---|---|
| SCAN-01 | C-06 | PIPE-01 | C-01 | CORE-01 | C-07 |
| SCAN-02 | M-11 | PIPE-02 | C-02 | CORE-02 | C-03 |
| SCAN-03 | M-12 | PIPE-03 | C-04 | CORE-03 | C-05 |
| SCAN-04 | M-14 | PIPE-04 | C-03 | CORE-04 | M-18 |
| SCAN-05 | M-15 | PIPE-05 | C-05 | CORE-05 | M-19 |
| SCAN-06 | M-16 | PIPE-06 | C-08 | CORE-06 | M-17 |
| SCAN-07 | M-13 | PIPE-07 | M-06 | CORE-07 | M-20 |
| SCAN-08 | M-08 | PIPE-08 | M-07 | CORE-08 | M-21 |
| SCAN-09 | M-32 | PIPE-09 | M-08 | CORE-09 | M-03 |
| SCAN-10 | N-01 | PIPE-10 | M-09 | CORE-10 | M-22 |
| SCAN-11 | M-17 | PIPE-11 | M-04 | CORE-11 | M-33 |
| SCAN-12 | N-02 | PIPE-12 | M-10 | CORE-12 | N-12 |
| SCAN-13 | M-16 | PIPE-13 | M-33 | CORE-13 | N-13 |
| SCAN-14 | N-03 | PIPE-14 | N-06 | CORE-14 | M-17 |
| SCAN-15 | N-04 | PIPE-15 | M-17 | CORE-15 | N-14 |
| SCAN-16 | N-34 | PIPE-16 | N-07 | CORE-16 | N-15 |
| SCAN-17 | N-45 | PIPE-17 | M-02 | CORE-17 | N-16 |
| SCAN-18 | N-10 | PIPE-18 | M-23 | CORE-18 | section 8 |
| SCAN-19 | N-05 | PIPE-19 | N-08 | CORE-19 | N-17 |
| SCAN-20 | N-37 | PIPE-20 | N-09 | CORE-20 | N-18 |
| SCAN-21 | N-36 | PIPE-21 | section 8 | CORE-21 | N-45 |
| SCAN-22 | N-35 | PIPE-22 | N-10 | CORE-22 | N-19, N-35 |
| | | PIPE-23 | N-11 | | |
| WEB-01 | C-10 | OPS-01 | C-02 | XC-01 | C-03 |
| WEB-02 | M-01 | OPS-02 | M-25 | XC-02 | C-01 |
| WEB-03 | C-09 | OPS-03 | M-26 | XC-03 | C-02, M-17 |
| WEB-04 | C-03 | OPS-04 | M-27 | XC-04 | M-04 |
| WEB-05 | M-02 | OPS-05 | M-28 | XC-05 | M-01 |
| WEB-06 | M-03 | OPS-06 | M-29 | XC-06 | C-06 |
| WEB-07 | C-09 | OPS-07 | M-30 | XC-07 | M-17 |
| WEB-08 | M-04 | OPS-08 | M-19 | XC-08 | M-05 |
| WEB-09 | N-20 | OPS-09 | M-17 | XC-09 | M-23 |
| WEB-10 | M-05 | OPS-10 | M-24 | XC-10 | M-34 |
| WEB-11 | M-23 | OPS-11 | C-03 | XC-11 | M-24 |
| WEB-12 | N-21 | OPS-12 | M-31 | XC-12 | N-34 |
| WEB-13 | N-22 | OPS-13 | M-34 | XC-13 | N-35 |
| WEB-14 | N-23 | OPS-14 | M-21 | XC-14 | N-36 |
| WEB-15 | section 8 | OPS-15 | N-25 | XC-15 | N-37 |
| WEB-16 | M-33 | OPS-16 | N-26 | XC-16 | N-38 |
| WEB-17 | N-24 | OPS-17 | N-27 | XC-17 | N-39 |
| WEB-18 | N-44 | OPS-18 | N-28 | XC-18 | M-33 |
| | | OPS-19 | section 8 | XC-19 | N-40, M-32 |
| | | OPS-20 | N-29 | XC-20 | N-41 |
| | | OPS-21 | N-29 | XC-21 | N-42 |
| | | OPS-22 | N-30 | XC-22 | N-10 |
| | | OPS-23 | N-31 | XC-23 | N-43 |
| | | OPS-24 | N-32 | XC-24 | N-44 |
| | | OPS-25 | M-33, N-33 | | |
| | | OPS-26 | M-09 | | |
| | | OPS-27 | N-44 | | |
