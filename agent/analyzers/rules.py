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
    "PVCPending",
    "VolumeResizeFailure",
    "NetworkPolicyBlocking",
    "HPAMaxReplicas",
    "ResourceQuotaExceeded",
    "PDBDisruptionBlocked",
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
            subtype = _classify_image_pull_issue(lower_message)
            if subtype == "auth":
                probable_causes = [
                    "Registry authentication failed while pulling the container image"
                ]
                recommendations = [
                    "Verify imagePullSecrets, registry credentials, and service account secret references",
                    "Confirm the registry repository permissions include pull access",
                ]
            elif subtype == "not_found":
                probable_causes = [
                    "The image name or tag is invalid, or the referenced artifact does not exist"
                ]
                recommendations = [
                    "Verify image repository and tag/digest exactly match published artifacts",
                    "Confirm the image exists in the target registry and has not been deleted",
                ]
            elif subtype == "network":
                probable_causes = [
                    "Network, DNS, or TLS connectivity to the registry failed during image pull"
                ]
                recommendations = [
                    "Check node egress, DNS resolution, proxy/firewall rules, and registry TLS configuration",
                    "Confirm the registry endpoint is reachable from cluster nodes",
                ]
            else:
                probable_causes = [
                    "Container image pull failed due to invalid image reference, auth, or registry connectivity"
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
        elif symptom == "PVCPending":
            severity = "warning"
            raw_message = str(trigger.raw_signal.get("message", "")).lower()
            if any(t in raw_message for t in {"waiting", "pending", "not bound", "unbound", "cannot find", "claim"}):
                probable_causes = [
                    "A PVC referenced by the pod is not in a Bound state, preventing volume mounting"
                ]
                recommendations = [
                    "Check PVC phase, storage class, and binding conditions; ensure PV exists and is not pre-bound elsewhere",
                    "Inspect events for the PVC to identify why binding is pending (e.g., no storage class, no PV, or WaitForFirstConsumer not satisfied)"
                ]
            else:
                probable_causes = [
                    "A required volume is not ready for the pod to start"
                ]
                recommendations = [
                    "Review pod events and PVC status to identify the volume provisioning issue"
                ]
        elif symptom == "VolumeResizeFailure":
            severity = "warning"
            probable_causes = [
                "A PersistentVolumeClaim failed to expand its underlying volume after capacity increase was requested"
            ]
            recommendations = [
                "Check PVC conditions for FileSystemResizePending; verify storage class allows expansion and that the PV supports online expansion",
                "If the filesystem is already full, a pod restart may be required after the expansion is complete"
            ]
        elif symptom == "NetworkPolicyBlocking":
            severity = "warning"
            probable_causes = [
                "A NetworkPolicy applied to the pod's namespace or pod selector is blocking required network connectivity"
            ]
            recommendations = [
                "Review NetworkPolicy specs in the namespace and verify that allowed ingress/egress rules cover required communication",
                "Check pod events for connection failures that match the policy's blocked ports or label selectors"
            ]
        elif symptom == "HPAMaxReplicas":
            severity = "warning"
            probable_causes = [
                "The HorizontalPodAutoscaler has reached its maxReplicas ceiling and cannot scale up further"
            ]
            recommendations = [
                "Review HPA spec maxReplicas and current replica count; assess whether the limit should be raised",
                "Inspect resource requests and limits; if CPU/memory are the scaling metric, consider increasing pod resource requests or adjusting the HPA target utilization"
            ]
        elif symptom == "ResourceQuotaExceeded":
            severity = "warning"
            probable_causes = [
                "A Namespace ResourceQuota is preventing pod or resource creation due to exceeded hard limits"
            ]
            recommendations = [
                "Run kubectl describe resourcequota -n <namespace> to see which quota is exceeded",
                "Either request a quota increase from the cluster admin or reduce resource usage within the namespace"
            ]
        elif symptom == "PDBDisruptionBlocked":
            severity = "warning"
            probable_causes = [
                "A PodDisruptionBudget's MinAvailable requirement is not satisfied, blocking voluntary disruptions"
            ]
            recommendations = [
                "Review PDB MinAvailable vs current ready pods; ensure enough pods are running to satisfy the disruption budget",
                "If performing a node drain, verify that the drain is not needed simultaneously across multiple pods constrained by the same PDB"
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


def _classify_image_pull_issue(lower_message: str) -> str:
    if any(
        token in lower_message
        for token in {"unauthorized", "authentication required", "denied", "forbidden", "no basic auth credentials"}
    ):
        return "auth"
    if any(token in lower_message for token in {"manifest unknown", "not found", "name unknown", "pull access denied"}):
        return "not_found"
    if any(
        token in lower_message
        for token in {"i/o timeout", "context deadline exceeded", "connection refused", "no such host", "tls handshake timeout", "temporary failure"}
    ):
        return "network"
    return "generic"
