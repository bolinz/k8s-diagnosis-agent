# Changelog

## v0.7.0 (Planned)

Release type: minor release focused on diagnosis breadth and operator maturity.

### Added

- New symptom types: `PVCPending`, `VolumeResizeFailure`, `NetworkPolicyBlocking`, `HPAMaxReplicas`, `ResourceQuotaExceeded`, `PDBDisruptionBlocked`.
- Batch diagnosis aggregation: when `K8S_DIAGNOSIS_BATCH_THRESHOLD` (default 5) or more workloads share the same symptom in the same namespace, a single aggregated report is written with `impactSummary.type=batch`.
- OLM (Operator Lifecycle Manager) bundle: `deploy/olm/bundle/` with `ClusterServiceVersion`, `Package`, CRD manifests, and `bundle.Dockerfile`. Supports OwnNamespace and MultiNamespace install modes.
- Prometheus metrics renamed with `k8s_diagnosis_` prefix; new metrics: `k8s_diagnosis_quality_score`, `k8s_diagnosis_uncertainty_total`, `k8s_diagnosis_batch_size`, `k8s_diagnosis_batch_report_total`.
- Grafana dashboard at `deploy/grafana/dashboard.json` with 8 panels: diagnosis rate, quality score, fallback ratio, uncertainty count, batch size, batch reports.
- Kubernetes health probes: `GET /healthz/ready` (K8s API + CRD check), `GET /healthz/live` (process alive), `GET /healthz/startup` (model readiness).

### Changed

- All metric names standardized to `k8s_diagnosis_` prefix (breaking change for existing Prometheus scrapers).
- `agent/k8s_client/runtime.py` now uses `PolicyV1Api` for PDB collection; added PVC Pending, HPA MaxReplicas, and PDB DisruptionBlocked snapshot collectors.
- `K8S_DIAGNOSIS_BATCH_THRESHOLD` env var added (default 5, set to 0 to disable).

### Security

## v0.5.3 (Planned)

Release type: patch release focused on report integrity fixes and CI/CD reliability improvements.

### Fixed

- Tightened report completeness checks so blank/placeholder `summary`, `evidence`, and `recommendations` are treated as incomplete.
- `list_reports` and `get_report` now normalize sparse legacy records with fallback-safe `summary/evidence/recommendations`.
- Backfill now rewrites reports that contain only whitespace/placeholder values in key status fields.

### Changed

- CI workflow now uses concurrency cancellation to stop superseded runs on the same ref.
- Frontend CI split into `frontend-unit` and `frontend-e2e` jobs for clearer failure isolation.
- Python test job now includes `compileall` gate.
- Container build switched to Buildx with GHA cache for faster repeated builds.

## v0.5.2

Release type: patch release for UI experience density, analysis transparency, and frontend stability.

### Added

- New `AI Analysis Session` detail card showing provider/model/fallback metadata and stage progress (`Signal Intake`, `Correlation`, `Conclusion`).
- New `Why This Recommendation` section mapping recommendations to evidence snippets when available.
- New release walkthrough doc with screenshots: `docs/releases/v0.5.2.md`.

### Changed

- Compact top bar and compact summary strip to reclaim vertical space.
- Detail-first layout ratio updated to `30/70` (and `24/76` on large screens).
- Card spacing and control density tuned to reduce excessive scrolling.

### Fixed

- Detail panel no longer blanks when legacy report detail fetch fails.
- Fixed timeline hook-order crash when switching between reports with and without timeline events.

## v0.5.1 (Unreleased)

Release type: patch release for frontend packaging and deployment consistency.

### Fixed

- Runtime image now always includes built `web` frontend assets, so cluster deployments render the Workbench UI instead of the fallback page.
- Added Docker multi-stage frontend build during image creation and packaged frontend static files into `agent.ui`.
- Added `.dockerignore` rules to prevent stale local `frontend_dist` from contaminating release images.

## v0.5.0

Release type: UI/operability enhancement release for the embedded workbench.

### Added

- Timeline density strip and focused event navigation controls.
- Timeline keyboard hotkeys: `,` (previous) and `.` (next) focused event.
- Event navigator grouping improvements:
  - grouped by signal
  - group sort modes (`By Count` / `By Time`)
  - per-group collapse/expand and active-group highlight
- Shortcut help panel (`Shortcuts` button, `?` hotkey).
- Additional Playwright e2e coverage for timeline grouping/navigation/persistence paths.

### Changed

- Timeline group sort preference is persisted in UI local storage preferences.

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
