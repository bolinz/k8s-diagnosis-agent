# v0.4.5 Weekly TODO

Target: 1 week
Goal: broaden diagnosis coverage with controlled scope and no architecture change.

## Backlog

- [ ] Symptom: refine `FailedScheduling` into resource shortage vs. constraint mismatch
  - [ ] Rule mapping and fallback recommendations
  - [ ] Event mapping for common scheduler messages
  - [ ] Unit tests for both branches

- [ ] Symptom: refine `FailedMount` into PVC unbound vs. missing Secret/ConfigMap
  - [ ] Rule mapping and fallback recommendations
  - [ ] Event/message parser updates
  - [ ] Unit tests for both branches

- [ ] Symptom: refine image-pull class into auth vs. not-found vs. network error
  - [ ] Reason/message signatures
  - [ ] Recommendation templates by subtype
  - [ ] Unit tests for subtype mapping

- [ ] Tool: add `get_namespace_events(namespace)`
  - [ ] Read-only bounded response (count + time window)
  - [ ] Tool registry wiring
  - [ ] Unit tests for success/not-found/forbidden

- [ ] Tool: add `get_node_events(node_name)`
  - [ ] Read-only bounded response (count + time window)
  - [ ] Tool registry wiring
  - [ ] Unit tests for success/not-found/forbidden

- [ ] Report/API/UI: add `category` and `primarySignal`
  - [ ] Status/schema write path
  - [ ] API list/detail response
  - [ ] UI filter by category
  - [ ] UI detail block for primary signal

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
