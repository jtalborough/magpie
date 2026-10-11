from __future__ import annotations

import base64
import json
import re


_PUBLISHABLE_KEY = re.compile(r"sb_publishable_[A-Za-z0-9_-]{20,}")
_JWT_SEGMENT = re.compile(r"[A-Za-z0-9_-]+")


def is_supabase_publishable_key(value: object) -> bool:
    """Accept current publishable keys and legacy anon-role JWT keys."""
    key = str(value or "").strip()
    if not key or any(character.isspace() for character in key):
        return False
    if _PUBLISHABLE_KEY.fullmatch(key):
        return True
    parts = key.split(".")
    if len(parts) != 3 or any(not _JWT_SEGMENT.fullmatch(part) for part in parts):
        return False
    try:
        payload_segment = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(
            base64.urlsafe_b64decode(payload_segment).decode("utf-8")
        )
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and payload.get("role") == "anon"


def require_supabase_publishable_key(value: object, *, setting: str) -> str:
    key = str(value or "").strip()
    if not key:
        raise ValueError(f"Supabase publishable key is not set: {setting}")
    if not is_supabase_publishable_key(key):
        raise ValueError(
            f"{setting} must contain a Supabase publishable key or legacy anon key"
        )
    return key
