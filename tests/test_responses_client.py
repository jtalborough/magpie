import json

import pytest

from searchable_storage_led.config import LlmConfig
from searchable_storage_led.llm_client import LlmClient, LlmError
from searchable_storage_led.responses_client import OpenAIResponsesClient


def test_responses_client_maps_tools_images_and_function_calls() -> None:
    captured: dict[str, object] = {}

    def transport(url, headers, body):
        captured.update(url=url, headers=headers, body=body)
        return {
            "id": "resp_1",
            "object": "response",
            "status": "completed",
            "output": [
                {"id": "rs_1", "type": "reasoning", "content": []},
                {
                    "id": "fc_1",
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "search_items",
                    "arguments": '{"query":"M4 T-nuts"}',
                },
            ],
        }

    client = OpenAIResponsesClient(
        model="gpt-5.6",
        env={"OPENAI_API_KEY": "secret"},
        transport=transport,
    )
    result = client.chat(
        messages=[
            {"role": "system", "content": "Manage storage."},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Find this."},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/jpeg;base64,AAA"},
                    },
                ],
            },
        ],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "search_items",
                    "description": "Search inventory.",
                    "parameters": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                    },
                },
            }
        ],
    )

    assert captured["url"] == "https://api.openai.com/v1/responses"
    assert captured["headers"]["authorization"] == "Bearer secret"
    body = captured["body"]
    assert body["model"] == "gpt-5.6"
    assert body["instructions"] == "Manage storage."
    assert body["input"][0]["content"][1] == {
        "type": "input_image",
        "image_url": "data:image/jpeg;base64,AAA",
        "detail": "auto",
    }
    assert body["tools"][0]["name"] == "search_items"
    assert result.tool_calls[0]["id"] == "call_1"
    assert result.tool_calls[0]["function"]["name"] == "search_items"
    assert result.raw["output"][0]["type"] == "reasoning"


def test_responses_client_defaults_to_high_volume_model_and_bounded_output() -> None:
    captured: dict[str, object] = {}

    def transport(url, headers, body):
        captured["body"] = body
        return {
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "done"}],
                }
            ]
        }

    OpenAIResponsesClient(
        env={"OPENAI_API_KEY": "secret"}, transport=transport
    ).chat(messages=[{"role": "user", "content": "hello"}])

    assert captured["body"]["model"] == "gpt-5.6-luna"
    assert captured["body"]["max_output_tokens"] == 1200


def test_responses_client_replays_reasoning_and_tool_output() -> None:
    captured: dict[str, object] = {}

    def transport(url, headers, body):
        captured["body"] = body
        return {
            "id": "resp_2",
            "object": "response",
            "status": "completed",
            "output": [
                {
                    "id": "msg_2",
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {"type": "output_text", "text": "It is in B010."}
                    ],
                }
            ],
        }

    previous_output = [
        {"id": "rs_1", "type": "reasoning", "content": []},
        {
            "id": "fc_1",
            "type": "function_call",
            "call_id": "call_1",
            "name": "search_items",
            "arguments": '{"query":"M4 T-nuts"}',
        },
    ]
    result = OpenAIResponsesClient(
        env={"OPENAI_API_KEY": "secret"},
        transport=transport,
    ).chat(
        messages=[
            {"role": "system", "content": "Manage storage."},
            {"role": "user", "content": "Find M4 T-nuts."},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [],
                "responses_output": previous_output,
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "content": json.dumps([{"location_id": "B010"}]),
            },
        ]
    )

    inputs = captured["body"]["input"]
    assert inputs[1:3] == previous_output
    assert inputs[3]["type"] == "function_call_output"
    assert inputs[3]["call_id"] == "call_1"
    assert result.text == "It is in B010."


def test_responses_client_uses_configured_fallback_without_openai_key() -> None:
    def fallback_transport(url, headers, body):
        return {"choices": [{"message": {"content": "fallback"}}]}

    fallback = LlmClient(
        LlmConfig(
            base_url="http://localhost:11434/v1",
            model="local",
            api_key_env="",
        ),
        env={},
        transport=fallback_transport,
    )
    client = OpenAIResponsesClient(env={}, fallback=fallback)

    result = client.chat(messages=[{"role": "user", "content": "hello"}])

    assert client.ready is False
    assert result.text == "fallback"


def test_responses_client_cools_down_failed_primary_and_keeps_quota_category() -> None:
    now = [10.0]
    primary_calls = 0
    fallback_calls = 0

    def primary_transport(url, headers, body):
        nonlocal primary_calls
        primary_calls += 1
        raise LlmError(
            "quota exhausted", category="quota_exhausted", status_code=429
        )

    def fallback_transport(url, headers, body):
        nonlocal fallback_calls
        fallback_calls += 1
        raise LlmError("bad key", category="authentication", status_code=401)

    fallback = LlmClient(
        LlmConfig(
            base_url="https://fallback.example/v1",
            model="fallback",
            api_key_env="FALLBACK_KEY",
        ),
        env={"FALLBACK_KEY": "bad"},
        transport=fallback_transport,
        clock=lambda: now[0],
    )
    client = OpenAIResponsesClient(
        env={"OPENAI_API_KEY": "empty"},
        fallback=fallback,
        transport=primary_transport,
        clock=lambda: now[0],
    )

    for _ in range(2):
        with pytest.raises(LlmError) as raised:
            client.chat(messages=[{"role": "user", "content": "hello"}])
        assert raised.value.category == "quota_exhausted"

    assert primary_calls == 1
    assert fallback_calls == 1
    assert [failure["category"] for failure in client.provider_failures()] == [
        "quota_exhausted",
        "authentication",
    ]


def test_responses_client_restores_primary_cooldown_after_restart(tmp_path) -> None:
    monotonic = [10.0]
    wall = [1000.0]
    primary_calls = 0
    state_path = tmp_path / "openai-failures.json"

    def primary_transport(url, headers, body):
        nonlocal primary_calls
        primary_calls += 1
        raise LlmError(
            "quota exhausted", category="quota_exhausted", status_code=429
        )

    first = OpenAIResponsesClient(
        env={"OPENAI_API_KEY": "secret"},
        transport=primary_transport,
        clock=lambda: monotonic[0],
        wall_clock=lambda: wall[0],
        failure_state_path=state_path,
    )
    with pytest.raises(LlmError):
        first.chat(messages=[])

    restarted = OpenAIResponsesClient(
        env={"OPENAI_API_KEY": "secret"},
        transport=primary_transport,
        clock=lambda: monotonic[0],
        wall_clock=lambda: wall[0],
        failure_state_path=state_path,
    )
    with pytest.raises(LlmError) as blocked:
        restarted.chat(messages=[])

    assert blocked.value.category == "quota_exhausted"
    assert primary_calls == 1
    assert restarted.provider_failures()[0]["retry_in_seconds"] == 21600
    assert state_path.stat().st_mode & 0o777 == 0o600


def test_responses_client_complete_json_supports_images() -> None:
    captured: dict[str, object] = {}

    def transport(url, headers, body):
        captured["body"] = body
        return {
            "output": [
                {
                    "type": "message",
                    "content": [
                        {"type": "output_text", "text": '{"length_mm": 75}'},
                    ],
                }
            ]
        }

    result = OpenAIResponsesClient(
        env={"OPENAI_API_KEY": "secret"},
        transport=transport,
    ).complete_json(
        system="Measure the item.",
        user_text="blue pry tool",
        images=["data:image/jpeg;base64,AAA"],
    )

    assert result == {"length_mm": 75}
    inputs = captured["body"]["input"]
    assert inputs[0]["content"][1]["type"] == "input_image"
