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


def _report_matches(report: dict[str, Any], workload_namespace: str, test_start: datetime | None) -> bool:
    spec = report.get("spec", {})
    if spec.get("namespace") != workload_namespace:
        return False
    if test_start is None:
        return True
    analyzed = _parse_time(report.get("status", {}).get("lastAnalyzedAt", ""))
    if analyzed is None:
        return False
    return analyzed >= test_start


def _is_nonempty_text_list(value: Any) -> bool:
    if not isinstance(value, list):
        return False
    for item in value:
        if str(item or "").strip():
            return True
    return False


def _validate_v060(report: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    status = report.get("status", {})

    quality = status.get("qualityScore")
    if not isinstance(quality, dict) or not quality:
        errors.append("status.qualityScore is missing or empty")
    else:
        if "overall" not in quality:
            errors.append("status.qualityScore.overall is missing")
        dims = quality.get("dimensions")
        if not isinstance(dims, dict) or not dims:
            errors.append("status.qualityScore.dimensions is missing or empty")

    uncertainties = status.get("uncertainties")
    if not isinstance(uncertainties, list):
        errors.append("status.uncertainties is missing or not a list")

    attribution = status.get("evidenceAttribution")
    if not isinstance(attribution, list) or not attribution:
        errors.append("status.evidenceAttribution is missing or empty")
    else:
        has_source_signal = any(
            isinstance(item, dict)
            and str(item.get("source", "")).strip()
            and str(item.get("signal", "")).strip()
            for item in attribution
        )
        if not has_source_signal:
            errors.append("status.evidenceAttribution lacks source/signal entries")

    if not _is_nonempty_text_list(status.get("evidence")):
        errors.append("status.evidence is empty")
    if not _is_nonempty_text_list(status.get("recommendations")):
        errors.append("status.recommendations is empty")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Assert v0.6 explainability fields on DiagnosisReport")
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
    reports = [
        item
        for item in payload.get("items", [])
        if _report_matches(item, args.workload_namespace, test_start)
    ]
    if not reports:
        print("FAIL: no matching DiagnosisReport found", file=sys.stderr)
        return 2

    reports.sort(key=lambda item: item.get("status", {}).get("lastAnalyzedAt", ""), reverse=True)
    latest = reports[0]
    errors = _validate_v060(latest)
    if errors:
        print("FAIL: v0.6 explainability checks failed", file=sys.stderr)
        for err in errors:
            print(f"- {err}", file=sys.stderr)
        print(json.dumps(latest, ensure_ascii=False, indent=2), file=sys.stderr)
        return 3

    status = latest.get("status", {})
    print(
        json.dumps(
            {
                "ok": True,
                "reportName": latest.get("metadata", {}).get("name"),
                "analysisVersion": status.get("analysisVersion"),
                "qualityOverall": status.get("qualityScore", {}).get("overall"),
                "uncertainties": len(status.get("uncertainties", [])),
                "evidenceAttribution": len(status.get("evidenceAttribution", [])),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
