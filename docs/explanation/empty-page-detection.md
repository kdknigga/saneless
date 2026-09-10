# How Empty Page Detection Works

## The Problem

ADF (Automatic Document Feeder) scanners with duplex capability scan both sides of every sheet. When scanning single-sided documents, the back of each page is blank -- but the scanner produces an image for it anyway. Without filtering, these blank pages end up in the final PDF, doubling the page count with useless white pages.

Even non-duplex ADF scanning can produce occasional blank pages if an empty sheet is mixed into the stack.

## The Algorithm

For each scanned page, saneless converts the image to grayscale and computes two statistics:

**Mean luminance**
:   The average pixel brightness across the entire image, on a scale from 0 (pure black) to 255 (pure white). A blank white page has a mean very close to 255.

**Standard deviation**
:   How much individual pixel values vary from the mean. A truly blank page has very low standard deviation because nearly every pixel is the same shade of white.

## The Detection Rule

A page is considered empty if **both** conditions are true:

- Mean luminance > `empty_page_mean_threshold` (default: **250.0**)
- Standard deviation < `empty_page_stddev_threshold` (default: **5.0**)

Both thresholds must be met simultaneously. This dual-threshold approach guards against two common false-positive scenarios:

**Why not mean alone?**
:   A page with a small mark, stamp, or scanner dust on an otherwise white background still has a high mean luminance. Mean alone would incorrectly discard it. But the mark creates enough pixel variation to push the standard deviation above the threshold, saving the page.

**Why not standard deviation alone?**
:   A uniformly gray page (from paper bleed-through or a tinted sheet) has very low standard deviation -- the pixels are uniform, just not white. Standard deviation alone would incorrectly discard it. But the gray tone keeps the mean luminance well below the threshold, saving the page.

Together, the two thresholds reliably identify pages that are both very white on average *and* very uniform -- the signature of a truly blank page.

## Tuning the Thresholds

The defaults work well for typical office scanners and white paper. If you find that pages are being incorrectly kept or discarded, adjust the thresholds in your scan profile configuration:

```toml
[profiles.duplex]
source = "ADF Duplex"
resolution = 300
mode = "color"
empty_page_mean_threshold = 245.0
empty_page_stddev_threshold = 8.0
```

**To be more aggressive** (discard more pages): lower the mean threshold or raise the stddev threshold.

**To be more conservative** (keep more pages): raise the mean threshold or lower the stddev threshold.

!!! tip
    If you are unsure whether detection is working correctly, run saneless with `--log-level DEBUG`. The log will show the exact mean and stddev values for each page along with the keep/discard decision.

## Disabling Empty Page Detection

If you want to keep all scanned pages regardless of content, disable detection in the profile:

```toml
[profiles.keep-all]
source = "ADF Duplex"
resolution = 300
mode = "color"
enable_empty_page_detection = false
```

This is useful when scanning documents where blank pages are intentional (such as forms with designated blank backs).
