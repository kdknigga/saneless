# Phase 25 Deferred Items

## From plan 25-09 (docs)

### A flip answer sent before the flip wait is kept and used

- **Found while:** documenting the flip endpoints in `docs/reference/web-api.md`
- **What:** `ScanWorker._process_job` creates the `WorkerFlipCoordinator` when a manual-duplex
  job starts, before pass A. `continue_flip` / `abort_flip` signal it whenever it exists, and the
  coordinator's first answer is final. A `POST /api/flip/continue` that arrives during pass A is
  therefore claimed as `CONTINUED`, and `wait_for_flip` returns at once after the fronts finish:
  pass B starts without waiting for the stack to be flipped.
- **Reach:** direct API callers only. The web UI renders the Continue and Abort scan buttons only
  in the `AWAITING_FLIP` branch of `partials/status.html`, so a browser user cannot send the
  early answer.
- **Why deferred:** a source change, outside a docs-only plan. The docs now state the behaviour
  honestly ("call them only while the job is `AWAITING_FLIP`").
- **Possible fix:** have the routes (or `ScanWorker.continue_flip` / `abort_flip`) ignore a signal
  unless the job's persisted state is `AWAITING_FLIP`, or arm the coordinator only when the
  pipeline emits `AWAITING_FLIP`.
