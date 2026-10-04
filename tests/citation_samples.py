"""
The planning-citation pattern and the sample identifiers its tests need.

The citation guard and the matching commit hook reject any line under src/,
scripts/, tests/, the workflows, .dockerignore, the decision records under
docs/explanation/decisions/, CONTRIBUTING.md, the Dockerfile or the compose
file that points at the planning records. The guard's own pattern, and the
identifiers that prove it bites, cannot be written down anywhere the hook reads
without the hook failing on them, so they live here: this is the one file the
hook skips, and a test pins that it stays the only one.
"""

from __future__ import annotations

import re

# The identifier shapes the planning records use: decision, finding, research
# and requirement IDs, threat IDs, the code review's report and nit IDs, phase
# and plan numbers, numbered research pitfalls, the context, research and UI
# spec record names, and the plan, summary, verification and review file names
# with their .md suffix. Other planning file names, such as the roadmap's, are
# not matched. An ID is matched only when the pattern names its prefix, so a
# requirement or audit family the records start using is added here, with a
# sample, or it gets through; an ID may end in one lowercase letter. The
# single-letter families need two digits or more, so a one-digit ID such as a
# research file's first contradiction gets through. That trade is deliberate:
# N-1 is ordinary arithmetic. The requirement and audit families need two
# digits too, because some of their prefixes open ordinary text with one, such
# as a PDF version or TEST-NET-1. The architecture, enumeration and multi-page
# requirement IDs are covered with one digit, because their prefixes appear in
# no other identifier shape. It also rejects a Python, HTML, CSS or JavaScript
# file named with a line number after a colon, the name of the assistant
# instructions file, and a phase referred to without its number: line numbers
# drift, and the other two cannot be looked up by a reader of the shipped tree
# either. It also rejects some domain prose ("phase 2", "the next phase", and
# ACME's "HTTP-01 challenge", which the HTTP family matches); reword it ("the
# second pass", "the following step", "the HTTP challenge"). It is a net for the
# common shapes, not a proof; review catches what it misses. The
# no-planning-citations hook in .pre-commit-config.yaml carries the same
# pattern, and a test in tests/test_deployment_config.py keeps the two
# identical.
PLANNING_CITATION = re.compile(
    r"\b(R[0-9]+-)?(C|D|F|M|N|R|S|U|W|CR|IN|WR)-[0-9]{2,}[a-z]?\b|\b(A|"
    r"API|APPL|ARCH|CFG|CTR|DARK|DLVR|DOCS|DPLX|ENUM|EXC|HARD|MPG|OUTC|ROBU|"
    r"SCAN|SCNR|STOR|SWP|TEST)-[0-9]+[a-z]?\b|\b(ARC|CLI|DOC|JOB|OPS|PIP|PPL|SCN|"
    r"SEC|UI|WEB)-[0-9]{2,}[a-z]?\b|\b(AP|AR|AUDIT|CF|CI|CONF|CORE|DEP|DPI|FW|GAP|"
    r"GATE|GS|HLTH|HTTP|LOG|NET|OBS|PDF|PIN|PIPE|PKG|PLSS|PROF|PS|RH|TOOL|"
    r"TQUAL|UIX|XC|P[0-9]+)-[0-9]{2,}[a-z]?\b|\bT-[0-9]+-[0-9]+[a-z]?\b|"
    r"\bT-[0-9]{2,}[a-z]?\b|"
    r"\b([Pp]hases?|PHASES?)[ -]?[0-9]+|\b[Pp]lan [0-9]+(\.[0-9]+)?-[0-9]+\b|"
    r"\bPitfall #?[0-9]+|UI-SPEC|\b(CONTEXT|RESEARCH)\b|\b(PLAN|"
    r"SUMMARY|VERIFICATION|REVIEW)\.md\b|Open Question|"
    r"[Pp]er user decision|\.planning/|\.(py|html|css|js):[0-9]+|CLAUDE\.md|"
    r"\b([Tt]his|[Tt]hat|[Tt]hese|[Tt]hose|[Tt]hree|[Ee]arlier|[Pp]revious|"
    r"[Pp]rior|[Ll]ater|[Nn]ext|[Ll]ast) phases?\b"
)

# Text the pattern must match: one of each identifier family a reader is
# likely to paste, including the multi-page IDs with and without a leading
# zero, and IDs with a letter suffix.
CITATION_SAMPLES = (
    "ENUM-03",
    "ARCH-03",
    "MPG-01",
    "MPG-4",
    "D-20",
    "N-59",
    "T-50-35-01",
    "Phase 50",
    "plan 50-35",
    "50-RESEARCH.md",
    "UI-SPEC",
    ".planning/",
    "checks.py:21",
    "worker.py:1288",
    "sane.py:188-213",
    "CLAUDE.md",
    "this phase",
    "three phases",
    "That phase",
    "earlier phases",
    "pre-phase-23",
    "ARC-10",
    "CLI-19",
    "DOC-23",
    "JOB-14",
    "OPS-13",
    "PIP-08",
    "PPL-17",
    "SCN-16",
    "SEC-15",
    "UI-17",
    "WEB-16",
    "T-14",
    "Phases 49 and 50",
    "phases 49-50",
    "PHASE 51",
    "Phase51",
    "index.html:42",
    "app.css:120",
    "htmx.js:7",
    "R-04",
    "AP-02",
    "AR-03",
    "AUDIT-01",
    "CF-01",
    "CI-01",
    "CONF-01",
    "CORE-02",
    "DEP-03",
    "DPI-01",
    "DOC-FW-02",
    "GAP-02",
    "GATE-01",
    "DOC-GS-01",
    "HLTH-01",
    "HTTP-02",
    "LOG-01",
    "NET-01",
    "OBS-01",
    "PDF-01",
    "PIN-01",
    "PIPE-01",
    "PKG-01",
    "PLSS-01",
    "PROF-01",
    "PS-01",
    "RH-08",
    "TOOL-01",
    "TQUAL-01",
    "UIX-01",
    "XC-01",
    "P12-01",
    "the previous phase",
    "a later phase",
    "next phase",
    "last phase",
    "prior phases",
    "these phases",
    "those phases",
    "D-12a",
    "N-42c",
    "WR-04b",
    "DEP-03a",
    "MPG-4b",
    "WEB-16a",
    "T-14a",
    "F-10",
)

# Ordinary text the pattern must leave alone: encodings, standards and hash
# names, a paper size, an off-by-one, SQLite's EXPLAIN QUERY PLAN, a PDF
# header, a documentation network and an elliptic curve.
NON_CITATIONS = (
    "UTF-8",
    "ISO-8601",
    "SHA-384",
    "A4",
    "N-1",
    "EXPLAIN QUERY PLAN",
    "every phase of the request",
    "the call phase",
    "spooling phase",
    "pool phases",
    "test_x.py::test_y",
    "tests/test_cli.py",
    "a two-phase commit",
    "phase out",
    "seeded.md:3",
    "WEB-1",
    "T-1",
    "data.json:4",
    "%PDF-1.7",
    "TEST-NET-1",
    "P-256",
)
