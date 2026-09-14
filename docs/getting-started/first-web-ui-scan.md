# First Web UI Scan

Walk through every element of the saneless web interface to scan a document and verify it in paperless-ngx.

## Prerequisites

- saneless running (`saneless serve` or a Docker container) -- see [Quick Start](quick-start.md)
- A browser on the same network as the saneless host
- paperless-ngx accessible with a valid API token in your saneless configuration

## Open the web UI

Navigate to `http://<host>:8080` in your browser, replacing `<host>` with the IP address or hostname of the machine running saneless (use `localhost` if running locally). You will see the scan form, a status area, and a job history table.

## Fill in scan details

The scan form has four fields:

1. **Profile** -- A dropdown listing your configured scan profiles. Select the profile that matches your scan type (flatbed, ADF, duplex). The profile named "default" is selected by default. Profiles control scanner source, resolution, color mode, and default metadata. See [Configure Scan Profiles](../how-to/configure-scan-profiles.md) to create additional profiles.

2. **Title** -- A text field for the document title. Enter a descriptive name, for example "Electricity Bill March 2026". If you leave this field empty, saneless auto-generates a title with a timestamp.

3. **Tags** -- A multi-select dropdown populated from your paperless-ngx instance. Select one or more tags to apply to the scanned document. Click the refresh button (circular arrow icon) next to the label to reload the tag list from paperless-ngx if you have recently added new tags.

4. **Correspondent** -- A single-select dropdown populated from your paperless-ngx instance, with "No correspondent" as the default. Select a correspondent to associate with the document. Click the refresh button next to the label to reload the correspondent list.

## Start the scan

Click the **Scan** button at the bottom of the form. The button disables and shows "Scanning..." while the job runs. Do not close the browser tab during scanning.

## Monitor progress

The status area below the form updates as the scan progresses through these stages:

- **Scanning** -- The scanner is acquiring pages.
- **Waiting for flip** -- Manual duplex profiles only: the front sides are scanned and saneless is waiting for you to flip the stack (see below).
- **Scanning backs** -- Manual duplex profiles only: the scanner is acquiring the back sides.
- **Assembling** -- Pages are being assembled into a PDF.
- **Uploading** -- The PDF is being uploaded to paperless-ngx.
- **Done** -- The document has been successfully uploaded.

A thumbnail of the first scanned page appears once the first page is acquired.

If your profile uses manual duplex scanning, a flip prompt appears after the front sides are scanned. Keep the pages in the same order, flip the whole stack over the long edge, load it back into the feeder, and click **Continue** to scan the back sides. As soon as the click is received, the prompt is replaced by a short confirmation, and the status moves on once the back sides start scanning. To stop instead, click **Abort scan**: the back sides are not scanned and nothing is uploaded. If nobody answers the prompt within `flip_timeout_seconds` (10 minutes by default), the scan fails the same way. See [Set Up ADF Duplex Scanning](../how-to/set-up-adf-duplex.md#manual-duplex).

If an error occurs, the status area displays the error message with details about what went wrong.

## Check job history

The job history table at the bottom of the page lists recent scan jobs with four columns:

- **Time** -- When the scan was initiated
- **Profile** -- Which scan profile was used
- **Title** -- The document title
- **Status** -- Current state of the job (Done, Error, Scanning, etc.)

The history table updates automatically when a job finishes.

## Verify in paperless-ngx

Open your paperless-ngx web interface and search for the document title you entered (for example, "Electricity Bill March 2026"). The scanned PDF should appear with the correct title, tags, and correspondent.

## Next steps

- [Configure Scan Profiles](../how-to/configure-scan-profiles.md) -- Set up profiles for different scan types (duplex, high resolution, grayscale).
- [Set Up ADF Duplex Scanning](../how-to/set-up-adf-duplex.md) -- Scan double-sided multi-page documents with an automatic document feeder.
- [First CLI Scan](first-cli-scan.md) -- Scan documents from the command line.
