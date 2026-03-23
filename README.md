# k8s-diagnosis-agent

`k8s-diagnosis-agent` is a Kubernetes diagnostics service that detects failure symptoms, stores structured findings as `DiagnosisReport` custom resources, and exposes a minimal UI for operators.

Current release: `v0.3.1`

## Features

- Periodic cluster scans for supported failure symptoms
- Cluster-wide event watch with filtered diagnosis triggers
- Structured `DiagnosisReport` CRs with fallback-safe status content
- Embedded HTTP API and UI for browsing diagnosis results
- GHCR-based container distribution and Kubernetes deployment manifests
- Read-only cluster inspection only; no automatic remediation

## Architecture

- `agent/triggers/`: scheduler, alert ingress, and event watcher
- `agent/analyzers/`: deterministic symptom detection and fallback diagnosis
- `agent/tools/`: read-only Kubernetes tools exposed to the model layer
- `agent/orchestrator/`: GPT-5-Codex / Responses API interaction
- `agent/reporting/`: `DiagnosisReport` spec/status writing and backfill
- `agent/ui/`: embedded API and browser UI
- `deploy/`: base manifests for CRD, RBAC, Deployment, Service, Ingress, and NetworkPolicy

## Supported Diagnosis Paths

- Scheduled anomaly scans
- Warning event-triggered diagnosis
- Alert webhook ingestion via `POST /alert`

Current supported symptoms:

- `CrashLoopBackOff`
- `ImagePullBackOff`
- `ErrImagePull`
- `OOMKilled`
- `Pending`
- `ProbeFailure`

## Quick Start

### Local Development

```bash
cd k8s-diagnosis-agent
python3 -m pip install -e ".[dev]"
python3 -m pytest -q
python3 -m agent.app run-once
```

### Container Image

Published images are available at:

```bash
ghcr.io/bolinz/k8s-diagnosis-agent-app:latest
ghcr.io/bolinz/k8s-diagnosis-agent-app:sha-<full-commit-sha>
```

For cluster deployments, prefer immutable `sha-<full-commit-sha>` tags. If you fork the repository, replace `bolinz` with your own GitHub owner.

### Deploy to Kubernetes

```bash
kubectl apply -k deploy/base
kubectl set image deployment/k8s-diagnosis-agent \
  agent=ghcr.io/bolinz/k8s-diagnosis-agent-app:sha-<full-commit-sha> \
  -n k8s-diagnosis-system
kubectl rollout status deployment/k8s-diagnosis-agent -n k8s-diagnosis-system
```

### Access the UI

The default manifest exposes the UI through an `nginx` ingress:

- host: `k8s-diagnosis.example.com`
- service: `k8s-diagnosis-agent`
- port: `8080`

You can also use `kubectl port-forward` for local inspection:

```bash
kubectl port-forward -n k8s-diagnosis-system svc/k8s-diagnosis-agent 18080:8080
```

## Configuration

Environment variables:

- `OPENAI_API_KEY`: enables Codex-backed diagnosis; without it the service uses fallback logic
- `OPENAI_MODEL`: defaults to `gpt-5-codex`
- `OPENAI_API_BASE_URL`: defaults to `https://api.openai.com/v1`
- `K8S_DIAGNOSIS_CLUSTER`: logical cluster name written into reports
- `K8S_DIAGNOSIS_NAMESPACE`: namespace that stores `DiagnosisReport` resources
- `K8S_DIAGNOSIS_SCAN_INTERVAL_SECONDS`: scheduler interval, default `300`
- `K8S_DIAGNOSIS_MIN_OBSERVATION_SECONDS`: scan threshold, default `600`
- `K8S_DIAGNOSIS_MAX_TOOL_CALLS`: max model tool invocations, default `8`
- `K8S_DIAGNOSIS_MAX_INPUT_BYTES`: max serialized tool output size, default `20000`
- `K8S_DIAGNOSIS_WEBHOOK_PORT`: API/UI port, default `8080`
- `K8S_DIAGNOSIS_EVENT_DEDUPE_WINDOW_SECONDS`: event dedupe window, default `300`

Secrets and image pull notes:

- `k8s-diagnosis-agent-secrets` may contain `OPENAI_API_KEY`; it is optional
- `ghcr-creds` is used to pull private or rate-limited GHCR images

## Limits and Known Boundaries

- Single-cluster oriented deployment model
- Read-only operational scope
- No automatic remediation or write-back to application manifests
- No persistent database; UI reads directly from `DiagnosisReport` objects
- Without `OPENAI_API_KEY`, all diagnoses use deterministic fallback output

## Release and Versioning

- Current release: `v0.3.1`
- Python package version: `0.3.1`
- Helm chart version: `0.3.1`
- GitHub releases are source-first and reference GHCR images plus deployment docs

The current CI workflow:

- runs tests on PRs and pushes
- pushes container images on `main` and tag pushes
- uses shell `docker` commands for image build and push
- does not auto-create GitHub Releases yet

`v0.3.1` fixes CRD schema pruning for cross-object attribution fields:

- preserve nested `relatedObjects`, `rootCauseCandidates`, and `evidenceTimeline` content in `DiagnosisReport.status`
- preserve nested `modelInfo`, `rawSignal`, and `impactSummary` content
- keep the `v0.3.0` attribution and evidence-chain runtime behavior unchanged

## Contributing and License

- Contribution guide: [CONTRIBUTING.md](./CONTRIBUTING.md)
- Security notes: [SECURITY.md](./SECURITY.md)
- License: [MIT](./LICENSE)

## OpenAI Runtime Note

The runtime integrates with the OpenAI Responses API and uses `gpt-5-codex` by default for tool-assisted diagnosis. The cluster service does not require the Codex desktop application or CLI.
