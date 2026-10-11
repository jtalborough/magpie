from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone
from typing import Mapping, Protocol

from .config import AuthConfig
from .supabase_client import SupabaseRestClient


class AgentCredentialService(Protocol):
    def authenticate(self, token_hash: str) -> dict[str, object] | None: ...

    def list(self, workspace_id: str) -> list[dict[str, object]]: ...

    def issue(
        self,
        workspace_id: str,
        user_id: str,
        name: str,
        token_hash: str,
        token_hint: str,
    ) -> dict[str, object]: ...

    def revoke(
        self, workspace_id: str, credential_id: str
    ) -> dict[str, object] | None: ...


class SupabaseAgentCredentialService:
    """Workspace-scoped credentials accepted only by Magpie's MCP route."""

    def __init__(
        self,
        *,
        service_client: SupabaseRestClient,
        cache_seconds: float = 30.0,
    ) -> None:
        self.service_client = service_client
        self.cache_seconds = max(0.0, float(cache_seconds))
        self._cache: dict[str, tuple[float, dict[str, object]]] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _public(row: Mapping[str, object]) -> dict[str, object]:
        return {
            key: row.get(key)
            for key in (
                "id",
                "workspace_id",
                "name",
                "token_hint",
                "created_by",
                "created_at",
                "last_used_at",
                "revoked_at",
            )
        }

    def authenticate(self, token_hash: str) -> dict[str, object] | None:
        clean_hash = token_hash.strip()
        if not clean_hash:
            return None
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(clean_hash)
            if cached is not None and cached[0] > now:
                return dict(cached[1])
            self._cache.pop(clean_hash, None)
        rows = self.service_client.select(
            "agent_credentials",
            columns=(
                "id,workspace_id,name,token_hint,created_by,created_at,"
                "last_used_at,revoked_at"
            ),
            filters={
                "token_hash": f"eq.{clean_hash}",
                "revoked_at": "is.null",
            },
            limit=1,
        )
        if not rows:
            return None
        record = self._public(rows[0])
        used_at = datetime.now(timezone.utc).isoformat()
        self.service_client.update(
            "agent_credentials",
            {"last_used_at": used_at},
            filters={"id": f"eq.{record['id']}", "revoked_at": "is.null"},
            return_rows=False,
        )
        record["last_used_at"] = used_at
        with self._lock:
            self._cache[clean_hash] = (now + self.cache_seconds, record)
        return dict(record)

    def list(self, workspace_id: str) -> list[dict[str, object]]:
        rows = self.service_client.select(
            "agent_credentials",
            columns=(
                "id,workspace_id,name,token_hint,created_by,created_at,"
                "last_used_at,revoked_at"
            ),
            filters={"workspace_id": f"eq.{workspace_id}"},
            order="created_at.desc",
        )
        return [self._public(row) for row in rows]

    def issue(
        self,
        workspace_id: str,
        user_id: str,
        name: str,
        token_hash: str,
        token_hint: str,
    ) -> dict[str, object]:
        clean_name = " ".join(name.split())
        if not clean_name:
            raise ValueError("connection name is required")
        if len(clean_name) > 80:
            raise ValueError("connection name must be 80 characters or fewer")
        rows = self.service_client.insert(
            "agent_credentials",
            {
                "workspace_id": workspace_id,
                "name": clean_name,
                "token_hash": token_hash,
                "token_hint": token_hint,
                "created_by": user_id,
            },
        )
        if not rows:
            raise RuntimeError("agent connection was not created")
        return self._public(rows[0])

    def revoke(
        self, workspace_id: str, credential_id: str
    ) -> dict[str, object] | None:
        revoked_at = datetime.now(timezone.utc).isoformat()
        rows = self.service_client.update(
            "agent_credentials",
            {"revoked_at": revoked_at},
            filters={
                "id": f"eq.{credential_id}",
                "workspace_id": f"eq.{workspace_id}",
                "revoked_at": "is.null",
            },
        )
        if not rows:
            return None
        record = self._public(rows[0])
        with self._lock:
            self._cache = {
                token_hash: cached
                for token_hash, cached in self._cache.items()
                if str(cached[1].get("id") or "") != credential_id
            }
        return record


def create_agent_credential_service(
    config: AuthConfig,
    *,
    environ: Mapping[str, str] | None = None,
) -> SupabaseAgentCredentialService:
    values = environ if environ is not None else os.environ
    secret_key = values.get(config.service_key_env, "").strip()
    if not secret_key:
        raise ValueError(
            f"Supabase service key environment variable is not set: {config.service_key_env}"
        )
    return SupabaseAgentCredentialService(
        service_client=SupabaseRestClient(url=config.supabase_url, api_key=secret_key),
        cache_seconds=config.cache_seconds,
    )
