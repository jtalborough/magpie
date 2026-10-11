"""One OpenAI-compatible chat client for every provider.

OpenRouter, Ollama, llama.cpp, vLLM and LM Studio all speak the same
`/chat/completions` + tool-calling API, so supporting "hosted or local" is one
client pointed at different base URLs (see docs/vision.md). The client tries the
primary endpoint, then an optional fallback (e.g. a local model on the Pi when
the network is down).

Tools and vision are both supported here so later slices (the agent loop) need
no client changes. Transport is injectable so tests run without a network.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from string import Template
from typing import Callable

from .config import LlmConfig, LlmEndpoint


class LlmError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        category: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        if status_code is None:
            status_match = re.search(r"\b(?:failed\s*)?\((\d{3})\)", message.casefold())
            if status_match:
                status_code = int(status_match.group(1))
        self.category = category or llm_error_category(status_code, message)
        self.status_code = status_code


def llm_error_category(status_code: int | None, detail: str = "") -> str:
    text = detail.casefold()
    if status_code is None:
        status_match = re.search(r"\b(?:failed\s*)?\((\d{3})\)", text)
        if status_match:
            status_code = int(status_match.group(1))
    if status_code in {401, 403}:
        return "authentication"
    if status_code == 402 or any(
        marker in text
        for marker in (
            "insufficient credit",
            "insufficient quota",
            "insufficient_quota",
            "quota exhausted",
        )
    ):
        return "quota_exhausted"
    if status_code == 429:
        return "rate_limited"
    if "rate limit" in text:
        return "rate_limited"
    if status_code is not None and status_code >= 500:
        return "provider_unavailable"
    return "model_error"


# A transport sends one request: (url, headers, body) -> parsed JSON response.
Transport = Callable[[str, dict[str, str], dict[str, object]], dict[str, object]]
Clock = Callable[[], float]


_FAILURE_COOLDOWNS = {
    "authentication": 3600.0,
    # Quota failures are billing-state failures, not burst rate limits. Retrying
    # every hour only burns requests and repeatedly surfaces the same error.
    "quota_exhausted": 21600.0,
    "rate_limited": 60.0,
    "provider_unavailable": 10.0,
    "model_error": 10.0,
}


class LlmFailureCooldown:
    """Avoid repeatedly calling an endpoint that just returned a known failure."""

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        wall_clock: Clock | None = None,
        state_path: str | Path | None = None,
    ) -> None:
        self._clock = clock or time.monotonic
        self._wall_clock = wall_clock or time.time
        self._state_path = Path(state_path) if state_path is not None else None
        self._failures: dict[str, tuple[float, LlmError]] = {}
        self._lock = threading.RLock()
        self._load()

    def _load(self) -> None:
        if self._state_path is None:
            return
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return
        failures = raw.get("failures") if isinstance(raw, dict) else None
        if not isinstance(failures, dict):
            return
        now = self._wall_clock()
        monotonic_now = self._clock()
        for endpoint_id, failure in failures.items():
            if not isinstance(endpoint_id, str) or not isinstance(failure, dict):
                continue
            try:
                remaining = float(failure.get("retry_at") or 0) - now
            except (TypeError, ValueError):
                continue
            if remaining <= 0:
                continue
            status_code = failure.get("status_code")
            self._failures[endpoint_id] = (
                monotonic_now + remaining,
                LlmError(
                    "endpoint is cooling down after a persisted provider failure",
                    category=str(failure.get("category") or "provider_unavailable"),
                    status_code=int(status_code) if isinstance(status_code, int) else None,
                ),
            )

    def _persist_locked(self) -> None:
        if self._state_path is None:
            return
        monotonic_now = self._clock()
        wall_now = self._wall_clock()
        payload = {
            "schema": "magpie.llm_failures.v1",
            "failures": {
                endpoint_id: {
                    "category": error.category,
                    "status_code": error.status_code,
                    "retry_at": wall_now + max(0.0, retry_at - monotonic_now),
                }
                for endpoint_id, (retry_at, error) in self._failures.items()
                if retry_at > monotonic_now
            },
        }
        temporary = self._state_path.with_suffix(self._state_path.suffix + ".tmp")
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
            temporary.chmod(0o600)
            temporary.replace(self._state_path)
        except OSError:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def blocked(self, endpoint_id: str) -> LlmError | None:
        with self._lock:
            failure = self._failures.get(endpoint_id)
            if failure is None:
                return None
            retry_at, error = failure
            if self._clock() >= retry_at:
                self._failures.pop(endpoint_id, None)
                self._persist_locked()
                return None
            return LlmError(
                f"endpoint is cooling down after {error.category}",
                category=error.category,
                status_code=error.status_code,
            )

    def failed(self, endpoint_id: str, error: LlmError) -> None:
        seconds = _FAILURE_COOLDOWNS.get(error.category, 10.0)
        with self._lock:
            self._failures[endpoint_id] = (self._clock() + seconds, error)
            self._persist_locked()

    def succeeded(self, endpoint_id: str) -> None:
        with self._lock:
            self._failures.pop(endpoint_id, None)
            self._persist_locked()

    def snapshot(self) -> dict[str, dict[str, object]]:
        now = self._clock()
        with self._lock:
            expired = [
                endpoint_id
                for endpoint_id, (retry_at, _) in self._failures.items()
                if now >= retry_at
            ]
            for endpoint_id in expired:
                self._failures.pop(endpoint_id, None)
            if expired:
                self._persist_locked()
            return {
                endpoint_id: {
                    "category": error.category,
                    "status_code": error.status_code,
                    "retry_in_seconds": max(1, int(retry_at - now + 0.999)),
                }
                for endpoint_id, (retry_at, error) in self._failures.items()
            }


@dataclass
class LlmResult:
    text: str
    tool_calls: list[dict[str, object]] = field(default_factory=list)
    raw: dict[str, object] = field(default_factory=dict)


LLM_USAGE_FIELDS = (
    "requests",
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "total_tokens",
)


def _usage_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def normalize_llm_usage(raw: dict[str, object]) -> dict[str, int]:
    """Normalize Responses and Chat Completions token metadata."""
    usage = raw.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    input_details = usage.get("input_tokens_details")
    if not isinstance(input_details, dict):
        input_details = usage.get("prompt_tokens_details")
    input_details = input_details if isinstance(input_details, dict) else {}
    output_details = usage.get("output_tokens_details")
    if not isinstance(output_details, dict):
        output_details = usage.get("completion_tokens_details")
    output_details = output_details if isinstance(output_details, dict) else {}
    input_tokens = _usage_int(usage.get("input_tokens", usage.get("prompt_tokens")))
    output_tokens = _usage_int(
        usage.get("output_tokens", usage.get("completion_tokens"))
    )
    return {
        "requests": 1 if raw else 0,
        "input_tokens": input_tokens,
        "cached_input_tokens": _usage_int(input_details.get("cached_tokens")),
        "output_tokens": output_tokens,
        "reasoning_tokens": _usage_int(output_details.get("reasoning_tokens")),
        "total_tokens": _usage_int(usage.get("total_tokens"))
        or input_tokens + output_tokens,
    }


def add_llm_usage(total: dict[str, int], usage: dict[str, int]) -> None:
    for field_name in LLM_USAGE_FIELDS:
        total[field_name] = total.get(field_name, 0) + _usage_int(usage.get(field_name))


def _is_local(base_url: str) -> bool:
    return "localhost" in base_url or "127.0.0.1" in base_url


def _urllib_transport(
    url: str, headers: dict[str, str], body: dict[str, object]
) -> dict[str, object]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise LlmError(
            f"LLM request failed ({exc.code}): {detail}",
            category=llm_error_category(exc.code, detail),
            status_code=exc.code,
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise LlmError(
            f"LLM request failed: {exc}", category="provider_unavailable"
        ) from exc


def _result_from_chat(raw: dict[str, object]) -> LlmResult:
    choices = raw.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LlmError("LLM response had no choices")
    message = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
    text = str(message.get("content") or "")
    tool_calls = message.get("tool_calls") or []
    if not isinstance(tool_calls, list):
        tool_calls = []
    return LlmResult(text=text, tool_calls=tool_calls, raw=raw)


def loads_json_object(text: str) -> dict[str, object]:
    """Parse a JSON object from a model reply, tolerating ``` fences."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1] if "\n" in cleaned else cleaned
        cleaned = cleaned.removeprefix("json").strip()
        if cleaned.endswith("```"):
            cleaned = cleaned[: cleaned.rfind("```")].strip()
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise LlmError(f"LLM reply was not valid JSON: {text[:200]}") from exc
    if not isinstance(parsed, dict):
        raise LlmError("LLM reply JSON must be an object")
    return parsed


class LlmClient:
    def __init__(
        self,
        config: LlmConfig,
        *,
        env: dict[str, str] | None = None,
        transport: Transport | None = None,
        clock: Clock | None = None,
        wall_clock: Clock | None = None,
        failure_state_path: str | Path | None = None,
    ) -> None:
        self.config = config
        self._env = env if env is not None else dict(os.environ)
        self._transport = transport or _urllib_transport
        self._cooldown = LlmFailureCooldown(
            clock=clock,
            wall_clock=wall_clock,
            state_path=failure_state_path,
        )

    def _endpoints(self) -> list[LlmEndpoint]:
        endpoints = [
            LlmEndpoint(
                self.config.base_url,
                self.config.model,
                self.config.api_key_env,
                self.config.provider,
            )
        ]
        if self.config.fallback is not None:
            endpoints.append(self.config.fallback)
        endpoints.extend(self.config.fallbacks)
        unique: list[LlmEndpoint] = []
        seen: set[tuple[str, str, str]] = set()
        for endpoint in endpoints:
            endpoint = LlmEndpoint(
                base_url=Template(endpoint.base_url).safe_substitute(self._env),
                model=Template(endpoint.model).safe_substitute(self._env),
                api_key_env=endpoint.api_key_env,
                provider=endpoint.provider,
            )
            identity = (
                endpoint.base_url.rstrip("/"),
                endpoint.model,
                endpoint.api_key_env,
            )
            if identity not in seen:
                unique.append(endpoint)
                seen.add(identity)
        return unique

    @staticmethod
    def _endpoint_id(endpoint: LlmEndpoint) -> str:
        return f"{endpoint.base_url.rstrip('/')}|{endpoint.model}|{endpoint.api_key_env}"

    def provider_failures(self) -> list[dict[str, object]]:
        failures = self._cooldown.snapshot()
        return [
            {
                "provider": endpoint.provider,
                "model": endpoint.model,
                **failure,
            }
            for index, endpoint in enumerate(self._endpoints())
            if (failure := failures.get(self._endpoint_id(endpoint))) is not None
        ]

    def chat(
        self,
        *,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]] | None = None,
        response_format: dict[str, object] | None = None,
    ) -> LlmResult:
        errors: list[str] = []
        failures: list[LlmError] = []
        for endpoint in self._endpoints():
            endpoint_id = self._endpoint_id(endpoint)
            if blocked := self._cooldown.blocked(endpoint_id):
                errors.append(f"{endpoint.model}: {blocked}")
                failures.append(blocked)
                continue
            try:
                result = self._chat_one(endpoint, messages, tools, response_format)
                self._cooldown.succeeded(endpoint_id)
                return result
            except LlmError as exc:
                self._cooldown.failed(endpoint_id, exc)
                errors.append(f"{endpoint.model}: {exc}")
                failures.append(exc)
        category_order = (
            "quota_exhausted",
            "rate_limited",
            "authentication",
            "provider_unavailable",
            "model_error",
        )
        category = next(
            (
                candidate
                for candidate in category_order
                if any(failure.category == candidate for failure in failures)
            ),
            "model_error",
        )
        status_code = next(
            (
                failure.status_code
                for failure in failures
                if failure.category == category and failure.status_code is not None
            ),
            None,
        )
        raise LlmError(
            "all LLM endpoints failed -> " + " | ".join(errors),
            category=category,
            status_code=status_code,
        )

    def _chat_one(
        self,
        endpoint: LlmEndpoint,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]] | None,
        response_format: dict[str, object] | None,
    ) -> LlmResult:
        key = self._env.get(endpoint.api_key_env) if endpoint.api_key_env else None
        if not key and not _is_local(endpoint.base_url):
            raise LlmError(
                f"{endpoint.api_key_env or 'API key'} is not set",
                category="authentication",
            )
        headers = {"content-type": "application/json"}
        if key:
            headers["authorization"] = f"Bearer {key}"
        body: dict[str, object] = {
            "model": endpoint.model,
            "messages": messages,
            "temperature": 0,
        }
        if tools:
            body["tools"] = tools
        if response_format:
            body["response_format"] = response_format
        url = endpoint.base_url.rstrip("/") + "/chat/completions"
        raw = self._transport(url, headers, body)
        return _result_from_chat(raw)

    def complete_json(
        self,
        *,
        system: str,
        user_text: str,
        images: list[str] | None = None,
    ) -> dict[str, object]:
        """Ask for a single JSON object, optionally with images attached.

        Content is a plain string when there are no images (friendliest to small
        local text models) and an array of parts when a photo is attached.
        """
        if images:
            content: object = [{"type": "text", "text": user_text}] + [
                {"type": "image_url", "image_url": {"url": url}} for url in images
            ]
        else:
            content = user_text
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": content},
        ]
        result = self.chat(
            messages=messages,
            response_format={"type": "json_object"},
        )
        return loads_json_object(result.text)
