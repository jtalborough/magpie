from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable, Protocol


class IdentityCandidate(Protocol):
    item_id: int
    name: str
    aliases: list[str]


@dataclass(frozen=True)
class ItemIdentity:
    text: str
    tokens: frozenset[str]
    family: str
    attributes: tuple[tuple[str, str], ...]
    dimensions: tuple[tuple[str, float], ...]

    @property
    def attribute_map(self) -> dict[str, str]:
        return dict(self.attributes)

    @property
    def dimension_map(self) -> dict[str, float]:
        return dict(self.dimensions)


@dataclass(frozen=True)
class IdentityMatch:
    candidate: IdentityCandidate
    score: float
    exact_name: bool


@dataclass(frozen=True)
class IdentityResolution:
    status: str
    matches: tuple[IdentityMatch, ...]

    @property
    def selected(self) -> IdentityCandidate | None:
        return self.matches[0].candidate if self.status in {"exact", "unique"} else None


_FAMILIES = (
    ("t_nut", re.compile(r"\b(?:t[ -]?nuts?|tee nuts?)\b", re.I)),
    ("screw", re.compile(r"\b(?:screws?|shcs|fhcs|bhcs)\b", re.I)),
    ("bolt", re.compile(r"\bbolts?\b", re.I)),
    ("washer", re.compile(r"\bwashers?\b", re.I)),
    ("nut", re.compile(r"\bnuts?\b", re.I)),
    ("bearing", re.compile(r"\bbearings?\b", re.I)),
    ("connector", re.compile(r"\bconnectors?\b", re.I)),
)

_ATTRIBUTES = {
    "head": {
        "button": r"\bbutton[ -]?heads?\b|\bbhcs\b",
        "socket_cap": (
            r"\b(?:socket[ -]?(?:head(?:[ -]?cap)?|cap)|cap[ -]?head)s?\b"
            r"|\bshcs\b"
        ),
        "flat": (
            r"\b(?:flat|countersunk|flush(?:[ -]?mount)?)[ -]?heads?\b"
            r"|\b(?:countersunk|flush[ -]?mount|fhcs)\b"
        ),
        "pan": r"\bpan[ -]?heads?\b",
        "hex": r"\bhex[ -]?heads?\b",
    },
    "drive": {
        "hex": r"\b(?:hex|allen|internal[ -]?hex)(?:[ -]?(?:drive|socket)s?)?\b",
        "torx": r"\btorx\b",
        "phillips": r"\bphillips\b",
        "slotted": r"\bslotted\b",
    },
}

_STOP_TOKENS = {
    "a",
    "an",
    "and",
    "each",
    "hardware",
    "item",
    "items",
    "of",
    "piece",
    "pieces",
    "the",
}


def parse_item_identity(value: str) -> ItemIdentity:
    raw_text = value.strip().lower().replace("×", "x")
    text = _canonical_text(value)
    imperial_thread = _imperial_thread(raw_text)
    dimensions: dict[str, float] = {}
    metric = re.search(
        r"\bm\s*(\d+(?:\.\d+)?)\s*[x×]\s*(\d+(?:\.\d+)?)\s*(?:mm)?\b",
        text,
        re.I,
    )
    if metric:
        dimensions["thread_diameter_mm"] = float(metric.group(1))
        dimensions["length_mm"] = float(metric.group(2))
    else:
        thread = re.search(r"\bm\s*(\d+(?:\.\d+)?)\b", text, re.I)
        if thread:
            dimensions["thread_diameter_mm"] = float(thread.group(1))
        length = re.search(r"\b(\d+(?:\.\d+)?)\s*mm\b", text, re.I)
        if length:
            dimensions["length_mm"] = float(length.group(1))

    imperial_length = _imperial_length_mm(raw_text, thread=imperial_thread)
    if "length_mm" not in dimensions and imperial_length is not None:
        dimensions["length_mm"] = imperial_length

    family = next(
        (name for name, pattern in _FAMILIES if pattern.search(raw_text)),
        "",
    )
    attributes: dict[str, str] = {}
    if family == "washer":
        if re.search(r"\b(?:lock(?:ing)?|split|tooth(?:ed)?|star)\b", text):
            attributes["washer_kind"] = "lock"
            if re.search(r"\bsplit\b", text):
                attributes["washer_lock_style"] = "split"
            elif re.search(r"\b(?:tooth(?:ed)?|star)\b", text):
                attributes["washer_lock_style"] = "toothed"
        elif re.search(r"\b(?:flat|plain)\b", text):
            attributes["washer_kind"] = "flat"
    for attribute, values in _ATTRIBUTES.items():
        match = next(
            (
                name
                for name, pattern in values.items()
                if re.search(pattern, raw_text, re.I)
            ),
            None,
        )
        if match:
            attributes[attribute] = match
    if imperial_thread:
        attributes["thread"] = imperial_thread
        if "thread_diameter_mm" not in dimensions:
            diameter = _imperial_thread_diameter_mm(imperial_thread)
            if diameter is not None:
                dimensions["thread_diameter_mm"] = diameter

    tokens = {
        _singular(token)
        for token in re.findall(r"[a-z]+|\d+(?:\.\d+)?", text)
        if token not in _STOP_TOKENS and token not in {"mm", "x"}
    }
    return ItemIdentity(
        text=text,
        tokens=frozenset(tokens),
        family=family,
        attributes=tuple(sorted(attributes.items())),
        dimensions=tuple(sorted(dimensions.items())),
    )


def inferred_dimensions(value: str) -> dict[str, float]:
    return parse_item_identity(value).dimension_map


def normalize_item_dimensions(value: object) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    dimensions: dict[str, float] = {}
    for key in ("length_mm", "width_mm", "height_mm", "thread_diameter_mm"):
        try:
            number = float(value.get(key))
        except (TypeError, ValueError):
            continue
        if 0 < number <= 100_000:
            dimensions[key] = round(number, 3)
    return dimensions


def resolve_item_identity(
    query: str,
    candidates: Iterable[IdentityCandidate],
) -> IdentityResolution:
    query_identity = parse_item_identity(query)
    by_item: dict[int, IdentityMatch] = {}
    for candidate in candidates:
        # A broad alias must not erase a known hardware variant.
        if _identity_score(query_identity, parse_item_identity(candidate.name)) is None:
            continue
        names = [candidate.name, *candidate.aliases]
        identities = [parse_item_identity(name) for name in names if name.strip()]
        scored = [
            _identity_score(query_identity, identity)
            for identity in identities
        ]
        compatible = [entry for entry in scored if entry is not None]
        if not compatible:
            continue
        exact_name = any(
            query_identity.text == identity.text
            or _identities_equivalent(query_identity, identity)
            for identity in identities
        )
        match = IdentityMatch(candidate, max(compatible), exact_name)
        previous = by_item.get(candidate.item_id)
        if previous is None or match.score > previous.score:
            by_item[candidate.item_id] = match

    matches = tuple(
        sorted(
            by_item.values(),
            key=lambda match: (-int(match.exact_name), -match.score, match.candidate.name.lower()),
        )
    )
    if not matches:
        return IdentityResolution("none", ())
    if query_identity.family == "washer" and len(matches) > 1:
        for attribute in ("washer_kind", "washer_lock_style"):
            variants = {
                parse_item_identity(match.candidate.name).attribute_map.get(attribute)
                for match in matches
            }
            if attribute not in query_identity.attribute_map and len(variants) > 1:
                return IdentityResolution("ambiguous", matches)
    exact = tuple(match for match in matches if match.exact_name)
    if len(exact) == 1:
        return IdentityResolution("exact", exact)
    if len(matches) == 1:
        return IdentityResolution("unique", matches)
    return IdentityResolution("ambiguous", matches)


def identities_conflict(left: str, right: str) -> bool:
    return _identity_score(parse_item_identity(left), parse_item_identity(right)) is None


def _identity_score(query: ItemIdentity, candidate: ItemIdentity) -> float | None:
    if query.family and candidate.family and query.family != candidate.family:
        return None
    query_attributes = query.attribute_map
    candidate_attributes = candidate.attribute_map
    for key in query_attributes.keys() & candidate_attributes.keys():
        if query_attributes[key] != candidate_attributes[key]:
            return None
    query_dimensions = query.dimension_map
    candidate_dimensions = candidate.dimension_map
    for key in query_dimensions.keys() & candidate_dimensions.keys():
        if abs(query_dimensions[key] - candidate_dimensions[key]) > 0.01:
            return None

    overlap = len(query.tokens & candidate.tokens)
    denominator = max(1, len(query.tokens))
    score = 10.0 * overlap / denominator
    if query.family and query.family == candidate.family:
        score += 20
    score += 12 * len(query_attributes.keys() & candidate_attributes.keys())
    score += 25 * len(query_dimensions.keys() & candidate_dimensions.keys())
    if query.text == candidate.text:
        score += 100
    return score


def _identities_equivalent(left: ItemIdentity, right: ItemIdentity) -> bool:
    return bool(
        left.family == right.family
        and left.attribute_map == right.attribute_map
        and left.dimension_map == right.dimension_map
        and left.tokens == right.tokens
    )


def _canonical_text(value: str) -> str:
    text = value.strip().lower().replace("×", "x")
    text = re.sub(r"\bm\s+(\d)", r"m\1", text)
    text = re.sub(r"(?<=\d)\s*x\s*(?=\d)", " x ", text)
    text = re.sub(r"[-_/]+", " ", text)
    text = re.sub(r"[^a-z0-9. ]+", " ", text)
    words = [_singular(word) for word in text.split()]
    return " ".join(words)


def _imperial_thread(value: str) -> str:
    if gauge := re.search(r"(?:^|\s)#\s*(\d+)\b", value):
        return f"#{int(gauge.group(1))}"
    unified = re.search(r"\b(\d+\s*/\s*\d+)\s*-\s*(\d+)\b", value)
    if not unified:
        return ""
    fraction = _fraction_value(unified.group(1))
    if fraction is None:
        return ""
    numerator, denominator = _reduced_fraction(unified.group(1))
    return f"{numerator}/{denominator}-{int(unified.group(2))}"


def _imperial_length_mm(value: str, *, thread: str = "") -> float | None:
    match = re.search(
        r"(?:\bx\b|\d\s*[x×])\s*"
        r"(?P<length>\d+\s*[- ]\s*\d+\s*/\s*\d+|\d+\s*/\s*\d+|\d+(?:\.\d+)?)"
        r"\s*(?P<unit>in(?:ch(?:es)?)?|[\"″])?(?=\s|$|[),.;])",
        value,
        re.I,
    )
    if not match or (not thread and not match.group("unit")):
        return None
    inches = _fraction_value(match.group("length"))
    if inches is None or inches <= 0 or inches > 200:
        return None
    return round(inches * 25.4, 3)


def _imperial_thread_diameter_mm(thread: str) -> float | None:
    if thread.startswith("#"):
        try:
            gauge = int(thread[1:])
        except ValueError:
            return None
        if not 0 <= gauge <= 24:
            return None
        return round((0.06 + 0.013 * gauge) * 25.4, 3)
    fraction = thread.split("-", 1)[0]
    inches = _fraction_value(fraction)
    if inches is None or inches <= 0 or inches > 4:
        return None
    return round(inches * 25.4, 3)


def _fraction_value(value: str) -> float | None:
    compact = re.sub(r"\s+", "", value).replace("-", " ")
    try:
        if " " in compact:
            whole, fraction = compact.split(" ", 1)
            numerator, denominator = fraction.split("/", 1)
            return float(whole) + float(numerator) / float(denominator)
        if "/" in compact:
            numerator, denominator = compact.split("/", 1)
            return float(numerator) / float(denominator)
        return float(compact)
    except (ValueError, ZeroDivisionError):
        return None


def _reduced_fraction(value: str) -> tuple[int, int]:
    numerator_text, denominator_text = re.sub(r"\s+", "", value).split("/", 1)
    numerator = int(numerator_text)
    denominator = int(denominator_text)
    divisor = _greatest_common_divisor(numerator, denominator)
    return numerator // divisor, denominator // divisor


def _greatest_common_divisor(left: int, right: int) -> int:
    while right:
        left, right = right, left % right
    return max(1, abs(left))


def _singular(token: str) -> str:
    if token.endswith("ies") and len(token) > 4:
        return token[:-3] + "y"
    if token.endswith("es") and len(token) > 4 and token[-3] in {"s", "x", "z"}:
        return token[:-2]
    if token.endswith("s") and len(token) > 3 and not token.endswith("ss"):
        return token[:-1]
    return token
