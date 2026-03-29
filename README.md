# k8s-diagnosis-agent

`k8s-diagnosis-agent` is a Kubernetes workload diagnostics service. It detects failure symptoms, correlates related objects, writes structured `DiagnosisReport` CRs, and provides a built-in UI/API for operators.

- Latest stable: `v0.5.2`
- Next patch in progress: `v0.5.3` ([release draft](./docs/releases/v0.5.3.md))

## What It Does

- Scheduled scans for abnormal workloads
- Cluster-wide warning-event watch
- Alert webhook ingestion (`POST /alert`)
- Read-only analysis (no auto-remediation)
- Structured diagnosis output with fallback safety

Supported symptoms include:
`CrashLoopBackOff`, `ImagePullBackOff`, `ErrImagePull`, `OOMKilled`, `Pending`, `ProbeFailure`, `CreateContainerConfigError`, `CreateContainerError`, `ContainerCannotRun`, `FailedMount`, `FailedCreatePodSandbox`, `Evicted`, `ProgressDeadlineExceeded`, `ReplicaMismatch`, `NodeNotReadyImpact`.

## Architecture

- `agent/triggers/`: scheduler, event watcher, alert ingress
- `agent/analyzers/`: deterministic detection + fallback logic
- `agent/tools/`: read-only Kubernetes tool layer
- `agent/orchestrator/`: model provider orchestration (OpenAI/Ollama) + tool-calling loop
- `agent/reporting/`: `DiagnosisReport` spec/status writer
- `agent/ui/`: HTTP API + embedded Workbench UI

## Quick Start

### Local

```bash
cd k8s-diagnosis-agent
python3 -m pip install -e ".[dev]"
python3 -m pytest -q
python3 -m agent.app run-once
```

### Deploy

```bash
kubectl apply -k deploy/base
kubectl set image deployment/k8s-diagnosis-agent \
  agent=ghcr.io/bolinz/k8s-diagnosis-agent-app:sha-<full-commit-sha> \
  -n k8s-diagnosis-system
kubectl rollout status deployment/k8s-diagnosis-agent -n k8s-diagnosis-system
```

Use immutable SHA image tags in production.

### Access UI

```bash
kubectl port-forward -n k8s-diagnosis-system svc/k8s-diagnosis-agent 18080:8080
```

Then open `http://127.0.0.1:18080`.

## Runtime Configuration

Key env vars:

- `MODEL_PROVIDER`: `openai` or `ollama` (default `openai`)
- `OPENAI_API_KEY`, `OPENAI_MODEL`, `OPENAI_API_BASE_URL`
- `OLLAMA_BASE_URL`, `OLLAMA_MODEL` (required when provider is `ollama`)
- `LOG_LEVEL`: `DEBUG|INFO|WARNING|ERROR`
- `K8S_DIAGNOSIS_CLUSTER`, `K8S_DIAGNOSIS_NAMESPACE`
- `K8S_DIAGNOSIS_SCAN_INTERVAL_SECONDS`, `K8S_DIAGNOSIS_MIN_OBSERVATION_SECONDS`
- `K8S_DIAGNOSIS_MAX_TOOL_CALLS`, `K8S_DIAGNOSIS_MAX_DIAGNOSIS_SECONDS`

Notes:

- Without `OPENAI_API_KEY`, OpenAI provider cannot run.
- Model diagnosis still works with `MODEL_PROVIDER=ollama` + valid `OLLAMA_MODEL`.

## CI / Release

- PR and push CI run backend tests + frontend unit + frontend Playwright e2e.
- Push to `main` or `v*` tags builds and pushes GHCR image.
- Tag `v*` creates GitHub release notes automatically.

## Limits

- Single-cluster oriented
- Read-only access model
- No automatic remediation
- No external metrics system integration in core path

## Links

- Release notes: [docs/releases](./docs/releases)
- Full changelog: [CHANGELOG.md](./CHANGELOG.md)
- Complex failure validation: [docs/testing/complex-failure-e2e.md](./docs/testing/complex-failure-e2e.md)
- Contributing: [CONTRIBUTING.md](./CONTRIBUTING.md)
- Security: [SECURITY.md](./SECURITY.md)
- License: [MIT](./LICENSE)
