from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from agent.analyzers.rules import RuleEngine
from agent.models import DiagnosisResult, ToolCallRecord, TriggerContext
from agent.tools.registry import ToolRegistry


DEFAULT_SYSTEM_PROMPT = """You are a Kubernetes reliability analyst.
You can only use the provided read-only tools.
You must not suggest direct kubectl mutations.
Return final output as strict JSON with fields:
summary, severity, probableCauses, evidence, recommendations, confidence.
Optional fields:
relatedObjects, rootCauseCandidates, evidenceTimeline, impactSummary.
Confidence must be a float between 0 and 1.
Evidence and recommendations must be short, concrete strings.
If evidence is insufficient, say so explicitly in summary and lower confidence.
Prefer the recommended tool sequence for the current symptom before using unrelated tools."""


@dataclass
class CodexDiagnosisAgent:
    responses_client: Any
    rule_engine: RuleEngine
    model: str
    max_tool_calls: int
    max_input_bytes: int
    tool_history: list[ToolCallRecord] = field(default_factory=list)

    def diagnose(
        self,
        trigger: TriggerContext,
        tool_registry: ToolRegistry,
    ) -> DiagnosisResult:
        if not getattr(self.responses_client, "api_key", None):
            return self.rule_engine.fallback_diagnosis(trigger)

        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": DEFAULT_SYSTEM_PROMPT,
            },
            {
                "role": "user",
                "content": self._build_user_prompt(trigger),
            },
        ]

        tool_calls = 0
        while True:
            response = self.responses_client.create_response(
                {
                    "model": self.model,
                    "input": messages,
                    "tools": tool_registry.openai_tools(),
                    "tool_choice": "auto",
                }
            )
            output = response.get("output", [])
            function_calls = [
                item for item in output if item.get("type") == "function_call"
            ]
            if not function_calls:
                return self._parse_final_response(response, trigger)
            for call in function_calls:
                if tool_calls >= self.max_tool_calls:
                    return self.rule_engine.fallback_diagnosis(trigger)
                tool_calls += 1
                arguments = json.loads(call.get("arguments") or "{}")
                tool_output = tool_registry.execute(call["name"], arguments)
                self.tool_history.append(
                    ToolCallRecord(
                        name=call["name"],
                        arguments=arguments,
                        output=tool_output[:5000],
                    )
                )
                messages.append(
                    {
                        "type": "function_call_output",
                        "call_id": call["call_id"],
                        "output": tool_output[: self.max_input_bytes],
                    }
                )

    def _build_user_prompt(self, trigger: TriggerContext) -> str:
        signal_json = json.dumps(trigger.raw_signal, ensure_ascii=True, sort_keys=True)
        preferred_tools = ", ".join(self._preferred_tool_sequence(trigger.symptom))
        return (
            f"Diagnose a Kubernetes issue.\n"
            f"Cluster: {trigger.cluster}\n"
            f"Namespace: {trigger.workload.namespace}\n"
            f"Workload kind: {trigger.workload.kind}\n"
            f"Workload name: {trigger.workload.name}\n"
            f"Symptom: {trigger.symptom}\n"
            f"Observed for seconds: {trigger.observed_for_seconds}\n"
            f"Raw signal: {signal_json[: self.max_input_bytes]}\n"
            f"Correlation context: {json.dumps(trigger.correlation_context, ensure_ascii=True, sort_keys=True)[: self.max_input_bytes]}\n"
            f"Preferred tool order: {preferred_tools}\n"
            "Use tools to gather evidence before concluding."
        )

    def _preferred_tool_sequence(self, symptom: str) -> list[str]:
        if symptom in {"CrashLoopBackOff", "ContainerCannotRun", "CreateContainerError"}:
            return [
                "get_container_statuses",
                "get_recent_logs",
                "get_pod_spec_summary",
                "get_pod_events",
            ]
        if symptom in {"Pending", "FailedMount", "CreateContainerConfigError"}:
            return [
                "get_pod_conditions",
                "get_pod_events",
                "get_namespace_quotas",
                "get_pvc_status",
                "get_config_refs",
            ]
        if symptom in {"ProgressDeadlineExceeded", "ReplicaMismatch"}:
            return [
                "get_deployment_status",
                "get_workload_events",
                "list_related_pods",
                "get_pod_conditions",
            ]
        return [
            "get_workload_status",
            "list_related_pods",
            "get_pod_events",
            "search_similar_reports",
        ]

    def _parse_final_response(
        self,
        response: dict[str, Any],
        trigger: TriggerContext,
    ) -> DiagnosisResult:
        text = self._extract_output_text(response)
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return self.rule_engine.fallback_diagnosis(trigger)
        return DiagnosisResult(
            summary=str(payload.get("summary", "")).strip() or "Diagnosis incomplete",
            severity=str(payload.get("severity", "warning")),
            probable_causes=[
                str(item) for item in payload.get("probableCauses", []) if str(item)
            ],
            evidence=[str(item) for item in payload.get("evidence", []) if str(item)],
            recommendations=[
                str(item) for item in payload.get("recommendations", []) if str(item)
            ],
            confidence=float(payload.get("confidence", 0.0)),
            related_objects=[
                item for item in payload.get("relatedObjects", []) if isinstance(item, dict)
            ],
            root_cause_candidates=[
                item for item in payload.get("rootCauseCandidates", []) if isinstance(item, dict)
            ],
            evidence_timeline=[
                item for item in payload.get("evidenceTimeline", []) if isinstance(item, dict)
            ],
            impact_summary=payload.get("impactSummary", {})
            if isinstance(payload.get("impactSummary", {}), dict)
            else {},
            raw_agent_output={"response": response, "text": text},
        )

    def _extract_output_text(self, response: dict[str, Any]) -> str:
        if isinstance(response.get("output_text"), str):
            return response["output_text"]
        chunks: list[str] = []
        for item in response.get("output", []):
            if item.get("type") != "message":
                continue
            for content in item.get("content", []):
                text = content.get("text")
                if text:
                    chunks.append(text)
        return "\n".join(chunks)
