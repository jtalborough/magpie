import json

from searchable_storage_led.config import LedConfig


def test_default_llm_is_openrouter() -> None:
    cfg = LedConfig()
    assert cfg.agent_max_steps == 4
    assert cfg.llm.provider == "openrouter"
    assert cfg.llm.base_url == "https://openrouter.ai/api/v1"
    assert cfg.llm.model == "google/gemini-3.1-flash-lite"
    assert cfg.llm.api_key_env == "OPENROUTER_API_KEY"
    assert cfg.llm.fallback is None
    assert cfg.llm.fallbacks == ()


def test_from_path_parses_llm_block_with_fallback(tmp_path) -> None:
    path = tmp_path / "cfg.json"
    path.write_text(
        json.dumps(
            {
                "backend": "wled",
                "wled_url": "http://controller",
                "agent_max_steps": 3,
                "llm": {
                    "provider": "openrouter",
                    "base_url": "https://openrouter.ai/api/v1",
                    "model": "google/gemini-3.1-flash-lite",
                    "api_key_env": "OPENROUTER_API_KEY",
                    "fallback": {
                        "base_url": "http://localhost:11434/v1",
                        "model": "qwen2.5:3b-instruct-q4_K_M",
                        "api_key_env": "",
                    },
                    "fallbacks": [
                        {
                            "provider": "cloudflare",
                            "base_url": "https://compatible.example/v1",
                            "model": "small-model",
                            "api_key_env": "COMPATIBLE_API_KEY",
                        }
                    ],
                },
            }
        )
    )

    cfg = LedConfig.from_path(path)

    assert cfg.agent_max_steps == 3
    assert cfg.llm.model == "google/gemini-3.1-flash-lite"
    assert cfg.llm.fallback is not None
    assert cfg.llm.fallback.model == "qwen2.5:3b-instruct-q4_K_M"
    assert cfg.llm.fallback.base_url == "http://localhost:11434/v1"
    assert len(cfg.llm.fallbacks) == 1
    assert cfg.llm.fallbacks[0].model == "small-model"
    assert cfg.llm.fallbacks[0].provider == "cloudflare"


def test_agent_max_steps_is_bounded() -> None:
    import pytest

    with pytest.raises(ValueError, match="agent_max_steps"):
        LedConfig(agent_max_steps=0).validated()
    with pytest.raises(ValueError, match="agent_max_steps"):
        LedConfig(agent_max_steps=9).validated()


def test_repo_config_loads_llm_block() -> None:
    cfg = LedConfig.from_path("config/led-controller.json")
    assert cfg.assistant_backend == "openai"
    assert cfg.llm.provider == "openrouter"
    assert cfg.llm.fallback is not None
    assert "11434" in cfg.llm.fallback.base_url
    assert cfg.zebra.host == "192.0.2.108"
    assert cfg.zebra.port == 9100
    assert cfg.zebra.width_dots == 1200
    assert cfg.zebra.dpi == 300
