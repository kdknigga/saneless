# How Empty Page Detection Works

## The problem

ADF (Automatic Document Feeder) scanners with duplex capability scan both sides of every sheet. When scanning single-sided documents, the back of each page is blank -- but the scanner produces an image for it anyway. Without filtering, these blank pages end up in the final PDF, doubling the page count with useless white pages.

Even non-duplex ADF scanning can produce occasional blank pages if an empty sheet is mixed into the stack.

## What is measured

saneless asks one question of each page: how much of it is ink? It measures that once, when the page is written to the scan's working directory, in five steps:

1. **Only the inside of the page is read.** 3% of the width is ignored at the left and right edges, and 3% of the height at the top and bottom. A dark frame, the scanner lid or a backing sheet around a smaller page usually falls in that margin, so it is not counted as ink. The margin is narrow on purpose: a page number printed 10 mm from the bottom edge, where word processors put footers, is still inside it.
2. **Each pixel is read in its darkest colour channel.** On a colour scan that is the lowest of red, green and blue. It sees a yellow highlighter or a light-blue pen that a greyscale conversion would render almost as light as the paper.
3. **Paper white is measured, not assumed.** It is the brightness of the page with its brightest 1% set aside, so tinted or recycled paper is judged against its own colour, and a few stray bright pixels cannot raise it.
4. **A pixel is ink when it is more than 40 levels darker than paper white**, on the 0-255 brightness scale. Scanner noise stays inside that margin. Faint marks do not: on paper measuring 247, anything darker than 207, light pencil included, counts as ink.
5. **Coverage is the ink pixels as a percentage of the inside of the page.**

The measurement is not a verdict. The verdict is made later, against the profile's threshold, so each profile can set its own.

### The area arithmetic

An A4 page scanned at 300 dpi is 2480 × 3508 pixels. The margin takes 74 pixels off each side and 105 off the top and the bottom -- about 6 mm and 9 mm -- which leaves 2332 × 3298 = 7,690,936 pixels. At the default threshold of 0.001%, a page is blank when no more than about 77 of those pixels are ink: one dot about 0.75 mm across. The threshold is a share of the page, so it means the same at any resolution.

A lone page number such as `- 7 -` in 11-point type covers several times that, so it is kept. A single line of typing covers far more.

## The detection rule

A page is blank, and is removed, when both of these are true:

- its paper white is at least 128, halfway up the brightness scale; and
- its ink coverage is at or below `empty_page_coverage_threshold`.

A page whose paper is darker than mid-grey is not blank-looking paper at all -- a dark cover with white type, a photograph, a coloured sheet -- so it is kept whatever its coverage.

The rule asks how much of a page is ink, not how bright it is on average. So a tinted or slightly noisy blank sheet is still blank, and so is a white page whose only mark is smaller than the threshold allows -- a speck of dust, or a dot under about 0.75 mm across at the default. A mark bigger than that, such as a lone page number, keeps the page.

### Keep when unsure

The default is set to keep pages, not to tidy them. A blank page that is kept costs one extra page in paperless-ngx. A page with something on it that is removed is gone, because removed pages are not kept anywhere. So the default keeps a lone page number and a few faint pencil lines, and in exchange it does not promise to remove every blank sheet.

Dust is the usual reason a blank sheet survives. A blank back with a few specks on it is removed, but one with more than about ten small specks can carry enough ink to be kept. That is by design.

## Tuning the threshold

There is one setting, per profile: `empty_page_coverage_threshold`, the most ink a blank page may carry, as a percentage of the inside of the page. The default is `0.001`. Any value from 0 to 100 is accepted, and a value outside that range is refused when the config loads.

- **To keep more pages** -- faint pencil, a light stamp, a nearly empty form that detection removed -- lower it. At `0`, only a page with no ink pixel at all is removed.
- **To remove more pages** -- dusty blank backs that keep getting through -- raise it, with care and in small steps. A lone page number measures only a few thousandths of a percent, so a value such as `0.01` removes pages like that too.

```toml
[profiles.pencil-notes]
source = "ADF"
resolution = 300
mode = "Gray"
empty_page_coverage_threshold = 0
```

The margin, the paper-white measurement and the 40-level ink margin are fixed; the threshold is the only thing to tune.

!!! tip
    Every page gets one line in the saneless log at the default `INFO` level, whether it was kept or removed:

    ```
    Blank-page check: page 3 of 12 (a-0003.png): ink 0.00034% of the inset, paper white 249, threshold 0.001% -> REMOVE
    ```

    The coverage is written at full precision, so you can see how far each page was from the threshold and pick a value between the pages you want kept and the ones you want removed. `saneless scan` writes these lines to its log file (`log_file` under `[output]`); `saneless serve` writes them to its stderr, which `docker logs` or `journalctl` shows.

### Turning detection off

If you want to keep every scanned page regardless of content, disable detection in the profile:

```toml
[profiles.keep-all]
source = "ADF Duplex"
resolution = 300
mode = "color"
enable_empty_page_detection = false
```

This is useful when scanning documents where blank pages are intentional, such as forms with designated blank backs.

## Removed pages

Removed pages are not kept anywhere. They are deleted with the scan's working directory when the scan ends, like every other page of a finished scan. Instead, saneless names them, by where they were in the scanned document:

```
Removed as blank: pages 2, 4 of 4 scanned.
```

You see this note:

- in the web UI, under the page counts in the status area and in the job history;
- in `saneless jobs --json`, as the `pages_removed_positions` list;
- on stdout after the `Done:` line, for `saneless scan`.

The numbers are page numbers in the document as it was scanned, before anything was removed. For a manual duplex scan that is the interleaved document, so page 2 is the back of the first sheet. The numbering is the same for every source, and it is the numbering the log lines above use.

The note is information, not a warning. A scan that removed blank pages is a plain success: **Done** in the web UI, exit 0 from `saneless scan`, and `"warning": null` in `saneless jobs --json`. If a page with something on it was taken for a blank one, rescan that sheet, and lower the threshold for the profile.

Detection does not run on the two PDFs a manual duplex mismatch produces, or on the pages kept after a failed scan. Those exist so you can see exactly what the scanner picked up.

## When every page is blank

If detection removes every page of a scan, nothing is uploaded and the scan fails: `saneless scan` exits 8, and the web UI shows the error on the job. The pages are not lost. saneless assembles every page it scanned, before detection removed any, into one PDF under `failed/` in its data directory, and the error names the file. If that PDF cannot be built -- the disk is short of room, say -- the page files are kept there instead, and the error names their directory.

If the document really is blank, delete the file. If it is not, lower `empty_page_coverage_threshold` for the profile as described under [Tuning the threshold](#tuning-the-threshold), or turn detection off for it, then scan again. Or upload the kept PDF yourself. [Troubleshoot a Failed Scan](../how-to/troubleshoot-a-failed-scan.md#every-page-looked-blank-exit-8) covers the same failure from its exit code.

A scanner that returns no pages at all is reported separately, as "No pages were scanned", and an empty feeder still reports that no paper was detected. The all-blank failure therefore always means the scanner did return pages and detection judged all of them blank, so the two situations are never confused.

Empty page detection is the only place saneless judges what is on a page. The scanner backend never discards a page for its content: with detection disabled, a page that is uniformly white or uniformly black is kept and assembled like any other. The only pages the backend skips are the ones it could not read at all -- an image with zero width or height, or a buffer far too small to be a real page -- and each of those is logged individually with its page number. Those are integrity checks rather than blank-page detection: they ask whether the scanner returned a decodable image, never whether the page was worth keeping.
