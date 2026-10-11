from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .supabase_keys import require_supabase_publishable_key


DEFAULT_CONFIG_PATHS = (
    Path("config/led-controller.json"),
    Path("/etc/searchable-storage/led-controller.json"),
)


@dataclass(frozen=True)
class LlmEndpoint:
    """An OpenAI-compatible chat-completions endpoint (OpenRouter, Ollama, ...)."""

    base_url: str
    model: str
    api_key_env: str = ""
    provider: str = "compatible"


@dataclass(frozen=True)
class LlmConfig:
    """Which model answers. Defaults to OpenRouter; one OpenAI-compatible client
    serves OpenRouter, a local Ollama/llama.cpp, or any compatible host by URL."""

    provider: str = "openrouter"
    base_url: str = "https://openrouter.ai/api/v1"
    model: str = "google/gemini-3.1-flash-lite"
    api_key_env: str = "OPENROUTER_API_KEY"
    fallback: LlmEndpoint | None = None
    fallbacks: tuple[LlmEndpoint, ...] = ()


@dataclass(frozen=True)
class ZebraConfig:
    host: str = ""
    port: int = 9100
    timeout_seconds: float = 3.0
    width_dots: int = 812
    length_dots: int = 406
    dpi: int = 203
    base_url: str = "https://magpie.example.com"


@dataclass(frozen=True)
class StorageConfig:
    backend: str = "sqlite"
    supabase_url: str = ""
    api_key_env: str = "SUPABASE_SERVICE_ROLE_KEY"
    workspace_id: str = ""
    photo_bucket: str = "magpie-photos"
    timeout_seconds: float = 10.0


@dataclass(frozen=True)
class EdgeConfig:
    """Central service to cabinet-controller command routing."""

    mode: str = "local"
    target_device_id: str = ""
    machine_url: str = ""
    tokens_env: str = "MAGPIE_EDGE_TOKENS_JSON"
    command_ttl_seconds: float = 300.0
    claim_timeout_seconds: float = 30.0
    max_attempts: int = 5

    def tokens(self, environ: dict[str, str] | None = None) -> dict[str, str]:
        values = environ if environ is not None else os.environ
        raw = values.get(self.tokens_env, "").strip()
        if not raw:
            return {}
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError(f"{self.tokens_env} must contain a JSON object")
        return {
            str(device_id).strip(): str(token).strip()
            for device_id, token in parsed.items()
            if str(device_id).strip() and str(token).strip()
        }


@dataclass(frozen=True)
class AuthConfig:
    """Human access control for a hosted Magpie service."""

    mode: str = "none"
    supabase_url: str = ""
    publishable_key_env: str = "SUPABASE_PUBLISHABLE_KEY"
    service_key_env: str = "SUPABASE_SERVICE_ROLE_KEY"
    workspace_id: str = ""
    multi_workspace: bool = False
    cache_seconds: float = 30.0

    def publishable_key(self, environ: dict[str, str] | None = None) -> str:
        values = environ if environ is not None else os.environ
        return require_supabase_publishable_key(
            values.get(self.publishable_key_env, ""),
            setting=self.publishable_key_env,
        )


def _parse_llm(raw: dict[str, object]) -> LlmConfig:
    defaults = LlmConfig()
    fallback_raw = raw.get("fallback")
    fallback = None
    if isinstance(fallback_raw, dict):
        fallback = LlmEndpoint(
            base_url=str(fallback_raw["base_url"]),
            model=str(fallback_raw["model"]),
            api_key_env=str(fallback_raw.get("api_key_env", "")),
            provider=str(fallback_raw.get("provider", "fallback")),
        )
    fallbacks: list[LlmEndpoint] = []
    fallbacks_raw = raw.get("fallbacks")
    if isinstance(fallbacks_raw, list):
        for endpoint in fallbacks_raw:
            if not isinstance(endpoint, dict):
                raise ValueError("llm.fallbacks entries must be objects")
            fallbacks.append(
                LlmEndpoint(
                    base_url=str(endpoint["base_url"]),
                    model=str(endpoint["model"]),
                    api_key_env=str(endpoint.get("api_key_env", "")),
                    provider=str(endpoint.get("provider", "compatible")),
                )
            )
    return LlmConfig(
        provider=str(raw.get("provider", defaults.provider)),
        base_url=str(raw.get("base_url", defaults.base_url)),
        model=str(raw.get("model", defaults.model)),
        api_key_env=str(raw.get("api_key_env", defaults.api_key_env)),
        fallback=fallback,
        fallbacks=tuple(fallbacks),
    )


def _parse_zebra(raw: dict[str, object]) -> ZebraConfig:
    defaults = ZebraConfig()
    return ZebraConfig(
        host=str(raw.get("host", defaults.host)).strip(),
        port=int(raw.get("port", defaults.port)),
        timeout_seconds=float(raw.get("timeout_seconds", defaults.timeout_seconds)),
        width_dots=int(raw.get("width_dots", defaults.width_dots)),
        length_dots=int(raw.get("length_dots", defaults.length_dots)),
        dpi=int(raw.get("dpi", defaults.dpi)),
        base_url=str(raw.get("base_url", defaults.base_url)).rstrip("/"),
    )


def _parse_storage(raw: dict[str, object]) -> StorageConfig:
    defaults = StorageConfig()
    return StorageConfig(
        backend=str(raw.get("backend", defaults.backend)).strip().lower(),
        supabase_url=str(raw.get("supabase_url", defaults.supabase_url)).strip().rstrip("/"),
        api_key_env=str(raw.get("api_key_env", defaults.api_key_env)).strip(),
        workspace_id=str(raw.get("workspace_id", defaults.workspace_id)).strip(),
        photo_bucket=str(raw.get("photo_bucket", defaults.photo_bucket)).strip(),
        timeout_seconds=float(raw.get("timeout_seconds", defaults.timeout_seconds)),
    )


def _parse_edge(raw: dict[str, object]) -> EdgeConfig:
    defaults = EdgeConfig()
    return EdgeConfig(
        mode=str(raw.get("mode", defaults.mode)).strip().lower(),
        target_device_id=str(
            raw.get("target_device_id", defaults.target_device_id)
        ).strip(),
        machine_url=str(raw.get("machine_url", defaults.machine_url))
        .strip()
        .rstrip("/"),
        tokens_env=str(raw.get("tokens_env", defaults.tokens_env)).strip(),
        command_ttl_seconds=float(
            raw.get("command_ttl_seconds", defaults.command_ttl_seconds)
        ),
        claim_timeout_seconds=float(
            raw.get("claim_timeout_seconds", defaults.claim_timeout_seconds)
        ),
        max_attempts=int(raw.get("max_attempts", defaults.max_attempts)),
    )


def _parse_auth(raw: dict[str, object]) -> AuthConfig:
    defaults = AuthConfig()
    return AuthConfig(
        mode=str(raw.get("mode", defaults.mode)).strip().lower(),
        supabase_url=str(raw.get("supabase_url", defaults.supabase_url))
        .strip()
        .rstrip("/"),
        publishable_key_env=str(
            raw.get("publishable_key_env", defaults.publishable_key_env)
        ).strip(),
        service_key_env=str(
            raw.get("service_key_env", defaults.service_key_env)
        ).strip(),
        workspace_id=str(raw.get("workspace_id", defaults.workspace_id)).strip(),
        multi_workspace=bool(raw.get("multi_workspace", defaults.multi_workspace)),
        cache_seconds=float(raw.get("cache_seconds", defaults.cache_seconds)),
    )


@dataclass(frozen=True)
class LedConfig:
    pixel_count: int = 28
    gpio_pin: int = 13
    backend: str = "pi5-pio"
    wled_url: str | None = None
    wled_start: int = 0
    color_order: str = "RBG"
    default_brightness: float = 0.4
    bind_host: str = "0.0.0.0"
    bind_port: int = 8765
    layout_path: str = "config/storage-layout.json"
    database_path: str = "state/inventory.db"
    assistant_backend: str = "auto"
    agent_max_steps: int = 4
    openai_model: str = "gpt-5.6-luna"
    public_origin: str = ""
    llm: LlmConfig = field(default_factory=LlmConfig)
    zebra: ZebraConfig = field(default_factory=ZebraConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    edge: EdgeConfig = field(default_factory=EdgeConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)

    @classmethod
    def from_path(cls, path: str | Path | None = None) -> "LedConfig":
        config_path = _resolve_config_path(path)
        if config_path is None:
            return cls()
        with config_path.open("r", encoding="utf-8") as fh:
            raw = json.load(fh)
        return cls(
            pixel_count=int(raw.get("pixel_count", cls.pixel_count)),
            gpio_pin=int(raw.get("gpio_pin", cls.gpio_pin)),
            backend=str(raw.get("backend", cls.backend)).lower(),
            wled_url=raw.get("wled_url", cls.wled_url),
            wled_start=int(raw.get("wled_start", cls.wled_start)),
            color_order=str(raw.get("color_order", cls.color_order)).upper(),
            default_brightness=float(
                raw.get("default_brightness", cls.default_brightness)
            ),
            bind_host=str(raw.get("bind_host", cls.bind_host)),
            bind_port=int(raw.get("bind_port", cls.bind_port)),
            layout_path=str(raw.get("layout_path", cls.layout_path)),
            database_path=str(raw.get("database_path", cls.database_path)),
            assistant_backend=str(
                raw.get("assistant_backend", cls.assistant_backend)
            ).lower(),
            agent_max_steps=int(raw.get("agent_max_steps", cls.agent_max_steps)),
            openai_model=str(raw.get("openai_model", cls.openai_model)),
            public_origin=str(raw.get("public_origin", cls.public_origin))
            .strip()
            .rstrip("/"),
            llm=_parse_llm(raw["llm"]) if isinstance(raw.get("llm"), dict) else LlmConfig(),
            zebra=(
                _parse_zebra(raw["zebra"])
                if isinstance(raw.get("zebra"), dict)
                else ZebraConfig()
            ),
            storage=(
                _parse_storage(raw["storage"])
                if isinstance(raw.get("storage"), dict)
                else StorageConfig()
            ),
            edge=(
                _parse_edge(raw["edge"])
                if isinstance(raw.get("edge"), dict)
                else EdgeConfig()
            ),
            auth=(
                _parse_auth(raw["auth"])
                if isinstance(raw.get("auth"), dict)
                else AuthConfig()
            ),
        ).validated()

    def validated(self) -> "LedConfig":
        if self.pixel_count < 0 or (
            self.backend != "none" and self.pixel_count == 0
        ):
            raise ValueError("pixel_count must be positive when a light backend is enabled")
        if self.gpio_pin <= 0:
            raise ValueError("gpio_pin must be a GPIO number, not a header pin number")
        if self.backend not in {"none", "pi5-pio", "wled"}:
            raise ValueError("backend must be none, pi5-pio, or wled")
        if self.backend == "wled" and not self.wled_url:
            raise ValueError("wled_url is required when backend is wled")
        if self.wled_start < 0:
            raise ValueError("wled_start must be zero or positive")
        if sorted(self.color_order) != ["B", "G", "R"]:
            raise ValueError("color_order must contain R, G, and B exactly once")
        if not 0 <= self.default_brightness <= 1:
            raise ValueError("default_brightness must be between 0 and 1")
        if not 1 <= self.bind_port <= 65535:
            raise ValueError("bind_port must be between 1 and 65535")
        if self.assistant_backend not in {"auto", "deterministic", "openai"}:
            raise ValueError(
                "assistant_backend must be auto, deterministic, or openai"
            )
        if not 1 <= self.agent_max_steps <= 8:
            raise ValueError("agent_max_steps must be between 1 and 8")
        if not self.openai_model.strip():
            raise ValueError("openai_model is required")
        if not 1 <= self.zebra.port <= 65535:
            raise ValueError("zebra port must be between 1 and 65535")
        if self.zebra.timeout_seconds <= 0:
            raise ValueError("zebra timeout must be positive")
        if self.zebra.width_dots <= 0 or self.zebra.length_dots <= 0:
            raise ValueError("zebra label dimensions must be positive")
        if self.zebra.dpi <= 0:
            raise ValueError("zebra dpi must be positive")
        if self.storage.backend not in {"sqlite", "supabase"}:
            raise ValueError("storage.backend must be sqlite or supabase")
        if self.storage.timeout_seconds <= 0:
            raise ValueError("storage.timeout_seconds must be positive")
        if not self.storage.photo_bucket:
            raise ValueError("storage.photo_bucket is required")
        if self.storage.backend == "supabase":
            if not self.storage.supabase_url:
                raise ValueError("storage.supabase_url is required for Supabase")
            if not self.storage.api_key_env:
                raise ValueError("storage.api_key_env is required for Supabase")
            if not self.storage.workspace_id and not self.auth.multi_workspace:
                raise ValueError("storage.workspace_id is required for Supabase")
        if self.edge.mode not in {"local", "central", "workspace"}:
            raise ValueError("edge.mode must be local, central, or workspace")
        if self.edge.mode == "central" and not self.edge.target_device_id:
            raise ValueError("edge.target_device_id is required in central mode")
        if self.edge.mode == "workspace" and self.edge.target_device_id:
            raise ValueError("workspace edge mode routes through location light_target")
        if self.edge.machine_url and not self.edge.machine_url.startswith("https://"):
            raise ValueError("edge.machine_url must use HTTPS")
        if not self.edge.tokens_env:
            raise ValueError("edge.tokens_env is required")
        if self.edge.command_ttl_seconds <= 0:
            raise ValueError("edge.command_ttl_seconds must be positive")
        if self.edge.claim_timeout_seconds <= 0:
            raise ValueError("edge.claim_timeout_seconds must be positive")
        if self.edge.max_attempts < 1:
            raise ValueError("edge.max_attempts must be positive")
        if self.auth.mode not in {"none", "supabase"}:
            raise ValueError("auth.mode must be none or supabase")
        if self.auth.multi_workspace and self.auth.mode != "supabase":
            raise ValueError("auth.multi_workspace requires Supabase auth")
        if self.edge.mode == "workspace" and not self.auth.multi_workspace:
            raise ValueError("workspace edge mode requires multi-workspace auth")
        if self.auth.cache_seconds < 0:
            raise ValueError("auth.cache_seconds must not be negative")
        if self.auth.mode == "supabase":
            if not self.auth.supabase_url:
                raise ValueError("auth.supabase_url is required for Supabase auth")
            if not self.auth.publishable_key_env:
                raise ValueError("auth.publishable_key_env is required for Supabase auth")
            if not self.auth.service_key_env:
                raise ValueError("auth.service_key_env is required for Supabase auth")
            if not self.auth.workspace_id and not self.auth.multi_workspace:
                raise ValueError("auth.workspace_id is required for Supabase auth")
            if self.auth.multi_workspace:
                if self.storage.backend != "supabase":
                    raise ValueError(
                        "auth.multi_workspace requires Supabase storage"
                    )
                if self.auth.workspace_id or self.storage.workspace_id:
                    raise ValueError(
                        "multi-workspace auth and storage must not pin workspace_id"
                    )
                if self.backend != "none" or self.edge.mode not in {"local", "workspace"}:
                    raise ValueError(
                        "multi-workspace service must use hardware-neutral local or workspace edge mode"
                    )
            if self.storage.backend == "supabase":
                if self.auth.supabase_url != self.storage.supabase_url:
                    raise ValueError(
                        "auth.supabase_url must match storage.supabase_url"
                    )
                if (
                    not self.auth.multi_workspace
                    and self.auth.workspace_id != self.storage.workspace_id
                ):
                    raise ValueError(
                        "auth.workspace_id must match storage.workspace_id"
                    )
        return self


def _resolve_config_path(path: str | Path | None) -> Path | None:
    if path is not None:
        candidate = Path(path)
        if not candidate.exists():
            raise FileNotFoundError(candidate)
        return candidate
    for candidate in DEFAULT_CONFIG_PATHS:
        if candidate.exists():
            return candidate
    return None
