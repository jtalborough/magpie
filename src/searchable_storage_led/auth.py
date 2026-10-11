from __future__ import annotations

import hashlib
import os
import threading
import time
from dataclasses import dataclass
from typing import Mapping, Protocol
from uuid import UUID

import httpx

from .config import AuthConfig
from .supabase_client import SupabaseRestClient


class AuthError(RuntimeError):
    status_code = 401


class InvalidTokenError(AuthError):
    status_code = 401


class WorkspaceAccessError(AuthError):
    status_code = 403


class AuthUnavailableError(AuthError):
    status_code = 503


@dataclass(frozen=True)
class AuthIdentity:
    user_id: str
    email: str
    workspace_id: str
    role: str

    def as_dict(self) -> dict[str, str]:
        return {
            "user_id": self.user_id,
            "email": self.email,
            "workspace_id": self.workspace_id,
            "role": self.role,
        }


class AuthService(Protocol):
    def authenticate(
        self,
        access_token: str,
        workspace_id: str | None = None,
        *,
        allow_unassigned: bool = False,
    ) -> AuthIdentity: ...


class SupabaseAuthService:
    """Validate a Supabase access token and authorize one workspace."""

    def __init__(
        self,
        *,
        url: str,
        publishable_key: str,
        service_client: SupabaseRestClient,
        workspace_id: str,
        cache_seconds: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.auth_url = f"{url.rstrip('/')}/auth/v1"
        self.publishable_key = publishable_key
        self.service_client = service_client
        self.workspace_id = workspace_id
        self.cache_seconds = cache_seconds
        self.client = client or httpx.Client(timeout=10)
        self._owns_client = client is None
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, AuthIdentity]] = {}

    def authenticate(
        self,
        access_token: str,
        workspace_id: str | None = None,
        *,
        allow_unassigned: bool = False,
    ) -> AuthIdentity:
        clean_token = access_token.strip()
        if not clean_token:
            raise InvalidTokenError("a bearer token is required")
        requested_workspace = (workspace_id or "").strip()
        if requested_workspace and not self.workspace_id:
            try:
                requested_workspace = str(UUID(requested_workspace))
            except ValueError as exc:
                raise WorkspaceAccessError("workspace id must be a UUID") from exc
        if (
            self.workspace_id
            and requested_workspace
            and requested_workspace != self.workspace_id
        ):
            raise WorkspaceAccessError(
                "this Magpie service is bound to a different workspace"
            )
        selected_workspace = requested_workspace or self.workspace_id
        token_hash = hashlib.sha256(clean_token.encode("utf-8")).hexdigest()
        cache_key = hashlib.sha256(
            f"{token_hash}:{selected_workspace}:{int(allow_unassigned)}".encode("utf-8")
        ).hexdigest()
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(cache_key)
            if cached is not None and cached[0] > now:
                return cached[1]
        try:
            response = self.client.get(
                f"{self.auth_url}/user",
                headers={
                    "apikey": self.publishable_key,
                    "Authorization": f"Bearer {clean_token}",
                },
            )
        except httpx.HTTPError as exc:
            raise AuthUnavailableError("Supabase Auth is unavailable") from exc
        if response.status_code in {401, 403}:
            raise InvalidTokenError("the Supabase session is invalid or expired")
        if not 200 <= response.status_code < 300:
            raise AuthUnavailableError(
                f"Supabase Auth rejected session validation ({response.status_code})"
            )
        try:
            user = response.json()
        except ValueError as exc:
            raise AuthUnavailableError("Supabase Auth returned invalid JSON") from exc
        if not isinstance(user, dict) or not str(user.get("id") or "").strip():
            raise InvalidTokenError("the Supabase session has no user identity")
        user_id = str(user["id"])
        membership_filters = {"user_id": f"eq.{user_id}"}
        if selected_workspace:
            membership_filters["workspace_id"] = f"eq.{selected_workspace}"
        try:
            memberships = self.service_client.select(
                "workspace_members",
                columns="workspace_id,user_id,role,created_at",
                filters=membership_filters,
                order="created_at.asc" if not selected_workspace else None,
                limit=1,
            )
        except Exception as exc:
            raise AuthUnavailableError(
                "workspace authorization is unavailable"
            ) from exc
        if not memberships and not selected_workspace and allow_unassigned:
            identity = AuthIdentity(
                user_id=user_id,
                email=str(user.get("email") or ""),
                workspace_id="",
                role="authenticated",
            )
            if self.cache_seconds > 0:
                with self._lock:
                    self._cache[cache_key] = (now + self.cache_seconds, identity)
            return identity
        if not memberships:
            raise WorkspaceAccessError(
                "this account is not a member of the Magpie workspace"
            )
        membership = memberships[0]
        membership_workspace = str(membership.get("workspace_id") or "")
        if not membership_workspace:
            raise AuthUnavailableError("workspace membership has no workspace id")
        identity = AuthIdentity(
            user_id=user_id,
            email=str(user.get("email") or ""),
            workspace_id=membership_workspace,
            role=str(membership.get("role") or "member"),
        )
        if self.cache_seconds > 0:
            with self._lock:
                self._cache[cache_key] = (now + self.cache_seconds, identity)
                if len(self._cache) > 1024:
                    self._cache = {
                        key: value
                        for key, value in self._cache.items()
                        if value[0] > now
                    }
        return identity

    def invalidate_user(self, user_id: str) -> None:
        """Discard cached identities after a workspace membership mutation."""
        with self._lock:
            self._cache = {
                key: value
                for key, value in self._cache.items()
                if value[1].user_id != user_id
            }

    def close(self) -> None:
        if self._owns_client:
            self.client.close()


def create_auth_service(
    config: AuthConfig,
    *,
    environ: Mapping[str, str] | None = None,
) -> SupabaseAuthService:
    values = environ if environ is not None else os.environ
    publishable_key = config.publishable_key(dict(values))
    service_key = values.get(config.service_key_env, "").strip()
    if not service_key:
        raise ValueError(
            f"Supabase service key environment variable is not set: {config.service_key_env}"
        )
    return SupabaseAuthService(
        url=config.supabase_url,
        publishable_key=publishable_key,
        service_client=SupabaseRestClient(url=config.supabase_url, api_key=service_key),
        workspace_id=config.workspace_id,
        cache_seconds=config.cache_seconds,
    )
