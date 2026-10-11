import re

from searchable_storage_led.assistant import first_empty_bin
from searchable_storage_led.locations import Location, LocationStore, backfill_from_layout
from searchable_storage_led.placement import (
    family_home_for,
    infer_item_capacity,
    item_capacity_for,
    measured_capacity_for,
    physical_fit,
    placement_context,
    suggest_locations,
)
from searchable_storage_led.store import InventoryStore
from searchable_storage_led.layout import StorageLayout


def _stores(tmp_path):
    layout = StorageLayout.from_path("config/storage-layout.json")
    store = InventoryStore(tmp_path / "inventory.db")
    locations = LocationStore(tmp_path / "inventory.db")
    backfill_from_layout(locations, layout)
    return store, locations


def test_solder_related_items_prefer_solder_pouch(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    locations.upsert(
        Location(
            id="printercab",
            type="cabinet",
            label="PrinterCab",
            capacity_class="large",
            purpose_tags=["printer", "tools"],
        )
    )
    locations.upsert(
        Location(
            id="printercab-drawer-1",
            type="drawer",
            parent_id="printercab",
            label="PrinterCab Drawer 1",
            capacity_class="large",
            purpose_tags=["printer", "tools", "soldering"],
        )
    )
    locations.upsert(
        Location(
            id="solder-pouch",
            type="pouch",
            parent_id="printercab-drawer-1",
            label="Solder Pouch",
            capacity_class="medium",
            purpose_tags=["solder", "soldering", "electronics"],
            placement_notes="Solder, solder wick, flux, tips, and compact soldering tools.",
        )
    )
    item_id = store.upsert_item(name="flux pen", category="soldering")
    store.put_inventory(
        item_id=item_id,
        bin_id="solder-pouch",
        quantity=1,
        unit="each",
        normalize_bin=False,
    )

    suggestions = suggest_locations(
        "solder extractor tool", store=store, locations=locations, limit=3
    )

    assert suggestions[0].location.id == "solder-pouch"
    assert any("pouch" in reason for reason in suggestions[0].reasons)


def test_large_printer_items_do_not_prefer_small_parts_bins(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    locations.upsert(
        Location(
            id="printercab-drawer-1",
            type="drawer",
            label="PrinterCab Drawer 1",
            capacity_class="large",
            purpose_tags=["printer", "parts", "tools"],
        )
    )
    suggestions = suggest_locations(
        "printer extruder assembly", store=store, locations=locations, limit=5
    )

    assert suggestions[0].location.id == "printercab-drawer-1"
    assert all(
        not (
            suggestion.location.id.startswith("B")
            and suggestion.location.capacity_class in {"tiny", "small"}
        )
        for suggestion in suggestions[:3]
    )


def test_hardware_variant_conflict_prefers_an_empty_location(tmp_path) -> None:
    store = InventoryStore(tmp_path / "inventory.db")
    locations = LocationStore(tmp_path / "inventory.db")
    locations.upsert(
        Location(
            id="m4-hardware",
            type="bin",
            label="M4 Hardware",
            capacity_class="small",
        )
    )
    locations.upsert(
        Location(
            id="spare-small-bin",
            type="bin",
            label="Spare Small Bin",
            capacity_class="small",
        )
    )
    item_id = store.upsert_item(name="M4 Bolts", category="fasteners")
    store.put_inventory(
        item_id=item_id,
        bin_id="m4-hardware",
        quantity=12,
        unit="each",
        normalize_bin=False,
    )

    suggestions = suggest_locations(
        "M5 Bolts", store=store, locations=locations, limit=3
    )

    assert suggestions[0].location.id == "spare-small-bin"
    assert "m4-hardware" not in {
        suggestion.location.id for suggestion in suggestions
    }


def test_structural_roots_are_not_direct_placement_candidates(tmp_path) -> None:
    store = InventoryStore(tmp_path / "inventory.db")
    locations = LocationStore(tmp_path / "inventory.db")
    locations.upsert(Location(id="garage", type="area", label="Garage"))
    locations.upsert(
        Location(
            id="printer-cabinet",
            type="cabinet",
            label="Printer Cabinet",
            parent_id="garage",
        )
    )
    locations.upsert(
        Location(
            id="shelf-1",
            type="shelf",
            label="Shelf 1",
            parent_id="garage",
        )
    )

    suggestions = suggest_locations(
        "blue USB drive", store=store, locations=locations, limit=8
    )

    assert [suggestion.location.id for suggestion in suggestions] == ["shelf-1"]


def test_explicit_rule_can_choose_a_structural_storage_root(tmp_path) -> None:
    store = InventoryStore(tmp_path / "inventory.db")
    locations = LocationStore(tmp_path / "inventory.db")
    locations.upsert(Location(id="garage", type="area", label="Garage"))
    locations.upsert(
        Location(
            id="shelf-1",
            type="shelf",
            label="Shelf 1",
            parent_id="garage",
        )
    )
    store.learn_rule(
        rule_text="Outdoor gear belongs loose in the garage.",
        trigger_terms=["outdoor", "gear"],
        target_location_id="garage",
    )

    suggestions = suggest_locations(
        "outdoor gear", store=store, locations=locations, limit=8
    )

    assert suggestions[0].location.id == "garage"
    assert "learned rule" in suggestions[0].reasons[0]


def test_physical_fit_rotates_item_and_rejects_oversize_extent() -> None:
    fits, reason = physical_fit(
        {"length_mm": 80, "width_mm": 30, "height_mm": 20},
        {"width_mm": 35, "depth_mm": 90, "height_mm": 25},
    )
    too_large, too_large_reason = physical_fit(
        {"length_mm": 95, "width_mm": 40},
        {"width_mm": 35, "depth_mm": 90, "height_mm": 25},
    )

    assert fits is True
    assert "fits" in reason
    assert too_large is False
    assert "does not fit" in too_large_reason


def test_measured_non_fit_is_excluded_from_placement_suggestions(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    locations.upsert(
        Location(
            id="favorite-small-box",
            type="box",
            label="Favorite tool box",
            capacity_class="large",
            purpose_tags=["tool"],
            internal_dimensions={"width_mm": 50, "depth_mm": 50, "height_mm": 30},
        )
    )
    locations.upsert(
        Location(
            id="fitting-box",
            type="box",
            label="General box",
            capacity_class="large",
            internal_dimensions={"width_mm": 100, "depth_mm": 80, "height_mm": 40},
        )
    )

    suggestions = suggest_locations(
        "blue pry tool",
        store=store,
        locations=locations,
        item_dimensions={"length_mm": 75, "width_mm": 20, "height_mm": 10},
    )

    assert "favorite-small-box" not in {entry.location.id for entry in suggestions}
    fitting = next(entry for entry in suggestions if entry.location.id == "fitting-box")
    assert any("measured item" in reason for reason in fitting.reasons)


def test_measured_capacity_uses_smallest_configured_container_that_fits(tmp_path) -> None:
    locations = LocationStore(tmp_path / "inventory.db")
    locations.upsert(
        Location(
            id="tiny-drawer",
            type="drawer",
            label="Tiny drawer",
            capacity_class="tiny",
            internal_dimensions={"width_mm": 40, "depth_mm": 40, "height_mm": 20},
        )
    )
    locations.upsert(
        Location(
            id="small-drawer",
            type="drawer",
            label="Small drawer",
            capacity_class="small",
            internal_dimensions={"width_mm": 100, "depth_mm": 80, "height_mm": 40},
        )
    )

    measured = measured_capacity_for(
        {"length_mm": 75, "width_mm": 20, "height_mm": 10},
        locations=locations,
    )

    assert measured == ("small", "measured item fits Small drawer")


def test_measured_dimensions_allow_a_smaller_location_than_name_inference(tmp_path) -> None:
    store = InventoryStore(tmp_path / "inventory.db")
    locations = LocationStore(tmp_path / "inventory.db")
    locations.upsert(
        Location(
            id="small-parts",
            type="drawer",
            label="Small Parts Drawer",
            capacity_class="small",
            internal_dimensions={"width_mm": 120, "depth_mm": 90, "height_mm": 45},
        )
    )
    locations.upsert(
        Location(
            id="tool-drawer",
            type="drawer",
            label="Tool Drawer",
            capacity_class="medium",
            purpose_tags=["tool"],
            internal_dimensions={"width_mm": 350, "depth_mm": 250, "height_mm": 100},
        )
    )

    suggestions = suggest_locations(
        "blue pry tool",
        store=store,
        locations=locations,
        item_dimensions={"length_mm": 75, "width_mm": 20, "height_mm": 10},
    )

    small = next(entry for entry in suggestions if entry.location.id == "small-parts")
    assert any("fits small items (measured item" in reason for reason in small.reasons)


def test_measured_capacity_does_not_infer_oversize_from_partial_catalog(tmp_path) -> None:
    locations = LocationStore(tmp_path / "inventory.db")
    locations.upsert(
        Location(
            id="small-drawer",
            type="drawer",
            label="Small drawer",
            capacity_class="small",
            internal_dimensions={"width_mm": 100, "depth_mm": 80, "height_mm": 40},
        )
    )

    measured = measured_capacity_for(
        {"length_mm": 200, "width_mm": 100},
        locations=locations,
    )

    assert measured is None


def test_size_rule_prevents_small_bin_for_medium_item(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    locations.upsert(
        Location(
            id="jewelry-box-shelf",
            type="shelf",
            label="Jewelry Box Shelf",
            capacity_class="medium",
            purpose_tags=["jewelry"],
        )
    )
    store.learn_size_rule(
        rule_text="Jewelry boxes should be treated as medium.",
        trigger_terms=["jewelry", "box"],
        size_class="medium",
        source_command="jewelry boxes are medium",
    )

    size_class, reason = item_capacity_for("jewelry box", store=store)
    suggestions = suggest_locations("jewelry box", store=store, locations=locations, limit=5)

    assert size_class == "medium"
    assert "learned size rule" in reason
    assert suggestions[0].location.id == "jewelry-box-shelf"
    assert all(
        not (
            suggestion.location.id.startswith("B")
            and suggestion.location.capacity_class in {"tiny", "small"}
        )
        for suggestion in suggestions[:3]
    )


def test_context_size_rule_can_classify_cleaned_item_name(tmp_path) -> None:
    store, _locations = _stores(tmp_path)
    store.learn_size_rule(
        rule_text="Jewelry boxes should be treated as medium.",
        trigger_terms=["jewelry", "box"],
        size_class="medium",
        source_command="jewelry boxes are medium",
    )

    size_class, reason = item_capacity_for(
        "Box",
        store=store,
        context_text="put away jewelry box",
    )

    assert size_class == "medium"
    assert "learned size rule" in reason


def test_exact_item_size_overrides_keyword_guess(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    item_id = store.upsert_item(name="Bracelet Clasp")
    store.update_item_size(item_id=item_id, size_class="medium", source="user")

    size_class, reason = item_capacity_for("Bracelet Clasp", store=store)

    assert size_class == "medium"
    assert reason == "learned item size"


def test_explicit_size_request_overrides_learned_item_size(tmp_path) -> None:
    store, _locations = _stores(tmp_path)
    item_id = store.upsert_item(name="Magnets")
    store.update_item_size(item_id=item_id, size_class="small", source="manual")

    size_class, reason = item_capacity_for(
        "Magnets",
        store=store,
        context_text="let's put these magnets in a medium bin",
    )

    assert size_class == "medium"
    assert reason == "requested medium container"


def test_generic_small_cabinet_bin_does_not_prefer_occupied_unrelated_bin(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    layout = StorageLayout.from_path("config/storage-layout.json")
    item_id = store.upsert_item(name="Additional soldering iron tips")
    store.put_inventory(item_id=item_id, bin_id="B001", quantity=1, unit="lot")

    suggestions = suggest_locations(
        "little USB drives small bin main cabinet",
        store=store,
        locations=locations,
        limit=8,
    )

    assert "B001" not in {suggestion.location.id for suggestion in suggestions}
    assert suggestions[0].location.id == first_empty_bin(layout, store, min_size="small")


def test_unrelated_item_does_not_use_empty_dedicated_named_bin(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    locations.upsert(
        Location(
            id="allen-keys-and-wrenches-bin",
            type="bin",
            label="Allen Keys and Wrenches Bin",
            parent_id="main-cabinet",
            capacity_class="medium",
        )
    )

    suggestions = suggest_locations(
        "put away LED strip scraps",
        store=store,
        locations=locations,
        limit=20,
    )

    assert "allen-keys-and-wrenches-bin" not in {
        suggestion.location.id for suggestion in suggestions
    }
    assert suggestions
    assert re.fullmatch(r"B\d{3}", suggestions[0].location.id)


def test_matching_item_can_use_dedicated_named_bin(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    locations.upsert(
        Location(
            id="allen-keys-and-wrenches-bin",
            type="bin",
            label="Allen Keys and Wrenches Bin",
            parent_id="main-cabinet",
            capacity_class="medium",
        )
    )

    suggestions = suggest_locations(
        "put away Allen keys",
        store=store,
        locations=locations,
        limit=8,
    )

    assert suggestions[0].location.id == "allen-keys-and-wrenches-bin"
    assert any("matches location name" in reason for reason in suggestions[0].reasons)


def test_unrelated_occupied_named_location_is_not_suggested(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    locations.upsert(
        Location(
            id="electronics-pouch",
            type="pouch",
            label="Electronics Pouch",
            capacity_class="medium",
        )
    )
    item_id = store.upsert_item(name="USB adapters", category="electronics")
    store.put_inventory(
        item_id=item_id,
        bin_id="electronics-pouch",
        quantity=1,
        unit="lot",
        normalize_bin=False,
    )

    suggestions = suggest_locations(
        "put away wood screws",
        store=store,
        locations=locations,
        limit=20,
    )

    assert "electronics-pouch" not in {
        suggestion.location.id for suggestion in suggestions
    }


def test_fastener_family_matches_hardware_location_without_merging_contents(
    tmp_path,
) -> None:
    store, locations = _stores(tmp_path)
    locations.upsert(
        Location(
            id="hardware-bin",
            type="bin",
            label="Hardware bin",
            capacity_class="small",
        )
    )
    locations.upsert(
        Location(
            id="electronics-bin",
            type="bin",
            label="Electronics bin",
            capacity_class="small",
        )
    )

    suggestions = suggest_locations(
        "put away M4 T-nuts",
        store=store,
        locations=locations,
    )

    assert suggestions[0].location.id == "hardware-bin"
    assert "matches location name: hardware" in suggestions[0].reasons
    assert all(
        suggestion.location.id != "electronics-bin"
        for suggestion in suggestions
    )


def test_placement_prefers_durable_item_home_after_item_moves(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    item_id = store.upsert_item(name="M4 T-nuts", category="fasteners")
    store.put_inventory(item_id=item_id, bin_id="B001", quantity=2, unit="each")
    assert store.move_inventory(item_id=item_id, from_bin="B001", to_bin="B002") is True

    suggestions = suggest_locations("put away M4 T-nuts", store=store, locations=locations)

    assert suggestions[0].location.id == "B001"
    assert "learned home for this exact item" in suggestions[0].reasons


def test_placement_context_includes_markdown_guidance_and_ranked_locations(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    locations.upsert(
        Location(
            id="solder-pouch",
            type="pouch",
            label="Solder Pouch",
            capacity_class="medium",
            purpose_tags=["solder", "electronics"],
        )
    )
    guide = tmp_path / "placement.md"
    guide.write_text("Prefer named pouches when the item matches the pouch purpose.", encoding="utf-8")

    context = placement_context(
        "put away solder wick", store=store, locations=locations, guidance_path=guide
    )

    assert "Durable placement guidance" in context
    assert "Prefer named pouches" in context
    assert "solder-pouch" in context
    assert "fits small items" in context


def test_placement_context_includes_learned_size_rules(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    store.learn_size_rule(
        rule_text="Filament spools should be large.",
        trigger_terms=["filament", "spool"],
        size_class="large",
        source_command="filament spools are large",
    )

    context = placement_context("put away filament spool", store=store, locations=locations)

    assert "Learned size rules" in context
    assert "Filament spools should be large" in context


def test_placement_context_flags_conflicting_learned_rules(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    store.learn_rule(
        rule_text="Jewelry should go in B001.",
        trigger_terms=["jewelry"],
        target_location_id="B001",
    )
    store.learn_rule(
        rule_text="Jewelry repair parts should go in B002.",
        trigger_terms=["jewelry", "repair"],
        target_location_id="B002",
    )
    store.learn_size_rule(
        rule_text="Jewelry boxes are medium.",
        trigger_terms=["jewelry", "box"],
        size_class="medium",
    )
    store.learn_size_rule(
        rule_text="Jewelry display boxes are large.",
        trigger_terms=["jewelry", "display"],
        size_class="large",
    )

    context = placement_context("put away jewelry repair box", store=store, locations=locations)

    assert "Potential conflicting learned rules for this request" in context
    assert "Placement rules" in context
    assert "B001, B002" in context or "B002, B001" in context
    assert "Size rules" in context
    assert "medium, large" in context or "large, medium" in context


def test_placement_context_does_not_offer_archived_rule_target(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    locations.upsert(Location(id="attic", type="area", label="Attic"))
    store.learn_rule(
        rule_text="Holiday decorations belong in the attic.",
        trigger_terms=["holiday", "decorations"],
        target_location_id="attic",
    )
    locations.archive_subtree("attic")

    context = placement_context(
        "put away holiday decorations",
        store=store,
        locations=locations,
    )

    active_section = context.split("Archived placement-rule targets", 1)[0]
    assert "Holiday decorations belong in the attic" not in active_section
    assert "Archived placement-rule targets (do not use unless restored)" in context
    assert "attic is archived" in context


def test_structural_rule_selects_item_destination_inside_organizer(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    locations.upsert(
        Location(
            id="jewelry-cabinet",
            type="cabinet",
            label="Jewelry Cabinet",
            accepts_items=False,
        )
    )
    locations.upsert(
        Location(
            id="jewelry-drawer",
            type="drawer",
            parent_id="jewelry-cabinet",
            label="Drawer 1",
            capacity_class="small",
            accepts_items=True,
        )
    )
    store.learn_rule(
        rule_text="Jewelry belongs in the jewelry cabinet.",
        trigger_terms=["jewelry"],
        target_location_id="jewelry-cabinet",
    )

    suggestions = suggest_locations(
        "put away jewelry",
        store=store,
        locations=locations,
    )

    assert suggestions[0].location.id == "jewelry-drawer"
    assert all(suggestion.location.id != "jewelry-cabinet" for suggestion in suggestions)
    assert "inside learned rule target" in " ".join(suggestions[0].reasons)


def test_infer_item_capacity_uses_largest_matching_hint() -> None:
    assert infer_item_capacity("M3 screw") == "tiny"
    assert infer_item_capacity("printer extruder assembly") == "large"
    assert infer_item_capacity("cordless circular saw") == "large"


def test_repeated_t_nut_homes_generalize_to_a_new_metric_size(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    locations.upsert(
        Location(
            id="metric-t-nuts",
            type="bin",
            label="Metric T-nuts",
            capacity_class="small",
        )
    )
    for name in ("M3 T-nuts", "M4 T-nuts"):
        item_id = store.upsert_item(name=name)
        store.put_inventory(
            item_id=item_id,
            bin_id="metric-t-nuts",
            quantity=10,
            unit="pieces",
            normalize_bin=False,
        )

    evidence = family_home_for("put away M5 T-nuts", store.list_inventory())
    suggestions = suggest_locations(
        "put away M5 T-nuts",
        store=store,
        locations=locations,
    )

    assert evidence is not None
    assert evidence.location_id == "metric-t-nuts"
    assert evidence.matching_items == 2
    assert suggestions[0].location.id == "metric-t-nuts"
    assert "learned home from 2 similar items" in suggestions[0].reasons


def test_family_home_rejects_ties_and_weak_majorities(tmp_path) -> None:
    store, _locations = _stores(tmp_path)
    for name, location in (
        ("M3 T-nuts", "B001"),
        ("M4 T-nuts", "B002"),
        ("M5 T-nuts", "B001"),
    ):
        item_id = store.upsert_item(name=name)
        store.put_inventory(item_id=item_id, bin_id=location, quantity=1, unit="bag")

    assert family_home_for("M6 T-nuts", store.list_inventory()) is None

    extra = store.upsert_item(name="M8 T-nuts")
    store.put_inventory(item_id=extra, bin_id="B002", quantity=1, unit="bag")
    assert family_home_for("M10 T-nuts", store.list_inventory()) is None


def test_screw_family_home_requires_subtype_consensus(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    locations.upsert(
        Location(
            id="flat-screws",
            type="bin",
            label="Flat-head screws",
            capacity_class="small",
        )
    )
    for name in ("M3 flush mount screws", "M4 flat head screws"):
        item_id = store.upsert_item(name=name)
        store.put_inventory(
            item_id=item_id,
            bin_id="flat-screws",
            quantity=10,
            unit="pieces",
            normalize_bin=False,
        )

    assert family_home_for("M5 screws", store.list_inventory()) is None
    evidence = family_home_for("M5 countersunk screws", store.list_inventory())
    assert evidence is not None
    assert evidence.location_id == "flat-screws"


def test_explicit_rule_outranks_inferred_family_home(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    for location_id, label in (
        ("old-t-nuts", "Old T-nuts"),
        ("project-t-nuts", "Project T-nuts"),
    ):
        locations.upsert(
            Location(
                id=location_id,
                type="bin",
                label=label,
                capacity_class="small",
            )
        )
    for name in ("M3 T-nuts", "M4 T-nuts"):
        item_id = store.upsert_item(name=name)
        store.put_inventory(
            item_id=item_id,
            bin_id="old-t-nuts",
            quantity=10,
            unit="pieces",
            normalize_bin=False,
        )
    store.learn_rule(
        rule_text="Project T-nuts belong in the project bin.",
        trigger_terms=["project", "t-nuts"],
        target_location_id="project-t-nuts",
    )

    suggestions = suggest_locations(
        "put away project M5 T-nuts",
        store=store,
        locations=locations,
    )

    assert suggestions[0].location.id == "project-t-nuts"
    assert suggestions[0].reasons[0].startswith("learned rule:")
