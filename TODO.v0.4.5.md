# v0.4.5 Weekly TODO

Target: 1 week
Goal: broaden diagnosis coverage with controlled scope and no architecture change.

## Backlog

- [x] Symptom: refine `FailedScheduling` into resource shortage vs. constraint mismatch
  - [x] Rule mapping and fallback recommendations
  - [x] Event mapping for common scheduler messages
  - [x] Unit tests for both branches

- [x] Symptom: refine `FailedMount` into PVC unbound vs. missing Secret/ConfigMap
  - [x] Rule mapping and fallback recommendations
  - [x] Event/message parser updates
  - [x] Unit tests for both branches

- [x] Symptom: refine image-pull class into auth vs. not-found vs. network error
  - [x] Reason/message signatures
  - [x] Recommendation templates by subtype
  - [x] Unit tests for subtype mapping

- [x] Tool: add `get_namespace_events(namespace)`
  - [x] Read-only bounded response (count + time window)
  - [x] Tool registry wiring
  - [x] Unit tests for success/not-found/forbidden

- [x] Tool: add `get_node_events(node_name)`
  - [x] Read-only bounded response (count + time window)
  - [x] Tool registry wiring
  - [x] Unit tests for success/not-found/forbidden

- [x] Report/API/UI: add `category` and `primarySignal`
  - [x] Status/schema write path
  - [x] API list/detail response
  - [x] UI filter by category
  - [x] UI detail block for primary signal

- [ ] Observability: add minimal metrics
  - [ ] `diagnosis_requests_total`
  - [ ] `diagnosis_fallback_total`
  - [ ] `diagnosis_duration_seconds`
  - [ ] `tool_calls_total`
  - [ ] `report_upsert_total`

- [ ] Release workflow: `v0.4.5`
  - [ ] Regression tests green
  - [ ] Tag and release notes
  - [ ] Deploy to cluster
  - [ ] Controlled alert verification

## Guardrails

- [ ] Keep Kubernetes interaction read-only
- [ ] Keep fallback path intact
- [ ] Keep new behavior behind explicit defaults when risky
- [ ] Avoid raw large-object logs in DEBUG mode

## Done Criteria

- [ ] All acceptance tests pass
- [ ] At least one controlled cluster run for each new symptom subtype
- [ ] No regression in existing `v0.4.x` Ollama path
