from __future__ import annotations

import json
from datetime import date, datetime
from dataclasses import dataclass
from typing import Any, Callable

from agent.k8s_client.base import KubernetesReadClient
from agent.models import TriggerContext


JSON = dict[str, Any]
ToolHandler = Callable[[JSON], JSON]


def _json_friendly(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_friendly(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_friendly(item) for item in value]
    if isinstance(value, tuple):
        return [_json_friendly(item) for item in value]
    return value


@dataclass
class RegisteredTool:
    name: str
    description: str
    parameters: JSON
    handler: ToolHandler

    def as_openai_tool(self) -> JSON:
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


class ToolRegistry:
    def __init__(self, client: KubernetesReadClient, trigger: TriggerContext) -> None:
        self.client = client
        self.trigger = trigger
        self._tools = {
            tool.name: tool for tool in self._build_tools()
        }

    def openai_tools(self) -> list[JSON]:
        return [tool.as_openai_tool() for tool in self._tools.values()]

    def execute(self, name: str, arguments: JSON) -> str:
        if name not in self._tools:
            return json.dumps({"error": f"Unknown tool: {name}"}, ensure_ascii=True)
        guard_error = self._guard_arguments(name, arguments)
        if guard_error:
            return json.dumps(guard_error, ensure_ascii=True, sort_keys=True)
        result = self._tools[name].handler(arguments)
        return json.dumps(_json_friendly(result), ensure_ascii=True, sort_keys=True)

    def _guard_arguments(self, name: str, arguments: JSON) -> JSON | None:
        namespace = arguments.get("namespace")
        if namespace and name not in {"search_similar_reports"}:
            allowed = self.trigger.workload.namespace
            if allowed and namespace != allowed:
                return {
                    "error": "namespace out of allowed scope",
                    "resource": "tool_guard",
                    "tool": name,
                    "namespace": namespace,
                    "allowedNamespace": allowed,
                }
        return None

    def _build_tools(self) -> list[RegisteredTool]:
        workload_defaults = {
            "type": "object",
            "properties": {
                "namespace": {"type": "string"},
                "kind": {"type": "string"},
                "name": {"type": "string"},
            },
            "required": ["namespace", "kind", "name"],
            "additionalProperties": False,
        }
        pod_defaults = {
            "type": "object",
            "properties": {
                "namespace": {"type": "string"},
                "pod_name": {"type": "string"},
                "container": {"type": "string"},
            },
            "required": ["namespace", "pod_name"],
            "additionalProperties": False,
        }
        return [
            RegisteredTool(
                name="get_workload_status",
                description="Read the current status of a workload or pod.",
                parameters=workload_defaults,
                handler=lambda args: self.client.get_workload_status(**args),
            ),
            RegisteredTool(
                name="get_workload_events",
                description="Get recent Kubernetes events for a workload object such as a Deployment or ReplicaSet.",
                parameters=workload_defaults,
                handler=lambda args: self.client.get_workload_events(**args),
            ),
            RegisteredTool(
                name="get_owner_chain",
                description="Get the owner chain for a Kubernetes object such as Pod -> ReplicaSet -> Deployment.",
                parameters=workload_defaults,
                handler=lambda args: self.client.get_owner_chain(**args),
            ),
            RegisteredTool(
                name="get_related_events",
                description="Get a bounded recent event summary for a Kubernetes object.",
                parameters=workload_defaults,
                handler=lambda args: self.client.get_related_events(**args),
            ),
            RegisteredTool(
                name="get_namespace_events",
                description="Get a bounded recent event summary for a namespace.",
                parameters={
                    "type": "object",
                    "properties": {
                        "namespace": {"type": "string"},
                    },
                    "required": ["namespace"],
                    "additionalProperties": False,
                },
                handler=lambda args: self.client.get_namespace_events(**args),
            ),
            RegisteredTool(
                name="list_related_pods",
                description="List pods related to the target workload.",
                parameters=workload_defaults,
                handler=lambda args: self.client.list_related_pods(**args),
            ),
            RegisteredTool(
                name="get_attached_pvcs",
                description="List PVCs mounted by the target pod with summarized binding state.",
                parameters={
                    "type": "object",
                    "properties": {
                        "namespace": {"type": "string"},
                        "pod_name": {"type": "string"},
                    },
                    "required": ["namespace", "pod_name"],
                    "additionalProperties": False,
                },
                handler=lambda args: self.client.get_attached_pvcs(**args),
            ),
            RegisteredTool(
                name="get_pvc_dependents",
                description="List pods in the namespace that currently mount the target PVC.",
                parameters={
                    "type": "object",
                    "properties": {
                        "namespace": {"type": "string"},
                        "pvc_name": {"type": "string"},
                    },
                    "required": ["namespace", "pvc_name"],
                    "additionalProperties": False,
                },
                handler=lambda args: self.client.get_pvc_dependents(**args),
            ),
            RegisteredTool(
                name="get_pod_events",
                description="Get recent Kubernetes events for a pod.",
                parameters={
                    "type": "object",
                    "properties": {
                        "namespace": {"type": "string"},
                        "pod_name": {"type": "string"},
                    },
                    "required": ["namespace", "pod_name"],
                    "additionalProperties": False,
                },
                handler=lambda args: self.client.get_pod_events(**args),
            ),
            RegisteredTool(
                name="get_pod_spec_summary",
                description="Inspect a summarized pod spec including images, commands, env refs, volumes, and probes.",
                parameters={
                    "type": "object",
                    "properties": {
                        "namespace": {"type": "string"},
                        "pod_name": {"type": "string"},
                    },
                    "required": ["namespace", "pod_name"],
                    "additionalProperties": False,
                },
                handler=lambda args: self.client.get_pod_spec_summary(**args),
            ),
            RegisteredTool(
                name="get_pod_conditions",
                description="Inspect pod phase, reason, message, and condition summaries.",
                parameters={
                    "type": "object",
                    "properties": {
                        "namespace": {"type": "string"},
                        "pod_name": {"type": "string"},
                    },
                    "required": ["namespace", "pod_name"],
                    "additionalProperties": False,
                },
                handler=lambda args: self.client.get_pod_conditions(**args),
            ),
            RegisteredTool(
                name="get_container_statuses",
                description="Get container statuses and restart details for a pod.",
                parameters={
                    "type": "object",
                    "properties": {
                        "namespace": {"type": "string"},
                        "pod_name": {"type": "string"},
                    },
                    "required": ["namespace", "pod_name"],
                    "additionalProperties": False,
                },
                handler=lambda args: self.client.get_container_statuses(**args),
            ),
            RegisteredTool(
                name="get_deployment_status",
                description="Inspect deployment replica counts and rollout conditions.",
                parameters={
                    "type": "object",
                    "properties": {
                        "namespace": {"type": "string"},
                        "name": {"type": "string"},
                    },
                    "required": ["namespace", "name"],
                    "additionalProperties": False,
                },
                handler=lambda args: self.client.get_deployment_status(**args),
            ),
            RegisteredTool(
                name="get_replicaset_status",
                description="Inspect ReplicaSet replica counts, conditions, and owners.",
                parameters={
                    "type": "object",
                    "properties": {
                        "namespace": {"type": "string"},
                        "name": {"type": "string"},
                    },
                    "required": ["namespace", "name"],
                    "additionalProperties": False,
                },
                handler=lambda args: self.client.get_replicaset_status(**args),
            ),
            RegisteredTool(
                name="get_config_refs",
                description="List referenced Secret and ConfigMap names for a workload without exposing secret values.",
                parameters=workload_defaults,
                handler=lambda args: self.client.get_config_refs(**args),
            ),
            RegisteredTool(
                name="get_pvc_status",
                description="Inspect PVC binding state by claim name or by pod-mounted PVCs.",
                parameters={
                    "type": "object",
                    "properties": {
                        "namespace": {"type": "string"},
                        "pvc_name": {"type": "string"},
                        "pod_name": {"type": "string"},
                    },
                    "required": ["namespace"],
                    "additionalProperties": False,
                },
                handler=lambda args: self.client.get_pvc_status(**args),
            ),
            RegisteredTool(
                name="get_recent_logs",
                description="Read the most recent logs for a pod container.",
                parameters=pod_defaults,
                handler=lambda args: self.client.get_recent_logs(**args),
            ),
            RegisteredTool(
                name="get_node_conditions",
                description="Inspect node conditions for a specific node or all nodes.",
                parameters={
                    "type": "object",
                    "properties": {
                        "node_name": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
                handler=lambda args: self.client.get_node_conditions(**args),
            ),
            RegisteredTool(
                name="get_node_events",
                description="Get a bounded recent event summary for a node.",
                parameters={
                    "type": "object",
                    "properties": {
                        "node_name": {"type": "string"},
                    },
                    "required": ["node_name"],
                    "additionalProperties": False,
                },
                handler=lambda args: self.client.get_node_events(**args),
            ),
            RegisteredTool(
                name="get_node_workload_impact",
                description="Summarize pods currently scheduled on a node to estimate workload blast radius.",
                parameters={
                    "type": "object",
                    "properties": {
                        "node_name": {"type": "string"},
                    },
                    "required": ["node_name"],
                    "additionalProperties": False,
                },
                handler=lambda args: self.client.get_node_workload_impact(**args),
            ),
            RegisteredTool(
                name="get_namespace_quotas",
                description="Inspect namespace resource quotas and usage.",
                parameters={
                    "type": "object",
                    "properties": {
                        "namespace": {"type": "string"},
                    },
                    "required": ["namespace"],
                    "additionalProperties": False,
                },
                handler=lambda args: self.client.get_namespace_quotas(**args),
            ),
            RegisteredTool(
                name="get_hpa_status",
                description="Inspect horizontal pod autoscaler state for a namespace or target.",
                parameters={
                    "type": "object",
                    "properties": {
                        "namespace": {"type": "string"},
                        "target_name": {"type": "string"},
                    },
                    "required": ["namespace"],
                    "additionalProperties": False,
                },
                handler=lambda args: self.client.get_hpa_status(**args),
            ),
            RegisteredTool(
                name="get_resource_metrics",
                description="Read summarized resource metrics when available.",
                parameters={
                    "type": "object",
                    "properties": {
                        "namespace": {"type": "string"},
                        "name": {"type": "string"},
                    },
                    "required": ["namespace"],
                    "additionalProperties": False,
                },
                handler=lambda args: self.client.get_resource_metrics(**args),
            ),
            RegisteredTool(
                name="search_similar_reports",
                description="Search previous DiagnosisReport objects for similar symptoms.",
                parameters={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
                handler=lambda _args: self.client.search_similar_reports(self.trigger),
            ),
            RegisteredTool(
                name="get_related_reports",
                description="List prior DiagnosisReport objects for the same workload.",
                parameters=workload_defaults,
                handler=lambda args: self.client.get_related_reports(**args),
            ),
        ]
