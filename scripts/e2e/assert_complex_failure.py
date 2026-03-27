#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any


def _run_json(cmd: list[str]) -> dict[str, Any]:
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"command failed: {' '.join(cmd)}\n{proc.stderr.strip()}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid json output: {' '.join(cmd)}") from exc


def _parse_time(value: str) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _report_matches(
    report: dict[str, Any],
    workload_namespace: str,
    test_start: datetime | None,
) -> bool:
    spec = report.get("spec", {})
    if spec.get("namespace") != workload_namespace:
        return False
    if test_start is None:
        return True
    analyzed = _parse_time(report.get("status", {}).get("lastAnalyzedAt", ""))
    if analyzed is None:
        return False
    return analyzed >= test_start


def _contains_noise_tokens(text: str) -> bool:
    lower = text.lower()
    return "unknown" in lower or "n/a" in lower


def _validate(report: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    spec = report.get("spec", {})
    status = report.get("status", {})
    summary = str(status.get("summary", "")).strip()
    evidence = status.get("evidence", [])
    recommendations = status.get("recommendations", [])
    related = status.get("relatedObjects", [])
    root_causes = status.get("rootCauseCandidates", [])
    timeline = status.get("evidenceTimeline", [])

    if not summary:
        errors.append("status.summary is empty")
    if not isinstance(evidence, list) or not evidence:
        errors.append("status.evidence is empty")
    if not isinstance(recommendations, list) or not recommendations:
        errors.append("status.recommendations is empty")
    if not isinstance(related, list) or len(related) < 2:
        errors.append("status.relatedObjects should contain >= 2 items")
    if not isinstance(root_causes, list) or not root_causes:
        errors.append("status.rootCauseCandidates is empty")
    if not isinstance(timeline, list):
        errors.append("status.evidenceTimeline is not a list")

    workload = spec.get("workloadRef", {})
    if not workload.get("kind") or not workload.get("name"):
        errors.append("spec.workloadRef is incomplete")

    symptom = str(spec.get("symptom", "")).strip()
    supported_signals = {
        "ProgressDeadlineExceeded",
        "ReplicaMismatch",
        "CreateContainerConfigError",
        "FailedMount",
        "Pending",
    }
    if symptom and symptom not in supported_signals:
        errors.append(f"unexpected symptom '{symptom}' for this test")

    raw_signal = status.get("rawSignal", {})
    raw_text = json.dumps(raw_signal, ensure_ascii=False)
    if _contains_noise_tokens(summary):
        errors.append("status.summary contains placeholder token (unknown/n/a)")
    if _contains_noise_tokens(raw_text):
        errors.append("status.rawSignal contains placeholder token (unknown/n/a)")

    return errors


def _pick_best_report(reports: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not reports:
        return None
    reports.sort(
        key=lambda item: item.get("status", {}).get("lastAnalyzedAt", ""),
        reverse=True,
    )
    return reports[0]


def main() -> int:
    parser = argparse.ArgumentParser(description="Assert complex failure DiagnosisReport")
    parser.add_argument("--report-namespace", default="k8s-diagnosis-system")
    parser.add_argument("--workload-namespace", default="diag-e2e")
    parser.add_argument("--test-start", default="", help="ISO8601 UTC timestamp")
    args = parser.parse_args()

    test_start = _parse_time(args.test_start) if args.test_start else None
    payload = _run_json(
        [
            "kubectl",
            "get",
            "diagnosisreports.ops.ai.yourorg",
            "-n",
            args.report_namespace,
            "-o",
            "json",
        ]
    )
    all_reports = payload.get("items", [])
    target_reports = [
        item
        for item in all_reports
        if _report_matches(item, args.workload_namespace, test_start)
    ]
    if not target_reports:
        print("FAIL: no matching DiagnosisReport found", file=sys.stderr)
        return 2

    sorted_reports = sorted(
        target_reports,
        key=lambda item: item.get("status", {}).get("lastAnalyzedAt", ""),
        reverse=True,
    )
    first_failure_report: dict[str, Any] | None = None
    first_failure_errors: list[str] = []
    for report in sorted_reports:
        errors = _validate(report)
        if not errors:
            metadata = report.get("metadata", {})
            spec = report.get("spec", {})
            status = report.get("status", {})
            print(
                json.dumps(
                    {
                        "ok": True,
                        "reportName": metadata.get("name"),
                        "symptom": spec.get("symptom"),
                        "summary": status.get("summary"),
                        "relatedObjects": len(status.get("relatedObjects", [])),
                        "rootCauseCandidates": len(status.get("rootCauseCandidates", [])),
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        if first_failure_report is None:
            first_failure_report = report
            first_failure_errors = errors

    print("FAIL: no matching report satisfied quality checks", file=sys.stderr)
    if first_failure_errors:
        for error in first_failure_errors:
            print(f"- {error}", file=sys.stderr)
    if first_failure_report is not None:
        print(json.dumps(first_failure_report, ensure_ascii=False, indent=2), file=sys.stderr)
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
