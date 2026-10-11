from __future__ import annotations

import hmac
import json
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Protocol

from .config import LedConfig, StorageConfig
from .supabase_client import SupabaseRestClient


EDGE_CREDENTIAL_SCOPES = frozenset(
    {"commands", "voice", "screen", "printer", "check_station"}
)


def normalize_credential_scopes(scopes: list[str] | None) -> list[str]:
    requested_scopes = ["commands"] if scopes is None else scopes
    normalized = sorted(
        {str(scope).strip().lower() for scope in requested_scopes if str(scope).strip()}
    )
    invalid = set(normalized) - EDGE_CREDENTIAL_SCOPES
    if invalid:
        raise ValueError(f"unsupported edge credential scopes: {', '.join(sorted(invalid))}")
    if not normalized:
        raise ValueError("at least one edge credential scope is required")
    return normalized


@dataclass(frozen=True)
class EdgeCommand:
    id: str
    device_id: str
    kind: str
    payload: dict[str, object]
    status: str
    attempts: int
    created_at: object
    expires_at: object

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "device_id": self.device_id,
            "kind": self.kind,
            "payload": self.payload,
            "status": self.status,
            "attempts": self.attempts,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }


class EdgeRepository(Protocol):
    def set_pairing(
        self,
        device_id: str,
        *,
        pairing_hash: str,
        pairing_hint: str,
        scopes: list[str],
        expires_at: float | str,
    ) -> dict[str, object]: ...

    def consume_pairing(
        self,
        device_id: str,
        *,
        pairing_hash: str,
        token_hash: str,
        token_hint: str,
    ) -> dict[str, object] | None: ...

    def set_credential(
        self,
        device_id: str,
        *,
        token_hash: str,
        token_hint: str,
        scopes: list[str] | None = None,
    ) -> dict[str, object]: ...

    def revoke_credential(self, device_id: str) -> dict[str, object] | None: ...

    def credential_matches(self, device_id: str, token_hash: str) -> bool: ...

    def credential_authorizes(
        self, device_id: str, token_hash: str, scope: str
    ) -> bool: ...

    def enqueue(
        self,
        device_id: str,
        kind: str,
        payload: Mapping[str, object],
        *,
        ttl_seconds: float,
    ) -> EdgeCommand: ...

    def heartbeat(
        self,
        device_id: str,
        *,
        version: str = "",
        capabilities: list[str] | None = None,
    ) -> dict[str, object]: ...

    def claim(
        self, device_id: str, *, claim_timeout: float, max_attempts: int
    ) -> EdgeCommand | None: ...

    def complete(
        self,
        device_id: str,
        command_id: str,
        *,
        succeeded: bool,
        error: str = "",
        retryable: bool = False,
        max_attempts: int = 5,
    ) -> EdgeCommand | None: ...

    def status(self) -> dict[str, object]: ...


def _command(row: Mapping[str, object]) -> EdgeCommand:
    payload = row.get("payload", {})
    if isinstance(payload, str):
        payload = json.loads(payload)
    return EdgeCommand(
        id=str(row["id"]),
        device_id=str(row["device_id"]),
        kind=str(row["kind"]),
        payload=dict(payload) if isinstance(payload, dict) else {},
        status=str(row["status"]),
        attempts=int(row.get("attempts", 0)),
        created_at=row.get("created_at", ""),
        expires_at=row.get("expires_at", ""),
    )


class SqliteEdgeRepository:
    def __init__(self, path: str | Path, *, workspace_id: str = "local") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.workspace_id = workspace_id
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS edge_devices (
                    workspace_id TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    version TEXT NOT NULL DEFAULT '',
                    capabilities_json TEXT NOT NULL DEFAULT '[]',
                    last_seen_at REAL NOT NULL,
                    PRIMARY KEY(workspace_id, device_id)
                );
                CREATE TABLE IF NOT EXISTS edge_commands (
                    id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    not_before REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    claimed_at REAL,
                    completed_at REAL,
                    error TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS edge_commands_claim_idx
                    ON edge_commands(workspace_id, device_id, status, not_before, created_at);
                """
            )
            columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(edge_devices)")
            }
            additions = {
                "credential_hash": "TEXT NOT NULL DEFAULT ''",
                "credential_hint": "TEXT NOT NULL DEFAULT ''",
                "credential_scopes_json": "TEXT NOT NULL DEFAULT '[\"commands\"]'",
                "credential_created_at": "REAL",
                "revoked_at": "REAL",
                "pairing_hash": "TEXT NOT NULL DEFAULT ''",
                "pairing_hint": "TEXT NOT NULL DEFAULT ''",
                "pairing_scopes_json": "TEXT NOT NULL DEFAULT '[]'",
                "pairing_expires_at": "REAL",
            }
            for name, definition in additions.items():
                if name not in columns:
                    conn.execute(
                        f"ALTER TABLE edge_devices ADD COLUMN {name} {definition}"
                    )

    def set_pairing(
        self,
        device_id: str,
        *,
        pairing_hash: str,
        pairing_hint: str,
        scopes: list[str],
        expires_at: float | str,
    ) -> dict[str, object]:
        credential_scopes = normalize_credential_scopes(scopes)
        expiry = float(expires_at)
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO edge_devices(
                    workspace_id, device_id, last_seen_at,
                    pairing_hash, pairing_hint, pairing_scopes_json,
                    pairing_expires_at
                ) VALUES (?, ?, 0, ?, ?, ?, ?)
                ON CONFLICT(workspace_id, device_id) DO UPDATE SET
                    pairing_hash=excluded.pairing_hash,
                    pairing_hint=excluded.pairing_hint,
                    pairing_scopes_json=excluded.pairing_scopes_json,
                    pairing_expires_at=excluded.pairing_expires_at""",
                (
                    self.workspace_id,
                    device_id,
                    pairing_hash,
                    pairing_hint,
                    json.dumps(credential_scopes),
                    expiry,
                ),
            )
        return {
            "device_id": device_id,
            "pairing_hint": pairing_hint,
            "pairing_scopes": credential_scopes,
            "pairing_expires_at": expiry,
            "pairing_pending": True,
        }

    def consume_pairing(
        self,
        device_id: str,
        *,
        pairing_hash: str,
        token_hash: str,
        token_hint: str,
    ) -> dict[str, object] | None:
        now = time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """SELECT pairing_hash, pairing_scopes_json, pairing_expires_at
                FROM edge_devices WHERE workspace_id=? AND device_id=?""",
                (self.workspace_id, device_id),
            ).fetchone()
            expected = str(row["pairing_hash"] or "") if row is not None else ""
            expiry = float(row["pairing_expires_at"] or 0) if row is not None else 0
            if (
                row is None
                or not expected
                or expiry <= now
                or not hmac.compare_digest(expected, pairing_hash)
            ):
                return None
            try:
                scopes = normalize_credential_scopes(
                    json.loads(str(row["pairing_scopes_json"] or "[]"))
                )
            except (json.JSONDecodeError, ValueError):
                return None
            updated = conn.execute(
                """UPDATE edge_devices SET
                    credential_hash=?, credential_hint=?,
                    credential_scopes_json=?, credential_created_at=?, revoked_at=NULL,
                    pairing_hash='', pairing_hint='', pairing_scopes_json='[]',
                    pairing_expires_at=NULL
                WHERE workspace_id=? AND device_id=? AND pairing_hash=?""",
                (
                    token_hash,
                    token_hint,
                    json.dumps(scopes),
                    now,
                    self.workspace_id,
                    device_id,
                    pairing_hash,
                ),
            )
            if updated.rowcount != 1:
                return None
        return {
            "device_id": device_id,
            "credential_hint": token_hint,
            "credential_scopes": scopes,
            "credential_created_at": now,
            "revoked_at": None,
            "enrolled": True,
            "pairing_pending": False,
        }

    def set_credential(
        self,
        device_id: str,
        *,
        token_hash: str,
        token_hint: str,
        scopes: list[str] | None = None,
    ) -> dict[str, object]:
        now = time.time()
        credential_scopes = normalize_credential_scopes(scopes)
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO edge_devices(
                    workspace_id, device_id, last_seen_at,
                    credential_hash, credential_hint, credential_scopes_json,
                    credential_created_at, revoked_at
                ) VALUES (?, ?, 0, ?, ?, ?, ?, NULL)
                ON CONFLICT(workspace_id, device_id) DO UPDATE SET
                    credential_hash=excluded.credential_hash,
                    credential_hint=excluded.credential_hint,
                    credential_scopes_json=excluded.credential_scopes_json,
                    credential_created_at=excluded.credential_created_at,
                    revoked_at=NULL,
                    pairing_hash='', pairing_hint='', pairing_scopes_json='[]',
                    pairing_expires_at=NULL""",
                (
                    self.workspace_id,
                    device_id,
                    token_hash,
                    token_hint,
                    json.dumps(credential_scopes),
                    now,
                ),
            )
        return {
            "device_id": device_id,
            "credential_hint": token_hint,
            "credential_scopes": credential_scopes,
            "credential_created_at": now,
            "revoked_at": None,
            "enrolled": True,
        }

    def revoke_credential(self, device_id: str) -> dict[str, object] | None:
        now = time.time()
        with self._connect() as conn:
            updated = conn.execute(
                """UPDATE edge_devices
                SET credential_hash='', credential_hint='', revoked_at=?,
                    pairing_hash='', pairing_hint='', pairing_scopes_json='[]',
                    pairing_expires_at=NULL
                WHERE workspace_id=? AND device_id=?""",
                (now, self.workspace_id, device_id),
            )
        if updated.rowcount != 1:
            return None
        return {"device_id": device_id, "revoked_at": now, "enrolled": False}

    def credential_matches(self, device_id: str, token_hash: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT credential_hash, revoked_at FROM edge_devices
                WHERE workspace_id=? AND device_id=?""",
                (self.workspace_id, device_id),
            ).fetchone()
        if row is None or row["revoked_at"] is not None:
            return False
        expected = str(row["credential_hash"] or "")
        return bool(expected and hmac.compare_digest(expected, token_hash))

    def credential_authorizes(
        self, device_id: str, token_hash: str, scope: str
    ) -> bool:
        clean_scope = str(scope).strip().lower()
        if clean_scope not in EDGE_CREDENTIAL_SCOPES:
            return False
        with self._connect() as conn:
            row = conn.execute(
                """SELECT credential_hash, credential_scopes_json, revoked_at
                FROM edge_devices WHERE workspace_id=? AND device_id=?""",
                (self.workspace_id, device_id),
            ).fetchone()
        if row is None or row["revoked_at"] is not None:
            return False
        expected = str(row["credential_hash"] or "")
        if not expected or not hmac.compare_digest(expected, token_hash):
            return False
        try:
            scopes = json.loads(str(row["credential_scopes_json"] or "[]"))
        except json.JSONDecodeError:
            return False
        return isinstance(scopes, list) and clean_scope in scopes

    def enqueue(
        self,
        device_id: str,
        kind: str,
        payload: Mapping[str, object],
        *,
        ttl_seconds: float,
    ) -> EdgeCommand:
        now = time.time()
        command_id = str(uuid.uuid4())
        with self._connect() as conn:
            if kind == "set_pixels":
                conn.execute(
                    """UPDATE edge_commands
                    SET status='failed', completed_at=?, error='superseded by newer state'
                    WHERE workspace_id=? AND device_id=? AND kind=? AND status='queued'""",
                    (now, self.workspace_id, device_id, kind),
                )
            conn.execute(
                """INSERT INTO edge_commands(
                    id, workspace_id, device_id, kind, payload_json,
                    created_at, not_before, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    command_id,
                    self.workspace_id,
                    device_id,
                    kind,
                    json.dumps(dict(payload), separators=(",", ":")),
                    now,
                    now,
                    now + ttl_seconds,
                ),
            )
            row = conn.execute(
                "SELECT * FROM edge_commands WHERE id = ?", (command_id,)
            ).fetchone()
        assert row is not None
        return _command(dict(row) | {"payload": row["payload_json"]})

    def heartbeat(
        self,
        device_id: str,
        *,
        version: str = "",
        capabilities: list[str] | None = None,
    ) -> dict[str, object]:
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO edge_devices(workspace_id, device_id, version, capabilities_json, last_seen_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(workspace_id, device_id) DO UPDATE SET
                    version=excluded.version,
                    capabilities_json=excluded.capabilities_json,
                    last_seen_at=excluded.last_seen_at""",
                (
                    self.workspace_id,
                    device_id,
                    version,
                    json.dumps(capabilities or []),
                    now,
                ),
            )
        return {
            "device_id": device_id,
            "version": version,
            "capabilities": capabilities or [],
            "last_seen_at": now,
        }

    def claim(
        self, device_id: str, *, claim_timeout: float, max_attempts: int
    ) -> EdgeCommand | None:
        now = time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """UPDATE edge_commands SET status='queued', claimed_at=NULL
                WHERE workspace_id=? AND device_id=? AND status='claimed'
                  AND claimed_at < ? AND attempts < ? AND expires_at > ?""",
                (self.workspace_id, device_id, now - claim_timeout, max_attempts, now),
            )
            conn.execute(
                """UPDATE edge_commands SET status='failed', completed_at=?, error='expired or retry limit reached'
                WHERE workspace_id=? AND device_id=? AND status IN ('queued','claimed')
                  AND (expires_at <= ? OR attempts >= ?)""",
                (now, self.workspace_id, device_id, now, max_attempts),
            )
            row = conn.execute(
                """SELECT * FROM edge_commands
                WHERE workspace_id=? AND device_id=? AND status='queued'
                  AND not_before <= ? AND expires_at > ?
                ORDER BY created_at LIMIT 1""",
                (self.workspace_id, device_id, now, now),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE edge_commands SET status='claimed', claimed_at=?, attempts=attempts+1 WHERE id=?",
                (now, row["id"]),
            )
            row = conn.execute(
                "SELECT * FROM edge_commands WHERE id=?", (row["id"],)
            ).fetchone()
        assert row is not None
        return _command(dict(row) | {"payload": row["payload_json"]})

    def complete(
        self,
        device_id: str,
        command_id: str,
        *,
        succeeded: bool,
        error: str = "",
        retryable: bool = False,
        max_attempts: int = 5,
    ) -> EdgeCommand | None:
        now = time.time()
        with self._connect() as conn:
            row = conn.execute(
                """SELECT * FROM edge_commands
                WHERE id=? AND workspace_id=? AND device_id=? AND status='claimed'""",
                (command_id, self.workspace_id, device_id),
            ).fetchone()
            if row is None:
                return None
            should_retry = (
                not succeeded
                and retryable
                and int(row["attempts"]) < max_attempts
                and float(row["expires_at"]) > now
            )
            status = (
                "queued" if should_retry else ("succeeded" if succeeded else "failed")
            )
            completed_at = None if should_retry else now
            claimed_at = None if should_retry else row["claimed_at"]
            not_before = (
                now + min(30.0, float(2 ** int(row["attempts"])))
                if should_retry
                else row["not_before"]
            )
            updated = conn.execute(
                """UPDATE edge_commands SET status=?, completed_at=?, claimed_at=?, not_before=?, error=?
                WHERE id=? AND workspace_id=? AND device_id=? AND status='claimed'""",
                (
                    status,
                    completed_at,
                    claimed_at,
                    not_before,
                    error[:1000],
                    command_id,
                    self.workspace_id,
                    device_id,
                ),
            )
            if updated.rowcount != 1:
                return None
            row = conn.execute(
                "SELECT * FROM edge_commands WHERE id=? AND workspace_id=? AND device_id=?",
                (command_id, self.workspace_id, device_id),
            ).fetchone()
        if row is None:
            return None
        return _command(dict(row) | {"payload": row["payload_json"]})

    def status(self) -> dict[str, object]:
        with self._connect() as conn:
            devices = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM edge_devices WHERE workspace_id=? ORDER BY device_id",
                    (self.workspace_id,),
                )
            ]
            counts = [
                dict(row)
                for row in conn.execute(
                    """SELECT device_id, status, count(*) AS count FROM edge_commands
                WHERE workspace_id=? GROUP BY device_id, status ORDER BY device_id, status""",
                    (self.workspace_id,),
                )
            ]
            recent = [
                dict(row)
                for row in conn.execute(
                    """SELECT id, device_id, kind, status, attempts, created_at, expires_at, error
                FROM edge_commands WHERE workspace_id=? ORDER BY created_at DESC LIMIT 20""",
                    (self.workspace_id,),
                )
            ]
        for device in devices:
            device["capabilities"] = json.loads(
                str(device.pop("capabilities_json", "[]"))
            )
            device.pop("credential_hash", None)
            device.pop("pairing_hash", None)
            try:
                device["credential_scopes"] = json.loads(
                    str(device.pop("credential_scopes_json", "[]"))
                )
            except json.JSONDecodeError:
                device["credential_scopes"] = []
            device["enrolled"] = bool(
                device.get("credential_hint") and device.get("revoked_at") is None
            )
            try:
                device["pairing_scopes"] = json.loads(
                    str(device.pop("pairing_scopes_json", "[]"))
                )
            except json.JSONDecodeError:
                device["pairing_scopes"] = []
            device["pairing_pending"] = bool(
                device.get("pairing_hint")
                and float(device.get("pairing_expires_at") or 0) > time.time()
            )
        return {"devices": devices, "counts": counts, "recent_commands": recent}


class SupabaseEdgeRepository:
    def __init__(self, client: SupabaseRestClient, *, workspace_id: str) -> None:
        self.client = client
        self.workspace_id = workspace_id

    def set_pairing(
        self,
        device_id: str,
        *,
        pairing_hash: str,
        pairing_hint: str,
        scopes: list[str],
        expires_at: float | str,
    ) -> dict[str, object]:
        credential_scopes = normalize_credential_scopes(scopes)
        if isinstance(expires_at, (int, float)):
            expiry = datetime.fromtimestamp(expires_at, timezone.utc).isoformat()
        else:
            expiry = str(expires_at)
        rows = self.client.upsert(
            "edge_devices",
            {
                "workspace_id": self.workspace_id,
                "device_id": device_id,
                "pairing_hash": pairing_hash,
                "pairing_hint": pairing_hint,
                "pairing_scopes": credential_scopes,
                "pairing_expires_at": expiry,
            },
            on_conflict="workspace_id,device_id",
        )
        return {
            "device_id": device_id,
            "pairing_hint": pairing_hint,
            "pairing_scopes": credential_scopes,
            "pairing_expires_at": expiry,
            "pairing_pending": True,
            **({"created_at": rows[0].get("created_at")} if rows else {}),
        }

    def consume_pairing(
        self,
        device_id: str,
        *,
        pairing_hash: str,
        token_hash: str,
        token_hint: str,
    ) -> dict[str, object] | None:
        result = self.client.rpc(
            "magpie_consume_edge_pairing",
            {
                "target_workspace_id": self.workspace_id,
                "target_device_id": device_id,
                "presented_pairing_hash": pairing_hash,
                "new_credential_hash": token_hash,
                "new_credential_hint": token_hint,
            },
        )
        if result is None:
            return None
        if not isinstance(result, dict):
            raise RuntimeError("Supabase edge pairing returned an invalid response")
        return {**result, "enrolled": True, "pairing_pending": False}

    def set_credential(
        self,
        device_id: str,
        *,
        token_hash: str,
        token_hint: str,
        scopes: list[str] | None = None,
    ) -> dict[str, object]:
        created_at = datetime.now(timezone.utc).isoformat()
        credential_scopes = normalize_credential_scopes(scopes)
        rows = self.client.upsert(
            "edge_devices",
            {
                "workspace_id": self.workspace_id,
                "device_id": device_id,
                "credential_hash": token_hash,
                "credential_hint": token_hint,
                "credential_scopes": credential_scopes,
                "credential_created_at": created_at,
                "revoked_at": None,
                "pairing_hash": "",
                "pairing_hint": "",
                "pairing_scopes": [],
                "pairing_expires_at": None,
            },
            on_conflict="workspace_id,device_id",
        )
        return {
            "device_id": device_id,
            "credential_hint": token_hint,
            "credential_scopes": credential_scopes,
            "credential_created_at": created_at,
            "revoked_at": None,
            "enrolled": True,
            **({"created_at": rows[0].get("created_at")} if rows else {}),
        }

    def revoke_credential(self, device_id: str) -> dict[str, object] | None:
        revoked_at = datetime.now(timezone.utc).isoformat()
        rows = self.client.update(
            "edge_devices",
            {
                "credential_hash": "",
                "credential_hint": "",
                "revoked_at": revoked_at,
                "pairing_hash": "",
                "pairing_hint": "",
                "pairing_scopes": [],
                "pairing_expires_at": None,
            },
            filters={
                "workspace_id": f"eq.{self.workspace_id}",
                "device_id": f"eq.{device_id}",
            },
        )
        if not rows:
            return None
        return {"device_id": device_id, "revoked_at": revoked_at, "enrolled": False}

    def credential_matches(self, device_id: str, token_hash: str) -> bool:
        rows = self.client.select(
            "edge_devices",
            columns="credential_hash,revoked_at",
            filters={
                "workspace_id": f"eq.{self.workspace_id}",
                "device_id": f"eq.{device_id}",
            },
            limit=1,
        )
        if not rows or rows[0].get("revoked_at") is not None:
            return False
        expected = str(rows[0].get("credential_hash") or "")
        return bool(expected and hmac.compare_digest(expected, token_hash))

    def credential_authorizes(
        self, device_id: str, token_hash: str, scope: str
    ) -> bool:
        clean_scope = str(scope).strip().lower()
        if clean_scope not in EDGE_CREDENTIAL_SCOPES:
            return False
        rows = self.client.select(
            "edge_devices",
            columns="credential_hash,credential_scopes,revoked_at",
            filters={
                "workspace_id": f"eq.{self.workspace_id}",
                "device_id": f"eq.{device_id}",
            },
            limit=1,
        )
        if not rows or rows[0].get("revoked_at") is not None:
            return False
        expected = str(rows[0].get("credential_hash") or "")
        scopes = rows[0].get("credential_scopes")
        return bool(
            expected
            and hmac.compare_digest(expected, token_hash)
            and isinstance(scopes, list)
            and clean_scope in scopes
        )

    def enqueue(
        self,
        device_id: str,
        kind: str,
        payload: Mapping[str, object],
        *,
        ttl_seconds: float,
    ) -> EdgeCommand:
        result = self.client.rpc(
            "magpie_enqueue_edge_command",
            {
                "target_workspace_id": self.workspace_id,
                "target_device_id": device_id,
                "command_kind": kind,
                "command_payload": dict(payload),
                "ttl_seconds": ttl_seconds,
            },
        )
        if not isinstance(result, dict):
            raise RuntimeError("Supabase edge enqueue returned an invalid response")
        return _command(result)

    def heartbeat(
        self,
        device_id: str,
        *,
        version: str = "",
        capabilities: list[str] | None = None,
    ) -> dict[str, object]:
        rows = self.client.upsert(
            "edge_devices",
            {
                "workspace_id": self.workspace_id,
                "device_id": device_id,
                "version": version,
                "capabilities": capabilities or [],
                "last_seen_at": datetime.now(timezone.utc).isoformat(),
            },
            on_conflict="workspace_id,device_id",
        )
        return rows[0] if rows else {"device_id": device_id}

    def claim(
        self, device_id: str, *, claim_timeout: float, max_attempts: int
    ) -> EdgeCommand | None:
        result = self.client.rpc(
            "magpie_claim_edge_command",
            {
                "target_workspace_id": self.workspace_id,
                "target_device_id": device_id,
                "claim_timeout_seconds": claim_timeout,
                "attempt_limit": max_attempts,
            },
        )
        if result is None:
            return None
        if not isinstance(result, dict):
            raise RuntimeError("Supabase edge claim returned an invalid response")
        return _command(result)

    def complete(
        self,
        device_id: str,
        command_id: str,
        *,
        succeeded: bool,
        error: str = "",
        retryable: bool = False,
        max_attempts: int = 5,
    ) -> EdgeCommand | None:
        result = self.client.rpc(
            "magpie_complete_edge_command",
            {
                "target_workspace_id": self.workspace_id,
                "target_device_id": device_id,
                "target_command_id": command_id,
                "command_succeeded": succeeded,
                "command_error": error[:1000],
                "command_retryable": retryable,
                "attempt_limit": max_attempts,
            },
        )
        return _command(result) if isinstance(result, dict) else None

    def status(self) -> dict[str, object]:
        filters = {"workspace_id": f"eq.{self.workspace_id}"}
        devices = self.client.select(
            "edge_devices",
            columns=(
                "workspace_id,device_id,version,capabilities,last_seen_at,created_at,"
                "updated_at,credential_hint,credential_created_at,revoked_at,"
                "credential_scopes,pairing_hint,pairing_scopes,pairing_expires_at"
            ),
            filters=filters,
            order="device_id",
        )
        commands = self.client.select(
            "edge_commands", filters=filters, order="created_at.desc", limit=100
        )
        counts: dict[tuple[str, str], int] = {}
        for command in commands:
            key = (str(command.get("device_id", "")), str(command.get("status", "")))
            counts[key] = counts.get(key, 0) + 1
        return {
            "devices": [
                {
                    **device,
                    "enrolled": bool(
                        device.get("credential_hint")
                        and device.get("revoked_at") is None
                    ),
                    "pairing_pending": bool(
                        device.get("pairing_hint")
                        and str(device.get("pairing_expires_at") or "")
                        > datetime.now(timezone.utc).isoformat()
                    ),
                }
                for device in devices
            ],
            "counts": [
                {"device_id": key[0], "status": key[1], "count": value}
                for key, value in sorted(counts.items())
            ],
            "recent_commands": commands[:20],
        }


class WorkspaceEdgeRepositoryRouter:
    """Route edge state through the workspace already bound to the request."""

    def __init__(
        self,
        factory: Callable[[str], EdgeRepository],
        workspace_id: Callable[[], str],
    ) -> None:
        self._factory = factory
        self._workspace_id = workspace_id
        self._repositories: dict[str, EdgeRepository] = {}
        self._lock = threading.RLock()

    def current(self) -> EdgeRepository:
        workspace_id = self._workspace_id().strip()
        if not workspace_id or workspace_id == "request-scoped":
            raise RuntimeError("no Magpie workspace is bound to this edge request")
        return self.for_workspace(workspace_id)

    def for_workspace(self, workspace_id: str) -> EdgeRepository:
        workspace_id = workspace_id.strip()
        if not workspace_id or workspace_id == "request-scoped":
            raise RuntimeError("a concrete Magpie workspace is required")
        with self._lock:
            repository = self._repositories.get(workspace_id)
            if repository is None:
                repository = self._factory(workspace_id)
                self._repositories[workspace_id] = repository
            return repository

    def __getattr__(self, name: str) -> object:
        return getattr(self.current(), name)


class QueuedEdgeWriter:
    def __init__(
        self,
        repository: EdgeRepository,
        *,
        device_id: str,
        ttl_seconds: float,
        on_enqueue: Callable[[EdgeCommand], None] | None = None,
    ) -> None:
        self.repository = repository
        self.device_id = device_id
        self.ttl_seconds = ttl_seconds
        self.on_enqueue = on_enqueue

    def write_pixels(
        self, pixels: list[tuple[int, int, int]], config: LedConfig
    ) -> None:
        command = self.repository.enqueue(
            self.device_id,
            "set_pixels",
            {
                "pixels": [list(pixel) for pixel in pixels],
                "pixel_count": config.pixel_count,
            },
            ttl_seconds=self.ttl_seconds,
        )
        if self.on_enqueue is not None:
            self.on_enqueue(command)


class WorkspaceQueuedEdgeWriter:
    """Queue complete pixel state for the location-selected edge device."""

    def __init__(
        self,
        repository: EdgeRepository,
        *,
        workspace_id: Callable[[], str],
        ttl_seconds: float,
        on_enqueue: Callable[[EdgeCommand], None] | None = None,
    ) -> None:
        self.repository = repository
        self.workspace_id = workspace_id
        self.ttl_seconds = ttl_seconds
        self.on_enqueue = on_enqueue

    def write_pixels(
        self,
        device_id: str,
        pixels: list[tuple[int, int, int]],
        *,
        pixel_count: int,
        workspace_id: str = "",
    ) -> None:
        selected_workspace = workspace_id.strip() or self.workspace_id().strip()
        repository_for_workspace = getattr(self.repository, "for_workspace", None)
        repository = (
            repository_for_workspace(selected_workspace)
            if callable(repository_for_workspace)
            else self.repository
        )
        command = repository.enqueue(
            device_id,
            "set_pixels",
            {
                "pixels": [list(pixel) for pixel in pixels],
                "pixel_count": pixel_count,
            },
            ttl_seconds=self.ttl_seconds,
        )
        if self.on_enqueue is not None:
            self.on_enqueue(command)


def create_edge_repository(
    storage: StorageConfig,
    *,
    sqlite_path: str | Path,
    environ: Mapping[str, str] | None = None,
) -> EdgeRepository:
    if storage.backend == "sqlite":
        return SqliteEdgeRepository(sqlite_path)
    import os

    values = environ if environ is not None else os.environ
    api_key = values.get(storage.api_key_env, "").strip()
    if not api_key:
        raise ValueError(
            f"Supabase API key environment variable is not set: {storage.api_key_env}"
        )
    return SupabaseEdgeRepository(
        SupabaseRestClient(
            url=storage.supabase_url,
            api_key=api_key,
            timeout_seconds=storage.timeout_seconds,
        ),
        workspace_id=storage.workspace_id,
    )


def create_workspace_edge_repository(
    storage: StorageConfig,
    *,
    workspace_id: Callable[[], str],
    environ: Mapping[str, str] | None = None,
) -> EdgeRepository:
    if storage.backend != "supabase":
        raise ValueError("workspace-routed edge storage requires Supabase")
    import os

    values = environ if environ is not None else os.environ
    api_key = values.get(storage.api_key_env, "").strip()
    if not api_key:
        raise ValueError(
            f"Supabase API key environment variable is not set: {storage.api_key_env}"
        )
    client = SupabaseRestClient(
        url=storage.supabase_url,
        api_key=api_key,
        timeout_seconds=storage.timeout_seconds,
    )
    return WorkspaceEdgeRepositoryRouter(
        lambda selected: SupabaseEdgeRepository(client, workspace_id=selected),
        workspace_id,
    )  # type: ignore[return-value]
