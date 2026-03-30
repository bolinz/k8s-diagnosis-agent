from __future__ import annotations

import json
from datetime import datetime, timezone

from agent.analyzers.rules import RuleEngine
from agent.config.settings import Settings
from agent.metrics import reset_metrics_for_tests, snapshot_metrics
from agent.models import DiagnosisResult, TriggerContext, WorkloadRef
from agent.orchestrator.codex_agent import CodexDiagnosisAgent
from agent.reporting.diagnosis_reporter import DiagnosisReportFormatter
from agent.service import AgentService, build_model_client, build_runtime_service
from agent.tools.registry import ToolRegistry
from agent.triggers.event_watcher import map_event_to_trigger


class FakeResponsesClient:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.api_key = "test-key"

    def create_response(self, payload):
        return self.outputs.pop(0)


class FakeKubernetesClient:
    def __init__(self):
        self.calls = []

    def list_anomaly_snapshot(self):
        return [
            {
                "namespace": "payments",
                "name": "checkout-abc",
                "kind": "Pod",
                "symptom": "CrashLoopBackOff",
                "observed_for_seconds": 1800,
            }
        ]

    def get_workload_status(self, namespace, kind, name):
        self.calls.append(("get_workload_status", namespace, kind, name))
        return {"namespace": namespace, "kind": kind, "name": name, "phase": "Running"}

    def get_workload_events(self, namespace, kind, name):
        self.calls.append(("get_workload_events", namespace, kind, name))
        return {"items": [{"reason": "ProgressDeadlineExceeded", "message": "Deployment exceeded its progress deadline"}]}

    def get_owner_chain(self, namespace, kind, name):
        self.calls.append(("get_owner_chain", namespace, kind, name))
        if kind == "Pod":
            return {
                "items": [
                    {"kind": "Pod", "namespace": namespace, "name": name},
                    {"kind": "ReplicaSet", "namespace": namespace, "name": "checkout-rs"},
                    {"kind": "Deployment", "namespace": namespace, "name": "checkout"},
                ]
            }
        return {"items": [{"kind": kind, "namespace": namespace, "name": name}]}

    def get_related_events(self, namespace, kind, name):
        self.calls.append(("get_related_events", namespace, kind, name))
        return {
            "items": [
                {
                    "reason": "BackOff" if kind == "Pod" else "ProgressDeadlineExceeded",
                    "message": "Recent warning event",
                    "timestamp": "2026-03-22T05:00:00+00:00",
                }
            ]
        }

    def get_namespace_events(self, namespace):
        self.calls.append(("get_namespace_events", namespace))
        if namespace == "missing":
            return {"error": "namespace not found", "resource": "namespace_events", "namespace": namespace}
        if namespace == "forbidden":
            return {"error": "forbidden", "resource": "namespace_events", "namespace": namespace}
        return {
            "items": [
                {
                    "reason": "BackOff",
                    "message": "Back-off restarting failed container",
                    "type": "Warning",
                    "namespace": namespace,
                    "timestamp": "2026-03-22T05:00:00+00:00",
                }
            ],
            "window": "recent",
            "count": 1,
        }

    def list_related_pods(self, namespace, kind, name):
        self.calls.append(("list_related_pods", namespace, kind, name))
        return {"items": [{"metadata": {"name": "checkout-abc"}}, {"metadata": {"name": "checkout-def"}}]}

    def get_attached_pvcs(self, namespace, pod_name):
        self.calls.append(("get_attached_pvcs", namespace, pod_name))
        return {
            "items": [
                {
                    "name": "checkout-pvc",
                    "phase": "Pending",
                    "volumeName": "",
                }
            ]
        }

    def get_pvc_dependents(self, namespace, pvc_name):
        self.calls.append(("get_pvc_dependents", namespace, pvc_name))
        return {
            "items": [
                {"kind": "Pod", "namespace": namespace, "name": "checkout-abc", "phase": "Pending"},
                {"kind": "Pod", "namespace": namespace, "name": "checkout-def", "phase": "Pending"},
            ]
        }

    def get_pod_events(self, namespace, pod_name):
        self.calls.append(("get_pod_events", namespace, pod_name))
        return {"items": [{"reason": "BackOff", "message": "Back-off restarting failed container"}]}

    def get_pod_spec_summary(self, namespace, pod_name):
        self.calls.append(("get_pod_spec_summary", namespace, pod_name))
        return {
            "containers": [
                {
                    "name": "checkout",
                    "image": "example/checkout:v1",
                    "command": ["/app/server"],
                    "args": ["--port=8080"],
                    "envFrom": [{"secretRef": {"name": "checkout-env"}}],
                }
            ],
            "volumes": [
                {"name": "config", "configMap": {"name": "checkout-config"}},
                {"name": "data", "persistentVolumeClaim": {"claimName": "checkout-pvc"}},
            ],
            "serviceAccountName": "default",
            "nodeName": "node-a",
        }

    def get_pod_conditions(self, namespace, pod_name):
        self.calls.append(("get_pod_conditions", namespace, pod_name))
        return {
            "phase": "Pending",
            "reason": "Unschedulable",
            "message": "0/3 nodes are available",
            "conditions": [{"type": "PodScheduled", "status": "False", "reason": "Unschedulable"}],
        }

    def get_container_statuses(self, namespace, pod_name):
        self.calls.append(("get_container_statuses", namespace, pod_name))
        return {"items": [{"restart_count": 12, "state": {"waiting": {"reason": "CrashLoopBackOff"}}}]}

    def get_deployment_status(self, namespace, name):
        self.calls.append(("get_deployment_status", namespace, name))
        return {
            "replicas": 3,
            "updatedReplicas": 1,
            "availableReplicas": 1,
            "unavailableReplicas": 2,
            "conditions": [{"type": "Progressing", "reason": "ProgressDeadlineExceeded"}],
        }

    def get_replicaset_status(self, namespace, name):
        self.calls.append(("get_replicaset_status", namespace, name))
        return {
            "replicas": 3,
            "readyReplicas": 1,
            "availableReplicas": 1,
            "conditions": [{"type": "ReplicaFailure", "reason": "FailedCreate"}],
            "ownerReferences": [{"kind": "Deployment", "name": "checkout"}],
        }

    def get_config_refs(self, namespace, kind, name):
        self.calls.append(("get_config_refs", namespace, kind, name))
        return {
            "items": [
                {"type": "Secret", "name": "checkout-env", "namespace": namespace, "optional": False},
                {"type": "ConfigMap", "name": "checkout-config", "namespace": namespace, "optional": False},
            ]
        }

    def get_pvc_status(self, namespace, pvc_name=None, pod_name=None):
        self.calls.append(("get_pvc_status", namespace, pvc_name, pod_name))
        return {
            "items": [
                {
                    "name": pvc_name or "checkout-pvc",
                    "phase": "Pending",
                    "storageClassName": "fast",
                    "capacity": {"storage": "10Gi"},
                    "volumeName": "",
                }
            ]
        }

    def get_recent_logs(self, namespace, pod_name, container=None):
        self.calls.append(("get_recent_logs", namespace, pod_name, container))
        return {"logs": "panic: missing DATABASE_URL"}

    def get_node_conditions(self, node_name=None):
        return {"items": []}

    def get_node_events(self, node_name):
        self.calls.append(("get_node_events", node_name))
        if node_name == "missing-node":
            return {"error": "node not found", "resource": "node_events", "node_name": node_name}
        if node_name == "forbidden-node":
            return {"error": "forbidden", "resource": "node_events", "node_name": node_name}
        return {
            "items": [
                {
                    "reason": "NodeNotReady",
                    "message": "Node is not ready",
                    "type": "Warning",
                    "namespace": "default",
                    "timestamp": "2026-03-22T05:00:00+00:00",
                    "involvedObject": {"kind": "Node", "name": node_name},
                }
            ],
            "window": "recent",
            "count": 1,
        }

    def get_node_workload_impact(self, node_name):
        self.calls.append(("get_node_workload_impact", node_name))
        return {
            "node": node_name,
            "podCount": 2,
            "crossNamespace": False,
            "items": [
                {"kind": "Pod", "namespace": "payments", "name": "checkout-abc", "phase": "Pending"},
                {"kind": "Pod", "namespace": "payments", "name": "checkout-def", "phase": "Pending"},
            ],
        }

    def get_namespace_quotas(self, namespace):
        return {"items": []}

    def get_hpa_status(self, namespace, target_name=None):
        return {"items": []}

    def get_resource_metrics(self, namespace, name=None):
        return {"status": "unavailable"}

    def search_similar_reports(self, trigger):
        return {"items": []}

    def get_related_reports(self, namespace, kind, name):
        self.calls.append(("get_related_reports", namespace, kind, name))
        return {"items": []}

    def list_reports(self):
        return [
            {
                "metadata": {"name": "diagnosis-a"},
                "spec": {
                    "source": "event",
                    "cluster": "prod",
                    "namespace": "payments",
                    "symptom": "CrashLoopBackOff",
                    "observedFor": 0,
                    "triggerAt": "2026-03-22T04:59:00+00:00",
                    "workloadRef": {"kind": "Pod", "name": "checkout-abc"},
                },
                "status": {
                    "severity": "critical",
                    "summary": "Checkout pod crashes because DATABASE_URL is missing.",
                    "probableCauses": ["Required database environment variable is absent."],
                    "evidence": ["Container logs contain missing DATABASE_URL."],
                    "recommendations": ["Restore secret wiring."],
                    "confidence": 0.93,
                    "relatedObjects": [
                        {"kind": "Pod", "namespace": "payments", "name": "checkout-abc", "role": "primary"},
                        {"kind": "Deployment", "namespace": "payments", "name": "checkout", "role": "owner"},
                    ],
                    "rootCauseCandidates": [
                        {
                            "objectRef": {"kind": "Deployment", "namespace": "payments", "name": "checkout"},
                            "reason": "Shared deployment rollout is broken.",
                            "confidence": 0.82,
                        }
                    ],
                    "evidenceTimeline": [
                        {
                            "time": "2026-03-22T05:00:00+00:00",
                            "objectRef": {"kind": "Pod", "namespace": "payments", "name": "checkout-abc"},
                            "signal": "BackOff",
                        }
                    ],
                    "impactSummary": {
                        "workloadCount": 1,
                        "podCount": 1,
                        "crossNamespace": False,
                        "relatedReportCount": 0,
                    },
                    "analysisVersion": "0.3.0",
                    "modelInfo": {"name": "gpt-5-codex", "fallback": False, "traceId": "trace-a"},
                    "diagnosisTrace": {
                        "traceId": "trace-a",
                        "toolCallsUsed": 2,
                        "scopeGuardHits": 0,
                        "fallbackReason": "",
                    },
                    "rawSignal": {
                        "reason": "BackOff",
                        "message": "restart",
                        "timestamp": "2026-03-22T05:00:00+00:00",
                        "podPhase": "Running",
                        "containerReason": "CrashLoopBackOff",
                    },
                    "lastAnalyzedAt": "2026-03-22T05:00:00+00:00",
                },
            }
        ]

    def get_report(self, name):
        if name == "diagnosis-a":
            return self.list_reports()[0]
        return None

    def watch_events(self):
        return iter([])


def build_settings() -> Settings:
    return Settings(
        model_provider="openai",
        openai_api_key="key",
        openai_model="gpt-5-codex",
        ollama_model="llama3.1",
        cluster_name="prod",
        report_namespace="k8s-diagnosis-system",
        scan_interval_seconds=300,
        min_observation_seconds=600,
        webhook_port=8080,
        max_tool_calls=8,
        max_diagnosis_seconds=45,
        max_input_bytes=20000,
        request_timeout_seconds=30,
        api_base_url="https://api.openai.com/v1",
        ollama_base_url="http://127.0.0.1:11434",
        diagnosis_name_prefix="diagnosis",
        event_dedupe_window_seconds=300,
        event_storm_threshold=5,
        scope_mode="strict",
        scope_allowed_namespaces=(),
        workload_name="k8s-diagnosis-agent",
        log_level="INFO",
    )


def build_fallback_agent() -> CodexDiagnosisAgent:
    client = FakeResponsesClient([])
    client.api_key = ""
    return CodexDiagnosisAgent(
        responses_client=client,
        rule_engine=RuleEngine(cluster_name="prod", min_observation_seconds=600),
        model="gpt-5-codex",
        max_tool_calls=8,
        max_input_bytes=20000,
    )


def test_build_model_client_selects_openai_and_ollama():
    openai_client = build_model_client(build_settings())
    assert openai_client.provider_name == "openai"

    settings = build_settings()
    settings.model_provider = "ollama"
    ollama_client = build_model_client(settings)
    assert ollama_client.provider_name == "ollama"
    assert ollama_client.model == "llama3.1"


def test_settings_scope_mode_and_allowlist_from_env(monkeypatch):
    monkeypatch.setenv("K8S_DIAGNOSIS_SCOPE_MODE", "relaxed")
    monkeypatch.setenv("K8S_DIAGNOSIS_SCOPE_ALLOWLIST", "kube-system,monitoring")
    monkeypatch.setenv("K8S_DIAGNOSIS_EVENT_STORM_THRESHOLD", "7")
    settings = Settings.from_env()
    assert settings.scope_mode == "relaxed"
    assert settings.scope_allowed_namespaces == ("kube-system", "monitoring")
    assert settings.event_storm_threshold == 7


def test_settings_invalid_scope_mode_falls_back_to_strict(monkeypatch):
    monkeypatch.setenv("K8S_DIAGNOSIS_SCOPE_MODE", "invalid-mode")
    settings = Settings.from_env()
    assert settings.scope_mode == "strict"


def test_metrics_increment_for_diagnosis_and_tool_calls():
    reset_metrics_for_tests()
    client = FakeKubernetesClient()
    settings = build_settings()
    engine = RuleEngine(cluster_name="prod", min_observation_seconds=600)
    response = {
        "output": [
            {
                "type": "function_call",
                "name": "get_recent_logs",
                "arguments": "{\"namespace\":\"payments\",\"pod_name\":\"checkout-abc\"}",
                "call_id": "call_1",
            }
        ]
    }
    final = {
        "output_text": json.dumps(
            {
                "summary": "Pod restarts because DATABASE_URL is missing.",
                "severity": "critical",
                "probableCauses": ["Missing DATABASE_URL."],
                "evidence": ["Logs show missing DATABASE_URL."],
                "recommendations": ["Restore secret wiring."],
                "confidence": 0.9,
            }
        )
    }
    agent = CodexDiagnosisAgent(
        responses_client=FakeResponsesClient([response, final]),
        rule_engine=engine,
        model="gpt-5-codex",
        max_tool_calls=8,
        max_input_bytes=20000,
    )
    service = AgentService(settings=settings, client=client, codex_agent=agent)
    service.process_alert(
        {
            "namespace": "payments",
            "name": "checkout-abc",
            "kind": "Pod",
            "symptom": "CrashLoopBackOff",
            "observed_for_seconds": 600,
        }
    )
    metrics = snapshot_metrics()
    assert metrics["diagnosis_requests_total"] >= 1
    assert metrics["tool_calls_total"] >= 1
    assert metrics["diagnosis_duration_seconds_count"] >= 1


def test_metrics_increment_for_fallback_diagnosis():
    reset_metrics_for_tests()
    client = FakeKubernetesClient()
    settings = build_settings()
    service = AgentService(
        settings=settings,
        client=client,
        codex_agent=build_fallback_agent(),
    )
    service.process_alert(
        {
            "namespace": "payments",
            "name": "checkout-abc",
            "kind": "Pod",
            "symptom": "CrashLoopBackOff",
            "observed_for_seconds": 600,
        }
    )
    metrics = snapshot_metrics()
    assert metrics["diagnosis_requests_total"] >= 1
    assert metrics["diagnosis_fallback_total"] >= 1


def test_process_alert_supports_workload_ref_payload_shape():
    service = AgentService(
        settings=build_settings(),
        client=FakeKubernetesClient(),
        codex_agent=build_fallback_agent(),
    )

    report = service.process_alert(
        {
            "source": "manual-test",
            "cluster": "test-cluster",
            "namespace": "kube-system",
            "workloadRef": {"kind": "Pod", "name": "coredns-6b4c956686-285zk"},
            "symptom": "ImagePullBackOff",
            "observedFor": "180s",
            "triggerAt": "2026-03-27T22:10:00+08:00",
        }
    )

    assert report["spec"]["cluster"] == "test-cluster"
    assert report["spec"]["namespace"] == "kube-system"
    assert report["spec"]["workloadRef"]["kind"] == "Pod"
    assert report["spec"]["workloadRef"]["name"] == "coredns-6b4c956686-285zk"
    assert report["spec"]["observedFor"] == 180
    assert report["spec"]["triggerAt"] == "2026-03-27T14:10:00+00:00"


def test_process_alert_observed_for_seconds_accepts_string_suffix():
    service = AgentService(
        settings=build_settings(),
        client=FakeKubernetesClient(),
        codex_agent=build_fallback_agent(),
    )

    report = service.process_alert(
        {
            "namespace": "payments",
            "name": "checkout-abc",
            "kind": "Pod",
            "symptom": "Pending",
            "observed_for_seconds": "95s",
        }
    )

    assert report["spec"]["observedFor"] == 95


def test_build_model_client_rejects_invalid_provider():
    settings = build_settings()
    settings.model_provider = "invalid"
    try:
        build_model_client(settings)
    except ValueError as exc:
        assert "Unsupported MODEL_PROVIDER" in str(exc)
    else:
        raise AssertionError("expected invalid provider to raise")


def test_build_runtime_service_uses_selected_provider_model(monkeypatch):
    settings = build_settings()
    settings.model_provider = "ollama"
    settings.ollama_model = "qwen3:8b"

    class FakeRuntimeClient:
        def __init__(self, report_namespace):
            self.report_namespace = report_namespace

    class FakeReportWriter:
        def __init__(self, report_namespace, formatter):
            self.report_namespace = report_namespace
            self.formatter = formatter

    monkeypatch.setattr(
        "agent.k8s_client.runtime.RuntimeKubernetesClient",
        FakeRuntimeClient,
    )
    monkeypatch.setattr(
        "agent.service.KubernetesDiagnosisReportWriter",
        FakeReportWriter,
    )

    service = build_runtime_service(settings)

    assert service.codex_agent.model == "qwen3:8b"
    assert service.codex_agent.responses_client.provider_name == "ollama"


def test_tool_registry_executes_registered_tools():
    client = FakeKubernetesClient()
    trigger = TriggerContext(
        source="scheduled",
        cluster="prod",
        workload=WorkloadRef(kind="Pod", namespace="payments", name="checkout-abc"),
        symptom="CrashLoopBackOff",
        observed_for_seconds=1800,
    )
    registry = ToolRegistry(client, trigger)
    output = registry.execute(
        "get_recent_logs",
        {"namespace": "payments", "pod_name": "checkout-abc"},
    )
    payload = json.loads(output)
    assert "DATABASE_URL" in payload["logs"]


def test_tool_registry_exposes_new_read_only_tools_without_secret_values():
    client = FakeKubernetesClient()
    trigger = TriggerContext(
        source="scheduled",
        cluster="prod",
        workload=WorkloadRef(kind="Pod", namespace="payments", name="checkout-abc"),
        symptom="CreateContainerConfigError",
        observed_for_seconds=1800,
    )
    registry = ToolRegistry(client, trigger)
    config_refs = json.loads(
        registry.execute(
            "get_config_refs",
            {"namespace": "payments", "kind": "Pod", "name": "checkout-abc"},
        )
    )
    pvc_status = json.loads(
        registry.execute(
            "get_pvc_status",
            {"namespace": "payments", "pod_name": "checkout-abc"},
        )
    )
    owner_chain = json.loads(
        registry.execute(
            "get_owner_chain",
            {"namespace": "payments", "kind": "Pod", "name": "checkout-abc"},
        )
    )
    node_impact = json.loads(
        registry.execute(
            "get_node_workload_impact",
            {"node_name": "node-a"},
        )
    )
    assert config_refs["items"][0]["name"] == "checkout-env"
    assert "value" not in json.dumps(config_refs)
    assert pvc_status["items"][0]["phase"] == "Pending"
    assert owner_chain["items"][-1]["kind"] == "Deployment"
    assert node_impact["podCount"] == 2


def test_tool_registry_get_namespace_events_success_not_found_forbidden():
    client = FakeKubernetesClient()
    trigger = TriggerContext(
        source="scheduled",
        cluster="prod",
        workload=WorkloadRef(kind="Pod", namespace="payments", name="checkout-abc"),
        symptom="Pending",
        observed_for_seconds=1800,
    )
    registry = ToolRegistry(client, trigger)

    ok_payload = json.loads(registry.execute("get_namespace_events", {"namespace": "payments"}))
    not_found_payload = json.loads(registry.execute("get_namespace_events", {"namespace": "missing"}))
    forbidden_payload = json.loads(registry.execute("get_namespace_events", {"namespace": "forbidden"}))

    assert ok_payload["count"] == 1
    assert ok_payload["items"][0]["reason"] == "BackOff"
    assert not_found_payload["resource"] == "tool_guard"
    assert not_found_payload["allowedNamespace"] == "payments"
    assert forbidden_payload["resource"] == "tool_guard"
    assert forbidden_payload["allowedNamespace"] == "payments"


def test_tool_registry_blocks_cross_namespace_access():
    client = FakeKubernetesClient()
    trigger = TriggerContext(
        source="scheduled",
        cluster="prod",
        workload=WorkloadRef(kind="Pod", namespace="payments", name="checkout-abc"),
        symptom="Pending",
        observed_for_seconds=1800,
    )
    registry = ToolRegistry(client, trigger)
    payload = json.loads(
        registry.execute(
            "get_pod_events",
            {"namespace": "kube-system", "pod_name": "coredns-abc"},
        )
    )
    assert payload["resource"] == "tool_guard"
    assert payload["tool"] == "get_pod_events"
    assert payload["allowedNamespace"] == "payments"
    assert payload["scopeMode"] == "strict"


def test_tool_registry_relaxed_scope_allows_explicit_allowlist_namespace():
    client = FakeKubernetesClient()
    trigger = TriggerContext(
        source="scheduled",
        cluster="prod",
        workload=WorkloadRef(kind="Pod", namespace="payments", name="checkout-abc"),
        symptom="Pending",
        observed_for_seconds=1800,
    )
    registry = ToolRegistry(
        client,
        trigger,
        scope_mode="relaxed",
        allowed_namespaces={"kube-system"},
    )
    payload = json.loads(
        registry.execute(
            "get_pod_events",
            {"namespace": "kube-system", "pod_name": "coredns-abc"},
        )
    )
    assert "error" not in payload
    assert payload["items"][0]["reason"] == "BackOff"


def test_tool_registry_relaxed_scope_blocks_non_allowlisted_namespace():
    client = FakeKubernetesClient()
    trigger = TriggerContext(
        source="scheduled",
        cluster="prod",
        workload=WorkloadRef(kind="Pod", namespace="payments", name="checkout-abc"),
        symptom="Pending",
        observed_for_seconds=1800,
    )
    registry = ToolRegistry(
        client,
        trigger,
        scope_mode="relaxed",
        allowed_namespaces={"kube-system"},
    )
    payload = json.loads(
        registry.execute(
            "get_pod_events",
            {"namespace": "default", "pod_name": "nginx-abc"},
        )
    )
    assert payload["resource"] == "tool_guard"
    assert payload["scopeMode"] == "relaxed"
    assert payload["allowedNamespaces"] == ["kube-system", "payments"]


def test_tool_registry_get_node_events_success_not_found_forbidden():
    client = FakeKubernetesClient()
    trigger = TriggerContext(
        source="scheduled",
        cluster="prod",
        workload=WorkloadRef(kind="Pod", namespace="payments", name="checkout-abc"),
        symptom="NodeNotReadyImpact",
        observed_for_seconds=1800,
    )
    registry = ToolRegistry(client, trigger)

    ok_payload = json.loads(registry.execute("get_node_events", {"node_name": "node-a"}))
    not_found_payload = json.loads(registry.execute("get_node_events", {"node_name": "missing-node"}))
    forbidden_payload = json.loads(registry.execute("get_node_events", {"node_name": "forbidden-node"}))

    assert ok_payload["count"] == 1
    assert ok_payload["items"][0]["reason"] == "NodeNotReady"
    assert ok_payload["items"][0]["involvedObject"]["kind"] == "Node"
    assert not_found_payload["resource"] == "node_events"
    assert "not found" in not_found_payload["error"]
    assert forbidden_payload["resource"] == "node_events"
    assert forbidden_payload["error"] == "forbidden"


def test_tool_registry_serializes_datetime_values():
    client = FakeKubernetesClient()
    client.get_workload_status = lambda namespace, kind, name: {
        "namespace": namespace,
        "kind": kind,
        "name": name,
        "timestamp": datetime(2026, 3, 23, 12, 0, tzinfo=timezone.utc),
    }
    trigger = TriggerContext(
        source="scheduled",
        cluster="prod",
        workload=WorkloadRef(kind="Pod", namespace="payments", name="checkout-abc"),
        symptom="CrashLoopBackOff",
        observed_for_seconds=1800,
    )
    registry = ToolRegistry(client, trigger)
    payload = json.loads(
        registry.execute(
            "get_workload_status",
            {"namespace": "payments", "kind": "Pod", "name": "checkout-abc"},
        )
    )

    assert payload["timestamp"] == "2026-03-23T12:00:00+00:00"


def test_codex_agent_handles_function_call_then_structured_result():
    response_one = {
        "output": [
            {
                "type": "function_call",
                "name": "get_recent_logs",
                "call_id": "call_1",
                "arguments": "{\"namespace\":\"payments\",\"pod_name\":\"checkout-abc\"}",
            }
        ]
    }
    response_two = {
        "output_text": json.dumps(
            {
                "summary": "Checkout pod crashes because DATABASE_URL is missing.",
                "severity": "critical",
                "probableCauses": ["Required database environment variable is absent."],
                "evidence": ["Container logs contain 'missing DATABASE_URL'."],
                "recommendations": ["Restore the secret or env wiring for DATABASE_URL."],
                "confidence": 0.93,
            }
        )
    }
    client = FakeKubernetesClient()
    trigger = TriggerContext(
        source="scheduled",
        cluster="prod",
        workload=WorkloadRef(kind="Pod", namespace="payments", name="checkout-abc"),
        symptom="CrashLoopBackOff",
        observed_for_seconds=1800,
    )
    engine = RuleEngine(cluster_name="prod", min_observation_seconds=600)
    agent = CodexDiagnosisAgent(
        responses_client=FakeResponsesClient([response_one, response_two]),
        rule_engine=engine,
        model="gpt-5-codex",
        max_tool_calls=8,
        max_input_bytes=20000,
    )
    registry = ToolRegistry(client, trigger)
    diagnosis = agent.diagnose(trigger, registry)
    assert diagnosis.summary.startswith("Checkout pod crashes")
    assert diagnosis.confidence == 0.93
    assert client.calls[0][0] == "get_recent_logs"
    assert diagnosis.raw_agent_output.get("trace", {}).get("toolCallsUsed", 0) >= 1
    assert diagnosis.raw_agent_output.get("trace", {}).get("traceId")


def test_codex_agent_normalizes_string_fields_and_severity():
    client = FakeKubernetesClient()
    trigger = TriggerContext(
        source="scheduled",
        cluster="prod",
        workload=WorkloadRef(kind="Pod", namespace="payments", name="checkout-abc"),
        symptom="ImagePullBackOff",
        observed_for_seconds=1800,
    )
    engine = RuleEngine(cluster_name="prod", min_observation_seconds=600)
    agent = CodexDiagnosisAgent(
        responses_client=FakeResponsesClient(
            [
                {
                    "output_text": json.dumps(
                        {
                            "summary": "Image pull failed due to missing manifest.",
                            "severity": "High",
                            "probableCauses": "Image tag does not exist in registry.",
                            "evidence": "failed to pull image: manifest unknown",
                            "recommendations": "Verify image tag and digest.",
                            "confidence": 0.8,
                        }
                    )
                }
            ]
        ),
        rule_engine=engine,
        model="gpt-5-codex",
        max_tool_calls=8,
        max_input_bytes=20000,
    )
    diagnosis = agent.diagnose(trigger, ToolRegistry(client, trigger))
    assert diagnosis.severity == "critical"
    assert diagnosis.probable_causes == ["Image tag does not exist in registry."]
    assert diagnosis.evidence == ["failed to pull image: manifest unknown"]
    assert diagnosis.recommendations == ["Verify image tag and digest."]


def test_codex_agent_falls_back_when_model_output_is_invalid():
    client = FakeKubernetesClient()
    trigger = TriggerContext(
        source="scheduled",
        cluster="prod",
        workload=WorkloadRef(kind="Pod", namespace="payments", name="checkout-abc"),
        symptom="ImagePullBackOff",
        observed_for_seconds=1800,
    )
    engine = RuleEngine(cluster_name="prod", min_observation_seconds=600)
    agent = CodexDiagnosisAgent(
        responses_client=FakeResponsesClient([{"output_text": "not-json"}]),
        rule_engine=engine,
        model="gpt-5-codex",
        max_tool_calls=8,
        max_input_bytes=20000,
    )
    diagnosis = agent.diagnose(trigger, ToolRegistry(client, trigger))
    assert diagnosis.used_fallback is True
    assert diagnosis.severity == "critical"
    assert diagnosis.raw_agent_output.get("trace", {}).get("fallbackReason") == "invalid_model_json"


def test_codex_agent_falls_back_when_diagnosis_time_budget_exceeded():
    client = FakeKubernetesClient()
    trigger = TriggerContext(
        source="scheduled",
        cluster="prod",
        workload=WorkloadRef(kind="Pod", namespace="payments", name="checkout-abc"),
        symptom="ImagePullBackOff",
        observed_for_seconds=1800,
    )
    engine = RuleEngine(cluster_name="prod", min_observation_seconds=600)
    agent = CodexDiagnosisAgent(
        responses_client=FakeResponsesClient(
            [
                {
                    "output_text": json.dumps(
                        {
                            "summary": "will never be used",
                            "severity": "critical",
                            "probableCauses": ["x"],
                            "evidence": ["y"],
                            "recommendations": ["z"],
                            "confidence": 0.8,
                        }
                    )
                }
            ]
        ),
        rule_engine=engine,
        model="gpt-5-codex",
        max_tool_calls=8,
        max_input_bytes=20000,
        max_diagnosis_seconds=0,
    )
    diagnosis = agent.diagnose(trigger, ToolRegistry(client, trigger))
    assert diagnosis.used_fallback is True
    assert diagnosis.severity == "critical"


def test_codex_agent_prompt_contains_preferred_tool_order_for_symptom():
    agent = CodexDiagnosisAgent(
        responses_client=FakeResponsesClient([]),
        rule_engine=RuleEngine(cluster_name="prod", min_observation_seconds=600),
        model="gpt-5-codex",
        max_tool_calls=8,
        max_input_bytes=20000,
    )
    prompt = agent._build_user_prompt(
        TriggerContext(
            source="scheduled",
            cluster="prod",
            workload=WorkloadRef(kind="Deployment", namespace="payments", name="checkout"),
            symptom="ProgressDeadlineExceeded",
            observed_for_seconds=1800,
        )
    )
    assert "Preferred tool order" in prompt
    assert "get_deployment_status" in prompt
    assert "get_workload_events" in prompt


def test_agent_service_scan_once_emits_structured_report_body():
    client = FakeKubernetesClient()
    response = {
        "output_text": json.dumps(
            {
                "summary": "Pod restarts are caused by repeated probe failures.",
                "severity": "warning",
                "probableCauses": ["The readiness endpoint returns failures during startup."],
                "evidence": ["Recent events show repeated BackOff warnings."],
                "recommendations": ["Increase probe initialDelaySeconds and verify the endpoint."],
                "confidence": 0.74,
            }
        )
    }
    settings = build_settings()
    engine = RuleEngine(cluster_name="prod", min_observation_seconds=600)
    agent = CodexDiagnosisAgent(
        responses_client=FakeResponsesClient([response]),
        rule_engine=engine,
        model="gpt-5-codex",
        max_tool_calls=8,
        max_input_bytes=20000,
    )
    service = AgentService(settings=settings, client=client, codex_agent=agent)
    reports = service.scan_once()
    assert len(reports) == 1
    report = reports[0]
    assert report["kind"] == "DiagnosisReport"
    assert report["status"]["summary"].startswith("Pod restarts")
    assert report["spec"]["symptom"] == "CrashLoopBackOff"


def test_formatter_dedupes_report_name_and_targets_report_namespace_shape():
    formatter = DiagnosisReportFormatter()
    trigger = TriggerContext(
        source="alert",
        cluster="prod",
        workload=WorkloadRef(kind="Deployment", namespace="payments", name="checkout"),
        symptom="Pending",
        observed_for_seconds=1900,
    )
    name = formatter.dedupe_name(trigger, "diagnosis")
    body = formatter.build_spec(
        name,
        "k8s-diagnosis-system",
        trigger,
    )
    status = formatter.build_status(
        engine_result(),
        "gpt-5-codex",
        raw_signal={"reason": "BackOff"},
        category="runtime",
        primary_signal="BackOff",
    )
    assert body["metadata"]["name"].startswith("diagnosis-")
    assert body["metadata"]["namespace"] == "k8s-diagnosis-system"
    assert body["spec"]["triggerAt"].endswith("+00:00")
    assert status["modelInfo"]["name"] == "gpt-5-codex"
    assert status["rawSignal"]["reason"] == "BackOff"
    assert status["category"] == "runtime"
    assert status["primarySignal"] == "BackOff"
    assert status["qualityScore"] == {}
    assert status["uncertainties"] == []
    assert status["evidenceAttribution"] == []


def test_formatter_writes_trace_into_status_model_info():
    formatter = DiagnosisReportFormatter()
    diagnosis = engine_result()
    diagnosis.raw_agent_output = {
        "trace": {
            "traceId": "trace-123",
            "toolCallsUsed": 2,
            "scopeGuardHits": 1,
            "fallbackReason": "",
        }
    }
    status = formatter.build_status(diagnosis, "gpt-5-codex")
    assert status["modelInfo"]["traceId"] == "trace-123"
    assert status["diagnosisTrace"]["toolCallsUsed"] == 2


def test_service_lists_and_reads_reports():
    client = FakeKubernetesClient()
    settings = build_settings()
    engine = RuleEngine(cluster_name="prod", min_observation_seconds=600)
    agent = CodexDiagnosisAgent(
        responses_client=FakeResponsesClient([]),
        rule_engine=engine,
        model="gpt-5-codex",
        max_tool_calls=8,
        max_input_bytes=20000,
    )
    service = AgentService(settings=settings, client=client, codex_agent=agent)
    reports = service.list_reports()
    assert reports[0]["name"] == "diagnosis-a"
    assert reports[0]["severity"] == "critical"
    assert reports[0]["category"] == "runtime"
    assert reports[0]["primarySignal"] == "BackOff"
    report = service.get_report("diagnosis-a")
    assert report["workload"]["name"] == "checkout-abc"
    assert report["cluster"] == "prod"
    assert report["triggerAt"]
    assert report["analysisVersion"] == "0.3.0"
    assert report["modelInfo"]["traceId"] == "trace-a"
    assert report["diagnosisTrace"]["traceId"] == "trace-a"
    assert report["rawSignal"]["podPhase"] == "Running"
    assert report["relatedObjects"][0]["role"] == "primary"
    assert report["rootCauseCandidates"][0]["objectRef"]["kind"] == "Deployment"
    assert report["category"] == "runtime"
    assert report["primarySignal"] == "BackOff"
    assert report["qualityScore"] == {}
    assert report["uncertainties"] == []
    assert report["evidenceAttribution"] == []
    assert service.get_report("missing") is None


def test_service_list_reports_supports_category_filter():
    client = FakeKubernetesClient()
    settings = build_settings()
    engine = RuleEngine(cluster_name="prod", min_observation_seconds=600)
    agent = CodexDiagnosisAgent(
        responses_client=FakeResponsesClient([]),
        rule_engine=engine,
        model="gpt-5-codex",
        max_tool_calls=8,
        max_input_bytes=20000,
    )
    service = AgentService(settings=settings, client=client, codex_agent=agent)
    runtime_reports = service.list_reports({"category": ["runtime"]})
    image_reports = service.list_reports({"category": ["image"]})
    assert len(runtime_reports) == 1
    assert runtime_reports[0]["name"] == "diagnosis-a"
    assert image_reports == []


def test_event_mapping_and_dedupe():
    trigger = map_event_to_trigger(
        "prod",
        {
            "type": "Warning",
            "reason": "BackOff",
            "message": "Back-off restarting failed container",
            "involvedObject": {
                "kind": "Pod",
                "namespace": "payments",
                "name": "checkout-abc",
            },
            "lastTimestamp": "2026-03-22T05:00:00Z",
        },
    )
    assert trigger is not None
    assert trigger.symptom == "CrashLoopBackOff"
    assert trigger.raw_signal["involvedObject"]["name"] == "checkout-abc"
    assert trigger.raw_signal["reason"] == "BackOff"
    assert trigger.raw_signal["timestamp"] == "2026-03-22T05:00:00+00:00"

    failed_mount = map_event_to_trigger(
        "prod",
        {
            "type": "Warning",
            "reason": "FailedMount",
            "message": "Unable to attach or mount volumes",
            "involvedObject": {
                "kind": "Pod",
                "namespace": "payments",
                "name": "checkout-abc",
            },
            "lastTimestamp": "2026-03-22T05:00:00Z",
        },
    )
    assert failed_mount is not None
    assert failed_mount.symptom == "FailedMount"

    failed_scheduling = map_event_to_trigger(
        "prod",
        {
            "type": "Warning",
            "reason": "FailedScheduling",
            "message": "0/3 nodes are available: 3 Insufficient cpu.",
            "involvedObject": {
                "kind": "Pod",
                "namespace": "payments",
                "name": "checkout-abc",
            },
            "lastTimestamp": "2026-03-22T05:00:01Z",
        },
    )
    assert failed_scheduling is not None
    assert failed_scheduling.symptom == "Pending"
    assert "Insufficient cpu" in failed_scheduling.raw_signal["message"]

    failed_image_pull = map_event_to_trigger(
        "prod",
        {
            "type": "Warning",
            "reason": "Failed",
            "message": "Failed to pull image \"priv-registry/app:1.2\": unauthorized: authentication required",
            "involvedObject": {
                "kind": "Pod",
                "namespace": "payments",
                "name": "checkout-abc",
            },
            "lastTimestamp": "2026-03-22T05:00:02Z",
        },
    )
    assert failed_image_pull is not None
    assert failed_image_pull.symptom == "ErrImagePull"
    assert "Failed to pull image" in failed_image_pull.raw_signal["message"]

    cannot_run = map_event_to_trigger(
        "prod",
        {
            "type": "Warning",
            "reason": "Failed",
            "message": "container cannot run: executable file not found",
            "involvedObject": {
                "kind": "Pod",
                "namespace": "payments",
                "name": "checkout-abc",
            },
        },
    )
    assert cannot_run is not None
    assert cannot_run.symptom == "ContainerCannotRun"

    client = FakeKubernetesClient()
    settings = build_settings()
    engine = RuleEngine(cluster_name="prod", min_observation_seconds=600)
    agent = CodexDiagnosisAgent(
        responses_client=FakeResponsesClient(
            [
                {
                    "output_text": json.dumps(
                        {
                            "summary": "Pod is repeatedly restarting.",
                            "severity": "warning",
                            "probableCauses": ["Repeated crash loop detected."],
                            "evidence": ["Warning event BackOff observed."],
                            "recommendations": ["Inspect container logs."],
                            "confidence": 0.7,
                        }
                    )
                }
            ]
        ),
        rule_engine=engine,
        model="gpt-5-codex",
        max_tool_calls=8,
        max_input_bytes=20000,
    )
    service = AgentService(settings=settings, client=client, codex_agent=agent)
    first = service.process_event_trigger(trigger)
    second = service.process_event_trigger(trigger)
    assert first is not None
    assert second is None


def test_event_storm_threshold_emits_single_aggregated_report():
    trigger = TriggerContext(
        source="event",
        cluster="prod",
        workload=WorkloadRef(kind="Pod", namespace="payments", name="checkout-abc"),
        symptom="CrashLoopBackOff",
        observed_for_seconds=0,
        raw_signal={"reason": "BackOff", "message": "Back-off restarting failed container"},
    )
    client = FakeKubernetesClient()
    settings = build_settings()
    settings.event_storm_threshold = 3
    agent = CodexDiagnosisAgent(
        responses_client=FakeResponsesClient(
            [
                {
                    "output_text": json.dumps(
                        {
                            "summary": "Pod is repeatedly restarting.",
                            "severity": "warning",
                            "probableCauses": ["Repeated crash loop detected."],
                            "evidence": ["Warning event BackOff observed."],
                            "recommendations": ["Inspect container logs."],
                            "confidence": 0.7,
                        }
                    )
                }
            ]
        ),
        rule_engine=RuleEngine(cluster_name="prod", min_observation_seconds=600),
        model="gpt-5-codex",
        max_tool_calls=8,
        max_input_bytes=20000,
    )
    service = AgentService(settings=settings, client=client, codex_agent=agent)
    first = service.process_event_trigger(trigger)
    second = service.process_event_trigger(trigger)
    third = service.process_event_trigger(trigger)
    fourth = service.process_event_trigger(trigger)
    assert first is not None
    assert second is None
    assert third is not None
    assert third["status"]["rawSignal"]["aggregated"] is True
    assert third["status"]["rawSignal"]["stormCount"] == 3
    assert third["status"]["modelInfo"]["fallback"] is True
    assert third["status"]["modelInfo"]["traceId"]
    assert third["status"]["diagnosisTrace"]["fallbackReason"] == "event_storm_aggregated"
    assert fourth is None


def test_event_mapping_skips_events_without_object_name():
    trigger = map_event_to_trigger(
        "prod",
        {
            "type": "Warning",
            "reason": "BackOff",
            "message": "Back-off restarting failed container",
            "involvedObject": {
                "kind": "Pod",
                "namespace": "payments",
                "name": "",
            },
        },
    )
    assert trigger is None


def test_event_watcher_ignores_agent_self_events():
    watcher = __import__("agent.triggers.event_watcher", fromlist=["EventWatcher"]).EventWatcher(
        stream_factory=lambda: iter(()),
        cluster_name="prod",
        report_namespace="k8s-diagnosis-system",
        workload_name="k8s-diagnosis-agent",
        on_trigger=lambda _trigger: None,
    )
    assert watcher._should_ignore_event(
        {
            "involvedObject": {
                "kind": "Deployment",
                "namespace": "k8s-diagnosis-system",
                "name": "k8s-diagnosis-agent",
            }
        }
    ) is True
    assert watcher._should_ignore_event(
        {
            "involvedObject": {
                "kind": "Pod",
                "namespace": "k8s-diagnosis-system",
                "name": "k8s-diagnosis-agent-abc123",
            }
        }
    ) is True
    assert watcher._should_ignore_event(
        {
            "involvedObject": {
                "kind": "Pod",
                "namespace": "payments",
                "name": "checkout-abc",
            }
        }
    ) is False


def test_service_fills_empty_diagnosis_fields_from_fallback():
    client = FakeKubernetesClient()
    settings = build_settings()
    trigger = TriggerContext(
        source="event",
        cluster="prod",
        workload=WorkloadRef(kind="Pod", namespace="payments", name="checkout-abc"),
        symptom="CrashLoopBackOff",
        observed_for_seconds=0,
        raw_signal={"reason": "BackOff", "message": "Back-off restarting failed container", "timestamp": "2026-03-22T05:00:00+00:00"},
    )
    engine = RuleEngine(cluster_name="prod", min_observation_seconds=600)
    agent = CodexDiagnosisAgent(
        responses_client=FakeResponsesClient(
            [
                {
                    "output_text": json.dumps(
                        {
                            "summary": "",
                            "severity": "warning",
                            "probableCauses": [],
                            "evidence": [],
                            "recommendations": [],
                            "confidence": 0.0,
                        }
                    )
                }
            ]
        ),
        rule_engine=engine,
        model="gpt-5-codex",
        max_tool_calls=8,
        max_input_bytes=20000,
    )
    service = AgentService(settings=settings, client=client, codex_agent=agent)
    diagnosis = service._ensure_complete_diagnosis(trigger, agent.diagnose(trigger, ToolRegistry(client, trigger)))
    assert diagnosis.summary.startswith("Detected CrashLoopBackOff")
    assert diagnosis.evidence
    assert diagnosis.recommendations
    assert diagnosis.quality_score.get("overall", 0) > 0
    assert diagnosis.quality_score.get("usedFallback") is True
    assert diagnosis.uncertainties
    assert any("fallback" in item.lower() for item in diagnosis.uncertainties)


def test_service_quality_score_prefers_richer_evidence_and_correlation():
    service = AgentService(
        settings=build_settings(),
        client=FakeKubernetesClient(),
        codex_agent=build_fallback_agent(),
    )
    trigger = TriggerContext(
        source="event",
        cluster="prod",
        workload=WorkloadRef(kind="Pod", namespace="payments", name="checkout-abc"),
        symptom="CrashLoopBackOff",
        observed_for_seconds=900,
        raw_signal={
            "eventType": "Warning",
            "reason": "BackOff",
            "message": "Back-off restarting failed container",
            "timestamp": "2026-03-22T05:00:00+00:00",
        },
        correlation_context={
            "relatedObjects": [
                {"kind": "Pod", "namespace": "payments", "name": "checkout-abc", "role": "primary"},
                {"kind": "Deployment", "namespace": "payments", "name": "checkout", "role": "owner"},
            ]
        },
    )
    diagnosis = DiagnosisResult(
        summary="Container exits quickly after startup.",
        severity="critical",
        probable_causes=["App boot config error."],
        evidence=[
            "Container status shows CrashLoopBackOff waiting reason.",
            "Pod event reason BackOff repeats every 30s.",
            "Recent logs contain startup configuration exception.",
        ],
        recommendations=[
            "Fix app startup configuration and redeploy.",
            "Validate required env and secret references before rollout.",
        ],
        confidence=0.9,
        related_objects=[
            {"kind": "Pod", "namespace": "payments", "name": "checkout-abc", "role": "primary"},
            {"kind": "Deployment", "namespace": "payments", "name": "checkout", "role": "owner"},
        ],
        root_cause_candidates=[
            {
                "objectRef": {"kind": "Deployment", "namespace": "payments", "name": "checkout"},
                "reason": "Deployment template references invalid startup configuration.",
                "confidence": 0.86,
            }
        ],
    )
    normalized = service._ensure_complete_diagnosis(trigger, diagnosis)
    quality = normalized.quality_score
    assert quality["usedFallback"] is False
    assert quality["method"] == "rule-v1"
    assert quality["overall"] >= 0.7
    assert quality["dimensions"]["evidenceCoverage"] == 1.0
    assert quality["dimensions"]["recommendationActionability"] == 1.0
    assert quality["dimensions"]["confidenceAlignment"] >= 0.9
    assert normalized.uncertainties == []


def test_service_quality_score_penalizes_fallback_outputs():
    service = AgentService(
        settings=build_settings(),
        client=FakeKubernetesClient(),
        codex_agent=build_fallback_agent(),
    )
    trigger = TriggerContext(
        source="scheduled",
        cluster="prod",
        workload=WorkloadRef(kind="Pod", namespace="payments", name="checkout-abc"),
        symptom="Pending",
        observed_for_seconds=900,
    )
    weak = DiagnosisResult(
        summary="Diagnosis incomplete",
        severity="warning",
        probable_causes=[],
        evidence=[],
        recommendations=[],
        confidence=0.0,
    )
    normalized = service._ensure_complete_diagnosis(trigger, weak)
    quality = normalized.quality_score
    assert quality["usedFallback"] is True
    assert 0.0 <= quality["overall"] <= 0.65
    assert quality["dimensions"]["evidenceCoverage"] >= 0.3
    assert quality["dimensions"]["recommendationActionability"] >= 0.5
    assert normalized.uncertainties
    assert any("fallback" in item.lower() for item in normalized.uncertainties)


def test_service_builds_evidence_attribution_from_trace_timeline_and_event_signal():
    service = AgentService(
        settings=build_settings(),
        client=FakeKubernetesClient(),
        codex_agent=build_fallback_agent(),
    )
    trigger = TriggerContext(
        source="event",
        cluster="prod",
        workload=WorkloadRef(kind="Pod", namespace="payments", name="checkout-abc"),
        symptom="CrashLoopBackOff",
        observed_for_seconds=600,
        raw_signal={
            "reason": "BackOff",
            "message": "Back-off restarting failed container",
            "timestamp": "2026-03-22T05:00:00+00:00",
        },
    )
    diagnosis = DiagnosisResult(
        summary="Container keeps restarting.",
        severity="critical",
        probable_causes=["Startup configuration missing."],
        evidence=["BackOff events observed repeatedly.", "Recent logs show config exception."],
        recommendations=["Restore missing config and redeploy.", "Verify startup env references."],
        confidence=0.82,
        evidence_timeline=[
            {
                "time": "2026-03-22T05:00:00+00:00",
                "objectRef": {"kind": "Pod", "namespace": "payments", "name": "checkout-abc"},
                "signal": "BackOff",
            }
        ],
        raw_agent_output={
            "trace": {
                "traceId": "trace-abc",
                "toolSequence": [
                    {"name": "get_pod_events", "durationMs": 12, "scopeGuardHit": False},
                    {"name": "get_recent_logs", "durationMs": 19, "scopeGuardHit": False},
                ],
            }
        },
    )
    normalized = service._ensure_complete_diagnosis(trigger, diagnosis)
    attribution = normalized.evidence_attribution
    assert any(item.get("source") == "tool" and item.get("tool") == "get_pod_events" for item in attribution)
    assert any(item.get("source") == "timeline" and item.get("signal") == "BackOff" for item in attribution)
    assert any(item.get("source") == "trigger" and item.get("reason") == "BackOff" for item in attribution)


def test_service_builds_minimal_fallback_attribution_when_no_signals_exist():
    service = AgentService(
        settings=build_settings(),
        client=FakeKubernetesClient(),
        codex_agent=build_fallback_agent(),
    )
    trigger = TriggerContext(
        source="scheduled",
        cluster="prod",
        workload=WorkloadRef(kind="Deployment", namespace="payments", name="checkout"),
        symptom="ReplicaMismatch",
        observed_for_seconds=1200,
    )
    diagnosis = DiagnosisResult(
        summary="Diagnosis incomplete",
        severity="warning",
        probable_causes=[],
        evidence=[],
        recommendations=[],
        confidence=0.0,
        used_fallback=True,
        evidence_timeline=[],
        raw_agent_output={"trace": {"traceId": "trace-empty", "toolSequence": []}},
    )
    normalized = service._ensure_complete_diagnosis(trigger, diagnosis)
    attribution = normalized.evidence_attribution
    assert attribution
    assert attribution[0].get("source") == "fallback"
    assert attribution[0].get("signal") == "ReplicaMismatch"


def test_service_merges_related_objects_with_correlation_context():
    service = AgentService(
        settings=build_settings(),
        client=FakeKubernetesClient(),
        codex_agent=build_fallback_agent(),
    )
    trigger = TriggerContext(
        source="scheduled",
        cluster="prod",
        workload=WorkloadRef(kind="Pod", namespace="payments", name="checkout-abc"),
        symptom="Pending",
        observed_for_seconds=1200,
        correlation_context={
            "relatedObjects": [
                {"kind": "Pod", "namespace": "payments", "name": "checkout-abc", "role": "primary"},
                {"kind": "Deployment", "namespace": "payments", "name": "checkout", "role": "owner"},
            ],
            "rootCauseCandidates": [
                {
                    "objectRef": {"kind": "Deployment", "namespace": "payments", "name": "checkout"},
                    "reason": "Deployment rollout blocked.",
                    "confidence": 0.7,
                }
            ],
            "evidenceTimeline": [],
            "impactSummary": {},
        },
    )
    diagnosis = DiagnosisResult(
        summary="Pending due to PVC pressure",
        severity="critical",
        probable_causes=["PVC unavailable"],
        evidence=["pod unschedulable"],
        recommendations=["check pvc"],
        confidence=0.8,
        related_objects=[
            {"kind": "PVC", "namespace": "payments", "name": "checkout-pvc", "role": "upstream-suspect"}
        ],
        root_cause_candidates=[],
    )

    completed = service._ensure_complete_diagnosis(trigger, diagnosis)

    assert any(item["kind"] == "Pod" for item in completed.related_objects)
    assert any(item["kind"] == "Deployment" for item in completed.related_objects)
    assert any(item["kind"] == "PVC" for item in completed.related_objects)
    assert completed.root_cause_candidates


def test_service_builds_minimal_root_cause_candidate_when_empty():
    service = AgentService(
        settings=build_settings(),
        client=FakeKubernetesClient(),
        codex_agent=build_fallback_agent(),
    )
    trigger = TriggerContext(
        source="scheduled",
        cluster="prod",
        workload=WorkloadRef(kind="Pod", namespace="payments", name="checkout-abc"),
        symptom="Pending",
        observed_for_seconds=1200,
        correlation_context={
            "relatedObjects": [
                {"kind": "Pod", "namespace": "payments", "name": "checkout-abc", "role": "primary"},
                {"kind": "PVC", "namespace": "payments", "name": "checkout-pvc", "role": "upstream-suspect"},
            ],
            "rootCauseCandidates": [],
            "evidenceTimeline": [],
            "impactSummary": {},
        },
    )
    diagnosis = DiagnosisResult(
        summary="Pending observed",
        severity="critical",
        probable_causes=["PVC not bound"],
        evidence=["unschedulable"],
        recommendations=["inspect pvc"],
        confidence=0.6,
        related_objects=[],
        root_cause_candidates=[],
    )

    completed = service._ensure_complete_diagnosis(trigger, diagnosis)

    assert completed.root_cause_candidates
    assert completed.root_cause_candidates[0]["objectRef"]["kind"] == "PVC"


def test_backfill_rewrites_incomplete_reports():
    class FakeWriter:
        def __init__(self):
            self.calls = []

        def upsert_report(self, trigger, diagnosis, model, prefix, category="", primary_signal=""):
            self.calls.append((trigger, diagnosis, model, prefix, category, primary_signal))
            return {"ok": True}

    class IncompleteClient(FakeKubernetesClient):
        def list_reports(self):
            return [
                {
                    "metadata": {"name": "diagnosis-b"},
                    "spec": {
                        "source": "scheduled",
                        "cluster": "prod",
                        "namespace": "payments",
                        "symptom": "Pending",
                        "observedFor": 1200,
                        "triggerAt": "2026-03-22T05:00:00+00:00",
                        "workloadRef": {"kind": "Pod", "name": "checkout-abc"},
                    },
                    "status": {"summary": "", "evidence": []},
                }
            ]

    client = IncompleteClient()
    writer = FakeWriter()
    settings = build_settings()
    engine = RuleEngine(cluster_name="prod", min_observation_seconds=600)
    service = AgentService(
        settings=settings,
        client=client,
        codex_agent=CodexDiagnosisAgent(
            responses_client=FakeResponsesClient([]),
            rule_engine=engine,
            model="gpt-5-codex",
            max_tool_calls=8,
            max_input_bytes=20000,
        ),
        report_writer=writer,
    )
    updated = service.backfill_incomplete_reports()
    assert updated == [{"ok": True}]
    assert writer.calls[0][1].summary
    assert writer.calls[0][1].evidence


def test_backfill_treats_blank_and_placeholder_fields_as_incomplete():
    class FakeWriter:
        def __init__(self):
            self.calls = []

        def upsert_report(self, trigger, diagnosis, model, prefix, category="", primary_signal=""):
            self.calls.append((trigger, diagnosis, model, prefix, category, primary_signal))
            return {"ok": True}

    class IncompleteClient(FakeKubernetesClient):
        def list_reports(self):
            return [
                {
                    "metadata": {"name": "diagnosis-empty"},
                    "spec": {
                        "source": "event",
                        "cluster": "prod",
                        "namespace": "payments",
                        "symptom": "FailedMount",
                        "observedFor": 60,
                        "triggerAt": "2026-03-22T05:00:00+00:00",
                        "workloadRef": {"kind": "Pod", "name": "checkout-abc"},
                    },
                    "status": {
                        "summary": "  ",
                        "evidence": [" ", "n/a"],
                        "recommendations": ["", "unknown"],
                    },
                }
            ]

    client = IncompleteClient()
    writer = FakeWriter()
    service = AgentService(
        settings=build_settings(),
        client=client,
        codex_agent=CodexDiagnosisAgent(
            responses_client=FakeResponsesClient([]),
            rule_engine=RuleEngine(cluster_name="prod", min_observation_seconds=600),
            model="gpt-5-codex",
            max_tool_calls=8,
            max_input_bytes=20000,
        ),
        report_writer=writer,
    )
    updated = service.backfill_incomplete_reports()
    assert updated == [{"ok": True}]
    assert writer.calls
    assert writer.calls[0][1].summary
    assert writer.calls[0][1].evidence
    assert writer.calls[0][1].recommendations


def test_service_normalizes_report_metadata_without_unknown_placeholders():
    class SparseClient(FakeKubernetesClient):
        def list_reports(self):
            return [
                {
                    "metadata": {"name": "diagnosis-sparse"},
                    "spec": {
                        "source": "event",
                        "cluster": "",
                        "namespace": "",
                        "symptom": "CrashLoopBackOff",
                        "observedFor": 0,
                        "triggerAt": "2026-03-22T05:00:00+00:00",
                        "workloadRef": {"kind": "", "name": ""},
                    },
                    "status": {
                        "severity": "warning",
                        "summary": "Detected CrashLoopBackOff.",
                        "probableCauses": ["Crash loop detected."],
                        "evidence": ["reason=BackOff"],
                        "recommendations": ["Inspect logs."],
                        "confidence": 0.6,
                        "analysisVersion": "0.3.0",
                        "modelInfo": {},
                        "rawSignal": {
                            "reason": "BackOff",
                            "message": "Back-off restarting failed container",
                            "timestamp": "2026-03-22T05:00:00+00:00",
                            "involvedObject": {
                                "kind": "Pod",
                                "name": "checkout-abc",
                                "namespace": "payments",
                            },
                        },
                    },
                }
            ]

    service = AgentService(
        settings=build_settings(),
        client=SparseClient(),
        codex_agent=CodexDiagnosisAgent(
            responses_client=FakeResponsesClient([]),
            rule_engine=RuleEngine(cluster_name="prod", min_observation_seconds=600),
            model="gpt-5-codex",
            max_tool_calls=8,
            max_input_bytes=20000,
        ),
    )
    report = service.list_reports()[0]
    assert report["workload"]["kind"] == "Pod"
    assert report["workload"]["name"] == "checkout-abc"
    assert report["namespace"] == "payments"
    assert report["cluster"] == "prod"
    assert report["modelInfo"] == {}
    assert report["rawSignal"]["reason"] == "BackOff"


def test_service_list_reports_fills_empty_legacy_status_fields():
    class SparseClient(FakeKubernetesClient):
        def list_reports(self):
            return [
                {
                    "metadata": {"name": "diagnosis-legacy-empty"},
                    "spec": {
                        "source": "scheduled",
                        "cluster": "prod",
                        "namespace": "payments",
                        "symptom": "Pending",
                        "observedFor": 300,
                        "triggerAt": "2026-03-22T05:00:00+00:00",
                        "workloadRef": {"kind": "Pod", "name": "checkout-abc"},
                    },
                    "status": {
                        "severity": "warning",
                        "summary": "",
                        "probableCauses": [],
                        "evidence": ["", "n/a"],
                        "recommendations": [],
                        "confidence": 0.4,
                        "modelInfo": {},
                        "rawSignal": {},
                    },
                }
            ]

    service = AgentService(
        settings=build_settings(),
        client=SparseClient(),
        codex_agent=CodexDiagnosisAgent(
            responses_client=FakeResponsesClient([]),
            rule_engine=RuleEngine(cluster_name="prod", min_observation_seconds=600),
            model="gpt-5-codex",
            max_tool_calls=8,
            max_input_bytes=20000,
        ),
    )

    report = service.list_reports()[0]
    assert report["summary"].startswith("Detected Pending")
    assert report["evidence"]
    assert report["recommendations"]
    assert report["probableCauses"]


def test_service_preserves_report_model_name_for_ollama_provider():
    class SparseClient(FakeKubernetesClient):
        def list_reports(self):
            return [
                {
                    "metadata": {"name": "diagnosis-ollama"},
                    "spec": {
                        "source": "event",
                        "cluster": "prod",
                        "namespace": "payments",
                        "symptom": "ImagePullBackOff",
                        "observedFor": 1200,
                        "triggerAt": "2026-03-22T05:00:00+00:00",
                        "workloadRef": {"kind": "Pod", "name": "checkout-abc"},
                    },
                    "status": {
                        "severity": "critical",
                        "summary": "ImagePullBackOff detected",
                        "probableCauses": ["image pull failed"],
                        "evidence": ["reason=ImagePullBackOff"],
                        "recommendations": ["check imagePullSecrets"],
                        "confidence": 0.6,
                        "analysisVersion": "0.4.3",
                        "modelInfo": {},
                    },
                }
            ]

    settings = build_settings()
    settings.model_provider = "ollama"
    settings.ollama_model = "qwen3:8b"
    service = AgentService(
        settings=settings,
        client=SparseClient(),
        codex_agent=CodexDiagnosisAgent(
            responses_client=FakeResponsesClient([]),
            rule_engine=RuleEngine(cluster_name="prod", min_observation_seconds=600),
            model="qwen3:8b",
            max_tool_calls=8,
            max_input_bytes=20000,
        ),
    )

    report = service.list_reports()[0]
    assert report["modelInfo"] == {}


def test_service_filters_placeholder_root_cause_candidates_from_report_view():
    class SparseClient(FakeKubernetesClient):
        def list_reports(self):
            return [
                {
                    "metadata": {"name": "diagnosis-placeholder-candidates"},
                    "spec": {
                        "source": "scheduled",
                        "cluster": "prod",
                        "namespace": "diag-e2e",
                        "symptom": "Pending",
                        "observedFor": 600,
                        "triggerAt": "2026-03-29T13:20:00+00:00",
                        "workloadRef": {"kind": "Pod", "name": "checkout-abc"},
                    },
                    "status": {
                        "severity": "critical",
                        "summary": "pending",
                        "probableCauses": ["pvc"],
                        "evidence": ["failed scheduling"],
                        "recommendations": ["inspect pvc"],
                        "confidence": 0.7,
                        "relatedObjects": [
                            {"kind": "PVC", "namespace": "diag-e2e", "name": "e2e-unbound-pvc", "role": "affected"},
                            {"kind": "PVC", "namespace": "diag-e2e", "name": "unknown", "role": "affected"},
                        ],
                        "rootCauseCandidates": [
                            {
                                "objectRef": {"kind": "PVC", "namespace": "diag-e2e", "name": "unknown-pvc"},
                                "reason": "placeholder candidate",
                                "confidence": 0.5,
                            },
                            {
                                "objectRef": {"kind": "PVC", "namespace": "diag-e2e", "name": "e2e-unbound-pvc"},
                                "reason": "valid pvc candidate",
                                "confidence": 0.9,
                            },
                        ],
                        "rawSignal": {"kind": "Pod", "name": "checkout-abc", "namespace": "diag-e2e", "symptom": "Pending"},
                    },
                }
            ]

    service = AgentService(
        settings=build_settings(),
        client=SparseClient(),
        codex_agent=CodexDiagnosisAgent(
            responses_client=FakeResponsesClient([]),
            rule_engine=RuleEngine(cluster_name="prod", min_observation_seconds=600),
            model="gpt-5-codex",
            max_tool_calls=8,
            max_input_bytes=20000,
        ),
    )

    report = service.list_reports()[0]
    assert all(item.get("name") != "unknown" for item in report["relatedObjects"])
    assert all(item["objectRef"].get("name") != "unknown-pvc" for item in report["rootCauseCandidates"])
    assert any(item["objectRef"].get("name") == "e2e-unbound-pvc" for item in report["rootCauseCandidates"])


def test_process_trigger_failed_mount_fallback_adds_specific_recommendation():
    service = AgentService(
        settings=build_settings(),
        client=FakeKubernetesClient(),
        codex_agent=CodexDiagnosisAgent(
            responses_client=FakeResponsesClient(
                [
                    {
                        "output_text": json.dumps(
                            {
                                "summary": "",
                                "severity": "",
                                "probableCauses": [],
                                "evidence": [],
                                "recommendations": [],
                                "confidence": 0.0,
                            }
                        )
                    }
                ]
            ),
            rule_engine=RuleEngine(cluster_name="prod", min_observation_seconds=600),
            model="gpt-5-codex",
            max_tool_calls=8,
            max_input_bytes=20000,
            max_diagnosis_seconds=1,
        ),
    )

    report = service.process_trigger(
        TriggerContext(
            source="event",
            cluster="prod",
            workload=WorkloadRef(kind="Pod", namespace="diag-e2e", name="checkout-abc"),
            symptom="FailedMount",
            observed_for_seconds=300,
            raw_signal={
                "eventType": "Warning",
                "reason": "FailedMount",
                "message": "MountVolume.SetUp failed for volume \"config\" : secret \"missing-secret\" not found",
            },
        )
    )

    status = report["status"]
    joined_recommendations = " ".join(status["recommendations"]).lower()
    joined_causes = " ".join(status["probableCauses"]).lower()
    assert "secret" in joined_recommendations
    assert "missing" in joined_recommendations
    assert "secret" in joined_causes


def test_process_trigger_normalizes_event_metadata_before_writing():
    service = AgentService(
        settings=build_settings(),
        client=FakeKubernetesClient(),
        codex_agent=CodexDiagnosisAgent(
            responses_client=FakeResponsesClient(
                [
                    {
                        "output_text": json.dumps(
                            {
                                "summary": "",
                                "severity": "warning",
                                "probableCauses": [],
                                "evidence": [],
                                "recommendations": [],
                                "confidence": 0.0,
                            }
                        )
                    }
                ]
            ),
            rule_engine=RuleEngine(cluster_name="prod", min_observation_seconds=600),
            model="gpt-5-codex",
            max_tool_calls=8,
            max_input_bytes=20000,
        ),
    )
    report = service.process_trigger(
        TriggerContext(
            source="event",
            cluster="",
            workload=WorkloadRef(kind="", namespace="", name=""),
            symptom="CrashLoopBackOff",
            observed_for_seconds=0,
            raw_signal={
                "reason": "BackOff",
                "message": "Back-off restarting failed container",
            },
        )
    )
    assert report["spec"]["cluster"] == "prod"
    assert report["spec"]["workloadRef"]["kind"] == "Pod"
    assert report["spec"]["workloadRef"]["name"] == ""
    assert report["status"]["modelInfo"]["name"] == "gpt-5-codex"
    assert report["status"]["rawSignal"]["reason"] == "BackOff"
    assert report["status"]["rawSignal"]["timestamp"]


def test_process_trigger_builds_correlation_for_pvc_propagation():
    service = AgentService(
        settings=build_settings(),
        client=FakeKubernetesClient(),
        codex_agent=build_fallback_agent(),
    )
    report = service.process_trigger(
        TriggerContext(
            source="event",
            cluster="prod",
            workload=WorkloadRef(kind="Pod", namespace="payments", name="checkout-abc"),
            symptom="FailedMount",
            observed_for_seconds=1200,
            raw_signal={"reason": "FailedMount", "message": "Unable to attach or mount volumes"},
        )
    )
    assert any(item["kind"] == "PVC" for item in report["status"]["relatedObjects"])
    assert report["status"]["rootCauseCandidates"][0]["objectRef"]["kind"] == "PVC"
    assert report["status"]["impactSummary"]["podCount"] >= 2


def test_process_trigger_builds_correlation_for_node_scoped_impact():
    service = AgentService(
        settings=build_settings(),
        client=FakeKubernetesClient(),
        codex_agent=build_fallback_agent(),
    )
    report = service.process_trigger(
        TriggerContext(
            source="scheduled",
            cluster="prod",
            workload=WorkloadRef(kind="Pod", namespace="payments", name="checkout-abc"),
            symptom="Pending",
            observed_for_seconds=1800,
        )
    )
    assert any(item["kind"] == "Node" for item in report["status"]["relatedObjects"])
    assert any(
        item["objectRef"]["kind"] == "Node"
        for item in report["status"]["rootCauseCandidates"]
    )


def test_process_trigger_builds_correlation_for_rollout_stuck():
    service = AgentService(
        settings=build_settings(),
        client=FakeKubernetesClient(),
        codex_agent=build_fallback_agent(),
    )
    report = service.process_trigger(
        TriggerContext(
            source="scheduled",
            cluster="prod",
            workload=WorkloadRef(kind="Deployment", namespace="payments", name="checkout"),
            symptom="ProgressDeadlineExceeded",
            observed_for_seconds=1800,
        )
    )
    assert report["status"]["rootCauseCandidates"][0]["objectRef"]["kind"] == "Deployment"
    assert report["status"]["evidenceTimeline"][0]["signal"]


def test_process_trigger_builds_correlation_for_shared_image_breakage():
    service = AgentService(
        settings=build_settings(),
        client=FakeKubernetesClient(),
        codex_agent=build_fallback_agent(),
    )
    report = service.process_trigger(
        TriggerContext(
            source="event",
            cluster="prod",
            workload=WorkloadRef(kind="Pod", namespace="payments", name="checkout-abc"),
            symptom="ImagePullBackOff",
            observed_for_seconds=900,
            raw_signal={"reason": "Failed", "message": "image pull failed"},
        )
    )
    assert any(
        item["objectRef"]["kind"] in {"Deployment", "ReplicaSet"}
        for item in report["status"]["rootCauseCandidates"]
    )


def test_process_trigger_enriches_raw_signal_for_pod_symptoms():
    service = AgentService(
        settings=build_settings(),
        client=FakeKubernetesClient(),
        codex_agent=CodexDiagnosisAgent(
            responses_client=FakeResponsesClient(
                [
                    {
                        "output_text": json.dumps(
                            {
                                "summary": "Volume mount is blocked by an unbound PVC.",
                                "severity": "critical",
                                "probableCauses": ["PVC is not bound."],
                                "evidence": ["PVC phase is Pending."],
                                "recommendations": ["Bind or recreate the PVC."],
                                "confidence": 0.81,
                            }
                        )
                    }
                ]
            ),
            rule_engine=RuleEngine(cluster_name="prod", min_observation_seconds=600),
            model="gpt-5-codex",
            max_tool_calls=8,
            max_input_bytes=20000,
        ),
    )
    report = service.process_trigger(
        TriggerContext(
            source="event",
            cluster="prod",
            workload=WorkloadRef(kind="Pod", namespace="payments", name="checkout-abc"),
            symptom="FailedMount",
            observed_for_seconds=0,
            raw_signal={"reason": "FailedMount", "message": "Unable to attach or mount volumes"},
        )
    )
    assert report["status"]["rawSignal"]["podPhase"] == "Pending"
    assert report["status"]["rawSignal"]["podReason"] == "Unschedulable"
    assert report["status"]["rawSignal"]["containerReason"] == "CrashLoopBackOff"
    assert report["status"]["rawSignal"]["pvcPhase"] == "Pending"


def test_process_trigger_enriches_raw_signal_for_deployment_symptoms():
    service = AgentService(
        settings=build_settings(),
        client=FakeKubernetesClient(),
        codex_agent=CodexDiagnosisAgent(
            responses_client=FakeResponsesClient(
                [
                    {
                        "output_text": json.dumps(
                            {
                                "summary": "Rollout is stalled.",
                                "severity": "critical",
                                "probableCauses": ["Deployment exceeded its progress deadline."],
                                "evidence": ["Deployment condition reports ProgressDeadlineExceeded."],
                                "recommendations": ["Inspect rollout health."],
                                "confidence": 0.7,
                            }
                        )
                    }
                ]
            ),
            rule_engine=RuleEngine(cluster_name="prod", min_observation_seconds=600),
            model="gpt-5-codex",
            max_tool_calls=8,
            max_input_bytes=20000,
        ),
    )
    report = service.process_trigger(
        TriggerContext(
            source="scheduled",
            cluster="prod",
            workload=WorkloadRef(kind="Deployment", namespace="payments", name="checkout"),
            symptom="ProgressDeadlineExceeded",
            observed_for_seconds=1800,
        )
    )
    assert report["status"]["rawSignal"]["deploymentCondition"] == "ProgressDeadlineExceeded"


def test_scan_once_filters_agent_self_findings():
    class SelfFindingClient(FakeKubernetesClient):
        def list_anomaly_snapshot(self):
            return [
                {
                    "namespace": "k8s-diagnosis-system",
                    "name": "k8s-diagnosis-agent-abc123",
                    "kind": "Pod",
                    "symptom": "Pending",
                    "observed_for_seconds": 1800,
                },
                {
                    "namespace": "payments",
                    "name": "checkout-abc",
                    "kind": "Pod",
                    "symptom": "CrashLoopBackOff",
                    "observed_for_seconds": 1800,
                },
            ]

    service = AgentService(
        settings=build_settings(),
        client=SelfFindingClient(),
        codex_agent=CodexDiagnosisAgent(
            responses_client=FakeResponsesClient(
                [
                    {
                        "output_text": json.dumps(
                            {
                                "summary": "Checkout pod crashes because DATABASE_URL is missing.",
                                "severity": "critical",
                                "probableCauses": ["Required database environment variable is absent."],
                                "evidence": ["Container logs contain missing DATABASE_URL."],
                                "recommendations": ["Restore secret wiring."],
                                "confidence": 0.93,
                            }
                        )
                    }
                ]
            ),
            rule_engine=RuleEngine(cluster_name="prod", min_observation_seconds=600),
            model="gpt-5-codex",
            max_tool_calls=8,
            max_input_bytes=20000,
        ),
    )
    reports = service.scan_once()
    assert len(reports) == 1
    assert reports[0]["spec"]["workloadRef"]["name"] == "checkout-abc"


def engine_result():
    from agent.models import DiagnosisResult

    return DiagnosisResult(
        summary="Scheduler cannot place the pod due to quota pressure.",
        severity="warning",
        probable_causes=["Namespace quota is exhausted."],
        evidence=["Quota usage is near limit."],
        recommendations=["Increase quota or reduce requests."],
        confidence=0.8,
    )
