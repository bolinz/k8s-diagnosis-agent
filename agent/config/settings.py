from __future__ import annotations

import os
from dataclasses import dataclass


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    return int(value)


def _csv_env(name: str) -> tuple[str, ...]:
    value = os.getenv(name, "")
    items = [item.strip() for item in value.split(",") if item.strip()]
    return tuple(items)


def _scope_mode_env(name: str, default: str) -> str:
    value = os.getenv(name, default).strip().lower()
    if value in {"strict", "relaxed"}:
        return value
    return default


@dataclass
class Settings:
    model_provider: str
    openai_api_key: str | None
    openai_model: str
    ollama_model: str
    cluster_name: str
    report_namespace: str
    scan_interval_seconds: int
    min_observation_seconds: int
    webhook_port: int
    max_tool_calls: int
    max_diagnosis_seconds: int
    max_input_bytes: int
    request_timeout_seconds: int
    api_base_url: str
    ollama_base_url: str
    diagnosis_name_prefix: str
    event_dedupe_window_seconds: int
    event_storm_threshold: int
    batch_threshold: int
    scope_mode: str
    scope_allowed_namespaces: tuple[str, ...]
    workload_name: str
    log_level: str

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            model_provider=os.getenv("MODEL_PROVIDER", "openai"),
            openai_api_key=os.getenv("OPENAI_API_KEY"),
            openai_model=os.getenv("OPENAI_MODEL", "gpt-5-codex"),
            ollama_model=os.getenv("OLLAMA_MODEL", ""),
            cluster_name=os.getenv("K8S_DIAGNOSIS_CLUSTER", "default-cluster"),
            report_namespace=os.getenv(
                "K8S_DIAGNOSIS_NAMESPACE", "k8s-diagnosis-system"
            ),
            scan_interval_seconds=_int_env(
                "K8S_DIAGNOSIS_SCAN_INTERVAL_SECONDS", 300
            ),
            min_observation_seconds=_int_env(
                "K8S_DIAGNOSIS_MIN_OBSERVATION_SECONDS", 600
            ),
            webhook_port=_int_env("K8S_DIAGNOSIS_WEBHOOK_PORT", 8080),
            max_tool_calls=_int_env("K8S_DIAGNOSIS_MAX_TOOL_CALLS", 8),
            max_diagnosis_seconds=_int_env("K8S_DIAGNOSIS_MAX_DIAGNOSIS_SECONDS", 45),
            max_input_bytes=_int_env("K8S_DIAGNOSIS_MAX_INPUT_BYTES", 20000),
            request_timeout_seconds=_int_env(
                "K8S_DIAGNOSIS_REQUEST_TIMEOUT_SECONDS", 45
            ),
            api_base_url=os.getenv("OPENAI_API_BASE_URL", "https://api.openai.com/v1"),
            ollama_base_url=os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434"),
            diagnosis_name_prefix=os.getenv(
                "K8S_DIAGNOSIS_NAME_PREFIX", "diagnosis"
            ),
            event_dedupe_window_seconds=_int_env(
                "K8S_DIAGNOSIS_EVENT_DEDUPE_WINDOW_SECONDS", 300
            ),
            event_storm_threshold=_int_env("K8S_DIAGNOSIS_EVENT_STORM_THRESHOLD", 5),
            batch_threshold=_int_env("K8S_DIAGNOSIS_BATCH_THRESHOLD", 5),
            scope_mode=_scope_mode_env("K8S_DIAGNOSIS_SCOPE_MODE", "strict"),
            scope_allowed_namespaces=_csv_env("K8S_DIAGNOSIS_SCOPE_ALLOWLIST"),
            workload_name=os.getenv("K8S_DIAGNOSIS_WORKLOAD_NAME", "k8s-diagnosis-agent"),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
        )
