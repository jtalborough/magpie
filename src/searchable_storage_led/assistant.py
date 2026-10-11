from __future__ import annotations

from itertools import product
import re

from .layout import Bin, StorageLayout
from .store import InventoryStore, SearchResult


FIND_WORDS = {
    "find",
    "need",
    "where",
    "locate",
    "looking",
    "search",
}
STORE_WORDS = {
    "add",
    "away",
    "put",
    "store",
    "stow",
}
FILLER_WORDS = {
    "a",
    "add",
    "all",
    "an",
    "away",
    "every",
    "find",
    "for",
    "get",
    "i",
    "is",
    "it",
    "locate",
    "looking",
    "me",
    "my",
    "need",
    "please",
    "put",
    "search",
    "show",
    "some",
    "store",
    "stow",
    "the",
    "these",
    "this",
    "to",
    "where",
}

# How many matches a find returns / how the UI lights them.
FIND_LIMIT = 20
FIND_CONTAINER_WORDS = (
    "bag|bin|box|bundle|case|container|drawer|pack|pile|pouch|set|tray"
)
CATEGORY_KEYWORDS = {
    "fasteners": {
        "bolt",
        "bolts",
        "nut",
        "nuts",
        "screw",
        "screws",
        "washer",
        "washers",
    },
    "tools": {
        "allen",
        "bit",
        "bits",
        "driver",
        "hex",
        "key",
        "tool",
        "wrench",
    },
    "electronics": {
        "adapter",
        "cable",
        "connector",
        "heat",
        "jumper",
        "led",
        "resistor",
        "sensor",
        "shrink",
        "tubing",
        "wire",
    },
}
SIZE_RANK = {"small": 0, "medium": 1, "large": 2}
SIZE_KEYWORDS = {
    "large": {
        "box",
        "boxes",
        "bulk",
        "case",
        "cases",
        "large",
        "long",
        "roll",
        "rolls",
        "spool",
        "spools",
    },
    "medium": {
        "assortment",
        "bundle",
        "pack",
        "packs",
    },
}


def assistant_decision(
    text: str,
    *,
    layout: StorageLayout,
    store: InventoryStore,
) -> dict[str, object]:
    command = text.strip()
    query = normalized_query(command)
    if not query:
        return {
            "action": "unknown",
            "message": "Tell me what you need or what you want to put away.",
            "query": "",
            "results": [],
            "suggested_bin": None,
            "item_draft": None,
        }

    words = set(command.lower().replace("?", " ").split())
    if words & STORE_WORDS:
        return put_away_decision(query, layout=layout, store=store)
    if words & FIND_WORDS:
        return find_decision(query, store=store)
    results = store.search(query, limit=FIND_LIMIT)
    if results:
        return find_response(query, results)
    return put_away_decision(query, layout=layout, store=store)


def find_decision(query: str, *, store: InventoryStore) -> dict[str, object]:
    return find_response(query, search_inventory(store, query, limit=FIND_LIMIT))


def relaxed_find_queries(query: str) -> list[str]:
    """Build conservative query variants only after an exact search misses."""
    cleaned = normalized_query(query)
    without_container = re.sub(
        rf"^(?:{FIND_CONTAINER_WORDS})\s+(?:of|with|containing)\s+",
        "",
        cleaned,
        flags=re.I,
    ).strip()
    variants: list[str] = [without_container]
    for base in dict.fromkeys((cleaned, without_container)):
        if not base:
            continue
        choices = [_singular_token_variants(token) for token in base.split()]
        for words in product(*choices):
            variant = " ".join(words)
            if variant != base:
                variants.append(variant)
    return [
        variant
        for variant in dict.fromkeys(variants)
        if variant and variant != query
    ]


def _singular_token_variants(token: str) -> tuple[str, ...]:
    """Return bounded English singular guesses without replacing exact search."""
    variants = [token]
    if len(token) > 4 and token.endswith("ies"):
        variants.append(f"{token[:-3]}y")
    elif len(token) > 4 and token.endswith(("ches", "shes", "xes", "zes", "oes")):
        variants.append(token[:-2])
    elif len(token) > 3 and token.endswith("s") and not token.endswith(("ss", "us", "is")):
        variants.append(token[:-1])
    return tuple(dict.fromkeys(variants))


def search_inventory(
    store: InventoryStore,
    query: str,
    *,
    limit: int = FIND_LIMIT,
) -> list[SearchResult]:
    results = store.search(query, limit=limit)
    if results:
        return results
    for relaxed in relaxed_find_queries(query):
        results = store.search(relaxed, limit=limit)
        if results:
            return results
    return []


def find_response(query: str, results: list[SearchResult]) -> dict[str, object]:
    if not results:
        return {
            "action": "find_item",
            "message": f"I could not find anything matching \"{query}\".",
            "query": query,
            "results": [],
            "suggested_bin": None,
            "item_draft": None,
        }
    first = results[0]
    return {
        "action": "find_item",
        "message": f"Best match is {first.name} in {first.bin_id}.",
        "query": query,
        "results": [result.as_dict() for result in results],
        "suggested_bin": first.bin_id,
        "item_draft": None,
    }


def put_away_decision(
    query: str,
    *,
    layout: StorageLayout,
    store: InventoryStore,
) -> dict[str, object]:
    existing = store.search(query, limit=5)
    required_bin_size = infer_required_bin_size(query)
    suggested_bin = (
        existing[0].bin_id
        if existing
        else first_empty_bin(layout, store, min_size=required_bin_size)
    )
    draft = {
        "name": title_item_name(query),
        "category": infer_category(query),
        "required_bin_size": required_bin_size,
        "aliases": alias_candidates(query),
        "description": "",
        "quantity": 1,
        "unit": "each",
        "bin_id": suggested_bin,
    }
    if existing:
        message = f"This looks related to {existing[0].name}; use {suggested_bin} unless it is a different item."
    elif suggested_bin:
        message = (
            f"I would put {draft['name']} in empty {required_bin_size} "
            f"bin {suggested_bin}."
        )
    else:
        message = "I could not find an empty bin."
    return {
        "action": "store_item",
        "message": message,
        "query": query,
        "results": [result.as_dict() for result in existing],
        "suggested_bin": suggested_bin,
        "item_draft": draft,
    }


def first_empty_bin(
    layout: StorageLayout,
    store: InventoryStore,
    *,
    min_size: str = "small",
) -> str | None:
    occupied = store.occupied_bin_ids()
    for bin in sorted(layout.bins, key=bin_sort_key):
        if bin.id not in occupied and bin_fits(bin.size, min_size):
            return bin.id
    return None


def bin_sort_key(bin: Bin) -> tuple[int, int]:
    return (SIZE_RANK.get(bin.size, 9), bin.number)


def bin_fits(candidate_size: str, required_size: str) -> bool:
    return SIZE_RANK.get(candidate_size, 9) >= SIZE_RANK.get(required_size, 0)


def normalized_query(text: str) -> str:
    words = [
        word.strip(".,!?;:").lower()
        for word in text.split()
        if word.strip(".,!?;:")
    ]
    useful = [word for word in words if word not in FILLER_WORDS]
    return " ".join(useful or words)


def title_item_name(query: str) -> str:
    return " ".join(word.capitalize() for word in query.split())


def alias_candidates(query: str) -> list[str]:
    words = query.split()
    aliases = {query}
    if len(words) > 1:
        aliases.add(" ".join(reversed(words)))
    return sorted(aliases)


def infer_category(query: str) -> str:
    words = set(query.lower().split())
    for category, keywords in CATEGORY_KEYWORDS.items():
        if words & keywords:
            return category
    return ""


def infer_required_bin_size(query: str) -> str:
    words = set(query.lower().split())
    for size in ("large", "medium"):
        if words & SIZE_KEYWORDS[size]:
            return size
    return "small"
