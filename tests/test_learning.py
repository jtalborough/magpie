import json

from fastapi.testclient import TestClient
import pytest

from searchable_storage_led.api import create_app
from searchable_storage_led.config import LedConfig
from searchable_storage_led.store import InventoryStore


def _store(tmp_path) -> InventoryStore:
    return InventoryStore(tmp_path / "inventory.db")


def test_learned_alias_improves_future_search(tmp_path) -> None:
    store = _store(tmp_path)
    item_id = store.upsert_item(name="Star Bits")
    store.put_inventory(item_id=item_id, bin_id="B005", quantity=1, unit="each")

    assert store.search("torx") == []  # unknown word today

    store.learn_alias(item_id=item_id, alias="torx")

    found = store.search("torx")
    assert found and found[0].name == "Star Bits"


def test_exact_match_short_circuits_on_name_or_alias(tmp_path) -> None:
    store = _store(tmp_path)
    item_id = store.upsert_item(name="Heat Shrink")
    store.put_inventory(item_id=item_id, bin_id="B009", quantity=1, unit="each")
    store.learn_alias(item_id=item_id, alias="shrink wrap")

    assert store.exact_match("heat shrink")[0].bin_id == "B009"
    assert store.exact_match("shrink wrap")[0].name == "Heat Shrink"
    assert store.exact_match("nonsense") == []


def test_alias_cannot_identify_two_hardware_variants(tmp_path) -> None:
    store = _store(tmp_path)
    short = store.upsert_item(name="M4 x 12 mm screws")
    long = store.upsert_item(name="M4 x 20 mm screws")
    store.put_inventory(item_id=short, bin_id="B001", quantity=10, unit="pieces")
    store.put_inventory(item_id=long, bin_id="B002", quantity=10, unit="pieces")
    store.learn_alias(item_id=long, alias="my M4 screws")

    with pytest.raises(ValueError, match="already identifies M4 x 20 mm screws"):
        store.learn_alias(item_id=short, alias="my M4 screws")

    assert store.exact_match("my M4 screws")[0].item_id == long


def test_suggest_home_returns_most_recent_location(tmp_path) -> None:
    store = _store(tmp_path)
    item_id = store.upsert_item(name="Calipers")
    store.put_inventory(item_id=item_id, bin_id="B012", quantity=1, unit="each")

    assert store.suggest_home("calipers") == "B012"
    assert store.suggest_home("unknown thing") is None


def test_examples_accumulate_and_corrections_rank_first(tmp_path) -> None:
    store = _store(tmp_path)
    store.record_example(command="put m3 bolts away", action="[]", kind="confirmed")
    store.record_example(command="find m3 bolts", action="{}", kind="correction")
    store.record_example(command="put washers away", action="[]", kind="confirmed")

    assert len(store.recent_examples(limit=10)) == 3

    hits = store.find_examples("where are my m3 bolts", limit=5)
    commands = [h["command"] for h in hits]
    assert "find m3 bolts" in commands
    # Correction outranks the plain confirmation for the same terms.
    assert commands[0] == "find m3 bolts"


def test_examples_for_eval_includes_metadata_and_filters_kind(tmp_path) -> None:
    store = _store(tmp_path)
    store.record_example(command="put m3 bolts away", action="[]", kind="confirmed")
    store.record_example(command="find m3 bolts", action="{}", kind="correction")

    confirmed = store.examples_for_eval(limit=10, kind="confirmed")

    assert len(confirmed) == 1
    assert confirmed[0]["command"] == "put m3 bolts away"
    assert confirmed[0]["kind"] == "confirmed"
    assert isinstance(confirmed[0]["id"], int)
    assert confirmed[0]["created_at"]


def test_example_kind_counts_exclude_rejected_by_default(tmp_path) -> None:
    store = _store(tmp_path)
    store.record_example(command="put m3 bolts away", action="[]", kind="confirmed")
    store.record_example(command="manual move", action="[]", kind="manual")
    store.record_example(command="rejected correction", action="{}", kind="correction")
    rejected_id = store.examples_for_eval(limit=1)[0]["id"]
    store.reject_example(rejected_id)

    assert store.example_kind_counts() == {"confirmed": 1, "manual": 1}
    assert store.example_kind_counts(include_rejected=True) == {
        "confirmed": 1,
        "correction": 1,
        "manual": 1,
    }


def test_learned_rules_match_future_placement_terms(tmp_path) -> None:
    store = _store(tmp_path)

    rule_id = store.learn_rule(
        rule_text="Jewelry should go in the main cabinet.",
        trigger_terms=["jewelry", "bracelet", "ring"],
        target_location_id="main-cabinet",
        source_command="I always keep jewelry in the main cabinet",
    )

    rules = store.rules()
    assert rules[0].id == rule_id
    assert rules[0].target_location_id == "main-cabinet"
    assert rules[0].source_command == "I always keep jewelry in the main cabinet"
    assert (
        store.matching_rules("bracelet clasp")[0].rule_text
        == "Jewelry should go in the main cabinet."
    )

    assert store.deactivate_rule(rule_id=rule_id, rule_type="placement")
    assert store.matching_rules("bracelet clasp") == []


def test_rule_conflicts_identify_overlapping_targets_and_sizes(tmp_path) -> None:
    store = _store(tmp_path)
    first = store.learn_rule(
        rule_text="Jewelry should go in the main cabinet.",
        trigger_terms=["jewelry", "ring"],
        target_location_id="main-cabinet",
    )
    second = store.learn_rule(
        rule_text="Jewelry repair parts should go in the bench drawer.",
        trigger_terms=["jewelry", "repair"],
        target_location_id="bench-drawer",
    )
    size_first = store.learn_size_rule(
        rule_text="Jewelry boxes are medium.",
        trigger_terms=["jewelry", "box"],
        size_class="medium",
    )
    size_second = store.learn_size_rule(
        rule_text="Jewelry display boxes are large.",
        trigger_terms=["jewelry", "display"],
        size_class="large",
    )

    conflicts = store.rule_conflicts("jewelry")

    placement = next(c for c in conflicts if c["rule_type"] == "placement")
    size = next(c for c in conflicts if c["rule_type"] == "size")
    assert set(placement["rule_ids"]) == {first, second}
    assert placement["shared_terms"] == ["jewelry"]
    assert set(placement["targets"]) == {"main-cabinet", "bench-drawer"}
    assert set(size["rule_ids"]) == {size_first, size_second}
    assert size["shared_terms"] == ["jewelry"]
    assert set(size["size_classes"]) == {"medium", "large"}


def test_update_rules_by_id_changes_matching_behavior(tmp_path) -> None:
    store = _store(tmp_path)
    placement_id = store.learn_rule(
        rule_text="Jewelry should go in the main cabinet.",
        trigger_terms=["jewelry"],
        target_location_id="main-cabinet",
    )
    size_id = store.learn_size_rule(
        rule_text="Jewelry boxes should be medium.",
        trigger_terms=["jewelry", "box"],
        size_class="medium",
    )

    assert store.update_rule(
        rule_id=placement_id,
        rule_text="Watches should go in the top tray.",
        trigger_terms=["watch", "watches"],
        target_location_id="top-tray",
        source_command="Change placement rule",
    )
    assert store.update_size_rule(
        rule_id=size_id,
        rule_text="Watch boxes should be small.",
        trigger_terms=["watch", "box"],
        size_class="small",
        source_command="Change size rule",
    )

    assert store.matching_rules("jewelry") == []
    placement = store.matching_rules("watch strap")[0]
    assert placement.id == placement_id
    assert placement.target_location_id == "top-tray"
    assert placement.source_command == "Change placement rule"
    size = store.matching_size_rules("watch box")[0]
    assert size.id == size_id
    assert size.size_class == "small"
    assert store.update_rule(
        rule_id=999,
        rule_text="Missing rule.",
        trigger_terms=["missing"],
        target_location_id="nowhere",
    ) is False


def test_item_size_and_size_rules_round_trip(tmp_path) -> None:
    store = _store(tmp_path)
    item_id = store.upsert_item(name="Filament Spool")

    store.update_item_size(
        item_id=item_id,
        size_class="large",
        source="user",
        dimensions={"length": 200, "unit": "mm"},
    )
    rule_id = store.learn_size_rule(
        rule_text="Jewelry boxes should be treated as medium.",
        trigger_terms=["jewelry", "box"],
        size_class="medium",
        source_command="jewelry boxes are medium",
    )

    store.put_inventory(item_id=item_id, bin_id="B001", quantity=1, unit="each")
    item = store.search("filament spool")[0]
    assert item.size_class == "large"
    assert item.size_source == "user"
    assert item.dimensions == {"length": 200, "unit": "mm"}
    assert store.item_size_class("Filament Spool") == "large"
    rule = store.matching_size_rules("small jewelry box")[0]
    assert rule.id == rule_id
    assert rule.size_class == "medium"

    assert store.deactivate_rule(rule_id=rule_id, rule_type="size")
    assert store.matching_size_rules("small jewelry box") == []


def test_correction_endpoint_learns_alias_end_to_end(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [{"size": "small", "bin_count": 1, "spans": [1]}],
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "inventory.db"
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(db_path),
    )
    seed = InventoryStore(db_path)
    item_id = seed.upsert_item(name="Dupont Wires")
    seed.put_inventory(item_id=item_id, bin_id="B001", quantity=1, unit="each")

    client = TestClient(create_app(cfg))
    response = client.post(
        "/feedback/correction", json={"query": "jumper cables", "item_id": item_id}
    )

    assert response.status_code == 200
    # The correction taught the system a word — the same search now resolves.
    assert InventoryStore(db_path).search("jumper cables")[0].name == "Dupont Wires"
