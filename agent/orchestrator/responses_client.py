from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Protocol
from urllib import request

from agent.runtime_logging import get_logger, log_event


LOGGER = get_logger("model_client")


class ModelClient(Protocol):
    provider_name: str
    model: str
    base_url: str
    api_key: str

    def create_response(self, payload: dict[str, Any]) -> dict[str, Any]:
        ...


def _post_json(
    url: str,
    body: dict[str, Any],
    timeout_seconds: int,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    req = request.Request(
        url=url,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers=headers or {"Content-Type": "application/json"},
    )
    with request.urlopen(req, timeout=timeout_seconds) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _prepare_openai_input(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    for item in messages:
        if item.get("role") in {"system", "user", "assistant"}:
            prepared.append(item)
            continue
        if item.get("type") == "function_call_output":
            prepared.append(item)
    return prepared


def _prepare_ollama_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    call_map: dict[str, str] = {}
    for item in messages:
        if item.get("role") in {"system", "user", "assistant"}:
            prepared.append(
                {
                    "role": item["role"],
                    "content": item.get("content", ""),
                }
            )
            continue
        if item.get("type") == "assistant_tool_calls":
            tool_calls = []
            for call in item.get("tool_calls", []):
                call_id = call.get("call_id", "")
                name = call.get("name", "")
                raw_arguments = call.get("arguments") or "{}"
                arguments = (
                    json.loads(raw_arguments)
                    if isinstance(raw_arguments, str)
                    else raw_arguments
                )
                if call_id and name:
                    call_map[call_id] = name
                tool_calls.append(
                    {
                        "function": {
                            "name": name,
                            "arguments": arguments,
                        }
                    }
                )
            prepared.append(
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": tool_calls,
                }
            )
            continue
        if item.get("type") == "function_call_output":
            prepared.append(
                {
                    "role": "tool",
                    "content": item.get("output", ""),
                    "name": call_map.get(item.get("call_id", ""), ""),
                }
            )
    return prepared


def _prepare_ollama_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    for item in tools:
        if item.get("type") != "function":
            continue
        prepared.append(
            {
                "type": "function",
                "function": {
                    "name": item.get("name", ""),
                    "description": item.get("description", ""),
                    "parameters": item.get("parameters", {}),
                },
            }
        )
    return prepared


@dataclass
class OpenAIResponsesClient:
    api_key: str
    model: str
    api_base_url: str
    timeout_seconds: int
    provider_name: str = "openai"

    @property
    def base_url(self) -> str:
        return self.api_base_url

    def create_response(self, payload: dict[str, Any]) -> dict[str, Any]:
        log_event(
            LOGGER,
            logging.INFO,
            "model_request_start",
            "sending model request",
            provider=self.provider_name,
            model=self.model,
            base_url=self.base_url,
        )
        try:
            response = _post_json(
                url=f"{self.api_base_url.rstrip('/')}/responses",
                body={
                    **payload,
                    "input": _prepare_openai_input(payload.get("input", [])),
                },
                timeout_seconds=self.timeout_seconds,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
            )
        except Exception as exc:
            log_event(
                LOGGER,
                logging.ERROR,
                "model_request_failed",
                "model request failed",
                provider=self.provider_name,
                model=self.model,
                error=str(exc),
            )
            raise
        log_event(
            LOGGER,
            logging.INFO,
            "model_request_end",
            "model request completed",
            provider=self.provider_name,
            model=self.model,
        )
        return response


@dataclass
class OllamaResponsesClient:
    model: str
    base_url: str
    timeout_seconds: int
    api_key: str = ""
    provider_name: str = "ollama"

    def create_response(self, payload: dict[str, Any]) -> dict[str, Any]:
        request_body = {
            "model": self.model,
            "stream": False,
            "messages": _prepare_ollama_messages(payload.get("input", [])),
            "tools": _prepare_ollama_tools(payload.get("tools", [])),
        }
        log_event(
            LOGGER,
            logging.INFO,
            "model_request_start",
            "sending model request",
            provider=self.provider_name,
            model=self.model,
            base_url=self.base_url,
        )
        try:
            response = _post_json(
                url=f"{self.base_url.rstrip('/')}/api/chat",
                body=request_body,
                timeout_seconds=self.timeout_seconds,
            )
        except Exception as exc:
            log_event(
                LOGGER,
                logging.ERROR,
                "model_request_failed",
                "model request failed",
                provider=self.provider_name,
                model=self.model,
                error=str(exc),
            )
            raise
        message = response.get("message", {})
        tool_calls = message.get("tool_calls", []) or []
        if tool_calls:
            output = []
            for index, call in enumerate(tool_calls, start=1):
                function = call.get("function", {})
                arguments = function.get("arguments", {})
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except json.JSONDecodeError:
                        arguments = {"raw": arguments}
                output.append(
                    {
                        "type": "function_call",
                        "name": function.get("name", ""),
                        "call_id": f"ollama_call_{index}",
                        "arguments": json.dumps(arguments),
                    }
                )
            mapped = {"output": output, "output_text": message.get("content", "")}
        else:
            text = message.get("content", "")
            mapped = {
                "output_text": text,
                "output": [
                    {
                        "type": "message",
                        "content": [{"text": text}],
                    }
                ],
            }
        log_event(
            LOGGER,
            logging.INFO,
            "model_request_end",
            "model request completed",
            provider=self.provider_name,
            model=self.model,
            tool_calls=len(tool_calls),
        )
        return mapped
