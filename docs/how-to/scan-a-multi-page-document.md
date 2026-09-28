# Scan a Multi-Page Document

Build one PDF from several scans: a document of more than two pages on a flatbed, or a stack you hand-feed through the document feeder a sheet or a few sheets at a time. saneless scans, asks whether there is another page, and keeps going until you say the document is finished. Every page lands in one PDF, in the order you scanned it.

## What you'll need

- saneless installed and configured ([Install on Bare Metal](install-bare-metal.md) or [Deploy with Docker Compose](deploy-docker-compose.md))
- A scan profile for the source you want to use ([Configure Scan Profiles](configure-scan-profiles.md))
- For the CLI: an interactive terminal

## When to use it

A flatbed scans one page per scan. Without this option a four-page letter on the glass becomes four separate documents in paperless-ngx. With it, the four scans become one document.

It works with every source, not only the flatbed:

- **Flatbed** -- each scan is one page. Put the next page on the glass between scans.
- **Document feeder** -- each scan takes whatever is in the feeder, from one sheet to a whole stack. Use it to feed a document in batches, or to add pages to a document the feeder cannot take in one go.
- **Hardware duplex** -- each scan takes both sides of every sheet in the feeder.
- **Auto** -- each scan follows the profile's `auto_source_mode`, as it does without this option.

It is a choice you make for each scan, not a profile setting: nothing is stored on the profile, and the next scan starts without it unless you choose it again.

!!! note "Not available with manual duplex"
    A profile with `duplex = "manual"` already runs its own two-pass flow, with its own flip
    prompt, and it cannot be combined with this one. The web form disables the option for such a
    profile, and the server and `saneless scan --multi-page` both refuse the combination. Manual
    duplex itself is unchanged: see [Set Up ADF Duplex Scanning](set-up-adf-duplex.md#manual-duplex).

## Scan from the web UI

1. Choose the profile. Beneath the profile description, tick **Multiple pages**. The box is unticked every time the page loads, so tick it for each document. On a manual-duplex profile it is greyed out, with *"Not available with manual duplex."* in place of its help line.
2. Put the first page on the scanner (or load the first sheets into the feeder) and press **Scan**.
3. When the scan is done, the status area asks what to do next. It says how many pages the document holds so far, for example **4 pages kept so far.**, and offers four buttons:
    - **Scan next page** -- scans again and adds the new pages to the end of the document. Put the next page on the scanner first.
    - **Finish document** -- ends the scan and uploads every page kept as one document.
    - **Re-scan last page** (or **Re-scan last N pages**, when the last scan added N pages) -- throws away everything the last scan added and scans again straight away. Use it when a page came out crooked or you scanned the wrong one. On a feeder, put back every sheet of that scan.
    - **Abort scan** -- stops without uploading anything. The browser asks you to confirm first, naming the pages that will not be uploaded. Nothing is kept, and the job ends as Cancelled.
4. Repeat until the last page is in, then press **Finish document**.

While saneless waits for your answer the Scan button reads **Waiting for you…** and the scanner is idle: nothing moves until you press a button. Only the browser that started the scan gets the buttons. Anyone else with the page open sees one line saying what the scan is waiting for, such as *"Waiting for the next page..."*, and the history table shows the job as **Waiting for more pages**. Another scan submitted meanwhile waits in the queue until this document is finished.

A document finished with **Finish document** reads **Done**, like any other scan. The line under it counts the pages kept, for example "4 pages scanned, 0 blank removed, 4 uploaded".

## Scan from the command line

```bash
saneless scan --profile flatbed --title "Lease agreement" --multi-page
```

After each scan, `saneless scan` asks a one-letter question:

```
Scanning...
Waiting for the next page...
1 page kept so far. [n]ext, [r]e-scan last, [f]inish, [a]bort: n
Scanning...
Waiting for the next page...
2 pages kept so far. [n]ext, [r]e-scan last, [f]inish, [a]bort: f
Assembling PDF...
Uploading to paperless-ngx...
Done: Lease agreement
```

- **`n`** scans the next page, **`r`** throws away the last scan and scans again, **`f`** finishes and uploads the document, **`a`** aborts.
- **`a`** asks you to confirm, with No as the default. Only a yes aborts the scan. It uploads nothing, keeps nothing and exits with code 130. If the wait runs out while you are confirming, saneless holds on for your answer, for up to one more `operator_wait_timeout_seconds`, rather than finishing the document under you. A no, or no answer by then, finishes it as a timeout would.
- Letters are not case-sensitive, and only the first character of your answer counts. Anything else prints `Error: Choose one of: ...`, listing the letters you can use, and asks again.
- Anything you typed while the scanner was busy is discarded before each question, so a key pressed early cannot answer the next question for you.
- **Ctrl-C** or **Ctrl-D** at the question cancels the scan straight away, with no confirmation, however many pages are kept. It uploads nothing, keeps nothing and exits with code 130. Press `f` if you want to keep the pages.

`--multi-page` needs someone at a terminal to answer, so it is refused, with exit code 2 and before the scanner is opened, when stdin is not a terminal -- cron, a pipe, redirected input:

```
--multi-page needs an interactive terminal: saneless asks after each scan whether there is another page. Run it from a terminal, or scan from the web UI.
```

It is also refused, with exit code 2, on a manual-duplex profile:

```
Profile 'manual-duplex' is manual duplex, and --multi-page is not available with manual duplex. Scan without --multi-page, or choose another profile.
```

## Blank pages

With empty-page detection on for the profile (the default), a scan without **Multiple pages** removes blank pages on its own. A multi-page scan asks you instead, once per scan, and only when that scan produced a page that looks blank:

- After a one-page scan: **This page looks blank.** with **Skip page**, **Keep page** and **Re-scan page**.
- After a feeder scan of several pages: for example **Pages 2, 4 of the 6 just scanned look blank.** with **Skip blank pages**, **Keep blank pages** and **Re-scan all 6**.

**Skip** leaves the blank pages out and keeps the rest of that scan. **Keep** adds them to the document anyway, and a page you keep is never judged again, so it is never removed later. **Re-scan** throws away the whole scan and scans it again. After Skip or Keep you get the usual next-page question. At the terminal the same choices are `s`, `k` and `r`.

Skipped pages are reported the way detection reports removed pages everywhere else: in the **Removed as blank** note under the finished scan, for example `Removed as blank: pages 2, 4 of 12 scanned.`, and in `pages_removed_positions` in `saneless jobs --json`. A page you kept on purpose is not listed. Pages thrown away by a re-scan, or by a failed scan (below), were never part of the document, so they are not counted as scanned at all, and the page numbers match the document you built.

While every page so far has been skipped, the document is empty, so it cannot be finished: **Finish document** is greyed out, with *"Nothing to finish yet: every page so far was skipped as blank. Scan another page, or abort."* beneath it. At the terminal `[f]inish` is not offered, and typing `f` prints the same sentence. Scan another page, or abort.

See [How Empty Page Detection Works](../explanation/empty-page-detection.md) for how a page is judged blank.

## When a scan fails part way

If a scan fails with a scanner fault -- a jam, an open cover, a scanner that went away -- after the document already holds at least one page, the pages you have are not lost and the job does not end. saneless goes back to you instead:

- **The last scan failed, so none of its pages were added.** The line below it says what the scanner reported.
- The failed scan adds nothing to the document, not even the pages it managed to read. Clear the scanner, put back every page from the failed scan, then press **Scan again**.
- **Finish document** uploads the pages kept so far, and **Abort scan** stops without uploading anything, as before.

An empty feeder is treated the same way: pressing **Scan next page** before loading the next sheet brings up this question with *"The scanner reported: No paper detected in feeder"*. Load the sheet and press **Scan again**. At the terminal the question reads:

```
The last scan failed, so none of its pages were added: No paper detected in feeder
3 pages kept so far. Put back every page from the failed scan. [n] scan again, [f]inish, [a]bort:
```

Other failures still end the job:

- A scanner fault or an empty feeder on the **first** scan, or while re-scanning the only scan so far, is an ordinary scan failure (exit code 1), exactly as without this option. There is nothing to go back to.
- A full disk, a failed upload or a bug in saneless ends the job whatever the page count. Nothing is uploaded, and the pages kept so far are saved as a PDF in `failed/` in the data directory. When the failure came during a scan, the pages that scan had already read are saved beside it as a separate `(partial)` PDF. See [Troubleshoot a Failed Scan](troubleshoot-a-failed-scan.md).

## When nobody answers

Every question waits for at most `operator_wait_timeout_seconds` in the `[output]` section of the config file, 600 seconds (10 minutes) by default -- the same setting that bounds the manual-duplex flip prompt. Each question says what happens if it runs out, for example *"No answer within 10 minutes finishes the document with these 4 pages."*

A timeout **finishes** the document rather than failing it. Nobody chose to stop, so the pages are not thrown away, but nobody said the document was complete either:

- At the next-page question, or after a failed scan, the document is uploaded with the pages kept, as **Uploaded with a warning**: *"Finished after 4 pages because nobody answered within 10 minutes; the document may be missing pages."* `saneless scan` exits with code 7.
- At the blank-page question, the blank pages are skipped and the document is uploaded the same way, with the warning *"Finished after 4 pages because nobody answered about the blank pages within 10 minutes, so they were left out; the document may be missing pages."* Exit code 7.
- If the document holds no page at all when the wait runs out -- every page so far was skipped as blank -- nothing is uploaded. The job fails the way a scan whose every page looked blank fails: the skipped pages are kept as a PDF in `failed/`, and `saneless scan` exits with code 8. See [Every page looked blank](troubleshoot-a-failed-scan.md#every-page-looked-blank-exit-8).

This is deliberately the opposite of the manual-duplex flip prompt, where a timeout **fails** the job and uploads nothing.

To give yourself longer at the scanner, raise the setting; see [Configuration](../reference/configuration.md#output).

## How long a document can grow

No new scan starts once a document holds 500 pages or more. A scan that is already running is not cut short, and one feeder scan can add up to 500 sheets, so a document can reach 999 pages: 499 pages kept, then one more full feeder scan. When the limit stops the loop, the document is uploaded as **Uploaded with a warning**, for example *"Finished at 512 pages: no new scan starts once a document has 500 pages. Scan any remaining pages as a new document."* `saneless scan` exits with code 7. Scan the rest as a second document.

A feeder scan that itself reaches its own limit -- 500 sheets, or 50 for an Auto source sent through the feeder -- finishes the document at once, with no question about another page, and the warning names the sheet that was fed but not kept, for example *"Finished at 520 pages: one scan stops after 500 sheets, so sheet 501 was fed but not kept. Scan sheet 501 and any remaining pages as a new document."*

## Stopping saneless during a scan

Stopping saneless is not an abort. If the server stops or restarts while a document waits for your answer -- `docker compose restart`, an upgrade, a host shutdown -- or `saneless scan --multi-page` receives SIGTERM, or the hangup a closing terminal or SSH session sends:

- Nothing is uploaded.
- Every page kept so far is saved as one PDF in `failed/` in the data directory. If the stop comes while a blank-page question is open, the scan that question is about is saved beside it as a separate `(partial)` PDF, because you had not decided about it yet.
- In the web UI, the job ends as failed with "The server restarted before this scan finished", followed by where the pages were kept. `saneless scan` exits with code 143 after SIGTERM and 129 after a hangup, and its `Interrupted:` line names what was kept.

Only an abort you chose throws the pages away: **Abort scan** in the web UI, a confirmed `a`, or Ctrl-C or Ctrl-D at the terminal (exit 130).

A server stop while a scan is still feeding paper is handled the way it is for any scan: see [Deploy with Docker Compose](deploy-docker-compose.md#stopping-and-restarting). If saneless is killed outright, the next start recovers the pages from its scratch directory into `failed/` (see [A scan stopped by a crash or a power cut](troubleshoot-a-failed-scan.md#a-scan-stopped-by-a-crash-or-a-power-cut)).

## Exit codes at a glance

| Exit code | How a `saneless scan --multi-page` run got there |
|---|---|
| 0 | You finished the document with `f` |
| 1 | The first scan failed; a later scan failed in a way saneless cannot ask about, such as the working directory running out of room; or reading your answer at the terminal failed. Pages kept so far are saved in `failed/` |
| 2 | Refused before scanning: stdin is not a terminal, or the profile is manual duplex |
| 7 | A question timed out, or the document reached the page limit, and the pages kept were uploaded with a warning |
| 8 | A question timed out while every page so far had been skipped as blank |
| 129 | A hangup (closed terminal or SSH session); the pages kept are in `failed/` |
| 130 | You aborted with a confirmed `a`, or pressed Ctrl-C or Ctrl-D at a question; nothing is kept |
| 143 | SIGTERM; the pages kept are in `failed/` |

The other codes mean what they mean for any scan; see [CLI Commands](../reference/cli-commands.md#saneless-scan).
