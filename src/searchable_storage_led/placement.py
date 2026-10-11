from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .item_identity import identities_conflict, parse_item_identity
from .locations import Location, LocationStore
from .store import InventoryStore, SearchResult


CAPACITY_RANK = {
    "tiny": 0,
    "small": 1,
    "medium": 2,
    "large": 3,
    "oversize": 4,
}

ITEM_SIZE_HINTS = {
    "tiny": {
        "bead",
        "bolt",
        "chip",
        "component",
        "diode",
        "fuse",
        "ic",
        "led",
        "nut",
        "resistor",
        "screw",
        "washer",
    },
    "small": {
        "adapter",
        "bit",
        "blade",
        "cable",
        "connector",
        "extractor",
        "flux",
        "nozzle",
        "probe",
        "solder",
        "wick",
        "wire",
    },
    "medium": {
        "bag",
        "canvas",
        "caliper",
        "driver",
        "gear",
        "handle",
        "iron",
        "kit",
        "meter",
        "pliers",
        "spool",
        "tool",
        "tweezer",
    },
    "large": {
        "drill",
        "extruder",
        "filament",
        "printer",
        "saw",
        "station",
    },
}

STOP_WORDS = {
    "a",
    "an",
    "and",
    "bin",
    "bins",
    "box",
    "boxes",
    "cabinet",
    "for",
    "in",
    "into",
    "my",
    "of",
    "put",
    "the",
    "these",
    "this",
    "to",
    "drawer",
    "drawers",
}

GENERIC_LOCATION_WORDS = {
    "cell",
    "container",
    "empty",
    "general",
    "large",
    "medium",
    "misc",
    "miscellaneous",
    "parts",
    "pouch",
    "shelf",
    "small",
    "spare",
    "storage",
    "tiny",
}

ITEM_FAMILY_LOCATION_TERMS = {
    "t_nut": {"fastener", "fasteners", "hardware"},
    "screw": {"fastener", "fasteners", "hardware"},
    "bolt": {"fastener", "fasteners", "hardware"},
    "washer": {"fastener", "fasteners", "hardware"},
    "nut": {"fastener", "fasteners", "hardware"},
    "bearing": {"hardware", "mechanical"},
    "connector": {"electronics", "electrical"},
}


@dataclass(frozen=True)
class PlacementSuggestion:
    location: Location
    score: int
    reasons: list[str] = field(default_factory=list)
    contents: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "location_id": self.location.id,
            "label": self.location.label,
            "type": self.location.type,
            "parent_id": self.location.parent_id,
            "capacity_class": self.location.capacity_class,
            "accepts_items": self.location.accepts_items,
            "purpose_tags": list(self.location.purpose_tags),
            "score": self.score,
            "reasons": list(self.reasons),
            "contents": list(self.contents),
        }


@dataclass(frozen=True)
class FamilyHomeEvidence:
    location_id: str
    matching_items: int
    considered_items: int


def family_home_for(
    item_name: str,
    inventory: list[SearchResult],
) -> FamilyHomeEvidence | None:
    identity = parse_item_identity(item_name)
    if not identity.family:
        return None
    requested_attributes = {
        key: value
        for key, value in identity.attributes
        if key in {"head", "drive"}
    }
    homes_by_item: dict[int, str] = {}
    for result in inventory:
        home = str(result.home_location_id or "").strip()
        if not home or result.item_id in homes_by_item:
            continue
        candidate = parse_item_identity(result.name)
        if candidate.family != identity.family:
            continue
        candidate_attributes = candidate.attribute_map
        if any(
            candidate_attributes.get(key) != value
            for key, value in requested_attributes.items()
        ):
            continue
        homes_by_item[result.item_id] = home
    if not homes_by_item:
        return None
    counts: dict[str, int] = {}
    for home in homes_by_item.values():
        counts[home] = counts.get(home, 0) + 1
    ranked = sorted(counts.items(), key=lambda row: (-row[1], row[0]))
    location_id, matching_items = ranked[0]
    if len(ranked) > 1 and ranked[1][1] == matching_items:
        return None
    minimum = (
        2
        if requested_attributes
        or identity.family in {"t_nut", "nut", "washer", "bearing"}
        else 3
    )
    considered_items = len(homes_by_item)
    if matching_items < minimum or matching_items / considered_items < 0.75:
        return None
    return FamilyHomeEvidence(
        location_id=location_id,
        matching_items=matching_items,
        considered_items=considered_items,
    )


def tokens(text: str) -> set[str]:
    return {
        t
        for t in re.findall(r"[a-z0-9]+", text.lower())
        if len(t) > 1 and t not in STOP_WORDS
    }


def infer_item_capacity(item_name: str) -> str:
    seen = tokens(item_name)
    best: str | None = None
    best_rank = -1
    for capacity, hints in ITEM_SIZE_HINTS.items():
        if seen & hints and CAPACITY_RANK[capacity] > best_rank:
            best = capacity
            best_rank = CAPACITY_RANK[capacity]
    return best or "small"


def requested_capacity(text: str) -> str | None:
    match = re.search(
        r"\b(tiny|small|medium|med|large|big|oversize|oversized)\s+"
        r"(?:bin|box|drawer|container)\b",
        text,
        re.I,
    )
    if not match:
        return None
    size = match.group(1).lower()
    return {"med": "medium", "big": "large", "oversized": "oversize"}.get(size, size)


def item_capacity_for(
    item_name: str,
    *,
    store: InventoryStore,
    context_text: str = "",
) -> tuple[str, str]:
    if explicit_size := requested_capacity(context_text):
        return explicit_size, f"requested {explicit_size} container"
    if learned_size := store.item_size_class(item_name):
        return learned_size, "learned item size"
    rule_text = " ".join(part for part in [item_name, context_text] if part).strip()
    if matching_rules := store.matching_size_rules(rule_text or item_name, limit=1):
        rule = matching_rules[0]
        return rule.size_class, "learned size rule: " + rule.rule_text
    return infer_item_capacity(rule_text or item_name), "inferred from item words"


def measured_capacity_for(
    item_dimensions: dict[str, object] | None,
    *,
    locations: LocationStore,
) -> tuple[str, str] | None:
    """Return the smallest measured container class that fits the item."""
    fitting: list[tuple[int, Location]] = []
    measured: list[Location] = []
    for location in locations.all():
        if not location.active or not location.internal_dimensions:
            continue
        fit, _ = physical_fit(item_dimensions, location.internal_dimensions)
        if fit is None:
            continue
        measured.append(location)
        if fit:
            fitting.append(
                (CAPACITY_RANK.get(location.capacity_class, 99), location)
            )
    if fitting:
        _, location = min(fitting, key=lambda entry: (entry[0], entry[1].id))
        return (
            location.capacity_class,
            f"measured item fits {location.label}",
        )
    if measured and max(
        CAPACITY_RANK.get(location.capacity_class, -1) for location in measured
    ) >= CAPACITY_RANK["large"]:
        return "oversize", "measured item exceeds configured large storage"
    return None


def physical_fit(
    item_dimensions: dict[str, object] | None,
    container_dimensions: dict[str, object] | None,
) -> tuple[bool | None, str]:
    item = _item_extents(item_dimensions)
    container = _container_extents(container_dimensions)
    if len(item) < 2 or len(container) < 2:
        return None, ""
    if len(item) >= 3 and len(container) >= 3:
        fits = all(part <= space for part, space in zip(sorted(item[:3]), sorted(container[:3])))
    else:
        fits = all(part <= space for part, space in zip(sorted(item[:2]), sorted(container[:2])))
    item_text = " x ".join(f"{value:g}" for value in item) + " mm"
    container_text = " x ".join(f"{value:g}" for value in container) + " mm"
    return fits, f"measured item {item_text} {'fits' if fits else 'does not fit'} {container_text} internal space"


def _item_extents(dimensions: dict[str, object] | None) -> list[float]:
    data = dimensions or {}
    values = [data.get("length_mm"), data.get("width_mm"), data.get("height_mm")]
    if values[1] is None and data.get("thread_diameter_mm") is not None:
        values[1] = data.get("thread_diameter_mm")
    return _positive_extents(values)


def _container_extents(dimensions: dict[str, object] | None) -> list[float]:
    data = dimensions or {}
    return _positive_extents(
        [data.get("width_mm"), data.get("depth_mm"), data.get("height_mm")]
    )


def _positive_extents(values: list[object]) -> list[float]:
    extents: list[float] = []
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number > 0:
            extents.append(number)
    return extents


def load_placement_guidance(path: str | Path = "config/placement-guidance.md") -> str:
    candidate = Path(path)
    if not candidate.exists():
        return ""
    return candidate.read_text(encoding="utf-8").strip()


def append_placement_memory(
    *,
    command: str,
    applied: list[dict],
    path: str | Path,
) -> None:
    clean = command.strip()
    if not clean:
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_text("# Locus Placement Memory\n\n", encoding="utf-8")
    lines = [f"## {clean}", ""]
    for entry in applied:
        if "stored" in entry:
            lines.append(f"- stored `{entry['stored']}` in `{entry['location_id']}`")
        elif "created_location" in entry:
            lines.append(f"- created location `{entry['created_location']}`")
        elif "moved" in entry:
            lines.append(f"- moved `{entry['moved']}` to `{entry.get('to')}`")
        else:
            lines.append(f"- {entry}")
    lines.append("")
    with target.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


def suggest_locations(
    item_name: str,
    *,
    store: InventoryStore,
    locations: LocationStore,
    limit: int = 8,
    item_dimensions: dict[str, object] | None = None,
) -> list[PlacementSuggestion]:
    item_tokens = tokens(item_name)
    item_family = parse_item_identity(item_name).family
    location_match_tokens = item_tokens | ITEM_FAMILY_LOCATION_TERMS.get(
        item_family,
        set(),
    )
    measured_capacity = measured_capacity_for(item_dimensions, locations=locations)
    if measured_capacity is not None:
        needed_capacity, size_reason = measured_capacity
    else:
        needed_capacity, size_reason = item_capacity_for(item_name, store=store)
    needed_rank = CAPACITY_RANK[needed_capacity]
    learned_home = store.suggest_home(item_name)
    matching_rules = store.matching_rules(item_name)
    rules_by_location: dict[str, list[str]] = {}
    for rule in matching_rules:
        rules_by_location.setdefault(rule.target_location_id, []).append(rule.rule_text)
    wants_main_cabinet = bool(re.search(r"\bmain\s+cabinet\b", item_name, re.I))
    all_inventory = store.list_inventory()
    family_home = family_home_for(item_name, all_inventory)
    contents_by_location: dict[str, list[SearchResult]] = {}
    for result in all_inventory:
        contents_by_location.setdefault(result.bin_id, []).append(result)

    suggestions: list[PlacementSuggestion] = []
    for loc in locations.all():
        if not loc.accepts_items:
            continue
        ancestor_ids = {ancestor.id for ancestor in locations.ancestors(loc.id)[1:]}
        learned_home_scope = bool(learned_home and learned_home in ancestor_ids)
        family_home_scope = bool(
            family_home and family_home.location_id in ancestor_ids
        )
        inherited_rule_texts = [
            rule_text
            for target_id, rule_texts in rules_by_location.items()
            if target_id in ancestor_ids
            for rule_text in rule_texts
        ]
        score = 0
        reasons: list[str] = []
        loc_rank = CAPACITY_RANK.get(loc.capacity_class, CAPACITY_RANK["medium"])
        measured_fit, measured_reason = physical_fit(
            item_dimensions,
            loc.internal_dimensions,
        )
        if measured_fit is True:
            score += 45
            reasons.append(measured_reason)
        elif measured_fit is False:
            score -= 200
            reasons.append(measured_reason)
        if wants_main_cabinet and not _is_under(loc, "main-cabinet", locations):
            score -= 80
            reasons.append("outside requested main cabinet")

        if learned_home and loc.id == learned_home:
            score += 80
            reasons.append("learned home for this exact item")
        elif learned_home_scope:
            score += 65
            reasons.append("inside the learned home for this exact item")

        if rule_texts := rules_by_location.get(loc.id):
            score += 95
            reasons.append("learned rule: " + rule_texts[0])
        elif inherited_rule_texts:
            score += 70
            reasons.append("inside learned rule target: " + inherited_rule_texts[0])

        if family_home and loc.id == family_home.location_id:
            score += 55
            reasons.append(
                f"learned home from {family_home.matching_items} similar items"
            )
        elif family_home_scope and family_home:
            score += 42
            reasons.append(
                "inside a learned family home from "
                f"{family_home.matching_items} similar items"
            )

        if loc_rank >= needed_rank:
            score += 20
            reasons.append(f"fits {needed_capacity} items ({size_reason})")
            if loc_rank == needed_rank:
                score += 8
                reasons.append("capacity is not much larger than needed")
        else:
            score -= 45
            reasons.append(f"too small for inferred {needed_capacity} item")

        purpose_hits = location_match_tokens & set(loc.purpose_tags)
        if purpose_hits:
            score += 30 + 5 * len(purpose_hits)
            reasons.append("matches purpose tags: " + ", ".join(sorted(purpose_hits)))

        label_hits = location_match_tokens & tokens(loc.label)
        if label_hits:
            score += 25 + 5 * len(label_hits)
            reasons.append("matches location name: " + ", ".join(sorted(label_hits)))

        note_hits = location_match_tokens & tokens(loc.placement_notes)
        if note_hits:
            score += 12 + 3 * len(note_hits)
            reasons.append("matches placement notes: " + ", ".join(sorted(note_hits)))

        explicit_destination = bool(
            loc.id == learned_home
            or learned_home_scope
            or loc.id in rules_by_location
            or inherited_rule_texts
            or (family_home and loc.id == family_home.location_id)
            or family_home_scope
        )
        if identities_conflict(item_name, loc.label) and not explicit_destination:
            continue

        contents = contents_by_location.get(loc.id, [])
        content_hits: set[str] = set()
        content_names: list[str] = []
        conflicting_content_names: list[str] = []
        for existing in contents[:8]:
            content_names.append(existing.name)
            if identities_conflict(item_name, existing.name):
                conflicting_content_names.append(existing.name)
                continue
            content_hits |= item_tokens & (
                tokens(existing.name)
                | tokens(existing.category)
                | set(existing.aliases)
            )
        if content_hits:
            score += 22 + 4 * len(content_hits)
            reasons.append("similar items already there: " + ", ".join(sorted(content_hits)))

        if (
            contents
            and len(conflicting_content_names) == len(content_names)
            and not explicit_destination
        ):
            continue
        semantic_match = bool(purpose_hits or label_hits or note_hits or content_hits)
        if contents and not (semantic_match or explicit_destination):
            continue
        if (
            _is_dedicated_location(loc)
            and not semantic_match
            and not explicit_destination
        ):
            continue

        if loc.type == "pouch" and (purpose_hits or label_hits or content_hits):
            score += 10
            reasons.append("specific pouch beats a generic drawer when it fits")

        if score > 0 and measured_fit is not False:
            suggestions.append(
                PlacementSuggestion(
                    location=loc,
                    score=score,
                    reasons=reasons,
                    contents=content_names[:3],
                )
            )

    suggestions.sort(key=lambda s: (-s.score, s.location.label.lower(), s.location.id))
    return suggestions[:limit]


def _is_under(loc: Location, ancestor_id: str, locations: LocationStore) -> bool:
    return any(node.id == ancestor_id for node in locations.ancestors(loc.id))


def _is_dedicated_location(loc: Location) -> bool:
    if re.fullmatch(r"B\d{3}", loc.id, re.I):
        return False
    meaningful_words = tokens(loc.label) - GENERIC_LOCATION_WORDS
    return bool(meaningful_words)


def placement_context(
    command: str,
    *,
    store: InventoryStore,
    locations: LocationStore,
    guidance_path: str | Path = "config/placement-guidance.md",
) -> str:
    guidance = load_placement_guidance(guidance_path)
    stored_rules = store.rules(active_only=True, limit=100)
    rules = [
        rule for rule in stored_rules
        if locations.get(rule.target_location_id) is not None
    ][:12]
    archived_matching_rules = [
        rule
        for rule in store.matching_rules(command, limit=6)
        if (
            (target := locations.get_any(rule.target_location_id)) is not None
            and not target.active
        )
    ]
    size_rules = store.size_rules(active_only=True, limit=12)
    conflicts = [
        conflict
        for conflict in store.rule_conflicts(command, limit=6)
        if (
            conflict.get("rule_type") == "size"
            or all(
                locations.get(str(target)) is not None
                for target in conflict.get("targets", [])
            )
        )
    ]
    suggestions = suggest_locations(command, store=store, locations=locations, limit=5)
    lines: list[str] = []
    if guidance:
        lines.append("Durable placement guidance from Markdown:")
        lines.append(guidance)
    if rules:
        lines.append("Learned placement rules:")
        for rule in rules:
            lines.append(
                f"- {rule.rule_text} -> {rule.target_location_id} "
                f"(triggers: {', '.join(rule.trigger_terms)})"
            )
    if archived_matching_rules:
        lines.append("Archived placement-rule targets (do not use unless restored):")
        for rule in archived_matching_rules:
            lines.append(
                f"- {rule.rule_text} -> {rule.target_location_id} is archived "
                f"(triggers: {', '.join(rule.trigger_terms)})"
            )
    if size_rules:
        lines.append("Learned size rules:")
        for rule in size_rules:
            lines.append(
                f"- {rule.rule_text} -> {rule.size_class} "
                f"(triggers: {', '.join(rule.trigger_terms)})"
            )
    if conflicts:
        lines.append("Potential conflicting learned rules for this request:")
        for conflict in conflicts:
            shared = ", ".join(str(term) for term in conflict.get("shared_terms", []))
            rule_ids = ", ".join(str(rule_id) for rule_id in conflict.get("rule_ids", []))
            if conflict.get("rule_type") == "size":
                targets = ", ".join(str(size) for size in conflict.get("size_classes", []))
                lines.append(f"- Size rules {rule_ids} share {shared} but choose {targets}.")
            else:
                targets = ", ".join(str(target) for target in conflict.get("targets", []))
                lines.append(f"- Placement rules {rule_ids} share {shared} but choose {targets}.")
    if suggestions:
        lines.append("Ranked placement candidates for this request:")
        for idx, suggestion in enumerate(suggestions, start=1):
            reasons = "; ".join(suggestion.reasons[:3])
            contents = ", ".join(suggestion.contents)
            suffix = f" Contents: {contents}." if contents else ""
            lines.append(
                f"{idx}. {suggestion.location.id} ({suggestion.location.label}, "
                f"{suggestion.location.type}, {suggestion.location.capacity_class}) "
                f"score={suggestion.score}. {reasons}.{suffix}"
            )
    return "\n".join(lines).strip()
