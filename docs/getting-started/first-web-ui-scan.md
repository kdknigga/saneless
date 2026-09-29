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

These are the same checks `saneless doctor` prints from a terminal. If the Paperless row is red because the API token or the paperless-ngx address has not been set, the **Scan** button is greyed out with the reason beneath it, and no scan can start until the config file is fixed.

## Fill in scan details

Each control has one line of help text beneath it. The form has up to five fields -- an operator can hide Tags and Correspondent with `show_tags` and `show_correspondent` in the `[web]` section of the config file, for a simpler form; see [Configuration](../reference/configuration.md#web). Hiding them does not change what a scan does: the profile's default tags and correspondent still apply.

1. **Profile** -- A dropdown listing your configured scan profiles. Select the one that matches your scan type. Beneath it, a description line explains the selected profile in a sentence ("Scans both sides of every page using the document feeder.", for example) and updates as you change the selection. The profile named "default" is selected when the page loads, or, when `auto-profiles` made it an exact copy of another profile, that profile, which then stands in for it: the dropdown lists the two only once. Profiles control scanner source, resolution, color mode, and default metadata. See [Configure Scan Profiles](../how-to/configure-scan-profiles.md) to create additional profiles.

2. **Multiple pages** -- A checkbox beneath the profile description, helped by *"Asks after each scan whether there is another page, and puts every page in one document."* Tick it to build one document from several scans, such as a letter of more than two pages on the flatbed. It is unticked every time the page loads, and on a manual duplex profile it is greyed out with *"Not available with manual duplex."* beneath it. See [Scan a Multi-Page Document](../how-to/scan-a-multi-page-document.md).

3. **Title** -- A text field for the document title, helped by *"What this document should be called in paperless-ngx."* Enter a descriptive name, for example "Electricity Bill March 2026". Leave it empty and saneless titles the document `Scan <date time>` in the server's local time.

4. **Tags** -- A list of checkboxes, one per tag in your paperless-ngx instance, sized to tap with a thumb. Tick as many as you like. Above the list is a **Filter tags** box: type in it to narrow the list, and tags you have already ticked stay ticked and stay visible even when they do not match the filter, so filtering can never silently drop a tag from your scan. The circular-arrow button beside the **Tags** heading reloads the list from paperless-ngx if you have just added tags there.

5. **Correspondent** -- A single-select dropdown populated from your paperless-ngx instance, helped by *"Who sent this document? Optional."* and with "No correspondent" as the default. Click the refresh button next to the label to reload the correspondent list.

## Start the scan

Click the **Scan** button at the bottom of the form. The button disables and shows "Scanning..." while the job runs. Do not close the browser tab during scanning.

## Monitor progress

The status area below the form updates as the scan progresses through these stages:

- **Scanning** -- The scanner is acquiring pages.
- **Waiting for flip** -- Manual duplex profiles only: the front sides are scanned and saneless is waiting for you to flip the stack (see below).
- **Scanning backs** -- Manual duplex profiles only: the scanner is acquiring the back sides.
- **Waiting for more pages**, **Waiting: blank pages found** or **Waiting: last scan failed** -- Multiple pages only: saneless is waiting for you to say whether there is another page, what to do about pages that look blank, or what to do after a scan that failed (see below).
- **Assembling** -- Pages are being assembled into a PDF.
- **Uploading** -- The PDF is being uploaded to paperless-ngx.
- **Done** -- The document has been successfully uploaded.

When a scan finishes, a line beneath the result sums up what happened to the paper -- "12 pages scanned, 2 blank removed, 10 uploaded". A scan that ended in an error or was cancelled has no counts to show, and shows none.

If another scan is already running when you press Scan, the status area keeps following *your* job rather than switching to whichever one is current, and tells you where you are in the queue -- "Waiting for 'Tax return' to finish (1 ahead of you)". The running scan is named only if your browser started it too; otherwise it reads "Waiting for 'Scan (title hidden)' to finish".

A thumbnail of the first scanned page appears once the first page is acquired, in the browser that started the scan.

If your profile uses manual duplex scanning, a flip prompt appears after the front sides are scanned. Keep the pages in the same order, flip the whole stack over the long edge, load it back into the feeder, and click **Continue** to scan the back sides. As soon as the click is received, the prompt is replaced by a short confirmation, and the status moves on once the back sides start scanning. To stop instead, click **Abort scan** and confirm -- the browser asks *"Abort this scan? It will stop and cannot be resumed."* -- after which the back sides are not scanned and nothing is uploaded. If nobody answers the prompt within `operator_wait_timeout_seconds` (10 minutes by default), the scan fails the same way. See [Set Up ADF Duplex Scanning](../how-to/set-up-adf-duplex.md#manual-duplex).

**Only the browser that started the scan gets those buttons.** Somebody else watching the same page sees "Waiting for the stack to be flipped" instead, because the person holding the paper is the one who should answer.

If you ticked **Multiple pages**, a question appears after every scan instead, with the number of pages kept so far and the buttons **Scan next page**, **Finish document**, **Re-scan last page** and **Abort scan**. Put the next page on the scanner and press **Scan next page**; when the last page is in, press **Finish document** and every page is uploaded as one document. While the question is open the Scan button reads **Waiting for you…**. If nobody answers within `operator_wait_timeout_seconds` (10 minutes by default), the document is finished with the pages kept and uploaded with a warning, rather than failed as a flip prompt would be. Here too, only the browser that started the scan gets the buttons. See [Scan a Multi-Page Document](../how-to/scan-a-multi-page-document.md) for the blank-page and failed-scan questions.

**Only the browser that started a scan sees what it is.** saneless recognises that browser by a cookie it sets when you press Scan. The cookie lasts a year, so a browser still knows its own scans after a restart. Anyone else on your network who opens the page sees the state, the outcome and the page counts. They see the title as "Scan (title hidden)" and no thumbnail. A different browser on your own phone or laptop counts as somebody else.

If an error occurs, the status area says in plain words what kind of problem it was and what to do next. Beneath that is a collapsed **Technical details** section. In the browser that started the scan it holds the underlying message, for when you want to report the problem or dig further. Folders on the server are named by the setting that holds them, so a PDF that saneless kept reads as `failed/<file>.pdf`. Every other browser sees a sentence pointing at the server instead: there, `saneless jobs --json` and the log have the full message.

## Check job history

The job history table at the bottom of the page lists recent scan jobs with four columns:

- **Time** -- When the scan was started, in the server's local timezone with the zone named. In a container that means setting `TZ`; without it the times read as UTC
- **Profile** -- Which scan profile was used
- **Title** -- The document title, with the page counts on a second line beneath it for jobs that recorded them. A scan another browser started shows "Scan (title hidden)"
- **Status** -- Current state of the job (Complete, Uploaded with a warning, Failed, Cancelled, Saved to folder, Scanning, and so on). The table says **Complete** where the status area above it says **Done**; they are the same state. A scan that was uploaded but did not go cleanly -- a sheet the scanner skipped, say -- reads **Uploaded with a warning** in both places, and the status area gives the warning beneath it

The history table updates automatically when a job finishes.

## Verify in paperless-ngx

Open your paperless-ngx web interface and search for the document title you entered (for example, "Electricity Bill March 2026"). The scanned PDF should appear with the correct title, tags, and correspondent.

## Next steps

- [Configure Scan Profiles](../how-to/configure-scan-profiles.md) -- Set up profiles for different scan types (duplex, high resolution, grayscale).
- [Set Up ADF Duplex Scanning](../how-to/set-up-adf-duplex.md) -- Scan double-sided multi-page documents with an automatic document feeder.
- [First CLI Scan](first-cli-scan.md) -- Scan documents from the command line.
