# Phase 18: Scanned Page Size / Scan Area Control - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-03-23
**Phase:** 18-automatic-scanned-page-size-detection-or-user-specified-paper-size-to-avoid-capturing-the-full-scanner-bed
**Areas discussed:** Scan area method, Paper size config, ADF vs flatbed, Auto-profile impact

---

## Scan Area Method

| Option | Description | Selected |
|--------|-------------|----------|
| SANE geometry (Recommended) | Set br-x/br-y before scanning — scanner only acquires the target area. Faster, less data. Standard approach matching scanimage behavior. | ✓ |
| Post-scan crop | Scan full bed, crop with Pillow after. Simpler but wastes scan time on unused area. | |
| Both options | Try SANE geometry first, fall back to post-scan crop if geometry options aren't available on the device. | |

**User's choice:** SANE geometry (Recommended)
**Notes:** None

### Follow-up: Fallback when geometry not supported

| Option | Description | Selected |
|--------|-------------|----------|
| Scan full bed silently | Log a warning and proceed with full-bed scan — no error, user still gets their document. | |
| Scan full bed + crop | Fall back to post-scan Pillow crop so the output PDF still matches the requested paper size. | ✓ |
| Error out | Raise a ScanError telling the user their scanner doesn't support scan area control. | |

**User's choice:** Scan full bed + crop
**Notes:** Combined decision: SANE geometry primary, Pillow crop fallback

---

## Paper Size Config

| Option | Description | Selected |
|--------|-------------|----------|
| Named presets (Recommended) | paper_size field with values like "a4", "letter", "legal", "full" (default). Maps to known mm dimensions internally. | ✓ |
| Named + custom mm | Same presets plus paper_size = "210x297" for arbitrary dimensions in mm. More flexible but more complex parsing. | |
| Raw mm only | paper_width_mm / paper_height_mm numeric fields. Maximum flexibility but no convenience names. | |

**User's choice:** Named presets (Recommended)
**Notes:** None

### Follow-up: Default value

| Option | Description | Selected |
|--------|-------------|----------|
| "full" (Recommended) | Default to full scanner bed — current behavior preserved. Users opt in to paper size constraint. Zero-change upgrade path. | ✓ |
| "a4" | Default to A4 (most common worldwide). Users with Letter scanners would need to change config. | |
| "auto" | Try to infer from scanner geometry — if max scan area matches a known paper size, use it. Falls back to full bed. | |

**User's choice:** "full" (Recommended)
**Notes:** None

---

## ADF vs Flatbed

| Option | Description | Selected |
|--------|-------------|----------|
| Both flatbed and ADF (Recommended) | Set geometry for all scan types. Consistent behavior regardless of source. | |
| Flatbed only | Only set geometry for flatbed scans. ADF scans always use full width. | |
| You decide | Claude picks the best approach based on how SANE ADF geometry actually works. | ✓ |

**User's choice:** You decide
**Notes:** Deferred to Claude's discretion

---

## Auto-profile Impact

| Option | Description | Selected |
|--------|-------------|----------|
| No, keep "full" (Recommended) | Auto-generated profiles default to full bed. Users who care about paper size configure it manually. Avoids wrong guesses. | ✓ |
| Infer from geometry | Read scanner's max br-x/br-y and match to nearest standard paper size. Could be wrong if scanner bed is larger than common paper. | |
| You decide | Claude picks based on what makes sense for the auto-profile generation logic. | |

**User's choice:** No, keep "full" (Recommended)
**Notes:** None

---

## Claude's Discretion

- ADF geometry behavior — whether paper_size applies to ADF scans, flatbed only, or both
- Additional paper size presets beyond A4, Letter, Legal
- SANE geometry option parsing approach
- Pillow crop alignment (centered vs top-left)
- Test structure

## Deferred Ideas

- Custom paper dimensions via "WxH" string format
- Auto-detection of paper edges via image analysis
- Web UI dropdown for paper size per-scan
