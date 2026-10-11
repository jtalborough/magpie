"""Find a real photo for an item by searching the web (no API key).

Primary source is DuckDuckGo image search (good product relevance); Openverse
(open-licensed) is a fallback if DuckDuckGo ever blocks. Given an item name, it
downloads the first usable result.
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
OPENVERSE = "https://api.openverse.org/v1/images/"


def search_item_image(
    query: str, *, max_bytes: int = 5_000_000, timeout: float = 12.0
) -> tuple[bytes, str, str]:
    """Return (image_bytes, content_type, source_url) for the first good result.

    Tries the exact name first (specific queries give better photos), then a
    de-specified fallback, via DuckDuckGo; finally Openverse. Raises on failure."""
    queries = [query.strip()]
    plain = " ".join(w for w in query.split() if not any(c.isdigit() for c in w)).strip()
    if plain and plain.lower() != query.strip().lower():
        queries.append(plain)

    for q in queries:
        try:
            results = _ddg_image_results(q, timeout=timeout)
        except Exception:
            results = []
        for result in results[:10]:
            image_url = result.get("image")
            if not image_url:
                continue
            try:
                blob, content_type = _download(image_url, max_bytes=max_bytes, timeout=timeout)
            except Exception:
                continue
            return blob, content_type, str(result.get("url") or image_url)

    # Fallback: Openverse (open-licensed; weaker relevance but reliable).
    for q in queries:
        try:
            return _openverse(q, max_bytes=max_bytes, timeout=timeout)
        except Exception:
            continue
    raise RuntimeError(f"no image found for {query!r}")


def _ddg_image_results(query: str, *, timeout: float) -> list[dict]:
    page = _fetch(
        "https://duckduckgo.com/?" + urllib.parse.urlencode(
            {"q": query, "iax": "images", "ia": "images"}
        ),
        timeout=timeout,
    ).decode("utf-8", "ignore")
    token = None
    for pattern in (r'vqd=["\']?([\d-]+)', r'vqd=([^&"\']+)'):
        match = re.search(pattern, page)
        if match:
            token = match.group(1)
            break
    if not token:
        return []
    data = json.loads(
        _fetch(
            "https://duckduckgo.com/i.js?" + urllib.parse.urlencode(
                {"l": "us-en", "o": "json", "q": query, "vqd": token, "f": ",,,", "p": "1"}
            ),
            timeout=timeout,
            referer="https://duckduckgo.com/",
        )
    )
    results = data.get("results")
    return results if isinstance(results, list) else []


def _openverse(query: str, *, max_bytes: int, timeout: float) -> tuple[bytes, str, str]:
    data = json.loads(
        _fetch(
            OPENVERSE + "?" + urllib.parse.urlencode({"q": query, "page_size": 6}),
            timeout=timeout,
        )
    )
    for result in data.get("results", []):
        image_url = result.get("url")
        if not image_url:
            continue
        try:
            blob, content_type = _download(image_url, max_bytes=max_bytes, timeout=timeout)
        except Exception:
            continue
        return blob, content_type, str(result.get("foreign_landing_url") or image_url)
    raise RuntimeError("no openverse result")


def _fetch(url: str, *, timeout: float, referer: str | None = None) -> bytes:
    headers = {"User-Agent": BROWSER_UA}
    if referer:
        headers["Referer"] = referer
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _download(image_url: str, *, max_bytes: int, timeout: float) -> tuple[bytes, str]:
    request = urllib.request.Request(image_url, headers={"User-Agent": BROWSER_UA})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        content_type = (response.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip()
        if not content_type.startswith("image/"):
            raise ValueError("not an image")
        blob = response.read(max_bytes + 1)
    if len(blob) > max_bytes or len(blob) < 500:
        raise ValueError("image too large or too small")
    return blob, content_type
