import json

import pytest

from searchable_storage_led.config import LlmConfig, LlmEndpoint
from searchable_storage_led.llm_client import (
    LlmClient,
    LlmError,
    llm_error_category,
    loads_json_object,
    normalize_llm_usage,
)


def _chat_reply(obj: dict) -> dict:
    return {"choices": [{"message": {"content": json.dumps(obj)}}]}


def test_complete_json_parses_content_and_sends_auth() -> None:
    captured: dict = {}

    def transport(url, headers, body):
        captured.update(url=url, headers=headers, body=body)
        return _chat_reply({"action": "find_item", "query": "m3"})

    cfg = LlmConfig(base_url="https://openrouter.ai/api/v1", model="m", api_key_env="OR_KEY")
    client = LlmClient(cfg, env={"OR_KEY": "secret"}, transport=transport)

    out = client.complete_json(system="sys", user_text="hi")

    assert out["action"] == "find_item"
    assert captured["url"].endswith("/chat/completions")
    assert captured["headers"]["authorization"] == "Bearer secret"
    assert captured["body"]["model"] == "m"
    # No images -> plain string content (friendliest to small local text models).
    assert captured["body"]["messages"][1]["content"] == "hi"


def test_images_become_content_parts() -> None:
    captured: dict = {}

    def transport(url, headers, body):
        captured["body"] = body
        return _chat_reply({"action": "store_item"})

    client = LlmClient(LlmConfig(api_key_env="OR_KEY"), env={"OR_KEY": "x"}, transport=transport)
    client.complete_json(system="s", user_text="t", images=["data:image/png;base64,AAA"])

    content = captured["body"]["messages"][1]["content"]
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "t"}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png")


def test_missing_key_for_remote_raises() -> None:
    client = LlmClient(
        LlmConfig(base_url="https://openrouter.ai/api/v1", api_key_env="OR_KEY"),
        env={},
        transport=lambda *a: _chat_reply({}),
    )
    with pytest.raises(LlmError) as raised:
        client.complete_json(system="s", user_text="t")
    assert raised.value.category == "authentication"


@pytest.mark.parametrize(
    ("status_code", "detail", "category"),
    [
        (401, "user not found", "authentication"),
        (402, "payment required", "quota_exhausted"),
        (429, "too many requests", "rate_limited"),
        (429, '{"code":"insufficient_quota"}', "quota_exhausted"),
        (503, "upstream offline", "provider_unavailable"),
        (400, "bad request", "model_error"),
        (None, "quota exhausted", "quota_exhausted"),
    ],
)
def test_llm_error_category_is_structured(status_code, detail, category) -> None:
    assert llm_error_category(status_code, detail) == category


def test_fallback_failure_preserves_actionable_category() -> None:
    def transport(url, headers, body):
        category = "authentication" if body["model"] == "primary" else "rate_limited"
        raise LlmError("failed", category=category)

    client = LlmClient(
        LlmConfig(
            model="primary",
            api_key_env="PRIMARY_KEY",
            fallback=LlmEndpoint("https://fallback.example/v1", "fallback", "FALLBACK_KEY"),
        ),
        env={"PRIMARY_KEY": "x", "FALLBACK_KEY": "y"},
        transport=transport,
    )

    with pytest.raises(LlmError) as raised:
        client.chat(messages=[{"role": "user", "content": "hello"}])

    assert raised.value.category == "rate_limited"


def test_local_endpoint_needs_no_key() -> None:
    def transport(url, headers, body):
        assert "authorization" not in headers  # no key sent to a local model
        return _chat_reply({"ok": True})

    cfg = LlmConfig(
        provider="local",
        base_url="http://localhost:11434/v1",
        model="qwen",
        api_key_env="",
    )
    out = LlmClient(cfg, env={}, transport=transport).complete_json(system="s", user_text="t")
    assert out["ok"] is True


def test_falls_back_to_secondary_endpoint() -> None:
    calls: list[str] = []

    def transport(url, headers, body):
        calls.append(body["model"])
        if body["model"] == "primary":
            raise LlmError("boom")
        return _chat_reply({"served_by": body["model"]})

    cfg = LlmConfig(
        base_url="https://openrouter.ai/api/v1",
        model="primary",
        api_key_env="OR_KEY",
        fallback=LlmEndpoint("http://localhost:11434/v1", "local", ""),
    )
    out = LlmClient(cfg, env={"OR_KEY": "x"}, transport=transport).complete_json(
        system="s", user_text="t"
    )

    assert out["served_by"] == "local"
    assert calls == ["primary", "local"]


def test_ordered_fallbacks_skip_duplicate_endpoints() -> None:
    calls: list[str] = []

    def transport(url, headers, body):
        calls.append(body["model"])
        if body["model"] != "third":
            raise LlmError("offline", category="provider_unavailable")
        return _chat_reply({"served_by": "third"})

    duplicate = LlmEndpoint("https://second.example/v1", "second", "SECOND_KEY")
    client = LlmClient(
        LlmConfig(
            base_url="https://first.example/v1",
            model="first",
            api_key_env="FIRST_KEY",
            fallback=duplicate,
            fallbacks=(
                duplicate,
                LlmEndpoint("https://third.example/v1", "third", "THIRD_KEY"),
            ),
        ),
        env={"FIRST_KEY": "1", "SECOND_KEY": "2", "THIRD_KEY": "3"},
        transport=transport,
    )

    result = client.chat(messages=[{"role": "user", "content": "hello"}])

    assert result.text
    assert calls == ["first", "second", "third"]


def test_endpoint_url_expands_environment_values() -> None:
    captured: dict[str, str] = {}

    def transport(url, headers, body):
        captured["url"] = url
        return _chat_reply({"ok": True})

    client = LlmClient(
        LlmConfig(
            base_url="https://api.example/accounts/${ACCOUNT_ID}/ai/v1",
            model="model",
            api_key_env="KEY",
        ),
        env={"KEY": "secret", "ACCOUNT_ID": "account-123"},
        transport=transport,
    )

    client.chat(messages=[])

    assert captured["url"] == (
        "https://api.example/accounts/account-123/ai/v1/chat/completions"
    )


def test_failed_endpoint_cools_down_then_recovers() -> None:
    now = [10.0]
    calls: list[str] = []

    def transport(url, headers, body):
        calls.append(body["model"])
        if len(calls) == 1:
            raise LlmError("quota exhausted", category="quota_exhausted", status_code=429)
        return _chat_reply({"ok": True})

    client = LlmClient(
        LlmConfig(
            base_url="https://first.example/v1",
            model="first",
            api_key_env="KEY",
        ),
        env={"KEY": "secret"},
        transport=transport,
        clock=lambda: now[0],
    )

    with pytest.raises(LlmError) as first:
        client.chat(messages=[])
    with pytest.raises(LlmError) as second:
        client.chat(messages=[])
    assert first.value.category == second.value.category == "quota_exhausted"
    assert calls == ["first"]

    failure = client.provider_failures()[0]
    assert failure["category"] == "quota_exhausted"
    assert failure["retry_in_seconds"] == 21600

    now[0] += 21601
    assert client.chat(messages=[]).text
    assert calls == ["first", "first"]
    assert client.provider_failures() == []


def test_failure_cooldown_survives_client_restart(tmp_path) -> None:
    monotonic = [10.0]
    wall = [1000.0]
    state_path = tmp_path / "provider-failures.json"
    calls = 0

    def failing_transport(url, headers, body):
        nonlocal calls
        calls += 1
        raise LlmError("quota exhausted", category="quota_exhausted", status_code=429)

    config = LlmConfig(
        base_url="https://provider.example/v1",
        model="model",
        api_key_env="KEY",
    )
    first = LlmClient(
        config,
        env={"KEY": "secret"},
        transport=failing_transport,
        clock=lambda: monotonic[0],
        wall_clock=lambda: wall[0],
        failure_state_path=state_path,
    )
    with pytest.raises(LlmError):
        first.chat(messages=[])

    restarted = LlmClient(
        config,
        env={"KEY": "secret"},
        transport=failing_transport,
        clock=lambda: monotonic[0],
        wall_clock=lambda: wall[0],
        failure_state_path=state_path,
    )
    with pytest.raises(LlmError) as blocked:
        restarted.chat(messages=[])

    assert blocked.value.category == "quota_exhausted"
    assert calls == 1
    assert restarted.provider_failures()[0]["retry_in_seconds"] == 21600


def test_success_clears_persisted_failure_state(tmp_path) -> None:
    state_path = tmp_path / "provider-failures.json"
    now = [10.0]
    wall = [1000.0]
    attempts = 0

    def transport(url, headers, body):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise LlmError("offline", category="provider_unavailable", status_code=503)
        return _chat_reply({"ok": True})

    config = LlmConfig(api_key_env="KEY")
    client = LlmClient(
        config,
        env={"KEY": "secret"},
        transport=transport,
        clock=lambda: now[0],
        wall_clock=lambda: wall[0],
        failure_state_path=state_path,
    )
    with pytest.raises(LlmError):
        client.chat(messages=[])
    now[0] += 11
    wall[0] += 11
    assert client.chat(messages=[]).text

    restarted = LlmClient(
        config,
        env={"KEY": "secret"},
        transport=transport,
        clock=lambda: now[0],
        wall_clock=lambda: wall[0],
        failure_state_path=state_path,
    )
    assert restarted.provider_failures() == []


def test_loads_json_object_tolerates_fences() -> None:
    assert loads_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert loads_json_object('{"b": 2}') == {"b": 2}
    with pytest.raises(LlmError):
        loads_json_object("not json at all")


def test_normalize_chat_completion_usage() -> None:
    usage = normalize_llm_usage(
        {
            "choices": [],
            "usage": {
                "prompt_tokens": 120,
                "prompt_tokens_details": {"cached_tokens": 80},
                "completion_tokens": 35,
                "completion_tokens_details": {"reasoning_tokens": 12},
                "total_tokens": 155,
            },
        }
    )

    assert usage == {
        "requests": 1,
        "input_tokens": 120,
        "cached_input_tokens": 80,
        "output_tokens": 35,
        "reasoning_tokens": 12,
        "total_tokens": 155,
    }


def test_normalize_responses_usage() -> None:
    usage = normalize_llm_usage(
        {
            "object": "response",
            "usage": {
                "input_tokens": 200,
                "input_tokens_details": {"cached_tokens": 150},
                "output_tokens": 40,
                "output_tokens_details": {"reasoning_tokens": 18},
                "total_tokens": 240,
            },
        }
    )

    assert usage["requests"] == 1
    assert usage["input_tokens"] == 200
    assert usage["cached_input_tokens"] == 150
    assert usage["output_tokens"] == 40
    assert usage["reasoning_tokens"] == 18
    assert usage["total_tokens"] == 240
