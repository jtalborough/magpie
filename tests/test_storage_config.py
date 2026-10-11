import json

import pytest

from searchable_storage_led.config import (
    AuthConfig,
    EdgeConfig,
    LedConfig,
    StorageConfig,
)


def test_storage_config_defaults_to_sqlite() -> None:
    config = LedConfig()

    assert config.storage == StorageConfig()
    assert config.edge == EdgeConfig()
    assert config.auth == AuthConfig()


def test_software_only_runtime_allows_zero_pixels() -> None:
    config = LedConfig(pixel_count=0, backend="none").validated()

    assert config.pixel_count == 0

    with pytest.raises(ValueError, match="pixel_count must be positive"):
        LedConfig(pixel_count=0, backend="wled", wled_url="http://wled").validated()


def test_only_central_edge_mode_requires_a_target_device() -> None:
    assert LedConfig(edge=EdgeConfig(mode="local", target_device_id="")).validated()

    with pytest.raises(ValueError, match="required in central mode"):
        LedConfig(edge=EdgeConfig(mode="central", target_device_id="")).validated()

    with pytest.raises(ValueError, match="requires multi-workspace auth"):
        LedConfig(edge=EdgeConfig(mode="workspace")).validated()


def test_edge_config_loads_and_reads_device_tokens(tmp_path, monkeypatch) -> None:
    path = tmp_path / "led.json"
    path.write_text(
        json.dumps(
            {
                "backend": "none",
                "edge": {
                    "mode": "central",
                    "target_device_id": "cabinet-7",
                    "machine_url": "https://edge.magpie.example/",
                    "tokens_env": "EDGE_TEST_TOKENS",
                    "command_ttl_seconds": 45,
                    "claim_timeout_seconds": 9,
                    "max_attempts": 2,
                },
            }
        )
    )
    monkeypatch.setenv("EDGE_TEST_TOKENS", json.dumps({"cabinet-7": "secret"}))

    edge = LedConfig.from_path(path).edge

    assert edge.mode == "central"
    assert edge.target_device_id == "cabinet-7"
    assert edge.machine_url == "https://edge.magpie.example"
    assert edge.tokens() == {"cabinet-7": "secret"}

    with pytest.raises(ValueError, match="edge.machine_url must use HTTPS"):
        LedConfig(edge=EdgeConfig(machine_url="http://edge.local")).validated()


def test_supabase_auth_config_loads_from_json(tmp_path, monkeypatch) -> None:
    path = tmp_path / "led.json"
    path.write_text(
        json.dumps(
            {
                "backend": "none",
                "auth": {
                    "mode": "supabase",
                    "supabase_url": "https://project.supabase.co/",
                    "publishable_key_env": "MAGPIE_PUBLIC_KEY",
                    "service_key_env": "MAGPIE_SERVICE_KEY",
                    "workspace_id": "workspace-1",
                    "cache_seconds": 12,
                },
            }
        )
    )
    monkeypatch.setenv(
        "MAGPIE_PUBLIC_KEY",
        "sb_publishable_1234567890abcdefghij_12345678",
    )

    auth = LedConfig.from_path(path).auth

    assert auth == AuthConfig(
        mode="supabase",
        supabase_url="https://project.supabase.co",
        publishable_key_env="MAGPIE_PUBLIC_KEY",
        service_key_env="MAGPIE_SERVICE_KEY",
        workspace_id="workspace-1",
        cache_seconds=12,
    )
    assert (
        auth.publishable_key()
        == "sb_publishable_1234567890abcdefghij_12345678"
    )


def test_supabase_auth_config_rejects_invalid_publishable_key(
    monkeypatch,
) -> None:
    auth = AuthConfig(
        mode="supabase",
        supabase_url="https://project.supabase.co",
        publishable_key_env="MAGPIE_PUBLIC_KEY",
        service_key_env="MAGPIE_SERVICE_KEY",
        workspace_id="workspace-1",
    )
    monkeypatch.setenv("MAGPIE_PUBLIC_KEY", "unrelated notification text")

    with pytest.raises(ValueError, match="MAGPIE_PUBLIC_KEY"):
        auth.publishable_key()


def test_multi_workspace_config_requires_hardware_neutral_supabase_service() -> None:
    auth = AuthConfig(
        mode="supabase",
        supabase_url="https://project.supabase.co",
        multi_workspace=True,
    )
    storage = StorageConfig(
        backend="supabase",
        supabase_url="https://project.supabase.co",
        workspace_id="",
    )

    config = LedConfig(
        pixel_count=0,
        backend="none",
        storage=storage,
        auth=auth,
    ).validated()

    assert config.auth.multi_workspace is True
    assert config.auth.workspace_id == ""
    assert config.storage.workspace_id == ""

    workspace_config = LedConfig(
        pixel_count=0,
        backend="none",
        storage=storage,
        auth=auth,
        edge=EdgeConfig(mode="workspace"),
    ).validated()
    assert workspace_config.edge.mode == "workspace"

    with pytest.raises(ValueError, match="hardware-neutral"):
        LedConfig(
            pixel_count=1,
            backend="wled",
            wled_url="http://wled.local",
            storage=storage,
            auth=auth,
        ).validated()


def test_multi_workspace_config_cannot_pin_one_workspace() -> None:
    with pytest.raises(ValueError, match="must not pin"):
        LedConfig(
            pixel_count=0,
            backend="none",
            storage=StorageConfig(
                backend="supabase",
                supabase_url="https://project.supabase.co",
                workspace_id="workspace-1",
            ),
            auth=AuthConfig(
                mode="supabase",
                supabase_url="https://project.supabase.co",
                workspace_id="workspace-1",
                multi_workspace=True,
            ),
        ).validated()


@pytest.mark.parametrize(
    ("auth", "message"),
    [
        (AuthConfig(mode="other"), "auth.mode"),
        (AuthConfig(cache_seconds=-1), "cache_seconds"),
        (AuthConfig(mode="supabase"), "supabase_url"),
        (
            AuthConfig(
                mode="supabase",
                supabase_url="https://project.supabase.co",
                workspace_id="workspace-1",
                publishable_key_env="",
            ),
            "publishable_key_env",
        ),
        (
            AuthConfig(
                mode="supabase",
                supabase_url="https://project.supabase.co",
                workspace_id="workspace-1",
                service_key_env="",
            ),
            "service_key_env",
        ),
        (
            AuthConfig(
                mode="supabase",
                supabase_url="https://project.supabase.co",
            ),
            "workspace_id",
        ),
    ],
)
def test_invalid_auth_config_is_rejected(auth: AuthConfig, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        LedConfig(auth=auth).validated()


@pytest.mark.parametrize(
    ("auth", "message"),
    [
        (
            AuthConfig(
                mode="supabase",
                supabase_url="https://auth.supabase.co",
                workspace_id="workspace-1",
            ),
            "auth.supabase_url",
        ),
        (
            AuthConfig(
                mode="supabase",
                supabase_url="https://storage.supabase.co",
                workspace_id="workspace-2",
            ),
            "auth.workspace_id",
        ),
    ],
)
def test_auth_must_protect_the_configured_storage_workspace(
    auth: AuthConfig, message: str
) -> None:
    storage = StorageConfig(
        backend="supabase",
        supabase_url="https://storage.supabase.co",
        workspace_id="workspace-1",
    )

    with pytest.raises(ValueError, match=message):
        LedConfig(storage=storage, auth=auth).validated()


def test_supabase_storage_config_loads_from_json(tmp_path) -> None:
    path = tmp_path / "led.json"
    path.write_text(
        json.dumps(
            {
                "backend": "wled",
                "wled_url": "http://wled.local",
                "storage": {
                    "backend": "supabase",
                    "supabase_url": "https://project.supabase.co/",
                    "api_key_env": "MAGPIE_SUPABASE_KEY",
                    "workspace_id": "workspace-1",
                    "photo_bucket": "cabinet-photos",
                    "timeout_seconds": 6,
                },
            }
        ),
        encoding="utf-8",
    )

    storage = LedConfig.from_path(path).storage

    assert storage == StorageConfig(
        backend="supabase",
        supabase_url="https://project.supabase.co",
        api_key_env="MAGPIE_SUPABASE_KEY",
        workspace_id="workspace-1",
        photo_bucket="cabinet-photos",
        timeout_seconds=6,
    )


@pytest.mark.parametrize(
    ("storage", "message"),
    [
        (StorageConfig(backend="other"), "backend"),
        (StorageConfig(timeout_seconds=0), "timeout"),
        (StorageConfig(photo_bucket=""), "photo_bucket"),
        (StorageConfig(backend="supabase"), "supabase_url"),
        (
            StorageConfig(
                backend="supabase",
                supabase_url="https://project.supabase.co",
                api_key_env="",
                workspace_id="workspace-1",
            ),
            "api_key_env",
        ),
        (
            StorageConfig(
                backend="supabase",
                supabase_url="https://project.supabase.co",
            ),
            "workspace_id",
        ),
    ],
)
def test_invalid_storage_config_is_rejected(storage: StorageConfig, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        LedConfig(storage=storage).validated()
