# Changelog

## v0.4.8

Release type: reliability patch for HTTP alert handling and runtime logging noise reduction.

### Added

- Async alert processing model for HTTP webhook:
  - `POST /alert` now enqueues background diagnosis and returns `202` with `requestId`.
  - `GET /api/alerts/{requestId}` returns task status (`queued|running|succeeded|failed`) and result metadata.
- New unit tests for async alert task manager success/failure behavior.

### Fixed

- Suppressed noisy request traceback on client disconnect:
  - gracefully handles `BrokenPipeError` and `ConnectionResetError` during response writes
  - records structured warning log (`http_client_disconnected`) instead of stack trace.

### Changed

- Default report `analysisVersion` bumped to `0.4.8`.
- Project package and Helm chart versions bumped to `0.4.8`.

## v0.4.7

Release type: minor capability update (release prep only in this branch; no tag created yet).

### Added

- Deterministic attribution scoring for `rootCauseCandidates` with stable ordering, `score`, and `rankReasons`.
- Event-storm suppression with time-window aggregation and configurable threshold:
  - `K8S_DIAGNOSIS_EVENT_STORM_THRESHOLD`
  - Emits one aggregated fallback report for bursts, suppresses subsequent duplicates in-window.
- Diagnosis audit trace metadata:
  - `modelInfo.traceId`
  - `status.diagnosisTrace` with tool sequence, budget usage, scope-guard hits, fallback reason.
- UI attribution readability improvements:
  - list chip for root-candidate count
  - `Top Root Candidate` block
  - `Evidence Timeline` first abnormal signal emphasis.

### Changed

- Default report `analysisVersion` bumped to `0.4.7`.
- Project package and Helm chart versions bumped to `0.4.7`.

### Compatibility

- Existing API/UI fields remain backward compatible.
- New fields are additive and optional.
