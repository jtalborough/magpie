#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import socket
from time import perf_counter
from urllib.parse import urlencode, urlparse

import httpx

from searchable_storage_led.config import LedConfig
from searchable_storage_led.repositories import StorageRepositories


def timed(label: str, operation):
    started = perf_counter()
    try:
        result = operation()
    except Exception as exc:  # diagnostics must report all lanes
        print(
            json.dumps(
                {
                    "label": label,
                    "ok": False,
                    "seconds": round(perf_counter() - started, 3),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
        )
        return None
    size = len(result) if hasattr(result, "__len__") else None
    print(
        json.dumps(
            {
                "label": label,
                "ok": True,
                "seconds": round(perf_counter() - started, 3),
                "result_count": size,
            }
        )
    )
    return result


def main() -> None:
    config = LedConfig.from_path(os.environ.get("LED_CONFIG"))
    storage = config.storage
    if storage.backend != "supabase":
        print(json.dumps({"ok": True, "skipped": "storage backend is not Supabase"}))
        return

    api_key = os.environ.get(storage.api_key_env, "").strip()
    if not api_key:
        raise SystemExit(f"{storage.api_key_env} is not set")

    host = urlparse(storage.supabase_url).hostname or ""
    addresses = sorted(
        {
            ("ipv6" if family == socket.AF_INET6 else "ipv4", address[0])
            for family, _, _, _, address in socket.getaddrinfo(host, 443)
            if family in {socket.AF_INET, socket.AF_INET6}
        }
    )
    print(json.dumps({"host": host, "addresses": addresses}))

    repositories = StorageRepositories.from_config(
        storage,
        sqlite_path=config.database_path,
    )
    client = repositories.inventory.client
    filters = {"workspace_id": f"eq.{storage.workspace_id}"}
    timed(
        "default-select-1",
        lambda: client.select("locations", columns="local_code", filters=filters, limit=1),
    )
    timed(
        "default-select-2",
        lambda: client.select("locations", columns="local_code", filters=filters, limit=1),
    )

    query = urlencode({"select": "local_code", **filters, "limit": "1"})
    url = f"{storage.supabase_url.rstrip('/')}/rest/v1/locations?{query}"
    headers = {"apikey": api_key, "Authorization": f"Bearer {api_key}"}
    transport = httpx.HTTPTransport(local_address="0.0.0.0", retries=0)
    with httpx.Client(transport=transport, timeout=storage.timeout_seconds) as ipv4:
        timed("ipv4-select-1", lambda: ipv4.get(url, headers=headers).raise_for_status().json())
        timed("ipv4-select-2", lambda: ipv4.get(url, headers=headers).raise_for_status().json())

    timed("locations-all-1", repositories.locations.all)
    timed("locations-all-2", repositories.locations.all)
    timed("location-counts-1", repositories.inventory.location_item_counts)
    timed("location-counts-2", repositories.inventory.location_item_counts)


if __name__ == "__main__":
    main()
