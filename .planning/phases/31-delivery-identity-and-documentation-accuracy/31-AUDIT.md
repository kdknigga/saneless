---
phase: 31
audited: 2026-09-18T00:00:00Z
audited_at_commit: c9689a1
status: passed
scores:
  rows_disposed: 34/34
  rows_permanently_defended: 24/34
  rows_point_in_time: 10/34
  recheck_claims_disposed: 17/17
  rows_needing_new_behaviour: 0/34
gaps:
  rows: []
  recheck_claims: []
  escalations: []
findings_this_audit:
  - row: 10
    was: "research called it corrected by Phases 21-30"
    actually: "still false; the inverted threshold advice survived every plan"
    closed_by: "31-08 task 1 (c9689a1), pinned by test_profile_howto_tuning_advice_matches_the_empty_page_rule"
  - row: 4
    was: "corrected -- the Done/FAILURE conflation is genuinely gone"
    actually: "the same page had since grown a second, different error: it named the status area's word for the history table's column"
    closed_by: "31-08 task 1 (c9689a1), pinned by test_first_web_ui_scan_names_real_history_labels"
---

# Phase 31 Documentation Accuracy Audit

**Source:** `.planning/reviews/2026-09-09-code-review.md` § 8 (`:980`-`:1029`)
**Audited against:** the tree at `c9689a1`, which contains plans 31-01 through 31-07
**Audited on:** 2026-09-18

This is a verification record, not user documentation (D-42). The original review file is
**not** edited in place: annotating the reviewers' findings after the fact would destroy the
record of what they actually found. `git diff --stat` on it is empty.

## What the two Defence values mean

D-41 requires this distinction to be explicit per row, and requires the artifact to be honest
about it. The two values are used with exactly these meanings:

| Value | Meaning |
|-------|---------|
| **permanently defended** | A named test fails if this row regresses. The test exists, it runs in the default suite, and its expectation is derived from the source or is a literal a regression would have to restore deliberately. |
| **point-in-time read** | Nothing pins this row. A human read the cited `file:line` on 2026-09-18 and found the claim true. It can silently become false again tomorrow. |

Where a row's truth has two halves and only one is pinned, the row is marked **point-in-time
read** and the Evidence cell names both the test and the unpinned half. Understating coverage
is the honest failure direction: it invites re-checking, where overstating invites a later
reader to skip it (T-31-31).

Two warnings from earlier plans in this phase, which shaped how the rows below were checked:

- **A passing doc-truth test is not proof the claim is true.** Plan 31-05 added a `WORKDIR` that
  falsified two documented claims, both pinned by tests, and both tests stayed green -- because
  the docs and their tests agreed with each other and had jointly stopped agreeing with the
  image. Where a row's truth depends on code, it was checked against the code here, not against
  the test.
- **`mkdocs build --strict` does not validate anchor fragments.** Plan 31-06 found that a link to
  a nonexistent heading passes silently. The one new cross-page anchor added by this plan was
  checked against the built site's `id=` attribute, not against strict mode.

## Table 1: the 34 rows the review found false

Row numbers line up one-to-one with review § 8's "Claims that are false" table (`:988`-`:1021`).
"Where" is the location **now**, which is often not where the review found it.

| # | Where (file:line) | Claim | Disposition | Evidence | Defence |
|---|---|---|---|---|---|
| 1 | `docs/how-to/set-up-adf-duplex.md:88-95` | "press Enter (CLI)"; "saneless prompts you to flip" | corrected in Phase 22 | The prompt exists: `src/saneless/cli.py:122` is the literal `"Flip the stack over and load it back into the feeder. Scan the back sides?"`. The page describes the CLI yes/no question and the Web UI Continue/Abort buttons separately | point-in-time read |
| 2 | `docs/how-to/set-up-adf-duplex.md:47,51,56` | Set `source = "Manual Duplex"` to enable manual duplex | corrected in Phase 22 | `"Manual Duplex"` as a source is gone; the mechanism is the `duplex` profile field (`src/saneless/config.py:206-224` still carries the legacy-source detector only to warn). The page's example is `source = "ADF"` + `duplex = "manual"` | point-in-time read |
| 3 | `docs/explanation/consume-directory-fallback.md:90,95,105` | "the scan job's final status is FALLBACK" | corrected in Phase 23 | `FALLBACK` is a real member of `JobState` (`src/saneless/vocabulary.py:84`), and the page describes it as the state that distinguishes the two paths | point-in-time read |
| 4 | `docs/getting-started/first-web-ui-scan.md:54,75` | "Done: the document has been successfully uploaded" | corrected in Phase 23; **residual error found and fixed by this audit** | The conflation is gone: `src/saneless/web/templates/partials/status.html:66` renders "Done" only under `JobState.DONE`, and `:71` renders FALLBACK as "Saved to folder". The page had since grown a second error -- line 75 gave the status area's word ("Done") for the history table's column, which renders `state_label(JobState.DONE)` = "Complete". Fixed here and pinned by `test_first_web_ui_scan_names_real_history_labels` | point-in-time read |
| 5 | `docs/reference/environment-variables.md:82` | "Profile fields cannot be set via environment variables" | corrected in Phase 27 | The false sentence is gone. The page now documents the mechanism: `SANELESS_PROFILES__<NAME>__<FIELD>`, with the `default`-profile requirement stated | point-in-time read |
| 6 | `docs/reference/configuration.md:111`, `docs/how-to/configure-scan-profiles.md:62`, `saneless.toml.example:21` | `title` profile key sets a default title, but nothing reads it | corrected in Phase 25 | `title` is read by `resolve_job_title` (imported at `src/saneless/cli.py:44`). All three surfaces now say it is used when the title is left blank | `test_configuration_reference_title_is_literal`, `test_profile_howto_title_is_literal`, `test_toml_example_title_is_literal` -- **permanently defended** |
| 7 | `docs/how-to/configure-scan-profiles.md:147` | Auto-profiles creates `flatbed-color-300`, `adf-gray-150` | corrected in Phase 24 | The page documents source-derived slugs: "lowercased and reduced to letters, digits and hyphens", giving `flatbed` and `automatic-document-feeder`. The rule is `_slugify` at `src/saneless/auto_profiles.py:57` | point-in-time read |
| 8 | `docs/how-to/configure-scan-profiles.md:151-162` | `--force` overwrites only auto-generated profiles | corrected in Phase 24 | Behaviour changed and the doc follows it: `--force` merges generated keys, keeps everything else, and skips profiles without `auto_generated = true` (`src/saneless/auto_profiles.py:527` `_is_auto_generated`) | `test_profile_howto_describes_force_as_a_merge`, `test_cli_reference_force_is_a_merge` -- **permanently defended** |
| 9 | `docs/how-to/configure-scan-profiles.md:130-135` | "Otherwise, it crops the image after scanning" (unreachable) | corrected in Phase 23 | The page now names the three conditions that send saneless to the software crop and says it logs which one happened | point-in-time read |
| 10 | `docs/how-to/configure-scan-profiles.md:224-235` | "lower the thresholds to detect faint pages as non-empty" (example mean 240) | **still false at audit time; corrected in Phase 31 (31-08 task 1)** | `src/saneless/pages.py:74` is `is_blank = mean > mean_threshold and stddev < stddev_threshold`, so lowering the mean threshold admits **more** pages to the blank set. The page said the opposite and its example lowered the mean to 240.0. Now states the rule, raises the mean to 253.0, and links the explanation page | `test_profile_howto_tuning_advice_matches_the_empty_page_rule` -- **permanently defended** |
| 11 | `docs/explanation/empty-page-detection.md:54` | Use `--log-level DEBUG` | corrected in Phase 27 | The tip is now `saneless -v scan ...` or `log_level = "DEBUG"` under `[output]`. No `--log-level` option is documented anywhere | `test_no_log_level_option_documented` -- **permanently defended** |
| 12 | `docs/explanation/empty-page-detection.md:58-68` | `enable_empty_page_detection = false` keeps all pages | corrected in Phase 23 | The backend's white-page policy was removed; `src/saneless/scanner/sane_backend.py:1385` records the decision, and the only remaining backend drop is a page that failed an integrity check, which `:1422-1424` keeps out of the blank count deliberately. The toggle now really does keep all pages | point-in-time read |
| 13 | `docs/reference/cli-commands.md:12` | `-v` "enables debug output" | corrected in Phase 27 | The row says `-v` raises saneless's own loggers to DEBUG and that other libraries and the web server keep the configured `log_level` | `test_cli_reference_verbose_is_saneless_debug` -- **permanently defended** |
| 14 | `docs/how-to/cli-scripting.md:82` | Exit 2 for a missing config file | corrected in Phase 27 | The exit-2 row explicitly names "a `--config` file that does not exist"; `docs/reference/configuration.md:16` states the same rule | `test_scripting_exit_code_two_examples`, `test_job_database_documented_under_exit_code_two` -- **permanently defended** |
| 15 | `docs/how-to/install-bare-metal.md:47` | Run `saneless --version` | became true without a doc edit -- Phase 31 (31-01 task 2, DLVR-10) | `@click.version_option(package_name="saneless")` at `src/saneless/cli.py:496`. The documented sentence was correct all along; the code was missing | `test_version_option_prints_the_installed_version`, `test_version_option_needs_no_config` (`tests/test_cli.py:299,319`) -- **permanently defended** |
| 16 | `README.md:44` | `saneless scan  # Scan a document` without `--title` | corrected in Phase 31 (31-02 task 2, DOCS-02) | The example is `saneless scan --title "Some Document"` | `test_readme_scan_example_carries_a_title` -- **permanently defended** |
| 17 | `README.md:63` | `source = "flatbed"` (compared case-sensitively) | corrected in Phase 31 (31-02 task 2, DOCS-02) | `source = "Flatbed"`. The expectation is derived from the profile model's own default spelling, not from a literal | `test_readme_source_values_are_real_sane_spellings` -- **permanently defended** |
| 18 | `README.md:74` | Link to `tutorials/scan-your-first-document/` | corrected in Phase 31 (31-02 task 2, DOCS-02) | The link is `…/getting-started/first-cli-scan/`, and the assertion generalises: every `kdknigga.github.io/saneless/<path>/` link must map to an existing `docs/<path>.md` | `test_every_readme_docs_link_resolves_to_a_page` -- **permanently defended** |
| 19 | 24 lines in nine files | `ghcr.io/kris-knigga/…`, `github.com/kris-knigga/…`, `kris-knigga.github.io/…` | corrected in Phase 31 (31-02 task 1, DLVR-01) | Every tracked file outside `.planning/` names the current owner. The guard is a whole-tree scan, not a fixed file list, so a new file cannot reintroduce the slug | `test_no_shipped_file_references_the_old_owner` -- **permanently defended** |
| 20 | `docs/how-to/deploy-docker-compose.md:34` | Container "will fail to start if the file is missing" | corrected in Phase 27 | The note says the container "still starts; it does not create the file for you", and goes on to say where `auto-profiles` writes instead | `test_no_fail_to_start_claim` -- **permanently defended** |
| 21 | `saneless.toml.example:11`, `docs/reference/docker.md:139` | `web_port` is a common Docker override; example sets 8081 | corrected in Phase 31 (31-05 task 3, DLVR-05) | The example line is commented and reads "bare metal only; in Docker remap with -p". The Docker reference's row states the container port is fixed at 8080 and that setting `web_port` makes the healthcheck report unhealthy. The port expectation is derived from `OutputConfig.web_port` (`src/saneless/config.py:484`) | `test_the_example_config_does_not_ship_a_live_web_port`, `test_every_documented_container_port_matches_the_model_default` -- **permanently defended** |
| 22 | `docs/reference/web-api.md:248,262` | Flip endpoints return "HTML partial with updated job status" | corrected in Phase 26 | Both endpoints now describe the acknowledgment partial precisely, naming the two literal strings (`Flip confirmed. Scanning reverse sides next...`, `Aborting scan...`) and saying the endpoint does not wait for pass B | point-in-time read (the abort's *exit code* half is pinned by `test_flip_prompt_abort_documented_as_cancelled`; the partial's content is not) |
| 23 | `docs/reference/web-api.md:94-97` | `/api/scan` "returns immediately after queuing" | corrected in Phase 26 | The response table documents 429 with `Retry-After: 30` when the queue is full and 503 when the worker is down or degraded, and says a refusal is recorded in job history | `test_every_rejection_member_is_documented_with_its_message` -- **permanently defended** |
| 24 | `docs/getting-started/first-web-ui-scan.md:62` | Click **Cancel**; "place the pages back face-up" | corrected in Phase 26 | Neither "Cancel" nor "face-up" appears on the page. It says **Continue** / **Abort scan** and "flip the whole stack over the long edge", quoting the browser's own confirm text | `test_first_web_ui_scan_walks_the_current_form` -- **permanently defended** |
| 25 | `docs/explanation/architecture.md:67` | Fallback triggers on "network error, timeout, auth failure" | corrected in Phase 23 | The paragraph is explicit: a 4xx rejection "such as an authentication failure" or a 3xx redirect "never falls back" | point-in-time read |
| 26 | `docs/explanation/architecture.md` (absence) | HTMX design "keeps the web layer fully responsive" | corrected in Phase 26 | The word "responsive" does not appear anywhere on the page | point-in-time read |
| 27 | `docs/reference/configuration.md:11,60,61` | "XDG config directory", "XDG state directory", while `$XDG_*` was ignored | corrected in Phase 27 | `$XDG_CONFIG_HOME` and `$XDG_STATE_HOME` are honoured (`src/saneless/config.py:136,150`, with the basedir-spec rule that an empty or relative value is ignored), and the reference names the variables with the `~/…` fallbacks in parentheses | `test_configuration_reference_documents_xdg_and_levels`, `test_search_path_lists_name_xdg_config_home` -- **permanently defended** |
| 28 | `docs/explanation/consume-directory-fallback.md:11` | "up to 3 attempts by default" (not configurable) | corrected in Phase 23 | The sentence reads "up to 3 times" without "by default". The count is still fixed: `src/saneless/paperless.py:455` `max_retries: int = 3` | point-in-time read |
| 29 | `docs/getting-started/first-cli-scan.md:9,64`, `docs/getting-started/which-setup.md:9,12` | USB scanners must be connected to the `saned` host | corrected in Phase 31 (31-06 task 2, DOCS-04) | All four statements agree: bare metal enumerates a local USB scanner directly, a container never touches the USB bus. The container-passthrough section was deleted rather than caveated | `test_no_doc_page_documents_usb_passthrough_into_a_container`, `test_scanner_host_documentation_carries_the_container_caveat` -- **permanently defended** |
| 30 | `docs/how-to/cli-scripting.md:47` | Job JSON shows `"id": "a1b2c3d4"`, `created_at` without offset | corrected in Phase 31 (31-07 task 1) | The example id is `8eae6099-6b24-4b69-a339-8daa8e6c9a5c`, a real UUID4 shape; `created_at` carries `+00:00` | `test_the_job_json_example_shows_a_real_id_shape`, `test_scripting_documents_jobs_json_created_at_as_utc` -- **permanently defended** |
| 31 | `docs/getting-started/quick-start.md:23` | `docker run -v ./config.toml:…` (relative host path) | corrected in Phase 31 (31-07 task 1) | The flag is `-v "$(pwd)/config:/etc/saneless"`. The guard covers every `-v`/`--volume` flag in every example, not just this one | `test_no_docker_run_example_uses_a_relative_host_path` -- **permanently defended** |
| 32 | `docs/how-to/deploy-docker-compose.md:47,49-50` | A Paperless service with only `PAPERLESS_SECRET_KEY` | corrected in Phase 31 (31-07 task 2, DOCS-06) | The compose example "defines saneless and nothing else"; a note explains paperless-ngx needs a database and a Redis-compatible broker, and links to that project's own compose documentation instead of copying half of it | `test_the_deploy_guide_does_not_ship_a_partial_paperless_stack` -- **permanently defended** |
| 33 | `src/saneless/job.py:1-9` | Docstring: "SQLite-backed persistence for crash recovery" | corrected in Phase 24 | The docstring now says a job survives the process only as a row, and that `fail_active_jobs` fails every still-active job at startup (ROBU-06) | point-in-time read |
| 34 | `.planning/milestones/v1.0-PRD.md` (was `docs/PRD.md`) | Device picker, Test Connection button, `/api/jobs/{id}`, 0-to-1 thresholds | corrected in Phase 31 (31-07 task 3, DOCS-03) | `docs/PRD.md` no longer exists; the document moved into the planning tree unchanged, because it is a true record of what v1.0 intended and was only in the wrong place. MkDocs publishes every page under `docs/` whether the nav lists it or not, which is how it reached the public site unnoticed | `test_no_planning_artifact_is_published_under_docs` -- **permanently defended** |

**Totals.** 34/34 disposed. 24 permanently defended, 10 point-in-time reads. Zero rows required
new behaviour, so D-43's escalation clause did not fire.

### The two rows this audit had to close itself

Both were classified "corrected by Phases 21-30" in `31-RESEARCH.md` § 9, whose inventory was
taken before any plan in this phase ran. Re-checking against the tree rather than trusting the
inventory is what found them, and is the reason this plan was written to re-verify rather than
transcribe.

**Row 10 was never corrected at all.** `configure-scan-profiles.md` still told the reader to
"lower the thresholds to detect pages with faint content as non-empty", and its example lowered
the mean threshold from the 250.0 default to 240.0. `is_empty_page` is
`mean > mean_threshold and stddev < stddev_threshold`, so lowering the mean threshold makes
*more* pages count as empty -- the exact inversion the reviewers caught. The rule was stated
correctly the whole time in `empty-page-detection.md:49-51`, two pages away, which is why the
error survived: each page is internally consistent and nothing compared them.

**Row 4 was corrected, and then the same page grew a different error.** The original
conflation is genuinely gone. But the walkthrough's description of the history table listed
"Done" among the words the Status column shows, and the table renders
`state_label(JobState.DONE)`, which is "Complete". The status area directly above the table
does say "Done" for the same state. The page named one surface's word while describing the
other's -- a class of error no keyword ban would catch, and the reason the replacement test
derives its expectation from the set `state_label` can return.

## Table 2: re-checking the claims the review said were already correct (D-44)

Review § 8's closing paragraph (`:1025`) lists claims the reviewers verified as correct on
2026-09-09. Twelve phases have changed behaviour since. A claim that was true in September and
is false now is exactly what this audit exists to catch, so each is re-checked here against the
tree, using the same columns as Table 1.

`claim superseded` is a distinct disposition from `still true`: it means the 2026-09-09
*sentence* no longer describes the system, while the *documentation* is correct and, in both
cases below, is now pinned by a derived test. Collapsing the two into one label would hide
exactly the drift D-44 asked about.

| # | Claim (as the review stated it) | Where (file:line) | Disposition | Evidence | Defence |
|---|---|---|---|---|---|
| C1 | Every `[output]` and `[profiles]` default in `configuration.md` matches `config.py` | `docs/reference/configuration.md:60-111` | still true | Spot-checked against `src/saneless/config.py`: `web_host = "0.0.0.0"` (`:483`), `web_port = 8080` (`:484`), `empty_page_mean_threshold = 250.0` (`:382`), `empty_page_stddev_threshold = 5.0` (`:383`). `[web]` keys are derived from `WebConfig.model_fields` | `test_every_web_config_field_is_documented` (the `[web]` section is derived; the `[output]`/`[profiles]` default *values* are not) -- point-in-time read |
| C2 | The config search order | `docs/reference/configuration.md:7-17` | still true | Four entries in the documented order, with `$XDG_CONFIG_HOME` named at position 3 and its fallback in parentheses; matches `src/saneless/config.py:127-150` | `test_search_path_lists_name_xdg_config_home` -- **permanently defended** |
| C3 | The env prefix and `__` delimiter | `docs/reference/environment-variables.md:82` | still true | `SANELESS_PROFILES__<NAME>__<FIELD>`, with the single-underscore mistake called out and given a suggestion | `test_environment_reference_documents_unknown_variables` -- **permanently defended** |
| C4 | Env-over-TOML-over-defaults precedence | `docs/reference/environment-variables.md:3`, `:86` | still true | Line 3: "All configuration can be set via environment variables, which override values from the TOML config file." Line 86 adds that saneless logs at startup which settings came from the environment -- dotted names only, never values | point-in-time read |
| C5 | The requirement for a `default` profile | `docs/reference/environment-variables.md:82` | still true | "A `default` profile must still exist: with no config file, set at least one `SANELESS_PROFILES__DEFAULT__<FIELD>` too, or loading fails" | point-in-time read |
| C6 | The `auto_source_mode` and `paper_size` literals | `docs/reference/configuration.md:105-106` | still true | `src/saneless/config.py:365` `Literal["flatbed", "adf"]`; `:374` `Literal["full", "a3", "a4", "a5", "letter", "legal"]`. Both documented sets match, and `configure-scan-profiles.md:126` lists the same six | point-in-time read |
| C7 | **All eleven routes and methods in `web-api.md`** | `docs/reference/web-api.md:9-23` | **claim superseded; the documentation is correct and is now pinned by `test_every_route_is_documented_in_the_web_api_reference`** | `src/saneless/web/routes.py` declares **15** route decorators, not eleven -- Phases 26 and 30 added `/api/jobs/{job_id}/status`, `/api/checks`, `/api/checks/refresh`, `/api/profiles/description`, `/api/flip/continue` and `/api/flip/abort`. The reference documents all 15. The test derives its expectation from the decorators, so route 16 cannot ship undocumented | **permanently defended** |
| C8 | The `/health` 200 and 503 bodies | `docs/reference/web-api.md:45-49` | still true | Both 503 bodies are documented with their conditions, plus the three ways the worker becomes degraded and the way it clears | point-in-time read |
| C9 | The `/api/paperless/test` status values | `docs/reference/web-api.md:61-65` | still true | All five 200 values are listed with their conditions, and the page states they are the complete set and a stable wire contract | point-in-time read |
| C10 | The `/api/scan` form fields | `docs/reference/web-api.md:81-86` | still true | Matches the signature at `src/saneless/web/routes.py:1148-1152`: `profile` (required), `title` (optional, max 256), `tags` (int list), `correspondent` (optional int) | point-in-time read |
| C11 | `jobs --limit` default 20 | `src/saneless/cli.py:814` | still true | `@click.option("--limit", default=20, type=int, help="Maximum jobs to show.")` | point-in-time read |
| C12 | `serve` defaults of `0.0.0.0:8080` and the no-auth note | `src/saneless/config.py:483-484`, `docs/reference/web-api.md:316` | still true | The defaults are unchanged. The no-auth note is now required on both deployment entry surfaces, not only the API reference a reader may never open | `test_the_no_auth_note_appears_on_both_entry_surfaces` -- **permanently defended** |
| C13 | **`scan` exit codes 1, 2 and 3 for the cases that are caught** | `docs/how-to/cli-scripting.md:78-88`, `docs/reference/cli-commands.md` | **claim superseded; the documentation is correct and is now pinned by `test_scripting_exit_code_table_matches_exit_code_enum` and `test_cli_reference_global_exit_code_table_matches_exit_code_enum`** | Phase 28 replaced the three-code table with `0/1/2/3/4/5/130` (`src/saneless/vocabulary.py:185-198`). Both documentation tables carry the full set, and the tests assert set equality with the `ExitCode` enum in both directions -- every documented code is real and every real code is documented | **permanently defended** |
| C14 | Three upload attempts with `2**attempt` backoff | `src/saneless/paperless.py:455`, `:707` | still true | `max_retries: int = 3` and `time.sleep(2**attempt)`, both unchanged. `docs/explanation/consume-directory-fallback.md:11` describes it as "up to 3 times, with exponential backoff" | point-in-time read |
| C15 | The `SANE_NET_HOSTS` precedence rule | `src/saneless/scanner/sane_backend.py:876-882` | still true | The config host is applied only when the variable is absent from the environment; otherwise saneless logs that it is ignoring `scanner.host` and names the value it found | point-in-time read |
| C16 | PNG-then-img2pdf lossless assembly | `docs/explanation/architecture.md:48-50` | still true | `src/saneless/pdf.py:5-10`: one page per `img2pdf.convert` call, embedding the very files the spool wrote. The test also bans the two overclaims the page used to make -- "byte-for-byte" and "PIL Images" -- so reverting either correction fails | `test_architecture_page_states_the_memory_disk_and_timeout_rules` -- **permanently defended** |
| C17 | The dual-threshold empty-page rule in `empty-page-detection.md` | `docs/explanation/empty-page-detection.md:23-26,49-51` | still true | Matches `src/saneless/pages.py:74` exactly, in both directions. **This claim is why row 10 above is a finding:** the explanation page states the rule correctly and the how-to's paraphrase of it did not, and nothing compared the two until now | `test_profile_howto_tuning_advice_matches_the_empty_page_rule` now compares the how-to's advice against `ProfileConfig`'s defaults -- **permanently defended** |

**Totals.** 17/17 re-checked. Two claims superseded (C7, C13) -- in both cases the 2026-09-09
sentence is stale while the shipped documentation is correct and derived-test-defended. Fifteen
still true. **Zero claims have silently become false**, but C17 is the claim whose correctness
made row 10's inversion invisible: a true statement on one page does not defend a false
paraphrase of it on another.

## Verification evidence

Commands run against the tree at `c9689a1`, after this plan's two corrections:

| Command | Outcome |
|---|---|
| `uv run pytest -m "not browser and not sane_hardware"` | pass |
| `uv run zizmor .` | pass (exit 0) |
| `uv run mkdocs build --strict` | pass (exit 0) |
| `uv run ruff check .` | pass |
| `uv run ruff format --check .` | pass |
| `uv run ty check` | pass |
| `uv run pyrefly check src tests` | pass |
| `uv run prek run --all-files` | pass |
| `uv run prek run --stage pre-push --all-files` | pass |
| `git diff --stat .planning/reviews/2026-09-09-code-review.md` | empty -- the review is unedited (D-42) |

Every test named in Table 1 and Table 2 was confirmed to exist in the tree and to pass. The
anchor added to `configure-scan-profiles.md` was confirmed against
`site/explanation/empty-page-detection/index.html`'s `id="tuning-the-thresholds"`, because
`mkdocs build --strict` does not validate fragments.

## Release rehearsal evidence

*Placeholder -- Plan 31-10 fills this section in.* It should record the RC tag, the release
workflow run URL, and the `pip install` and `docker pull` output that D-21 and D-22 require
before the milestone can be called delivered.

| Item | Value |
|---|---|
| RC tag | *pending 31-10* |
| Release workflow run | *pending 31-10* |
| `pip install` from the published artifact | *pending 31-10* |
| `docker pull` of the published image | *pending 31-10* |

## A note on the rename, so nobody goes looking for migration work

Row 19 renamed 24 lines across nine files, which usually implies a migration. It does not here,
because **nothing was ever published under the old name.**

There is no `ghcr.io/kris-knigga/saneless` package to deprecate or redirect. GitHub Pages never
deployed under the old owner, so no published URL is breaking. No stored data, OS registration,
secret, or environment variable carries the slug: the config schema, the `SANELESS_*` prefix,
the SQLite schema, the XDG paths and the container paths are all named for the project, not for
its owner. The only stale artefact anywhere is a gitignored `site/` build directory, which is
regenerated by the next `mkdocs build`.

The rename is therefore a documentation correction with no runtime, registry or user-facing
consequence, and the guard that keeps it correct is a whole-tree scan rather than a migration
step that could be half-applied.
