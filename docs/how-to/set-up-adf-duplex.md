# Set Up ADF Duplex Scanning

Scan both sides of multi-page documents using your scanner's Automatic Document Feeder (ADF). saneless supports three ADF modes depending on your hardware.

## What you'll need

- A scanner with an ADF feeder
- saneless installed and configured ([Install on Bare Metal](install-bare-metal.md) or [Deploy with Docker Compose](deploy-docker-compose.md))

## The three ADF modes

The profile names below are hand-written, but they match what `saneless auto-profiles` generates, because every profile is named after the scanner's own source name -- a device reporting `ADF` gets a profile called `adf`.

### ADF Simplex

Scans one side of each page. Load a stack face-up and saneless feeds each page through the ADF, scanning the front only.

```toml
[profiles.adf]
source = "ADF"
resolution = 300
mode = "Color"
```

```bash
saneless scan --profile adf --title "Meeting Notes"
```

### ADF Hardware Duplex

The scanner scans both sides of each page automatically in a single pass. This requires hardware duplex support -- check your scanner's specifications.

```toml
[profiles.duplex]
source = "ADF Duplex"
duplex = "hardware"
resolution = 300
mode = "Color"
```

```bash
saneless scan --profile duplex --title "Contract"
```

Most scanners select duplex by source name, so on them `source = "ADF Duplex"` is what makes this a duplex scan, and `duplex = "hardware"` records what the source does. `saneless auto-profiles` writes it for sources whose name says they scan both sides.

Some scanners have no duplex source. The epson2, kodakaio and magicolor drivers, and older epsonds ones, list a single feeder source and a separate ADF mode option, `adf-mode`, that switches it between `Simplex` and `Duplex`. On those, `duplex = "hardware"` is what makes the scan double-sided: saneless sets `adf-mode` to `Duplex` right after selecting the source. On every other scan where the option is in use it sets `Simplex`, so a `Duplex` left behind by an earlier scan does not carry over. `auto-profiles` does not generate this profile, because such a scanner may not report the option as usable until its feeder is selected, so write it yourself:

```toml
[profiles.duplex]
source = "ADF"
duplex = "hardware"
resolution = 300
mode = "Color"
```

If the scanner lists `adf-mode` but has it switched off for that source -- a feeder with no duplex unit, for example -- the scan is refused before any sheet is fed, with an error naming `adf_mode` and `'Duplex'`. If the scanner has neither a duplex source nor an ADF mode and the profile names a single-sided feeder, saneless logs a warning that only one side of each sheet is scanned, and scans one side.

### Manual Duplex

For scanners without hardware duplex, saneless coordinates a two-pass scan: first the front sides, then the back sides. saneless reverses and interleaves the pages to produce the correct page order.

A manual duplex profile takes two keys: `source`, set to a feeder source your scanner actually reports, and `duplex = "manual"`, which tells saneless to run the two-pass flow. Run `saneless devices --capabilities` to list the sources your scanner reports.

```toml
[profiles.manual-duplex]
source = "ADF"
duplex = "manual"
resolution = 300
mode = "Color"
```

```bash
saneless scan --profile manual-duplex --title "Double-sided doc"
```

!!! note "Manual duplex needs a document feeder"
    Manual duplex feeds the same stack through the feeder twice, so both passes
    use a single-sided feeder source. saneless uses the `source` you configured
    when your scanner reports it and it is a single-sided feeder; otherwise it
    uses the first single-sided feeder the scanner reports. A source that
    already scans both sides (such as `ADF Duplex`) is never used for manual
    duplex -- each pass would return every page twice -- so if you name one,
    saneless logs a warning and uses the single-sided feeder instead. It is not
    a flatbed workflow -- on a flatbed-only scanner, use a plain flatbed profile
    and scan each side as its own job.

The manual duplex flow:

1. Load the stack face-up in the feeder
2. Start the scan -- saneless scans all front sides (pass A)
3. A prompt appears asking you to flip the stack
4. Keep the pages in the same order, flip the whole stack over the long edge, and load it back into the feeder
5. Confirm the flip -- saneless scans all back sides (pass B)
6. saneless reverses the backs and interleaves them with the fronts: page 1 front, page 1 back, page 2 front, page 2 back, etc.
7. The assembled PDF is uploaded to paperless-ngx

How you confirm the flip depends on where you started the scan:

- **Web UI:** a flip prompt with **Continue** and **Abort scan** buttons appears automatically once the front sides are scanned. As soon as saneless receives your click, the buttons are replaced by a short confirmation, and the status moves on once pass B starts. **Abort scan** stops the scan before pass B and ends the job as **Cancelled**, shown in grey rather than as an error, and nothing is uploaded.
- **CLI:** `saneless scan` asks a yes/no question and waits until you answer:

    ```
    Flip the stack over and load it back into the feeder. Scan the back sides? [Y/n]:
    ```

    Answering yes (or pressing Enter, since yes is the default) starts pass B. Answering no, pressing Ctrl-C, or closing input (Ctrl-D) cancels the scan: saneless prints `Manual duplex scan cancelled at the flip prompt` and exits with exit code 130. An error reading the terminal at the prompt is a failure, not a cancel: the scan fails straight away with a `Scan error: Flip prompt failed: ...` line and exit code 1, and the error is logged. Nothing is uploaded in either case -- but the two cases differ in what is kept. A cancel keeps nothing, because you chose to stop. A failure keeps the front sides pass A already scanned, as a PDF under `failed/` in the data directory, whose path the error names.

Either way, the wait is bounded by `operator_wait_timeout_seconds` in the `[output]` section (600 seconds by default). If nobody confirms the flip in time, the job fails with `Manual duplex flip wait timed out after 600 seconds: nobody confirmed the stack was flipped` and nothing is uploaded. The fronts are kept, as above: nobody decided to abandon the scan, so the sheets that were fed are preserved rather than thrown away. A timeout is a failure, not a cancel: the web job ends as failed, and `saneless scan` exits with code 1. See [Configuration](../reference/configuration.md#output).

Stopping saneless is not a cancel either. If the server stops or restarts while a web job waits at the flip prompt -- `docker compose restart`, an upgrade, a host shutdown -- the fronts are kept under `failed/` as above, and the job ends as failed with `The server restarted before this scan finished`, followed by the path of the kept PDF. The same holds if the stop arrives while pass A is still scanning and pass A finishes within 5 seconds of it; a pass A that takes longer is not interrupted, and its pages are left in the scratch directory for the next start to recover (see [Deploy with Docker Compose](deploy-docker-compose.md#stopping-and-restarting) for when that start has nothing to recover). At the CLI, Ctrl-C at the prompt is still a cancel that keeps nothing (exit code 130), but a SIGTERM, or the hangup a closing terminal or SSH session sends, keeps the fronts and exits with 143 or 129 respectively. Under Docker, keeping the fronts is a copy onto the data volume, so leave the container time to finish it; see [Deploy with Docker Compose](deploy-docker-compose.md#stopping-and-restarting).

!!! warning "The CLI needs an interactive terminal for manual duplex"
    Someone has to flip the stack between the two passes, so `saneless scan` refuses a manual
    duplex profile when it is not run from a terminal -- from cron, a pipe, or a script with
    redirected input. It exits with code 2 before the scanner feeds a single page:

    ```
    Profile 'manual-duplex' is manual duplex, which needs an interactive terminal: saneless must prompt you to flip the stack between the two passes. Run it from a terminal, or scan from the web UI.
    ```

    For unattended automation, use a simplex or hardware duplex profile instead. See
    [CLI Scripting](cli-scripting.md#exit-codes).

### Scanners with no document feeder

If your scanner reports no feeder source at all, a manual duplex scan fails before pass A starts, and the error lists the sources the scanner does report:

```
Manual duplex needs a document feeder, and the device reports none. Available: ['Flatbed', 'Auto']
```

saneless does not fall back to a flatbed or `Auto` source for manual duplex, because that would scan the platen twice instead of feeding your stack.

#### Scanners whose only feeder scans both sides

If every feeder source your scanner reports already scans both sides of each sheet, manual duplex is refused before any page is fed:

```
Manual duplex needs a single-sided document feeder, and every feeder the device reports scans both sides; set duplex = "hardware" with one of them instead. Available: ['Flatbed', 'ADF Duplex']
```

Such a scanner does not need the flip workflow: set `duplex = "hardware"` and use that source, as in [ADF Hardware Duplex](#adf-hardware-duplex).

#### Scanners that report no source list

Some scanners expose no `source` option at all, so saneless has no source to select. Manual duplex still runs on such a scanner when `source` names its feeder (for example `ADF`), because the scanner feeds without being told. With a source that is not a feeder, such as `Flatbed`, manual duplex is refused before any page is fed:

```
Manual duplex needs a feeder source, and this device exposes no source option to choose one; set source to the name of its feeder (got 'Flatbed')
```

Other scanners have a `source` option but report its list in a form saneless cannot read. saneless then selects the source your profile names and leaves the scanner to accept or refuse it, as it does for a one-sided scan. Because it cannot see which sources scan both sides, manual duplex runs only when `source` names a single-sided feeder, such as `ADF`. Any other source, including a both-sides feeder such as `ADF Duplex`, is refused before any page is fed:

```
Manual duplex needs a single-sided document feeder, and this device's list of sources could not be read to find one; set source to the name of its single-sided feeder (got 'ADF Duplex')
```

!!! info "Auto source scanners"
    If your scanner reports only an `Auto` source instead of `ADF` or `ADF Duplex`, you can
    route it to multi-page ADF behavior by setting `auto_source_mode = "adf"` in your profile.
    See [Configure Scan Profiles](configure-scan-profiles.md#auto-source) for details.

## Paper size on a feeder

`paper_size` works differently on a feeder than on the flatbed. On the glass, the sheet lies in the top-left corner, so saneless sets the scan area from that corner. In a feeder, the sheet sits wherever the feeder guides it -- against one side on some models, in the middle on others -- and saneless cannot see which. So on a scan through the feeder:

- If the scanner reports `page-width` and `page-height` options for the selected source, saneless sets them to the paper size, the scanner places its own window over the sheet, and the scan area is set inside that window.
- Otherwise the paper size is not applied: the page is scanned at the full width of the feeder window and is not cropped, so nothing is cut off a sheet the feeder centred. The log says, at INFO, that `paper_size` was not applied and why.

## Empty page detection

When scanning duplex documents, blank back sides are common. saneless detects and removes empty pages by default, by measuring how much of each page is ink. This is controlled per profile:

```toml
[profiles.duplex]
source = "ADF Duplex"
resolution = 300
mode = "Color"
enable_empty_page_detection = true
empty_page_coverage_threshold = 0.001
```

The default keeps a page that carries only a page number or a few faint pencil lines, and may keep a dusty blank back. Removed pages are not kept; the scan names them instead, by page number in the interleaved document, for example `Removed as blank: pages 2, 4 of 4 scanned.` To tune the threshold or disable empty page detection, see [Configure Scan Profiles](configure-scan-profiles.md#empty-page-detection-tuning) and [How Empty Page Detection Works](../explanation/empty-page-detection.md).

## When a manual duplex scan does not come out whole

saneless does not discard your scans, whichever way a manual duplex job goes wrong.

If the front and back pass produce different page counts, it assembles the fronts and backs into separate PDFs and uploads both to paperless-ngx for manual review.

The same happens when either pass could not read a sheet, even if the two counts still agree. A sheet the scanner skips moves every later page of that pass by one, so if each pass skips a different sheet the counts match but the fronts and backs no longer line up. saneless does not guess which back belongs to which front. It uploads the two PDFs and warns that the scanner could not read the sheets, so the fronts and backs could not be paired reliably.

The two uploads are titled with `(fronts)` and `(backs)` after your title. The `(backs)` PDF is in sheet order, the same order as `(fronts)`, not the reversed order pass B fed them in. Page N of `(backs)` is the back of page N of `(fronts)` only when both passes fed every sheet exactly once, and a mismatch means one did not: a sheet was skipped, missed or fed twice, and from that sheet on the two drift apart. Match the halves by what is on the pages, not by page number.

If the job fails instead -- a fault during pass B, a pass B that fed nothing, a flip wait that timed out, a flip prompt that could not be read, or saneless stopping while the job waited at the flip prompt -- nothing is uploaded, and the fronts pass A scanned (with any backs pass B managed before it failed) are preserved as separately named PDFs under `failed/` in the data directory, the `(backs)` PDF in sheet order as above. A pass B that failed part way fed only the last sheets of the stack, so its `(backs)` PDF holds their backs, and the error says which `(fronts)` page its page 1 goes with if every sheet was fed exactly once. If pass A could not read a sheet, the error says instead that the halves cannot be paired by page number: match them by what is on the pages. The error names the paths. The one manual duplex ending that keeps nothing is the one you chose: **Abort scan**, answering no, or Ctrl-C at the flip prompt.

Empty page detection is deliberately skipped for these two partial PDFs, even when the profile has `enable_empty_page_detection = true`. When the passes disagree, a blank back side is evidence about why -- a sheet that double-fed, or one that did not feed at all -- and the partial PDFs exist so you can see exactly what each pass picked up. Removing blank pages would throw that evidence away. Every scan that does not hit a mismatch still has its blank pages removed as usual.

### When a pass reaches its sheet cap

One scan through the feeder keeps at most 500 sheets, or 50 for an Auto source that `auto_source_mode = "adf"` sends through the feeder. The cap applies to each pass separately, not to the whole job. Reaching it is not a failure: a feeder can only tell that the stack goes on by feeding one more sheet, so that sheet is fed but not kept, the pages kept are uploaded, and the job finishes as **Uploaded with a warning** that names the sheet, so you know where to resume. A simplex or hardware duplex scan uploads its pages as one document.

In manual duplex, what happens depends on which pass reached the cap:

- **The fronts pass (pass A).** saneless uploads the fronts it kept, as one document, and ends the job without asking you to flip the stack. The sheet it fed but did not keep is already in the output tray with the fronts, so turning the stack over would pair every back with the wrong front. The warning says so: *"The backs were not scanned: sheet 501 is already in the output tray, so turning the stack over would pair every back with the wrong front."* Scan the backs, and the sheets that were not fed, as new documents.
- **The backs pass (pass B).** The fronts and backs are never interleaved, even when their counts agree, because pass B fed a sheet pass A never did. They are uploaded as `(fronts)` and `(backs)`, as for a page-count mismatch, and the warning says that the scan of the backs stopped at its sheet cap and names the sheet that was fed but not kept. Its page count is the pages of both documents together. Pass A ended on its own, so the turned-over stack held sheets whose fronts were never scanned, and resuming from the sheet that was not kept would not recover them: *"... so sheet 501 of the turned-over stack was fed but not kept, and the stack held more sheets than the scan of the fronts fed. Check both documents, and scan any sheet missing from either again, both sides, as a new document."*

!!! tip
    Always test with a few pages first to confirm page ordering before scanning a large batch. This helps verify that your scanner feeds pages in the expected direction.
