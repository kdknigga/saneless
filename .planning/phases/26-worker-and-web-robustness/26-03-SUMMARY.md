---
phase: 26-worker-and-web-robustness
plan: 03
subsystem: web
tags: [htmx, picocss, sri, vendoring, offline, prek]
requires: []
provides:
  - "htmx 2.0.8 and Pico 2.1.1 served from /static/vendor/ with SHA-384 integrity"
  - "tests/test_vendor_assets.py (T10 pinning test)"
  - "prek top-level exclude + .gitattributes -text for static/vendor/"
affects:
  - "26-05 (htmx-config meta goes into the same base.html <head>)"
  - "26-11 (app.js tag removal in base.html)"
  - "26-12 (browser tests can move into CI now that no CDN is involved)"
tech-stack:
  added: ["htmx.org 2.0.8 (vendored file, 0BSD)", "@picocss/pico 2.1.1 (vendored file, MIT)"]
  patterns: ["Same-origin SRI with hashes recomputed from bytes at test time"]
key-files:
  created:
    - .gitattributes
    - src/saneless/web/static/vendor/htmx-2.0.8.min.js
    - src/saneless/web/static/vendor/pico-2.1.1.min.css
    - src/saneless/web/static/vendor/LICENSE-htmx.txt
    - src/saneless/web/static/vendor/LICENSE-pico.md
    - tests/test_vendor_assets.py
  modified:
    - .pre-commit-config.yaml
    - src/saneless/web/templates/base.html
    - src/saneless/web/templates/partials/flip.html
decisions:
  - "Removed the redundant xmlns=\"http://www.w3.org/2000/svg\" from the two inline SVGs in flip.html so the literal 'no http(s):// in any template' contract holds; the HTML parser puts inline <svg> in the SVG namespace on its own (verified in Chromium)"
metrics:
  duration: "~20 min"
  completed: 2026-09-14
requirements: [ROBU-09]
---

# Phase 26 Plan 03: Vendored htmx and Pico with SRI Summary

htmx 2.0.8 and Pico 2.1.1 now ship inside the package at `/static/vendor/`. Their bytes match the npm tarballs exactly, and `base.html` loads them with SHA-384 `integrity`. The prek hooks and git EOL conversion no longer touch that directory, and a fast test fails if the template hashes and the file bytes ever disagree.

## Tasks

| Task | Name | Commit | Files |
|------|------|--------|-------|
| 1 | Protect vendored bytes from the commit hooks | 135b60f | .pre-commit-config.yaml, .gitattributes |
| 2 (RED) | Failing vendor pinning tests | 6a81ad5 | tests/test_vendor_assets.py |
| 2 (GREEN) | Vendor files, point base.html at them | 1bbc455 | static/vendor/* (4), base.html, partials/flip.html |

Task 1 was committed on its own, before any vendored file was staged. That way the hook config in force at the vendoring commit already excluded the directory.

## Supply-chain verification (T-26-08)

Tarballs were fetched into the session scratchpad, not the repo. Their SHA-512 was compared against the registry `dist.integrity`, using both `npm view` and the registry JSON:

| Tarball | local sha512 == registry dist.integrity |
|---|---|
| htmx.org-2.0.8.tgz | `fm297iru0iWsNJlBrjvtN7V9zjaxd+69Oqjh4F/Vq9Wwi2kFisLcrLCiv5oBX0KLfOX/zG8AUo9ROMU5XUB44Q==` (match) |
| pico-2.1.1.tgz | `kIDugA7Ps4U+2BHxiNHmvgPIQDWPDU4IeU6TNRdvXQM1uZX+FibqDQT2xUOnnO2yq/LUHcwnGlu1hvf4KfXnMg==` (match) |

Extracted file sizes and SHA-384 hashes matched the research pins before copying (`cp`, no editor):

| File | bytes | sha384 |
|---|---|---|
| package/dist/htmx.min.js | 51250 | `/TgkGk7p307TH7EXJDuUlgG3Ce1UVolAOFopFekQkkXihi5u/6OCvVKyz1W+idaz` |
| package/css/pico.min.css | 83319 | `L1dWfspMTHU/ApYnFiMz2QID/PlP1xCW9visvBdbEkOLkSSWsP6ZJWhPw6apiXxU` |

Hashes of the committed bytes, taken after the commit had gone through the prek hooks:

```
git show HEAD:src/saneless/web/static/vendor/htmx-2.0.8.min.js  | openssl dgst -sha384 -binary | base64 -w0
/TgkGk7p307TH7EXJDuUlgG3Ce1UVolAOFopFekQkkXihi5u/6OCvVKyz1W+idaz
git show HEAD:src/saneless/web/static/vendor/pico-2.1.1.min.css | openssl dgst -sha384 -binary | base64 -w0
L1dWfspMTHU/ApYnFiMz2QID/PlP1xCW9visvBdbEkOLkSSWsP6ZJWhPw6apiXxU
```

## Wheel check

`uv build --wheel` (output in the scratchpad, not committed):

```
saneless-0.1.0-py3-none-any.whl
  saneless/web/static/vendor/
  saneless/web/static/vendor/LICENSE-htmx.txt
  saneless/web/static/vendor/LICENSE-pico.md
  saneless/web/static/vendor/htmx-2.0.8.min.js
  saneless/web/static/vendor/pico-2.1.1.min.css
all four vendored paths present
```

## Mutation self-check (UI-SPEC "change one byte of pico")

I flipped one byte in a scratch copy of `pico-2.1.1.min.css` and hashed it with `sri_sha384` from the test module. The result was `sha384-7NdlKi3jmkoW85Pg6VYDc43KMph6zlS0KAOwz6uEi26cH003ApLp3JPoplZUnKQC`, so the pin mismatch was detected.

## Verification

- `uv run pytest tests/test_vendor_assets.py tests/test_web.py -q`: 46 passed. `test_vendor_assets.py` has 8 tests. At RED, 4 of them failed (integrity count, file sizes, external URL, licences). The other 4 are 23.1 contract guards that already held.
- `uv run pytest -m "not browser and not sane_hardware" -q`: 1075 passed
- `uv run pytest -m browser tests/test_browser.py`: 31 passed against the real uvicorn app serving the vendored SRI assets, including `test_pico_css_applied` and `test_htmx_loaded`.
- `uv run ruff check tests`, `uv run ty check`, and `uv run pyrefly check src tests`: clean (0 errors). pyrefly's 4 warnings are old and in files this plan didn't touch.
- `uv run prek run --all-files`: pass
- `grep -rn "https\?://" src/saneless/web/templates/`: no matches. `cdn.jsdelivr` count in base.html: 0. Line 2 of base.html: `<html lang="en">`.
- `git check-attr text` on the vendored path gives `text: unset`. `git check-ignore` exits 1.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] SVG namespace URLs in flip.html failed the no-external-URL contract**
- **Found during:** Task 2 GREEN
- **Issue:** `partials/flip.html` had `xmlns="http://www.w3.org/2000/svg"` on two inline `<svg>` elements. They are namespace identifiers, not fetches, but they broke the plan's literal acceptance check (`grep -rn "https\?://" src/saneless/web/templates/` returns nothing) and T10.
- **Fix:** Removed both `xmlns` attributes. The HTML5 parser puts inline `<svg>` in the SVG namespace without them. I checked this in Chromium via Playwright: both SVGs have `namespaceURI` `http://www.w3.org/2000/svg`, all rect/path/polygon children are in the same namespace, and every shape has a non-zero bounding box. No test and no other phase-26 plan references flip.html.
- **Files modified:** src/saneless/web/templates/partials/flip.html
- **Commit:** 1bbc455

**2. [Setup] Worktree base reset**
- The worktree branch started from an older commit (merge-base a87b3dd), so it was reset to the expected base 44ef78d before any work, as the spawn instructions direct.

## TDD Gate Compliance

- RED: `test(26-03)` 6a81ad5
- GREEN: `feat(26-03)` 1bbc455 (after RED)
- REFACTOR: none needed

## Known Stubs

None.

## Self-Check: PASSED

- FOUND: .gitattributes, tests/test_vendor_assets.py, all four static/vendor files
- FOUND commits: 135b60f, 6a81ad5, 1bbc455
