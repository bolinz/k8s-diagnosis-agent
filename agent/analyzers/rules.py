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
    "CreateContainerConfigError",
    "CreateContainerError",
    "ContainerCannotRun",
    "FailedMount",
    "FailedCreatePodSandbox",
    "Evicted",
    "ProgressDeadlineExceeded",
    "ReplicaMismatch",
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
        raw_message = str(trigger.raw_signal.get("message", ""))
        lower_message = raw_message.lower()
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
            if any(token in lower_message for token in {"insufficient", "didn't have enough", "insufficient cpu", "insufficient memory", "insufficient ephemeral-storage"}):
                probable_causes = [
                    "The scheduler could not place the pod because cluster resources are insufficient"
                ]
                recommendations = [
                    "Check node allocatable capacity and current requests; lower pod requests or add capacity"
                ]
            elif any(token in lower_message for token in {"taint", "didn't tolerate", "affinity", "selector", "topology"}):
                probable_causes = [
                    "Scheduling constraints (taints, affinity, selectors, topology rules) prevent placement"
                ]
                recommendations = [
                    "Review pod tolerations, affinity, node selectors, and topology constraints"
                ]
            else:
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
        elif symptom == "CreateContainerConfigError":
            severity = "critical"
            probable_causes = [
                "A referenced Secret, ConfigMap, env source, or volume configuration is invalid or missing"
            ]
            recommendations = [
                "Verify referenced Secret and ConfigMap names, envFrom entries, and projected volume sources"
            ]
        elif symptom in {"CreateContainerError", "ContainerCannotRun"}:
            severity = "critical"
            probable_causes = [
                "The container entrypoint, command, permissions, or runtime arguments are invalid"
            ]
            recommendations = [
                "Inspect container command, args, image contents, and recent container status reasons"
            ]
        elif symptom == "FailedMount":
            severity = "critical"
            if any(token in lower_message for token in {"persistentvolumeclaim", "pvc", "not bound", "unbound"}):
                probable_causes = [
                    "A referenced PVC is not bound, so the pod cannot mount required storage"
                ]
                recommendations = [
                    "Inspect PVC phase, storage class, and events; resolve pending/binding issues before retrying"
                ]
            elif any(token in lower_message for token in {"secret", "configmap", "not found", "couldn't find key"}):
                probable_causes = [
                    "A referenced Secret or ConfigMap for a volume mount is missing or invalid"
                ]
                recommendations = [
                    "Verify volume source names and keys for Secrets/ConfigMaps in the pod spec"
                ]
            else:
                probable_causes = [
                    "A PVC, Secret, ConfigMap, or projected volume cannot be mounted into the pod"
                ]
                recommendations = [
                    "Inspect pod events, PVC binding state, and referenced volume sources"
                ]
        elif symptom == "FailedCreatePodSandbox":
            severity = "critical"
            probable_causes = [
                "The cluster runtime failed to create the pod sandbox before containers could start"
            ]
            recommendations = [
                "Review pod events and node runtime messages for sandbox or CNI startup failures"
            ]
        elif symptom == "Evicted":
            severity = "critical"
            probable_causes = [
                "The pod was evicted because the node was under memory, disk, or PID pressure"
            ]
            recommendations = [
                "Review node pressure signals and adjust requests, limits, or pod placement"
            ]
        elif symptom == "ProgressDeadlineExceeded":
            severity = "critical"
            probable_causes = [
                "The deployment rollout is stalled because new replicas are not becoming ready"
            ]
            recommendations = [
                "Inspect deployment conditions, related pod events, probes, and image or dependency failures"
            ]
        elif symptom == "ReplicaMismatch":
            probable_causes = [
                "Desired and available replicas diverge because rollout or readiness is not completing"
            ]
            recommendations = [
                "Inspect deployment status, unavailable replicas, and related pod conditions"
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
