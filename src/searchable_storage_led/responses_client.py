from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

from .llm_client import (
    Clock,
    LlmClient,
    LlmError,
    LlmFailureCooldown,
    LlmResult,
    llm_error_category,
    loads_json_object,
)


ResponsesTransport = Callable[
    [str, dict[str, str], dict[str, object]],
    dict[str, object],
]


def _urllib_transport(
    url: str,
    headers: dict[str, str],
    body: dict[str, object],
) -> dict[str, object]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise LlmError(
            f"Responses API request failed ({exc.code}): {detail}",
            category=llm_error_category(exc.code, detail),
            status_code=exc.code,
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise LlmError(
            f"Responses API request failed: {exc}",
            category="provider_unavailable",
        ) from exc


def _response_text(raw: dict[str, object]) -> str:
    chunks: list[str] = []
    output = raw.get("output")
    if not isinstance(output, list):
        return ""
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and part.get("type") == "output_text":
                chunks.append(str(part.get("text") or ""))
    return "".join(chunks)


def _response_tool_calls(raw: dict[str, object]) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []
    output = raw.get("output")
    if not isinstance(output, list):
        return calls
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "function_call":
            continue
        call_id = str(item.get("call_id") or item.get("id") or "")
        calls.append(
            {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": str(item.get("name") or ""),
                    "arguments": str(item.get("arguments") or "{}"),
                },
            }
        )
    return calls


def _responses_tools(tools: list[dict[str, object]] | None) -> list[dict[str, object]]:
    converted: list[dict[str, object]] = []
    for tool in tools or []:
        function = tool.get("function")
        if tool.get("type") != "function" or not isinstance(function, dict):
            continue
        converted.append(
            {
                "type": "function",
                "name": str(function.get("name") or ""),
                "description": str(function.get("description") or ""),
                "parameters": function.get("parameters") or {"type": "object"},
            }
        )
    return converted


def _content_parts(content: object) -> object:
    if not isinstance(content, list):
        return content
    parts: list[dict[str, object]] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text":
            parts.append({"type": "input_text", "text": str(part.get("text") or "")})
        elif part.get("type") == "image_url":
            image = part.get("image_url")
            url = image.get("url") if isinstance(image, dict) else image
            if url:
                parts.append(
                    {
                        "type": "input_image",
                        "image_url": str(url),
                        "detail": "auto",
                    }
                )
    return parts


def _responses_input(
    messages: list[dict[str, object]],
) -> tuple[str, list[dict[str, object]]]:
    instructions: list[str] = []
    inputs: list[dict[str, object]] = []
    for message in messages:
        if message.get("role") == "system":
            if content := str(message.get("content") or "").strip():
                instructions.append(content)
            continue
        raw_output = message.get("responses_output")
        if isinstance(raw_output, list):
            inputs.extend(item for item in raw_output if isinstance(item, dict))
            continue
        role = str(message.get("role") or "")
        if role == "tool":
            inputs.append(
                {
                    "type": "function_call_output",
                    "call_id": str(message.get("tool_call_id") or ""),
                    "output": str(message.get("content") or ""),
                }
            )
            continue
        if role == "assistant" and isinstance(message.get("tool_calls"), list):
            content = str(message.get("content") or "").strip()
            if content:
                inputs.append({"role": "assistant", "content": content})
            for call in message["tool_calls"]:
                if not isinstance(call, dict):
                    continue
                function = call.get("function")
                if not isinstance(function, dict):
                    continue
                inputs.append(
                    {
                        "type": "function_call",
                        "call_id": str(call.get("id") or ""),
                        "name": str(function.get("name") or ""),
                        "arguments": str(function.get("arguments") or "{}"),
                    }
                )
            continue
        if role in {"user", "assistant"}:
            inputs.append(
                {
                    "role": role,
                    "content": _content_parts(message.get("content")),
                }
            )
    return "\n\n".join(instructions), inputs


class OpenAIResponsesClient:
    """Responses API adapter with an OpenAI-compatible chat fallback."""

    def __init__(
        self,
        *,
        model: str = "gpt-5.6-luna",
        api_key_env: str = "OPENAI_API_KEY",
        base_url: str = "https://api.openai.com/v1",
        reasoning_effort: str = "low",
        fallback: LlmClient | None = None,
        env: dict[str, str] | None = None,
        transport: ResponsesTransport | None = None,
        clock: Clock | None = None,
        wall_clock: Clock | None = None,
        failure_state_path: str | Path | None = None,
    ) -> None:
        self.model = model
        self.api_key_env = api_key_env
        self.base_url = base_url.rstrip("/")
        self.reasoning_effort = reasoning_effort
        self.fallback = fallback
        self._env = env if env is not None else dict(os.environ)
        self._transport = transport or _urllib_transport
        self._cooldown = LlmFailureCooldown(
            clock=clock,
            wall_clock=wall_clock,
            state_path=failure_state_path,
        )

    @property
    def ready(self) -> bool:
        return bool(self._env.get(self.api_key_env))

    def provider_failures(self) -> list[dict[str, object]]:
        endpoint_id = f"{self.base_url}|{self.model}|{self.api_key_env}"
        primary = self._cooldown.snapshot().get(endpoint_id)
        failures = (
            [{"provider": "openai", "model": self.model, **primary}]
            if primary is not None
            else []
        )
        if self.fallback is not None:
            failures.extend(self.fallback.provider_failures())
        return failures

    def chat(
        self,
        *,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]] | None = None,
        response_format: dict[str, object] | None = None,
    ) -> LlmResult:
        endpoint_id = f"{self.base_url}|{self.model}|{self.api_key_env}"
        primary_error = self._cooldown.blocked(endpoint_id)
        if primary_error is None:
            try:
                result = self._chat_one(
                    messages=messages,
                    tools=tools,
                    response_format=response_format,
                )
                self._cooldown.succeeded(endpoint_id)
                return result
            except LlmError as exc:
                primary_error = exc
                self._cooldown.failed(endpoint_id, exc)
        if self.fallback is not None:
            try:
                sanitized = [
                    {
                        key: value
                        for key, value in message.items()
                        if key != "responses_output"
                    }
                    for message in messages
                ]
                return self.fallback.chat(
                    messages=sanitized,
                    tools=tools,
                    response_format=response_format,
                )
            except LlmError as fallback_error:
                category_order = (
                    "quota_exhausted",
                    "rate_limited",
                    "authentication",
                    "provider_unavailable",
                    "model_error",
                )
                category = next(
                    category
                    for category in category_order
                    if category in {primary_error.category, fallback_error.category}
                )
                chosen = (
                    primary_error
                    if primary_error.category == category
                    else fallback_error
                )
                raise LlmError(
                    "all LLM providers are unavailable",
                    category=category,
                    status_code=chosen.status_code,
                ) from fallback_error
        if primary_error is not None:
            raise primary_error
        raise LlmError("no LLM provider is configured", category="authentication")

    def complete_json(
        self,
        *,
        system: str,
        user_text: str,
        images: list[str] | None = None,
    ) -> dict[str, object]:
        content: object = user_text
        if images:
            content = [{"type": "text", "text": user_text}] + [
                {"type": "image_url", "image_url": {"url": url}}
                for url in images
            ]
        result = self.chat(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
            response_format={"type": "json_object"},
        )
        return loads_json_object(result.text)

    def _chat_one(
        self,
        *,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]] | None,
        response_format: dict[str, object] | None,
    ) -> LlmResult:
        key = self._env.get(self.api_key_env)
        if not key:
            raise LlmError(
                f"{self.api_key_env} is not set", category="authentication"
            )
        instructions, inputs = _responses_input(messages)
        body: dict[str, object] = {
            "model": self.model,
            "instructions": instructions,
            "input": inputs,
            "reasoning": {"effort": self.reasoning_effort},
            "text": {"verbosity": "low"},
            "store": False,
            "max_output_tokens": 1200,
        }
        converted_tools = _responses_tools(tools)
        if converted_tools:
            body["tools"] = converted_tools
            body["parallel_tool_calls"] = True
        if response_format:
            body["text"] = {
                "verbosity": "low",
                "format": response_format,
            }
        raw = self._transport(
            self.base_url + "/responses",
            {
                "authorization": f"Bearer {key}",
                "content-type": "application/json",
            },
            body,
        )
        if raw.get("error"):
            raise LlmError(f"Responses API error: {raw['error']}")
        return LlmResult(
            text=_response_text(raw),
            tool_calls=_response_tool_calls(raw),
            raw=raw,
        )
