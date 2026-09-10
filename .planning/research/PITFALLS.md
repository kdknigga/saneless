# Pitfalls Research

**Domain:** Hardening a working Python 3.14 scanner-to-paperless-ngx bridge (FastAPI + HTMX, python-sane, worker thread + `queue.Queue`, SQLite job store, Click CLI, Docker, GitHub Actions) against a 99-finding code review
**Researched:** 2026-09-09
**Confidence:** HIGH (most pitfalls verified by reading this repository's own source; library semantics verified via Context7)

## Scope note

This document is **not** about scanner-bridge domain pitfalls — those were researched for v1.0 and the code already embodies them. It is about the mistakes that get made when **applying these specific remediations to a system that currently works**. Every entry names the review finding it protects (`C-`, `M-`, `N-`, `U-` IDs) and the remediation step from review section 10 (steps 1–11) that should own it.

Roadmap phases continue from 20. Where this document says "Step N" it means review section 10's ordered plan, which the roadmap is expected to follow one-step-per-phase or thereabouts.

---

## Critical Pitfalls

### Pitfall 1: The C-03 raises land inside the `TemporaryDirectory` that C-04 is supposed to fix

**Findings:** C-03, C-04, M-22 · **Step:** 1

**What goes wrong:**
C-03 says "in `poll_task`, raise `PaperlessError` on `FAILURE`, and raise on timeout." `poll_task` is called from `run_pipeline` inside `with tempfile.TemporaryDirectory()` (`pipeline.py:354`, `:415-429`). The moment that raise is added, a *new* data-loss path opens that did not exist before: paperless-ngx rejects a duplicate, the task goes `FAILURE`, `poll_task` raises, the `with` block unwinds, and the PDF is deleted. Before the fix the user got a wrong green tick; after a half-applied fix they get a correct red error **and no document**. The regression is invisible because nobody was measuring "PDF survives" before.

**Why it happens:**
C-03 and C-04 read as two independent bullets in the same step. C-04's example code only wraps `upload_document`. `poll_task` is a *different* call site, later in the flow, and its failure mode is new.

**How to avoid:**
Write the preservation as a single `try/except PaperlessError` that spans **both** the upload and the poll, not a wrapper around `upload_document`. Land C-04's `failed/` preservation *before* C-03's new raises within the step, and make the very first test of the step "upload succeeds, poll returns FAILURE, assert a PDF exists on disk and the error names its path."

**Warning signs:**
- The diff adds a `raise` in `paperless.py` and the only new `except` is around `upload_document`.
- A test asserts `JobState.ERROR` for the FAILURE case but never asserts a file exists.
- `git grep -n "TemporaryDirectory" src/saneless/pipeline.py` still shows every failure path inside the block.

---

### Pitfall 2: "Preserved" PDFs are written to a directory the OS deletes

**Findings:** C-04, N-39, M-03, U-08 · **Steps:** 1 (write), 5/7 (location and mount)

**What goes wrong:**
C-04's suggested `failed_dir = Path(settings.output.tmp_dir) / "failed"` puts the rescued document under `tmp_dir`, which defaults to the system temp directory (`config.py:85`). `systemd-tmpfiles` and most distributions clear that on reboot or after 10 days (N-39 flags this for `saneless.db` already). In Docker it is worse: `tmp_dir` is inside the container's writable layer and the documented update procedure (`docker compose pull && up -d`) destroys it, and the recommended compose file mounts no volume for it at all (U-08). The feature reports "your scan is preserved at /tmp/saneless/failed/…" and then the file is gone.

**Why it happens:**
`tmp_dir` is the only output path the code already knows about, so it is the path of least resistance. The durability requirement is new and lives in a different finding.

**How to avoid:**
Introduce `output.state_dir` (N-39) in the same milestone and derive **both** `saneless.db` and `failed/` from it, defaulting under `$XDG_STATE_HOME` (M-20). In the compose template, put `failed/` and the database on the named `saneless-data` volume and the consume directory on a real bind mount (U-08), and have the status strip say "Fallback: not configured" when it is not (U-03).

**Warning signs:**
- Any new durable artefact whose path starts from `settings.output.tmp_dir`.
- `docker compose down -v` (or a plain image update) loses job history — test it once, deliberately.
- The status strip has no row for the fallback directory.

---

### Pitfall 3: Adding `JobState.FALLBACK` renders a blank, non-polling status area

**Findings:** C-03, M-05, C-10 · **Step:** 1 (enum) + 4 (single state source)

**What goes wrong:**
`partials/status.html` is an `{% if %}/{% elif %}` chain over seven literal state strings **with no `{% else %}`**, and its polling trigger is gated on a hard-coded `active_states` list at line 1. Add `JobState.FALLBACK` in Step 1 and the terminal FALLBACK job matches no branch: the status area renders as an empty `<div>`, the poll trigger is absent (correctly, it is terminal), the history-refresh hook that the DONE and ERROR branches carry is missing, and — under C-10's out-of-band button (Step 4) — the button may never come back. The user sees a blank page after a scan that actually succeeded into the consume directory.

**Why it happens:**
The state name is added to the Python enum and the worker mapping. The three template copies of the state list (`index.html:57-59` twice, `status.html:1`) and `app.js` are exactly the "seven places across five files" M-05 warns about, and nothing type-checks them.

**How to avoid:**
Do M-05's consolidation (`ACTIVE_STATES: frozenset[JobState]`, `TERMINAL_STATES`, label attached to the enum, `dict[JobState, str]` label maps so ty/pyrefly flag a missing member) **as a prerequisite** to adding any new state, or at minimum in the same commit. Add an `{% else %}` fallback branch that renders the humanized state rather than nothing. Add M-05's suggested test: every `JobState` member has a label, and every member is in exactly one of active/terminal.

**Warning signs:**
- `git grep -n '"PENDING"' src/saneless/web` returns more than one file after Step 4.
- A new enum member is added without a template diff.
- `humanize_state` still silently returns the raw value for unknown input (`test_humanize_state_filter_unit` currently pins that behaviour — it must be deleted, not preserved).

---

### Pitfall 4: The `"fallback"` sentinel has readers you did not grep for

**Findings:** C-03, M-33 · **Step:** 1

**What goes wrong:**
`upload_document` returns the string `"fallback"` in place of a task id. Replacing it with `UploadResult(task_id, fallback_path)` changes a `str` return to an object. Every silent consumer breaks or, worse, keeps working incorrectly: anything that did `if task_id:` now gets a truthy object in *all* cases including failure; anything that formatted the id into a log line or a job record now writes `UploadResult(task_id=None, ...)`; tests that assert `== "fallback"` turn red and the tempting fix is to update the literal rather than assert the new semantics.

**Why it happens:**
The sentinel is a `str` in a dynamically-typed seam. Neither ty nor pyrefly will flag a caller that only truth-tests the value, and both current callers discard the return entirely (C-03), so the compiler has nothing to complain about.

**How to avoid:**
Change the return type annotation first and let the type checkers point at every site; then search for the string separately (`git grep -n '"fallback"'` across `src/` **and** `tests/` **and** `docs/`). Make the new type non-truthy-ambiguous: no `__bool__`, and force callers to branch on `result.fallback_path is not None`. Give `mock_paperless` a `spec=PaperlessClient` (M-33) so a renamed method is caught. Assert **persisted job state**, never the returned object.

**Warning signs:**
- A test diff that only changes a string literal.
- `run_pipeline` still annotated `-> dict`.
- Docs (`docs/explanation/consume-directory-fallback.md:66`) still describe the old sentinel.

---

### Pitfall 5: The manual-duplex compat shim is applied in the pipeline instead of at config load

**Findings:** C-01, M-09, M-18, M-24, N-07 · **Steps:** 2 (field) + 5 (config strictness)

**What goes wrong:**
Existing users have `source = "Manual Duplex"` in their TOML because the how-to told them to. Adding `duplex: Literal["none","hardware","manual"]` and translating the legacy string *inside `run_pipeline`* leaves four other readers of `profile.source` unchanged: `worker.py:206-209`'s duplicate detection (N-07), `auto_profiles.is_bare_default` (M-04), the profile dropdown label (U-04), and `classify_source` (C-06). Each will disagree about what that profile is. Meanwhile the CLI's `--profile` path may bypass the pipeline translation entirely.

**Why it happens:**
C-01's own suggested snippet does the translation at `ScanSettings` construction, which is the narrowest possible place. That is right for the *device value* and wrong for the *strategy*.

**How to avoid:**
Do the translation once, in a pydantic `model_validator(mode="before")` on `ProfileConfig`: if `source` matches the manual-duplex marker, set `duplex="manual"`, rewrite `source` to `"ADF"`, and emit a deprecation warning naming the profile and the file. After the validator, `profile.source` is a pure SANE value everywhere and `profile.duplex` is the only strategy switch. Then delete the string rule from `pipeline.py:94-96` and `worker.py:206-209` (N-07) — do not leave a "belt and braces" copy.

**Warning signs:**
- `git grep -in "manual" src/saneless/` returns more than one site after Step 2.
- No test loads a legacy `source = "Manual Duplex"` config and asserts `duplex == "manual"` and `source == "ADF"`.
- `--force` profile regeneration (M-09) does not write the `duplex` key, so refreshed profiles silently revert to `"none"`.

---

### Pitfall 6: `extra="forbid"` on nested models turns typos in *running* deployments into startup crashes, and the existing error renderer names the wrong thing

**Findings:** M-18, M-24, C-08, U-01 · **Step:** 5

**What goes wrong:**
Three distinct failures.

1. **The error message lies.** `_build_settings` builds its message from `err["loc"][0]` (`config.py:171-181`). Today `loc` is `("tokne",)` for a top-level extra. With nested `extra="forbid"`, a typo under `[paperless]` produces `loc == ("paperless", "tokne")`, so the code will print *"Unknown config section 'paperless'. Valid top-level sections: scanner, paperless, output, profiles. Did you mean [profiles.paperless]?"* — it names a valid section as unknown and offers nonsense advice. This is strictly worse than the silence it replaces.
2. **Env vars become fatal.** pydantic-settings deep-merges `(init, env, toml)`. For a known top-level field, `SANELESS_PAPERLESS__*` is collected into a nested dict and handed to `PaperlessConfig`. Any stray or misspelled `SANELESS_PAPERLESS__X` in a user's `docker-compose.yml` — previously ignored — now **prevents the container from starting**, with the misleading message from (1). Note the asymmetry: an unknown *top-level* `SANELESS_FOO` is still ignored, because the env source only looks up known field names. So M-18 does not actually catch every config typo, and claiming it does in the docs would be a new false claim. *(MEDIUM confidence on the exact env-source behaviour; verify with a two-line test before writing docs.)*
3. **M-24 + M-18 is a breaking change.** If M-24 is resolved by *deleting* `default_title_template` (alias `title`), every config containing the documented `title = "Receipt"` line stops loading, hard, at startup. Deleting a documented key and forbidding extras in the same step is a config-file guillotine.

**Why it happens:**
`extra="forbid"` is a one-line change with a large blast radius, and its blast radius is other people's files, which are not in the repository.

**How to avoid:**
- Rewrite `_build_settings` to render the **full `loc` path** and to derive valid keys from `model_fields` of the model at that path (M-18 already says this — treat it as mandatory, not optional). Delete `_VALID_SECTIONS`.
- Emit the `[profiles.X]` hint only when `X` is not a case-insensitive match of a real section.
- Prefer **implementing** `title` over deleting it (M-24). If you delete a key, keep it as a deprecated field that warns for one release rather than a hard failure.
- Add tests for: nested typo, key in the wrong section, unknown env sub-key, legacy `title` key, and a config written by `write_profiles_to_config` (C-08's round-trip).

**Warning signs:**
- The `extra="forbid"` diff does not touch `_build_settings`.
- No test asserts the *text* of the error for a nested typo.
- `saneless.toml.example` or the docs contain any key not present in the models — run a "every documented key exists in `model_fields`" test.

---

### Pitfall 7: Everything `auto_profiles` writes must now be loadable, and `--force` must own exactly the keys it generates

**Findings:** C-08, M-09, M-18, M-10 · **Step:** 5

**What goes wrong:**
Under `extra="forbid"`, the writer and the reader become tightly coupled: any key `write_profiles_to_config` emits that is not a `ProfileConfig` field bricks the next start (this is C-08 with a sharper edge). Separately, the M-09 fix — "update in place on the existing tomlkit table" — has to decide which keys it owns. Own too few and a profile refreshed for a new scanner keeps a stale `duplex`/`paper_size`; own too many and the user's `default_tags` vanish, which is the bug M-09 is fixing.

**Why it happens:**
"Generated keys" is currently implicit — it is whatever the writer happens to write. Nothing declares the set.

**How to avoid:**
Declare it explicitly: `_GENERATED_KEYS = frozenset({"source", "resolution", "mode", "duplex", "auto_generated"})` in one place, use it in the merge, and assert in a test that it is a subset of `ProfileConfig.model_fields`. Under `--force`, update only those keys, on the existing table, and skip tables whose on-disk `auto_generated` is not `true` (M-09). Add the C-08 round-trip test — `write_profiles_to_config` then `load_settings` — parametrised over flatbed-only, feeder-only, and mixed source lists.

**Warning signs:**
- A test asserting `default_tags` survives `--force` does not exist.
- The generated-keys set is spelled out in more than one function.
- `auto_generated` is written but never read at merge time.

---

### Pitfall 8: The atomic config write (`os.replace`) fails on the *documented* Docker mount

**Findings:** M-10, M-30, M-04 · **Steps:** 5 (write) blocked by 7 (mount) — **ordering hazard**

**What goes wrong:**
M-10's fix is "write `config.toml.tmp`, then `os.replace`". The shipped `docker-compose.yml` bind-mounts the **file** (`./config.toml:/etc/saneless/config.toml:ro`). Renaming over a bind-mounted file is not permitted on Linux — the target is a mount point, and `rename(2)` returns `EBUSY` ("Device or resource busy"). Even with the `:ro` removed, the atomic write is the one write pattern that cannot work through a file bind mount. M-30's fix (mount the *directory*) is what makes M-10 viable, and M-30 sits in Step 7 while M-10 sits in Step 5. Apply them in review order and the "durable config write" ships broken for the deployment the docs recommend, with a new failure mode (`OSError: [Errno 16]`) replacing the old one. *(MEDIUM-HIGH confidence — well-established Linux bind-mount behaviour; confirm with a 30-second `docker run -v ./x.toml:/etc/x.toml` experiment before planning around it.)*

**Why it happens:**
The two findings are filed under different areas (Configuration vs Delivery) and the dependency runs backwards through the recommended order.

**How to avoid:**
Pull M-30's "mount the directory, not the file" forward into the same phase as M-10, or split it out and do it first. Make the atomic writer catch `OSError` and raise a `ConfigError` that says *"cannot replace {path}; if this file is bind-mounted into a container, mount its directory instead"* — the message is the fix. Also preserve mode bits and, in containers, be aware the file may be owned by a different UID than the process (a token file at 0600 owned by host-uid 1000 is unwritable by a container running as root-in-userns).

**Warning signs:**
- Step 5 lands and the compose file still shows a `:/etc/saneless/config.toml` file mount.
- A `.toml.tmp` file left behind in a user's config directory.
- No integration test that runs `auto-profiles --force` against a path under a directory the test made read-only.

---

### Pitfall 9: The `JobStore` lock introduces a re-entrancy trap and does not fix shutdown

**Findings:** C-07, C-09, M-03 · **Step:** 4

**What goes wrong:**
Four distinct traps in one small fix.

1. **Nested `with self._conn`.** `RLock` is re-entrant, so a public method calling another public method succeeds — but `with self._conn` is **not** re-entrant in any useful sense: the inner exit commits, ending the outer method's transaction early. Two "atomic" updates silently become one-and-a-half.
2. **Per-thread connections change the failure mode, they do not remove it.** If you choose `threading.local()` connections instead of a lock, the two threads now contend for real: `sqlite3.OperationalError: database is locked` after the default 5-second busy timeout. WAL (already enabled at `job.py:89`) makes readers non-blocking but writers still serialise, and WAL needs a shared-memory `-shm` file, which does not work on NFS/CIFS-backed bind mounts — a realistic home-NAS deployment. If you go this route you must pass `timeout=` explicitly and document "database on a local volume".
3. **`prune()` in the per-job `finally` under the lock.** A `prune` over 500 rows while a browser polls every second is the exact contention window C-07 describes. The review already says move `prune` out of the job path — do it in the same commit, not later.
4. **A lock does not prevent `Cannot operate on a closed database`.** That is M-03's shutdown ordering (lifespan closes the store while the daemon worker is mid-scan). C-07's lock makes the concurrency test pass and leaves the real production stranding bug in place.

**Why it happens:**
"Add a lock" reads as complete. The transaction semantics of `with connection` and the lifetime semantics of `close()` are separate concerns that look like the same concern.

**How to avoid:**
Public methods take the lock and call **private, lock-free** `_impl` helpers; never call a public method from a public method. One `with self._conn` per public method, at the top level. Keep the C-07 two-thread stress test (200 rounds, 10 runs) as a permanent test. Add a shutdown test: submit a job, stop the app mid-job, assert no `Cannot operate on a closed database` and that the row ends `ERROR`, not stranded.

**Warning signs:**
- `git grep -n "self\._lock" src/saneless/job.py` shows the lock taken in a method that calls another locked method.
- `close()` has no guard/flag and no "closed" sentinel that raises a project exception rather than `sqlite3.ProgrammingError`.
- The two-thread test is marked `flaky` or given a retry.

---

### Pitfall 10: The `async def` → `def` conversion removes accidental serialisation from shared mutable state

**Findings:** M-01, M-04, C-07, N-21 · **Step:** 4

**What goes wrong:**
Today every route body runs on one event-loop thread, so `MetadataCache`'s dict, `settings.profiles`, and `worker._current_job_id` are only ever touched by one request at a time. `def` routes run in the AnyIO threadpool (default **40** threads, verified), so after M-01 they run genuinely concurrently. Three concrete consequences:

- **Cache stampede.** `_get_cached_or_fetch` (`routes.py:26-60`) does check-then-fetch-then-set with no lock. With paperless slow, ten open tabs' `hx-trigger="load"` selects all miss, all issue a 30-second blocking `httpx` call, and ten threadpool threads are parked. M-01's "cache the unreachable outcome" must include a **single-flight lock**, not just a TTL.
- **`RuntimeError: dictionary changed size during iteration`.** M-04 has the worker thread mutating `settings.profiles` while the index template iterates it. Under `async def` the interleaving window was tiny; under `def` it is a genuine race across 40 threads. M-04's "merge under a lock" and "move generation to startup" are load-bearing for M-01, not optional polish.
- **No cancellation.** `run_in_threadpool` does not interrupt a running sync function on client disconnect. A user closing the tab during a 60-second paperless timeout keeps the thread. This is fine at 40 threads and one scanner; it is worth one comment so nobody assumes otherwise.

**Why it happens:**
M-01 is advertised as "no other code changes are needed", which is true for correctness of *blocking* and false for correctness of *sharing*.

**How to avoid:**
Convert routes and add locks to the cache and the profiles dict in the same commit. Put a comment on the router explaining why the handlers are `def` (M-01 asks for this — it also stops a future contributor "fixing" it back). Pin the deployment to **one uvicorn worker**: multiple workers means multiple worker threads, multiple in-memory queues, two processes writing one SQLite file, and `fail_active_jobs` (M-03) in process B killing process A's live job. Assert it in `serve` and say it in the Docker reference.

**Warning signs:**
- The M-01 diff touches only `async def` keywords.
- `docs/reference/docker.md` or the compose file mentions `--workers` or gunicorn.
- `test_flip_continue` still takes 2.00 s after M-02 removes `wait_transition` — that means the blocking wait is still there.

---

### Pitfall 11: htmx does not swap 4xx responses, so the 429 back-pressure is invisible

**Findings:** C-09, C-10, U-06 · **Step:** 4

**What goes wrong:**
htmx 2's default `responseHandling` is `[{"code":"204","swap":false},{"code":"[23]..","swap":true},{"code":"[45]..","swap":false,"error":true}]` (verified against htmx 2.0.4 docs). So when `POST /api/scan` returns **429** (queue full) or **503** (worker dead), htmx swaps **nothing**: no error message, no status update, and — critically — the `hx-swap-oob` Scan button in the response body is *not* processed either, because OOB processing only happens on a swapped response. The user clicks Scan, sees the button flicker, and nothing happens. This is C-10 all over again, reintroduced by the fix for C-09.

**Why it happens:**
"Return 429" is server-side thinking. The HTMX default is to treat 4xx as a programming error, not as content.

**How to avoid:** Pick one, deliberately:
- **Preferred:** add a `htmx-config` meta with `{"code":"429","swap":true},{"code":"503","swap":true}` alongside the defaults, and have those responses render the normal status partial plus the OOB button plus a plain-language message ("The scanner is busy — 3 jobs ahead of you", per U-06).
- Or return **200** with an error partial and set the real status only on the JSON API. Ugly for API consumers; acceptable if documented.
- Do **not** rely on a global `htmx:beforeSwap` handler — Step 4 is deleting `app.js` (C-10) and reintroducing a JS file for this recreates the two-sources-of-truth problem C-10 diagnosed.

Add a browser test: fill the queue, click Scan, assert a visible message and an enabled button.

**Warning signs:**
- A 429 path with no template.
- `htmx:responseError` in the browser console during any manual test.
- No `htmx-config` meta and no `responseHandling` entry after Step 4.

---

### Pitfall 12: The out-of-band Scan button duplicates an `id` and swallows the double-submit guard

**Findings:** C-10, M-05, U-06 · **Step:** 4

**What goes wrong:**
`index.html` renders `<button id="scan-btn">` inside the form **and** `{% include "partials/status.html" %}` at line 63. If the OOB button markup is added to `status.html`, the full page render emits **two elements with `id="scan-btn"`** — invalid HTML, and htmx/`getElementById` will pick whichever comes first, so the fix silently targets the wrong button. Two more traps:

- The status partial is swapped with `hx-swap="outerHTML"` on `#status-area`. An OOB element nested *inside* `<div id="status-area">` is part of the swapped content, so you get the duplicate-id problem again in the live DOM. The OOB button must be a **sibling at the top level of the response fragment**, outside `#status-area`.
- Any route that returns the status partial into a DOM where `#scan-btn` does not exist fires `htmx:oobErrorNoTarget` (verified event exists in htmx 2). The history-refresh partial and any direct `GET /api/jobs/current/status` in a test will hit this.
- Deleting `app.js` also deletes `disableScanButton()` on `htmx:beforeRequest`. Between the click and the first 1-second poll there is a window where the button is live and a second click enqueues a **second job** for the same stack. C-10's own fix note covers this (`hx-disabled-elt="#scan-btn"` on the form) — it is easy to drop because it is the last sentence.

**Why it happens:**
`{% include %}` makes the partial serve two roles (fragment of a page, and standalone response) and the OOB attribute is only correct in one of them.

**How to avoid:**
Extract `partials/scan_button.html` rendering the button from `job` + `ACTIVE_STATES`, take an `oob` boolean in the context, and set `hx-swap-oob="true"` only when `oob` is true. `index.html` includes it with `oob=False` inside the form; partial-returning routes include it with `oob=True` as a top-level sibling after the closing `</div>` of `#status-area`. Add `hx-disabled-elt="#scan-btn"` on the form in the same commit. Add the C-10 browser test (click Scan → wait `.status-done` → assert enabled) **and** a double-click test asserting exactly one job row.

**Warning signs:**
- `grep -c 'id="scan-btn"' src/saneless/web/templates/*.html src/saneless/web/templates/partials/*.html` sums to more than one occurrence per rendered document.
- Browser console shows `htmx:oobErrorNoTarget`.
- `app.js` is deleted but `hx-disabled-elt` appears nowhere.

---

### Pitfall 13: Cancelling a SANE read from a timeout thread — the fix that hangs shutdown

**Findings:** M-12, M-13, N-04, C-09 · **Step:** 8 (with M-13's flatbed path)

**What goes wrong:**
M-12's fix waits for the cancelled read to acknowledge before closing. That is correct and it introduces two new hazards:

- **Non-daemon executor threads block interpreter exit.** `ThreadPoolExecutor` workers are non-daemon and joined at exit. "Keep one long-lived executor per backend" (M-12) plus a thread permanently stuck in `sane_read()` means `saneless serve` cannot exit at all — the exact `docker stop` → SIGKILL outcome C-09 is trying to eliminate. Create the executor with a daemon-thread factory or accept `cancel_futures=True` + an explicit "leaked reader thread" log line and `os._exit`-free shutdown path with a bounded wait.
- **Leaving the handle open leaks the device.** M-12 correctly says do not `close()` a handle whose reader has not returned. The next scan then opens the same device and gets `Device busy` — which, before M-11 lands, is reported to the user as *"No paper detected in feeder"*. Sequence M-11 (Step 3) before M-12 (Step 8), which the review's order already does; do not reorder them.
- **`sane.exit()` is process-global (N-04).** Adding a `close()` that calls `sane.exit()` tears down the library for every other backend instance in the process. And `SANE_NET_HOSTS` must be set before the first `init()`, so a singleton freezes `scanner.host` for the process lifetime — changing the host in the config then requires a restart, which the status strip (U-03) and the docs must say. Never call `sane.exit()` from a request path or from `doctor`.

**Why it happens:**
The C API's cancellation contract ("no other operation until the cancelled one returns") has no Python-level enforcement, and `ThreadPoolExecutor` looks like it owns its threads' lifetimes. It does not.

**How to avoid:**
One `_acquire_with_timeout(dev, fn, timeout)` used by **both** ADF and flatbed (M-13), on daemon threads, with a `_CANCEL_GRACE_SECONDS` bound, a `logger.critical` when the read does not return, and a "device is poisoned" flag that makes the next `open()` attempt report a clear error instead of `Device busy`. Test with a fake whose read blocks on an `Event`: assert `close()` was **not** called while blocked, and assert the process can still exit.

**Warning signs:**
- `pytest` starts hanging at the end of a run (a leaked non-daemon thread).
- `docker stop` takes the full 10-second grace period.
- `sane.exit()` appears anywhere outside a single process-level shutdown hook.

---

### Pitfall 14: Enabling the SANE `test` backend in CI leaks a phantom scanner into everything else

**Findings:** M-32, C-06, M-11, M-16, M-26 · **Steps:** 3 (tests) + 7 (CI)

**What goes wrong:**
The `test` backend is the right integration target, and it is enabled by editing `dll.conf` — a **process- and machine-global** switch. Get this wrong and: developers' `saneless devices` starts listing `test:0`; the auto-profile generator writes phantom profiles into a real config; the browser tests pick up the fake device; and CI's "real hardware" test silently passes on every machine including ones with no SANE at all, so it proves nothing. Additionally, `uv sync` on a bare runner must compile `python-sane` from source and needs `libsane-dev` (M-26) — the integration job and the ordinary CI job both need it, or one of them fails for a reason unrelated to the test.

**Why it happens:**
SANE configuration is ambient. `SANE_CONFIG_DIR` is the only clean scoping mechanism and it is easy to forget in one of the several places tests get invoked.

**How to avoid:**
Exactly as M-32 describes: an **opt-in** module that sets `SANE_CONFIG_DIR` to a `tmp_path` containing a `dll.conf` with only `test`, guarded by a pytest marker (`@pytest.mark.sane_integration`) that is deselected by default and enabled in one dedicated CI job. Never mutate the system `dll.conf`. Assert in the integration test that the device list is exactly `["test:0", ...]` so a misconfigured environment fails loudly rather than skipping quietly. Use the backend's real feeder name `"Automatic Document Feeder"` (C-06), its range-tuple resolution constraint (M-16), and its `read-delay` option for the timeout path (M-12).

**Warning signs:**
- `dll.conf` edited outside a `tmp_path`.
- The integration job is green on a runner where `libsane-dev` was never installed.
- The marker is registered but nothing deselects it, so browser-less runners run it.

---

### Pitfall 15: Spooling pages to disk breaks page order, duplex interleave, and cleanup

**Findings:** M-08, C-04, C-01, M-14, N-39 · **Step:** 8

**What goes wrong:**
Four classic failures at once:

- **Lexicographic ordering.** `sorted(dir.glob("page-*.png"))` gives `page-1, page-10, page-11, page-2` — a ten-page scan comes out shuffled, silently, and the PDF looks plausible. Zero-pad (`page-0001.png`) *and* keep an explicit ordered list; do not re-derive order from the filesystem.
- **Duplex interleave over paths.** The reverse-and-interleave step currently works on in-memory images. Once pages are paths, the interleave must reorder the *path list*, and the mismatch-recovery path (which splits into fronts/backs PDFs, C-05) must carry the `-fronts`/`-backs` naming through. Getting this wrong produces a correctly-sized PDF in the wrong order — the worst kind of bug, because it passes every count assertion.
- **Cleanup on every exit path.** Once C-04 moves the PDF *out* of the temporary directory on failure, and M-07 adds a flip timeout that aborts mid-batch, and M-12 can leave a reader thread alive, the number of exit paths multiplies. A `finally` that deletes the spool directory will now delete the fronts the user needs preserved. Decide explicitly: spool pages die with the job; the assembled PDF outlives it.
- **Thumbnail and empty-page stats must be computed on the fly.** M-08 says so, and it interacts with M-14: with the backend's white-page rejection removed, `filter_empty_pages` runs over paths and must delete or skip files, and the raw pre-filter count that manual duplex relies on must be captured *before* filtering.

**Why it happens:**
The in-memory list carried order, identity, and lifetime for free. Paths carry none of them.

**How to avoid:**
Introduce a small `ScannedPage(index: int, path: Path)` record rather than bare paths, so order is data not convention. One `SpoolDir` object owning creation and cleanup, with an explicit `keep()` that hands ownership of a file to the caller (used by C-04). Test: 12 pages assemble in order 1..12; manual duplex with 10+10 interleaves 1,11,2,12,…; abort mid-batch leaves no spool directory but does leave the `failed/` PDF.

**Warning signs:**
- Any `sorted(glob(...))` producing page order.
- A test that asserts page *count* but never page *content order* (use distinct solid colours per page and assert the sequence).
- `tmp_dir` growing across runs — add a startup sweep of orphaned spool directories.

---

### Pitfall 16: Startup reconciliation racing shutdown, and the schema migration nobody wrote

**Findings:** M-03, C-07, C-03, U-02 · **Step:** 4 (with schema changes from 1 and 11)

**What goes wrong:**
- **Ordering in `lifespan`.** `fail_active_jobs()` must run **after** the store is open and **before** `worker.start()`. Put it after `start()` and it can mark the first real job of the new process as "Interrupted by restart". Put it in `create_app` instead of `lifespan` and the `TestClient` tests that construct an app without entering the lifespan diverge from production.
- **Multiple processes.** With more than one uvicorn worker (see Pitfall 10) each process runs `fail_active_jobs` at startup and kills the others' live jobs.
- **The schema has no real migration path.** `job.py:100-105` does `ALTER TABLE ... ADD COLUMN` inside a bare `except sqlite3.OperationalError: pass`. That `except` swallows `database is locked` and `disk I/O error` identically to "duplicate column name", so a transient failure leaves a store permanently missing a column and every later `INSERT` fails with `no such column`. Step 1 adds `warning`; Step 11 adds `pages_scanned` and `pages_uploaded` (U-02). Three more copies of that pattern is three more silent-corruption sites.

**Why it happens:**
The existing migration is a one-off that worked, so it becomes the template.

**How to avoid:**
Replace the ad-hoc `ALTER` with a tiny `user_version`-based migration list (`PRAGMA user_version`, a list of DDL steps, applied in one transaction), added in Step 1 before the first new column. Match the exception on the message (`"duplicate column name"`) or, better, read `PRAGMA table_info` — never swallow the whole class. Test: open a store, close it, reopen with the next version's schema, assert existing rows survive and the new column defaults sanely.

**Warning signs:**
- A second bare `except sqlite3.OperationalError: pass`.
- No test that opens a v1 database file with v2 code.
- `fail_active_jobs` called anywhere other than `lifespan`.

---

### Pitfall 17: Cleaning the test suite deletes the coverage of the bugs you just fixed

**Findings:** M-33, M-34, N-18, N-24, N-40, M-32 · **Step:** 9 (but see the Step 4 conflict)

**What goes wrong:**
- **`stop()` no longer synchronises.** N-24's justification for deleting 26 sleeps is *"`stop()` enqueues its sentinel behind the job and joins the thread, so the job is already processed"*. C-09 (Step 4) **replaces the sentinel with a stop flag** and a `queue.get(timeout=0.5)` loop, precisely so stopping never depends on queue capacity. After that change, `stop()` sets the flag and the loop may exit with the job still queued. Delete the sleeps as N-24 describes and you get a suite that is flaky in exactly the way sleeps were hiding. **This is a direct conflict between Step 4 and Step 9 and must be planned for.**
- **Fake rewrite turns green tests red — and the tempting fix is the test.** M-32 rewrites the SANE fakes to match the real `sane.py`. `test_empty_feeder_out_of_documents_error` is built entirely on `multi_scan()` raising, which the real class cannot do; `_NoGeometryDevice` raises where the real `__setattr__` succeeds. These tests must be **rewritten to the real semantics**, not patched to keep passing.
- **Deleting "low-value" tests deletes real coverage.** N-40's ~35 candidates include five pairs of near-duplicates where the two members differ in one assertion that matters.
- **Playwright flakiness.** Replacing sleeps with polling in browser tests means using `expect(locator).to_be_enabled()` style auto-retrying assertions, not `page.wait_for_timeout`. Browser tests also need a real uvicorn on an ephemeral port (M-34 flags the hard-coded 9090) and `playwright install --with-deps chromium` in CI (M-26).

**Why it happens:**
Step 9 is described as cleanup, which sounds risk-free, and it is scheduled five steps after the change that invalidates its central assumption.

**How to avoid:**
- Add a `wait_for_state(store, job_id, state, timeout)` polling helper in Step 4, **when** the stop semantics change, and convert the sleeps then. Do not defer to Step 9.
- Before deleting any test, run `pytest --cov --cov-report=xml`, delete, re-run, and **diff the set of covered lines** — not the percentage. Any line that loses its last cover is a stop.
- Land the fake rewrite (M-32) in the same phase as the backend fixes it exposes (Step 3), so every newly-red test has an obvious owner.
- Make hermeticity (M-34) the *first* thing in the test work: an autouse `monkeypatch.chdir(tmp_path)` + patched `Path.home()` fixture. Otherwise later test edits inherit the shared `/tmp/saneless-test` state and you debug the wrong thing. Note that this will expose tests that were passing only because they read the developer's real `./saneless.toml`.

**Warning signs:**
- Any `time.sleep` remaining in `tests/` after Step 9, or any new one added in Step 4.
- Coverage percentage stays flat while the test count drops by 35 (that is the *expected* shape — verify by line diff, not by the number).
- A CI run that is green on a machine with no `~/.config/saneless` and red on the developer's, or vice versa.

---

### Pitfall 18: The rename is three renames, and two of them are external systems

**Findings:** M-27, M-26, N-31 · **Step:** 7

**What goes wrong:**
`kris-knigga/saneless` → `kdknigga/saneless` touches three independent namespaces and only one is in the repository.

- **PyPI.** The review is explicit that the **distribution name `saneless` stays**; only URLs and the image name change. Renaming the *distribution* as well would be a separate decision requiring a name-availability check and a redirect story for anyone who already installed. Confirm the owner's intent before any `pyproject.toml` `name =` change. Also: the PyPI **trusted publisher** is configured as (owner, repository, workflow filename, environment). It must be created for `kdknigga`/`saneless`/`release.yml` **before** the first tag, or the publish fails with `invalid-publisher` after all the tests passed — the worst possible moment (M-26's "exercise the release path end to end" applies here specifically; use TestPyPI with an `rc` tag).
- **GHCR.** `ghcr.io/${{ github.repository }}` lowercases to `ghcr.io/kdknigga/saneless`, which is correct already. But a newly-created GHCR package is **private by default**: `docker pull` from the quick start will 401 for everyone until the package visibility is set to public and linked to the repo via the `org.opencontainers.image.source` label. Nothing in the workflow will tell you this; the release will be green and the docs still broken.
- **Docs site.** `mkdocs.yml` `site_url` change plus GitHub Pages enabled on the new repo; every existing external link to `kris-knigga.github.io/saneless` dies. Regenerate `site/` (the review notes the committed `site/` directory).
- **The CI guard eats itself.** M-27's `git grep -n -e "kris-knigga" -e "saneless\.github\.io" -- ':!.planning' ':!site'` must keep those exclusions in the CI step, or the check fails permanently on this very research file and on `.planning/` history.

**Why it happens:**
The in-repo edit is a 24-line find-and-replace and feels finished when the grep returns nothing.

**How to avoid:**
Treat the rename as a checklist with external items, verified by an actual `pip download` / `docker pull` / `curl` of the published artefacts from a clean machine, not by reading the workflow. Do the PyPI trusted-publisher and GHCR-visibility setup *before* the release-workflow fixes, so the first tag is the last unknown.

**Warning signs:**
- The CI name-guard step lacks the `':!.planning' ':!site'` pathspecs.
- The release workflow was never run, not even as `v0.0.0rc1` against TestPyPI.
- README still says "Not yet published" after a successful release, or still doesn't after an unsuccessful one.

---

### Pitfall 19: `permissions:` blocks break OIDC, and SHA pins rot without Dependabot

**Findings:** N-31, M-25, M-26 · **Step:** 7

**What goes wrong:**
Adding a workflow-level `permissions: { contents: read }` (which is the right default, and what M-25's CI snippet shows) **removes** the token scopes the release jobs need. Three jobs break at once:

| Job | Needs |
|---|---|
| PyPI publish (trusted publishing) | `id-token: write` |
| GHCR push | `packages: write`, `contents: read` |
| Pages deploy (`docs.yml`) | `pages: write`, `id-token: write` |

The failure surfaces as a 403 at the very last step of a release. Separately, pinning every action to a SHA without adding `.github/dependabot.yml` with `package-ecosystem: "github-actions"` means the pins are frozen forever, including the security fixes the pinning was meant to control — and the SHA loses the human-readable version, so nobody notices they are two years behind.

**Why it happens:**
`permissions` is documented as a workflow-level key, which is the convenient place to put it, and job-level overrides are a second step.

**How to avoid:**
Workflow-level `permissions: { contents: read }` **plus** an explicit job-level `permissions:` block on every job that needs more, written in the same commit. Every SHA pin carries a `# vX.Y.Z` trailing comment (Dependabot maintains it). Add the Dependabot config in the same commit as the pins. Verify by running the release workflow on a throwaway pre-release tag, not by reading it.

**Warning signs:**
- A `permissions:` key exists and no job has its own.
- `.github/dependabot.yml` absent after pinning.
- `docs.yml` still `pip install mkdocs-material` unpinned while `uv.lock` has 9.7.6 (N-31) — build docs with `uv run` so the lock applies.

---

### Pitfall 20: Vendoring htmx/Pico and adding origin checks breaks the tests that prove the UI works

**Findings:** N-21, N-22, U-09, C-10 · **Step:** 10 (but pull ahead of the Step 4 browser tests)

**What goes wrong:**
- **Version drift on vendoring.** The C-10 diagnosis is version-specific (it turns on htmx 2.0.8's `swapOuterHTML` event semantics). Vendoring "htmx 2.x latest" instead of the exact version the fix was verified against reintroduces uncertainty in the one place the review had to use a browser to get right. Pin the exact file, record the version in a comment, and add it to the Dependabot/renovate story or a manual checklist.
- **Ordering vs the browser tests.** Step 4 adds Playwright tests for the scan cycle. If those tests run against CDN-loaded htmx, they will fail in CI's sandbox (no egress) or be slow and flaky. Vendor **before** or **with** the browser tests, not in the Step 10 sweep.
- **`Sec-Fetch-Site` scope.** The review's rule is "reject POSTs whose `Sec-Fetch-Site` header is *present* and not `same-origin`" — absent means allow, so `curl`, the CLI, and the container healthcheck are unaffected. Implement exactly that. Implementing the stricter "require `same-origin`" instead breaks the healthcheck, the documented API in `docs/reference/web-api.md`, and any `TestClient` test that does not set the header. Also note the interaction with U-09: this is a **browser-CSRF** mitigation, not authentication, and must not be described as making the UI safe to expose.
- **Default bind change.** N-22 suggests defaulting `web_host` to `127.0.0.1`. That silently breaks every existing Docker deployment (the container would only listen on loopback inside its own namespace). If you do it, the Dockerfile/compose must set `SANELESS_OUTPUT__WEB_HOST=0.0.0.0` in the same commit, and it is a documented breaking change.

**Why it happens:**
These read as low-risk nits, so they get batched into a sweep at the end, after the tests that depend on them are already written.

**How to avoid:**
Vendor the exact pinned versions in the same phase as the browser tests. Write the `Sec-Fetch-Site` middleware with an explicit allow-when-absent branch and a test for each of: absent, `same-origin`, `cross-site`, `none`. Change `web_host` only together with the container default.

**Warning signs:**
- `base.html` still references `unpkg.com` or `@picocss/pico@2` (a floating major) after the browser tests are written.
- A `TestClient` test starts failing with 403 after the middleware lands.
- The healthcheck goes red after the origin check.

---

### Pitfall 21: The status strip probes the scanner on page load and starves the single worker

**Findings:** U-03, M-01, N-04, C-09 · **Step:** 11

**What goes wrong:**
`sane.get_devices()` over `saned` on an unreachable host takes tens of seconds; against a live scanner it can wake the device and, on some backends, contend with an in-progress scan. Wiring the status strip to enumerate devices on every page load means: a threadpool thread parked per tab (M-01), a possible `Device busy` for the worker mid-scan (which, pre-M-11, is reported as "No paper detected in feeder"), and — with the healthcheck or `saneless doctor` in a compose `healthcheck:` running every 30 seconds — permanent background SANE traffic. The feature designed to make setup diagnosable makes scanning unreliable.

**Why it happens:**
"Show a green light for the scanner" reads as a cheap read. It is the single most expensive and most stateful call in the system.

**How to avoid:**
Never enumerate from a request path. Probe once at startup into a cached snapshot (timestamp + result), refresh on an explicit user-initiated button and on a long TTL from a background thread, and **skip the probe entirely while `worker.current_job_id is not None`** — show "busy scanning" for that row instead. Render the strip with `hx-trigger="load"` against the cached snapshot so page load is instant. Do not use `saneless doctor` as the container healthcheck; keep `/health` cheap and let `doctor` be a human command. Remember N-04: `SANE_NET_HOSTS` is fixed at first `init()`, so a host change needs a restart — say so in the strip's error text.

**Warning signs:**
- `get_devices()` reachable from any route.
- `GET /` latency grows when the scanner host is unplugged.
- `doctor` appears in `healthcheck:` or in the Dockerfile `HEALTHCHECK`.

---

### Pitfall 22: The flip-prompt owner token becomes accidental auth, or breaks the prompt entirely

**Findings:** U-06, U-09, C-02, M-02 · **Step:** 11

**What goes wrong:**
The flip prompt is rendered **server-side into a partial fetched by a 1-second poll**. That means the owner identity must reach the server on *every poll*, not just at submit:

- **localStorage does not travel.** A token in `localStorage` must be attached to every `hx-get` (via `hx-headers` or a query param) — which means threading it through the status partial's own polling attributes, which means the partial that *renders* the token also has to *carry* it. Easy to get half-right: submit works, polls lose the token, and the owner sees "waiting for someone else to flip" for their own job. Which is unrecoverable, because nobody can press Continue.
- **A token in the poll URL lands in access logs**, and after M-28 those logs go to stdout and into the operator's `docker logs` and any log shipper. Prefer a `HttpOnly`, `SameSite=Lax` cookie set at submit: it is sent automatically on every poll, survives a reload, and does not appear in URLs.
- **Fail-open vs fail-closed.** A user who reloads the page, or opens it on a second device to press Continue while standing at the scanner, has no token. Fail-closed and the scan is stuck until the M-07 flip timeout fires. The right behaviour is: show the prompt read-only to non-owners *with* a "Continue anyway" escape that requires an extra confirm, and never let the prompt be the only way to unstick the worker.
- **It is not security.** U-09 is explicit that there is no login. The token stops a child on a tablet from aborting a parent's 50-page job (a footgun); it does not stop anyone on the LAN. Documenting it as access control would be a new false claim of the kind section 8 catalogues.

**Why it happens:**
"Only show it to the submitter" sounds like a UI condition. It is a session-identity feature spread across submit, poll, continue, and abort.

**How to avoid:**
Cookie, set at `POST /api/scan`, compared server-side against a `owner_token` column on the job row. One helper reads it; four endpoints use it. Test: submit in browser context A, assert the prompt with live buttons in A and a read-only message in context B; assert Continue from B is refused without the confirm; assert a reload of A still owns it.

**Warning signs:**
- Any token in a URL or in a `hx-get` query string.
- No test using two browser contexts.
- A job that can only be unstuck by pressing a button nobody can see.

---

## Cross-Step Ordering Hazards

The review's step order is sound but has four places where a later step invalidates or blocks an earlier one. Plan around these explicitly.

| Hazard | Earlier step | Blocked/invalidated by | Resolution |
|---|---|---|---|
| Atomic config write cannot rename over a bind-mounted **file** | Step 5 (M-10) | Step 7 (M-30 mount the directory) | Pull M-30's mount change into Step 5, or split it out and do it first |
| Deleting the 26 worker-test sleeps assumes `stop()` drains the queue | Step 9 (N-24) | Step 4 (C-09 replaces the sentinel with a stop flag) | Add a `wait_for_state` polling helper and convert the sleeps **in Step 4** |
| Preserved-PDF and job-history durability | Step 1 (C-04) | Step 5/7 (N-39 `state_dir`, U-08 volume) | Introduce `output.state_dir` in Step 1 or 5; do not ship `failed/` under `tmp_dir` |
| Browser tests depend on a CDN | Step 4 (C-10 browser test) | Step 10 (N-21 vendoring) | Vendor htmx/Pico at pinned versions in Step 4 |
| Job-table columns (`warning`, then `pages_scanned`/`pages_uploaded`) | Steps 1 and 11 | No migration mechanism | Add `PRAGMA user_version` migrations in Step 1, before the first new column |
| Friendly error messages built on `ErrorCategory` | Step 11 (U-05) | Step 3 (M-11 misclassification) | U-05 explicitly depends on M-11 landing first — the review says so; keep it |

## Technical Debt Patterns

| Shortcut | Immediate Benefit | Long-term Cost | When Acceptable |
|---|---|---|---|
| Keep `"Manual Duplex"` translated in the pipeline instead of at config load | One-line change, tests pass | Four other readers of `profile.source` disagree; N-07's duplication returns | Never — a `model_validator` is the same size |
| Add the `JobStore` lock without moving `prune` out of the job `finally` | C-07's stress test goes green today | Reintroduces the contention window on every scan; housekeeping can still fail the job | Never — both are in the same C-07 fix note |
| Fix C-10 by reading `evt.target` in `app.js` instead of server-owned OOB | One-line, no template churn | Preserves the two-sources-of-truth that caused C-10; C-09's 429 has no path to update it | Only as a hotfix on a release branch |
| `except sqlite3.OperationalError: pass` for the next `ALTER TABLE` | Two lines, matches existing code | Silently produces a store missing a column; every later INSERT fails | Never — the existing one is a bug to replace |
| `--force` merge that owns "all keys the writer knows" implicitly | No new constant | Drifts from `ProfileConfig`; new fields silently unowned or user fields silently clobbered | Never — declare `_GENERATED_KEYS` |
| Return 200 with an error body instead of 429/503 so htmx swaps it | Avoids htmx config | Lies to the JSON API and to monitoring; breaks the `/health` story | Acceptable **only** for HTML partial routes, documented, with the JSON API keeping true codes |
| Ship the status strip probing SANE synchronously | Simplest possible implementation | Slow page loads, `Device busy` during scans, background SANE traffic from healthchecks | Never — cache the snapshot |
| Skip the SANE `test`-backend integration module, keep only rewritten fakes | Saves the CI plumbing | The fakes drift again; this is exactly how C-01, C-06, M-11, M-15 shipped green | Only if the fakes are generated from `sane.py` and a rebuild is CI-enforced |

## Integration Gotchas

| Integration | Common Mistake | Correct Approach |
|---|---|---|
| **htmx 2.x** | Assuming a 429/503 body will be rendered | Default `responseHandling` is `swap:false, error:true` for `[45]..`; add explicit `{"code":"429","swap":true}` entries or return 200 for HTML partials (HIGH confidence, verified against htmx 2.0.4 docs) |
| **htmx `hx-swap-oob`** | Nesting the OOB element inside the element being swapped, or emitting a duplicate `id` from `{% include %}` | OOB element must be a top-level sibling in the response fragment; extract a `scan_button.html` partial with an `oob` flag; watch for `htmx:oobErrorNoTarget` |
| **FastAPI / Starlette sync routes** | Believing `def` is a pure win | Runs in the AnyIO threadpool, default **40** tokens (verified); shared mutable state now genuinely races; no cancellation on client disconnect; pin uvicorn to one worker |
| **pydantic-settings** | Assuming `extra="forbid"` catches every typo | Unknown *nested* keys from env and TOML are rejected; unknown *top-level* env vars are still ignored by the env source. Verify before documenting (MEDIUM confidence) |
| **pydantic aliases** | Adding `extra="forbid"` to a model with `Field(alias=...)` and no `populate_by_name` | `ProfileConfig` already sets `populate_by_name=True`; the other three nested models must not grow aliases without it, or round-tripped configs stop loading |
| **sqlite3 + threads** | Treating `check_same_thread=False` as thread safety | It disables a check, nothing more. Lock + `with conn` per public method, private lock-free helpers, no public→public calls |
| **SQLite WAL in Docker** | Putting the DB on an NFS/CIFS-backed bind mount | WAL needs a shared-memory `-shm` file; use a named volume or a local path. Symptom: `database is locked` / `disk I/O error` |
| **`os.replace` atomic write** | Renaming over a bind-mounted **file** | `EBUSY`. Mount the directory (M-30). Catch `OSError` and say so in the message (MEDIUM-HIGH confidence — verify with one `docker run`) |
| **python-sane** | Treating `sane.init()`/`sane.exit()` as per-object | Process-global; `SANE_NET_HOSTS` is read at first `init()` only. Guard with a module flag; never call `exit()` from a request or from `doctor` |
| **python-sane cancel** | `executor.shutdown(wait=False)` then `dev.close()` | Wait for the future with a grace bound; skip `close()` if it has not returned; use daemon threads or the process cannot exit |
| **SANE `test` backend** | Editing the system `dll.conf` | Point `SANE_CONFIG_DIR` at a `tmp_path`; opt-in pytest marker; assert the device list so a misconfigured env fails loudly |
| **PyPI trusted publishing** | Assuming it follows the repo rename | Publisher is keyed to (owner, repo, workflow filename, environment); create it for `kdknigga/saneless` before the first tag; rehearse against TestPyPI |
| **GHCR** | Assuming the pushed package is public | New packages are private; set visibility and link via `org.opencontainers.image.source`; `docker pull` from the quick start is the only real test |
| **GitHub Actions `permissions`** | Workflow-level `contents: read` only | Job-level overrides for `id-token: write` (PyPI OIDC, Pages), `packages: write` (GHCR), `pages: write` |

## Performance Traps

| Trap | Symptoms | Prevention | When It Breaks |
|---|---|---|---|
| Cache stampede on `/api/tags` after `async`→`def` | 10 tabs, 10 parked threads, 30 s page loads when paperless is down | Single-flight lock around the fetch; cache the *unreachable* outcome with a short TTL (M-01) | 3+ open tabs with paperless unreachable |
| 1-second HTMX poll × N tabs × sqlite lock | Rising p99 on `/api/jobs/current/status` | Poll only while the job is active (already the case); keep store methods short; move `prune` off the job path | ~10 tabs, or `prune` over 500 rows |
| Status strip probing SANE per page load | `GET /` takes 10–60 s with the scanner host down | Cached snapshot + background refresh; never from a request (U-03) | Immediately, on any misconfigured host |
| Whole batch in RAM (pre-M-08) | OOM-killed mid-scan, all pages lost | Spool to disk, one page working set, per-page disk check (M-08) | ~50 colour pages at 300 DPI (~1.3 GB) on a 2 GB box |
| PNG spool + PDF coexisting on disk | `OSError` that is not a `ScanError`, pages lost | Per-page disk estimate, `outputstream=` to `img2pdf.convert` (M-08) | ~1 GB peak for 50 pages; the 500 MB pre-check is half what is needed |
| `poll_task` wall-clock drift | "Uploading" for many minutes, no other scan can start | Monotonic deadline; treat unexpected status as a signal (M-22) | Any paperless that hangs rather than refuses |
| Doctor/healthcheck enumerating devices every 30 s | Constant SANE traffic; scanner wakes repeatedly | Keep `/health` cheap; `doctor` is a human command | Immediately once `doctor` is used as a healthcheck |

## Security Mistakes

| Mistake | Risk | Prevention |
|---|---|---|
| Presenting the U-06 owner token as access control | Operators skip the reverse proxy because "it has tokens now" | State plainly (U-09) that there is no login; the token is a footgun guard, not auth |
| Owner token in the polling URL | Token lands in access logs, which after M-28 go to stdout and any log shipper | `HttpOnly`, `SameSite=Lax` cookie set at submit |
| `SecretStr` applied to the field but `get_secret_value()` sprayed everywhere | Re-introduces the leak N-15 fixes | Exactly two call sites (client construction); a test asserting `"token" not in repr(settings)` and not in `model_dump()` |
| Atomic config rewrite dropping mode bits | A 0600 file containing the paperless token becomes 0644 | Copy `stat().st_mode` onto the temp file before `os.replace` (M-10) |
| `.dockerignore` written as a deny-list | The developer's untracked `saneless.toml` with a live token still lands in a build layer (M-31) | Allow-list (`*` then `!pyproject.toml`, `!uv.lock`, `!src/`, …) and explicit `COPY` |
| Truncating 4xx bodies but still logging the full one | 5 kB of HTML in `job.error`, in the history table, and in stdout logs (M-17) | Truncate at ~200 chars, prefer the JSON `detail`, log the full body only at DEBUG |
| Strict `Sec-Fetch-Site` requirement | Breaks the container healthcheck, the CLI, and documented API use | Reject only when the header is **present** and not `same-origin` (N-22) |
| Placeholder-token check that logs the token | A "your token is the placeholder" message that prints it | Compare against known placeholders, never echo the value (U-01) |

## UX Pitfalls

| Pitfall | User Impact | Better Approach |
|---|---|---|
| A 429 that renders nothing (htmx default) | Click Scan, nothing happens, exactly the C-10 experience the milestone is fixing | Render queue position: "Waiting — 2 jobs ahead" (U-06 + C-09) |
| `JobState.FALLBACK` shown as `FALLBACK` | A raw enum name where a sentence belongs | Label on the enum; "Saved to the paperless inbox — it will be picked up when paperless is back" |
| Friendly error text built before M-11 is fixed | Confidently wrong advice ("load paper") for a jam or an open lid | U-05 explicitly depends on M-11; fix classification first |
| Preserving the PDF and telling the user to retry a *duplicate* rejection | Retry loop producing more duplicates | Distinguish "paperless refused this document" from "paperless was unreachable"; only the latter is retryable |
| Owner-token prompt that fails closed on reload | Scan is stuck until the M-07 timeout; nobody can press Continue | Read-only prompt for non-owners with a confirmed "Continue anyway" escape |
| Profile labels generated at startup but not persisted under a read-only mount | Dropdown differs between restarts with no explanation | Keep in memory, say so on the status strip (U-04 + M-30) |
| Deleting `app.js` without `hx-disabled-elt` | Double-click enqueues a second job for the same stack | Add `hx-disabled-elt="#scan-btn"` in the same commit (C-10) |
| Page counts shown only on success | The C-06 class of silent loss stays invisible on the error path | Show "Scanned N, removed M blank, uploaded K" on every terminal state (U-02) |

## "Looks Done But Isn't" Checklist

- [ ] **Typed pipeline result (C-03):** often missing the `poll_task` failure path's PDF preservation — verify a FAILURE task leaves a file under `failed/` and the message names it
- [ ] **`JobState.FALLBACK` (C-03):** often missing a `status.html` branch — verify the status area is non-empty, stops polling, and refreshes history
- [ ] **`duplex` field (C-01):** often missing the legacy `source = "Manual Duplex"` load-time translation — verify an old config still scans, with a deprecation warning
- [ ] **`extra="forbid"` (M-18):** often missing the `_build_settings` message rewrite — verify the text for `[paperless] tokne` names `paperless` as the *section* and `tokne` as the *key*
- [ ] **Atomic config write (M-10):** often missing the bind-mounted-file case — verify `auto-profiles --force` inside the documented compose deployment
- [ ] **JobStore lock (C-07):** often missing the shutdown ordering — verify stopping mid-job produces `ERROR`, not a stranded row and not `Cannot operate on a closed database`
- [ ] **Sync routes (M-01):** often missing the cache/profiles locks — verify 10 concurrent `GET /` with paperless unreachable, and no `dictionary changed size during iteration`
- [ ] **429 back-pressure (C-09):** often missing the browser side — verify a full queue shows a visible message and an enabled button
- [ ] **OOB Scan button (C-10):** often missing the duplicate-id check and `hx-disabled-elt` — verify one `#scan-btn` in the rendered DOM and that double-click creates one job
- [ ] **SANE timeout (M-12/M-13):** often missing the flatbed path and process exit — verify both paths time out and that `pytest` and `docker stop` still exit promptly
- [ ] **Spool to disk (M-08):** often missing page ordering — verify a 12-page scan comes out 1..12 with distinct page content, and a duplex 10+10 interleaves correctly
- [ ] **Crash recovery (M-03):** often missing the "most recent job" rendering change — verify a stale active row does not make a fresh page look busy
- [ ] **Rename (M-27):** often missing the external systems — verify by `pip install`, `docker pull`, and opening the docs URL from a clean machine
- [ ] **Actions hardening (N-31):** often missing job-level permissions — verify by actually running the release workflow on a pre-release tag
- [ ] **Hermetic tests (M-34):** often missing the developer-config leak — verify the suite passes with `HOME` set to an empty directory and no `./saneless.toml`
- [ ] **Status strip (U-03):** often missing the "do not probe while scanning" rule — verify `GET /` is fast while a scan is running and while the scanner host is unplugged

## Recovery Strategies

| Pitfall | Recovery Cost | Recovery Steps |
|---|---|---|
| C-03 raise deletes the PDF (Pitfall 1) | HIGH — the user's document is gone | Cannot recover the document. Widen the preserve to span poll, add the regression test, and add a release note if any version shipped with it |
| Blank status area from a new enum member (Pitfall 3) | LOW | Add the template branch and the `{% else %}`; do M-05's consolidation so it cannot recur |
| `extra="forbid"` bricks existing deployments (Pitfall 6) | MEDIUM — users cannot start | Ship a `saneless config check` / `doctor` path that prints the offending key and file; consider a one-release warn-only mode before hard failure |
| Atomic write `EBUSY` (Pitfall 8) | LOW if caught, MEDIUM if shipped | Catch `OSError`, fall back to in-place write with a loud warning, and tell the user to mount the directory |
| Lock re-entrancy splitting a transaction (Pitfall 9) | MEDIUM — partial job records | Refactor to private lock-free helpers; add an assertion that no public method is called while the lock is held |
| Schema column missing from a swallowed `ALTER` (Pitfall 16) | MEDIUM — store unusable | `PRAGMA user_version` migrations; a repair path that recreates the table from `table_info` and copies rows |
| Deleted tests removed real coverage (Pitfall 17) | LOW if caught by the line diff, HIGH if not | Restore from git; make the covered-line diff a required step, not a suggestion |
| PyPI/GHCR publish fails at the tag (Pitfall 18) | LOW technically, HIGH in reputation | Rehearse on TestPyPI with an `rc` tag before the real one; PyPI filenames cannot be reused after a bad upload |
| Reintroduced page-order bug (Pitfall 15) | HIGH — silently wrong documents in paperless | Content-ordered test with distinct page colours; audit any documents produced by an affected build |

## Pitfall-to-Step Mapping

| Pitfall | Findings | Remediation step (section 10) | Verification |
|---|---|---|---|
| 1. C-03 raise inside the temp dir | C-03, C-04 | 1 | Poll returns FAILURE → PDF exists under `failed/`, message names path |
| 2. Preserved files in a volatile dir | C-04, N-39, U-08 | 1 (+5/7) | Image update / reboot; job history and `failed/` survive |
| 3. New enum member, blank status | C-03, M-05 | 1 + 4 | Every `JobState` has a label and a template branch; type checkers flag a missing key |
| 4. `"fallback"` sentinel readers | C-03, M-33 | 1 | `git grep '"fallback"'` empty in `src/`, `tests/`, `docs/`; `spec=PaperlessClient` on the mock |
| 5. Duplex compat shim in the wrong layer | C-01, N-07, M-09 | 2 (+5) | Legacy config loads to `duplex="manual"`, `source="ADF"`; one `manual` reference in `src/` |
| 6. `extra="forbid"` blast radius | M-18, M-24, U-01 | 5 | Error text test for a nested typo; unknown env sub-key test; legacy `title` key test |
| 7. Writer/reader coupling and `--force` | C-08, M-09, M-10 | 5 | Round-trip test parametrised over source lists; `default_tags` survives `--force` |
| 8. Atomic write vs bind-mounted file | M-10, M-30 | 5 (needs 7's mount) | `auto-profiles --force` inside the documented compose stack |
| 9. Lock re-entrancy and shutdown | C-07, M-03 | 4 | Two-thread stress (200 rounds × 10); stop-mid-job leaves `ERROR` |
| 10. `def` routes and shared state | M-01, M-04 | 4 | 10 concurrent `GET /` with paperless down; no dict-mutation errors; one uvicorn worker asserted |
| 11. htmx 4xx no-swap | C-09, C-10 | 4 | Full-queue browser test shows a message and an enabled button |
| 12. OOB duplicate id + double submit | C-10, M-05 | 4 | One `#scan-btn` in the DOM; double-click creates one job; no `oobErrorNoTarget` |
| 13. SANE cancel, exit, and process teardown | M-12, M-13, N-04 | 8 (after 3's M-11) | `close()` not called while blocked; `pytest` and `docker stop` exit promptly |
| 14. `test` backend leaking into the environment | M-32, C-06 | 3 (+7 CI) | Opt-in marker, `SANE_CONFIG_DIR` under `tmp_path`, device list asserted |
| 15. Spool ordering, interleave, cleanup | M-08, C-04, M-14 | 8 | 12 pages in order by content; duplex interleave; abort leaves no spool, keeps `failed/` |
| 16. Startup reconciliation and migrations | M-03, U-02 | 4 (schema from 1 and 11) | v1 DB opened by v2 code; `fail_active_jobs` only in `lifespan`, before `start()` |
| 17. Test cleanup losing coverage / stop semantics | M-33, M-34, N-24, N-40 | 4 (sleeps) + 9 (rest) | Covered-line diff before/after; zero `time.sleep` in `tests/`; suite green with empty `HOME` |
| 18. Rename touches three namespaces | M-27, M-26 | 7 | Clean-machine `pip install`, `docker pull`, docs URL; CI grep guard keeps its pathspecs |
| 19. `permissions:` vs OIDC, SHA pins rot | N-31, M-25, M-26 | 7 | Pre-release tag runs the whole release workflow green; `dependabot.yml` present |
| 20. Vendoring and origin checks vs tests | N-21, N-22, U-09 | 4 (vendor) + 10 (origin check) | Browser tests pass with no network egress; healthcheck and CLI unaffected by the header check |
| 21. Status strip starving the worker | U-03, M-01, N-04 | 11 | `GET /` fast with scanner unplugged and during a scan; `doctor` not in `healthcheck:` |
| 22. Owner token: auth confusion and dead prompts | U-06, U-09 | 11 | Two-browser-context test; reload keeps ownership; non-owner escape path works |

## Sources

- `.planning/reviews/2026-09-09-code-review.md` — sections 3 (C-01..C-10), 4 (M-01..M-34), 5 (N-01..N-45), 9 (test suite), 10 (remediation order), 11 (U-01..U-10). HIGH confidence: findings were re-verified by execution by the review's lead.
- Direct reading of this repository (HIGH confidence): `src/saneless/config.py` (nested models, `populate_by_name`, `title` alias, `_build_settings` `loc[0]` message construction, source order `(init, env, toml)`), `src/saneless/job.py` (WAL pragma, `CREATE TABLE IF NOT EXISTS`, the bare `except sqlite3.OperationalError: pass` migration, `JobState` members), `src/saneless/worker.py` (blocking `put`, sentinel `stop()`), `src/saneless/web/routes.py` (`_get_cached_or_fetch` check-then-set, no lock), `src/saneless/web/templates/index.html` (`{% include %}` of the status partial, duplicated active-state lists, `#scan-btn`), `partials/status.html` (no `{% else %}`, literal `active_states`), `static/app.js`.
- Context7 `/bigskysoftware/htmx` v2.0.4 (HIGH confidence): default `responseHandling` (`[45].. → swap:false, error:true`), `htmx:beforeSwap` retargeting, `htmx:oobErrorNoTarget`, `htmx:responseError`.
- Context7 `/kludex/fastapi-tips` (HIGH confidence): sync `def` endpoints run in the AnyIO threadpool, default limiter 40 tokens, adjustable in `lifespan`.
- Context7 `/pydantic/pydantic-settings` (MEDIUM confidence for the specific nested-extra behaviour): `env_nested_delimiter` handling, `populate_by_name` for nested models and aliases, `nested_model_default_partial_update`. Verify the "unknown nested env sub-key is rejected / unknown top-level env var is ignored" asymmetry with a two-line test before documenting it.
- Linux `rename(2)` over a bind-mount target returning `EBUSY` (MEDIUM-HIGH confidence, not executed this session): confirm with one `docker run -v ./x.toml:/etc/x.toml` experiment before planning M-10/M-30 ordering around it.

---
*Pitfalls research for: applying the 2026-09-09 code-review remediations to the working saneless codebase*
*Researched: 2026-09-09*
