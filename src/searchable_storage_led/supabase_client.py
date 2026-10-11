from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import Lock
from typing import Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

import httpx


class SupabaseError(RuntimeError):
    pass


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes = b""
    headers: Mapping[str, str] = field(default_factory=dict)


Transport = Callable[[Request, float], HttpResponse]


class _PooledHttpTransport:
    def __init__(self) -> None:
        self._client = httpx.Client()

    def __call__(self, request: Request, timeout: float) -> HttpResponse:
        max_attempts = 4
        last_error: BaseException | None = None
        for attempt in range(max_attempts):
            try:
                response = self._client.request(
                    request.get_method(),
                    request.full_url,
                    headers=dict(request.header_items()),
                    content=request.data,
                    timeout=timeout,
                )
                if (
                    response.status_code in {408, 425, 429, 500, 502, 503, 504}
                    and attempt < max_attempts - 1
                ):
                    time.sleep(0.5 * (2**attempt))
                    continue
                return HttpResponse(
                    status=response.status_code,
                    body=response.content,
                    headers=dict(getattr(response, "headers", {}) or {}),
                )
            except httpx.TransportError as exc:
                last_error = exc
                if attempt < max_attempts - 1:
                    time.sleep(0.5 * (2**attempt))
        assert last_error is not None
        raise SupabaseError(
            f"Supabase request failed after {max_attempts} attempts: {last_error}"
        ) from last_error


def _urlopen_transport(request: Request, timeout: float) -> HttpResponse:
    max_attempts = 4
    last_error: BaseException | None = None
    for attempt in range(max_attempts):
        try:
            with urlopen(request, timeout=timeout) as response:
                return HttpResponse(
                    status=response.status,
                    body=response.read(),
                    headers=dict(getattr(response, "headers", {}) or {}),
                )
        except HTTPError as exc:
            if exc.code in {408, 425, 429, 500, 502, 503, 504} and attempt < max_attempts - 1:
                last_error = exc
                time.sleep(0.5 * (2**attempt))
                continue
            return HttpResponse(status=exc.code, body=exc.read())
        except (URLError, ConnectionError, TimeoutError) as exc:
            last_error = exc
            if attempt < max_attempts - 1:
                time.sleep(0.5 * (2**attempt))
    assert last_error is not None
    detail = getattr(last_error, "reason", last_error)
    raise SupabaseError(
        f"Supabase request failed after {max_attempts} attempts: {detail}"
    ) from last_error


class SupabaseRestClient:
    """Small synchronous PostgREST client for Magpie repositories.

    The service role key is suitable for a trusted Pi process. Browser and
    native clients must use user JWTs so the schema's workspace RLS applies.
    """

    def __init__(
        self,
        *,
        url: str,
        api_key: str,
        timeout_seconds: float = 10.0,
        transport: Transport | None = None,
    ) -> None:
        clean_url = url.strip().rstrip("/")
        if not clean_url:
            raise ValueError("Supabase URL is required")
        if not api_key.strip():
            raise ValueError("Supabase API key is required")
        if timeout_seconds <= 0:
            raise ValueError("Supabase timeout must be positive")
        self.rest_url = f"{clean_url}/rest/v1"
        self.storage_url = f"{clean_url}/storage/v1"
        self.api_key = api_key.strip()
        self.timeout_seconds = float(timeout_seconds)
        self.transport = transport or _PooledHttpTransport()
        self._usage_started_at = datetime.now(UTC)
        self._usage_started_monotonic = time.monotonic()
        self._usage_lock = Lock()
        self._usage_requests = 0
        self._usage_response_bytes = 0
        self._usage_errors = 0
        self._usage_by_operation: dict[str, dict[str, int | str]] = {}

    def select(
        self,
        table: str,
        *,
        columns: str = "*",
        filters: Mapping[str, str] | None = None,
        order: str = "",
        limit: int | None = None,
    ) -> list[dict[str, object]]:
        query: dict[str, str] = {"select": columns}
        query.update(filters or {})
        if order:
            query["order"] = order
        if limit is not None:
            query["limit"] = str(max(0, int(limit)))
        result = self._request("GET", table, query=query)
        if not isinstance(result, list):
            raise SupabaseError("Supabase select returned a non-list response")
        return [row for row in result if isinstance(row, dict)]

    def insert(
        self,
        table: str,
        values: Mapping[str, object] | list[Mapping[str, object]],
        *,
        return_rows: bool = True,
    ) -> list[dict[str, object]]:
        result = self._request(
            "POST",
            table,
            payload=values,
            prefer="return=representation" if return_rows else "return=minimal",
        )
        return self._rows(result)

    def select_page(
        self,
        table: str,
        *,
        columns: str = "*",
        filters: Mapping[str, str] | None = None,
        order: str = "",
        limit: int = 30,
        offset: int = 0,
    ) -> tuple[list[dict[str, object]], int]:
        clean_limit = max(1, int(limit))
        clean_offset = max(0, int(offset))
        query: dict[str, str] = {"select": columns}
        query.update(filters or {})
        if order:
            query["order"] = order
        url = f"{self.rest_url}/{table.strip('/')}?{urlencode(query)}"
        headers = {
            **self._auth_headers(),
            "Prefer": "count=exact",
            "Range-Unit": "items",
            "Range": f"{clean_offset}-{clean_offset + clean_limit - 1}",
        }
        response = self._send(
            Request(url, headers=headers, method="GET"),
            operation=f"rest/{table.strip('/')}",
        )
        parsed = self._decode(response.body)
        if not 200 <= response.status < 300:
            raise SupabaseError(
                f"Supabase GET {table.strip('/')} failed ({response.status}): "
                f"{self._error_message(parsed)}"
            )
        if not isinstance(parsed, list):
            raise SupabaseError("Supabase paged select returned a non-list response")
        rows = [row for row in parsed if isinstance(row, dict)]
        content_range = next(
            (
                value
                for key, value in response.headers.items()
                if key.lower() == "content-range"
            ),
            "",
        )
        total_text = content_range.rsplit("/", 1)[-1] if "/" in content_range else ""
        total = int(total_text) if total_text.isdigit() else clean_offset + len(rows)
        return rows, total

    def upsert(
        self,
        table: str,
        values: Mapping[str, object] | list[Mapping[str, object]],
        *,
        on_conflict: str,
        return_rows: bool = True,
    ) -> list[dict[str, object]]:
        prefer = "resolution=merge-duplicates"
        prefer += ",return=representation" if return_rows else ",return=minimal"
        result = self._request(
            "POST",
            table,
            query={"on_conflict": on_conflict},
            payload=values,
            prefer=prefer,
        )
        return self._rows(result)

    def update(
        self,
        table: str,
        values: Mapping[str, object],
        *,
        filters: Mapping[str, str],
        return_rows: bool = True,
    ) -> list[dict[str, object]]:
        result = self._request(
            "PATCH",
            table,
            query=filters,
            payload=values,
            prefer="return=representation" if return_rows else "return=minimal",
        )
        return self._rows(result)

    def delete(
        self,
        table: str,
        *,
        filters: Mapping[str, str],
        return_rows: bool = True,
    ) -> list[dict[str, object]]:
        result = self._request(
            "DELETE",
            table,
            query=filters,
            prefer="return=representation" if return_rows else "return=minimal",
        )
        return self._rows(result)

    def rpc(
        self,
        function: str,
        arguments: Mapping[str, object] | None = None,
    ) -> object:
        return self._request("POST", f"rpc/{function}", payload=arguments or {})

    def upload_object(
        self,
        *,
        bucket: str,
        path: str,
        data: bytes,
        content_type: str,
        upsert: bool = False,
    ) -> dict[str, object]:
        clean_bucket = self._storage_segment(bucket, "bucket")
        clean_path = self._storage_path(path)
        request = Request(
            f"{self.storage_url}/object/{quote(clean_bucket)}/{quote(clean_path, safe='/')}",
            data=data,
            headers={
                **self._auth_headers(),
                "Content-Type": content_type.strip() or "application/octet-stream",
                "x-upsert": "true" if upsert else "false",
            },
            method="POST",
        )
        response = self._send(
            request,
            operation=f"storage/object/{clean_bucket}",
        )
        parsed = self._decode(response.body)
        if not 200 <= response.status < 300:
            raise SupabaseError(
                f"Supabase photo upload failed ({response.status}): "
                f"{self._error_message(parsed)}"
            )
        if not isinstance(parsed, dict):
            raise SupabaseError("Supabase photo upload returned a non-object response")
        return parsed

    def delete_objects(self, *, bucket: str, paths: list[str]) -> None:
        clean_bucket = self._storage_segment(bucket, "bucket")
        clean_paths = [self._storage_path(path) for path in paths]
        if not clean_paths:
            return
        request = Request(
            f"{self.storage_url}/object/{quote(clean_bucket)}",
            data=json.dumps({"prefixes": clean_paths}, separators=(",", ":")).encode(
                "utf-8"
            ),
            headers={**self._auth_headers(), "Content-Type": "application/json"},
            method="DELETE",
        )
        response = self._send(
            request,
            operation=f"storage/object/{clean_bucket}",
        )
        parsed = self._decode(response.body)
        if not 200 <= response.status < 300:
            raise SupabaseError(
                f"Supabase photo delete failed ({response.status}): "
                f"{self._error_message(parsed)}"
            )

    def download_object(self, *, bucket: str, path: str) -> bytes:
        clean_bucket = self._storage_segment(bucket, "bucket")
        clean_path = self._storage_path(path)
        request = Request(
            f"{self.storage_url}/object/{quote(clean_bucket)}/{quote(clean_path, safe='/')}",
            headers=self._auth_headers(),
            method="GET",
        )
        response = self._send(
            request,
            operation=f"storage/object/{clean_bucket}",
        )
        if not 200 <= response.status < 300:
            parsed = self._decode(response.body)
            raise SupabaseError(
                f"Supabase photo download failed ({response.status}): "
                f"{self._error_message(parsed)}"
            )
        return response.body

    def public_object_url(self, *, bucket: str, path: str) -> str:
        clean_bucket = self._storage_segment(bucket, "bucket")
        clean_path = self._storage_path(path)
        return (
            f"{self.storage_url}/object/public/{quote(clean_bucket)}/"
            f"{quote(clean_path, safe='/')}"
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, str] | None = None,
        payload: object | None = None,
        prefer: str = "",
    ) -> object:
        clean_path = path.strip("/")
        if not clean_path:
            raise ValueError("Supabase path is required")
        url = f"{self.rest_url}/{clean_path}"
        if query:
            url = f"{url}?{urlencode(query)}"
        headers = self._auth_headers()
        data = None
        if payload is not None:
            data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if prefer:
            headers["Prefer"] = prefer
        response = self._send(
            Request(url, data=data, headers=headers, method=method),
            operation=f"rest/{clean_path}",
        )
        parsed = self._decode(response.body)
        if not 200 <= response.status < 300:
            message = self._error_message(parsed)
            raise SupabaseError(
                f"Supabase {method} {clean_path} failed ({response.status}): {message}"
            )
        return parsed

    def usage_snapshot(self) -> dict[str, object]:
        """Return secret-safe traffic observed by this client process.

        Counts are logical Magpie requests and final response bodies. Provider
        retries and billing-side protocol overhead are intentionally not
        estimated here.
        """

        with self._usage_lock:
            operations = [dict(row) for row in self._usage_by_operation.values()]
            requests = self._usage_requests
            response_bytes = self._usage_response_bytes
            errors = self._usage_errors
        elapsed_seconds = max(0.0, time.monotonic() - self._usage_started_monotonic)
        elapsed_minutes = elapsed_seconds / 60.0
        operations.sort(
            key=lambda row: (
                -int(row["response_bytes"]),
                -int(row["requests"]),
                str(row["operation"]),
            )
        )
        return {
            "schema": "magpie.supabase_client_usage.v1",
            "started_at": self._usage_started_at.isoformat().replace("+00:00", "Z"),
            "logical_requests": requests,
            "response_bytes": response_bytes,
            "errors": errors,
            "elapsed_seconds": round(elapsed_seconds, 3),
            "requests_per_minute": (
                round(requests / elapsed_minutes, 3) if elapsed_minutes else 0.0
            ),
            "response_bytes_per_minute": (
                round(response_bytes / elapsed_minutes, 3)
                if elapsed_minutes
                else 0.0
            ),
            "operations": operations,
        }

    def _send(self, request: Request, *, operation: str) -> HttpResponse:
        method = request.get_method().upper()
        try:
            response = self.transport(request, self.timeout_seconds)
        except Exception:
            self._record_usage(
                method=method,
                operation=operation,
                response_bytes=0,
                error=True,
            )
            raise
        self._record_usage(
            method=method,
            operation=operation,
            response_bytes=len(response.body),
            error=not 200 <= response.status < 300,
        )
        return response

    def _record_usage(
        self,
        *,
        method: str,
        operation: str,
        response_bytes: int,
        error: bool,
    ) -> None:
        key = f"{method} {operation}"
        with self._usage_lock:
            self._usage_requests += 1
            self._usage_response_bytes += max(0, int(response_bytes))
            self._usage_errors += int(error)
            row = self._usage_by_operation.setdefault(
                key,
                {
                    "method": method,
                    "operation": operation,
                    "requests": 0,
                    "response_bytes": 0,
                    "errors": 0,
                },
            )
            row["requests"] = int(row["requests"]) + 1
            row["response_bytes"] = int(row["response_bytes"]) + max(
                0, int(response_bytes)
            )
            row["errors"] = int(row["errors"]) + int(error)

    @staticmethod
    def _decode(body: bytes) -> object:
        if not body:
            return []
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SupabaseError("Supabase returned invalid JSON") from exc

    @staticmethod
    def _rows(value: object) -> list[dict[str, object]]:
        if not isinstance(value, list):
            raise SupabaseError("Supabase mutation returned a non-list response")
        return [row for row in value if isinstance(row, dict)]

    @staticmethod
    def _error_message(value: object) -> str:
        if isinstance(value, dict):
            for key in ("message", "details", "hint", "code"):
                candidate = value.get(key)
                if candidate:
                    return str(candidate)
        return "request rejected"

    def _auth_headers(self) -> dict[str, str]:
        return {
            "apikey": self.api_key,
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "User-Agent": "Magpie/0.1",
        }

    @staticmethod
    def _storage_segment(value: str, label: str) -> str:
        clean = value.strip().strip("/")
        if not clean or "/" in clean or clean in {".", ".."}:
            raise ValueError(f"Supabase storage {label} is invalid")
        return clean

    @staticmethod
    def _storage_path(path: str) -> str:
        clean = path.strip().strip("/")
        parts = clean.split("/")
        if not clean or any(part in {"", ".", ".."} for part in parts):
            raise ValueError("Supabase storage path is invalid")
        return clean
