# Changelog

## v0.7.0

Release type: minor capability release.

Release focus: diagnosis breadth (6 new symptom types, batch aggregation) and operator maturity (OLM, Prometheus, health probes).

### Added

- New symptom types: `PVCPending`, `VolumeResizeFailure`, `NetworkPolicyBlocking`, `HPAMaxReplicas`, `ResourceQuotaExceeded`, `PDBDisruptionBlocked`.
- Batch diagnosis aggregation: when `K8S_DIAGNOSIS_BATCH_THRESHOLD` (default 5) or more workloads share the same symptom in the same namespace, a single aggregated report is written with `impactSummary.type=batch`.
- OLM (Operator Lifecycle Manager) bundle: `deploy/olm/bundle/` with `ClusterServiceVersion`, `Package`, CRD manifests, and `bundle.Dockerfile`. Supports OwnNamespace and MultiNamespace install modes.
- Prometheus metrics renamed with `k8s_diagnosis_` prefix; new metrics: `k8s_diagnosis_quality_score`, `k8s_diagnosis_uncertainty_total`, `k8s_diagnosis_batch_size`, `k8s_diagnosis_batch_report_total`.
- Grafana dashboard at `deploy/grafana/dashboard.json` with 7 panels: diagnosis rate, quality score, fallback ratio, uncertainty count, batch size, batch reports.
- Kubernetes health probes: `GET /healthz/ready` (K8s API + CRD check), `GET /healthz/live` (process alive), `GET /healthz/startup` (model readiness).

### Changed

- All metric names standardized to `k8s_diagnosis_` prefix (breaking change for existing Prometheus scrapers).
- `agent/k8s_client/runtime.py` now uses `PolicyV1Api` for PDB collection; added PVC Pending, HPA MaxReplicas, and PDB DisruptionBlocked snapshot collectors.
- `K8S_DIAGNOSIS_BATCH_THRESHOLD` env var added (default 5, set to 0 to disable).
- `DiagnosisReport.status.analysisVersion` bumped to `0.7.0`.

### Fixed

- **evidenceTimeline reconstruction**: previously taken directly from model JSON output, which was unreliable (model often omitted events or hallucinated timestamps). Now extracts events from actual `get_pod_events`/`get_workload_events` tool outputs in `tool_history`, deduplicates by `(signal, kind, name, timestamp)`, sorts ascending, caps at 20 entries.
- **tool_history leak**: `tool_history` is now cleared at `diagnose()` start, preventing stale events from prior calls from leaking into subsequent diagnoses.
- **PVCPending phase lookup**: fixed `getattr`-on-string bug when constructing `message` from PVC phase.
- **ResourceQuotaExceeded event filter**: removed invalid `field_selector` (`reason=FiredCreate` is not a standard K8s Event field); now uses in-code message filtering.

## v0.6.0

Release type: minor capability release.

Release focus: diagnosis trustworthiness and explainability.

### Added

- Deterministic `qualityScore` object in `DiagnosisReport.status` with `overall` score, per-dimension scores (`evidence_coverage`, `recommendation_actionability`, `root_cause_strength`, `correlation_strength`, `confidence_alignment`), `method`, and `usedFallback` flag. Configurable via `agent/config/quality_scoring.yaml`.
- `uncertainties: string[]` field for explicit operator caveats, generated for fallback and weak-evidence cases.
- `evidenceAttribution: object[]` auto-populated from tool call sequence and timeline signals, mapping evidence items to source (tool, timestamp, signal, objectRef).
- Rule-based fallback diagnosis engine externalized to `agent/config/fallback_rules.yaml` — 20 symptom types with severity, probable_causes, recommendations, and confidence.
- Event-storm dedup state machine: tracks `count`, `first_seen`, `last_seen`, `aggregated` per event key; `mark_aggregated()` and `next_state()` methods.
- Async event queue: `AsyncEventQueue` class with bounded worker pool and backpressure for decoupled event processing.

### Architecture Refactoring

- `AgentService` split into focused components: `EventWatcher`, `ReportWriter`, `TriggerTransformer`, `ToolExecutor`, `EventStormDeduper`, `AsyncEventQueue`.
- `DiagnosisAgent.diagnose()` refactored to pure functional — returns `tuple[DiagnosisResult, list[ToolCallRecord]]` with local `tool_history` instead of mutating instance state.
- `RuntimeKubernetesClient` split: tool implementations extracted to `agent/k8s_client/executor.py`; `RuntimeKubernetesClient` becomes facade delegating to `ToolExecutor`.
- `KubernetesDiagnosisReportWriter.upsert_report()` race condition fixed: replaced `get`→`patch`/`create` TOCTOU pattern with atomic `create`→catch-409→`patch`.

### Fixed

- Fallback reports now always include non-empty `evidenceAttribution`.
- Strict explainability checker now requires `analysisVersion` in `0.6.x` range.
- Report normalization now sanitizes `rootCauseCandidates` against `relatedObjects` and primary workload.

## v0.5.3

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
