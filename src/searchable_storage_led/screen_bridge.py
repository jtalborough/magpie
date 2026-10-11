from __future__ import annotations

import argparse
from contextlib import asynccontextmanager
from importlib import resources
import os
import re
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, Response
import httpx
from pydantic import BaseModel
import uvicorn

from .machine_auth import cloudflare_access_headers


LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
DEVICE_ID_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


class ScreenHeartbeat(BaseModel):
    screen_id: str = "cabinet-kiosk"
    url: str = ""
    build_label: str = ""
    last_seen_interaction_id: int = 0
    status: str = "active"


def validate_screen_bridge(
    central_url: str,
    device_id: str,
    token: str,
    bind_host: str = "127.0.0.1",
    workspace_id: str = "",
) -> tuple[str, str]:
    parsed = urlsplit(central_url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("MAGPIE_SCREEN_API_URL must be an HTTP(S) origin")
    if parsed.scheme != "https" and parsed.hostname not in LOOPBACK_HOSTS:
        raise ValueError("the authenticated Magpie screen API must use HTTPS")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("MAGPIE_SCREEN_API_URL must not contain credentials or parameters")
    if parsed.path not in {"", "/"}:
        raise ValueError("MAGPIE_SCREEN_API_URL must not contain a path")
    clean_device_id = device_id.strip().lower()
    if not DEVICE_ID_PATTERN.fullmatch(clean_device_id):
        raise ValueError("MAGPIE_EDGE_DEVICE_ID is invalid")
    if not token.strip():
        raise ValueError("MAGPIE_EDGE_TOKEN is required")
    if bind_host.strip().lower() not in LOOPBACK_HOSTS:
        raise ValueError("the screen bridge must bind to loopback")
    return central_url.strip().rstrip("/"), clean_device_id


def create_screen_bridge(
    *,
    central_url: str,
    device_id: str,
    token: str,
    workspace_id: str = "",
    cf_access_client_id: str = "",
    cf_access_client_secret: str = "",
    bind_host: str = "127.0.0.1",
    client: httpx.AsyncClient | None = None,
) -> FastAPI:
    base_url, clean_device_id = validate_screen_bridge(
        central_url,
        device_id,
        token,
        bind_host=bind_host,
        workspace_id=workspace_id,
    )
    owned_client = client is None
    http = client or httpx.AsyncClient(
        timeout=httpx.Timeout(35.0, connect=10.0),
        follow_redirects=False,
    )
    upstream_headers = {
        "Authorization": f"Bearer {token.strip()}",
        "X-Magpie-Device-ID": clean_device_id,
        **cloudflare_access_headers(
            cf_access_client_id,
            cf_access_client_secret,
        ),
    }
    if workspace_id.strip():
        upstream_headers["X-Magpie-Workspace"] = workspace_id.strip()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        if owned_client:
            await http.aclose()

    app = FastAPI(title="Magpie screen bridge", lifespan=lifespan)

    def static_asset(name: str, media_type: str, *, no_store: bool = False) -> Response:
        content = (
            resources.files("searchable_storage_led")
            .joinpath("static", name)
            .read_bytes()
        )
        return Response(
            content=content,
            media_type=media_type,
            headers={
                "Cache-Control": (
                    "no-cache, no-store, must-revalidate"
                    if no_store
                    else "public, max-age=604800"
                )
            },
        )

    async def forward(
        method: str,
        path: str,
        *,
        params: dict[str, object] | None = None,
        json: dict[str, object] | None = None,
    ) -> Response:
        try:
            upstream = await http.request(
                method,
                f"{base_url}{path}",
                headers=upstream_headers,
                params=params,
                json=json,
            )
        except httpx.HTTPError as exc:
            return JSONResponse(
                status_code=502,
                content={"detail": "central Magpie screen service is unavailable"},
                headers={"Cache-Control": "no-store"},
            )
        content_type = upstream.headers.get("content-type", "application/json")
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            headers={
                "Cache-Control": "no-store",
                "Content-Type": content_type,
            },
        )

    @app.get("/", response_class=HTMLResponse)
    @app.get("/kiosk", response_class=HTMLResponse)
    def kiosk() -> Response:
        return static_asset("kiosk.html", "text/html; charset=utf-8", no_store=True)

    @app.get("/app-icon.svg")
    def app_icon() -> Response:
        return static_asset("app-icon.svg", "image/svg+xml")

    @app.get("/bridge/health")
    def bridge_health() -> dict[str, object]:
        return {
            "ok": True,
            "central_url": base_url,
            "device_id": clean_device_id,
            "workspace_id": workspace_id.strip(),
            "credential_present": True,
            "machine_auth_present": bool(cf_access_client_id.strip()),
        }

    @app.get("/bridge/ready")
    async def bridge_ready(require_voice: bool = False) -> Response:
        try:
            upstream = await http.get(
                f"{base_url}/kiosk/state",
                headers=upstream_headers,
                params={"wait_seconds": 0},
            )
        except httpx.HTTPError:
            return JSONResponse(
                status_code=503,
                content={
                    "ok": False,
                    "central_ready": False,
                    "detail": "central Magpie screen service is unavailable",
                },
                headers={"Cache-Control": "no-store"},
            )
        try:
            payload = upstream.json()
        except ValueError:
            payload = None
        if (
            upstream.status_code != 200
            or not isinstance(payload, dict)
            or payload.get("ok") is not True
        ):
            return JSONResponse(
                status_code=503,
                content={
                    "ok": False,
                    "central_ready": False,
                    "detail": "central Magpie rejected the screen bridge",
                    "upstream_status": upstream.status_code,
                },
                headers={"Cache-Control": "no-store"},
            )
        voice = payload.get("voice")
        voice_reporting = bool(
            isinstance(voice, dict)
            and voice.get("active") is True
            and str(voice.get("device_id") or "").strip().lower()
            == clean_device_id
        )
        if require_voice and not voice_reporting:
            return JSONResponse(
                status_code=503,
                content={
                    "ok": False,
                    "central_ready": True,
                    "voice_reporting": False,
                    "detail": "this cabinet voice service is not reporting to central Magpie",
                },
                headers={"Cache-Control": "no-store"},
            )
        return JSONResponse(
            content={
                "ok": True,
                "central_ready": True,
                "voice_reporting": voice_reporting,
                "central_url": base_url,
                "device_id": clean_device_id,
                "workspace_id": workspace_id.strip(),
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/build")
    async def build() -> Response:
        return await forward("GET", "/build")

    @app.get("/kiosk/state")
    async def kiosk_state(
        after_id: int = 0,
        voice_updated_at: float = -1,
        wait_seconds: float = 20,
        screen_id: str = "",
        screen_url: str = "",
        build_label: str = "",
        status: str = "active",
        last_seen_interaction_id: int = 0,
    ) -> Response:
        return await forward(
            "GET",
            "/kiosk/state",
            params={
                "after_id": max(0, int(after_id)),
                "voice_updated_at": float(voice_updated_at),
                "wait_seconds": max(0.0, min(float(wait_seconds), 25.0)),
                "screen_id": screen_id.strip(),
                "screen_url": screen_url.strip(),
                "build_label": build_label.strip(),
                "status": status.strip() or "active",
                "last_seen_interaction_id": max(
                    0, int(last_seen_interaction_id or 0)
                ),
            },
        )

    @app.post("/screen/heartbeat")
    async def screen_heartbeat(heartbeat: ScreenHeartbeat) -> Response:
        return await forward(
            "POST",
            "/screen/heartbeat",
            json=heartbeat.model_dump(),
        )

    @app.post("/kiosk/actions/confirm")
    async def kiosk_confirm() -> Response:
        return await forward("POST", "/kiosk/actions/confirm")

    @app.post("/kiosk/actions/cancel")
    async def kiosk_cancel() -> Response:
        return await forward("POST", "/kiosk/actions/cancel")

    @app.post("/kiosk/actions/undo")
    async def kiosk_undo() -> Response:
        return await forward("POST", "/kiosk/actions/undo")

    @app.post("/kiosk/actions/clear")
    async def kiosk_clear() -> Response:
        return await forward("POST", "/kiosk/actions/clear")

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Magpie kiosk screen bridge")
    parser.add_argument(
        "--central-url",
        default=os.environ.get("MAGPIE_SCREEN_API_URL", ""),
    )
    parser.add_argument(
        "--device-id",
        default=os.environ.get("MAGPIE_EDGE_DEVICE_ID", "main-cabinet"),
    )
    parser.add_argument("--token", default=os.environ.get("MAGPIE_EDGE_TOKEN", ""))
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
        "--bind-host",
        default=os.environ.get("MAGPIE_SCREEN_BIND_HOST", "127.0.0.1"),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("MAGPIE_SCREEN_PORT", "8767")),
    )
    args = parser.parse_args()
    if not args.central_url:
        parser.error("MAGPIE_SCREEN_API_URL is required")
    try:
        app = create_screen_bridge(
            central_url=args.central_url,
            device_id=args.device_id,
            token=args.token,
            workspace_id=args.workspace_id,
            cf_access_client_id=args.cf_access_client_id,
            cf_access_client_secret=args.cf_access_client_secret,
            bind_host=args.bind_host,
        )
    except ValueError as exc:
        parser.error(str(exc))
    uvicorn.run(app, host=args.bind_host, port=args.port)


if __name__ == "__main__":
    main()
