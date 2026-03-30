# k8s-diagnosis-agent

`k8s-diagnosis-agent` is a read-only Kubernetes workload diagnostics service.
It detects symptoms, correlates related objects, writes `DiagnosisReport` CRs, and exposes a built-in UI/API for operators.

- Latest stable: `v0.5.2`
- Next patch: `v0.5.3` ([release draft](./docs/releases/v0.5.3.md))

## Core Capabilities

- Scheduled scan + cluster-wide warning-event watch + alert webhook (`POST /alert`)
- Cross-object correlation (`relatedObjects`, `rootCauseCandidates`, `evidenceTimeline`, `impactSummary`)
- Explainability outputs (`qualityScore`, `uncertainties`, `evidenceAttribution`) surfaced in report detail UI
- Model provider runtime (`MODEL_PROVIDER=openai|ollama`) with fallback-safe output
- Embedded Workbench UI and JSON API (`/api/reports`, `/api/reports/{name}`)
- CI with backend tests + frontend unit + Playwright e2e + GHCR image publish

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

### UI

```bash
kubectl port-forward -n k8s-diagnosis-system svc/k8s-diagnosis-agent 18080:8080
```

Open `http://127.0.0.1:18080`.

## Key Configuration

- `MODEL_PROVIDER=openai|ollama`
- OpenAI: `OPENAI_API_KEY`, `OPENAI_MODEL`, `OPENAI_API_BASE_URL`
- Ollama: `OLLAMA_BASE_URL`, `OLLAMA_MODEL`
- Runtime: `LOG_LEVEL`, `K8S_DIAGNOSIS_SCAN_INTERVAL_SECONDS`, `K8S_DIAGNOSIS_MAX_DIAGNOSIS_SECONDS`

Notes:

- Without `OPENAI_API_KEY`, OpenAI provider cannot run.
- Model diagnosis still works with Ollama when `MODEL_PROVIDER=ollama` and `OLLAMA_MODEL` is set.

## Limits

- Single-cluster oriented
- Read-only operations (no automatic remediation)
- No external metrics platform integration in core analysis path

## Documentation

- Docs index: [docs/README.md](./docs/README.md)
- Release notes: [docs/releases](./docs/releases)
- Changelog: [CHANGELOG.md](./CHANGELOG.md)
- Complex-failure validation: [docs/testing/complex-failure-e2e.md](./docs/testing/complex-failure-e2e.md)
- Contributing: [CONTRIBUTING.md](./CONTRIBUTING.md)
- Security: [SECURITY.md](./SECURITY.md)
- License: [MIT](./LICENSE)
