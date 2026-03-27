# v0.4.7 Plan

## Goal

Build a controlled autonomous probing release that improves cross-object attribution quality while keeping runtime behavior auditable and bounded.

## Included Baseline Changes

- Controlled autonomous probing guardrails:
  - `max_tool_calls` + `max_diagnosis_seconds` budget
  - namespace scope guard in tool execution
- Agent naming refactor:
  - `DiagnosisAgent` as primary name
  - `CodexDiagnosisAgent` compatibility alias retained
- Output stability fixes:
  - normalize string-valued `probableCauses`, `evidence`, `recommendations`
  - normalize severity aliases (`High -> critical`, etc.)
- Repo hygiene:
  - ignore macOS `.DS_Store`

## New Scope for v0.4.7

1. Attribution scoring v1
- Add deterministic scoring for `rootCauseCandidates`.
- Sort by score and include short reason labels for ranking.

2. Event-storm suppression v1
- Add object-symptom time-bucket aggregation.
- When threshold is exceeded, write a single aggregated report with `impactSummary`.

3. Probing audit trail v1
- Record per-diagnosis trace:
  - tool sequence
  - budget usage
  - scope-guard hits
  - fallback reason
- Expose trace IDs in logs and report metadata.

4. UI attribution readability
- Add fixed "Top Root Candidate" block.
- Render `evidenceTimeline` with first-abnormal-signal emphasis.
- Add list-level summary chips:
  - related objects count
  - root cause candidates count

5. Config and safety closure
- Add `K8S_DIAGNOSIS_SCOPE_MODE` with `strict|relaxed` (default `strict`).
- `strict`: current namespace-only behavior.
- `relaxed`: allow only explicit namespace allowlist.

## Parallel PR Work Plan

PR-A: Guardrails + config
- `K8S_DIAGNOSIS_SCOPE_MODE`
- allowlist parsing
- tests for strict/relaxed behavior

PR-B: Attribution scoring
- scoring module
- candidate ranking integration
- unit tests for deterministic ordering

PR-C: Event storm suppression
- aggregation keys and thresholds
- summary-only write path
- integration tests for burst scenarios

PR-D: Audit trail
- diagnosis trace structure
- log emission + report linkage
- tests for trace presence and sensitive-data boundaries

PR-E: UI/API upgrades
- top root candidate and timeline emphasis
- list summary chips
- API shape tests and UI smoke tests

PR-F: Release prep
- version bump to `0.4.7`
- changelog/release note draft
- rollout and controlled alert checklist

## Acceptance Criteria

- No unbounded probing loops.
- Scope guard behavior is test-covered and deterministic.
- New reports provide ranked root cause candidates with evidence timeline.
- Event bursts do not create excessive duplicate reports.
- UI exposes the new attribution summary without placeholder noise.
- Full test suite passes before merge and before tag.

## Release Gates

1. All planned PRs merged to `main`.
2. `python3 -m pytest -q` passes.
3. CI + release workflows green.
4. Controlled cluster validation:
- alert-triggered report has normalized fields and ranked candidates
- metrics/logs show bounded probing and no scope violations

## Out of Scope

- Multi-cluster federation
- Auto-remediation/mutations
- External metrics systems integration
