"""
Measure how long a 50-page feeder scan takes on the SANE ``test`` backend.

One run is a number of feeder passes (default 5) of 10 A4 pages at 300 dpi on
``test:0``, each pass spooled through ``SpooledPageSink`` as a job spools its
pages, so the time covers acquisition, PNG encoding and the writes to disk.
The test backend's feeder empties after 10 sheets, which is why 50 pages take
five passes.  The wall time of a run runs from just before its first pass to
just after its last.

The backend is configured only through a temporary ``SANE_CONFIG_DIR``
(``dll.conf`` and ``test.conf``), never through options set in this process,
so the same settings reach a scan made in this process and one made in a
child process alike.  When the backend offers a ``scan_session``, the passes
of a run share one session; otherwise each pass is a plain ``scan_pages``
call.

Options:
    --modes: The scan modes to measure, default ``Color Gray``.
    --runs: How many runs per mode, default 3; the median is reported.
    --passes: How many 10-page feeder passes per run, default 5.

Output is one line per mode on standard output: mode, dpi, page size, pages
per run, the median run time and every run's time, in seconds.  A run that
fails, or that returns fewer pages than it should, is an error: the reason
goes to standard error and the script exits 1.

Needs libsane with the ``test`` backend installed.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Final

from saneless.pipeline import _SPOOL_LABEL_A
from saneless.scanner.base import ScanSettings
from saneless.scanner.sane_backend import SaneBackend
from saneless.spool import SpooledPageSink
from saneless.thread_unwinder import load_thread_unwinder

_DEVICE: Final = "test:0"
_FEEDER: Final = "Automatic Document Feeder"
_RESOLUTION: Final = 300
_PAGE_SIZE: Final = "A4"
_SHEETS_PER_PASS: Final = 10
_PAGE_PIXELS: Final = (2480, 3507)

_DLL_CONF: Final = "test\n"

# An A4 scan window (210 x 297 mm); at 300 dpi the backend reports a
# 2480 x 3507 pixel page, which every run checks it was given.
_TEST_CONF: Final = """\
number_of_devices 2
test-picture "Color pattern"
geometry_min 0.0
geometry_max 300.0
geometry_quant 1.0
tl_x 0.0
tl_y 0.0
br_x 210.0
br_y 297.0
"""


class BenchmarkError(Exception):
    """A run failed or produced the wrong number of pages."""


def _write_config(config_dir: Path) -> None:
    """
    Write the SANE configuration this benchmark scans with.

    Args:
        config_dir: The directory ``SANE_CONFIG_DIR`` will name.

    """
    (config_dir / "dll.conf").write_text(_DLL_CONF, encoding="utf-8")
    (config_dir / "test.conf").write_text(_TEST_CONF, encoding="utf-8")


def _one_run(backend: SaneBackend, mode: str, passes: int, spool_root: Path) -> float:
    """
    Scan ``passes`` feeder passes in one mode and time them.

    Args:
        backend: The backend to scan through.
        mode: The scan mode, e.g. ``Color``.
        passes: How many 10-page feeder passes to make.
        spool_root: An empty directory for this run's spooled pages.

    Returns:
        The wall time of the run, in seconds.

    Raises:
        BenchmarkError: A pass returned other than 10 pages or a page not
            2480 x 3507 pixels, or the run other than ``passes`` times 10.

    """
    settings = ScanSettings(source=_FEEDER, resolution=_RESOLUTION, mode=mode)
    session_factory = getattr(backend, "scan_session", None)
    total = 0
    start = time.perf_counter()
    session = (
        session_factory() if callable(session_factory) else contextlib.nullcontext()
    )
    with session:
        for index in range(passes):
            directory = spool_root / f"pass-{index}"
            directory.mkdir()
            sink = SpooledPageSink(directory, _SPOOL_LABEL_A, 0)
            batch = backend.scan_pages(_DEVICE, settings, sink)
            if len(batch.pages) != _SHEETS_PER_PASS:
                msg = (
                    f"{mode} pass {index + 1} returned {len(batch.pages)} pages, "
                    f"expected {_SHEETS_PER_PASS}"
                )
                raise BenchmarkError(msg)
            size = batch.pages[0].size
            if size != _PAGE_PIXELS:
                msg = (
                    f"{mode} pass {index + 1} scanned {size[0]} x {size[1]} pixel "
                    f"pages, expected {_PAGE_PIXELS[0]} x {_PAGE_PIXELS[1]}: is "
                    "test.conf applied?"
                )
                raise BenchmarkError(msg)
            total += len(batch.pages)
    elapsed = time.perf_counter() - start
    if total != passes * _SHEETS_PER_PASS:
        msg = f"{mode} run returned {total} pages, expected {passes * _SHEETS_PER_PASS}"
        raise BenchmarkError(msg)
    return elapsed


def _measure(args: argparse.Namespace, work: Path) -> list[str]:
    """
    Make every run and format one result line per mode.

    Args:
        args: The parsed command line.
        work: A scratch directory for spooled pages.

    Returns:
        The result lines, each ending in a newline.

    """
    load_thread_unwinder()
    backend = SaneBackend()
    pages = args.passes * _SHEETS_PER_PASS
    lines: list[str] = []
    for mode in args.modes:
        times: list[float] = []
        for run in range(args.runs):
            spool_root = work / f"{mode}-{run}"
            spool_root.mkdir()
            times.append(_one_run(backend, mode, args.passes, spool_root))
        every = " ".join(f"{value:.2f}" for value in times)
        lines.append(
            f"mode={mode} dpi={_RESOLUTION} page={_PAGE_SIZE} pages={pages} "
            f"median={statistics.median(times):.2f}s runs=[{every}]\n"
        )
    return lines


def main(argv: list[str] | None = None) -> int:
    """
    Run the benchmark and print one line per mode.

    Args:
        argv: Command-line arguments; ``None`` means ``sys.argv[1:]``.

    Returns:
        0 when every run scanned all its pages, 1 otherwise.

    """
    parser = argparse.ArgumentParser(
        description="Time 10-page feeder passes on the SANE test backend."
    )
    parser.add_argument(
        "--modes",
        nargs="+",
        default=["Color", "Gray"],
        help="scan modes to measure",
    )
    parser.add_argument(
        "--runs", type=int, default=3, help="runs per mode; the median is reported"
    )
    parser.add_argument(
        "--passes", type=int, default=5, help="10-page feeder passes per run"
    )
    args = parser.parse_args(argv)
    if args.runs < 1 or args.passes < 1:
        sys.stderr.write("feeder throughput: --runs and --passes must be at least 1\n")
        return 1

    with tempfile.TemporaryDirectory(prefix="feeder-throughput-") as scratch:
        root = Path(scratch)
        config_dir = root / "sane.d"
        config_dir.mkdir()
        _write_config(config_dir)
        # Set before any scan: SANE reads its configuration when it starts,
        # which is in each scanning child as it starts, and the child takes
        # this process's environment.
        os.environ["SANE_CONFIG_DIR"] = str(config_dir)
        work = root / "spool"
        work.mkdir()
        try:
            lines = _measure(args, work)
        except Exception as exc:  # report any failure as a failed run
            sys.stderr.write(f"feeder throughput: run failed: {exc}\n")
            return 1
    for line in lines:
        sys.stdout.write(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
