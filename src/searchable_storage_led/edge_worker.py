from __future__ import annotations

import argparse
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

import httpx

from .build import build_info
from .machine_auth import cloudflare_access_headers


class EdgeWorkerError(RuntimeError):
    pass


LOCAL_EDGE_CAPABILITIES = frozenset(
    {"set_pixels", "voice_assistant", "kiosk_display", "label_printer", "check_station"}
)


def _validate_edge_token(token: str) -> str:
    clean = token.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", clean):
        raise EdgeWorkerError("pairing response included an invalid device credential")
    return clean


def load_edge_machine_auth(path: str | Path) -> tuple[str, str]:
    target = Path(path).expanduser()
    if not target.exists():
        return "", ""
    values: dict[str, str] = {}
    names = {
        "MAGPIE_CF_ACCESS_CLIENT_ID",
        "MAGPIE_CF_ACCESS_CLIENT_SECRET",
    }
    for line in target.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator and key in names:
            values[key] = value
    client_id = values.get("MAGPIE_CF_ACCESS_CLIENT_ID", "")
    client_secret = values.get("MAGPIE_CF_ACCESS_CLIENT_SECRET", "")
    cloudflare_access_headers(client_id, client_secret)
    return client_id.strip(), client_secret.strip()


def validate_worker_urls(central_url: str, local_url: str) -> None:
    central = urlsplit(central_url)
    local = urlsplit(local_url)
    loopback_hosts = {"127.0.0.1", "localhost", "::1"}
    if central.scheme != "https" and central.hostname not in loopback_hosts:
        raise ValueError("the central edge URL must use HTTPS")
    if local.scheme != "http" or local.hostname not in loopback_hosts:
        raise ValueError("the local controller URL must use loopback HTTP")


def pair_device(
    *,
    central_url: str,
    device_id: str,
    pairing_code: str,
    workspace_id: str = "",
    cf_access_client_id: str = "",
    cf_access_client_secret: str = "",
    client: httpx.Client | None = None,
) -> str:
    validate_worker_urls(central_url, "http://127.0.0.1")
    owned_client = client is None
    http = client or httpx.Client(timeout=20.0)
    headers = cloudflare_access_headers(
        cf_access_client_id,
        cf_access_client_secret,
    )
    if workspace_id.strip():
        headers["X-Magpie-Workspace"] = workspace_id.strip()
    try:
        response = http.post(
            f"{central_url.rstrip('/')}/edge/devices/{device_id}/pair",
            headers=headers,
            json={"pairing_code": pairing_code.strip()},
        )
        response.raise_for_status()
        body = response.json()
        token = str(body.get("token") or "") if isinstance(body, dict) else ""
        return _validate_edge_token(token)
    finally:
        if owned_client:
            http.close()


def store_edge_token(path: str | Path, token: str) -> Path:
    token = _validate_edge_token(token)
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    existing = target.read_text(encoding="utf-8").splitlines() if target.exists() else []
    lines = [line for line in existing if not line.startswith("MAGPIE_EDGE_TOKEN=")]
    lines.append(f"MAGPIE_EDGE_TOKEN={token}")
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=target.parent,
        prefix=f".{target.name}.",
        delete=False,
    ) as handle:
        handle.write("\n".join(lines) + "\n")
        temporary = Path(handle.name)
    temporary.chmod(0o600)
    temporary.replace(target)
    return target


def execute_local_command(
    command: dict[str, object], *, local_url: str, client: httpx.Client
) -> None:
    kind = str(command.get("kind", ""))
    payload = command.get("payload")
    if not isinstance(payload, dict):
        raise EdgeWorkerError(f"unsupported edge command: {kind or 'missing kind'}")
    if kind == "print_zpl":
        zpl = payload.get("zpl")
        if (
            not isinstance(zpl, str)
            or not zpl.strip().startswith("^XA")
            or not zpl.strip().endswith("^XZ")
            or len(zpl.encode("utf-8")) > 256 * 1024
        ):
            raise EdgeWorkerError("print_zpl payload contains invalid ZPL")
        response = client.post(
            f"{local_url.rstrip('/')}/edge/local/print-zpl",
            json={"zpl": zpl},
        )
        response.raise_for_status()
        return
    if kind != "set_pixels":
        raise EdgeWorkerError(f"unsupported edge command: {kind or 'missing kind'}")
    raw_pixels = payload.get("pixels")
    if not isinstance(raw_pixels, list):
        raise EdgeWorkerError("set_pixels payload is missing pixels")
    updates: list[list[object]] = []
    for index, color in enumerate(raw_pixels):
        if not isinstance(color, list) or len(color) != 3:
            raise EdgeWorkerError("set_pixels payload contains an invalid color")
        values = [int(channel) for channel in color]
        if any(channel < 0 or channel > 255 for channel in values):
            raise EdgeWorkerError("set_pixels payload contains an invalid channel")
        if any(values):
            updates.append([index, values])
    response = client.post(
        f"{local_url.rstrip('/')}/pixels",
        json={"pixels": updates, "clear_others": True, "brightness": 1.0},
    )
    response.raise_for_status()


def discover_local_capabilities(
    *, local_url: str, client: httpx.Client
) -> list[str]:
    try:
        response = client.get(f"{local_url.rstrip('/')}/edge/local-capabilities")
        response.raise_for_status()
        body = response.json()
    except (httpx.HTTPError, ValueError):
        return ["set_pixels"]
    capabilities = body.get("capabilities") if isinstance(body, dict) else None
    if not isinstance(capabilities, list):
        return ["set_pixels"]
    return sorted(
        {
            str(capability)
            for capability in capabilities
            if str(capability) in LOCAL_EDGE_CAPABILITIES
        }
    )


def run_once(
    *,
    central_url: str,
    local_url: str,
    device_id: str,
    token: str,
    workspace_id: str = "",
    cf_access_client_id: str = "",
    cf_access_client_secret: str = "",
    version: str = "",
    heartbeat: bool = True,
    wait_seconds: float = 0,
    client: httpx.Client | None = None,
    executor: Callable[..., None] = execute_local_command,
) -> bool:
    validate_worker_urls(central_url, local_url)
    clean_wait = max(0.0, min(float(wait_seconds), 25.0))
    owned_client = client is None
    http = client or httpx.Client(timeout=max(15.0, clean_wait + 10.0))
    headers = {
        "Authorization": f"Bearer {token}",
        **cloudflare_access_headers(
            cf_access_client_id,
            cf_access_client_secret,
        ),
    }
    if workspace_id.strip():
        headers["X-Magpie-Workspace"] = workspace_id.strip()
    try:
        if heartbeat:
            capabilities = discover_local_capabilities(
                local_url=local_url,
                client=http,
            )
            heartbeat_response = http.post(
                f"{central_url.rstrip('/')}/edge/devices/{device_id}/heartbeat",
                headers=headers,
                json={"version": version, "capabilities": capabilities},
            )
            heartbeat_response.raise_for_status()
        response = http.post(
            f"{central_url.rstrip('/')}/edge/devices/{device_id}/commands/claim",
            headers=headers,
            params={"wait_seconds": clean_wait},
        )
        response.raise_for_status()
        body = response.json()
        command = body.get("command") if isinstance(body, dict) else None
        if not isinstance(command, dict):
            return False
        error = ""
        succeeded = True
        retryable = False
        try:
            executor(command, local_url=local_url, client=http)
        except Exception as exc:
            succeeded = False
            error = str(exc)
            retryable = not isinstance(exc, EdgeWorkerError)
        completion = http.post(
            f"{central_url.rstrip('/')}/edge/devices/{device_id}/commands/{command['id']}/complete",
            headers=headers,
            json={"succeeded": succeeded, "error": error, "retryable": retryable},
        )
        completion.raise_for_status()
        return succeeded
    finally:
        if owned_client:
            http.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Magpie cabinet edge worker")
    parser.add_argument(
        "--central-url", default=os.environ.get("MAGPIE_CENTRAL_URL", "")
    )
    parser.add_argument(
        "--local-url",
        default=os.environ.get("MAGPIE_LOCAL_URL", "http://127.0.0.1:8765"),
    )
    parser.add_argument(
        "--device-id", default=os.environ.get("MAGPIE_EDGE_DEVICE_ID", "main-cabinet")
    )
    parser.add_argument("--token", default=os.environ.get("MAGPIE_EDGE_TOKEN", ""))
    parser.add_argument(
        "--pairing-code",
        default=os.environ.get("MAGPIE_EDGE_PAIRING_CODE", ""),
    )
    parser.add_argument(
        "--secrets-path",
        default=os.environ.get(
            "MAGPIE_EDGE_SECRETS_PATH",
            str(Path.home() / "searchable-storage-led" / "secrets.env"),
        ),
    )
    parser.add_argument(
        "--workspace-id",
        default=os.environ.get("MAGPIE_EDGE_WORKSPACE_ID", ""),
    )
    parser.add_argument(
        "--cf-access-client-id",
        default=os.environ.get("MAGPIE_CF_ACCESS_CLIENT_ID", ""),
    )
    parser.add_argument(
        "--cf-access-client-secret",
        default=os.environ.get("MAGPIE_CF_ACCESS_CLIENT_SECRET", ""),
    )
    parser.add_argument(
        "--version",
        default=os.environ.get("MAGPIE_EDGE_VERSION", build_info().label()),
    )
    parser.add_argument(
        "--wait-seconds",
        type=float,
        default=float(os.environ.get("MAGPIE_EDGE_WAIT_SECONDS", "20")),
    )
    parser.add_argument(
        "--heartbeat-seconds",
        type=float,
        default=float(os.environ.get("MAGPIE_EDGE_HEARTBEAT_SECONDS", "60")),
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=float(os.environ.get("MAGPIE_EDGE_POLL_SECONDS", "1")),
    )
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--pair-only", action="store_true")
    args = parser.parse_args()
    if not args.central_url:
        parser.error("MAGPIE_CENTRAL_URL is required")
    if not args.cf_access_client_id and not args.cf_access_client_secret:
        try:
            (
                args.cf_access_client_id,
                args.cf_access_client_secret,
            ) = load_edge_machine_auth(args.secrets_path)
        except (OSError, UnicodeError, ValueError) as exc:
            parser.error(f"could not load edge machine authentication: {exc}")
    try:
        cloudflare_access_headers(
            args.cf_access_client_id,
            args.cf_access_client_secret,
        )
    except ValueError as exc:
        parser.error(str(exc))
    if args.pairing_code:
        args.token = pair_device(
            central_url=args.central_url,
            device_id=args.device_id,
            pairing_code=args.pairing_code,
            workspace_id=args.workspace_id,
            cf_access_client_id=args.cf_access_client_id,
            cf_access_client_secret=args.cf_access_client_secret,
        )
        credential_path = store_edge_token(args.secrets_path, args.token)
        print(
            f"paired {args.device_id}; device credential stored in {credential_path}",
            flush=True,
        )
        if args.pair_only:
            return
    if not args.token:
        parser.error("MAGPIE_EDGE_TOKEN or MAGPIE_EDGE_PAIRING_CODE is required")
    last_heartbeat = 0.0
    failures = 0
    while True:
        now = time.monotonic()
        send_heartbeat = now - last_heartbeat >= max(5.0, args.heartbeat_seconds)
        try:
            processed = run_once(
                central_url=args.central_url,
                local_url=args.local_url,
                device_id=args.device_id,
                token=args.token,
                workspace_id=args.workspace_id,
                cf_access_client_id=args.cf_access_client_id,
                cf_access_client_secret=args.cf_access_client_secret,
                version=args.version,
                heartbeat=send_heartbeat,
                wait_seconds=0 if args.once else args.wait_seconds,
            )
            if send_heartbeat:
                last_heartbeat = time.monotonic()
            failures = 0
        except (httpx.HTTPError, ValueError) as exc:
            failures += 1
            print(f"edge worker request failed: {exc}", flush=True)
            if not args.once:
                time.sleep(min(30.0, max(1.0, float(2 ** min(failures, 5)))))
        if args.once:
            return
        if failures == 0:
            time.sleep(0.2 if processed else max(0.2, args.poll_seconds))


if __name__ == "__main__":
    main()
