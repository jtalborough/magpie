import json
from urllib.error import HTTPError, URLError
from urllib.request import Request
from urllib.parse import parse_qs, urlparse

import pytest

from searchable_storage_led.supabase_client import (
    HttpResponse,
    SupabaseError,
    SupabaseRestClient,
    _urlopen_transport,
)


class RecordingTransport:
    def __init__(self, responses: list[HttpResponse] | None = None) -> None:
        self.responses = list(responses or [HttpResponse(200, b"[]")])
        self.requests = []

    def __call__(self, request, timeout: float) -> HttpResponse:
        self.requests.append((request, timeout))
        return self.responses.pop(0)


def _client(transport: RecordingTransport) -> SupabaseRestClient:
    return SupabaseRestClient(
        url="https://project.supabase.co/",
        api_key="secret-key",
        timeout_seconds=4.5,
        transport=transport,
    )


def test_select_sends_auth_filters_order_and_limit() -> None:
    transport = RecordingTransport(
        [HttpResponse(200, b'[{"local_id":7,"name":"T-nuts"}]')]
    )
    client = _client(transport)

    rows = client.select(
        "items",
        columns="local_id,name",
        filters={"workspace_id": "eq.workspace-1", "name": "ilike.*nut*"},
        order="local_id.desc",
        limit=10,
    )

    request, timeout = transport.requests[0]
    query = parse_qs(urlparse(request.full_url).query)
    assert rows == [{"local_id": 7, "name": "T-nuts"}]
    assert request.get_method() == "GET"
    assert request.full_url.startswith("https://project.supabase.co/rest/v1/items?")
    assert query == {
        "select": ["local_id,name"],
        "workspace_id": ["eq.workspace-1"],
        "name": ["ilike.*nut*"],
        "order": ["local_id.desc"],
        "limit": ["10"],
    }
    assert request.headers["Apikey"] == "secret-key"
    assert request.headers["Authorization"] == "Bearer secret-key"
    assert request.headers["User-agent"] == "Magpie/0.1"
    assert timeout == 4.5


def test_select_page_uses_exact_count_and_range_headers() -> None:
    transport = RecordingTransport(
        [
            HttpResponse(
                206,
                b'[{"local_id":11},{"local_id":12}]',
                {"Content-Range": "10-11/35"},
            )
        ]
    )
    client = _client(transport)

    rows, total = client.select_page(
        "inventory_entries",
        columns="local_id,item_id",
        filters={"workspace_id": "eq.workspace-1"},
        order="local_id.asc",
        limit=2,
        offset=10,
    )

    request, timeout = transport.requests[0]
    query = parse_qs(urlparse(request.full_url).query)
    assert rows == [{"local_id": 11}, {"local_id": 12}]
    assert total == 35
    assert query["order"] == ["local_id.asc"]
    assert request.headers["Prefer"] == "count=exact"
    assert request.headers["Range-unit"] == "items"
    assert request.headers["Range"] == "10-11"
    assert timeout == 4.5


def test_usage_snapshot_counts_traffic_without_exposing_queries_or_secrets() -> None:
    first_body = b'[{"local_id":7,"name":"T-nuts"}]'
    second_body = b'[{"local_id":11}]'
    error_body = b'{"message":"duplicate item"}'
    transport = RecordingTransport(
        [
            HttpResponse(200, first_body),
            HttpResponse(206, second_body, {"Content-Range": "0-0/1"}),
            HttpResponse(409, error_body),
        ]
    )
    client = _client(transport)

    client.select(
        "items",
        filters={"name": "eq.private-query-value"},
        limit=1,
    )
    client.select_page("inventory_entries", limit=1)
    with pytest.raises(SupabaseError, match="duplicate item"):
        client.insert("items", {"name": "private-payload-value"})

    snapshot = client.usage_snapshot()
    encoded = json.dumps(snapshot)

    assert snapshot["schema"] == "magpie.supabase_client_usage.v1"
    assert snapshot["logical_requests"] == 3
    assert snapshot["response_bytes"] == sum(
        len(body) for body in (first_body, second_body, error_body)
    )
    assert snapshot["errors"] == 1
    assert snapshot["elapsed_seconds"] >= 0
    assert snapshot["requests_per_minute"] >= 0
    assert snapshot["response_bytes_per_minute"] >= 0
    assert {row["operation"] for row in snapshot["operations"]} == {
        "rest/items",
        "rest/inventory_entries",
    }
    assert "secret-key" not in encoded
    assert "private-query-value" not in encoded
    assert "private-payload-value" not in encoded


def test_urlopen_transport_retries_transient_connection_failures(monkeypatch) -> None:
    attempts = 0

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @staticmethod
        def read() -> bytes:
            return b"[]"

    def flaky_urlopen(_request, *, timeout):
        nonlocal attempts
        assert timeout == 4.5
        attempts += 1
        if attempts < 3:
            raise URLError(ConnectionResetError(104, "connection reset by peer"))
        return Response()

    sleeps: list[float] = []
    monkeypatch.setattr("searchable_storage_led.supabase_client.urlopen", flaky_urlopen)
    monkeypatch.setattr("searchable_storage_led.supabase_client.time.sleep", sleeps.append)

    response = _urlopen_transport(Request("https://project.supabase.co/rest/v1/items"), 4.5)

    assert response == HttpResponse(200, b"[]")
    assert attempts == 3
    assert sleeps == [0.5, 1.0]


def test_urlopen_transport_retries_direct_connection_reset(monkeypatch) -> None:
    attempts = 0

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @staticmethod
        def read() -> bytes:
            return b"[]"

    def flaky_urlopen(_request, *, timeout):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionResetError(104, "connection reset by peer")
        return Response()

    sleeps: list[float] = []
    monkeypatch.setattr("searchable_storage_led.supabase_client.urlopen", flaky_urlopen)
    monkeypatch.setattr("searchable_storage_led.supabase_client.time.sleep", sleeps.append)

    response = _urlopen_transport(Request("https://project.supabase.co/rest/v1/items"), 4.5)

    assert response == HttpResponse(200, b"[]")
    assert attempts == 2
    assert sleeps == [0.5]


def test_urlopen_transport_retries_retryable_http_status(monkeypatch) -> None:
    attempts = 0

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @staticmethod
        def read() -> bytes:
            return b"[]"

    def flaky_urlopen(_request, *, timeout):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise HTTPError(
                "https://project.supabase.co/rest/v1/items",
                503,
                "service unavailable",
                {},
                None,
            )
        return Response()

    sleeps: list[float] = []
    monkeypatch.setattr("searchable_storage_led.supabase_client.urlopen", flaky_urlopen)
    monkeypatch.setattr("searchable_storage_led.supabase_client.time.sleep", sleeps.append)

    response = _urlopen_transport(Request("https://project.supabase.co/rest/v1/items"), 4.5)

    assert response == HttpResponse(200, b"[]")
    assert attempts == 2
    assert sleeps == [0.5]


def test_upsert_encodes_json_and_conflict_preference() -> None:
    transport = RecordingTransport([HttpResponse(201, b'[{"local_id":3}]')])
    client = _client(transport)

    rows = client.upsert(
        "items",
        {"workspace_id": "workspace-1", "name": "Blue pry tool"},
        on_conflict="workspace_id,name",
    )

    request, _ = transport.requests[0]
    assert rows == [{"local_id": 3}]
    assert request.get_method() == "POST"
    assert parse_qs(urlparse(request.full_url).query) == {
        "on_conflict": ["workspace_id,name"]
    }
    assert json.loads(request.data) == {
        "workspace_id": "workspace-1",
        "name": "Blue pry tool",
    }
    assert request.headers["Content-type"] == "application/json"
    assert request.headers["Prefer"] == "resolution=merge-duplicates,return=representation"


def test_update_delete_and_rpc_use_expected_methods() -> None:
    transport = RecordingTransport(
        [
            HttpResponse(200, b'[{"local_id":4}]'),
            HttpResponse(200, b'[{"local_id":4}]'),
            HttpResponse(200, b'{"ready":true}'),
        ]
    )
    client = _client(transport)

    client.update(
        "items",
        {"category": "tools"},
        filters={"workspace_id": "eq.workspace-1", "local_id": "eq.4"},
    )
    client.delete(
        "items",
        filters={"workspace_id": "eq.workspace-1", "local_id": "eq.4"},
    )
    result = client.rpc("magpie_readiness", {"target_workspace_id": "workspace-1"})

    assert [entry[0].get_method() for entry in transport.requests] == [
        "PATCH",
        "DELETE",
        "POST",
    ]
    assert result == {"ready": True}
    assert transport.requests[2][0].full_url.endswith("/rest/v1/rpc/magpie_readiness")


def test_http_error_uses_server_message_without_leaking_api_key() -> None:
    transport = RecordingTransport(
        [HttpResponse(409, b'{"message":"duplicate item","code":"23505"}')]
    )
    client = _client(transport)

    with pytest.raises(SupabaseError, match="duplicate item") as exc:
        client.insert("items", {"name": "T-nuts"})

    assert "secret-key" not in str(exc.value)
    assert "409" in str(exc.value)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"url": "", "api_key": "key"}, "URL"),
        ({"url": "https://example.test", "api_key": ""}, "API key"),
        (
            {"url": "https://example.test", "api_key": "key", "timeout_seconds": 0},
            "timeout",
        ),
    ],
)
def test_client_rejects_invalid_configuration(kwargs, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        SupabaseRestClient(**kwargs)


def test_invalid_json_response_is_reported() -> None:
    client = _client(RecordingTransport([HttpResponse(200, b"not-json")]))

    with pytest.raises(SupabaseError, match="invalid JSON"):
        client.select("items")


def test_storage_upload_delete_and_public_url() -> None:
    transport = RecordingTransport(
        [
            HttpResponse(200, b'{"Key":"photos/workspace/a b.jpg"}'),
            HttpResponse(200, b'[{"name":"workspace/a b.jpg"}]'),
        ]
    )
    client = _client(transport)

    uploaded = client.upload_object(
        bucket="photos",
        path="workspace/a b.jpg",
        data=b"jpeg-data",
        content_type="image/jpeg",
    )
    client.delete_objects(bucket="photos", paths=["workspace/a b.jpg"])

    upload, _ = transport.requests[0]
    delete, _ = transport.requests[1]
    assert uploaded == {"Key": "photos/workspace/a b.jpg"}
    assert upload.get_method() == "POST"
    assert upload.full_url.endswith("/storage/v1/object/photos/workspace/a%20b.jpg")
    assert upload.data == b"jpeg-data"
    assert upload.headers["Content-type"] == "image/jpeg"
    assert upload.headers["X-upsert"] == "false"
    assert delete.get_method() == "DELETE"
    assert json.loads(delete.data) == {"prefixes": ["workspace/a b.jpg"]}
    assert client.public_object_url(
        bucket="photos", path="workspace/a b.jpg"
    ).endswith("/storage/v1/object/public/photos/workspace/a%20b.jpg")


def test_storage_download_returns_binary_content() -> None:
    transport = RecordingTransport([HttpResponse(200, b"jpeg-data")])
    client = _client(transport)

    content = client.download_object(bucket="photos", path="workspace/photo.jpg")

    request, _ = transport.requests[0]
    assert content == b"jpeg-data"
    assert request.get_method() == "GET"
    assert request.full_url.endswith("/storage/v1/object/photos/workspace/photo.jpg")


@pytest.mark.parametrize("path", ["", "../secret", "folder/../secret", "folder//x"])
def test_storage_rejects_unsafe_paths(path: str) -> None:
    client = _client(RecordingTransport())

    with pytest.raises(ValueError, match="path"):
        client.public_object_url(bucket="photos", path=path)
