# Set Up ADF Duplex Scanning

Scan both sides of multi-page documents using your scanner's Automatic Document Feeder (ADF). saneless supports three ADF modes depending on your hardware.

## What you'll need

- A scanner with an ADF feeder
- saneless installed and configured ([Install on Bare Metal](install-bare-metal.md) or [Deploy with Docker Compose](deploy-docker-compose.md))

## The three ADF modes

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
resolution = 300
mode = "Color"
```

```bash
saneless scan --profile duplex --title "Contract"
```

### Manual Duplex

For scanners without hardware duplex, saneless coordinates a two-pass scan: first the front sides, then the back sides. saneless reverses and interleaves the pages to produce the correct page order.

The manual duplex flow:

1. Load pages face-up in the ADF
2. Start the scan -- saneless scans all front sides (Pass A)
3. A prompt appears asking you to flip the page stack
4. Flip the entire stack face-down and reload it in the ADF
5. Click Continue (web UI) or press Enter (CLI) -- saneless scans all back sides (Pass B)
6. saneless reverses the backs and interleaves them with the fronts: page 1 front, page 1 back, page 2 front, page 2 back, etc.
7. The assembled PDF is uploaded to paperless-ngx

To use manual duplex, set the source to any string containing both "manual" and "duplex" (case-insensitive):

```toml
[profiles.manual-duplex]
source = "Manual Duplex"
resolution = 300
mode = "Color"
```

```bash
saneless scan --profile manual-duplex --title "Double-sided doc"
```

In the CLI, saneless prompts you to flip the pages between passes. In the web UI, a flip prompt with Continue and Cancel buttons appears automatically.

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

## Page count mismatch handling

If the front and back pass produce different page counts during manual duplex, saneless does not discard your scans. Instead, it assembles the fronts and backs into separate PDFs and uploads both to paperless-ngx for manual review.

!!! tip
    Always test with a few pages first to confirm page ordering before scanning a large batch. This helps verify that your scanner feeds pages in the expected direction.
