from __future__ import annotations

from typing import Any

from agent.models import TriggerContext

try:
    from kubernetes import client, config
    from kubernetes.client import ApiException
    from kubernetes.watch import Watch
except ImportError:  # pragma: no cover - exercised only when dependency missing
    client = None
    config = None
    ApiException = Exception
    Watch = None


POD_WAITING_REASONS = {
    "CrashLoopBackOff",
    "ImagePullBackOff",
    "ErrImagePull",
    "CreateContainerConfigError",
    "CreateContainerError",
    "ContainerCannotRun",
}


def _coerce_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return {"value": str(value)}


def _coerce_list(items: list[Any] | None) -> list[dict[str, Any]]:
    return [_coerce_dict(item) for item in items or []]


def _api_error(exc: Exception, resource: str, **context: Any) -> dict[str, Any]:
    payload = {"error": str(exc), "resource": resource}
    payload.update({key: value for key, value in context.items() if value is not None})
    return payload


def _selector_string(match_labels: dict[str, str] | None) -> str:
    if not match_labels:
        return ""
    return ",".join(f"{key}={value}" for key, value in sorted(match_labels.items()))


def _condition_summaries(conditions: list[Any] | None) -> list[dict[str, Any]]:
    items = []
    for condition in conditions or []:
        items.append(
            {
                "type": getattr(condition, "type", None),
                "status": getattr(condition, "status", None),
                "reason": getattr(condition, "reason", None),
                "message": getattr(condition, "message", None),
            }
        )
    return items


def _object_ref(kind: str | None, namespace: str | None, name: str | None, **extra: Any) -> dict[str, Any]:
    payload = {
        "kind": kind or "",
        "namespace": namespace or "",
        "name": name or "",
    }
    payload.update({key: value for key, value in extra.items() if value not in (None, "")})
    return payload


def _container_spec_summary(container: Any) -> dict[str, Any]:
    return {
        "name": getattr(container, "name", None),
        "image": getattr(container, "image", None),
        "command": list(getattr(container, "command", None) or []),
        "args": list(getattr(container, "args", None) or []),
        "env": [
            {
                "name": getattr(env, "name", None),
                "valueFrom": _coerce_dict(getattr(env, "value_from", None)),
            }
            for env in getattr(container, "env", None) or []
        ],
        "envFrom": [_coerce_dict(item) for item in getattr(container, "env_from", None) or []],
        "volumeMounts": [
            {
                "name": getattr(mount, "name", None),
                "mountPath": getattr(mount, "mount_path", None),
                "readOnly": getattr(mount, "read_only", None),
            }
            for mount in getattr(container, "volume_mounts", None) or []
        ],
        "livenessProbe": _coerce_dict(getattr(container, "liveness_probe", None)),
        "readinessProbe": _coerce_dict(getattr(container, "readiness_probe", None)),
        "startupProbe": _coerce_dict(getattr(container, "startup_probe", None)),
    }


class RuntimeKubernetesClient:
    def __init__(self, report_namespace: str) -> None:
        if client is None or config is None:
            raise RuntimeError(
                "The kubernetes dependency is required for the runtime client."
            )
        try:
            config.load_incluster_config()
        except Exception:
            config.load_kube_config()
        self.report_namespace = report_namespace
        self.core = client.CoreV1Api()
        self.apps = client.AppsV1Api()
        self.events = client.CoreV1Api()
        self.autoscaling = client.AutoscalingV2Api()
        self.custom = client.CustomObjectsApi()

    def list_anomaly_snapshot(self) -> list[dict]:
        findings: list[dict] = []
        pods = self.core.list_pod_for_all_namespaces(watch=False)
        for pod in pods.items:
            namespace = pod.metadata.namespace
            name = pod.metadata.name
            for status in pod.status.container_statuses or []:
                waiting = getattr(status.state, "waiting", None)
                if waiting and waiting.reason in POD_WAITING_REASONS:
                    findings.append(
                        {
                            "namespace": namespace,
                            "name": name,
                            "kind": "Pod",
                            "symptom": waiting.reason,
                            "observed_for_seconds": 1800,
                            "containerReason": waiting.reason,
                        }
                    )
                terminated = getattr(status.state, "terminated", None)
                if terminated and terminated.reason == "OOMKilled":
                    findings.append(
                        {
                            "namespace": namespace,
                            "name": name,
                            "kind": "Pod",
                            "symptom": "OOMKilled",
                            "observed_for_seconds": 1800,
                            "containerReason": terminated.reason,
                        }
                    )
            if pod.status.phase == "Pending":
                findings.append(
                    {
                        "namespace": namespace,
                        "name": name,
                        "kind": "Pod",
                        "symptom": "Pending",
                        "observed_for_seconds": 1800,
                        "podPhase": pod.status.phase,
                        "podReason": pod.status.reason,
                    }
                )
            if pod.status.reason == "Evicted":
                findings.append(
                    {
                        "namespace": namespace,
                        "name": name,
                        "kind": "Pod",
                        "symptom": "Evicted",
                        "observed_for_seconds": 1800,
                        "podPhase": pod.status.phase,
                        "podReason": pod.status.reason,
                    }
                )

        deployments = self.apps.list_deployment_for_all_namespaces(watch=False)
        for deployment in deployments.items:
            namespace = deployment.metadata.namespace
            name = deployment.metadata.name
            desired = int(getattr(deployment.spec, "replicas", 0) or 0)
            available = int(getattr(deployment.status, "available_replicas", 0) or 0)
            unavailable = int(getattr(deployment.status, "unavailable_replicas", 0) or 0)
            for condition in deployment.status.conditions or []:
                if (
                    getattr(condition, "type", "") == "Progressing"
                    and getattr(condition, "reason", "") == "ProgressDeadlineExceeded"
                ):
                    findings.append(
                        {
                            "namespace": namespace,
                            "name": name,
                            "kind": "Deployment",
                            "symptom": "ProgressDeadlineExceeded",
                            "observed_for_seconds": 1800,
                            "deploymentCondition": getattr(condition, "reason", ""),
                        }
                    )
            if desired > 0 and (available < desired or unavailable > 0):
                findings.append(
                    {
                        "namespace": namespace,
                        "name": name,
                        "kind": "Deployment",
                        "symptom": "ReplicaMismatch",
                        "observed_for_seconds": 1800,
                        "deploymentCondition": "ReplicaMismatch",
                    }
                )
        return findings

    def _read_workload(self, namespace: str, kind: str, name: str):
        kind_lower = kind.lower()
        if kind_lower == "pod":
            return self.core.read_namespaced_pod(name, namespace)
        if kind_lower == "deployment":
            return self.apps.read_namespaced_deployment(name, namespace)
        if kind_lower == "replicaset":
            return self.apps.read_namespaced_replica_set(name, namespace)
        if kind_lower == "statefulset":
            return self.apps.read_namespaced_stateful_set(name, namespace)
        if kind_lower == "daemonset":
            return self.apps.read_namespaced_daemon_set(name, namespace)
        raise ValueError(f"Unsupported workload kind: {kind}")

    def _pod_template_spec(self, namespace: str, kind: str, name: str):
        workload = self._read_workload(namespace, kind, name)
        if kind.lower() == "pod":
            return getattr(workload, "spec", None)
        template = getattr(getattr(workload, "spec", None), "template", None)
        return getattr(template, "spec", None)

    def get_workload_status(self, namespace: str, kind: str, name: str) -> dict:
        try:
            workload = self._read_workload(namespace, kind, name)
        except Exception as exc:
            return _api_error(exc, "workload", namespace=namespace, kind=kind, name=name)
        return _coerce_dict(workload)

    def get_workload_events(self, namespace: str, kind: str, name: str) -> dict:
        try:
            events = self.events.list_namespaced_event(
                namespace=namespace,
                field_selector=f"involvedObject.kind={kind},involvedObject.name={name}",
            )
        except Exception as exc:
            return _api_error(exc, "workload_events", namespace=namespace, kind=kind, name=name)
        return {
            "items": [
                {
                    "reason": item.reason,
                    "message": item.message,
                    "type": item.type,
                    "timestamp": str(
                        getattr(item, "event_time", None)
                        or getattr(item, "last_timestamp", None)
                        or getattr(item, "first_timestamp", None)
                        or ""
                    ),
                }
                for item in events.items
            ]
        }

    def get_owner_chain(self, namespace: str, kind: str, name: str) -> dict:
        try:
            current = self._read_workload(namespace, kind, name)
        except Exception as exc:
            return _api_error(exc, "owner_chain", namespace=namespace, kind=kind, name=name)
        items: list[dict[str, Any]] = []
        current_kind = kind
        current_namespace = namespace
        while current is not None:
            metadata = getattr(current, "metadata", None)
            items.append(
                _object_ref(
                    current_kind,
                    current_namespace,
                    getattr(metadata, "name", name),
                )
            )
            owner_refs = list(getattr(metadata, "owner_references", None) or [])
            if not owner_refs:
                break
            owner = owner_refs[0]
            current_kind = owner.kind
            current_namespace = namespace
            try:
                current = self._read_workload(namespace, owner.kind, owner.name)
            except Exception:
                items.append(_object_ref(owner.kind, namespace, owner.name))
                break
        return {"items": items}

    def get_related_events(self, namespace: str, kind: str, name: str) -> dict:
        result = self.get_workload_events(namespace, kind, name)
        items = result.get("items", []) if isinstance(result, dict) else []
        return {"items": items[:10], "window": "recent"}

    def list_related_pods(self, namespace: str, kind: str, name: str) -> dict:
        try:
            if kind.lower() == "pod":
                pod = self.core.read_namespaced_pod(name, namespace)
                return {"items": [_coerce_dict(pod)]}
            workload = self._read_workload(namespace, kind, name)
            selector = _selector_string(
                getattr(getattr(workload, "spec", None), "selector", None).match_labels
                if getattr(getattr(workload, "spec", None), "selector", None) is not None
                else None
            )
            if not selector:
                selector = f"app={name}"
            pods = self.core.list_namespaced_pod(namespace=namespace, label_selector=selector)
            return {"items": [_coerce_dict(item) for item in pods.items], "selector": selector}
        except Exception as exc:
            return _api_error(exc, "related_pods", namespace=namespace, kind=kind, name=name)

    def get_attached_pvcs(self, namespace: str, pod_name: str) -> dict:
        try:
            pod = self.core.read_namespaced_pod(name=pod_name, namespace=namespace)
        except Exception as exc:
            return _api_error(exc, "attached_pvcs", namespace=namespace, pod_name=pod_name)
        items = []
        for volume in pod.spec.volumes or []:
            claim = getattr(volume, "persistent_volume_claim", None)
            if claim is None or not getattr(claim, "claim_name", None):
                continue
            pvc = self.get_pvc_status(namespace=namespace, pvc_name=claim.claim_name)
            pvc_item = (pvc.get("items") or [{}])[0]
            items.append(
                {
                    "name": claim.claim_name,
                    "phase": pvc_item.get("phase"),
                    "volumeName": pvc_item.get("volumeName"),
                }
            )
        return {"items": items}

    def get_pvc_dependents(self, namespace: str, pvc_name: str) -> dict:
        try:
            pods = self.core.list_namespaced_pod(namespace=namespace)
        except Exception as exc:
            return _api_error(exc, "pvc_dependents", namespace=namespace, pvc_name=pvc_name)
        items = []
        for pod in pods.items:
            for volume in pod.spec.volumes or []:
                claim = getattr(volume, "persistent_volume_claim", None)
                if claim is not None and getattr(claim, "claim_name", None) == pvc_name:
                    items.append(
                        {
                            "kind": "Pod",
                            "namespace": namespace,
                            "name": pod.metadata.name,
                            "phase": getattr(pod.status, "phase", None),
                        }
                    )
                    break
        return {"items": items}

    def get_pod_events(self, namespace: str, pod_name: str) -> dict:
        try:
            events = self.events.list_namespaced_event(
                namespace=namespace, field_selector=f"involvedObject.name={pod_name}"
            )
        except Exception as exc:
            return _api_error(exc, "pod_events", namespace=namespace, pod_name=pod_name)
        return {"items": [_coerce_dict(item) for item in events.items]}

    def get_pod_spec_summary(self, namespace: str, pod_name: str) -> dict:
        try:
            pod = self.core.read_namespaced_pod(name=pod_name, namespace=namespace)
        except Exception as exc:
            return _api_error(exc, "pod_spec_summary", namespace=namespace, pod_name=pod_name)
        spec = pod.spec
        return {
            "serviceAccountName": spec.service_account_name,
            "nodeName": spec.node_name,
            "imagePullSecrets": [item.name for item in spec.image_pull_secrets or []],
            "containers": [_container_spec_summary(item) for item in spec.containers or []],
            "initContainers": [_container_spec_summary(item) for item in spec.init_containers or []],
            "volumes": [
                {
                    "name": volume.name,
                    "secret": _coerce_dict(volume.secret),
                    "configMap": _coerce_dict(volume.config_map),
                    "persistentVolumeClaim": _coerce_dict(volume.persistent_volume_claim),
                    "projected": _coerce_dict(volume.projected),
                }
                for volume in spec.volumes or []
            ],
        }

    def get_pod_conditions(self, namespace: str, pod_name: str) -> dict:
        try:
            pod = self.core.read_namespaced_pod(name=pod_name, namespace=namespace)
        except Exception as exc:
            return _api_error(exc, "pod_conditions", namespace=namespace, pod_name=pod_name)
        return {
            "phase": pod.status.phase,
            "reason": pod.status.reason,
            "message": getattr(pod.status, "message", None),
            "conditions": _condition_summaries(pod.status.conditions),
        }

    def get_container_statuses(self, namespace: str, pod_name: str) -> dict:
        try:
            pod = self.core.read_namespaced_pod(name=pod_name, namespace=namespace)
        except Exception as exc:
            return _api_error(exc, "container_statuses", namespace=namespace, pod_name=pod_name)
        return {"items": [_coerce_dict(item) for item in pod.status.container_statuses or []]}

    def get_deployment_status(self, namespace: str, name: str) -> dict:
        try:
            deployment = self.apps.read_namespaced_deployment(name, namespace)
        except Exception as exc:
            return _api_error(exc, "deployment_status", namespace=namespace, name=name)
        return {
            "replicas": getattr(deployment.spec, "replicas", 0),
            "updatedReplicas": getattr(deployment.status, "updated_replicas", 0),
            "availableReplicas": getattr(deployment.status, "available_replicas", 0),
            "unavailableReplicas": getattr(deployment.status, "unavailable_replicas", 0),
            "conditions": _condition_summaries(deployment.status.conditions),
            "selector": _selector_string(
                getattr(getattr(deployment.spec, "selector", None), "match_labels", None)
            ),
        }

    def get_replicaset_status(self, namespace: str, name: str) -> dict:
        try:
            rs = self.apps.read_namespaced_replica_set(name, namespace)
        except Exception as exc:
            return _api_error(exc, "replicaset_status", namespace=namespace, name=name)
        return {
            "replicas": getattr(rs.spec, "replicas", 0),
            "readyReplicas": getattr(rs.status, "ready_replicas", 0),
            "availableReplicas": getattr(rs.status, "available_replicas", 0),
            "conditions": _condition_summaries(rs.status.conditions),
            "ownerReferences": [
                {"kind": ref.kind, "name": ref.name}
                for ref in rs.metadata.owner_references or []
            ],
        }

    def get_config_refs(self, namespace: str, kind: str, name: str) -> dict:
        try:
            spec = self._pod_template_spec(namespace, kind, name)
        except Exception as exc:
            return _api_error(exc, "config_refs", namespace=namespace, kind=kind, name=name)
        items: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str, bool | None]] = set()
        for container in list(spec.containers or []) + list(spec.init_containers or []):
            for env_from in container.env_from or []:
                if env_from.secret_ref:
                    ref = ("Secret", env_from.secret_ref.name, namespace, getattr(env_from.secret_ref, "optional", None))
                    seen.add(ref)
                if env_from.config_map_ref:
                    ref = ("ConfigMap", env_from.config_map_ref.name, namespace, getattr(env_from.config_map_ref, "optional", None))
                    seen.add(ref)
            for env in container.env or []:
                value_from = env.value_from
                if not value_from:
                    continue
                if value_from.secret_key_ref:
                    ref = ("Secret", value_from.secret_key_ref.name, namespace, getattr(value_from.secret_key_ref, "optional", None))
                    seen.add(ref)
                if value_from.config_map_key_ref:
                    ref = ("ConfigMap", value_from.config_map_key_ref.name, namespace, getattr(value_from.config_map_key_ref, "optional", None))
                    seen.add(ref)
        for volume in spec.volumes or []:
            if volume.secret:
                seen.add(("Secret", volume.secret.secret_name, namespace, getattr(volume.secret, "optional", None)))
            if volume.config_map:
                seen.add(("ConfigMap", volume.config_map.name, namespace, getattr(volume.config_map, "optional", None)))
            if volume.projected:
                for source in volume.projected.sources or []:
                    if source.secret:
                        seen.add(("Secret", source.secret.name, namespace, getattr(source.secret, "optional", None)))
                    if source.config_map:
                        seen.add(("ConfigMap", source.config_map.name, namespace, getattr(source.config_map, "optional", None)))
        for ref_type, ref_name, ref_namespace, optional in sorted(seen):
            if ref_name:
                items.append(
                    {
                        "type": ref_type,
                        "name": ref_name,
                        "namespace": ref_namespace,
                        "optional": optional,
                    }
                )
        return {"items": items}

    def get_pvc_status(
        self,
        namespace: str,
        pvc_name: str | None = None,
        pod_name: str | None = None,
    ) -> dict:
        try:
            pvc_names: list[str] = []
            if pvc_name:
                pvc_names.append(pvc_name)
            if pod_name:
                pod = self.core.read_namespaced_pod(name=pod_name, namespace=namespace)
                pvc_names.extend(
                    volume.persistent_volume_claim.claim_name
                    for volume in pod.spec.volumes or []
                    if volume.persistent_volume_claim is not None
                )
            items = []
            for name in sorted(set(filter(None, pvc_names))):
                pvc = self.core.read_namespaced_persistent_volume_claim(name=name, namespace=namespace)
                items.append(
                    {
                        "name": pvc.metadata.name,
                        "phase": getattr(pvc.status, "phase", None),
                        "storageClassName": getattr(pvc.spec, "storage_class_name", None),
                        "accessModes": list(getattr(pvc.status, "access_modes", None) or []),
                        "capacity": _coerce_dict(getattr(pvc.status, "capacity", None)),
                        "volumeName": getattr(pvc.spec, "volume_name", None),
                    }
                )
            return {"items": items}
        except Exception as exc:
            return _api_error(exc, "pvc_status", namespace=namespace, pvc_name=pvc_name, pod_name=pod_name)

    def get_recent_logs(
        self, namespace: str, pod_name: str, container: str | None = None
    ) -> dict:
        try:
            text = self.core.read_namespaced_pod_log(
                name=pod_name,
                namespace=namespace,
                container=container,
                tail_lines=200,
                timestamps=True,
            )
        except ApiException as exc:
            text = f"log_unavailable: {exc}"
        return {"pod_name": pod_name, "container": container, "logs": text}

    def get_node_conditions(self, node_name: str | None = None) -> dict:
        if node_name:
            try:
                node = self.core.read_node(node_name)
            except Exception as exc:
                return _api_error(exc, "node_conditions", node_name=node_name)
            return _coerce_dict(node)
        nodes = self.core.list_node()
        return {"items": [_coerce_dict(item) for item in nodes.items]}

    def get_node_workload_impact(self, node_name: str) -> dict:
        try:
            pods = self.core.list_pod_for_all_namespaces(
                watch=False,
                field_selector=f"spec.nodeName={node_name}",
            )
        except Exception as exc:
            return _api_error(exc, "node_workload_impact", node_name=node_name)
        items = []
        namespaces: set[str] = set()
        for pod in pods.items[:20]:
            namespaces.add(pod.metadata.namespace)
            items.append(
                {
                    "kind": "Pod",
                    "namespace": pod.metadata.namespace,
                    "name": pod.metadata.name,
                    "phase": getattr(pod.status, "phase", None),
                    "reason": getattr(pod.status, "reason", None),
                }
            )
        return {
            "node": node_name,
            "items": items,
            "podCount": len(items),
            "crossNamespace": len(namespaces) > 1,
        }

    def get_namespace_quotas(self, namespace: str) -> dict:
        try:
            quotas = self.core.list_namespaced_resource_quota(namespace)
        except Exception as exc:
            return _api_error(exc, "namespace_quotas", namespace=namespace)
        return {"items": [_coerce_dict(item) for item in quotas.items]}

    def get_hpa_status(self, namespace: str, target_name: str | None = None) -> dict:
        try:
            hpas = self.autoscaling.list_namespaced_horizontal_pod_autoscaler(namespace)
        except Exception as exc:
            return _api_error(exc, "hpa_status", namespace=namespace, target_name=target_name)
        items = [_coerce_dict(item) for item in hpas.items]
        if target_name:
            items = [
                item
                for item in items
                if item.get("spec", {})
                .get("scale_target_ref", {})
                .get("name")
                == target_name
            ]
        return {"items": items}

    def get_resource_metrics(self, namespace: str, name: str | None = None) -> dict:
        return {
            "status": "unavailable",
            "reason": "metrics integration is cluster-specific and should be wired through a dedicated adapter",
            "namespace": namespace,
            "name": name,
        }

    def search_similar_reports(self, trigger: TriggerContext) -> dict:
        reports = self.custom.list_namespaced_custom_object(
            group="ops.ai.yourorg",
            version="v1alpha1",
            namespace=self.report_namespace,
            plural="diagnosisreports",
        )
        items = []
        for item in reports.get("items", []):
            spec = item.get("spec", {})
            if (
                spec.get("namespace") == trigger.workload.namespace
                and spec.get("workloadRef", {}).get("name") == trigger.workload.name
                and spec.get("symptom") == trigger.symptom
            ):
                items.append(item)
        return {"items": items}

    def get_related_reports(self, namespace: str, kind: str, name: str) -> dict:
        reports = self.custom.list_namespaced_custom_object(
            group="ops.ai.yourorg",
            version="v1alpha1",
            namespace=self.report_namespace,
            plural="diagnosisreports",
        )
        items = []
        for item in reports.get("items", []):
            spec = item.get("spec", {})
            workload = spec.get("workloadRef", {})
            if (
                spec.get("namespace") == namespace
                and workload.get("kind") == kind
                and workload.get("name") == name
            ):
                items.append(item)
        return {"items": items[:10]}

    def list_reports(self) -> list[dict]:
        reports = self.custom.list_namespaced_custom_object(
            group="ops.ai.yourorg",
            version="v1alpha1",
            namespace=self.report_namespace,
            plural="diagnosisreports",
        )
        return reports.get("items", [])

    def get_report(self, name: str) -> dict | None:
        try:
            return self.custom.get_namespaced_custom_object(
                group="ops.ai.yourorg",
                version="v1alpha1",
                namespace=self.report_namespace,
                plural="diagnosisreports",
                name=name,
            )
        except Exception:
            return None

    def watch_events(self):
        if Watch is None:
            raise RuntimeError("kubernetes watch dependency is required")
        watcher = Watch()
        return watcher.stream(self.core.list_event_for_all_namespaces, timeout_seconds=0)
