#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import urllib.error
import urllib.parse
import urllib.request


def required_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def create_pairing(
    *,
    supabase_url: str,
    service_role_key: str,
    workspace_id: str,
    device_id: str,
    ttl_seconds: int = 600,
) -> tuple[str, str]:
    pairing_code = secrets.token_urlsafe(24)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
    payload = {
        "workspace_id": workspace_id,
        "device_id": device_id,
        "pairing_hash": hashlib.sha256(pairing_code.encode("utf-8")).hexdigest(),
        "pairing_hint": pairing_code[-6:],
        "pairing_scopes": ["commands", "screen", "voice"],
        "pairing_expires_at": expires_at.isoformat(),
    }
    query = urllib.parse.urlencode(
        {"on_conflict": "workspace_id,device_id"},
        safe=",",
    )
    request = urllib.request.Request(
        f"{supabase_url.rstrip('/')}/rest/v1/edge_devices?{query}",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "apikey": service_role_key,
            "Authorization": f"Bearer {service_role_key}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
            "User-Agent": "Magpie-Edge-Pairing/0.1",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            if response.status not in {200, 201, 204}:
                raise RuntimeError(f"Supabase pairing request failed ({response.status})")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Supabase pairing request failed ({exc.code}): {detail}"
        ) from exc
    return pairing_code, expires_at.isoformat()


def write_secret(path: Path, value: str) -> None:
    path.unlink(missing_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(value)
        handle.write("\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create a short-lived cabinet pairing code without logging it."
    )
    parser.add_argument("--device-id", default="main-cabinet")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    pairing_code, expires_at = create_pairing(
        supabase_url=required_environment("MAGPIE_SUPABASE_URL"),
        service_role_key=required_environment("MAGPIE_SUPABASE_SERVICE_ROLE_KEY"),
        workspace_id=required_environment("MAGPIE_SUPABASE_WORKSPACE_ID"),
        device_id=args.device_id,
    )
    write_secret(args.output, pairing_code)
    print(f"Created short-lived pairing for {args.device_id}; expires {expires_at}")


if __name__ == "__main__":
    main()
