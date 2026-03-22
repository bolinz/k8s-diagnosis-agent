from __future__ import annotations

from dataclasses import dataclass

from agent.models import DiagnosisResult, PendingFinding, TriggerContext, WorkloadRef


SUPPORTED_SYMPTOMS = {
    "CrashLoopBackOff",
    "ImagePullBackOff",
    "ErrImagePull",
    "OOMKilled",
    "Pending",
    "ProbeFailure",
    "NodeNotReadyImpact",
}


@dataclass
class RuleEngine:
    cluster_name: str
    min_observation_seconds: int

    def findings_from_snapshot(self, snapshot: list[dict]) -> list[PendingFinding]:
        findings: list[PendingFinding] = []
        for item in snapshot:
            symptom = item.get("symptom")
            observed_for = int(item.get("observed_for_seconds", 0))
            if symptom not in SUPPORTED_SYMPTOMS:
                continue
            if observed_for < self.min_observation_seconds:
                continue
            workload = WorkloadRef(
                kind=item.get("kind", "Workload"),
                namespace=item["namespace"],
                name=item["name"],
            )
            trigger = TriggerContext(
                source=item.get("source", "scheduled"),
                cluster=self.cluster_name,
                workload=workload,
                symptom=symptom,
                observed_for_seconds=observed_for,
                raw_signal=item,
            )
            findings.append(
                PendingFinding(
                    trigger=trigger,
                    rule_hint=item.get("rule_hint", symptom),
                )
            )
        return findings

    def fallback_diagnosis(self, trigger: TriggerContext) -> DiagnosisResult:
        symptom = trigger.symptom
        summary = f"{symptom} detected for {trigger.workload.kind}/{trigger.workload.name}"
        evidence = [
            f"symptom={symptom}",
            f"observed_for_seconds={trigger.observed_for_seconds}",
        ]
        probable_causes: list[str]
        recommendations: list[str]
        severity = "warning"

        if symptom == "CrashLoopBackOff":
            probable_causes = [
                "Application exits immediately after startup",
                "Startup or liveness probes may be too aggressive",
            ]
            recommendations = [
                "Inspect previous container logs and recent restart reasons",
                "Verify startup command, secrets, and probe thresholds",
            ]
        elif symptom in {"ImagePullBackOff", "ErrImagePull"}:
            severity = "critical"
            probable_causes = [
                "Container image tag is missing or registry access is denied"
            ]
            recommendations = [
                "Verify the image reference and imagePullSecrets",
                "Confirm the target registry is reachable from the cluster",
            ]
        elif symptom == "OOMKilled":
            severity = "critical"
            probable_causes = ["Container memory limit is lower than runtime demand"]
            recommendations = [
                "Inspect memory usage and increase limits or reduce memory spikes"
            ]
        elif symptom == "Pending":
            probable_causes = [
                "Scheduler constraints or insufficient cluster capacity prevent placement"
            ]
            recommendations = [
                "Review Pod events for taints, affinity, quota, and resource shortage messages"
            ]
        elif symptom == "ProbeFailure":
            probable_causes = ["Health probes fail before the service is ready"]
            recommendations = [
                "Relax initial delay and timeout values or fix the health endpoint"
            ]
        else:
            probable_causes = [
                "A node condition or cluster dependency is affecting the workload"
            ]
            recommendations = [
                "Inspect node conditions and workload rescheduling behavior"
            ]

        return DiagnosisResult(
            summary=summary,
            severity=severity,
            probable_causes=probable_causes,
            evidence=evidence,
            recommendations=recommendations,
            confidence=0.35,
            used_fallback=True,
            raw_agent_output={"mode": "fallback"},
        )
