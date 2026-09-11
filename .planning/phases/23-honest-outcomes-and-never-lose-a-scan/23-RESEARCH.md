# Phase 23: Honest Outcomes and Never Lose a Scan - Research

**Researched:** 2026-09-11
**Domain:** Python service reliability — typed outcome persistence, crash-safe file preservation, PDF page geometry, HTTP polling contracts
**Confidence:** HIGH (every mechanical claim below was executed against this repo's own `uv` environment; the paperless-ngx API claims were read from upstream source, not from training data)

---

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions

All sixteen decisions are **locked for planning**. A planner may implement them, not relitigate
them. Reproduced in condensed form; `23-CONTEXT.md` is authoritative for the full rationale.

**Outcome vocabulary**

- **D-01: `ScanOutcome` gains no `FAILED` member. Failures raise.** `outcome` is
  `SUCCESS | FALLBACK`, and stays `NULL` on the error path; `JobState.ERROR` carries the failure.
  The reserving comment at `vocabulary.py:57-60` is replaced with the answer.
- **D-02: `JobState.FALLBACK` is added, and a total `ScanOutcome -> JobState` function maps onto it.**
  The function lives in `vocabulary.py` behind `match` + `assert_never`, mirroring
  `PipelineEvent.job_state` at `pipeline.py:48-87`. `SUCCESS -> DONE`, `FALLBACK -> FALLBACK`.
- **D-03: `FALLBACK` joins `TERMINAL_STATES`,** and gains arms in `state_label()`,
  `progress_label()`, and any other total function over `JobState`.
- **D-04: a new `finish_job()` on `JobStore` performs the terminal write.** One `@_locked` method
  setting `state`, `outcome`, `warning` and the three page counts together, plus `error` and
  `error_category` on the failure path. `update_state` keeps its current signature and **never names
  the six Phase 22 result columns**.
- **D-05: a `FALLBACK` job reads "Saved to folder" and is styled as a warning.** Amber/warning
  treatment in the status partial, the history table, and `saneless jobs`. The `warning` column
  carries the "title, tags and correspondent were not applied" detail.

**Preservation**

- **D-06: one `try/except` spanning `upload_document` and `poll_task`, inside `run_pipeline`'s
  `with` block.** On any exception: `shutil.move` the PDF to `<data_dir>/failed/`, then re-raise with
  the destination path named in the message. `shutil.move`, **not** `Path.rename` / `os.replace`.
- **D-07: only the PDF is preserved; page images are not.** The verifier must not read OUTC-04 as
  requiring PNGs in `failed/`.
- **D-08: the duplex-mismatch path gets full parity.** Both partial PDFs inside the same preservation
  guard, both tasks polled, outcome + warning + page counts persisted.
- **D-09: the PDF is uniquely named at assembly, not at the copy sites.** Add `job_id` to
  `PipelineRequest`, give `assemble_pdf` a filename argument, build the name once — timestamp + job
  id + sanitised title. The consume-dir copy and the `failed/` move inherit uniqueness via
  `pdf_path.name`. The consume-directory `.part` rename must happen **inside** the consume directory.

**Timeouts and error semantics**

- **D-10: the PDF is preserved on FAILURE *and* on TIMEOUT.** The raised error must name the task id.
- **D-11: `PaperlessTimeoutError` subclasses `PaperlessError`.**
- **D-12: `poll_task` raises immediately on any non-200 response,** carrying status code and body.
  The deadline becomes `time.monotonic()`-based and includes request time. A **200 with an empty
  list** is the existing race (Pitfall #8) and must keep being tolerated inside the deadline.
- **D-13: `test_connection` returns a `ConnectionStatus` StrEnum defined in `vocabulary.py`.**
  Members `CONNECTED`, `TOKEN_REJECTED`, `NOT_FOUND`, `SERVER_ERROR`, `UNREACHABLE`, message behind
  `match` + `assert_never`. `CONNECTED` only for a 2xx. **Wire compatibility required** — the three
  existing strings stay byte-identical.

**data_dir**

- **D-14: `output.data_dir` defaults to `~/.local/state/saneless`, and there is no migration of an
  existing database.** The old file is left where it is; release/upgrade docs mention it.
  `<data_dir>/saneless.db` and `<data_dir>/failed/`. Written in the same hardcoded
  `Path.home() / ".local" / "state"` style as `log_file` at `config.py:86`. Do **not** read
  `$XDG_STATE_HOME` in this phase.
- **D-15: the Dockerfile sets the container's `data_dir`; compose mounts the volume there.**
  `ENV SANELESS_OUTPUT__DATA_DIR=/var/lib/saneless`; compose repoints `saneless-data` to
  `/var/lib/saneless`; `/tmp/saneless` stops being mounted. **Do not touch the
  `ghcr.io/kris-knigga/saneless` image name** (Phase 31).
- **D-16: `db_path` and `failed_dir` become computed `@property` on `OutputConfig`.**
  `validate_settings_dirs` gains a `data_dir` writability check.

### Claude's Discretion

- The OUTC-10 end-to-end test's shape and hermeticity (must be fast now — Phase 20 gate on every push).
- The img2pdf DPI layout function's authoritative DPI source, and behaviour when pages disagree.
- The exact `FALLBACK` rendering in the status partial, history table, and `saneless jobs`, within
  D-05; including whether `warning` is inline or on hover, and whether `progress_label` needs a
  meaningful `FALLBACK` arm or a total-lookup placeholder.
- Which doc sentences get rewritten (minimum:
  `docs/explanation/consume-directory-fallback.md:66`, `docs/reference/web-api.md:55-56`, the job-db
  location docs, the compose volume documentation).
- The sanitisation rule for D-09's PDF filename — character set, length cap, empty-title behaviour.
- Whether the default `paperless_task_timeout` of 300 s is still right now that a timeout is fatal.

### Deferred Ideas (OUT OF SCOPE)

- Assembling the PDF outside the temporary directory so "kept" is the default.
- Page images spooled to disk and preserved alongside the PDF — Phase 29 (M-08).
- `$XDG_STATE_HOME` / `~` expansion for `data_dir` and `log_file` — Phase 27.
- Read-back DPI from the device, and real SANE error messages — Phase 24.
- Plain-language error display via `error_message()` — Phase 30 (U-05).
- The `kris-knigga` -> `kdknigga` image rename in `docker-compose.yml` — Phase 31 (M-25..M-31).
- The general "no `time.sleep` in tests" sweep — Phase 32 (M-34).
- Splitting `failed/` into `$XDG_DATA_HOME`.
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| OUTC-01 | Paperless FAILURE or timeout raises; job recorded FAILED with the Paperless message, never DONE | § "Paperless-ngx task semantics" — exact status values per API version, exact field carrying the failure message, and the **API v10 pagination break** that must be fixed for this requirement to work at all |
| OUTC-02 | Consume-dir-only delivery is recorded `FALLBACK` with a warning; status partial, history table, and CLI `jobs` render it distinctly | § "Rendering surfaces inventory" — the four exact edit sites; § "Vocabulary completeness tests that will break" |
| OUTC-03 | Duplex mismatch recorded with a warning, not a silent DONE | § "`_handle_duplex_mismatch` today" — it returns `(warning, delivered)` and the caller already builds a correct `ScanResult`; only the worker's discard stands in the way |
| OUTC-04 | Preserve the assembled PDF to `<data_dir>/failed/`; guard spans `upload_document` **and** `poll_task` | § "Pattern 2: the preservation guard" — verified `shutil.move` semantics, the `shutil.Error`-on-collision trap, and the exception-masking trap |
| OUTC-05 | Unique PDF file name; consume-dir copy written to `.part` and renamed atomically | § "Pattern 3: unique naming" + § "Pattern 4: the atomic `.part` rename" — verified same-directory `os.replace` is atomic and unaffected by the bind-mount `EBUSY` finding |
| OUTC-06 | A4 @ 300 DPI produces a 595 x 842 pt MediaBox via img2pdf's fixed-DPI layout function | § "Pattern 1: fixed-DPI layout" — API verified against installed img2pdf 0.6.3, arithmetic executed, and the exact float values the test must tolerate |
| OUTC-07 | `poll_task` raises on any non-200; monotonic deadline includes request time | § "Pattern 5: the monotonic deadline" + the deadline-clamped sleep that makes OUTC-10 fast |
| OUTC-08 | `test_connection` reports connected only for 2xx; 404 and 5xx distinct | § "Pattern 6: `ConnectionStatus`" — wire-compat constraint and the `httpx` exception surface |
| OUTC-09 | Job database and `failed/` under `output.data_dir`, never under `tmp_dir` | § "Runtime State Inventory" + § "SQLite/WAL and the data_dir move" — verified `sqlite3.connect` does **not** create parent directories, and which WAL sidecar files exist |
| OUTC-10 | Parametrised e2e test through five outcomes asserting persisted state, outcome, page counts, file preservation | § "Making OUTC-10's end-to-end test fast" — measured sleep costs and a zero-sleep recipe using only existing seams |
</phase_requirements>

---

## Summary

This phase has no new libraries and no new algorithms. Everything it needs is already installed and
already has a precedent in the codebase. The research risk is concentrated in three places, and one
of them is a **latent production bug that will become a phase-breaking bug if it is not fixed here.**

**The finding that changes the phase:** `poll_task` reads `/api/tasks/` assuming an *unpaginated JSON
list with UPPERCASE status strings*. That contract is **API version 9**. Modern paperless-ngx serves
**API version 10 by default when no `Accept` header is sent**, and v10 (a) paginates `/api/tasks/`
into `{"count", "next", "previous", "results"}`, (b) lower-cases every status
(`pending`/`started`/`success`/`failure`/`revoked`), and (c) replaced the `result` string with a
`result_data` dict whose failure message is at `result_data["error_message"]`. `saneless` sends no
version header. Against a current server, `isinstance(tasks, list)` is `False`, the loop never sees a
terminal status, and every poll runs to its full 300 s timeout. Today that is invisible (the return
value is discarded at `pipeline.py:521-527`). **After this phase it is fatal:** OUTC-01 makes a
timeout raise, D-10 preserves the PDF, and every successful scan against a current paperless-ngx
would be recorded FAILED with a stray file in `failed/`. The fix is small and belongs in this phase:
pin `Accept: application/json; version=9` on the client, or teach `poll_task` to accept both shapes.
Recommendation and rationale in § "State of the Art".

**The two discretion items both have a defensible, verified answer.** For OUTC-06,
`img2pdf.get_fixed_dpi_layout_fun((dpi, dpi))` is confirmed present and correct in the installed
0.6.3, and the DPI source should be `profile.resolution` — because `crop_to_paper_size` already
treats it as authoritative, and because PIL simply does not carry a DPI on this path (verified:
`image.crop()` returns `info == {}`, and the SANE backend never sets one). For OUTC-10, the test can
be made to sleep *zero* seconds using only existing constructor parameters: `max_retries=1` removes
both upload sleeps (measured: 3.00 s -> 0.00 s), and a deadline-clamped sleep in the rewritten
`poll_task` makes the TIMEOUT case cost `paperless_task_timeout` seconds exactly — set it to 0.05 in
the fixture. No new seam, no monkeypatching of `time.sleep`.

**Everything else is mechanical and verified.** `pikepdf` 10.5.1 is already in the lockfile as a
transitive dependency of `img2pdf`, so asserting a MediaBox costs no new runtime dependency — but it
should be promoted to an explicit dev dependency rather than relied on transitively.
`sqlite3.connect` raises `OperationalError: unable to open database file` when the parent directory
is missing, which makes the `data_dir.mkdir()` ordering a correctness requirement, not hygiene — and
exposes an existing latent bug in `cli.py:227` that has no `mkdir` today. `shutil.move` onto an
*existing directory* raises `shutil.Error` on basename collision while `shutil.move` onto an
*explicit full path* silently overwrites; the explicit form is what the preservation guard wants.
Same-directory `os.replace` is atomic and is unaffected by the bind-mounted-*file* `EBUSY` finding
the roadmap records for Phase 27.

**Primary recommendation:** Sequence the phase as (1) vocabulary + `finish_job` + worker wiring,
(2) `paperless.py` contract rewrite *including the API-version pin*, (3) preservation guard + unique
naming + fixed-DPI layout, (4) `data_dir` + config + Docker, (5) rendering + docs, (6) the OUTC-10
parametrised e2e test last, as the integrating proof. Raise the API-version finding to the user
before planning: it is new information that was not on the table when the sixteen decisions were
made, and it changes what "records the job FAILED with the Paperless message" has to read.

---

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Outcome vocabulary (`JobState.FALLBACK`, `ScanOutcome -> JobState`, `ConnectionStatus`) | `vocabulary.py` (leaf module) | — | Phase 21 established `vocabulary.py` as the leaf that everything imports and that imports nothing but `exceptions`. `ConnectionStatus` is a domain word, not an HTTP detail. |
| Terminal result persistence (`finish_job`) | `job.py` (`JobStore`) | — | D-04. The six result columns exist from Phase 22 with no writer; the writer is a store method, not SQL assembled in the worker. |
| Deciding *which* terminal state a run earned | `worker.py` (`_process_job`) | `vocabulary.py` (the total mapping) | The worker owns the terminal write (`status_cb` deliberately refuses to write terminal states — `worker.py:212-216`). The *mapping* is vocabulary's; the *decision to write* is the worker's. |
| Preservation on delivery failure | `pipeline.py` (`run_pipeline`, inside the `with`) | `config.py` (`failed_dir`) | D-06. Only `run_pipeline` is inside the `TemporaryDirectory` scope; a guard anywhere else is too late. |
| PDF file naming | `pdf.py` (`assemble_pdf`) | `pipeline.py` (supplies `job_id` + title) | D-09 — name built once at assembly so both copy sites inherit it. |
| Page geometry / DPI declaration | `pdf.py` (`assemble_pdf` via img2pdf layout fun) | `pipeline.py` (passes `profile.resolution`) | The DPI is a property of the acquisition, known to the pipeline; declaring it in the PDF is pdf.py's job. |
| HTTP contract: status values, pagination shape, failure message extraction | `paperless.py` | — | The only module that speaks to paperless-ngx. EXC-01 (Phase 28) will later require no `httpx` type escapes this boundary; do not widen the boundary here. |
| Atomic consume-directory delivery | `paperless.py` (`upload_document`'s fallback branch) | — | The `.part` file must be created and renamed inside the consume directory, which only `paperless.py` knows about. |
| Path computation (`db_path`, `failed_dir`) | `config.py` (`OutputConfig` properties) | — | D-16 — one definition, three readers (`cli.py`, `web/app.py`, pipeline). |
| Rendering a `FALLBACK` job | `web/templates/partials/*.html`, `web/static/app.css`, `cli.py` | `vocabulary.py` (`state_label`) | Labels are vocabulary's; presentation (colour class, column) is the surface's. |

---

## Standard Stack

### Core — no additions

| Library | Version (lockfile) | Purpose | Why Standard |
|---------|--------------------|---------|--------------|
| `img2pdf` | 0.6.3 | Lossless image -> PDF assembly, and the fixed-DPI page layout OUTC-06 requires | Already the project's assembler. `get_fixed_dpi_layout_fun` confirmed present in the installed version [VERIFIED: `uv run python -c "import img2pdf; inspect.signature(img2pdf.get_fixed_dpi_layout_fun)"` -> `(fixed_dpi)`] |
| `httpx` | >=0.28.1 | Paperless HTTP client, and `MockTransport` for the e2e test | Already the client; `_transport` seam already exists at `paperless.py:91` and is already used 24 times in `tests/test_paperless.py` [VERIFIED: codebase] |
| `Pillow` | 12.1.1 | Page images | Already present. Note it does **not** supply a usable DPI on this path — see Pitfall 4 |
| stdlib `shutil` | 3.14 | `shutil.move` for the cross-device-safe preservation move (D-06) | Semantics verified below |
| stdlib `os` | 3.14 | `os.replace` for the atomic `.part` rename (OUTC-05) | POSIX `rename(2)` within one directory is atomic |
| stdlib `sqlite3` | 3.14 | Job store | Unchanged; no new migration needed (Phase 22 D-11 added every column) |

### Supporting — one dev dependency to promote

| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| `pikepdf` | 10.5.1 (already in `uv.lock`) | Reading a PDF's `MediaBox` in the OUTC-06 test | **Already installed** as a transitive dependency of `img2pdf` 0.6.3 [VERIFIED: `uv.lock:241-247` lists `pikepdf` under `img2pdf`'s `dependencies`; `uv run python -c "import pikepdf"` -> 10.5.1]. Promote to an explicit `[dependency-groups] dev` entry rather than importing a transitive dep — see Pitfall 8 |

**No `pypdf` needed.** [VERIFIED: `pyproject.toml` dev group contains no PDF reader; `pikepdf` arrives free.]

### Alternatives Considered

| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| `pikepdf` MediaBox assertion | Regex the raw PDF bytes for `/MediaBox [0 0 595.2 841.68]` | Zero dependencies, but brittle against img2pdf output changes and unreadable in a failure message. Reject. |
| `get_fixed_dpi_layout_fun` | `img.save(png_path, dpi=(res, res))` so img2pdf reads the DPI from the PNG | Produces the *same* MediaBox [VERIFIED: a PNG saved with `dpi=(300,300)` yields 595.2 x 841.92 with no layout fun]. But it round-trips DPI through PNG's pixels-per-metre integer and comes back as **299.9994**, and OUTC-06 names the layout function explicitly. Reject. |
| `shutil.move` | `Path.rename` / `os.replace` | Explicitly rejected by D-06: raises `OSError: [Errno 18] Invalid cross-device link` once `data_dir` and `tmp_dir` are separate settings [VERIFIED: `os.replace("/dev/shm/x", "./y")` -> `errno 18 Invalid cross-device link`] |
| Pin `Accept: version=9` | Teach `poll_task` to read both v9 and v10 shapes | See § "State of the Art" — recommendation is to pin *and* tolerate |

**Installation:**

```bash
uv add --dev pikepdf    # promote the existing transitive dep to explicit
```

No runtime `uv add` is needed by this phase.

---

## Package Legitimacy Audit

| Package | Registry | Age | Downloads | Source Repo | slopcheck | Disposition |
|---------|----------|-----|-----------|-------------|-----------|-------------|
| `pikepdf` | PyPI | ~9 yrs (0.1.0 through 10.13.0.post1) | multi-million/mo | github.com/pikepdf/pikepdf | `[OK]` | Approved — already in `uv.lock` at 10.5.1 as an `img2pdf` transitive dep |

[VERIFIED: `slopcheck install pikepdf` -> `[OK] pikepdf (pypi)`, `1 OK`]
[VERIFIED: `pip index versions pikepdf` -> `pikepdf (10.13.0.post1)`, ~200 historical releases back to 0.1.0]

**Packages removed due to slopcheck `[SLOP]` verdict:** none.
**Packages flagged as suspicious `[SUS]`:** none.

Note: `slopcheck` does not accept a `--json` flag in the installed build; the human-readable output
was used instead. No other external package is introduced by this phase.

---

## Architecture Patterns

### System Architecture Diagram

```
  CLI `saneless scan`            Web POST /api/scan
          |                              |
          |                        JobStore.create_job  --> jobs row (PENDING)
          |                              |
          |                        queue --> ScanWorker._process_job   [worker.py:171]
          |                              |
          +------------------------------+
                         |
                         v
            run_pipeline(scanner, paperless, settings, request)   [pipeline.py:398]
                         |
       .-----------------+ with TemporaryDirectory(dir=tmp_dir)  [pipeline.py:448]
       |                 |
       |                 v
       |         scan pages  ---(manual duplex?)---> _scan_manual_duplex
       |                 |                                   |
       |                 |                          count mismatch? --yes--> _handle_duplex_mismatch
       |                 |                                   |                  (2 PDFs, 2 uploads,
       |                 v                                   |                   D-08: now 2 polls)
       |         _drop_empty_pages                           |
       |                 |                                   |
       |                 v                                   |
       |         assemble_pdf(images, dir, filename, dpi) ----+   [pdf.py:29]
       |                 |        ^                 ^
       |                 |        |                 `-- get_fixed_dpi_layout_fun((res,res))  OUTC-06
       |                 |        `-- D-09 unique name: <ts>-<job_id>-<slug>.pdf
       |                 v
       |   .-------- PRESERVATION GUARD (D-06 / OUTC-04) -----------------.
       |   |             |                                                |
       |   |    upload_document()  [paperless.py:105]                     |
       |   |      |            \                                          |
       |   |   2xx + uuid    retries exhausted                            |
       |   |      |              |                                        |
       |   |      |        consume_dir set? --no--> raise PaperlessError  |
       |   |      |              |yes                                     |
       |   |      |        write <name>.part in consume_dir,              |
       |   |      |        os.replace -> <name>          OUTC-05          |
       |   |      |              |                                        |
       |   |      |        UploadResult(delivered=False, path)            |
       |   |      |              |                                        |
       |   |    poll_task()  [paperless.py:210]                           |
       |   |      |    monotonic deadline; non-200 -> raise  OUTC-07      |
       |   |      |    terminal FAILURE/REVOKED -> raise w/ message       |
       |   |      |    deadline passed -> raise PaperlessTimeoutError     |
       |   |      |                                                       |
       |   `------|--- on ANY exception: shutil.move(pdf, failed_dir) ----'
       |          |                      then re-raise naming the path
       |          v
       `--- ScanResult(outcome=SUCCESS|FALLBACK, 3 page counts, warning)
                    |
                    v
       worker: finish_job(job_id, state=job_state_for(outcome), outcome,
                          warning, pages_scanned, pages_removed, pages_uploaded)
                    |            ^ D-02 total match/assert_never
                    |
       worker except: finish_job(job_id, ERROR, error=str(exc),
                                 error_category=classify_error(exc))
                    |
                    v
              jobs row (DONE | FALLBACK | ERROR)
                    |
        .-----------+-----------.
        v           v           v
  status.html   history.html  `saneless jobs`     <-- all three need FALLBACK  OUTC-02
```

### Recommended file map (no new modules)

```
src/saneless/
├── vocabulary.py   # + JobState.FALLBACK, + ConnectionStatus, + job_state_for(outcome),
│                   #   + FALLBACK arms in state_label/progress_label, + TERMINAL_STATES
├── exceptions.py   # + PaperlessTimeoutError(PaperlessError)          D-11
├── config.py       # + OutputConfig.data_dir, .db_path, .failed_dir   D-14/D-16
│                   # + data_dir writability in validate_settings_dirs
├── job.py          # + finish_job() @_locked                          D-04
├── pdf.py          # assemble_pdf(images, output_dir, filename, dpi)  D-09/OUTC-06
├── pipeline.py     # + PipelineRequest.job_id, preservation guard,    D-06/D-08/D-09
│                   #   poll in _handle_duplex_mismatch
├── paperless.py    # poll_task rewrite, test_connection -> enum,      OUTC-07/08
│                   #   .part rename, Accept version header            OUTC-05
├── worker.py       # ScanResult consumed -> finish_job                OUTC-01/02/03
├── cli.py          # db_path property; FALLBACK in jobs table
└── web/
    ├── app.py      # db_path property
    ├── static/app.css              # .status-fallback
    └── templates/partials/
        ├── status.html             # FALLBACK branch
        └── history.html            # FALLBACK class
```

---

### Pattern 1: img2pdf fixed-DPI layout (OUTC-06)

**What:** `img2pdf.get_fixed_dpi_layout_fun(fixed_dpi)` returns a callable that *overrides whatever
DPI is claimed in the input images*, and is passed to `convert` as `layout_fun=`.

**The exact API in the installed version** [VERIFIED: `inspect.getsource`, img2pdf 0.6.3]:

```python
def get_fixed_dpi_layout_fun(fixed_dpi):
    """Layout function that overrides whatever DPI is claimed in input images.

    >>> layout_fun = get_fixed_dpi_layout_fun((300, 300))
    >>> convert(image1, layout_fun=layout_fun, ... outputstream=...)
    """
    def fixed_dpi_layout_fun(imgwidthpx, imgheightpx, ndpi):
        return default_layout_fun(imgwidthpx, imgheightpx, fixed_dpi)
    return fixed_dpi_layout_fun
```

The argument is a **2-tuple `(x_dpi, y_dpi)`**, not a scalar. Passing an int silently produces
wrong geometry (`default_layout_fun` unpacks it).

**Verified arithmetic** [VERIFIED: executed in this repo's venv]:

| Input | `layout_fun` | Resulting MediaBox | Rounded |
|-------|--------------|--------------------|---------|
| 2480 x 3508 px PNG, no DPI metadata | `get_fixed_dpi_layout_fun((300,300))` | `[0, 0, 595.2, 841.92]` | 595 x 842 ✅ |
| 2480 x 3508 px PNG, no DPI metadata | *none* | `[0, 0, 1860.0, 2631.0]` | ❌ |
| `crop_to_paper_size(img, "a4", 300)` -> 2480 x **3507** px | `get_fixed_dpi_layout_fun((300,300))` | `[0, 0, 595.2, **841.68**]` | 595 x 842 ✅ |

Two things the roadmap's prose gets slightly wrong, and the planner must know:

1. **Without a layout function the MediaBox is 1860 x 2631 pt, not 2480 x 3508 pt.** `img2pdf`'s
   `default_dpi` is **96**, not 72, so an unannotated image is laid out at 96 DPI:
   `2480 / 96 * 72 = 1860`. The bug is real; the number in `23-CONTEXT.md` § "Phase Boundary" item 4
   is not. Do not assert 2480 x 3508 in a regression test.
2. **The MediaBox is never exactly `595 x 842`.** It is `595.2 x 841.92` for a mathematically exact
   A4 raster and `595.2 x 841.68` for what `crop_to_paper_size` actually produces (because
   `int(297 * 300 / 25.4) == 3507`, truncating 3507.87). The OUTC-06 test **must** round or use a
   tolerance. `[round(v) for v in mediabox] == [0, 0, 595, 842]` passes for both.

**Which DPI is authoritative — recommendation: `profile.resolution`.** Three independent reasons,
all verified:

- **PIL carries no DPI on this path.** The SANE backend yields images from `dev.snap()` and then
  routes them through `_maybe_crop` -> `crop_to_paper_size`, whose `Image.crop()` returns a fresh
  image with `info == {}` [VERIFIED: `crop_to_paper_size(Image.new("RGB",(2600,3700)), "a4", 300).info` -> `{}`].
  `Image.info["dpi"]` is simply absent, so a "prefer PIL, fall back to profile" order would fall back
  100% of the time and be dead code.
- **`crop_to_paper_size` already treats `profile.resolution` as authoritative** (`paper_sizes.py:56-58`
  computes crop pixels from it). Using a different DPI source for the MediaBox than for the crop
  would let the two disagree — the crop would be A4-shaped and the MediaBox would not.
- **PNG DPI round-trips lossily.** A PNG saved with `dpi=(300,300)` reads back as
  `(299.9994, 299.9994)` [VERIFIED] because PNG stores pixels-per-metre as an integer. img2pdf's
  `pil_get_dpi` compensates with `int(round(ndpi))`, but any saneless code comparing to 300 would
  not. Avoid the whole class of bug.

**What happens if pages disagree:** the question is moot under a fixed layout function — it applies
the same DPI to *every* page unconditionally. [VERIFIED: a 300-DPI-tagged A4 page and an untagged
150-DPI-sized page in one `convert` call with `get_fixed_dpi_layout_fun((300,300))` produced
`595.2 x 841.92` and `297.6 x 420.96` — the second page is half-size because it was *forced* to 300
DPI, not because img2pdf honoured its own DPI.] Since one pipeline run scans with one
`profile.resolution`, all pages are genuinely at one DPI and this is correct. Document the property:
*a fixed-DPI layout means a page's PDF size is determined entirely by its pixel count.*

```python
# Source: img2pdf 0.6.3 docstring, verified by execution
layout_fun = img2pdf.get_fixed_dpi_layout_fun((dpi, dpi))
pdf_bytes = img2pdf.convert(image_paths, layout_fun=layout_fun)
```

---

### Pattern 2: the preservation guard (OUTC-04 / D-06)

**What:** a `try/except` inside `run_pipeline`'s `with tempfile.TemporaryDirectory(...)` block,
spanning `upload_document` **and** `poll_task`, that moves the PDF to `failed_dir` and re-raises.

**Verified `shutil.move` semantics** [VERIFIED: all executed]:

| Call | Behaviour |
|------|-----------|
| `shutil.move(src, existing_dir)` | Moves in keeping the basename — **but raises `shutil.Error("Destination path '...' already exists")` if the basename is taken** |
| `shutil.move(src, existing_dir / "name.pdf")` | `os.rename` — **silently overwrites** an existing destination |
| `shutil.move(src, missing_dir / "name.pdf")` | `FileNotFoundError` |
| `shutil.move(src, missing_dir)` (dir target that doesn't exist) | Creates a **file** named `missing_dir` — silent data-shaped corruption |
| cross-device | `os.rename` raises `OSError`, caught, falls through to `copy2` + `os.unlink` — preserves mode/atime/mtime, not owner |
| return value | a `str`, even when given `Path` arguments; typeshed types it `Any` |

**Three traps the planner must design around:**

1. **`shutil.Error` is not an `OSError`.** A `try: shutil.move(...) except OSError:` will not catch
   the basename-collision case. Use the **explicit full destination path** form
   (`failed_dir / pdf_path.name`) — it never raises `shutil.Error`, and D-09's unique names make
   overwrite a non-event.
2. **The destination directory must exist.** `failed_dir.mkdir(parents=True, exist_ok=True)` before
   the move, inside the except handler — not at startup only, because `data_dir` could have been
   removed between startup and now.
3. **The preservation must not mask the original exception.** If the move itself fails, the user
   needs to learn both that delivery failed *and* that preservation failed. Chain deliberately:

```python
# Recommended shape (illustrative; not copied from any source)
try:
    upload_result = paperless.upload_document(pdf_path, ...)
    task_uuid = upload_result.task_uuid
    if task_uuid is not None:
        paperless.poll_task(task_uuid, timeout=settings.output.paperless_task_timeout)
        outcome = ScanOutcome.SUCCESS
    else:
        outcome = ScanOutcome.FALLBACK
except Exception as exc:
    failed_dir = settings.output.failed_dir
    try:
        failed_dir.mkdir(parents=True, exist_ok=True)
        destination = failed_dir / pdf_path.name
        shutil.move(pdf_path, destination)          # explicit full path: no shutil.Error
    except OSError as move_exc:
        msg = f"{exc}; the scan could NOT be preserved to {failed_dir}: {move_exc}"
        raise PaperlessError(msg) from exc
    msg = f"{exc}; the scan was preserved at {destination}"
    raise type(exc)(msg) from exc        # <-- see note
```

**Note on the re-raise:** `raise type(exc)(msg) from exc` is fragile — it assumes a single-string
constructor, which is true for every saneless exception but not for arbitrary third-party types
(`httpx` exceptions in particular take keyword arguments). Since D-12 and OUTC-01 make `poll_task`
raise `PaperlessError`/`PaperlessTimeoutError`, and `upload_document` already raises
`PaperlessError`, the guard can legitimately narrow its `except` to `PaperlessError` and re-raise a
`PaperlessError`. That also satisfies D-06's "a broad guard ... would catch failures in code
unrelated to delivery and report them in the job error as delivery failures" rejection — a narrow
`except PaperlessError` is *more* aligned with the recorded reasoning than a bare `except Exception`.
**Flag this to the user:** D-06 says "on any exception"; narrowing to `PaperlessError` is a small
deviation with a good reason, and should be raised rather than silently chosen.

**The `_handle_duplex_mismatch` parity (D-08).** That function already returns `(warning, delivered)`
and `run_pipeline:481-490` already builds a correct `ScanResult` from it. Its gaps are exactly two:
it polls neither task (`paperless.py` is called twice at `pipeline.py:227,234` with the
`UploadResult`s discarded except for `.delivered_to_api`), and both `assemble_pdf` calls are outside
any guard. Bringing it inside the same guard means the guard must handle **two** PDFs — the simplest
shape is a small helper that takes a list of paths to preserve.

---

### Pattern 3: unique PDF naming (OUTC-05 / D-09)

**Existing precedent to follow:** `auto_profiles._slugify` (`auto_profiles.py:33-44`) is the
project's only slug helper today and is deliberately minimal —
`lower.replace(" ", "-").replace("_", "-")`. It is *not* safe for a user-supplied title: it passes
`/`, `..`, NUL, and control characters straight through. A title of `../../etc/passwd` would escape
`failed_dir`. **Do not reuse it for this.** Write a separate, allow-list-based sanitiser in `pdf.py`.

Recommended rule (this is the discretion item; record the choice in the plan):

- **Allow-list**, not deny-list: `[A-Za-z0-9]` kept, everything else -> `-`, runs of `-` collapsed,
  leading/trailing `-` stripped. An allow-list cannot be defeated by an unanticipated character.
- **Length cap** on the slug only (the timestamp and job id are fixed width). 60 characters keeps the
  whole name well under the 255-byte `NAME_MAX` on ext4/overlayfs and under the 143-byte limit on
  eCryptfs.
- **Empty after sanitisation** -> omit the slug segment entirely. `20260911-143052-a1b2c3d4.pdf` is
  still unique and still legible; a literal `untitled` is also fine but is a lie if the title was
  `日本語` (all non-ASCII, all stripped).
- **Uniqueness comes from the job id, not the timestamp.** Two jobs submitted in the same second with
  the same title collide on timestamp+title alone. `job.id` is a `uuid4` string; the first 8
  characters are ample and keep the name readable.

Shape: `{YYYYmmdd-HHMMSS}-{job_id[:8]}-{slug}.pdf`.

**Timezone:** the codebase already standardises on `datetime.now(tz=UTC)` (`pipeline.py:505`,
`job.py`) and ruff's `DTZ` ruleset is enabled — a naive `datetime.now()` will fail lint.

---

### Pattern 4: the atomic `.part` rename (OUTC-05)

**What:** write the consume-directory copy to `<consume_dir>/<name>.pdf.part`, `fsync`, then
`os.replace` to `<consume_dir>/<name>.pdf`.

**Why the rename must be inside the consume directory:** `rename(2)` is atomic only within a single
filesystem. The consume directory is, in the documented deployment, a Docker volume or a network
share — a different filesystem from `tmp_dir`. A rename across that boundary is `EXDEV`
[VERIFIED: `os.replace` from `/dev/shm` to a local path -> `errno 18, Invalid cross-device link`].
The existing code does `shutil.copy2(pdf_path, dest)` (`paperless.py:203`) — a non-atomic copy into
the directory paperless-ngx is actively watching, so paperless can pick up a half-written PDF.
Writing `.part` first works because paperless-ngx's consumer ignores unknown extensions, and the
final `os.replace` is a pure metadata operation inside one directory.

**Does the roadmap's `EBUSY` finding apply here? No.** [VERIFIED: `ROADMAP.md:230` records that
`os.replace` over a bind-mounted **file** returns `EBUSY` — that is the Phase 27 config-file case,
where the *destination inode itself* is the mount point. Here the destination is an ordinary new file
*inside* a bind-mounted directory; the directory is the mount point and the rename does not touch it
as an inode. Same-directory `os.replace` verified working on this machine.]

**Durability:** `.part` + rename gives *atomicity* (no partial file visible) but not *durability* —
for that you need `f.flush(); os.fsync(f.fileno())` before the rename, and strictly an `fsync` on the
directory fd after. The consume directory is a handoff, not a system of record, so directory fsync is
overkill; file fsync before rename is cheap and worth doing. Record the choice.

**Cleanup:** if the copy raises partway, the `.part` file is left behind. Wrap in `try/except` and
`Path(part).unlink(missing_ok=True)` before re-raising, so a failed fallback does not litter the
directory paperless-ngx scans.

---

### Pattern 5: the monotonic deadline (OUTC-07)

**The bug in `paperless.py:227,243-245`:**

```python
delay = 0.5
elapsed = 0.0
while elapsed < timeout:
    response = self._client.get(...)      # <-- up to 30 s (client timeout), uncounted
    ...
    time.sleep(delay)
    elapsed += delay                       # <-- only sleep time accumulates
    delay = min(delay * 2, 30.0)
```

With a 30 s per-request `httpx` timeout (`paperless.py:94`) and a 300 s budget, the real wall clock
ceiling is roughly `300 + 30 * (number of polls)` — unbounded relative to the documented value.

**The replacement shape, and the property that makes OUTC-10 fast:**

```python
deadline = time.monotonic() + timeout
delay = 0.5
while True:
    response = self._client.get("/api/tasks/", params={"task_id": task_id})
    if response.status_code != 200:                                   # D-12 / OUTC-07
        msg = f"Paperless task poll failed: {response.status_code} {response.text}"
        raise PaperlessError(msg)
    task = _extract_task(response.json())          # tolerates both API shapes; None if absent
    if task is not None:
        status = _normalise_status(task)           # upper() -- v10 is lowercase
        if status in _TERMINAL_STATUSES:           # SUCCESS, FAILURE, REVOKED
            return task                            # caller raises on FAILURE/REVOKED
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise PaperlessTimeoutError(
            f"Paperless task {task_id} did not finish within {timeout}s"
        )
    time.sleep(min(delay, remaining))              # <-- the clamp; makes small timeouts exact
    delay = min(delay * 2, 30.0)
```

The `min(delay, remaining)` clamp is what makes a `paperless_task_timeout` of `0.05` cost 0.05 s
instead of the 0.5 s the current first sleep costs unconditionally. It is also simply correct:
sleeping past your own deadline is a bug.

**Empty-list tolerance (Pitfall #8) is preserved** because `_extract_task` returning `None` falls
through to the sleep, and the response was still 200. Make this explicit in a comment and in a test
— it is the one non-obvious constraint D-12 calls out.

**`REVOKED` must be terminal.** It is one of paperless-ngx's `COMPLETE_STATUSES`
[VERIFIED: upstream `src/documents/models.py`]. The current code loops on it until timeout. After
this phase that becomes a fatal, misattributed timeout. Treat it like `FAILURE`.

---

### Pattern 6: `ConnectionStatus` (OUTC-08 / D-13)

Current behaviour (`paperless.py:250-268`) returns `"connected"` for **any** non-401/403 response,
including 404 and 500, and catches only `httpx.ConnectError` — so a `httpx.ConnectTimeout`,
`ReadTimeout`, or `httpx.InvalidURL` propagates and hits `routes.py:130`'s blanket `except`, which
returns HTTP 502 `{"status": "error"}`. That path is out of OUTC-08's literal scope but is worth a
sentence in the plan.

Mapping (wire-compatible per D-13):

| Condition | Member | JSON value | New? |
|-----------|--------|------------|------|
| 2xx | `CONNECTED` | `"connected"` | existing string |
| 401, 403 | `TOKEN_REJECTED` | `"token_rejected"` | existing string |
| 404 | `NOT_FOUND` | `"not_found"` | **new row in web-api.md** |
| 5xx | `SERVER_ERROR` | `"server_error"` | **new row in web-api.md** |
| `httpx.ConnectError` (and siblings) | `UNREACHABLE` | `"unreachable"` | existing string |

`StrEnum` members serialise through `json.dumps` / FastAPI as their string values with no extra work,
so `routes.py:128`'s `return {"status": status}` keeps working — but the declared return type
`dict[str, str]` is satisfied only because `StrEnum` *is* a `str`. Both `ty` and `pyrefly` accept
that. What is **not** covered by the enum: the 2xx/4xx/5xx branch itself. Every non-2xx status that
is not 401/403/404/5xx (e.g. a 302 from a misconfigured reverse proxy, a 429) needs an explicit home.
Recommend: `response.is_success` -> `CONNECTED`, else fall through the specific cases, else
`SERVER_ERROR` as the total fallback with the status code logged.

**`assert_never` applies to the message lookup, not the classification.** The classification is a
chain of integer comparisons, exactly like `classify_error`'s `isinstance` chain
(`vocabulary.py:230-250`) — that function's own docstring explains why a chain is correct there. Mirror
that reasoning in the docstring.

---

### Anti-Patterns to Avoid

- **Extending `update_state` with the result columns.** Its SQL is an unconditional
  `SET state = ?, error = ?, error_category = ?` (`job.py:616-624`) — it already blanks `error` on
  every in-flight transition, by design. Six more columns would blank the results on every
  `SCANNING`/`ASSEMBLING`/`UPLOADING` write. D-04 is not a style preference.
- **Writing `0` page counts on the failure path.** Phase 22's D-08: `NULL` means "never recorded".
  `finish_job`'s failure signature must not require page counts.
- **A `dict[ScanOutcome, JobState]` for D-02's mapping.** Phase 21's D-08 *measured* that neither
  `ty` nor `pyrefly` diagnoses a missing key. `match` + `assert_never` is diagnosed by both.
- **Reusing `auto_profiles._slugify` for the PDF filename.** It is a two-`replace` helper for
  already-trusted SANE source names and passes `/` and `..` through untouched.
- **A bare `except Exception` around the whole `with` block.** D-06 explicitly rejects a guard that
  spans more than delivery.
- **Asserting `MediaBox == [0, 0, 595, 842]` exactly.** The real values are `595.2` and
  `841.68`/`841.92`. Round.
- **Adding a new `_transport`-style seam or a `sleep_fn` parameter for the e2e test.** Not needed —
  see below.

---

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Cross-filesystem file move | `try: rename except OSError: copy + unlink` | `shutil.move` | Already does exactly that, plus symlink recreation and directory handling. D-06 mandates it. |
| PDF page-size arithmetic | Computing `px / dpi * 72` and writing a `/MediaBox` | `img2pdf.get_fixed_dpi_layout_fun` | Handles rotation, the `default_dpi` fallback, TIFF resolution units, and the "72.009 DPI problem" documented in `pil_get_dpi`'s own source |
| Reading a PDF's MediaBox in a test | Regex over PDF bytes | `pikepdf.open(...).pages[0].MediaBox` | Already installed; survives img2pdf output format changes |
| Slug/filename safety | Deny-list of "bad characters" | An `[A-Za-z0-9]` allow-list + `re.sub` | A deny-list is defeated by the character you did not think of; there is no NUL, no `..`, no `/` in an allow-list by construction |
| Stubbing the paperless HTTP surface | A fake `PaperlessClient` subclass | `httpx.MockTransport` via the existing `_transport=` kwarg (`paperless.py:91`) | Already the established pattern in 24 places in `tests/test_paperless.py`; exercises the *real* `upload_document`/`poll_task` code, which OUTC-10 requires |
| Making tests fast | Monkeypatching `time.sleep` globally | `max_retries=1` + a small `paperless_task_timeout` + the deadline clamp | Uses only constructor parameters that already exist; monkeypatching `time.sleep` in the module under test would also disable the clamp the test is meant to verify |
| Enum completeness enforcement | A hand-written list of member names in a test | `@pytest.mark.parametrize("state", list(JobState))` | Phase 21 D-09's established mechanism; a new member fails the suite automatically |

**Key insight:** this phase's failure mode is not "we built the wrong thing", it is "we built the
right thing on top of a wrong assumption about the paperless-ngx wire format". Every hand-rolled
temptation above is cheap to resist; the API-version assumption is the one that needs deliberate
verification against a real server.

---

## Runtime State Inventory

> This phase relocates a durable file and changes a deployment mount. It is a migration phase in the
> sense that matters: a grep audit will not find what breaks.

| Category | Items Found | Action Required |
|----------|-------------|------------------|
| **Stored data** | The existing job database at `<tmp_dir>/saneless.db` (default `/tmp/saneless/saneless.db`), plus `-wal` and `-shm` sidecars while a connection is open [VERIFIED: after `PRAGMA journal_mode=WAL` + commit, the directory holds `saneless.db`, `saneless.db-shm`, `saneless.db-wal`; after a clean `close()` only `saneless.db` remains — SQLite checkpoints and deletes the sidecars on last-connection close]. **D-14 locks NO migration.** | **Code edit only.** The old file stays. Release notes / upgrade docs must say where it was and where it now is, so a user can move all three files by hand if they want their history. |
| **Live service config** | **Docker named volume `saneless-data`**, currently mounted at `/tmp/saneless` (`docker-compose.yml:21`). Its *contents* live in the Docker volume driver, not in git. After D-15 repoints it to `/var/lib/saneless`, the volume still holds the old `saneless.db` at its root — harmless, but it is the file a user would want to recover. | **Compose edit + a documented recovery note.** No API patch, no data migration. Also: `docs/how-to/deploy-docker-compose.md:53` and `docs/reference/docker.md:106` each carry a second copy of the same mount line and will silently drift if only `docker-compose.yml` is edited. |
| **OS-registered state** | None. No systemd unit, launchd plist, cron entry, or Task Scheduler registration in the repo [VERIFIED: no `*.service`, `*.plist`, or `*.timer` files; the only process manager is Docker's `restart: unless-stopped`]. | None. |
| **Secrets / env vars** | One **new** env var, `SANELESS_OUTPUT__DATA_DIR`, baked into the image by D-15. No existing secret or env var is renamed. `SANELESS_OUTPUT__TMP_DIR` keeps its meaning but loses its second job; `docs/reference/environment-variables.md:44` documents it and needs a `DATA_DIR` sibling row. | **Add one row to `docs/reference/environment-variables.md`.** No secret rotation. |
| **Build artifacts / installed packages** | None affected. `uv_build` produces a wheel from `src/`; no egg-info, no compiled artifact carries a path. The Docker image is rebuilt from the Dockerfile, which is where the new `ENV` lands. | None beyond the normal image rebuild. |

**The canonical question — after every file in the repo is updated, what runtime systems still have
the old path?** Answer: exactly one — the `saneless-data` Docker volume, which will contain an
orphaned `saneless.db` at its root after D-15 repoints the mount. That is acceptable and expected
under D-14, and the upgrade note is the mitigation.

---

## Common Pitfalls

### Pitfall 1: paperless-ngx API v10 breaks `poll_task` completely

**What goes wrong:** `poll_task` polls to full timeout on every scan against a current
paperless-ngx, and after this phase that becomes a fatal `PaperlessTimeoutError` with a preserved PDF
— on *successful* uploads.
**Why it happens:** three simultaneous v9->v10 changes, all confirmed in upstream source:

- `/api/tasks/` is now paginated. `TasksViewSet.paginate_queryset` returns `None` (unpaginated) only
  when `request.version < 10`; otherwise `StandardPagination` applies, so the body is
  `{"count", "next", "previous", "results"}` and `isinstance(tasks, list)` is `False`.
  [VERIFIED: `src/documents/views.py` `TasksViewSet.paginate_queryset`; `docs/api.md:451-457`]
- Status values are **lowercase** in v10 (`Status.PENDING = "pending"` etc.) and are only mapped back
  to `"PENDING"`/`"SUCCESS"` by `TaskSerializerV9._STATUS_TO_V9`.
  [VERIFIED: `src/documents/models.py`, `src/documents/serialisers.py`]
- The `result` field exists **only** in `TaskSerializerV9`. v10 exposes `result_data`, a dict whose
  failure message is at `result_data["error_message"]`. [VERIFIED: both serializers read in full]
- **If no version is specified, Paperless serves the configured default API version (currently
  `10`).** [CITED: paperless-ngx `docs/api.md` § "API Versioning"]

**How to avoid:** send `Accept: application/json; version=9` on the client (one line in
`PaperlessClient.__init__`'s `headers` dict, alongside the existing `Authorization`) **and** write
`_extract_task` to accept both shapes. See § "State of the Art" for the recommendation and its
tradeoff.
**Warning signs:** a scan that the paperless-ngx web UI shows as consumed, while `saneless` reports a
timeout ~300 s later. Today that symptom is invisible because the return value is discarded.

### Pitfall 2: wrapping only `upload_document` deletes the document

**What goes wrong:** the roadmap's own note. `poll_task` raising `FAILURE` (the correct new
behaviour) propagates out of the `with tempfile.TemporaryDirectory(...)`, which deletes `tmp_path`
and the assembled PDF with it.
**Why it happens:** `TemporaryDirectory.__exit__` runs on the exception path; `pdf_path` lives inside
`tmp_path` (`pipeline.py:502`).
**How to avoid:** the guard's `try` must open *before* `paperless.upload_document` and its `except`
must close *after* `paperless.poll_task`. A test that asserts `failed_dir` is non-empty after a
`FAILURE` poll is the direct proof; write it before the implementation.
**Warning signs:** a test that only exercises the upload-raises path passes while the poll-raises
path silently loses the file.

### Pitfall 3: `sqlite3.connect` does not create the parent directory

**What goes wrong:** `JobStore(db_path=str(settings.output.db_path))` raises
`sqlite3.OperationalError: unable to open database file` on a fresh install where
`~/.local/state/saneless/` does not exist.
**Why it happens:** verified — `sqlite3.connect` into a missing directory raises; it does not mkdir.
[VERIFIED: executed]
**How to avoid:** `settings.output.data_dir.mkdir(parents=True, exist_ok=True)` before every
`JobStore(...)`. `web/app.py:57` does this today for `tmp_dir`; **`cli.py:227` does not** — the
`saneless jobs` command has no mkdir at all, and only works today because `/tmp/saneless` is created
by an earlier scan. Moving to `data_dir` makes that latent bug reachable on a clean machine.
Either both entry points mkdir, or (better, given D-16) `OutputConfig.db_path` is read through a
small helper that ensures the directory — but note a `@property` with a filesystem side effect is
surprising, so prefer explicit `mkdir` at both call sites.
**Warning signs:** `saneless jobs` on a machine that has never scanned.

### Pitfall 4: expecting a DPI from PIL

**What goes wrong:** code written as "use `img.info['dpi']` if present, else `profile.resolution`"
looks defensive but the first branch is unreachable dead code.
**Why it happens:** `Image.crop()` returns an image with `info == {}` [VERIFIED], and
`crop_to_paper_size` is on the path for every non-`"full"` paper size. Even without the crop,
`dev.snap()` images carry no DPI. And a PNG that *did* carry one reads back as `299.9994`, not `300`.
**How to avoid:** use `profile.resolution` unconditionally; say so in `assemble_pdf`'s docstring so
Phase 24 (which adds device read-back) knows exactly which line to change.
**Warning signs:** a "prefer the image's own DPI" branch with no test that reaches it.

### Pitfall 5: `shutil.move` onto a directory raises on collision

**What goes wrong:** `shutil.move(pdf, failed_dir)` raises `shutil.Error` — not an `OSError` — when
`failed_dir/<basename>` exists. In an `except OSError:` handler that means an unhandled exception
*inside the exception handler*, masking the original delivery failure with a confusing traceback.
**Why it happens:** `shutil.move`'s directory branch explicitly checks `os.path.exists(real_dst)` and
raises `shutil.Error`; the explicit-path branch goes straight to `os.rename`, which overwrites.
[VERIFIED: source read + executed]
**How to avoid:** always pass the full destination path.
**Warning signs:** a preservation test that never runs twice with the same filename.

### Pitfall 6: `filterwarnings = ["error"]` turns any new DeprecationWarning into a failure

**What goes wrong:** a library call that emits a warning fails the whole suite.
**Why it happens:** `pyproject.toml` sets `filterwarnings = ["error"]` and `xfail_strict = true`.
**How to avoid:** run the new tests once locally before relying on CI. Relevant because
`pikepdf.open` on a file-like object and `img2pdf` both emit warnings under some inputs.
**Warning signs:** a test that passes with `-W ignore` and fails without.

### Pitfall 7: `pytest-timeout` with `timeout_method = "signal"` and the worker thread

**What goes wrong:** an OUTC-10 test that hangs in the worker thread is killed at the 60 s global
timeout — but the SIGALRM fires on the **main** thread, so the traceback points at the test's wait
loop rather than at the stuck worker.
**Why it happens:** `timeout = 60`, `timeout_method = "signal"` in `pyproject.toml`; SIGALRM is
delivered to the main thread by the Python signal machinery regardless of which thread is blocked.
**How to avoid:** this is a diagnosis-quality problem, not a correctness one — the 60 s budget is
generous for a test designed to sleep zero seconds. If the test's wait helper polls with a short
per-attempt budget (see § "Validation Architecture") it will fail with a useful message long before
SIGALRM. Do **not** raise the global timeout to accommodate a slow test.
**Warning signs:** an OUTC-10 case that takes more than ~2 s.

### Pitfall 8: importing a transitive dependency

**What goes wrong:** the OUTC-06 test `import pikepdf`, which is present only because `img2pdf`
0.6.3 happens to depend on it. If img2pdf ever swaps its PDF backend, the test breaks with an
`ImportError` unrelated to the thing it tests.
**Why it happens:** `uv.lock:241-247` lists `pikepdf` under `img2pdf`'s dependencies; nothing in
`pyproject.toml` declares it.
**How to avoid:** `uv add --dev pikepdf`. One line, no new download (it is already resolved and
installed), and the dependency becomes intentional.
**Warning signs:** `grep pikepdf pyproject.toml` returns nothing while `tests/` imports it.

### Pitfall 9: forgetting the second and third copies of the compose mount

**What goes wrong:** `docker-compose.yml` is fixed; `docs/how-to/deploy-docker-compose.md:53` and
`docs/reference/docker.md:106` still tell the user to mount `saneless-data:/tmp/saneless`, and
`docs/reference/docker.md:30` still says `/tmp/saneless` holds the "Scan temp files and SQLite
database / Required: No (ephemeral OK)".
**Why it happens:** the mount line is duplicated in three files; the volume table is a fourth claim.
**How to avoid:** the doc sweep is part of the phase, not a follow-up. Grep for `/tmp/saneless`
across `docs/` and `README.md` — [VERIFIED: 7 hits in 5 files, listed in § "Docs whose claims change"].
**Warning signs:** the `docs/reference/docker.md:30` row still saying "ephemeral OK" after this phase
— that sentence becomes actively dangerous once `failed/` lives there.

---

## Code Examples

### Reading a MediaBox in a test (no new runtime dependency)

```python
# Source: pikepdf 10.5.1, already installed as an img2pdf transitive dependency.
# Verified by execution in this repository's uv environment.
import pikepdf

def test_a4_at_300dpi_has_a4_mediabox(tmp_path: Path) -> None:
    page = Image.new("RGB", (2480, 3508), "white")   # A4 @ 300 dpi
    pdf_path = assemble_pdf([page], tmp_path, filename="x.pdf", dpi=300)
    with pikepdf.open(pdf_path) as pdf:
        media_box = [round(float(v)) for v in pdf.pages[0].MediaBox]
    assert media_box == [0, 0, 595, 842]
    # Exact values are 595.2 x 841.92 -- round(), never ==.
```

### Tolerating both paperless-ngx API shapes

```python
# Source: paperless-ngx upstream src/documents/views.py (TasksViewSet.paginate_queryset)
#         and src/documents/serialisers.py (TaskSerializerV9 / TaskSerializerV10).
_TERMINAL = frozenset({"SUCCESS", "FAILURE", "REVOKED"})

def _extract_task(payload: object) -> dict[str, object] | None:
    """Return the single task from a v9 bare list or a v10 paginated object."""
    if isinstance(payload, dict):            # API v10: {"count", "next", ..., "results": [...]}
        payload = payload.get("results")
    if isinstance(payload, list) and payload:
        first = payload[0]
        if isinstance(first, dict):
            return first
    return None                              # 200 with no task yet -- Pitfall #8, keep polling

def _task_status(task: dict[str, object]) -> str:
    """Normalise v10's lowercase status to the v9 uppercase spelling."""
    return str(task.get("status", "")).upper()

def _failure_message(task: dict[str, object]) -> str:
    """The Paperless message OUTC-01 records, from whichever field carries it."""
    result = task.get("result")                       # v9: reconstructed human string
    if isinstance(result, str) and result:
        return result
    data = task.get("result_data")                    # v10: {"error_type", "error_message", ...}
    if isinstance(data, dict):
        message = data.get("error_message") or data.get("reason")
        if isinstance(message, str) and message:
            return message
    return "Paperless reported a failure with no message"
```

### The zero-sleep e2e fixture

```python
# Measured in this repository: max_retries=3 costs 3.00 s of time.sleep in the
# consume-dir-fallback case; max_retries=2 costs 1.00 s; max_retries=1 costs 0.00 s.
client = PaperlessClient(
    url="http://paperless.invalid",
    token="test-token",
    consume_dir=str(consume_dir),
    max_retries=1,                 # kills BOTH `time.sleep(2**attempt)` calls
    _transport=httpx.MockTransport(handler),
)
settings.output.paperless_task_timeout = 0.05   # with the deadline clamp, costs exactly 0.05 s
```

---

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| `/api/tasks/` returns a bare JSON list | v10 paginates it into `{"count","next","previous","results"}` | paperless-ngx API v10 | **`poll_task`'s `isinstance(tasks, list)` is `False` — it never sees a terminal status** |
| Task `status` is `"SUCCESS"` / `"FAILURE"` | v10 uses `"success"` / `"failure"` / `"pending"` / `"started"` / `"revoked"` | paperless-ngx API v10 | `status in ("SUCCESS","FAILURE")` never matches |
| Task carries a `result` string | v10 carries `result_data: {"error_type", "error_message", "traceback"}` | paperless-ngx API v10 | OUTC-01's "the Paperless message" is in a different field |
| `task_name`, `type` | v10: `task_type`, `trigger_source` | paperless-ngx API v10 | not used by saneless |
| No version negotiation | `Accept: application/json; version=N`; **default is 10** when the header is absent; supported versions are 9 and 10; v9 support guaranteed for at least one year after v10's release | paperless-ngx API v10 | A client that sends no header gets v10 |

**Deprecated / outdated:**

- `img2pdf`'s `default_dpi` is **96**, not 72 — an image with no DPI metadata is laid out at 96 DPI.
  Any mental model that says "no layout function means 1 px = 1 pt" is wrong.
  [VERIFIED: `img2pdf.default_dpi` -> `96`; a 2480 px wide image with no layout fun -> 1860 pt]
- `paperless.py`'s `test_connection` docstring says it "distinguishes three states"; D-13 makes that
  five. The docstring and `docs/reference/web-api.md:55-56` are both claims this phase falsifies.

### Recommendation on the API version

**Pin `version=9` in the request headers *and* make `_extract_task` shape-tolerant.** Rationale:

- Pinning alone is the smallest correct change and makes the wire contract explicit and stable —
  the code already *assumes* v9, so pinning makes a hidden assumption visible. It is one line.
- Tolerating alone (no pin) works but leaves the client silently tracking whatever the server's
  configured default happens to be, which will one day become 11.
- Doing both costs ~15 lines and means the client keeps working when v9 support is eventually
  dropped (guaranteed for at least a year after v10's release, then "may be dropped"), without
  needing an emergency release.
- The *failure-message* extraction has to handle both fields anyway, because `result` is v9-only and
  `result_data` is v10-only; there is no single field that works for both.

**This is new information relative to the sixteen locked decisions.** None of D-01..D-16 anticipated
an API-version negotiation, and OUTC-07's "raises on any non-200" interacts with it: an *invalid*
version string makes paperless-ngx return **406 Not Acceptable**, which under D-12 would become an
immediate hard failure with a clear message — good, but only if the pinned version string is right.
Surface this to the user before planning rather than absorbing it silently.

---

## Rendering surfaces inventory (OUTC-02 / D-05)

Exactly four rendering sites plus one stylesheet:

| Site | Line | Today | Needs |
|------|------|-------|-------|
| `src/saneless/web/templates/partials/status.html` | 12-17 | `{% elif job.state == JobState.DONE %}` ... `{% elif job.state == JobState.ERROR %}` | A third terminal branch for `FALLBACK`, with the same `hx-get="/api/jobs/history"` refresh trigger the other two carry |
| `src/saneless/web/templates/partials/history.html` | 7-8 | `class="{% if job.state == JobState.DONE %}status-done{% elif ... %}"`, label via `{{ job.state \| state_label }}` | A `status-fallback` class arm; the label comes free from `state_label` |
| `src/saneless/web/static/app.css` | 12, 16 | `.status-done`, `.status-error` | `.status-fallback` (amber/warning) |
| `src/saneless/cli.py` | 262 | `f"{j.state.value}"` — prints the **raw enum value**, not the label | `FALLBACK` would print as `FALLBACK`, not "Saved to folder". Note this is a pre-existing inconsistency with the web UI; D-05 says "distinct in `saneless jobs` output", which the raw value technically already is. Recommend switching to `state_label(j.state)` for consistency and recording the user-visible change. |
| `src/saneless/cli.py` | 239 | JSON mode emits `"state": j.state.value` | `"FALLBACK"` appears automatically; consider adding `outcome` and `warning` to the JSON since the columns now have writers |

`src/saneless/web/app.py:92` registers the `state_label` filter; `JobState` is already exposed to
templates (used at `status.html:10`), so no new template globals are needed.

### Vocabulary completeness tests that will break (this is the intended mechanism)

| Test | Line | Why it breaks | Fix |
|------|------|---------------|-----|
| `test_job_state_has_exactly_seven_members` | `tests/test_vocabulary.py:50` | `assert len(list(JobState)) == 7` | -> 8, and rename the test |
| `test_terminal_states_membership` | `tests/test_vocabulary.py:133` | `frozenset({DONE, ERROR}) == TERMINAL_STATES` | add `FALLBACK` |
| `test_state_label_strings` | `tests/test_vocabulary.py:152-166` | hand-written `(state, expected)` list | add `(JobState.FALLBACK, "Saved to folder")` |
| `test_progress_label_strings` / `test_terminal_states_have_progress_prose_for_totality` | `tests/test_vocabulary.py:179-205` | the totality test names only DONE and ERROR | add a `FALLBACK` assertion matching whichever placeholder is chosen |
| `test_state_label_is_complete`, `test_progress_label_is_complete`, `test_state_classification` | parametrised over `list(JobState)` | will fail *at import/collection* until the `match` arms exist | no edit needed — these are the safety net working |
| `@_locked` reflective coverage | `tests/test_job.py:805-824` | a new public `finish_job` without `@_locked` fails | add the decorator |
| "no public method calls another public method" | `tests/test_job.py:826+` | `finish_job` must not call `update_state` | build one SQL statement in `finish_job` |

---

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | `POST /api/documents/post_document/` returns a **bare JSON string** (the task UUID), which is what `task_id = response.json()` at `paperless.py:157` assumes. The narrative docs say "with the UUID of the consumption task as the data", but the drf-spectacular schema that Context7 surfaced shows `{"task_id": "..."}`. The two disagree and the view source could not be located by name. | Pattern 5 | If the body is an object, `str(task_id)` becomes `"{'task_id': '...'}"` and every poll 404s/misses — but this is **existing behaviour**, unchanged by this phase, and would already be visibly broken. LOW risk, but worth a single `curl` against the user's server. |
| A2 | The user's paperless-ngx instance is recent enough to default to API v10. If they run an older release, `poll_task` works today and the version pin is a no-op improvement. | Pitfall 1 | None — pinning `version=9` is correct against both old (which has no versioning for tasks) and new servers. Older servers ignore the header. |
| A3 | `.part` is a safe extension for the paperless-ngx consume directory watcher (it ignores unknown extensions rather than attempting and failing to consume). | Pattern 4 | If paperless-ngx logs a noisy "unsupported file type" error per `.part` file, the atomicity fix trades one problem for log spam. A dotfile prefix (`.name.pdf.part`) is the more conservative choice — paperless-ngx's `inotify` consumer skips hidden files. Recommend the dotfile prefix. |
| A4 | "Saved to folder" is the `state_label` for `FALLBACK`; `progress_label` gets a matching placeholder. | § Rendering | Cosmetic only; D-05 fixes the phrase, the placeholder is discretion. |
| A5 | The `warning` text for a FALLBACK job is derived from `docs/explanation/consume-directory-fallback.md:62`'s existing sentence about title/tags/correspondent not being applied. | D-05 | Cosmetic; the exact wording is discretion. |
| A6 | 60 characters is a safe slug length cap. Based on `NAME_MAX` (255 bytes on ext4/overlayfs) minus the fixed prefix, with margin for eCryptfs's 143-byte limit. | Pattern 3 | A shorter cap is always safe; the risk is only aesthetic. |

---

## Open Questions

1. **Does the user's paperless-ngx serve API v9 or v10 by default?**
   - What we know: upstream docs state the default is 10; v9 and v10 are both supported; the
     `X-Api-Version` response header reports the server's version.
   - What's unclear: which release the user actually runs.
   - Recommendation: a one-line `curl -H "Authorization: Token ..." -i <url>/api/tags/ | grep -i x-api-version`
     answers it in seconds. Either way the recommended fix (pin + tolerate) is correct. **Raise this
     to the user before planning** — it may warrant its own requirement.

2. **Should the preservation guard's `except` be `Exception` (literal D-06) or `PaperlessError`?**
   - What we know: D-06 says "on any exception", but also explicitly rejects "a broad guard ... which
     would catch failures in code unrelated to delivery and report them in the job error as delivery
     failures". After OUTC-01/D-12, `upload_document` and `poll_task` raise only
     `PaperlessError`/`PaperlessTimeoutError`.
   - What's unclear: whether "any exception" was meant literally or as shorthand for "any failure of
     these two calls".
   - Recommendation: narrow to `PaperlessError` and record it as a deliberate reading of D-06's
     rationale. Flag to the user in the plan rather than choosing silently. (A `KeyboardInterrupt` or
     `MemoryError` mid-upload would not preserve under the narrow form — arguably correct, since a
     `BaseException` should not be caught anyway.)

3. **Is `paperless_task_timeout = 300` still right?** (Explicit discretion item.)
   - What we know: paperless-ngx consumption is asynchronous on Celery and can legitimately sit in
     `pending` for minutes under load; D-10 makes a timeout preserve the PDF rather than lose it, so
     the cost of a too-short timeout is a stray file, not a lost scan. The cost of a too-long timeout
     is a scanner-blocking worker thread (the worker processes one job at a time).
   - Recommendation: **keep 300 s.** Changing it in the same phase that makes timeouts fatal
     conflates two variables; if 300 s proves wrong the config key is already there. Record the
     reasoning; do not change the default.

4. **Should `finish_job` be one method or two?** D-04 describes one method that also handles the
   failure path (`plus error and error_category on the failure path`). A single method with six
   optional parameters is awkward under ruff's `PLR0913` (too many arguments — enabled via `PL`).
   - Recommendation: check whether `PLR0913`'s default max (5) is hit. If so, either take a
     `ScanResult` directly (`finish_job(job_id, result)` — clean, and `ScanResult` already carries
     outcome + three counts + warning) plus a separate `fail_job(job_id, error, category)`, or raise
     the limit. **Taking `ScanResult` is the better design** and keeps `job.py`'s import of
     `pipeline` out — except that would invert the dependency. Resolve in planning: a small
     `finish_job(job_id, *, state, outcome, warning, pages_scanned, pages_removed, pages_uploaded)`
     with keyword-only arguments reads well and `PLR0913` counts keyword-only args too, so verify
     against the linter before committing to a signature.

---

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| Python | everything | ✓ | 3.14.2 | — |
| `uv` | build/test | ✓ | project uses `uv_build` | — |
| `img2pdf` | OUTC-06 | ✓ | 0.6.3 | — |
| `pikepdf` | OUTC-06 test | ✓ (transitive) | 10.5.1 | regex over PDF bytes (rejected) |
| `Pillow` | page images | ✓ | 12.1.1 | — |
| `httpx` + `MockTransport` | OUTC-10 | ✓ | >=0.28.1 | — |
| `pytest` | all tests | ✓ | >=9.0.2 | — |
| `pytest-timeout` | CI gate | ✓ | 2.4.0 | — |
| `slopcheck` | package audit | ✓ | installed, no `--json` flag | human-readable output |
| SQLite with WAL | job store | ✓ | `PRAGMA journal_mode=WAL` -> `('wal',)` | — |
| A live paperless-ngx instance | confirming A2/Open Question 1 | ✗ | — | The pin+tolerate fix is correct without one; verification is a single `curl` the user can run |
| Docker | verifying D-15's `ENV` + volume mount end to end | not probed in this session | — | The compose/Dockerfile change is declarative and reviewable; a `docker build` is a nice-to-have, not a blocker |

**Missing dependencies with no fallback:** none.
**Missing dependencies with fallback:** a live paperless-ngx server (see Open Question 1) — the phase
is implementable without one; only the API-version *confirmation* needs it.

---

## Validation Architecture

### Test Framework

| Property | Value |
|----------|-------|
| Framework | pytest 9.0.2 (+ pytest-timeout 2.4.0, pytest-playwright 0.7.0) |
| Config file | `pyproject.toml` `[tool.pytest.ini_options]` |
| Quick run command | `uv run pytest tests/test_vocabulary.py tests/test_pdf.py tests/test_paperless.py -x -q` |
| Full suite command | `uv run pytest` |
| Global timeout | `timeout = 60`, `timeout_method = "signal"` |
| Strictness | `filterwarnings = ["error"]`, `xfail_strict = true`, `--strict-markers`, `--strict-config` |

### Phase Requirements -> Test Map

| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|--------------|
| OUTC-01 | `poll_task` raises `PaperlessError` on FAILURE carrying the Paperless message; raises `PaperlessTimeoutError` past the deadline | unit | `uv run pytest tests/test_paperless.py -k "poll_task_failure or poll_task_timeout" -x` | ✅ (`tests/test_paperless.py:250-300` — existing tests assert the *old* dict returns and must be rewritten) |
| OUTC-01 | Worker records ERROR, never DONE, on a raising pipeline | unit | `uv run pytest tests/test_worker.py -k finish -x` | ❌ Wave 0 — new `TestFinishJob` |
| OUTC-02 | `finish_job` persists `FALLBACK` + outcome + warning + three counts | unit | `uv run pytest tests/test_job.py -k finish_job -x` | ❌ Wave 0 |
| OUTC-02 | `state_label(FALLBACK) == "Saved to folder"`; classification; totality | unit | `uv run pytest tests/test_vocabulary.py -x` | ✅ (parametrised tests fail automatically; four hand-written lists need edits — see table above) |
| OUTC-02 | status partial + history table render FALLBACK distinctly | unit | `uv run pytest tests/test_web_state_rendering.py -x` | ✅ (file exists; needs a FALLBACK case) |
| OUTC-02 | `saneless jobs` renders FALLBACK distinctly | unit | `uv run pytest tests/test_cli.py -k jobs -x` | ✅ (needs a FALLBACK case) |
| OUTC-03 | duplex mismatch persists warning + outcome + counts | integration | covered by the OUTC-10 parametrised case | ❌ Wave 0 |
| OUTC-04 | PDF lands in `failed_dir` when upload raises; **and** when poll raises | integration | `uv run pytest tests/test_pipeline.py -k preserv -x` | ❌ Wave 0 — the poll-raises case is the one the roadmap note exists for |
| OUTC-04 | the raised error names the destination path | integration | same | ❌ Wave 0 |
| OUTC-05 | two jobs, same title -> two distinct filenames; sanitiser rejects `/`, `..`, control chars | unit | `uv run pytest tests/test_pdf.py -k name -x` | ❌ Wave 0 |
| OUTC-05 | consume-dir copy goes via `.part` and the final file is complete | unit | `uv run pytest tests/test_paperless.py -k consume -x` | ✅ (existing fallback test asserts the copied name; extend to assert no `.part` remains) |
| OUTC-06 | A4 @ 300 DPI -> `[0,0,595,842]` rounded | unit | `uv run pytest tests/test_pdf.py -k mediabox -x` | ❌ Wave 0 |
| OUTC-07 | non-200 raises immediately (not after the timeout); deadline includes request time; 200-with-empty-list keeps polling | unit | `uv run pytest tests/test_paperless.py -k poll -x` | ✅ file exists, cases missing |
| OUTC-08 | 2xx/401/403/404/5xx/ConnectError -> five distinct members; JSON strings unchanged for the original three | unit, parametrised | `uv run pytest tests/test_paperless.py -k connection -x` | ✅ file exists, needs parametrisation over `list(ConnectionStatus)` |
| OUTC-09 | `db_path`/`failed_dir` derive from `data_dir`; `validate_settings_dirs` rejects an unwritable `data_dir`; `JobStore` opens under a freshly created `data_dir` | unit | `uv run pytest tests/test_config.py -k data_dir -x` | ❌ Wave 0 |
| OUTC-10 | real worker + real pipeline + stub scanner through SUCCESS / FAILURE / TIMEOUT / fallback / duplex mismatch | integration, parametrised | `uv run pytest tests/test_outcomes_e2e.py -x` | ❌ Wave 0 — new file |

### Sampling Rate

- **Per task commit:** `uv run ruff check . && uv run ruff format --check . && uv run pytest <touched test module> -x -q`
- **Per wave merge:** `uv run pytest && uv run ty check && uv run pyrefly check src tests`
- **Phase gate:** full suite green + both type checkers clean before `/gsd-verify-work` (this is the
  Phase 20 CI gate's exact contract).

### Wave 0 Gaps

- [ ] `tests/test_outcomes_e2e.py` — new file, covers OUTC-10 (and transitively OUTC-01/02/03/04)
- [ ] `tests/test_pdf.py` — add MediaBox and filename-uniqueness/sanitisation classes (OUTC-05, OUTC-06)
- [ ] `tests/test_job.py` — add `TestFinishJob` (OUTC-02, D-04)
- [ ] `tests/test_config.py` — add `data_dir` / `db_path` / `failed_dir` / writability cases (OUTC-09)
- [ ] `tests/test_pipeline.py` — add preservation-guard cases for **both** the upload-raises and
      poll-raises paths (OUTC-04)
- [ ] `tests/test_vocabulary.py` — update four hand-written lists (the parametrised ones self-update)
- [ ] `tests/test_paperless.py` — rewrite the three `TestPollTask` cases from return-value to raise
      semantics; add non-200, empty-list-tolerance, and API-shape cases; parametrise `test_connection`
- [ ] `uv add --dev pikepdf` — promote the transitive dependency (Pitfall 8)
- [ ] **No new conftest fixtures strictly required** — `default_settings`, `mock_scanner`,
      `sample_pil_images`, and `multi_page_images` already exist. A `wait_for_state(store, job_id,
      state, timeout)` polling helper is worth adding to `tests/conftest.py` (the existing worker
      tests use a flat `time.sleep(0.5)` 20+ times; ROBU-03 formalises this in Phase 26, but OUTC-10
      needs it now and a shared helper avoids a 21st sleep).

### Notes on making OUTC-10 fast — recommendation

**Use the seams that already exist. Add none.** Measured costs in this repository:

| Lever | Cost today | Cost with the lever |
|-------|-----------|---------------------|
| upload retries (consume-dir fallback case) | 3.00 s with `max_retries=3` | **0.00 s** with `max_retries=1` [VERIFIED: measured 3.00 / 1.00 / 0.00 for 3 / 2 / 1] |
| poll timeout case | `0.5 s` minimum (the unconditional first sleep) | `paperless_task_timeout` exactly, once `time.sleep(min(delay, remaining))` clamps — set it to `0.05` |
| SUCCESS / FAILURE / duplex cases | 0 s (terminal status on the first poll, before any sleep) | unchanged |

Total for all five parametrised cases: well under one second of sleeping. That is faster than what
monkeypatching `time.sleep` would give, because it needs no patch at all, and it leaves the real
sleep in place so the clamp itself is exercised.

**Why not the alternatives:**

- *Monkeypatch `time.sleep` in `saneless.paperless`* — would disable the deadline clamp the test is
  supposed to prove, and Phase 32 (M-34) owns this sweep anyway.
- *Inject a `sleep_fn` parameter* — a new production seam that exists only for tests, for a problem
  two existing parameters already solve.
- *Make the backoff base configurable* — same objection, plus a new config key with no user story.

**Worker driving:** `ScanWorker` runs the pipeline on a background thread. The test should submit a
job, poll `store.get_job(job_id)` until `state in TERMINAL_STATES` with a short overall budget
(~2 s), then `worker.stop()`. Do **not** monkeypatch `saneless.worker.run_pipeline` — OUTC-10
explicitly requires the real one. Stub only the scanner (a `MagicMock(spec=ScannerBackend)` whose
`scan_pages` returns an iterator of PIL images — the `mock_scanner` fixture's exact shape) and the
HTTP transport.

**Duplex mismatch case:** `_is_manual_duplex(profile.source)` is a substring test for "manual" and
"duplex" (`pipeline.py:182-184`), so a profile with `source = "Manual Duplex"` plus a scanner stub
returning an odd number of pages across the two passes drives that branch. The stub must also honour
`flip_event` — check `_scan_manual_duplex`'s contract before writing the stub.

---

## Security Domain

### Applicable ASVS Categories

| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V2 Authentication | no | No auth surface changes in this phase (Phase 30 owns the owner-token work) |
| V3 Session Management | no | — |
| V4 Access Control | no | — |
| V5 Input Validation | **yes** | The user-supplied job **title** becomes part of a **filesystem path** for the first time (D-09). An `[A-Za-z0-9]` allow-list slug is the control. This is the single new attack surface this phase creates. |
| V6 Cryptography | no | No crypto. `uuid4` job ids are used for uniqueness, not for unguessability. |
| V12 File and Resource | **yes** | Two new write destinations: `<data_dir>/failed/` and the `.part` file in `consume_dir`. Both must write under a directory derived from config, never from user input, and both must have their parent validated at startup (D-16's `validate_settings_dirs` addition). |
| V14 Configuration | **yes** | `SANELESS_OUTPUT__DATA_DIR` is a new env-controlled path baked into the image. D-15's rationale (image runs as root, unset default lands in `/root/.local/state`) is itself a configuration-security argument. |

### Known Threat Patterns for this stack

| Pattern | STRIDE | Standard Mitigation |
|---------|--------|---------------------|
| Path traversal via job title (`../../etc/cron.d/x`) reaching `failed_dir / name` or `consume_dir / name` | Tampering | Allow-list slug (`[A-Za-z0-9]` only), then assert the resolved path's parent is the intended directory. **Do not** reuse `auto_profiles._slugify` — it passes `/` and `.` through. |
| Absolute-path title (`/etc/passwd`) — `Path("/a") / "/etc/passwd"` resolves to `/etc/passwd` | Tampering | Same allow-list; `/` is stripped so the segment can never be absolute |
| NUL byte in title truncating a path at the C layer | Tampering | Allow-list strips it; Python also raises `ValueError: embedded null byte`, but do not rely on that as the only control |
| Name length overflow -> `OSError: [Errno 36] File name too long` on a path the user controls | DoS | 60-char slug cap |
| Symlink in `failed_dir` redirecting a preserved scan outside the volume | Tampering | `failed_dir` is operator-controlled, not attacker-controlled; out of scope, but do not follow symlinks when creating it (`mkdir(exist_ok=True)` on an existing symlink-to-directory succeeds silently) |
| Paperless token leaking into a preserved-file error message or a job `error` column | Information Disclosure | The preservation error interpolates `str(exc)` and a path. `PaperlessError` messages from `upload_document` already include `exc.response.text` (`paperless.py:182-186`) — a paperless error body could echo a header. CFG-05 (Phase 27) makes the token a `SecretStr`; this phase should at minimum not *add* a new path for it. Verify the new `poll_task` non-200 message (`response.text`) does not echo request headers. |
| Half-written PDF consumed by paperless-ngx | Integrity | The `.part` + `os.replace` pattern is the control (OUTC-05) |

---

## Project Constraints (from CLAUDE.md)

Directives the planner must verify compliance against:

- **Python 3.14** only. PEP 758 bracketless `except A, B:` is valid syntax and must not be flagged.
- **`uv`** for all package and env operations. Never `pip`, `poetry`, or `conda`. Adding pikepdf is
  `uv add --dev pikepdf`.
- **`prek`**, not `pre-commit`: `uv run prek run`.
- **All five gates must pass with zero errors or warnings:** `uv run ruff check .`,
  `uv run ruff format .`, `uv run ty check`, `uv run pyrefly check src tests`, and the test suite.
- **No suppression.** No `# type: ignore`, no `# noqa`, no disabling rules. Relevant traps this phase
  will hit: `ANN` (annotate everything, including the new `finish_job` keyword-only parameters),
  `D` (docstrings on every public function — `finish_job`, `job_state_for`, `ConnectionStatus`,
  `PaperlessTimeoutError`, the sanitiser), `DTZ` (no naive `datetime.now()`), `PTH` (use `pathlib`,
  not `os.path` — note `shutil.move` and `os.replace` accept `Path` and are not `PTH` violations),
  `EM` (no string literal directly in `raise X("...")` — assign to `msg` first, as every existing
  raise site does), `S` (bandit — `S108` hardcoded temp dir may fire on new `/tmp` literals),
  `PLR0913` (too many arguments — see Open Question 4), `TRY`/`RSE` (no `raise X(...) from None`
  patterns), `RET` (no unnecessary `else` after `return`).
- **`ty` and `pyrefly` may disagree** on the same code; both must pass. Watch `shutil.move`'s
  `Any` return type and `StrEnum` narrowing in the `match` statements.
- **Prefer external packages over reimplementing.** Reflected in § "Don't Hand-Roll".
- **Use Context7 for library docs, not training data.** Used for paperless-ngx; img2pdf was verified
  by direct source inspection of the installed package, which is stronger.
- **Playwright MCP for all browser validation. Never mark a browser check "manual-only".** The
  `FALLBACK` rendering in the status partial and history table (OUTC-02) is a browser-verifiable
  change: `tests/test_browser.py` exists and is marked `@pytest.mark.browser`. The plan must include
  a Playwright assertion that a FALLBACK job renders with the warning class and the "Saved to folder"
  label — not a human-verification checkpoint.
- **MCP servers available:** Serena (semantic navigation), Context7 (docs), Tavily (search),
  Playwright (browser).

---

## Sources

### Primary (HIGH confidence)

- **Direct execution in this repository's `uv` environment** — every claim tagged `[VERIFIED]`:
  `img2pdf.get_fixed_dpi_layout_fun` signature and source; `img2pdf.default_dpi == 96`; MediaBox
  values for four input shapes; `pil_get_dpi` source; `crop_to_paper_size(...).info == {}`; PNG DPI
  round-trip -> `299.9994`; `sqlite3.connect` into a missing directory; WAL sidecar file lifecycle;
  `shutil.move` behaviour across five input shapes; `shutil.move` source; same-directory
  `os.replace`; cross-device `os.replace` -> `EXDEV`; upload retry sleep costs at
  `max_retries` 1/2/3; `pikepdf.__version__ == 10.5.1`.
- `/paperless-ngx/paperless-ngx` (Context7) — `PaperlessTask.Status` choices, `COMPLETE_STATUSES`,
  `TaskSerializerV9` / `TaskSerializerV10` field lists, `task_failure_handler`'s `result_data` shape.
- `https://raw.githubusercontent.com/paperless-ngx/paperless-ngx/dev/src/documents/serialisers.py` —
  `TaskSerializerV9.get_result` and `_STATUS_TO_V9`, read in full.
- `https://raw.githubusercontent.com/paperless-ngx/paperless-ngx/dev/src/documents/views.py` —
  `TasksViewSet.get_serializer_class` and `TasksViewSet.paginate_queryset`, the authoritative source
  for the v9/v10 pagination split.
- `https://raw.githubusercontent.com/paperless-ngx/paperless-ngx/dev/docs/api.md` §§ "POSTing
  documents", "API Versioning", "Version 10" — the default-version-is-10 statement and the tasks
  pagination changelog entry.
- **This repository's own source**, read directly: `pipeline.py`, `paperless.py`, `worker.py`,
  `pdf.py`, `vocabulary.py`, `job.py`, `config.py`, `cli.py`, `web/app.py`, `web/routes.py`,
  `web/templates/partials/*.html`, `paper_sizes.py`, `exceptions.py`, `auto_profiles.py`,
  `pyproject.toml`, `uv.lock`, `Dockerfile`, `docker-compose.yml`, and `tests/`.
- `slopcheck install pikepdf` -> `[OK]`; `pip index versions pikepdf`.

### Secondary (MEDIUM confidence)

- The `post_document` response shape (A1) — the narrative docs and the drf-spectacular schema
  disagree; neither was cross-checked against a live server.

### Tertiary (LOW confidence)

- A3: paperless-ngx's consumer ignoring `.part` files. Inferred from the general design of
  directory-watching consumers, not read from the consumer source. The dotfile-prefix
  recommendation makes this moot.

---

## Metadata

**Confidence breakdown:**

- **Standard stack: HIGH** — no new packages; every version read from `uv.lock` and confirmed by
  importing it in the project venv.
- **Architecture: HIGH** — every pattern is an extension of a precedent that already exists in this
  codebase, with the file and line of the precedent cited.
- **img2pdf / DPI (OUTC-06): HIGH** — API, arithmetic, and the mixed-DPI behaviour were all executed,
  not inferred. Two numbers in the existing planning docs are corrected as a result.
- **Test speed (OUTC-10): HIGH** — the 3.00 / 1.00 / 0.00 second measurement is direct.
- **`shutil.move` / `os.replace` / WAL (OUTC-04, OUTC-05, OUTC-09): HIGH** — executed, plus source
  read for `shutil.move`.
- **paperless-ngx task semantics (OUTC-01, OUTC-07): HIGH for the wire contract** (read from
  upstream `views.py`, `serialisers.py`, `models.py`, `docs/api.md`); **MEDIUM for which version the
  user's server serves** (Open Question 1) and **MEDIUM for the `post_document` body shape** (A1).
- **Pitfalls: HIGH** — every one is either executed or read from source; none is from memory.

**Research date:** 2026-09-11
**Valid until:** 2026-10-11 (30 days). Shorter for the paperless-ngx section — API v10 is actively
evolving and the v9 deprecation clock is running; re-verify the version pin if this phase slips more
than a few weeks.
