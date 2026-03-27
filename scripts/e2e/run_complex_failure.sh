#!/usr/bin/env bash
set -euo pipefail

WORKLOAD_NS="${WORKLOAD_NS:-diag-e2e}"
REPORT_NS="${REPORT_NS:-k8s-diagnosis-system}"
WAIT_SECONDS="${WAIT_SECONDS:-240}"
ASSERT_WAIT_SECONDS="${ASSERT_WAIT_SECONDS:-240}"
ASSERT_INTERVAL_SECONDS="${ASSERT_INTERVAL_SECONDS:-30}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MANIFEST="${ROOT_DIR}/deploy/e2e/complex-failure.yaml"
ASSERT_SCRIPT="${ROOT_DIR}/scripts/e2e/assert_complex_failure.py"

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  cat <<'EOF'
Usage:
  WORKLOAD_NS=diag-e2e REPORT_NS=k8s-diagnosis-system WAIT_SECONDS=240 scripts/e2e/run_complex_failure.sh

Inject a multi-signal workload failure and assert the generated DiagnosisReport quality.
EOF
  exit 0
fi

echo "[e2e] test_start=$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
TEST_START="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

echo "[e2e] ensure namespace ${WORKLOAD_NS}"
kubectl get ns "${WORKLOAD_NS}" >/dev/null 2>&1 || kubectl create ns "${WORKLOAD_NS}"

echo "[e2e] apply complex failure manifest"
kubectl apply -f "${MANIFEST}"

echo "[e2e] wait for signal accumulation (${WAIT_SECONDS}s)"
sleep "${WAIT_SECONDS}"

echo "[e2e] workload snapshot"
kubectl get pods -n "${WORKLOAD_NS}" -o wide || true
kubectl get pvc -n "${WORKLOAD_NS}" || true
kubectl get deploy -n "${WORKLOAD_NS}" || true

echo "[e2e] recent warning events"
kubectl get events -n "${WORKLOAD_NS}" --sort-by=.lastTimestamp | tail -n 40 || true

echo "[e2e] assert DiagnosisReport quality (retry up to ${ASSERT_WAIT_SECONDS}s)"
ASSERT_DEADLINE=$(( $(date +%s) + ASSERT_WAIT_SECONDS ))
while true; do
  if python3 "${ASSERT_SCRIPT}" \
    --report-namespace "${REPORT_NS}" \
    --workload-namespace "${WORKLOAD_NS}" \
    --test-start "${TEST_START}"; then
    break
  fi
  NOW_TS="$(date +%s)"
  if (( NOW_TS >= ASSERT_DEADLINE )); then
    echo "[e2e] assertion timed out after ${ASSERT_WAIT_SECONDS}s"
    exit 2
  fi
  echo "[e2e] no valid report yet, retry in ${ASSERT_INTERVAL_SECONDS}s..."
  sleep "${ASSERT_INTERVAL_SECONDS}"
done

echo "[e2e] PASS"
echo "[e2e] cleanup with: kubectl delete ns ${WORKLOAD_NS}"
