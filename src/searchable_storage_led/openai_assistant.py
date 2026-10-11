from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any, Callable

from .assistant import (
    FIND_LIMIT,
    bin_fits,
    find_response,
    first_empty_bin,
    infer_category,
    infer_required_bin_size,
    normalized_query,
    put_away_decision,
    title_item_name,
)
from .layout import StorageLayout
from .llm_client import LlmClient, LlmError
from .store import InventoryStore, PhotoRecord


ASSISTANT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["action", "message", "query", "item_draft"],
    "properties": {
        "action": {
            "type": "string",
            "enum": ["find_item", "store_item", "unknown"],
        },
        "message": {"type": "string"},
        "query": {"type": "string"},
        "item_draft": {
            "anyOf": [
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "name",
                        "category",
                        "required_bin_size",
                        "aliases",
                        "description",
                        "quantity",
                        "unit",
                    ],
                    "properties": {
                        "name": {"type": "string"},
                        "category": {"type": "string"},
                        "required_bin_size": {
                            "type": "string",
                            "enum": ["small", "medium", "large"],
                        },
                        "aliases": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "description": {"type": "string"},
                        "quantity": {"type": "number"},
                        "unit": {"type": "string"},
                    },
                },
                {"type": "null"},
            ]
        },
    },
}


# Back-compat alias: the assistant error type is now provider-agnostic.
OpenAIAssistantError = LlmError


ASSISTANT_INSTRUCTIONS = (
    "You are a storage assistant. Decide whether the user wants to find "
    "an existing item or put something away. Identify photographed parts "
    "when possible. Estimate the smallest physical bin size that can fit "
    "the actual full item group shown or described. Do not infer size from "
    "item type alone: a few pieces can be small, a retail pack may be "
    "medium, and a large roll or box may be large. Use the provided cabinet "
    "size profiles and candidate empty bins. When uncertain from the image, "
    "choose the smaller size only if the whole group clearly fits it. Return "
    "only the structured decision as a JSON object with keys action "
    "('find_item' | 'store_item' | 'unknown'), message, query, and item_draft "
    "(an object with the item fields, or null). "
    "Never claim an inventory write happened."
)


ENRICH_INSTRUCTIONS = (
    "You are tidying one inventory entry. From the user's rough description "
    "(and any photo), return a clean entry. Fix spelling/typos in the name and "
    "use proper casing. Give a short lowercase category (e.g. fasteners, tools, "
    "electronics, adhesives). Provide 2-4 helpful lowercase search aliases people "
    "might use. Give the unit (default 'each'). Give quantity if the user stated "
    "one, else 1. Return ONLY JSON: {\"name\":..., \"category\":..., "
    "\"aliases\":[...], \"unit\":..., \"quantity\":...}."
)


def enrich_item_draft(
    text: str,
    *,
    photos: list[PhotoRecord],
    client: LlmClient,
    photo_loader: Callable[[int], bytes] | None = None,
) -> dict[str, object]:
    """Ask the model to clean one item description into a tidy filed entry:
    corrected name, category, aliases, unit, quantity."""
    images = [
        data_url
        for photo in photos[:2]
        if (data_url := photo_data_url(photo, photo_loader)) is not None
    ]
    raw = client.complete_json(
        system=ENRICH_INSTRUCTIONS, user_text=text, images=images
    )
    fallback = title_item_name(normalized_query(text)) or text.strip()
    aliases = raw.get("aliases")
    return {
        "name": str(raw.get("name") or fallback).strip() or fallback,
        "category": str(raw.get("category") or "").strip().lower(),
        "aliases": [str(a).strip() for a in aliases if str(a).strip()]
        if isinstance(aliases, list)
        else [],
        "unit": str(raw.get("unit") or "each").strip() or "each",
        "quantity": numeric_or_default(raw.get("quantity"), 1),
    }


def assistant_payload(
    *,
    command: str,
    inventory: list[dict[str, object]],
    empty_bins: list[dict[str, str]],
    size_profiles: dict[str, dict[str, object]],
) -> dict[str, object]:
    return {
        "user_command": command,
        "known_inventory": inventory[:50],
        "candidate_empty_bins": empty_bins,
        "bin_size_profiles": prompt_size_profiles(size_profiles, empty_bins),
    }


def llm_assistant_decision(
    text: str,
    *,
    layout: StorageLayout,
    store: InventoryStore,
    photos: list[PhotoRecord],
    client: LlmClient,
    photo_loader: Callable[[int], bytes] | None = None,
) -> dict[str, object]:
    payload = assistant_payload(
        command=text,
        inventory=[item.as_dict() for item in store.list_inventory()],
        empty_bins=empty_bin_candidates(layout, store, limit=20),
        size_profiles=layout.size_profiles,
    )
    images = [
        data_url
        for photo in photos[:3]
        if (data_url := photo_data_url(photo, photo_loader)) is not None
    ]
    suggestion = client.complete_json(
        system=ASSISTANT_INSTRUCTIONS,
        user_text=json.dumps(payload),
        images=images,
    )
    return normalize_openai_decision(
        suggestion,
        text=text,
        layout=layout,
        store=store,
    )


def normalize_openai_decision(
    suggestion: dict[str, Any],
    *,
    text: str,
    layout: StorageLayout,
    store: InventoryStore,
) -> dict[str, object]:
    query = str(suggestion.get("query") or normalized_query(text)).strip()
    if not query:
        query = normalized_query(text)
    action = suggestion.get("action")
    if action == "find_item":
        return find_response(query, store.search(query, limit=FIND_LIMIT))
    if action == "store_item":
        decision = put_away_decision(query, layout=layout, store=store)
        draft = dict(decision.get("item_draft") or {})
        llm_draft = suggestion.get("item_draft")
        if isinstance(llm_draft, dict):
            draft.update(
                {
                    "name": str(llm_draft.get("name") or draft.get("name") or title_item_name(query)),
                    "category": str(
                        llm_draft.get("category")
                        or draft.get("category")
                        or infer_category(query)
                    ),
                    "required_bin_size": valid_bin_size(
                        llm_draft.get("required_bin_size")
                    )
                    or str(draft.get("required_bin_size") or infer_required_bin_size(query)),
                    "aliases": clean_aliases(llm_draft.get("aliases"), query),
                    "description": str(llm_draft.get("description") or ""),
                    "quantity": numeric_or_default(llm_draft.get("quantity"), 1),
                    "unit": str(llm_draft.get("unit") or "each"),
                }
            )
        required_bin_size = str(draft.get("required_bin_size") or "small")
        suggested_bin = valid_suggested_bin(
            str(draft.get("bin_id") or ""),
            required_bin_size=required_bin_size,
            layout=layout,
        ) or first_empty_bin(layout, store, min_size=required_bin_size)
        draft["bin_id"] = suggested_bin
        decision["suggested_bin"] = suggested_bin
        decision["item_draft"] = draft
        if suggested_bin:
            decision["message"] = (
                f"I would put {draft['name']} in {required_bin_size} bin "
                f"{suggested_bin}."
            )
        return decision
    return {
        "action": "unknown",
        "message": str(suggestion.get("message") or "I need more detail."),
        "query": query,
        "results": [],
        "suggested_bin": None,
        "item_draft": None,
    }


def parse_response_json(response: dict[str, Any]) -> dict[str, Any]:
    text = response.get("output_text")
    if not text:
        text = output_text_from_items(response.get("output", []))
    if not text:
        raise OpenAIAssistantError("OpenAI response did not include output text")
    try:
        parsed = json.loads(str(text))
    except json.JSONDecodeError as exc:
        raise OpenAIAssistantError("OpenAI response was not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise OpenAIAssistantError("OpenAI response JSON must be an object")
    return parsed


def output_text_from_items(items: object) -> str:
    if not isinstance(items, list):
        return ""
    chunks: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        for content in item.get("content", []):
            if not isinstance(content, dict):
                continue
            if content.get("type") in {"output_text", "text"}:
                chunks.append(str(content.get("text", "")))
    return "".join(chunks)


def empty_bin_candidates(
    layout: StorageLayout,
    store: InventoryStore,
    *,
    limit: int,
) -> list[dict[str, str]]:
    occupied = store.occupied_bin_ids()
    return [
        {"id": bin.id, "size": bin.size}
        for bin in sorted(layout.bins, key=lambda bin: bin.number)
        if bin.id not in occupied
    ][:limit]


def prompt_size_profiles(
    size_profiles: dict[str, dict[str, object]],
    empty_bins: list[dict[str, str]],
) -> dict[str, dict[str, object]]:
    profiles: dict[str, dict[str, object]] = {}
    empty_counts = {"small": 0, "medium": 0, "large": 0}
    for bin in empty_bins:
        size = str(bin.get("size", ""))
        if size in empty_counts:
            empty_counts[size] += 1
    for size in ("small", "medium", "large"):
        profile = dict(size_profiles.get(size, {}))
        min_span = profile.get("min_span_units")
        max_span = profile.get("max_span_units")
        if min_span == max_span and min_span is not None:
            unit = "unit" if int(min_span) == 1 else "units"
            relative_size = f"{min_span} cabinet width {unit}"
        elif min_span is not None and max_span is not None:
            relative_size = f"{min_span}-{max_span} cabinet width units"
        else:
            relative_size = fallback_relative_size(size)
        profile["relative_size"] = relative_size
        profile["fit_rule"] = fit_rule(size)
        profile["candidate_empty_count_in_prompt"] = empty_counts[size]
        profiles[size] = profile
    return profiles


def fallback_relative_size(size: str) -> str:
    if size == "medium":
        return "3-4 cabinet width units"
    if size == "large":
        return "7 cabinet width units"
    return "1 cabinet width unit"


def fit_rule(size: str) -> str:
    if size == "medium":
        return (
            "Use when the actual object group is a package, kit, bundle, or "
            "small box that likely exceeds one small drawer."
        )
    if size == "large":
        return (
            "Use for bulky boxes, rolls, spools, long items, large bags, or "
            "anything that would overflow a medium drawer."
        )
    return (
        "Use only when the whole object group is loose tiny parts or one "
        "compact item that clearly fits a single small drawer."
    )


def valid_suggested_bin(
    bin_id: str,
    *,
    required_bin_size: str,
    layout: StorageLayout,
) -> str | None:
    if not bin_id:
        return None
    bin = layout.get(bin_id)
    if bin is None or not bin_fits(bin.size, required_bin_size):
        return None
    return bin.id


def photo_data_url(
    photo: PhotoRecord,
    loader: Callable[[int], bytes] | None = None,
) -> str | None:
    if loader is not None:
        try:
            content = loader(photo.id)
        except Exception as exc:
            raise LlmError(f"photo {photo.id} could not be loaded: {exc}") from exc
        if not content:
            raise LlmError(f"photo {photo.id} is empty")
    else:
        path = Path(photo.path)
        if not path.exists():
            return None
        content = path.read_bytes()
    data = base64.b64encode(content).decode("ascii")
    return f"data:{photo.content_type};base64,{data}"


def clean_aliases(value: object, query: str) -> list[str]:
    aliases = [query]
    if isinstance(value, list):
        aliases.extend(str(alias) for alias in value)
    return sorted({alias.strip() for alias in aliases if alias.strip()})


def numeric_or_default(value: object, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def valid_bin_size(value: object) -> str | None:
    if value in {"small", "medium", "large"}:
        return str(value)
    return None
