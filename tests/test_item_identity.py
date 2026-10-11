from dataclasses import dataclass, field

from searchable_storage_led.item_identity import (
    identities_conflict,
    inferred_dimensions,
    normalize_item_dimensions,
    parse_item_identity,
    resolve_item_identity,
)


@dataclass
class Candidate:
    item_id: int
    name: str
    aliases: list[str] = field(default_factory=list)


def test_parses_metric_thread_and_length_dimensions() -> None:
    identity = parse_item_identity("M4 x 12 mm button-head hex screws")

    assert identity.family == "screw"
    assert identity.dimension_map == {
        "length_mm": 12.0,
        "thread_diameter_mm": 4.0,
    }
    assert identity.attribute_map == {"drive": "hex", "head": "button"}
    assert inferred_dimensions("M4x20 screws") == {
        "length_mm": 20.0,
        "thread_diameter_mm": 4.0,
    }


def test_normalizes_only_positive_canonical_item_dimensions() -> None:
    assert normalize_item_dimensions(
        {
            "length_mm": "304.8",
            "width_mm": 203.2,
            "height_mm": -1,
            "thread_diameter_mm": None,
            "unit": "in",
        }
    ) == {"length_mm": 304.8, "width_mm": 203.2}


def test_exact_variant_wins_and_different_length_conflicts() -> None:
    short = Candidate(1, "M4 x 12 mm button head screws")
    long = Candidate(2, "M4 x 20 mm button head screws")

    resolution = resolve_item_identity("M4x20 button-head screw", [short, long])

    assert resolution.status == "exact"
    assert resolution.selected is long
    assert identities_conflict(short.name, long.name)


def test_underspecified_hardware_variant_requires_clarification() -> None:
    short = Candidate(1, "M4 x 12 mm button head screws")
    long = Candidate(2, "M4 x 20 mm button head screws")

    resolution = resolve_item_identity("M4 button head screws", [short, long])

    assert resolution.status == "ambiguous"
    assert {match.candidate.item_id for match in resolution.matches} == {1, 2}


def test_t_nuts_do_not_match_ordinary_nuts() -> None:
    t_nuts = Candidate(1, "M4 T-nuts")
    ordinary = Candidate(2, "M4 hex nuts")

    resolution = resolve_item_identity("M4 tee nuts", [ordinary, t_nuts])

    assert resolution.selected is t_nuts
    assert identities_conflict("M4 T-nuts", "M4 hex nuts")


def test_flush_mount_is_a_flat_head_hardware_variant() -> None:
    flush = parse_item_identity("small M3 flush mount screws")

    assert flush.family == "screw"
    assert flush.dimension_map == {"thread_diameter_mm": 3.0}
    assert flush.attribute_map["head"] == "flat"
    assert identities_conflict(
        "M3 flush mount screws",
        "M3 pan head screws",
    )


def test_common_socket_and_flat_head_abbreviations_are_normalized() -> None:
    socket_cap = parse_item_identity("M4x20 SHCS")
    flat_cap = parse_item_identity("M4 x 20 FHCS")
    button_cap = parse_item_identity("M4 x 20 BHCS")

    assert socket_cap.family == flat_cap.family == button_cap.family == "screw"
    assert socket_cap.attribute_map["head"] == "socket_cap"
    assert flat_cap.attribute_map["head"] == "flat"
    assert button_cap.attribute_map["head"] == "button"


def test_imperial_screw_gauge_and_length_distinguish_variants() -> None:
    half_inch = parse_item_identity('#8 x 1/2" flat head screws')
    three_quarter = Candidate(2, '#8 x 3/4" flat head screws')
    matching = Candidate(1, '#8 x 1/2" flush mount screws')

    assert half_inch.attribute_map == {"head": "flat", "thread": "#8"}
    assert half_inch.dimension_map == {
        "length_mm": 12.7,
        "thread_diameter_mm": 4.166,
    }
    resolution = resolve_item_identity(
        '#8 x 1/2" countersunk screw',
        [three_quarter, matching],
    )
    assert resolution.selected is matching
    assert identities_conflict(matching.name, three_quarter.name)


def test_unified_thread_and_mixed_fraction_length_are_normalized() -> None:
    identity = parse_item_identity('1/4-20 x 1-1/2" socket head cap screw')

    assert identity.attribute_map == {
        "head": "socket_cap",
        "thread": "1/4-20",
    }
    assert identity.dimension_map == {
        "length_mm": 38.1,
        "thread_diameter_mm": 6.35,
    }


def test_non_hardware_x_dimension_is_not_assumed_to_be_imperial() -> None:
    assert inferred_dimensions("2 x 4 lumber") == {}


def test_flat_and_lock_washers_are_distinct_even_through_broad_aliases() -> None:
    flat = Candidate(1, "M3 flat washers", ["M3 washers"])
    lock = Candidate(2, "M3 lock washers", ["M3 washers"])
    assert identities_conflict(flat.name, lock.name)
    assert resolve_item_identity("M3 flat washer", [lock]).status == "none"
    assert resolve_item_identity("M3 lock washer", [flat]).status == "none"
    assert resolve_item_identity("M3 plain washer", [lock, flat]).selected is flat
    assert resolve_item_identity("M3 locking washer", [flat, lock]).selected is lock
    assert resolve_item_identity("M3 washers", [flat, lock]).status == "ambiguous"


def test_lock_washer_styles_are_distinct_without_inventing_a_generic_style() -> None:
    split = Candidate(1, "M3 split lock washers")
    toothed = Candidate(2, "M3 toothed lock washers")
    assert identities_conflict(split.name, toothed.name)
    assert resolve_item_identity("M3 lock washer", [split, toothed]).status == "ambiguous"
    assert "washer_kind" not in parse_item_identity("M3 washer").attribute_map
    assert "washer_kind" not in parse_item_identity("M3 flat head screw").attribute_map


def test_generic_washer_exact_name_does_not_hide_variant_ambiguity() -> None:
    generic = Candidate(1, "M3 washers")
    lock = Candidate(2, "M3 lock washers")
    resolution = resolve_item_identity("M3 washers", [generic, lock])
    assert resolution.status == "ambiguous"
    assert resolution.selected is None


def test_generic_lock_washer_name_does_not_hide_style_ambiguity() -> None:
    generic = Candidate(1, "M3 lock washers")
    split = Candidate(2, "M3 split lock washers")
    assert resolve_item_identity("M3 lock washers", [generic, split]).status == "ambiguous"
