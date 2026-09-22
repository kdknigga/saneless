# First Web UI Scan

Walk through every element of the saneless web interface to scan a document and verify it in paperless-ngx.

## Prerequisites

- saneless running (`saneless serve` or a Docker container) -- see [Quick Start](quick-start.md)
- A browser on the same network as the saneless host
- paperless-ngx accessible with a valid API token in your saneless configuration

## Open the web UI

Navigate to `http://<host>:8080` in your browser, replacing `<host>` with the IP address or hostname of the machine running saneless (use `localhost` if running locally). Top to bottom, the page is: a **System status** panel, the scan form, a status area, and a job history table.

## Check the System status panel first

The panel at the top answers "can this thing scan right now?" before you feed any paper. It has one row per check -- six of them -- each with a tick, a warning triangle or a cross, a short sentence, and -- when something is wrong -- the next step to take:

- **Configuration** -- whether a configuration file was loaded, and whether one is being ignored.
- **Scanner** -- whether a scanner is reachable, and which one.
- **Paperless** -- whether saneless can reach paperless-ngx and whether the API token works.
- **Profiles** -- how many scan profiles are configured.
- **Fallback** -- whether a folder is set up to keep scans if paperless-ngx is down.
- **Data folder** -- whether saneless can write its durable state.

Under the rows, a line says when the checks last ran, in your server's local time. The **Check again** button re-runs them all immediately: press it after plugging the scanner back in rather than reloading the page. While a scan is running the scanner check is paused -- saneless will not interrupt a scan to probe the device -- and the panel says so.

A red **Configuration** row naming a `config.toml` means saneless found a file under the name it used to read: rename that file to `saneless.toml` and restart saneless, and the row goes green with your settings loaded.

These are the same checks `saneless doctor` prints from a terminal. If the Paperless row is red because the API token has not been set, the **Scan** button is greyed out with the reason beneath it, and no scan can start until the token is fixed in the config file.

## Fill in scan details

Each control has one line of help text beneath it. The form has up to four fields -- an operator can hide Tags and Correspondent with `show_tags` and `show_correspondent` in the `[web]` section of the config file, for a simpler form; see [Configuration](../reference/configuration.md#web). Hiding them does not change what a scan does: the profile's default tags and correspondent still apply.

1. **Profile** -- A dropdown listing your configured scan profiles. Select the one that matches your scan type. Beneath it, a description line explains the selected profile in a sentence ("Feeder, double-sided", for example) and updates as you change the selection. The profile named "default" is selected when the page loads. Profiles control scanner source, resolution, color mode, and default metadata. See [Configure Scan Profiles](../how-to/configure-scan-profiles.md) to create additional profiles.

2. **Title** -- A text field for the document title, helped by *"What this document should be called in paperless-ngx."* Enter a descriptive name, for example "Electricity Bill March 2026". Leave it empty and saneless titles the document `Scan <date time>` in the server's local time.

3. **Tags** -- A list of checkboxes, one per tag in your paperless-ngx instance, sized to tap with a thumb. Tick as many as you like. Above the list is a **Filter tags** box: type in it to narrow the list, and tags you have already ticked stay ticked and stay visible even when they do not match the filter, so filtering can never silently drop a tag from your scan. The circular-arrow button beside the **Tags** heading reloads the list from paperless-ngx if you have just added tags there.

4. **Correspondent** -- A single-select dropdown populated from your paperless-ngx instance, helped by *"Who sent this document? Optional."* and with "No correspondent" as the default. Click the refresh button next to the label to reload the correspondent list.

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

When a scan finishes, a line beneath the result sums up what happened to the paper -- "12 pages scanned, 2 blank removed, 10 uploaded". A scan that ended in an error or was cancelled has no counts to show, and shows none.

If another scan is already running when you press Scan, the status area keeps following *your* job rather than switching to whichever one is current, and tells you where you are in the queue -- "Waiting for 'Tax return' to finish (1 ahead of you)".

A thumbnail of the first scanned page appears once the first page is acquired.

If your profile uses manual duplex scanning, a flip prompt appears after the front sides are scanned. Keep the pages in the same order, flip the whole stack over the long edge, load it back into the feeder, and click **Continue** to scan the back sides. As soon as the click is received, the prompt is replaced by a short confirmation, and the status moves on once the back sides start scanning. To stop instead, click **Abort scan** and confirm -- the browser asks *"Abort this scan? It will stop and cannot be resumed."* -- after which the back sides are not scanned and nothing is uploaded. If nobody answers the prompt within `flip_timeout_seconds` (10 minutes by default), the scan fails the same way. See [Set Up ADF Duplex Scanning](../how-to/set-up-adf-duplex.md#manual-duplex).

**Only the browser that started the scan gets those buttons.** Somebody else watching the same page sees "Waiting for the stack to be flipped" instead, because the person holding the paper is the one who should answer. Everything else -- the state, title, counts and thumbnail -- is the same for both.

If an error occurs, the status area says in plain words what kind of problem it was and what to do next. Beneath that is a collapsed **Technical details** section: open it for the underlying message when you want to report the problem or dig further.

## Check job history

The job history table at the bottom of the page lists recent scan jobs with four columns:

- **Time** -- When the scan was started, in the server's local timezone with the zone named. In a container that means setting `TZ`; without it the times read as UTC
- **Profile** -- Which scan profile was used
- **Title** -- The document title, with the page counts on a second line beneath it for jobs that recorded them
- **Status** -- Current state of the job (Complete, Failed, Cancelled, Saved to folder, Scanning, and so on). The table says **Complete** where the status area above it says **Done**; they are the same state

The history table updates automatically when a job finishes.

## Verify in paperless-ngx

Open your paperless-ngx web interface and search for the document title you entered (for example, "Electricity Bill March 2026"). The scanned PDF should appear with the correct title, tags, and correspondent.

## Next steps

- [Configure Scan Profiles](../how-to/configure-scan-profiles.md) -- Set up profiles for different scan types (duplex, high resolution, grayscale).
- [Set Up ADF Duplex Scanning](../how-to/set-up-adf-duplex.md) -- Scan double-sided multi-page documents with an automatic document feeder.
- [First CLI Scan](first-cli-scan.md) -- Scan documents from the command line.
