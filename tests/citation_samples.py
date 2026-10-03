"""
The planning-citation pattern and the sample identifiers its tests need.

The citation guard and the matching commit hook reject any line under src/,
scripts/, tests/, the workflows, .dockerignore or the decision records under
docs/explanation/decisions/ that points at the planning records. The guard's own pattern, and the identifiers that prove it bites,
cannot be written down anywhere the hook reads without the hook failing on
them, so they live here: this is the one file the hook skips, and a test pins
that it stays the only one.
"""

from __future__ import annotations

import re

# The identifier shapes the planning records use: decision, finding and
# requirement IDs, threat IDs, phase and plan numbers, numbered research
# pitfalls and the planning file names. The architecture, enumeration and
# multi-page requirement IDs are covered too, because their prefixes appear in
# no other identifier shape above. It also rejects a source file named with a
# line number after a colon, the name of the assistant instructions file, and
# a phase referred to without its number: line numbers drift, and the other
# two cannot be looked up by a reader of the shipped tree either. The
# no-planning-citations hook in .pre-commit-config.yaml carries the same
# pattern, and a test in tests/test_deployment_config.py keeps the two
# identical.
PLANNING_CITATION = re.compile(
    r"\b(R[0-9]+-)?(C|D|M|N|S|U|W|CR|IN|WR)-[0-9]{2,}\b|\b(A|"
    r"API|APPL|ARCH|CFG|CTR|DARK|DLVR|DOCS|DPLX|ENUM|EXC|HARD|MPG|OUTC|ROBU|"
    r"SCAN|SCNR|STOR|SWP|TEST)-[0-9]+\b|\bT-[0-9]+-[0-9]+\b|"
    r"\b[Pp]hase[ -][0-9]+|\b[Pp]lan [0-9]+(\.[0-9]+)?-[0-9]+\b|"
    r"\bPitfall #?[0-9]+|UI-SPEC|\b(CONTEXT|RESEARCH)\b|\b(PLAN|"
    r"SUMMARY|VERIFICATION|REVIEW)\.md\b|Open Question|"
    r"[Pp]er user decision|\.planning/|\.py:[0-9]+|CLAUDE\.md|"
    r"\b([Tt]his|[Tt]hat|[Tt]hree|[Ee]arlier) phases?\b"
)

# Text the pattern must match: one of each identifier family a reader is
# likely to paste, including the multi-page IDs with and without a leading
# zero.
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
)

# Ordinary text the pattern must leave alone: encodings, standards and hash
# names, a paper size, an off-by-one, and SQLite's EXPLAIN QUERY PLAN.
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
)
