# 0016. Scan sessions run in a child process

## Status

Accepted, 2026-10-04.

## Context

A libsane call blocks inside C. A Python signal handler cannot run while the main thread is inside `sane_read`, and no thread can interrupt it, so a scanner that stops answering mid-page holds the calling process for as long as the backend likes. On the backends whose cancel is asynchronous (avision, hp, umax, hp3500), a cancel issued mid-read need not end the read at all, and snapscan and plustek fall back to their own cancel only after 10 s. A scan run in the server's own process therefore has no bound on a cancel, a server stop or a page that never arrives, and a hung call leaves libsane in a state no later scan in that process can trust. libsane also crashes the calling process on some failures, which for an in-process scan is the whole server.

## Decision

Every SANE call of a scan job runs in one child process started for that job, through `saneless.scanner.scan_child`. The child serves every pass of the job, including the manual-duplex pause, holds no device between passes and restarts SANE inside itself when the job asks for it before a later pass. It is started like every SANE child (ADR 0002): a clean exec of the interpreter in isolated mode, in a session of its own, with the device id on stdin.

saneless owns every deadline. Start-up, open, configure, close, a SANE restart and the child's exit get 30 s each; the start and read of one page share the page budget, 120 to 3600 s worked out from the page; no deadline runs while the job is paused between passes. A page that overruns its budget, a cancel, Ctrl-C, a server stop and a sink that refuses a page all ask the child to cancel, wait up to the 10 s grace, then kill its process group and reap it. Any other overrun kills it at once.

The child validates and crops each page, then sends it as a small header and the image's public `tobytes()` pixels, made and written in strips of whole rows, and waits for saneless to spool it before it reads the next sheet. saneless receives each page into one buffer of exactly its size and wraps it without a further copy.

The invariant: no libsane call is made in the saneless process, and a scan's child is reaped before the scanner gate is released.

## Consequences

- A cancel or a server stop ends the scanner's use within the 10 s grace plus reaping on every backend, the asynchronous-cancel ones and snapscan and plustek included, because the kill does not depend on what the backend does.
- A child that overruns a stage is reported through the ordinary scan error, naming the stage, for example "The scanner stopped answering while opening the scanner; saneless stopped it"; a page past its budget gets the page timeout's own line. The pages already received are kept as after any page timeout. The next job starts a fresh child; there is no state that refuses later scans.
- A crash in libsane while scanning fails the job, not the server.
- Each job pays a child start of 54 to 61 ms before its first page, most of it the interpreter, the standard library and Pillow.
- Throughput on a 50-page feeder scan of the SANE test backend, A4 at 300 dpi, median of three runs: Color takes 10.26 s against 9.305 s in-process (1.103 times as long), Gray 2.60 s against 2.345 s (1.109 times). The extra time per page is the pixels' copy into strips and through the pipe, and, for Color, Pillow's own unpack of RGB into its four-byte pixels when the page is wrapped.
- saneless widens the reply pipe to 1 MiB where the kernel allows, and pixels cross in strips of about 256 KiB, so making a strip overlaps reading the one before.
- The parent holds at most one decoded page; the child holds one page and one strip.
- The libsane hang mitigation runs in the child: the thread unwinder is loaded before python-sane, and a failed read waits for the backend's reader thread before any cancel. With it, none of 1200 children that failed a read on the test backend needed a kill; without it, 5 to 41 did.
- An error raised in the child crosses as its type, text, stage and page. Its `__cause__` does not cross the process boundary; a traceback the child logs reaches saneless's log as text.

**Alternatives rejected:** keeping libsane in the server process with only the hang mitigation, which leaves a mid-read cancel on an asynchronous-cancel backend unbounded; a child per pass, which pays a SANE start-up per pass and has no single process whose reaping ends a job's use of the scanner; sending python-sane's undocumented raw buffer instead of `tobytes()`; a signal-based cancel, which cannot run while the main thread is inside `sane_read`; `fork`, `spawn` and `forkserver` children, for the reasons in ADR 0002.
