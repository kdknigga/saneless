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

The scanner decides to scan both sides from the source name it is given, so `source = "ADF Duplex"` is what makes this a duplex scan. `duplex = "hardware"` is optional and changes nothing about how the scan runs: it records what the scanner does, so the profile describes itself. `saneless auto-profiles` writes it for hardware duplex sources.

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

Either way, the wait is bounded by `flip_timeout_seconds` in the `[output]` section (600 seconds by default). If nobody confirms the flip in time, the job fails with `Manual duplex flip wait timed out after 600 seconds: nobody confirmed the stack was flipped` and nothing is uploaded. The fronts are kept, as above: nobody decided to abandon the scan, so the sheets that were fed are preserved rather than thrown away. A timeout is a failure, not a cancel: the web job ends as failed, and `saneless scan` exits with code 1. See [Configuration](../reference/configuration.md#output).

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

!!! info "Auto source scanners"
    If your scanner reports only an `Auto` source instead of `ADF` or `ADF Duplex`, you can
    route it to multi-page ADF behavior by setting `auto_source_mode = "adf"` in your profile.
    See [Configure Scan Profiles](configure-scan-profiles.md#auto-source) for details.

## Empty page detection

When scanning duplex documents, blank back sides are common. saneless detects and removes empty pages by default using luminance analysis. This is controlled per profile:

```toml
[profiles.duplex]
source = "ADF Duplex"
resolution = 300
mode = "Color"
enable_empty_page_detection = true
empty_page_mean_threshold = 250.0
empty_page_stddev_threshold = 5.0
```

To tune the thresholds or disable empty page detection, see [Configure Scan Profiles](configure-scan-profiles.md).

## When a manual duplex scan does not come out whole

saneless does not discard your scans, whichever way a manual duplex job goes wrong.

If the front and back pass produce different page counts, it assembles the fronts and backs into separate PDFs and uploads both to paperless-ngx for manual review.

If the job fails instead -- a fault during pass B, a pass B that fed nothing, a flip wait that timed out, or a flip prompt that could not be read -- nothing is uploaded, and the fronts pass A scanned (with any backs pass B managed before it failed) are preserved as separately named PDFs under `failed/` in the data directory. The error names the paths. The one manual duplex ending that keeps nothing is the one you chose: **Abort scan**, answering no, or Ctrl-C at the flip prompt.

Empty page detection is deliberately skipped for these two partial PDFs, even when the profile has `enable_empty_page_detection = true`. When the passes disagree, a blank back side is evidence about why -- a sheet that double-fed, or one that did not feed at all -- and the partial PDFs exist so you can see exactly what each pass picked up. Removing blank pages would throw that evidence away. Every scan that does not hit a mismatch still has its blank pages removed as usual.

The feeder page cap applies to each pass separately, not to the whole job: each pass can feed up to 500 sheets.

!!! tip
    Always test with a few pages first to confirm page ordering before scanning a large batch. This helps verify that your scanner feeds pages in the expected direction.
