import asyncio
import json

from fastapi.testclient import TestClient
import httpx
import pytest

from searchable_storage_led.screen_bridge import (
    create_screen_bridge,
    validate_screen_bridge,
)


def test_screen_bridge_requires_secure_origin_loopback_and_credentials() -> None:
    assert validate_screen_bridge(
        "https://magpie.example.com/", "Main-Cabinet", "secret"
    ) == ("https://magpie.example.com", "main-cabinet")
    assert validate_screen_bridge(
        "http://127.0.0.1:8765", "screen-1", "secret"
    ) == ("http://127.0.0.1:8765", "screen-1")

    with pytest.raises(ValueError, match="HTTPS"):
        validate_screen_bridge("http://magpie.example.com", "screen-1", "secret")
    with pytest.raises(ValueError, match="credentials or parameters"):
        validate_screen_bridge(
            "https://token@magpie.example.com?device=x", "screen-1", "secret"
        )
    with pytest.raises(ValueError, match="must not contain a path"):
        validate_screen_bridge("https://magpie.example.com/api", "screen-1", "secret")
    with pytest.raises(ValueError, match="DEVICE"):
        validate_screen_bridge("https://magpie.example.com", "bad/device", "secret")
    with pytest.raises(ValueError, match="TOKEN"):
        validate_screen_bridge("https://magpie.example.com", "screen-1", "")
    with pytest.raises(ValueError, match="loopback"):
        validate_screen_bridge(
            "https://magpie.example.com", "screen-1", "secret", "0.0.0.0"
        )


def test_screen_bridge_serves_kiosk_and_forwards_only_screen_contract() -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["authorization"] == "Bearer edge-secret"
        assert request.headers["x-magpie-device-id"] == "main-cabinet"
        assert request.headers["cf-access-client-id"] == "machine-id.access"
        assert request.headers["cf-access-client-secret"] == "machine-secret"
        if request.url.path == "/build":
            return httpx.Response(200, json={"label": "v0.1.0 test"})
        if request.url.path == "/kiosk/state":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "voice": {
                        "state": "waiting",
                        "active": True,
                        "device_id": "main-cabinet",
                        "active_finds": [],
                    },
                    "interactions": [],
                },
            )
        if request.url.path == "/screen/heartbeat":
            return httpx.Response(200, json={"ok": True})
        if request.url.path.startswith("/kiosk/actions/"):
            return httpx.Response(200, json={"ok": True, "action": request.url.path.rsplit("/", 1)[-1]})
        return httpx.Response(404, json={"detail": "unexpected path"})

    upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app = create_screen_bridge(
        central_url="https://magpie.example.com",
        device_id="main-cabinet",
        token="edge-secret",
        workspace_id="workspace-1",
        cf_access_client_id="machine-id.access",
        cf_access_client_secret="machine-secret",
        client=upstream,
    )
    with TestClient(app) as client:
        kiosk = client.get("/kiosk")
        assert kiosk.status_code == 200
        assert "Magpie Kiosk" in kiosk.text
        assert "edge-secret" not in kiosk.text
        assert client.get("/app-icon.svg").status_code == 200
        assert client.get("/bridge/health").json() == {
            "ok": True,
            "central_url": "https://magpie.example.com",
            "device_id": "main-cabinet",
            "workspace_id": "workspace-1",
            "credential_present": True,
            "machine_auth_present": True,
        }
        assert client.get("/bridge/ready").json() == {
            "ok": True,
            "central_ready": True,
            "voice_reporting": True,
            "central_url": "https://magpie.example.com",
            "device_id": "main-cabinet",
            "workspace_id": "workspace-1",
        }
        assert client.get("/bridge/ready?require_voice=true").status_code == 200
        assert client.get("/build").json()["label"] == "v0.1.0 test"
        state = client.get(
            "/kiosk/state?after_id=7&voice_updated_at=12.5&wait_seconds=90"
            "&screen_id=workshop-screen&screen_url=http%3A%2F%2F127.0.0.1%3A8767%2Fkiosk"
            "&build_label=test-build"
            "&status=active&last_seen_interaction_id=8"
        )
        assert state.status_code == 200
        assert client.post(
            "/screen/heartbeat",
            json={"screen_id": "cabinet-kiosk", "last_seen_interaction_id": 9},
        ).status_code == 200
        for action in ("confirm", "cancel", "undo", "clear"):
            response = client.post(f"/kiosk/actions/{action}")
            assert response.json() == {"ok": True, "action": action}
        assert client.post("/clear").status_code == 404

    state_request = next(
        request
        for request in requests
        if request.url.path == "/kiosk/state"
        and request.url.params.get("after_id") == "7"
    )
    assert state_request.url.params["after_id"] == "7"
    assert state_request.url.params["voice_updated_at"] == "12.5"
    assert state_request.url.params["wait_seconds"] == "25.0"
    assert state_request.url.params["screen_id"] == "workshop-screen"
    assert state_request.url.params["screen_url"] == "http://127.0.0.1:8767/kiosk"
    assert state_request.url.params["build_label"] == "test-build"
    assert state_request.url.params["last_seen_interaction_id"] == "8"
    heartbeat_request = next(
        request for request in requests if request.url.path == "/screen/heartbeat"
    )
    heartbeat = json.loads(heartbeat_request.content)
    assert heartbeat["screen_id"] == "cabinet-kiosk"
    assert heartbeat["last_seen_interaction_id"] == 9
    assert heartbeat["status"] == "active"
    action_requests = [
        request for request in requests if request.url.path.startswith("/kiosk/actions/")
    ]
    assert [request.url.path for request in action_requests] == [
        "/kiosk/actions/confirm",
        "/kiosk/actions/cancel",
        "/kiosk/actions/undo",
        "/kiosk/actions/clear",
    ]
    assert all(request.method == "POST" for request in action_requests)
    assert all(
        request.headers["x-magpie-workspace"] == "workspace-1"
        for request in requests
    )
    asyncio.run(upstream.aclose())


def test_screen_bridge_fails_closed_when_central_is_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app = create_screen_bridge(
        central_url="https://magpie.example.com",
        device_id="screen-1",
        token="edge-secret",
        client=upstream,
    )
    with TestClient(app) as client:
        assert client.get("/bridge/health").status_code == 200
        ready = client.get("/bridge/ready")
        assert ready.status_code == 503
        assert ready.json() == {
            "ok": False,
            "central_ready": False,
            "detail": "central Magpie screen service is unavailable",
        }
        response = client.get("/kiosk/state?wait_seconds=0")
        assert response.status_code == 502
        assert response.json()["detail"] == "central Magpie screen service is unavailable"
    asyncio.run(upstream.aclose())


def test_screen_bridge_readiness_rejects_wrong_device_scope() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"detail": "edge credential does not allow screen"})

    upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app = create_screen_bridge(
        central_url="https://magpie.example.com",
        device_id="screen-1",
        token="edge-secret",
        client=upstream,
    )
    with TestClient(app) as client:
        ready = client.get("/bridge/ready")

    assert ready.status_code == 503
    assert ready.json() == {
        "ok": False,
        "central_ready": False,
        "detail": "central Magpie rejected the screen bridge",
        "upstream_status": 403,
    }
    asyncio.run(upstream.aclose())


def test_screen_bridge_readiness_rejects_non_magpie_success_page() -> None:
    upstream = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, text="<html>Sign in</html>")
        )
    )
    app = create_screen_bridge(
        central_url="https://magpie.example.com",
        device_id="screen-1",
        token="edge-secret",
        client=upstream,
    )
    with TestClient(app) as client:
        ready = client.get("/bridge/ready")

    assert ready.status_code == 503
    assert ready.json()["central_ready"] is False
    assert ready.json()["upstream_status"] == 200
    asyncio.run(upstream.aclose())


def test_screen_bridge_cabinet_readiness_requires_matching_voice_reporter() -> None:
    upstream = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={
                    "ok": True,
                    "voice": {
                        "active": True,
                        "device_id": "other-voice-station",
                    },
                },
            )
        )
    )
    app = create_screen_bridge(
        central_url="https://magpie.example.com",
        device_id="main-cabinet",
        token="edge-secret",
        client=upstream,
    )
    with TestClient(app) as client:
        screen_ready = client.get("/bridge/ready")
        cabinet_ready = client.get("/bridge/ready?require_voice=true")

    assert screen_ready.status_code == 200
    assert screen_ready.json()["voice_reporting"] is False
    assert cabinet_ready.status_code == 503
    assert cabinet_ready.json() == {
        "ok": False,
        "central_ready": True,
        "voice_reporting": False,
        "detail": "this cabinet voice service is not reporting to central Magpie",
    }
    asyncio.run(upstream.aclose())
