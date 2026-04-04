# v0.7.0 Plan

## Goal

Expand diagnostic coverage with new symptom types and advance operator maturity through OLM integration, rich observability, and improved health management.

## Theme

**Diagnosis breadth + Operator readiness**

---

## Part A: Diagnosis Capability Expansion

### A.1 New Symptom Types

Add support for the following symptom types in `SUPPORTED_SYMPTOMS` and `fallback_diagnosis()`:

| Symptom | Detection Signal | Fallback Classification |
|---------|-----------------|------------------------|
| `PVCPending` | PVC stuck in Pending with no binding | storage provisioning failure |
| `VolumeResizeFailure` | PVC condition `FileSystemResizePending` | volume expansion blocked |
| `NetworkPolicyBlocking` | pods with NetworkPolicy but no connectivity | policy misconfiguration |
| `HPAMaxReplicas` | HPA at maxReplicas for sustained period | resource ceiling reached |
| `ResourceQuotaExceeded` | Namespace Quota exceeded events | quota limit prevents scheduling |
| `PDBDisruptionBlocked` | Pods blocked by PDB MinAvailable violation | pdb prevents eviction |

**Implementation Notes:**
- `agent/analyzers/rules.py`: add 6 new entries to `SUPPORTED_SYMPTOMS` + `fallback_diagnosis()`
- `agent/k8s_client/runtime.py`: add new snapshot collectors for PVC, HPA, PDB, Quota
- `agent/triggers/event_watcher.py`: map new event reasons to symptom names
- Add unit tests for each fallback branch
- Add Playwright e2e coverage for new symptom types

### A.2 Batch Diagnosis Aggregation

- When scan finds N > 5 pods with the **same symptom and namespace**, write one aggregated `DiagnosisReport`
- Aggregated report includes `impactSummary` with affected workload count list
- Config: `K8S_DIAGNOSIS_BATCH_THRESHOLD=5` (0 = disabled)
- UI: batch reports show "N workloads affected" chip

---

## Part C: Operator Maturity

### C.1 OLM (Operator Lifecycle Manager) Integration

- Add `bundle/` directory with:
  - `manifests/k8s-diagnosis-agent.clusterserviceversion.yaml`
  - `manifests/k8s-diagnosis-agent.package.yaml`
  - `bundle.Dockerfile`
- CSV features:
  - Owned CRD: `diagnosisreports.ops.ai.yourorg`
  - Required CRDs: core Kubernetes APIs
  - Install modes: `OwnNamespace` (default), `MultiNamespace`
  - ServiceAccount + RBAC baked into CSV
  - Env var config via `Spec.Configurations`
  - Health checks via `readinessGates`

### C.2 Prometheus Metrics + Grafana Dashboard

**Metrics improvements:**
- Add `diagnosis_quality_score` histogram (overall + per-dimension)
- Add `diagnosis_uncertainty_total` counter (labeled by uncertainty type)
- Add `report_batch_size` histogram for batch aggregation
- Standardize all metric names under `k8s_diagnosis_` prefix

**Grafana dashboard:**
- `deploy/grafana/dashboard.json`: JSON dashboard definition
  - Panel: Diagnosis rate (requests/sec)
  - Panel: Quality score distribution
  - Panel: Fallback ratio trend
  - Panel: Uncertainty breakdown
  - Panel: Report batch size
  - Panel: Batch reports
  - Panel: Top symptoms by frequency

### C.3 Health Probes

**Kubernetes probes:**
- Readiness probe: `GET /healthz/ready` → 200 when agent service is initialized and K8s client is connected
- Liveness probe: `GET /healthz/live` → 200 always (or 503 if unrecoverable state)
- Startup probe: `GET /healthz/startup` → 200 when all startup tasks complete (model probe, CRD check)

**Endpoints:**
- `GET /healthz/ready` — checks K8s API connectivity + CRD existence
- `GET /healthz/live` — checks process is alive
- `GET /healthz/startup` — checks model connectivity (Ollama/OpenAI ping)

**Implementation:**
- `agent/ui/http_server.py`: add `/healthz/ready`, `/healthz/live`, `/healthz/startup` routes
- `deploy/base/deployment.yaml`: add `readinessProbe`, `livenessProbe`, `startupProbe` blocks
- Unit tests for each probe path

---

## Parallel PR Work Plan

| PR | Scope | Files |
|----|-------|-------|
| PR-A | Symptom types + rules | `agent/analyzers/rules.py`, `agent/k8s_client/runtime.py`, tests |
| PR-B | Batch aggregation | `agent/service.py`, `agent/analyzers/rules.py` |
| PR-C | Batch aggregation | `agent/service.py`, `agent/analyzers/rules.py` |
| PR-D | OLM bundle | `bundle/` dir, `deploy/olm/` |
| PR-E | Metrics + Grafana | `agent/metrics.py`, `deploy/grafana/dashboard.json` |
| PR-F | Health probes | `agent/ui/http_server.py`, `deploy/base/deployment.yaml` |
| PR-G | Version bump + release prep | `pyproject.toml`, `Chart.yaml`, CHANGELOG |

---

## Acceptance Criteria

- All 6 new symptom types produce valid fallback reports with proper evidence/recommendations
- Ollama backend continues to work without vision model requirements
- Batch aggregation triggers correctly at threshold; aggregated reports are distinct
- OLM bundle passes `operator-sdk bundle validate`
- Prometheus metrics endpoint returns all defined metrics
- Grafana dashboard renders with mock data
- All 3 health probe endpoints return correct status codes
- Full test suite passes
- `run_complex_failure.sh` passes on cluster

## Release Gates

1. All PRs merged to `main`
2. `python3 -m pytest -q` + `cd web && npm run test` + Playwright e2e green
3. OLM bundle validate passes
4. Cluster e2e run with new symptom types
5. Helm chart and Kustomize base both deploy successfully

## Out of Scope

- Auto-remediation / mutations
- Multi-cluster federation
- External metrics platform push (Prometheus pull model only)
