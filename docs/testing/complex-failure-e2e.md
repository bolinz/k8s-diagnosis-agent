# Complex Failure E2E

This test injects a real multi-signal workload failure into the cluster and verifies that the diagnosis agent can produce a high-quality `DiagnosisReport` with correlation context.

## What It Injects

Namespace: `diag-e2e`

Injected objects:
- `Deployment/e2e-checkout` (3 replicas, `progressDeadlineSeconds=120`)
- `Deployment/e2e-config-break` (2 replicas, missing Secret references)
- `PersistentVolumeClaim/e2e-unbound-pvc` (invalid `storageClassName`)

Fault signals generated:
- `Pending` / PVC unbound scheduling pressure
- `FailedMount` (missing volume secret)
- `CreateContainerConfigError` (missing env secret)
- rollout stuck path (`ProgressDeadlineExceeded` expected after deadline)

## Run

```bash
cd k8s-diagnosis-agent
chmod +x scripts/e2e/run_complex_failure.sh scripts/e2e/assert_complex_failure.py
scripts/e2e/run_complex_failure.sh
```

Optional overrides:

```bash
WORKLOAD_NS=diag-e2e REPORT_NS=k8s-diagnosis-system WAIT_SECONDS=300 scripts/e2e/run_complex_failure.sh
```

You can also tune assertion retry window for slower scan/model cycles:

```bash
WORKLOAD_NS=diag-e2e REPORT_NS=k8s-diagnosis-system WAIT_SECONDS=180 \
ASSERT_WAIT_SECONDS=420 ASSERT_INTERVAL_SECONDS=30 \
scripts/e2e/run_complex_failure.sh
```

## Pass Criteria

The assertion script requires at least one new report in `REPORT_NS` where:
- `status.summary` is non-empty
- `status.evidence` is non-empty
- `status.recommendations` is non-empty
- `status.relatedObjects` has at least 2 objects
- `status.rootCauseCandidates` is non-empty
- `spec.workloadRef.kind/name` is complete
- no misleading placeholders (`unknown`, `n/a`) in summary/raw signal

## Manual Inspection Commands

```bash
kubectl get diagnosisreports.ops.ai.yourorg -n k8s-diagnosis-system
kubectl get diagnosisreport <report-name> -n k8s-diagnosis-system -o json | jq '.spec,.status'
kubectl logs -n k8s-diagnosis-system deployment/k8s-diagnosis-agent --since=15m
```

## Cleanup

```bash
kubectl delete ns diag-e2e
```

## Capability Proof (2026-03-27)

Validated on cluster `<redacted-cluster>` with image:

- `ghcr.io/bolinz/k8s-diagnosis-agent-app:sha-076127160ac72cffd95c94920214b1d87de1a086`

Execution:

```bash
WORKLOAD_NS=diag-e2e REPORT_NS=k8s-diagnosis-system WAIT_SECONDS=180 \
ASSERT_WAIT_SECONDS=420 ASSERT_INTERVAL_SECONDS=30 \
scripts/e2e/run_complex_failure.sh
```

Observed result:

- Assertion output: `{"ok": true, "reportName": "diagnosis-02212e9683", "symptom": "Pending", "relatedObjects": 8, "rootCauseCandidates": 4}`
- Event stream included mixed real signals: `FailedScheduling`, `FailedMount`, and PVC `ProvisioningFailed`
- Report quality checks passed (`summary/evidence/recommendations` non-empty, structured correlation fields present)
