# Changelog

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
