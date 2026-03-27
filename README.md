# k8s-diagnosis-agent

`k8s-diagnosis-agent` is a Kubernetes diagnostics service that detects failure symptoms, stores structured findings as `DiagnosisReport` custom resources, and exposes a minimal UI for operators.

Current release: `v0.4.6`

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
- `MODEL_PROVIDER`: `openai` or `ollama`, default `openai`
- `OPENAI_MODEL`: defaults to `gpt-5-codex`
- `OPENAI_API_BASE_URL`: defaults to `https://api.openai.com/v1`
- `OLLAMA_BASE_URL`: defaults to `http://127.0.0.1:11434`
- `OLLAMA_MODEL`: required when `MODEL_PROVIDER=ollama`
- `LOG_LEVEL`: `DEBUG|INFO|WARNING|ERROR`, default `INFO`
- `K8S_DIAGNOSIS_REQUEST_TIMEOUT_SECONDS`: model request timeout in seconds, default `45`
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

Example Ollama runtime configuration:

```bash
kubectl set env deployment/k8s-diagnosis-agent -n k8s-diagnosis-system \
  MODEL_PROVIDER=ollama \
  OLLAMA_BASE_URL=http://192.168.100.182:11434 \
  OLLAMA_MODEL=qwen3:8b \
  LOG_LEVEL=DEBUG \
  K8S_DIAGNOSIS_REQUEST_TIMEOUT_SECONDS=120
```

## Limits and Known Boundaries

- Single-cluster oriented deployment model
- Read-only operational scope
- No automatic remediation or write-back to application manifests
- No persistent database; UI reads directly from `DiagnosisReport` objects
- Without `OPENAI_API_KEY`, all diagnoses use deterministic fallback output

## Release and Versioning

- Current release: `v0.4.6`
- Python package version: `0.4.6`
- Helm chart version: `0.4.6`
- GitHub releases are source-first and reference GHCR images plus deployment docs

The current CI/release workflows:

- runs tests on PRs and pushes
- pushes container images on `main` and tag pushes
- uses shell `docker` commands for image build and push
- auto-creates GitHub Releases on `v*` tag pushes

`v0.4.6` is a patch release focused on model output normalization correctness:

- normalize string-valued `probableCauses`, `evidence`, and `recommendations` into single-item lists
- prevent per-character list corruption when a provider returns plain strings
- normalize severity aliases (for example `High`) to supported values (`critical|warning|info`)

`v0.4.5` expanded diagnosis breadth for workload operations and improved operator-facing signals:

- scheduler and mount fallback refinement (`FailedScheduling` and `FailedMount` subtypes)
- image-pull subtype refinement (auth vs not-found vs network)
- new read-only tools: `get_namespace_events` and `get_node_events`
- report/API/UI enrichment with `category` and `primarySignal`
- minimal runtime metrics exposed via `/metrics`

## Contributing and License

- Contribution guide: [CONTRIBUTING.md](./CONTRIBUTING.md)
- Security notes: [SECURITY.md](./SECURITY.md)
- License: [MIT](./LICENSE)

## OpenAI Runtime Note

The runtime integrates with the OpenAI Responses API and uses `gpt-5-codex` by default for tool-assisted diagnosis. The cluster service does not require the Codex desktop application or CLI.
