import json

import pytest
from fastapi.testclient import TestClient

from searchable_storage_led.agent import (
    AGENT_INSTRUCTIONS,
    TOOL_SCHEMAS,
    _run_read_tool,
    execute_plan,
    normalize_plan,
    run_read_tool,
    run_agent,
    slugify,
)
from searchable_storage_led.assistant import first_empty_bin
from searchable_storage_led.api import create_app
from searchable_storage_led.config import LedConfig, LlmConfig
from searchable_storage_led.layout import StorageLayout
from searchable_storage_led.llm_client import LlmClient
from searchable_storage_led.locations import (
    LOCATION_TYPES,
    Location,
    LocationStore,
    backfill_from_layout,
)
from searchable_storage_led.store import InventoryStore


def _tool_calls(calls, content=None):
    return {
        "choices": [
            {
                "message": {
                    "content": content,
                    "tool_calls": [
                        {
                            "id": f"c{i}",
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)},
                        }
                        for i, (name, args) in enumerate(calls)
                    ],
                }
            }
        ]
    }


def _text(content):
    return {"choices": [{"message": {"content": content}}]}


class Scripted:
    def __init__(self, responses):
        self.responses = list(responses)
        self.bodies = []

    def __call__(self, url, headers, body):
        self.bodies.append(body)
        return self.responses.pop(0)


def _client(scripted):
    return LlmClient(LlmConfig(api_key_env="OR_KEY"), env={"OR_KEY": "x"}, transport=scripted)


def _stores(tmp_path):
    layout = StorageLayout.from_path("config/storage-layout.json")
    store = InventoryStore(tmp_path / "inventory.db")
    locations = LocationStore(tmp_path / "inventory.db")
    backfill_from_layout(locations, layout)
    return store, locations


def test_agent_can_restrict_tools_for_voice_adapters(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    scripted = Scripted([_text("Use Places to create a new bin.")])

    result = run_agent(
        "Put away M4 screws in a new bin",
        store=store,
        locations=locations,
        client=_client(scripted),
        allowed_tools=frozenset(
            {
                "attach_photo",
                "move_item",
                "put_item",
                "search_items",
                "suggest_locations",
            }
        ),
    )

    offered_tools = {
        tool["function"]["name"] for tool in scripted.bodies[0]["tools"]
    }
    assert result.plan == []
    assert {"attach_photo", "move_item", "put_item"}.issubset(offered_tools)
    assert "create_location" not in offered_tools
    assert "update_location" not in offered_tools
    assert "archive_location" not in offered_tools
    assert "restore_location" not in offered_tools
    assert "move_container" not in offered_tools
    assert "preview_label_sheet" not in offered_tools
    assert "list_rules" not in offered_tools


def test_agent_rejects_hallucinated_write_outside_adapter_policy(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    scripted = Scripted(
        [
            _tool_calls(
                [
                    ("create_location", {"type": "bin", "label": "New Bin"}),
                    (
                        "put_item",
                        {"item": "M4 screws", "location_id": "new-bin"},
                    ),
                ]
            ),
            _text("Open Places to create the destination, then continue put-away."),
        ]
    )

    result = run_agent(
        "Put away M4 screws in a new bin",
        store=store,
        locations=locations,
        client=_client(scripted),
        allowed_tools=frozenset(
            {
                "attach_photo",
                "move_item",
                "put_item",
                "search_items",
                "suggest_locations",
            }
        ),
    )

    assert result.plan == []
    [guard] = [
        entry for entry in result.trace if entry["tool"] == "input_policy_guard"
    ]
    assert guard["arguments"] == {"blocked_tools": ["create_location"]}
    assert guard["result"]["accepted"] is False


def test_agent_can_propose_archive_and_restore_locations(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    locations.upsert(Location(id="attic", type="area", label="Attic"))
    archive_script = Scripted(
        [_tool_calls([("archive_location", {"location_id": "attic"})])]
    )

    archive_result = run_agent(
        "Archive the attic",
        store=store,
        locations=locations,
        client=_client(archive_script),
    )

    assert archive_result.plan == [
        {"tool": "archive_location", "arguments": {"location_id": "attic"}}
    ]
    applied = execute_plan(archive_result.plan, store=store, locations=locations)
    assert applied[0]["archived_location_id"] == "attic"
    assert locations.get("attic") is None

    archived = _run_read_tool(
        "list_archived_locations", {}, store=store, locations=locations
    )
    assert archived[0]["id"] == "attic"

    restored = execute_plan(
        [{"tool": "restore_location", "arguments": {"location_id": "attic"}}],
        store=store,
        locations=locations,
    )
    assert restored[0]["restored_location_id"] == "attic"
    assert locations.get("attic").active is True


def test_search_marks_inventory_inside_archived_storage(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    locations.upsert(Location(id="attic", type="area", label="Attic"))
    item_id = store.upsert_item(name="holiday lights")
    store.put_inventory(item_id=item_id, bin_id="attic", quantity=2, unit="boxes")
    locations.archive_subtree("attic")

    results = _run_read_tool(
        "search_items", {"query": "holiday lights"}, store=store, locations=locations
    )

    assert results[0]["archived"] is True
    assert results[0]["path"] == [{"id": "attic", "label": "Attic"}]
    assert results[0]["nearest_lit_location_id"] is None


def _case_find_solder(store, locations):
    item_id = store.upsert_item(name="solder wick")
    store.put_inventory(item_id=item_id, bin_id="B001", quantity=1, unit="roll")
    return Scripted(
        [
            _tool_calls([("search_items", {"query": "solder wick"})]),
            _text("Solder wick is in B001."),
        ]
    )


def _case_put_away_known_item(store, locations):
    locations.upsert(
        Location(
            id="solder-pouch",
            type="pouch",
            label="Solder Pouch",
            parent_id="B001",
            capacity_class="small",
            purpose_tags=["solder"],
        )
    )
    return Scripted(
        [
            _tool_calls([("suggest_locations", {"item": "solder wick"})]),
            _tool_calls([("put_item", {"item": "solder wick", "location_id": "solder-pouch"})]),
        ]
    )


def _case_consolidate_tools(store, locations):
    allen_id = store.upsert_item(name="Allen keys")
    wrench_id = store.upsert_item(name="metric wrenches")
    store.put_inventory(item_id=allen_id, bin_id="B041", quantity=1, unit="set")
    store.put_inventory(item_id=wrench_id, bin_id="B042", quantity=1, unit="set")
    return Scripted(
        [
            _tool_calls(
                [
                    (
                        "plan_reorganization",
                        {
                            "queries": ["Allen keys", "wrenches"],
                            "destination_label": "Large Hand Tool Bin",
                            "parent_id": "main-cabinet",
                            "capacity_class": "large",
                        },
                    ),
                ]
            ),
            _tool_calls(
                [
                    (
                        "create_location",
                        {
                            "type": "bin",
                            "label": "Large Hand Tool Bin",
                            "parent_id": "main-cabinet",
                            "capacity_class": "large",
                        },
                    ),
                    (
                        "move_item",
                        {
                            "item_id": allen_id,
                            "item": "Allen keys",
                            "from_location_id": "B041",
                            "to_location_id": "large-hand-tool-bin",
                        },
                    ),
                    (
                        "move_item",
                        {
                            "item_id": wrench_id,
                            "item": "metric wrenches",
                            "from_location_id": "B042",
                            "to_location_id": "large-hand-tool-bin",
                        },
                    ),
                ]
            ),
        ]
    )


def _case_remove_discarded_item(store, locations):
    item_id = store.upsert_item(name="dead batteries")
    store.put_inventory(item_id=item_id, bin_id="B001", quantity=4, unit="each")
    return Scripted(
        [
            _tool_calls([("search_items", {"query": "dead batteries"})]),
            _tool_calls(
                [
                    (
                        "remove_item",
                        {
                            "item_id": item_id,
                            "item": "dead batteries",
                            "location_id": "B001",
                        },
                    )
                ]
            ),
        ]
    )


def _case_learn_placement_rule(store, locations):
    return Scripted(
        [
            _tool_calls(
                [
                    (
                        "learn_rule",
                        {
                            "rule_text": "Jewelry should go in the main cabinet.",
                            "trigger_terms": ["jewelry"],
                            "target_location_id": "main-cabinet",
                        },
                    )
                ]
            )
        ]
    )


def _case_learn_size_rule(store, locations):
    return Scripted(
        [
            _tool_calls(
                [
                    (
                        "learn_size_rule",
                        {
                            "rule_text": "Jewelry boxes are medium.",
                            "trigger_terms": ["jewelry", "box"],
                            "size_class": "medium",
                        },
                    )
                ]
            )
        ]
    )


def _case_correct_fit(store, locations):
    return Scripted(
        [
            _tool_calls(
                [
                    (
                        "update_item_size",
                        {"item": "solder extractor", "size_class": "large"},
                    )
                ]
            )
        ]
    )


def _case_update_item_metadata(store, locations):
    item_id = store.upsert_item(name="Generic Hardware")
    store.put_inventory(item_id=item_id, bin_id="B001", quantity=2, unit="each")
    return Scripted(
        [
            _tool_calls([("search_items", {"query": "Generic Hardware"})]),
            _tool_calls(
                [
                    (
                        "update_item",
                        {
                            "item_id": item_id,
                            "name": "M4 T-nuts",
                            "category": "fasteners",
                        },
                    )
                ]
            ),
        ]
    )


def _case_rule_inspection(store, locations):
    store.learn_rule(
        rule_text="Jewelry should go in the main cabinet.",
        trigger_terms=["jewelry"],
        target_location_id="main-cabinet",
    )
    store.learn_size_rule(
        rule_text="Jewelry boxes are medium.",
        trigger_terms=["jewelry", "box"],
        size_class="medium",
    )
    return Scripted(
        [
            _tool_calls([("list_rules", {"query": "jewelry"})]),
            _text("Jewelry has one placement rule and one size rule."),
        ]
    )


def _case_label_sheet(store, locations):
    return Scripted(
        [
            _tool_calls([("preview_label_sheet", {"location_id": "main-cabinet"})]),
            _text("Here is the printable label sheet for the main cabinet."),
        ]
    )


def _case_photo_sort(store, locations):
    return Scripted(
        [
            _tool_calls(
                [
                    ("put_item", {"item": "tactile switches", "location_id": "B034", "quantity": 6}),
                    ("put_item", {"item": "LEDs", "location_id": "B035", "quantity": 10}),
                    ("put_item", {"item": "ceramic capacitors", "location_id": "B036", "quantity": 2}),
                ]
            )
        ]
    )


FEATURE_COMPLETE_EVALS: list[dict[str, object]] = [
    {
        "name": "find",
        "command": "Where is my solder wick?",
        "script": _case_find_solder,
        "trace_tools": ["search_items"],
        "plan_tools": [],
    },
    {
        "name": "put-away-known-item",
        "command": "Put away solder wick.",
        "script": _case_put_away_known_item,
        "trace_tools": ["suggest_locations"],
        "plan_tools": ["put_item"],
    },
    {
        "name": "consolidate-tools",
        "command": "Move all Allen keys and wrenches to a big bin.",
        "script": _case_consolidate_tools,
        "trace_tools": ["plan_reorganization"],
        "plan_tools": ["create_location", "move_item", "move_item"],
    },
    {
        "name": "remove-discarded-item",
        "command": "I threw away the dead batteries.",
        "script": _case_remove_discarded_item,
        "trace_tools": ["search_items"],
        "plan_tools": ["remove_item"],
    },
    {
        "name": "learn-placement",
        "command": "I always keep jewelry in the main cabinet.",
        "script": _case_learn_placement_rule,
        "trace_tools": [],
        "plan_tools": ["learn_rule"],
    },
    {
        "name": "learn-size",
        "command": "Jewelry boxes are medium.",
        "script": _case_learn_size_rule,
        "trace_tools": [],
        "plan_tools": ["learn_size_rule"],
    },
    {
        "name": "correct-fit",
        "command": "That does not fit, this solder extractor is large.",
        "script": _case_correct_fit,
        "trace_tools": [],
        "plan_tools": ["update_item_size"],
    },
    {
        "name": "update-item-metadata",
        "command": "Rename Generic Hardware to M4 T-nuts and mark it as fasteners.",
        "script": _case_update_item_metadata,
        "trace_tools": ["search_items"],
        "plan_tools": ["update_item"],
    },
    {
        "name": "rule-inspection",
        "command": "What rules do you know about jewelry?",
        "script": _case_rule_inspection,
        "trace_tools": ["list_rules"],
        "plan_tools": [],
    },
    {
        "name": "label-sheet",
        "command": "Make QR labels for the main cabinet.",
        "script": _case_label_sheet,
        "trace_tools": ["preview_label_sheet"],
        "plan_tools": [],
    },
    {
        "name": "photo-sort",
        "command": "Sort this photo into separate bins.",
        "script": _case_photo_sort,
        "trace_tools": [],
        "plan_tools": ["put_item", "put_item", "put_item"],
        "images": ["data:image/jpeg;base64,AAA"],
    },
]


def test_slugify() -> None:
    assert slugify("Blue Pouch") == "blue-pouch"
    assert slugify("  M3 / M4 Bag! ") == "m3-m4-bag"


def test_plan_reorganization_returns_move_brief(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    allen_id = store.upsert_item(name="Allen keys")
    wrench_id = store.upsert_item(name="metric wrenches")
    store.put_inventory(item_id=allen_id, bin_id="B041", quantity=1, unit="set")
    store.put_inventory(item_id=wrench_id, bin_id="B042", quantity=1, unit="set")

    brief = _run_read_tool(
        "plan_reorganization",
        {
            "queries": ["Allen keys", "wrenches"],
            "destination_label": "Large Hand Tool Bin",
            "parent_id": "main-cabinet",
            "capacity_class": "large",
        },
        store=store,
        locations=locations,
    )

    assert brief["destination_id"] == "large-hand-tool-bin"
    assert brief["destination_exists"] is False
    assert brief["move_count"] == 2
    assert [step["tool"] for step in brief["proposed_steps"]] == [
        "create_location",
        "move_item",
        "move_item",
    ]
    assert {match["item_id"] for match in brief["matches"]} == {allen_id, wrench_id}
    assert brief["proposed_steps"][1]["arguments"]["to_location_id"] == "large-hand-tool-bin"


def test_plan_reorganization_infers_the_matching_non_cabinet_root(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    locations.upsert(
        Location(id="printer-cabinet", type="cabinet", label="Printer Cabinet")
    )
    locations.upsert(
        Location(
            id="printer-tools",
            type="drawer",
            label="Printer Tools",
            parent_id="printer-cabinet",
        )
    )
    item_id = store.upsert_item(name="nozzle wrench")
    store.put_inventory(
        item_id=item_id,
        bin_id="printer-tools",
        quantity=1,
        unit="each",
    )

    brief = _run_read_tool(
        "plan_reorganization",
        {
            "queries": ["nozzle wrenches"],
            "destination_label": "Large Printer Tool Bin",
            "capacity_class": "large",
        },
        store=store,
        locations=locations,
    )

    assert brief["parent_id"] == "printer-cabinet"
    assert brief["parent_source"] == "matching_items"
    assert brief["matching_root_ids"] == ["printer-cabinet"]
    assert brief["needs_parent_selection"] is False
    assert brief["proposed_steps"][0]["arguments"]["parent_id"] == (
        "printer-cabinet"
    )
    assert brief["proposed_steps"][1]["arguments"]["item_id"] == item_id


def test_plan_reorganization_requires_a_root_when_matches_span_storage_spaces(
    tmp_path,
) -> None:
    store, locations = _stores(tmp_path)
    locations.upsert(
        Location(id="printer-cabinet", type="cabinet", label="Printer Cabinet")
    )
    locations.upsert(
        Location(
            id="printer-tools",
            type="drawer",
            label="Printer Tools",
            parent_id="printer-cabinet",
        )
    )
    allen_id = store.upsert_item(name="Allen keys")
    wrench_id = store.upsert_item(name="nozzle wrench")
    store.put_inventory(item_id=allen_id, bin_id="B041", quantity=1, unit="set")
    store.put_inventory(
        item_id=wrench_id,
        bin_id="printer-tools",
        quantity=1,
        unit="each",
    )

    brief = _run_read_tool(
        "plan_reorganization",
        {
            "queries": ["Allen keys", "nozzle wrenches"],
            "destination_label": "Large Tool Bin",
            "capacity_class": "large",
        },
        store=store,
        locations=locations,
    )

    assert brief["needs_parent_selection"] is True
    assert brief["needs_confirmation"] is False
    assert brief["matching_root_ids"] == ["main-cabinet", "printer-cabinet"]
    assert {choice["id"] for choice in brief["parent_choices"]} == {
        "main-cabinet",
        "printer-cabinet",
    }
    assert {choice["selection_command"] for choice in brief["parent_choices"]} == {
        "Use Main Cabinet for the new Large Tool Bin",
        "Use Printer Cabinet for the new Large Tool Bin",
    }
    assert {match["item_id"] for match in brief["matches"]} == {
        allen_id,
        wrench_id,
    }
    assert brief["proposed_steps"] == []
    assert brief["move_count"] == 0


def test_assistant_api_does_not_offer_confirmation_before_cross_root_choice(
    tmp_path,
) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps({"name": "software-only", "columns": 1, "rows_bottom_to_top": []}),
        encoding="utf-8",
    )
    scripted = Scripted(
        [
            _tool_calls(
                [
                    (
                        "plan_reorganization",
                        {
                            "queries": ["Allen keys", "nozzle wrenches"],
                            "destination_label": "Large Tool Bin",
                            "capacity_class": "large",
                        },
                    )
                ]
            ),
            _text("Should the new tool bin go in the Main Cabinet or Printer Cabinet?"),
        ]
    )
    client = TestClient(
        create_app(
            LedConfig(
                pixel_count=0,
                backend="none",
                layout_path=str(layout_path),
                database_path=str(tmp_path / "inventory.db"),
            ),
            llm=_client(scripted),
        )
    )
    for location in (
        {"id": "main-cabinet", "type": "cabinet", "label": "Main Cabinet"},
        {
            "id": "main-tools",
            "type": "drawer",
            "label": "Main Tools",
            "parent_id": "main-cabinet",
        },
        {
            "id": "printer-cabinet",
            "type": "cabinet",
            "label": "Printer Cabinet",
        },
        {
            "id": "printer-tools",
            "type": "drawer",
            "label": "Printer Tools",
            "parent_id": "printer-cabinet",
        },
    ):
        assert client.post("/locations", json=location).status_code == 200
    assert client.post(
        "/inventory",
        json={"name": "Allen keys", "bin_id": "main-tools"},
    ).status_code == 200
    assert client.post(
        "/inventory",
        json={"name": "nozzle wrench", "bin_id": "printer-tools"},
    ).status_code == 200

    response = client.post(
        "/assistant/chat",
        json={"text": "Move Allen keys and nozzle wrenches into a large tool bin"},
    ).json()

    assert response["action"] == "answered"
    assert response["plan"] == []
    assert "Main Cabinet or Printer Cabinet" in response["message"]
    assert response["trace"][0]["result"]["needs_parent_selection"] is True
    assert {choice["id"] for choice in response["location_candidates"]} == {
        "main-cabinet",
        "printer-cabinet",
    }
    assert all(
        choice["selection_command"] for choice in response["location_candidates"]
    )
    latest = client.get("/assistant/interactions?limit=1").json()["interactions"][0]
    assert latest["status"] == ""
    assert (
        client.get("/kiosk/state?wait_seconds=0").json()["pending_confirmation"]
        is None
    )


def test_agent_adopts_reorganization_proposed_steps_when_model_stops_at_prose(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    allen_id = store.upsert_item(name="Allen keys")
    wrench_id = store.upsert_item(name="metric wrenches")
    store.put_inventory(item_id=allen_id, bin_id="B041", quantity=1, unit="set")
    store.put_inventory(item_id=wrench_id, bin_id="B042", quantity=1, unit="set")
    scripted = Scripted(
        [
            _tool_calls(
                [
                    (
                        "plan_reorganization",
                        {
                            "queries": ["Allen keys", "wrenches"],
                            "destination_label": "Large Hand Tool Bin",
                            "parent_id": "main-cabinet",
                            "capacity_class": "large",
                        },
                    ),
                ]
            ),
            _text("I found two tool groups to consolidate."),
        ]
    )

    result = run_agent(
        "Move all Allen keys and wrenches to a big bin.",
        store=store,
        locations=locations,
        client=_client(scripted),
    )

    assert result.message == "I found two tool groups to consolidate."
    assert [step["tool"] for step in result.plan] == [
        "create_location",
        "move_item",
        "move_item",
    ]
    assert result.plan[1]["arguments"]["item_id"] == allen_id
    assert result.plan[2]["arguments"]["item_id"] == wrench_id
    assert result.trace[0]["tool"] == "plan_reorganization"


def test_agent_aggregates_usage_across_tool_rounds(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    scripted = Scripted(
        [
            _tool_calls([("search_items", {"query": "M4 T-nuts"})])
            | {
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "total_tokens": 120,
                }
            },
            _text("I did not find M4 T-nuts.")
            | {
                "usage": {
                    "prompt_tokens": 140,
                    "prompt_tokens_details": {"cached_tokens": 90},
                    "completion_tokens": 15,
                    "total_tokens": 155,
                }
            },
        ]
    )

    result = run_agent(
        "Find M4 T-nuts",
        store=store,
        locations=locations,
        client=_client(scripted),
    )

    assert result.usage == {
        "requests": 2,
        "input_tokens": 240,
        "cached_input_tokens": 90,
        "output_tokens": 35,
        "reasoning_tokens": 0,
        "total_tokens": 275,
    }


def test_agent_does_not_adopt_empty_reorganization_destination(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    scripted = Scripted(
        [
            _tool_calls(
                [
                    (
                        "plan_reorganization",
                        {
                            "queries": ["left-handed sky hooks"],
                            "destination_label": "Mystery Bin",
                            "parent_id": "main-cabinet",
                            "capacity_class": "large",
                        },
                    ),
                ]
            ),
            _text("I did not find anything matching that request."),
        ]
    )

    result = run_agent(
        "Move all left-handed sky hooks to a big bin.",
        store=store,
        locations=locations,
        client=_client(scripted),
    )

    assert result.plan == []
    assert result.trace[0]["result"]["move_count"] == 0


def test_reorganization_finds_singular_family_and_avoids_location_id_collision(
    tmp_path,
) -> None:
    store, locations = _stores(tmp_path)
    locations.upsert(
        Location(
            id="printercab",
            type="cabinet",
            label="PrinterCab",
            capacity_class="large",
        )
    )
    locations.upsert(
        Location(
            id="printercab-middle",
            type="drawer",
            label="Middle Drawer",
            parent_id="printercab",
            capacity_class="large",
        )
    )
    locations.upsert(
        Location(
            id="allen-keys-and-wrenches-bin",
            type="bin",
            label="Allen Keys and Wrenches Bin",
            parent_id="main-cabinet",
            capacity_class="medium",
        )
    )
    item_id = store.upsert_item(name="Allen Wrench")
    store.put_inventory(item_id=item_id, bin_id="B114", quantity=1, unit="each")

    result = _run_read_tool(
        "plan_reorganization",
        {
            "queries": ["Allen keys", "wrenches"],
            "destination_id": "allen-keys-and-wrenches-bin",
            "destination_label": "Allen Keys and Wrenches Bin",
            "parent_id": "printercab-middle",
            "capacity_class": "large",
        },
        store=store,
        locations=locations,
    )

    assert result["move_count"] == 1
    assert result["matches"][0]["item"] == "Allen Wrench"
    assert result["matches"][0]["matched_variant"] == "allen"
    assert result["destination_id"] == (
        "printercab-middle-allen-keys-and-wrenches-bin"
    )
    assert result["proposed_steps"][0]["arguments"]["parent_id"] == (
        "printercab-middle"
    )
    assert result["proposed_steps"][1]["arguments"]["to_location_id"] == (
        "printercab-middle-allen-keys-and-wrenches-bin"
    )


def test_agent_receives_recent_conversation_for_pronouns_and_corrections(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    scripted = Scripted([_text("I can move it to a larger bin.")])

    run_agent(
        "No, use a bigger bin.",
        store=store,
        locations=locations,
        client=_client(scripted),
        conversation_history=[
            {
                "route": "/assistant/chat",
                "command": "Put away the blue pry tool.",
                "status": "confirmed",
                "response": {
                    "message": "Put the blue pry tool in B010.",
                    "action": "proposed",
                    "plan": [
                        {
                            "tool": "put_item",
                            "arguments": {
                                "item": "blue pry tool",
                                "location_id": "B010",
                            },
                        }
                    ],
                },
            }
        ],
    )

    messages = scripted.bodies[0]["messages"]
    history = next(
        message["content"]
        for message in messages
        if "Recent Magpie conversation" in str(message.get("content"))
    )
    assert "Put away the blue pry tool" in history
    assert '"location_id":"B010"' in history


def test_agent_does_not_turn_destination_words_into_pending_item(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    scripted = Scripted(
        [
            _tool_calls(
                [
                    (
                        "put_item",
                        {
                            "item": "Small Bin",
                            "location_id": "B052",
                            "quantity": 1,
                        },
                    )
                ]
            ),
            _tool_calls(
                [
                    (
                        "put_item",
                        {
                            "item": "Razor Travel Case",
                            "location_id": "B052",
                            "quantity": 1,
                        },
                    )
                ]
            ),
        ]
    )
    history = [
        {
            "route": "/assistant/chat",
            "command": "Put away a Razor Travel Case.",
            "status": "pending",
            "response": {
                "message": "Put Razor Travel Case in the bottom drawer.",
                "action": "proposed",
                "plan": [
                    {
                        "tool": "put_item",
                        "arguments": {
                            "item": "Razor Travel Case",
                            "location_id": "printercab-c1-r3",
                        },
                    }
                ],
            },
        }
    ]

    result = run_agent(
        "No, move it to a small bin in the main cabinet.",
        store=store,
        locations=locations,
        client=_client(scripted),
        conversation_history=history,
    )

    assert result.plan[0]["arguments"]["item"] == "Razor Travel Case"
    assert result.plan[0]["arguments"]["location_id"] == "B052"
    guard = next(entry for entry in result.trace if entry["tool"] == "plan_guard")
    assert guard["result"]["accepted"] is False
    assert "do not replace it with Small Bin" in guard["result"]["reason"]


def test_agent_prompt_handles_pending_plan_revisions() -> None:
    assert "steward of the whole storage space" in AGENT_INSTRUCTIONS
    assert "Propose moves, consolidation, clearer homes" in AGENT_INSTRUCTIONS
    assert "answer with the best matching item's path labels" in AGENT_INSTRUCTIONS
    assert "nearest_lit_location_id" in AGENT_INSTRUCTIONS
    assert "call move_item" in AGENT_INSTRUCTIONS
    assert "propose remove_item" in AGENT_INSTRUCTIONS
    assert "propose attach_photo" in AGENT_INSTRUCTIONS
    assert "move all Allen keys and wrenches" in AGENT_INSTRUCTIONS
    assert "call plan_reorganization" in AGENT_INSTRUCTIONS
    assert "propose update_location" in AGENT_INSTRUCTIONS
    assert "Convert inches to millimeters" in AGENT_INSTRUCTIONS
    assert "Do not merge distinct hardware" in AGENT_INSTRUCTIONS
    assert "ask one short clarifying question" in AGENT_INSTRUCTIONS
    assert "Current proposed plan JSON" in AGENT_INSTRUCTIONS
    assert "return no write tools when the plan should not change" in AGENT_INSTRUCTIONS
    assert "return the full revised plan" in AGENT_INSTRUCTIONS.lower()
    assert "do not treat the follow-up text itself as a new inventory item" in AGENT_INSTRUCTIONS
    assert "Never replace a move_item or put_item step with remove_item" in AGENT_INSTRUCTIONS
    assert "do not propose put_item again" in AGENT_INSTRUCTIONS
    assert "A put-away command is the explicit add-more case" in AGENT_INSTRUCTIONS
    assert "Default an unspecified put-away quantity to 1 each" in AGENT_INSTRUCTIONS
    assert "Do not ask how many" in AGENT_INSTRUCTIONS
    assert "plain text with no Markdown" in AGENT_INSTRUCTIONS
    assert "Do not narrate placement reasoning unless" in AGENT_INSTRUCTIONS
    assert "do not shuffle it to an equivalent location" in AGENT_INSTRUCTIONS
    assert "sort, catalog, split, or file things separately" in AGENT_INSTRUCTIONS
    assert "one put_item call per group" in AGENT_INSTRUCTIONS
    assert "needs_review=true" in AGENT_INSTRUCTIONS
    assert "propose learn_rule" in AGENT_INSTRUCTIONS
    assert "propose learn_size_rule" in AGENT_INSTRUCTIONS
    assert "update_rule" in AGENT_INSTRUCTIONS
    assert "update_size_rule" in AGENT_INSTRUCTIONS
    assert "propose update_item" in AGENT_INSTRUCTIONS
    assert "propose update_item_size" in AGENT_INSTRUCTIONS
    assert "propose update_item_home" in AGENT_INSTRUCTIONS
    assert "propose print_label" in AGENT_INSTRUCTIONS
    assert "call get_printer_status first" in AGENT_INSTRUCTIONS
    assert "call preview_label_sheet" in AGENT_INSTRUCTIONS
    assert "call preview_label_batch" in AGENT_INSTRUCTIONS
    assert "preview_label_sheet and preview_label_batch are" in AGENT_INSTRUCTIONS
    assert "call list_rules" in AGENT_INSTRUCTIONS
    assert "call get_location" in AGENT_INSTRUCTIONS
    assert "call explain_location_choice" in AGENT_INSTRUCTIONS
    assert "propose forget_rule" in AGENT_INSTRUCTIONS


def test_agent_location_tools_support_every_storage_location_type() -> None:
    schemas = {
        schema["function"]["name"]: schema["function"]["parameters"]
        for schema in TOOL_SCHEMAS
    }

    for tool in ("create_location", "update_location"):
        assert schemas[tool]["properties"]["type"]["enum"] == list(LOCATION_TYPES)


def test_agent_item_tools_accept_dimensions_for_fit_and_persistence() -> None:
    schemas = {
        schema["function"]["name"]: schema["function"]["parameters"]
        for schema in TOOL_SCHEMAS
    }

    assert "dimensions" in schemas["suggest_locations"]["properties"]
    assert "dimensions" in schemas["explain_location_choice"]["properties"]
    assert "dimensions" in schemas["put_item"]["properties"]
    assert "size_class" in schemas["put_item"]["properties"]
    assert "physical dimensions for an item" in AGENT_INSTRUCTIONS


def test_suggest_locations_uses_agent_supplied_item_dimensions(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    locations.upsert(
        Location(
            id="small-case-bin",
            type="bin",
            label="Small Case Bin",
            capacity_class="small",
            internal_dimensions={"width_mm": 120, "depth_mm": 100, "height_mm": 50},
        )
    )
    locations.upsert(
        Location(
            id="large-case-bin",
            type="bin",
            label="Large Case Bin",
            capacity_class="large",
            internal_dimensions={"width_mm": 400, "depth_mm": 300, "height_mm": 200},
        )
    )

    suggestions = run_read_tool(
        "suggest_locations",
        {
            "item": "Camera case",
            "dimensions": {
                "length_mm": 304.8,
                "width_mm": 203.2,
                "height_mm": 76.2,
            },
        },
        store=store,
        locations=locations,
    )

    assert suggestions[0]["location_id"] == "large-case-bin"
    assert "small-case-bin" not in {row["location_id"] for row in suggestions}


def test_agent_refuses_print_plan_when_printer_is_unavailable(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    scripted = Scripted(
        [
            _tool_calls([("print_label", {"location_id": "B001"})]),
            _tool_calls([("get_printer_status", {})]),
            _text("The physical label printer is unavailable."),
        ]
    )

    result = run_agent(
        "Print a label for B001.",
        store=store,
        locations=locations,
        client=_client(scripted),
        read_tool_runner=lambda name, args: {
            "ok": False,
            "error": "printer timed out",
        },
    )

    assert result.plan == []
    assert result.message == "The physical label printer is unavailable."
    guard = next(entry for entry in result.trace if entry["tool"] == "plan_guard")
    assert "check get_printer_status" in guard["result"]["reason"]
    status = next(
        entry for entry in result.trace if entry["tool"] == "get_printer_status"
    )
    assert status["result"]["ok"] is False


def test_agent_rejects_accidental_remove_and_replans_as_move(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    item_id = store.upsert_item(name="Allen Wrench")
    store.put_inventory(item_id=item_id, bin_id="B041", quantity=1, unit="each")
    scripted = Scripted(
        [
            _tool_calls(
                [
                    (
                        "remove_item",
                        {
                            "item_id": item_id,
                            "item": "Allen Wrench",
                            "location_id": "B114",
                        },
                    )
                ]
            ),
            _tool_calls(
                [
                    (
                        "move_item",
                        {
                            "item_id": item_id,
                            "item": "Allen Wrench",
                            "from_location_id": "B041",
                            "to_location_id": "B114",
                        },
                    )
                ]
            ),
        ]
    )
    command = (
        'Current proposed plan JSON: [{"tool":"move_item","arguments":'
        f'{{"item_id":{item_id},"from_location_id":"B041",'
        '"to_location_id":"B114"}}}]\n\n'
        "User follow-up: use the medium bin B114"
    )

    result = run_agent(
        command,
        store=store,
        locations=locations,
        client=_client(scripted),
    )

    assert [step["tool"] for step in result.plan] == ["move_item"]
    assert result.plan[0]["arguments"]["to_location_id"] == "B114"
    guard = next(entry for entry in result.trace if entry["tool"] == "plan_guard")
    assert guard["result"]["accepted"] is False
    assert "explicit user request" in guard["result"]["reason"]
    second_messages = scripted.bodies[1]["messages"]
    assert any(
        message.get("role") == "tool" and "non-destructive tool" in message.get("content", "")
        for message in second_messages
    )


def test_agent_never_returns_unrequested_remove_plan(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    item_id = store.upsert_item(name="Allen Wrench")
    store.put_inventory(item_id=item_id, bin_id="B041", quantity=1, unit="each")
    scripted = Scripted(
        [
            _tool_calls(
                [
                    (
                        "remove_item",
                        {
                            "item_id": item_id,
                            "item": "Allen Wrench",
                            "location_id": "B114",
                        },
                    )
                ]
            )
        ]
    )

    result = run_agent(
        "Move the Allen wrench to B114.",
        store=store,
        locations=locations,
        client=_client(scripted),
        max_steps=1,
    )

    assert result.plan == []
    assert result.trace[-1]["tool"] == "plan_guard"
    assert result.trace[-1]["result"]["accepted"] is False


def test_agent_rejects_duplicate_put_in_same_location(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    locations.upsert(
        Location(
            id="roaster-parts-bin",
            type="bin",
            label="Roaster Parts Bin",
            parent_id="main-cabinet",
            capacity_class="large",
        )
    )
    item_id = store.upsert_item(name="spare fans")
    store.put_inventory(
        item_id=item_id,
        bin_id="roaster-parts-bin",
        quantity=1,
        unit="each",
    )
    scripted = Scripted(
        [
            _tool_calls(
                [
                    (
                        "put_item",
                        {
                            "item": "spare fans",
                            "location_id": "roaster-parts-bin",
                            "quantity": 1,
                        },
                    )
                ]
            ),
            _text("Spare fans are already stored in Roaster Parts Bin."),
        ]
    )

    result = run_agent(
        "The Roaster Parts Bin contains spare fans.",
        store=store,
        locations=locations,
        client=_client(scripted),
    )

    assert result.plan == []
    assert result.trace[-1]["tool"] == "plan_guard"
    assert "do not create a duplicate" in result.trace[-1]["result"]["reason"]


def test_agent_allows_explicit_additional_quantity_in_same_location(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    item_id = store.upsert_item(name="spare fans")
    store.put_inventory(item_id=item_id, bin_id="B001", quantity=1, unit="each")
    scripted = Scripted(
        [
            _tool_calls(
                [
                    (
                        "put_item",
                        {
                            "item": "spare fans",
                            "location_id": "B001",
                            "quantity": 1,
                        },
                    )
                ]
            ),
            _tool_calls(
                [
                    (
                        "put_item",
                        {
                            "item": "spare fans",
                            "location_id": "B001",
                            "quantity": 1,
                            "quantity_mode": "add",
                        },
                    )
                ]
            ),
        ]
    )

    result = run_agent(
        "Add another spare fan to B001.",
        store=store,
        locations=locations,
        client=_client(scripted),
    )

    assert [step["tool"] for step in result.plan] == ["put_item"]
    assert result.plan[0]["arguments"]["quantity_mode"] == "add"
    assert result.trace[-1]["tool"] == "plan_guard"
    assert "existing quantity is not overwritten" in result.trace[-1]["result"]["reason"]


def test_agent_allows_putaway_add_mode_in_existing_home(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    item_id = store.upsert_item(name="M3 flush mount screws")
    store.put_inventory(item_id=item_id, bin_id="B001", quantity=1, unit="each")
    scripted = Scripted(
        [
            _tool_calls(
                [
                    (
                        "put_item",
                        {
                            "item": "M3 flush mount screws",
                            "location_id": "B001",
                            "quantity": 1,
                            "quantity_mode": "add",
                        },
                    )
                ]
            )
        ]
    )

    result = run_agent(
        "Put away M3 flush mount screws.",
        store=store,
        locations=locations,
        client=_client(scripted),
    )

    assert result.plan[0]["arguments"]["quantity_mode"] == "add"
    assert all(entry["tool"] != "plan_guard" for entry in result.trace)


def test_agent_rejects_move_between_equivalent_generic_bins(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    item_id = store.upsert_item(name="T8x8 Lead Screw Nut")
    store.put_inventory(item_id=item_id, bin_id="B010", quantity=2, unit="each")
    scripted = Scripted(
        [
            _tool_calls(
                [
                    (
                        "move_item",
                        {
                            "item_id": item_id,
                            "item": "T8x8 Lead Screw Nut",
                            "from_location_id": "B010",
                            "to_location_id": "B051",
                        },
                    )
                ]
            ),
            _text("It is already in a small bin in the main cabinet."),
        ]
    )

    result = run_agent(
        "Move the T8x8 lead screw nuts into a main cabinet small bin.",
        store=store,
        locations=locations,
        client=_client(scripted),
    )

    assert result.plan == []
    assert result.trace[-1]["tool"] == "plan_guard"
    assert "equivalent location" in result.trace[-1]["result"]["reason"]


def test_feature_complete_agent_eval_smoke_suite(tmp_path) -> None:
    for index, case in enumerate(FEATURE_COMPLETE_EVALS):
        store, locations = _stores(tmp_path / str(index))
        scripted = case["script"](store, locations)

        result = run_agent(
            case["command"],
            store=store,
            locations=locations,
            client=_client(scripted),
            images=case.get("images"),
        )

        assert [entry["tool"] for entry in result.trace] == case["trace_tools"], case["name"]
        assert [step["tool"] for step in result.plan] == case["plan_tools"], case["name"]
        if case["plan_tools"]:
            # Write actions remain proposals until explicit confirmation.
            assert not any(
                step["tool"] in {"put_item", "move_item", "create_location"}
                for step in result.trace
            ), case["name"]


def test_agent_can_propose_location_metadata_update(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    locations.upsert(
        Location(
            id="solder-pouch",
            type="pouch",
            label="Solder Pouch",
            parent_id="B001",
            capacity_class="small",
        )
    )
    scripted = Scripted(
        [
            _tool_calls(
                [
                    (
                        "update_location",
                        {
                            "location_id": "solder-pouch",
                            "label": "Solder Kit",
                            "capacity_class": "medium",
                            "purpose_tags": ["solder", "electronics"],
                            "placement_notes": "Solder wick, flux, tips, and small soldering tools.",
                        },
                    )
                ]
            )
        ]
    )

    result = run_agent(
        "Rename the solder pouch to Solder Kit and use it for soldering tools.",
        store=store,
        locations=locations,
        client=_client(scripted),
    )

    assert result.plan == [
        {
            "tool": "update_location",
            "arguments": {
                "location_id": "solder-pouch",
                "label": "Solder Kit",
                "capacity_class": "medium",
                "purpose_tags": ["solder", "electronics"],
                "placement_notes": "Solder wick, flux, tips, and small soldering tools.",
            },
        }
    ]
    assert locations.get("solder-pouch").label == "Solder Pouch"


def test_agent_replans_when_putaway_ignores_explicit_location(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    item_id = store.upsert_item(name="USB adapter")
    store.put_inventory(item_id=item_id, bin_id="B050", quantity=1, unit="each")
    scripted = Scripted(
        [
            _tool_calls([("search_items", {"query": "USB adapter"})]),
            _tool_calls(
                [
                    (
                        "put_item",
                        {
                            "item": "USB adapter",
                            "location_id": "B050",
                            "quantity": 1,
                            "quantity_mode": "add",
                        },
                    )
                ]
            ),
            _tool_calls(
                [
                    (
                        "put_item",
                            {
                                "item": "USB adapter",
                                "location_id": "B034",
                                "quantity": 1,
                                "quantity_mode": "add",
                            },
                    )
                ]
            ),
        ]
    )

    result = run_agent(
        "I put another USB adapter in B034",
        store=store,
        locations=locations,
        client=_client(scripted),
    )

    assert result.plan[0]["arguments"]["location_id"] == "B034"
    assert result.trace[-1]["tool"] == "plan_guard"
    assert "explicitly requested B034" in result.trace[-1]["result"]["reason"]


def test_revision_guard_uses_embedded_plan_instead_of_stale_history(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    scripted = Scripted(
        [
            _tool_calls(
                [
                    (
                        "move_item",
                        {
                            "item": "Allen Wrench",
                            "item_id": 72,
                            "from_location_id": "B041",
                            "to_location_id": "B115",
                        },
                    )
                ]
            )
        ]
    )
    history = [
        {
            "status": "pending",
            "response": {
                "plan": [
                    {
                        "tool": "put_item",
                        "arguments": {
                            "item": "Magpie Acceptance Washer 20260715",
                            "location_id": "B113",
                        },
                    }
                ]
            },
        }
    ]
    prompt = """Revise the pending storage plan.

Current proposed plan JSON:
[{"tool":"move_item","arguments":{"item":"Allen Wrench","item_id":72,"from_location_id":"B041","to_location_id":"allen-keys-and-wrenches-bin"}}]

User follow-up:
the allen keys and wrenches bin should be a medium one B113 - B124

If it asks a question, answer briefly."""

    result = run_agent(
        prompt,
        store=store,
        locations=locations,
        client=_client(scripted),
        conversation_history=history,
    )

    assert result.plan[0]["arguments"]["item"] == "Allen Wrench"
    assert result.plan[0]["arguments"]["to_location_id"] == "B115"
    assert all(entry["tool"] != "plan_guard" for entry in result.trace)


def test_agent_can_propose_photo_attachment(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    photo = store.add_photo(filename="part.jpg", content_type="image/jpeg", data=b"photo")
    item_id = store.upsert_item(name="solder wick")
    store.put_inventory(item_id=item_id, bin_id="B001", quantity=1, unit="roll")
    scripted = Scripted(
        [
            _tool_calls(
                [
                    (
                        "attach_photo",
                        {
                            "photo_id": photo.id,
                            "object_type": "item",
                            "object_id": "solder wick",
                        },
                    )
                ]
            )
        ]
    )

    result = run_agent(
        "attach this photo to solder wick",
        store=store,
        locations=locations,
        client=_client(scripted),
    )

    assert result.plan == [
        {
            "tool": "attach_photo",
            "arguments": {
                "photo_id": photo.id,
                "object_type": "item",
                "object_id": "solder wick",
            },
        }
    ]
    assert store.photo(photo.id).item_id is None


def test_agent_runs_read_tool_then_answers(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    item_id = store.upsert_item(name="M3 hex nuts")
    store.put_inventory(item_id=item_id, bin_id="1", quantity=50, unit="each")

    scripted = Scripted(
        [
            _tool_calls([("search_items", {"query": "hex nuts"})]),
            _text("They're in B001."),
        ]
    )
    result = run_agent(
        "where are my hex nuts?", store=store, locations=locations, client=_client(scripted)
    )

    assert result.message == "They're in B001."
    assert result.plan == []
    assert result.trace[0]["tool"] == "search_items"
    assert result.trace[0]["result"][0]["item_id"] == item_id
    assert result.trace[0]["result"][0]["location_id"] == "B001"
    # The read result was fed back to the model on the second turn.
    assert any(m.get("role") == "tool" for m in scripted.bodies[1]["messages"])


def test_agent_lists_all_top_level_storage_spaces(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    locations.upsert(Location(id="attic", type="room", label="Attic"))
    locations.upsert(Location(id="printercab", type="cabinet", label="PrinterCab"))
    locations.upsert(
        Location(
            id="printer-bottom",
            type="drawer",
            label="Bottom Drawer",
            parent_id="printercab",
        )
    )
    item_id = store.upsert_item(name="Dremel")
    store.put_inventory(
        item_id=item_id,
        bin_id="printer-bottom",
        quantity=1,
        unit="each",
        normalize_bin=False,
    )
    scripted = Scripted(
        [
            _tool_calls([("list_storage_roots", {})]),
            _text("Main Cabinet, Attic, and PrinterCab."),
        ]
    )

    result = run_agent(
        "What storage spaces do I have?",
        store=store,
        locations=locations,
        client=_client(scripted),
    )

    roots = result.trace[0]["result"]
    assert {root["id"] for root in roots} >= {"main-cabinet", "attic", "printercab"}
    printercab = next(root for root in roots if root["id"] == "printercab")
    assert printercab["location_count"] == 2
    assert printercab["item_count"] == 1
    assert printercab["occupied_location_count"] == 1


def test_agent_executes_immediate_light_tool_through_shared_runner(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    scripted = Scripted(
        [
            _tool_calls(
                [("light_location", {"location_id": "B047", "color": "blue"})]
            ),
            _text("B047 is lit blue."),
        ]
    )
    calls = []

    result = run_agent(
        "show me B47 in blue",
        store=store,
        locations=locations,
        client=_client(scripted),
        read_tool_runner=lambda name, args: calls.append((name, args))
        or {
            "ok": True,
            "lit": True,
            "location_id": "B047",
            "lit_location": "B047",
            "color": (40, 120, 255),
        },
    )

    assert result.plan == []
    assert result.message == "B047 is lit blue."
    assert result.trace[0]["tool"] == "light_location"
    assert calls == [("light_location", {"location_id": "B047", "color": "blue"})]


def test_agent_recovers_explicit_light_after_model_only_inspects_location(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    locations.upsert(
        Location(
            id="B047",
            type="bin",
            label="B047",
            parent_id="main-cabinet",
            light_indexes=[47],
        )
    )
    scripted = Scripted(
        [
            _tool_calls([("get_location", {"location_id": "B047"})]),
            _text("B047 has a light, but I cannot activate it from here."),
        ]
    )
    calls = []

    def run_tool(name, args):
        calls.append((name, args))
        if name == "get_location":
            return {
                "found": True,
                "location": {"id": "B047", "label": "B047", "is_lit": True},
            }
        return {
            "ok": True,
            "lit": True,
            "location_id": "B047",
            "lit_location": "B047",
            "color": (255, 255, 255),
        }

    result = run_agent(
        "can you light B47?",
        store=store,
        locations=locations,
        client=_client(scripted),
        read_tool_runner=run_tool,
    )

    assert result.message == "Lit B047."
    assert [entry["tool"] for entry in result.trace] == ["get_location", "light_location"]
    assert calls == [
        ("get_location", {"location_id": "B047"}),
        ("light_location", {"location_id": "B047"}),
    ]


def test_agent_search_items_returns_physical_path_and_nearest_light(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    locations.upsert(
        Location(
            id="solder-pouch",
            type="pouch",
            label="Solder Pouch",
            parent_id="B001",
        )
    )
    item_id = store.upsert_item(name="solder wick")
    store.put_inventory(
        item_id=item_id,
        bin_id="solder-pouch",
        quantity=1,
        unit="roll",
        normalize_bin=False,
    )

    results = _run_read_tool(
        "search_items",
        {"query": "solder wick"},
        store=store,
        locations=locations,
    )

    assert results[0]["item_id"] == item_id
    assert results[0]["location_id"] == "solder-pouch"
    assert [node["id"] for node in results[0]["path"]] == [
        "main-cabinet",
        "B001",
        "solder-pouch",
    ]
    assert results[0]["nearest_lit_location_id"] == "B001"


def test_agent_lists_learned_rules_before_answering(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    store.learn_rule(
        rule_text="Jewelry should go in the main cabinet.",
        trigger_terms=["jewelry", "bracelet"],
        target_location_id="main-cabinet",
    )
    store.learn_rule(
        rule_text="Jewelry repair parts should go in B010.",
        trigger_terms=["jewelry", "repair"],
        target_location_id="B010",
    )
    store.learn_size_rule(
        rule_text="Jewelry boxes should be medium.",
        trigger_terms=["jewelry", "box"],
        size_class="medium",
    )
    scripted = Scripted(
        [
            _tool_calls([("list_rules", {"query": "jewelry"})]),
            _text("You have a jewelry placement rule and a jewelry size rule."),
        ]
    )

    result = run_agent(
        "what rules do you know about jewelry?",
        store=store,
        locations=locations,
        client=_client(scripted),
    )

    assert result.message == "You have a jewelry placement rule and a jewelry size rule."
    listed = result.trace[0]["result"]
    assert listed["placement_rules"][0]["rule_type"] == "placement"
    assert [node["id"] for node in listed["placement_rules"][0]["target_path"]] == [
        "main-cabinet"
    ]
    assert listed["placement_rules"][0]["target_path_label"] == "Main Cabinet"
    assert listed["placement_rules"][0]["target_active"] is True
    assert listed["placement_rules"][0]["target_archived"] is False
    assert listed["size_rules"][0]["rule_type"] == "size"
    assert listed["conflicts"][0]["rule_type"] == "placement"
    assert listed["conflicts"][0]["target_path_labels"] == [
        "Main Cabinet",
        "Main Cabinet > B010",
    ]
    assert "jewelry" in listed["conflicts"][0]["shared_terms"]


def test_agent_marks_rules_whose_preserved_target_is_archived(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    locations.upsert(Location(id="attic", type="area", label="Attic"))
    store.learn_rule(
        rule_text="Holiday decorations belong in the attic.",
        trigger_terms=["holiday", "decorations"],
        target_location_id="attic",
    )
    locations.archive_subtree("attic")

    listed = _run_read_tool(
        "list_rules",
        {"query": "holiday decorations"},
        store=store,
        locations=locations,
    )

    rule = listed["placement_rules"][0]
    assert rule["target_path_label"] == "Attic"
    assert rule["target_active"] is False
    assert rule["target_archived"] is True


def test_agent_lists_and_proposes_approval_for_review_item(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    item_id = store.upsert_item(name="unknown sensor board")
    store.put_inventory(
        item_id=item_id,
        bin_id="B024",
        quantity=1,
        unit="each",
        needs_review=True,
        review_reason="Photo identity was uncertain.",
    )
    scripted = Scripted(
        [
            _tool_calls([("list_review_items", {})]),
            _tool_calls(
                [
                    (
                        "approve_review_item",
                        {"item_id": item_id, "location_id": "B024"},
                    )
                ]
            ),
        ]
    )

    result = run_agent(
        "Show me what needs review and approve the unknown sensor board.",
        store=store,
        locations=locations,
        client=_client(scripted),
    )

    assert result.trace[0]["tool"] == "list_review_items"
    assert result.trace[0]["result"]["items"][0]["name"] == "unknown sensor board"
    assert result.plan == [
        {
            "tool": "approve_review_item",
            "arguments": {"item_id": item_id, "location_id": "B024"},
        }
    ]
    assert store.review_queue()[0].item_id == item_id


def test_execute_plan_approves_review_and_records_learning_example(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    item_id = store.upsert_item(name="unknown sensor board")
    store.put_inventory(
        item_id=item_id,
        bin_id="B024",
        quantity=1,
        unit="each",
        needs_review=True,
        review_reason="Photo identity was uncertain.",
    )

    applied = execute_plan(
        [
            {
                "tool": "approve_review_item",
                "arguments": {"item_id": item_id, "location_id": "B024"},
            }
        ],
        store=store,
        locations=locations,
    )

    assert applied == [
        {
            "approved_review": item_id,
            "item": "unknown sensor board",
            "location_id": "B024",
            "previous_review_reason": "Photo identity was uncertain.",
        }
    ]
    assert store.review_queue() == []
    example = store.examples_for_eval(limit=1)[0]
    assert example["kind"] == "review_approved"
    assert "unknown sensor board" in example["command"]


def test_get_location_read_tool_returns_path_contents_and_light(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    locations.upsert(
        Location(
            id="solder-pouch",
            type="pouch",
            label="Solder Pouch",
            parent_id="B001",
            capacity_class="small",
            purpose_tags=["solder"],
            placement_notes="Solder wick and compact soldering consumables.",
        )
    )
    item_id = store.upsert_item(name="solder wick")
    store.put_inventory(
        item_id=item_id,
        bin_id="solder-pouch",
        quantity=1,
        unit="roll",
        normalize_bin=False,
    )

    detail = _run_read_tool(
        "get_location",
        {"location_id": "Solder Pouch"},
        store=store,
        locations=locations,
    )

    assert detail["found"] is True
    assert detail["location"]["id"] == "solder-pouch"
    assert detail["location"]["purpose_tags"] == ["solder"]
    assert [node["id"] for node in detail["path"]][-1] == "solder-pouch"
    assert detail["contents"][0]["name"] == "solder wick"
    assert detail["contents"][0]["bin_id"] == "solder-pouch"
    assert detail["nearest_lit_location_id"] == "B001"
    assert detail["subtree_count"] == 1


def test_get_location_read_tool_returns_paths_for_ambiguous_label(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    locations.upsert(Location(id="basement", type="room", label="Basement"))
    locations.upsert(Location(id="garage", type="room", label="Garage"))
    locations.upsert(
        Location(
            id="printer-cabinet",
            type="cabinet",
            label="Printer Cabinet",
            parent_id="basement",
        )
    )
    locations.upsert(
        Location(
            id="printer-cabinet-2",
            type="cabinet",
            label="Printer Cabinet",
            parent_id="garage",
        )
    )

    ambiguous = _run_read_tool(
        "get_location",
        {"location_id": "Printer Cabinet"},
        store=store,
        locations=locations,
    )
    qualified = _run_read_tool(
        "get_location",
        {"location_id": "Garage > Printer Cabinet"},
        store=store,
        locations=locations,
    )

    assert ambiguous["found"] is False
    assert "ambiguous location" in ambiguous["error"]
    assert {
        tuple(node["label"] for node in candidate["path"])
        for candidate in ambiguous["candidates"]
    } == {
        ("Basement", "Printer Cabinet"),
        ("Garage", "Printer Cabinet"),
    }
    assert qualified["found"] is True
    assert qualified["location"]["id"] == "printer-cabinet-2"


def test_agent_gets_location_before_answering_location_question(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    store.put_inventory(
        item_id=store.upsert_item(name="blue pry tool"),
        bin_id="B001",
        quantity=1,
        unit="each",
    )
    scripted = Scripted(
        [
            _tool_calls([("get_location", {"location_id": "B001"})]),
            _text("B001 contains the blue pry tool."),
        ]
    )

    result = run_agent(
        "what is in B001?",
        store=store,
        locations=locations,
        client=_client(scripted),
    )

    assert result.plan == []
    assert result.message == "B001 contains the blue pry tool."
    assert result.trace[0]["tool"] == "get_location"
    assert result.trace[0]["result"]["contents"][0]["name"] == "blue pry tool"
    assert any(m.get("role") == "tool" for m in scripted.bodies[1]["messages"])


def test_agent_previews_printable_label_sheet_without_confirm(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    scripted = Scripted(
        [
            _tool_calls([("preview_label_sheet", {"location_id": "main-cabinet"})]),
            _text("Open the main cabinet label sheet."),
        ]
    )

    result = run_agent(
        "make QR labels for the main cabinet",
        store=store,
        locations=locations,
        client=_client(scripted),
    )

    assert result.plan == []
    assert result.trace[0]["tool"] == "preview_label_sheet"
    assert result.trace[0]["result"]["found"] is True
    assert result.trace[0]["result"]["location_id"] == "main-cabinet"
    assert result.trace[0]["result"]["url"] == "/locations/main-cabinet/labels.html"
    assert result.trace[0]["result"]["label_count"] > 1


def test_agent_previews_explicit_label_batch_without_confirm(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    locations.upsert(
        Location(
            id="solder-pouch",
            type="pouch",
            label="Solder Pouch",
            parent_id="B001",
            capacity_class="small",
        )
    )
    item_id = store.upsert_item(name="solder wick")
    store.put_inventory(
        item_id=item_id,
        bin_id="solder-pouch",
        quantity=1,
        unit="roll",
        normalize_bin=False,
    )
    scripted = Scripted(
        [
            _tool_calls(
                [
                    (
                        "preview_label_batch",
                        {"location_ids": ["B001", "Solder Pouch", "missing-bin"]},
                    )
                ]
            ),
            _text("I found two labels to print and one missing location."),
        ]
    )

    result = run_agent(
        "make QR labels for B001 and the solder pouch",
        store=store,
        locations=locations,
        client=_client(scripted),
    )

    assert result.plan == []
    assert result.trace[0]["tool"] == "preview_label_batch"
    batch = result.trace[0]["result"]
    assert batch["label_count"] == 2
    assert [label["location_id"] for label in batch["labels"]] == ["B001", "solder-pouch"]
    assert batch["labels"][1]["item_count"] == 1
    assert batch["labels"][1]["pdf_url"] == "/locations/solder-pouch/labels.pdf"
    assert batch["missing_location_ids"] == ["missing-bin"]
    assert any(m.get("role") == "tool" for m in scripted.bodies[1]["messages"])


def test_agent_explains_location_choice_with_suggestion_reasons(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    loc = locations.get("B001")
    assert loc is not None
    locations.upsert(
        Location(
            id=loc.id,
            type=loc.type,
            parent_id=loc.parent_id,
            label=loc.label,
            grid=loc.grid,
            col=loc.col,
            row=loc.row,
            w=loc.w,
            h=loc.h,
            light_target=loc.light_target,
            light_indexes=loc.light_indexes,
            capacity_class="small",
            purpose_tags=["solder"],
            placement_notes=loc.placement_notes,
        )
    )
    scripted = Scripted(
        [
            _tool_calls(
                [
                    (
                        "explain_location_choice",
                        {"item": "solder wick", "location_id": "B001"},
                    )
                ]
            ),
            _text("B001 matches solder and fits the item."),
        ]
    )

    result = run_agent(
        "why did you pick B001 for solder wick?",
        store=store,
        locations=locations,
        client=_client(scripted),
    )

    explanation = result.trace[0]["result"]
    assert explanation["location_id"] == "B001"
    assert [node["label"] for node in explanation["path"]] == [
        "Main Cabinet",
        "B001",
    ]
    assert any("matches purpose tags: solder" in r for r in explanation["reasons"])


def test_agent_stages_pouch_plan_without_writing(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    scripted = Scripted(
        [
            _tool_calls(
                [
                    ("create_location", {"type": "pouch", "label": "Blue Pouch", "parent_id": "B001"}),
                    ("put_item", {"item": "M3 hex nuts", "location_id": "blue-pouch", "quantity": 50}),
                ]
            )
        ]
    )
    result = run_agent(
        "put these m3 hex nuts in a new blue pouch in drawer 1",
        store=store,
        locations=locations,
        client=_client(scripted),
    )

    assert [s["tool"] for s in result.plan] == ["create_location", "put_item"]
    # Nothing written yet — it is only a proposal.
    assert store.search("hex nuts") == []
    assert locations.get("blue-pouch") is None


def test_agent_receives_placement_context_before_planning(tmp_path) -> None:
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
    scripted = Scripted(
        [
            _tool_calls(
                [("put_item", {"item": "solder wick", "location_id": "solder-pouch"})]
            )
        ]
    )

    result = run_agent(
        "put away solder wick",
        store=store,
        locations=locations,
        client=_client(scripted),
    )

    assert result.plan[0]["arguments"]["location_id"] == "solder-pouch"
    system_messages = [
        m["content"] for m in scripted.bodies[0]["messages"] if m.get("role") == "system"
    ]
    assert any("Ranked placement candidates" in content for content in system_messages)
    assert any("solder-pouch" in content for content in system_messages)


def test_agent_receives_learned_rules_before_planning(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    store.learn_rule(
        rule_text="Jewelry should go in the main cabinet.",
        trigger_terms=["jewelry", "bracelet", "ring"],
        target_location_id="main-cabinet",
        source_command="I always keep jewelry in the main cabinet",
    )
    scripted = Scripted(
        [
            _tool_calls(
                [("put_item", {"item": "bracelet clasp", "location_id": "main-cabinet"})]
            )
        ]
    )

    result = run_agent(
        "put away bracelet clasp",
        store=store,
        locations=locations,
        client=_client(scripted),
    )

    assert result.plan[0]["arguments"]["location_id"] == "main-cabinet"
    system_messages = [
        m["content"] for m in scripted.bodies[0]["messages"] if m.get("role") == "system"
    ]
    assert any("Learned placement rules" in content for content in system_messages)
    assert any("Jewelry should go in the main cabinet" in content for content in system_messages)


def test_agent_receives_photo_sort_feedback_examples(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    examples = [
        {
            "command": "sort this photo into separate bins",
            "kind": "edited_photo_sort",
            "action": json.dumps(
                {
                    "original_plan": [
                        {
                            "tool": "put_item",
                            "arguments": {"item": "nuts", "location_id": "B001"},
                        }
                    ],
                    "corrected_plan": [
                        {
                            "tool": "put_item",
                            "arguments": {"item": "M4 T-nuts", "location_id": "B010"},
                        }
                    ],
                }
            ),
        },
        {
            "command": "sort this photo into separate bins",
            "kind": "rejected_photo_sort",
            "action": json.dumps(
                {
                    "rejected_plan": [
                        {
                            "tool": "put_item",
                            "arguments": {"item": "assorted hardware", "location_id": "B001"},
                        }
                    ]
                }
            ),
        },
        {
            "command": "review corrected generic nuts to M4 T-nuts",
            "kind": "review_correction",
            "action": json.dumps(
                {
                    "before": {"name": "generic nuts", "bin_id": "B001"},
                    "after": {"name": "M4 T-nuts", "bin_id": "B010"},
                }
            ),
        },
        {
            "command": "review approved M4 T-nuts in B010",
            "kind": "review_approved",
            "action": json.dumps(
                {
                    "approved_review": {
                        "name": "M4 T-nuts",
                        "bin_id": "B010",
                        "quantity": 2,
                        "category": "hardware",
                        "review_reason": "Photo sort created multiple item groups; confirm each item and destination.",
                    }
                }
            ),
        },
    ]
    scripted = Scripted(
        [
            _tool_calls(
                [("put_item", {"item": "M4 T-nuts", "location_id": "B010"})]
            )
        ]
    )

    run_agent(
        "sort this photo into separate bins",
        store=store,
        locations=locations,
        client=_client(scripted),
        examples=examples,
    )

    system_messages = [
        m["content"] for m in scripted.bodies[0]["messages"] if m.get("role") == "system"
    ]
    learned = "\n".join(system_messages)
    assert "model first proposed" in learned
    assert "user corrected to" in learned
    assert "M4 T-nuts" in learned
    assert "user rejected this plan" in learned
    assert "do not repeat that grouping or generic labeling" in learned
    assert "review correction" in learned
    assert "prefer the corrected name, quantity, category, aliases, and destination" in learned
    assert "review approved" in learned
    assert "accepted item" in learned
    assert "this name, quantity, category, destination, and review reason were accepted" in learned


def test_agent_list_locations_reports_item_counts(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    item_id = store.upsert_item(name="Additional soldering iron tips")
    store.put_inventory(item_id=item_id, bin_id="B001", quantity=1, unit="lot")
    scripted = Scripted(
        [
            _tool_calls([("list_locations", {"parent_id": "main-cabinet"})]),
            _text("Use an empty bin."),
        ]
    )

    run_agent(
        "put usb drives in a small bin in the main cabinet",
        store=store,
        locations=locations,
        client=_client(scripted),
    )

    b001 = next(entry for entry in scripted.bodies[1]["messages"] if entry.get("role") == "tool")
    locations_payload = json.loads(b001["content"])
    b001_location = next(loc for loc in locations_payload if loc["id"] == "B001")
    assert b001_location["item_count"] == 1
    assert b001_location["path"] == [
        {"id": "main-cabinet", "label": "Main Cabinet"},
        {"id": "B001", "label": "B001"},
    ]


def test_agent_list_locations_filters_and_bounds_results(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    item_id = store.upsert_item(name="occupied item")
    store.put_inventory(item_id=item_id, bin_id="B001", quantity=1, unit="each")

    result = run_read_tool(
        "list_locations",
        {
            "parent_id": "main-cabinet",
            "capacity_class": "small",
            "empty_only": True,
            "limit": 2,
        },
        store=store,
        locations=locations,
    )

    assert len(result) == 2
    assert all(entry["capacity_class"] == "small" for entry in result)
    assert all(entry["item_count"] == 0 for entry in result)
    assert all(entry["id"] != "B001" for entry in result)


def test_agent_photo_sorting_can_stage_multiple_grouped_items(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    scripted = Scripted(
        [
            _tool_calls(
                [
                    ("put_item", {"item": "tactile switches", "location_id": "B034", "quantity": 6}),
                    ("put_item", {"item": "LEDs", "location_id": "B035", "quantity": 10}),
                    ("put_item", {"item": "ceramic capacitors", "location_id": "B036", "quantity": 2}),
                ]
            )
        ]
    )

    result = run_agent(
        "sort this photo into separate component bins",
        store=store,
        locations=locations,
        client=_client(scripted),
        images=["data:image/jpeg;base64,AAA"],
    )

    assert [step["arguments"]["item"] for step in result.plan] == [
        "tactile switches",
        "LEDs",
        "ceramic capacitors",
    ]
    user_message = next(
        message for message in scripted.bodies[0]["messages"] if message.get("role") == "user"
    )
    content = user_message["content"]
    assert isinstance(content, list)
    assert content[0]["text"] == "sort this photo into separate component bins"
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg")


def test_execute_plan_creates_pouch_and_item_resolves_to_nearest_lit(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    plan = [
        {"tool": "create_location", "arguments": {"type": "pouch", "label": "Blue Pouch", "parent_id": "B001"}},
        {"tool": "put_item", "arguments": {"item": "M3 hex nuts", "location_id": "blue-pouch", "quantity": 50}},
    ]

    applied = execute_plan(plan, store=store, locations=locations)

    assert applied[0] == {"created_location": "blue-pouch"}
    assert applied[1]["stored"] == "M3 hex nuts"
    assert applied[1]["location_id"] == "blue-pouch"
    assert applied[1]["item_id"]
    pouch = locations.get("blue-pouch")
    assert pouch.parent_id == "B001" and pouch.type == "pouch" and pouch.is_lit is False
    assert store.search("hex nuts")[0].bin_id == "blue-pouch"
    # Unlit pouch -> the find lights the bin it sits in.
    assert locations.nearest_lit("blue-pouch").id == "B001"


def test_execute_plan_rejects_unknown_location_types_before_writing(tmp_path) -> None:
    store, locations = _stores(tmp_path)

    with pytest.raises(ValueError, match="unknown storage place type"):
        execute_plan(
            [
                {
                    "tool": "create_location",
                    "arguments": {"type": "spaceship", "label": "Invalid"},
                }
            ],
            store=store,
            locations=locations,
        )

    assert locations.get_any("invalid") is None


def test_execute_plan_can_mark_put_item_for_review(tmp_path) -> None:
    store, locations = _stores(tmp_path)

    applied = execute_plan(
        [
            {
                "tool": "put_item",
                "arguments": {
                    "item": "unknown electronic part",
                    "location_id": "B001",
                    "needs_review": True,
                },
            }
        ],
        store=store,
        locations=locations,
    )

    assert applied[0]["needs_review"] is True
    [review_item] = store.review_queue()
    assert review_item.name == "unknown electronic part"
    assert review_item.bin_id == "B001"


def test_execute_plan_resolves_bin_case(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    # The model lowercased the bin; it should still land in the real "B115".
    execute_plan(
        [{"tool": "put_item", "arguments": {"item": "glue sticks", "location_id": "b115", "quantity": 1}}],
        store=store,
        locations=locations,
    )
    assert store.items_in_location("B115")[0].name == "glue sticks"


def test_execute_plan_moves_container(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    execute_plan(
        [{"tool": "create_location", "arguments": {"type": "pouch", "label": "Kit Bag", "parent_id": "B001"}}],
        store=store,
        locations=locations,
    )

    applied = execute_plan(
        [{"tool": "move_container", "arguments": {"location_id": "kit-bag", "new_parent_id": "B002"}}],
        store=store,
        locations=locations,
    )

    assert locations.get("kit-bag").parent_id == "B002"
    assert applied == [
        {
            "moved_container": "kit-bag",
            "from_parent_id": "B001",
            "to_parent_id": "B002",
            "location_id": "kit-bag",
        }
    ]
    events = locations.events("kit-bag")
    assert [event["event_type"] for event in events[:2]] == [
        "location_moved",
        "location_created",
    ]
    assert events[0]["note"] == "from B001 to B002"


def test_execute_plan_moves_existing_item_without_duplication(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    item_id = store.upsert_item(name="Allen keys")
    store.put_inventory(item_id=item_id, bin_id="B041", quantity=1, unit="set")

    applied = execute_plan(
        [
            {
                "tool": "create_location",
                "arguments": {
                    "type": "bin",
                    "label": "Large Hand Tool Bin",
                    "parent_id": "main-cabinet",
                    "capacity_class": "large",
                },
            },
            {
                "tool": "move_item",
                "arguments": {
                    "item_id": item_id,
                    "item": "Allen keys",
                    "from_location_id": "B041",
                    "to_location_id": "large-hand-tool-bin",
                },
            },
        ],
        store=store,
        locations=locations,
    )

    assert applied[0] == {"created_location": "large-hand-tool-bin"}
    assert applied[1]["moved_item"] == item_id
    assert applied[1]["from_location_id"] == "B041"
    assert applied[1]["to_location_id"] == "large-hand-tool-bin"
    assert applied[1]["previous_from_quantity"] == 1
    assert applied[1]["previous_from_unit"] == "set"
    assert applied[1]["previous_to_quantity"] is None
    assert store.items_in_location("B041") == []
    moved = store.items_in_location("large-hand-tool-bin")
    assert len(moved) == 1
    assert moved[0].item_id == item_id


def test_execute_plan_removes_existing_item_from_inventory(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    item_id = store.upsert_item(name="dead batteries")
    store.put_inventory(item_id=item_id, bin_id="B001", quantity=4, unit="each")

    applied = execute_plan(
        [
            {
                "tool": "remove_item",
                "arguments": {
                    "item_id": item_id,
                    "item": "dead batteries",
                    "location_id": "B001",
                },
            }
        ],
        store=store,
        locations=locations,
    )

    assert applied == [
        {
            "removed_item": item_id,
            "item": "dead batteries",
            "location_id": "B001",
            "quantity": 4,
            "unit": "each",
        }
    ]
    assert store.items_in_location("B001") == []


def test_execute_plan_learns_rule(tmp_path) -> None:
    store, locations = _stores(tmp_path)

    applied = execute_plan(
        [
            {
                "tool": "learn_rule",
                "arguments": {
                    "rule_text": "Jewelry should go in the main cabinet.",
                    "trigger_terms": ["jewelry", "bracelet", "ring"],
                    "target_location_id": "main-cabinet",
                    "source_command": "I always keep jewelry in the main cabinet",
                },
            }
        ],
        store=store,
        locations=locations,
    )

    assert applied[0]["learned_rule"]
    rule = store.matching_rules("bracelet clasp")[0]
    assert rule.target_location_id == "main-cabinet"
    assert rule.source_command == "I always keep jewelry in the main cabinet"


def test_execute_plan_learns_size_rule(tmp_path) -> None:
    store, locations = _stores(tmp_path)

    applied = execute_plan(
        [
            {
                "tool": "learn_size_rule",
                "arguments": {
                    "rule_text": "Filament spools should be large.",
                    "trigger_terms": ["filament", "spool"],
                    "size_class": "large",
                    "source_command": "filament spools are large",
                },
            }
        ],
        store=store,
        locations=locations,
    )

    assert applied[0]["learned_size_rule"]
    rule = store.matching_size_rules("filament spool")[0]
    assert rule.size_class == "large"
    assert rule.source_command == "filament spools are large"


def test_execute_plan_updates_item_size(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    item_id = store.upsert_item(name="Jewelry Box")
    store.put_inventory(item_id=item_id, bin_id="B001", quantity=1, unit="each")
    store.update_item_size(item_id=item_id, size_class="small", source="manual", confidence=0.7)

    applied = execute_plan(
        [
            {
                "tool": "update_item_size",
                "arguments": {"item": "Jewelry Box", "size_class": "medium"},
            }
        ],
        store=store,
        locations=locations,
    )

    assert applied[0]["item"] == "Jewelry Box"
    assert applied[0]["size_class"] == "medium"
    assert applied[0]["previous_size_class"] == "small"
    assert applied[0]["previous_size_source"] == "manual"
    assert applied[0]["previous_size_confidence"] == 0.7
    assert store.item_size_class("Jewelry Box") == "medium"


def test_execute_plan_updates_item_home(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    item_id = store.upsert_item(name="M4 T-nuts")
    store.put_inventory(item_id=item_id, bin_id="B001", quantity=2, unit="each")
    store.move_inventory(item_id=item_id, from_bin="B001", to_bin="B002")

    applied = execute_plan(
        [
            {
                "tool": "update_item_home",
                "arguments": {"item": "M4 T-nuts", "location_id": "B003"},
            }
        ],
        store=store,
        locations=locations,
    )

    assert applied[0]["updated_item_home"] == item_id
    assert applied[0]["location_id"] == "B003"
    assert applied[0]["previous_home_location_id"] == "B001"
    assert applied[0]["previous_last_seen_location_id"] == "B002"
    [item] = store.search("M4 T-nuts")
    assert item.home_location_id == "B003"
    assert item.last_seen_location_id == "B002"
    assert store.suggest_home("put away M4 T-nuts") == "B003"


def test_execute_plan_updates_item_metadata_and_keeps_old_name_alias(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    item_id = store.upsert_item(name="Generic Hardware", aliases=["misc fastener"])
    store.put_inventory(item_id=item_id, bin_id="B001", quantity=2, unit="each")

    applied = execute_plan(
        [
            {
                "tool": "update_item",
                "arguments": {
                    "item_id": item_id,
                    "name": "M4 T-nuts",
                    "category": "fasteners",
                },
            }
        ],
        store=store,
        locations=locations,
    )

    assert applied[0]["updated_item"] == item_id
    assert applied[0]["item"] == "M4 T-nuts"
    assert applied[0]["previous_name"] == "Generic Hardware"
    assert applied[0]["previous_category"] == ""
    assert applied[0]["previous_aliases"] == ["misc fastener"]
    item = store.items_in_location("B001")[0]
    assert item.name == "M4 T-nuts"
    assert item.category == "fasteners"
    assert set(item.aliases) == {"misc fastener", "Generic Hardware"}
    assert store.search("Generic Hardware")[0].name == "M4 T-nuts"


def test_execute_plan_forgets_rule(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    rule_id = store.learn_rule(
        rule_text="Jewelry should go in the main cabinet.",
        trigger_terms=["jewelry", "bracelet"],
        target_location_id="main-cabinet",
    )

    applied = execute_plan(
        [
            {
                "tool": "forget_rule",
                "arguments": {"rule_id": rule_id, "rule_type": "placement"},
            }
        ],
        store=store,
        locations=locations,
    )

    assert applied == [{"forgotten_rule": rule_id, "rule_type": "placement"}]
    assert store.matching_rules("bracelet clasp") == []


def test_execute_plan_updates_placement_rule(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    rule_id = store.learn_rule(
        rule_text="Jewelry should go in the main cabinet.",
        trigger_terms=["jewelry"],
        target_location_id="main-cabinet",
    )

    applied = execute_plan(
        [
            {
                "tool": "update_rule",
                "arguments": {
                    "rule_id": rule_id,
                    "rule_text": "Watches should go in the top tray.",
                    "trigger_terms": ["watch", "watches"],
                    "target_location_id": "main-cabinet",
                },
            }
        ],
        store=store,
        locations=locations,
    )

    assert applied[0]["updated_rule"] == rule_id
    assert applied[0]["previous_rule_text"] == "Jewelry should go in the main cabinet."
    assert applied[0]["previous_trigger_terms"] == ["jewelry"]
    assert applied[0]["previous_target_location_id"] == "main-cabinet"
    assert store.matching_rules("jewelry") == []
    assert store.matching_rules("watch band")[0].rule_text == "Watches should go in the top tray."


def test_execute_plan_updates_size_rule(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    rule_id = store.learn_size_rule(
        rule_text="Jewelry boxes should be medium.",
        trigger_terms=["jewelry", "box"],
        size_class="medium",
    )

    applied = execute_plan(
        [
            {
                "tool": "update_size_rule",
                "arguments": {
                    "rule_id": rule_id,
                    "rule_text": "Watch boxes should be small.",
                    "trigger_terms": ["watch", "box"],
                    "size_class": "small",
                },
            }
        ],
        store=store,
        locations=locations,
    )

    assert applied[0]["updated_size_rule"] == rule_id
    assert applied[0]["previous_rule_text"] == "Jewelry boxes should be medium."
    assert set(applied[0]["previous_trigger_terms"]) == {"jewelry", "box"}
    assert applied[0]["previous_size_class"] == "medium"
    assert store.matching_size_rules("jewelry ring") == []
    assert store.matching_size_rules("watch box")[0].size_class == "small"


def test_execute_plan_requests_label_print_after_confirm(tmp_path) -> None:
    store, locations = _stores(tmp_path)

    applied = execute_plan(
        [{"tool": "print_label", "arguments": {"location_id": "B001"}}],
        store=store,
        locations=locations,
    )

    assert applied == [{"print_label": "B001"}]


def test_execute_plan_resolves_parent_label_to_slug(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    execute_plan(
        [{"tool": "create_location", "arguments": {"type": "room", "label": "Attic"}}],
        store=store,
        locations=locations,
    )

    execute_plan(
        [
            {
                "tool": "create_location",
                "arguments": {
                    "type": "box",
                    "label": "Roaster Parts Bin",
                    "parent_id": "Attic",
                },
            }
        ],
        store=store,
        locations=locations,
    )

    assert locations.get("roaster-parts-bin").parent_id == "attic"


def test_normalize_plan_drops_existing_location_create(tmp_path) -> None:
    _, locations = _stores(tmp_path)
    locations.upsert(
        Location(
            id="attic",
            type="room",
            label="Attic",
            capacity_class="oversize",
        )
    )
    plan = [
        {"tool": "create_location", "arguments": {"type": "room", "label": "Attic"}},
        {
            "tool": "create_location",
            "arguments": {
                "type": "box",
                "label": "Printer Supplies Bin",
                "parent_id": "attic",
            },
        },
    ]

    normalized = normalize_plan(plan, locations=locations)

    assert normalized == [plan[1]]


def test_normalize_plan_keeps_same_label_when_requested_under_different_parent(tmp_path) -> None:
    _, locations = _stores(tmp_path)
    locations.upsert(Location(id="basement", type="room", label="Basement"))
    locations.upsert(Location(id="garage", type="room", label="Garage"))
    locations.upsert(
        Location(
            id="printer-cabinet",
            type="cabinet",
            label="Printer Cabinet",
            parent_id="basement",
        )
    )
    plan = [
        {
            "tool": "create_location",
            "arguments": {
                "type": "cabinet",
                "label": "Printer Cabinet",
                "parent_id": "garage",
            },
        }
    ]

    assert normalize_plan(plan, locations=locations) == plan


def test_normalize_plan_redirects_generic_main_cabinet_bin_from_occupied_bin(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    layout = StorageLayout.from_path("config/storage-layout.json")
    item_id = store.upsert_item(name="Additional soldering iron tips")
    store.put_inventory(item_id=item_id, bin_id="B001", quantity=1, unit="lot")
    expected = first_empty_bin(layout, store, min_size="small")
    plan = [
        {
            "tool": "put_item",
            "arguments": {
                "item": "USB drives and card readers",
                "location_id": "B001",
                "quantity": 4,
            },
        }
    ]

    normalized = normalize_plan(
        plan,
        locations=locations,
        store=store,
        layout=layout,
        command="Little USB drives can go in a small bin in the main cabinet",
    )

    assert normalized[0]["arguments"]["location_id"] == expected
    assert normalized[0]["arguments"]["location_id"] != "B001"


def test_normalize_plan_uses_existing_bin_instead_of_generic_small_drawer_create(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    layout = StorageLayout.from_path("config/storage-layout.json")
    expected = first_empty_bin(layout, store, min_size="small")
    plan = [
        {
            "tool": "create_location",
            "arguments": {
                "type": "drawer",
                "label": "Small Drawer",
                "parent_id": "main-cabinet",
            },
        },
        {
            "tool": "put_item",
            "arguments": {
                "item": "USB drives and card readers",
                "location_id": "small-drawer",
                "quantity": 4,
            },
        },
    ]

    normalized = normalize_plan(
        plan,
        locations=locations,
        store=store,
        layout=layout,
        command="Just a small drawer in the main cabinet",
    )

    assert normalized == [
        {
            "tool": "put_item",
            "arguments": {
                "item": "USB drives and card readers",
                "location_id": expected,
                "quantity": 4,
            },
        }
    ]


def test_normalize_plan_photo_sort_uses_existing_related_bins_not_created_bins(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    layout = StorageLayout.from_path("config/storage-layout.json")
    led_item = store.upsert_item(
        name="LED Variety Pack",
        category="electronics",
        aliases=["assorted leds", "light emitting diodes"],
    )
    store.put_inventory(item_id=led_item, bin_id="B016", quantity=1, unit="each")
    switch_item = store.upsert_item(
        name="Tactile Push Button Switch",
        category="electronics",
        aliases=["tact switch", "push button", "momentary switch"],
    )
    store.put_inventory(item_id=switch_item, bin_id="B025", quantity=5, unit="each")
    limit_switch_item = store.upsert_item(name="limit switches")
    store.put_inventory(item_id=limit_switch_item, bin_id="B014", quantity=8, unit="each")
    misc_item = store.upsert_item(
        name="Assorted Electronic Components",
        category="electronics",
        aliases=["capacitors", "diodes"],
    )
    store.put_inventory(item_id=misc_item, bin_id="B027", quantity=15, unit="each")
    plan = [
        {"tool": "create_location", "arguments": {"type": "bin", "label": "LEDs Bin", "parent_id": "main-cabinet"}},
        {"tool": "create_location", "arguments": {"type": "bin", "label": "Switches Bin", "parent_id": "main-cabinet"}},
        {"tool": "put_item", "arguments": {"item": "LEDs (assorted)", "location_id": "leds-bin", "quantity": 9}},
        {"tool": "put_item", "arguments": {"item": "Tactile Switches", "location_id": "switches-bin", "quantity": 5}},
        {"tool": "put_item", "arguments": {"item": "Ceramic Capacitors (104)", "location_id": "capacitors-bin", "quantity": 2}},
        {"tool": "put_item", "arguments": {"item": "Diodes", "location_id": "diodes-bin", "quantity": 2}},
    ]

    normalized = normalize_plan(
        plan,
        locations=locations,
        store=store,
        layout=layout,
        command="sort this photo into separate component bins",
    )

    assert [step["tool"] for step in normalized] == ["put_item"] * 4
    destinations = {
        step["arguments"]["item"]: step["arguments"]["location_id"]
        for step in normalized
    }
    assert destinations["LEDs (assorted)"] == "B016"
    assert destinations["Tactile Switches"] == "B025"
    assert destinations["Ceramic Capacitors (104)"] == "B027"
    assert destinations["Diodes"] == "B027"


def test_normalize_plan_photo_sort_uses_distinct_empty_bins_for_unknown_groups(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    layout = StorageLayout.from_path("config/storage-layout.json")
    plan = [
        {"tool": "put_item", "arguments": {"item": "unknown sensor", "location_id": "sensor-bin"}},
        {"tool": "put_item", "arguments": {"item": "mystery module", "location_id": "module-bin"}},
    ]

    normalized = normalize_plan(
        plan,
        locations=locations,
        store=store,
        layout=layout,
        command="catalog this photo separately",
    )

    destinations = [step["arguments"]["location_id"] for step in normalized]
    assert destinations == ["B001", "B002"]


def test_normalize_plan_prefers_related_existing_hardware_bin_over_occupied_bin(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    layout = StorageLayout.from_path("config/storage-layout.json")
    lead_nut_id = store.upsert_item(name="T8x8 Lead Screw Nut")
    store.put_inventory(item_id=lead_nut_id, bin_id="B010", quantity=2, unit="each")
    t_nut_id = store.upsert_item(name="T-nuts")
    store.put_inventory(item_id=t_nut_id, bin_id="B043", quantity=1, unit="each")
    plan = [
        {
            "tool": "put_item",
            "arguments": {
                "item": "M4 T-nuts",
                "location_id": "B010",
                "quantity": 2,
                "unit": "pieces",
            },
        }
    ]

    normalized = normalize_plan(
        plan,
        locations=locations,
        store=store,
        layout=layout,
        command="Put away M4 t nut",
    )

    assert normalized[0]["arguments"]["location_id"] == "B043"


def test_normalize_plan_preserves_explicit_occupied_destination(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    layout = StorageLayout.from_path("config/storage-layout.json")
    adapter_id = store.upsert_item(name="USB adapters")
    store.put_inventory(item_id=adapter_id, bin_id="B050", quantity=1, unit="each")
    occupied_id = store.upsert_item(name="Other adapters")
    store.put_inventory(item_id=occupied_id, bin_id="B034", quantity=1, unit="each")
    plan = [
        {
            "tool": "put_item",
            "arguments": {
                "item": "USB adapters",
                "location_id": "B034",
                "quantity": 1,
                "unit": "each",
            },
        }
    ]

    normalized = normalize_plan(
        plan,
        locations=locations,
        store=store,
        layout=layout,
        command="I put another USB adapter in B034 as well",
    )

    assert normalized[0]["arguments"]["location_id"] == "B034"


def test_normalize_plan_does_not_reuse_conflicting_hardware_variant_bin(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    layout = StorageLayout.from_path("config/storage-layout.json")
    existing = store.upsert_item(name="M4 x 12 mm button head screws")
    store.put_inventory(item_id=existing, bin_id="B043", quantity=10, unit="pieces")
    occupied = store.upsert_item(name="Assorted brackets")
    store.put_inventory(item_id=occupied, bin_id="B010", quantity=2, unit="pieces")
    plan = [
        {
            "tool": "put_item",
            "arguments": {
                "item": "M4 x 20 mm button head screws",
                "location_id": "B010",
                "quantity": 4,
                "unit": "pieces",
            },
        }
    ]

    normalized = normalize_plan(
        plan,
        locations=locations,
        store=store,
        layout=layout,
        command="Put away M4 x 20 mm button head screws",
    )

    assert normalized[0]["arguments"]["location_id"] != "B043"


def test_execute_plan_does_not_overwrite_existing_location_metadata(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    locations.upsert(
        Location(
            id="attic",
            type="room",
            label="Attic",
            capacity_class="oversize",
            purpose_tags=["storage"],
        )
    )

    applied = execute_plan(
        [
            {
                "tool": "create_location",
                "arguments": {
                    "type": "room",
                    "label": "Attic",
                    "capacity_class": "medium",
                    "purpose_tags": ["wrong"],
                },
            },
            {
                "tool": "create_location",
                "arguments": {
                    "type": "box",
                    "label": "Printer Supplies Bin",
                    "parent_id": "Attic",
                },
            },
        ],
        store=store,
        locations=locations,
    )

    assert applied == [{"created_location": "printer-supplies-bin"}]
    attic = locations.get("attic")
    assert attic.capacity_class == "oversize"
    assert attic.purpose_tags == ["storage"]
    assert locations.get("printer-supplies-bin").parent_id == "attic"


def test_execute_plan_creates_scoped_duplicate_location_and_preserves_references(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    locations.upsert(Location(id="basement", type="room", label="Basement"))
    locations.upsert(Location(id="garage", type="room", label="Garage"))
    locations.upsert(
        Location(
            id="printer-cabinet",
            type="cabinet",
            label="Printer Cabinet",
            parent_id="basement",
            purpose_tags=["resin"],
        )
    )

    applied = execute_plan(
        [
            {
                "tool": "create_location",
                "arguments": {
                    "type": "cabinet",
                    "label": "Printer Cabinet",
                    "parent_id": "garage",
                    "accepts_items": True,
                },
            },
            {
                "tool": "create_location",
                "arguments": {
                    "type": "drawer",
                    "label": "Filament Drawer",
                    "parent_id": "printer-cabinet",
                },
            },
            {
                "tool": "put_item",
                "arguments": {
                    "item": "PLA samples",
                    "location_id": "printer-cabinet",
                    "quantity": 3,
                },
            },
        ],
        store=store,
        locations=locations,
    )

    assert applied[0] == {"created_location": "printer-cabinet-2"}
    assert locations.get("printer-cabinet").parent_id == "basement"
    assert locations.get("printer-cabinet").purpose_tags == ["resin"]
    assert locations.get("printer-cabinet-2").parent_id == "garage"
    assert locations.get("filament-drawer").parent_id == "printer-cabinet-2"
    [stored] = store.items_in_location("printer-cabinet-2")
    assert stored.name == "PLA samples"
    assert store.items_in_location("printer-cabinet") == []


def test_execute_plan_requires_path_for_duplicate_location_labels(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    locations.upsert(Location(id="basement", type="room", label="Basement"))
    locations.upsert(Location(id="garage", type="room", label="Garage"))
    locations.upsert(
        Location(
            id="printer-cabinet",
            type="cabinet",
            label="Printer Cabinet",
            parent_id="basement",
        )
    )
    locations.upsert(
        Location(
            id="printer-cabinet-2",
            type="cabinet",
            label="Printer Cabinet",
            parent_id="garage",
        )
    )

    with pytest.raises(ValueError, match="ambiguous location"):
        execute_plan(
            [{"tool": "archive_location", "arguments": {"location_id": "Printer Cabinet"}}],
            store=store,
            locations=locations,
        )

    applied = execute_plan(
        [
            {
                "tool": "archive_location",
                "arguments": {"location_id": "Garage > Printer Cabinet"},
            }
        ],
        store=store,
        locations=locations,
    )
    assert applied[0]["archived_location_id"] == "printer-cabinet-2"
    assert locations.get("printer-cabinet") is not None
    assert locations.get("printer-cabinet-2") is None


def test_execute_plan_puts_item_in_qualified_duplicate_location(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    locations.upsert(Location(id="basement", type="room", label="Basement"))
    locations.upsert(Location(id="garage", type="room", label="Garage"))
    locations.upsert(
        Location(
            id="printer-cabinet",
            type="cabinet",
            label="Printer Cabinet",
            parent_id="basement",
        )
    )
    locations.upsert(
        Location(
            id="printer-cabinet-2",
            type="cabinet",
            label="Printer Cabinet",
            parent_id="garage",
        )
    )

    with pytest.raises(ValueError, match="ambiguous location"):
        execute_plan(
            [
                {
                    "tool": "put_item",
                    "arguments": {"item": "USB cable", "location_id": "Printer Cabinet"},
                }
            ],
            store=store,
            locations=locations,
        )
    assert store.list_inventory() == []

    execute_plan(
        [
            {
                "tool": "put_item",
                "arguments": {
                    "item": "USB cable",
                    "location_id": "Printer Cabinet in Garage",
                },
            }
        ],
        store=store,
        locations=locations,
    )
    assert store.items_in_location("printer-cabinet") == []
    assert store.items_in_location("printer-cabinet-2")[0].name == "USB cable"


def test_execute_plan_does_not_reactivate_archived_location_on_name_collision(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    locations.upsert(Location(id="parts-box", type="box", label="Parts Box"))
    locations.archive_subtree("parts-box")

    applied = execute_plan(
        [{"tool": "create_location", "arguments": {"type": "box", "label": "Parts Box"}}],
        store=store,
        locations=locations,
    )

    assert applied == [{"created_location": "parts-box-2"}]
    assert locations.get_any("parts-box").active is False
    assert locations.get("parts-box-2").active is True


def test_execute_plan_updates_location_metadata_with_previous_values(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    locations.upsert(
        Location(
            id="solder-pouch",
            type="pouch",
            label="Solder Pouch",
            parent_id="B001",
            capacity_class="small",
            purpose_tags=["solder"],
            placement_notes="Solder wick only.",
            internal_dimensions={"width_mm": 100, "depth_mm": 80, "height_mm": 30},
        )
    )

    applied = execute_plan(
        [
            {
                "tool": "update_location",
                "arguments": {
                    "location_id": "Solder Pouch",
                    "label": "Solder Kit",
                    "type": "box",
                    "capacity_class": "medium",
                    "purpose_tags": ["solder", "electronics"],
                    "placement_notes": "Solder wick, flux, tips, and small soldering tools.",
                    "internal_dimensions": {
                        "width_mm": 200,
                        "depth_mm": 160,
                        "height_mm": 60,
                    },
                },
            }
        ],
        store=store,
        locations=locations,
    )

    assert applied == [
        {
            "updated_location": "solder-pouch",
            "label": "Solder Kit",
            "type": "box",
            "capacity_class": "medium",
            "purpose_tags": ["solder", "electronics"],
            "placement_notes": "Solder wick, flux, tips, and small soldering tools.",
            "internal_dimensions": {
                "width_mm": 200.0,
                "depth_mm": 160.0,
                "height_mm": 60.0,
            },
            "accepts_items": True,
            "previous_label": "Solder Pouch",
            "previous_type": "pouch",
            "previous_capacity_class": "small",
            "previous_purpose_tags": ["solder"],
            "previous_placement_notes": "Solder wick only.",
            "previous_internal_dimensions": {
                "width_mm": 100,
                "depth_mm": 80,
                "height_mm": 30,
            },
            "previous_accepts_items": True,
        }
    ]
    updated = locations.get("solder-pouch")
    assert updated.label == "Solder Kit"
    assert updated.type == "box"
    assert updated.capacity_class == "medium"
    assert updated.purpose_tags == ["solder", "electronics"]
    assert "flux" in updated.placement_notes
    assert updated.internal_dimensions == {
        "width_mm": 200.0,
        "depth_mm": 160.0,
        "height_mm": 60.0,
    }


def test_execute_plan_creates_location_with_internal_dimensions(tmp_path) -> None:
    store, locations = _stores(tmp_path)

    applied = execute_plan(
        [
            {
                "tool": "create_location",
                "arguments": {
                    "type": "drawer",
                    "label": "Measured Drawer",
                    "internal_dimensions": {
                        "width_mm": 304.8,
                        "depth_mm": 457.2,
                        "height_mm": 76.2,
                    },
                },
            }
        ],
        store=store,
        locations=locations,
    )

    assert applied == [{"created_location": "measured-drawer"}]
    assert locations.get("measured-drawer").internal_dimensions == {
        "width_mm": 304.8,
        "depth_mm": 457.2,
        "height_mm": 76.2,
    }


def test_execute_plan_persists_measured_item_dimensions(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    locations.upsert(
        Location(
            id="large-case-bin",
            type="bin",
            label="Large Case Bin",
            capacity_class="large",
            internal_dimensions={
                "width_mm": 400,
                "depth_mm": 300,
                "height_mm": 200,
            },
        )
    )

    applied = execute_plan(
        [
            {
                "tool": "put_item",
                "arguments": {
                    "item": "Camera case",
                    "location_id": "large-case-bin",
                    "dimensions": {
                        "length_mm": 304.8,
                        "width_mm": 203.2,
                        "height_mm": 76.2,
                    },
                },
            }
        ],
        store=store,
        locations=locations,
    )

    saved = store.exact_match("Camera case")[0]
    assert saved.size_class == "large"
    assert saved.size_source == "agent"
    assert saved.dimensions == {
        "length_mm": 304.8,
        "width_mm": 203.2,
        "height_mm": 76.2,
    }
    assert applied[1]["updated_item_size"] == saved.item_id
    assert applied[0]["stored"] == "Camera case"


def test_execute_plan_attaches_photo_to_item_and_location(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    item_photo = store.add_photo(filename="item.jpg", content_type="image/jpeg", data=b"photo")
    location_photo = store.add_photo(filename="bin.jpg", content_type="image/jpeg", data=b"photo")
    item_id = store.upsert_item(name="solder wick")
    store.put_inventory(item_id=item_id, bin_id="B001", quantity=1, unit="roll")

    applied = execute_plan(
        [
            {
                "tool": "attach_photo",
                "arguments": {
                    "photo_id": item_photo.id,
                    "object_type": "item",
                    "object_id": "solder wick",
                },
            },
            {
                "tool": "attach_photo",
                "arguments": {
                    "photo_id": location_photo.id,
                    "object_type": "location",
                    "object_id": "B001",
                },
            },
        ],
        store=store,
        locations=locations,
    )

    assert applied[0]["attached_photo"] == item_photo.id
    assert applied[0]["object_type"] == "item"
    assert applied[0]["item_id"] == item_id
    assert applied[0]["previous_item_id"] is None
    assert store.photo(item_photo.id).item_id == item_id
    assert applied[1]["object_type"] == "location"
    assert applied[1]["location_id"] == "B001"
    assert store.photo(location_photo.id).location_id == "B001"


def test_execute_plan_rejects_unknown_parent_without_sqlite_500(tmp_path) -> None:
    store, locations = _stores(tmp_path)

    try:
        execute_plan(
            [
                {
                    "tool": "create_location",
                    "arguments": {
                        "type": "box",
                        "label": "Roaster Parts Bin",
                        "parent_id": "Attic",
                    },
                }
            ],
            store=store,
            locations=locations,
        )
    except ValueError as exc:
        assert "unknown parent location: Attic" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_execute_plan_preflight_prevents_partial_location_setup(tmp_path) -> None:
    store, locations = _stores(tmp_path)

    with pytest.raises(ValueError, match=r"plan step 2 .*unknown parent location"):
        execute_plan(
            [
                {
                    "tool": "create_location",
                    "arguments": {
                        "type": "room",
                        "label": "Garage",
                        "accepts_items": True,
                    },
                },
                {
                    "tool": "create_location",
                    "arguments": {
                        "type": "cabinet",
                        "label": "Printer Cabinet",
                        "parent_id": "Missing Room",
                    },
                },
            ],
            store=store,
            locations=locations,
        )

    assert locations.get("garage") is None
    assert locations.get("printer-cabinet") is None


def test_execute_plan_preflight_rejects_duplicate_sibling_locations(tmp_path) -> None:
    store, locations = _stores(tmp_path)

    with pytest.raises(ValueError, match=r"plan step 3 .*already exists"):
        execute_plan(
            [
                {
                    "tool": "create_location",
                    "arguments": {"type": "room", "label": "Garage"},
                },
                {
                    "tool": "create_location",
                    "arguments": {
                        "type": "cabinet",
                        "label": "Parts Cabinet",
                        "parent_id": "Garage",
                    },
                },
                {
                    "tool": "create_location",
                    "arguments": {
                        "type": "shelf",
                        "label": " parts   cabinet ",
                        "parent_id": "Garage",
                    },
                },
            ],
            store=store,
            locations=locations,
        )

    assert locations.get("garage") is None
    assert locations.get("parts-cabinet") is None


def test_execute_plan_preflight_allows_nested_new_locations(tmp_path) -> None:
    store, locations = _stores(tmp_path)

    applied = execute_plan(
        [
            {
                "tool": "create_location",
                "arguments": {"type": "room", "label": "Garage"},
            },
            {
                "tool": "create_location",
                "arguments": {
                    "type": "cabinet",
                    "label": "Printer Cabinet",
                    "parent_id": "Garage",
                },
            },
        ],
        store=store,
        locations=locations,
    )

    assert applied == [
        {"created_location": "garage"},
        {"created_location": "printer-cabinet"},
    ]
    assert locations.get("printer-cabinet").parent_id == "garage"


def test_execute_plan_preflight_tracks_new_items_across_steps(tmp_path) -> None:
    store, locations = _stores(tmp_path)

    applied = execute_plan(
        [
            {
                "tool": "put_item",
                "arguments": {"item": "USB-C adapters", "location_id": "B001"},
            },
            {
                "tool": "move_item",
                "arguments": {
                    "item": "USB-C adapters",
                    "from_location_id": "B001",
                    "to_location_id": "B002",
                },
            },
        ],
        store=store,
        locations=locations,
    )

    item_id = int(applied[0]["item_id"])
    assert store.item_inventory(item_id)[0].bin_id == "B002"


def test_execute_plan_preflight_rejects_bad_quantity_before_location_write(
    tmp_path,
) -> None:
    store, locations = _stores(tmp_path)

    with pytest.raises(ValueError, match=r"plan step 2 .*could not convert"):
        execute_plan(
            [
                {
                    "tool": "create_location",
                    "arguments": {
                        "type": "room",
                        "label": "Garage",
                        "accepts_items": True,
                    },
                },
                {
                    "tool": "put_item",
                    "arguments": {
                        "item": "USB-C adapters",
                        "location_id": "Garage",
                        "quantity": "several",
                    },
                },
            ],
            store=store,
            locations=locations,
        )

    assert locations.get("garage") is None
    assert store.search("USB-C adapters") == []


def test_execute_plan_preflight_prevents_location_write_before_bad_item_move(
    tmp_path,
) -> None:
    store, locations = _stores(tmp_path)
    item_id = store.upsert_item(name="M4 T-nuts")
    store.put_inventory(item_id=item_id, bin_id="B001", quantity=10, unit="each")

    with pytest.raises(ValueError, match=r"plan step 2 .*not stored in B002"):
        execute_plan(
            [
                {
                    "tool": "create_location",
                    "arguments": {"type": "room", "label": "Garage"},
                },
                {
                    "tool": "move_item",
                    "arguments": {
                        "item_id": item_id,
                        "from_location_id": "B002",
                        "to_location_id": "B001",
                    },
                },
            ],
            store=store,
            locations=locations,
        )

    assert locations.get("garage") is None
    assert store.item_inventory(item_id)[0].bin_id == "B001"


def test_agent_gives_up_after_max_steps(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    scripted = Scripted([_tool_calls([("search_items", {"query": "x"})]) for _ in range(4)])

    result = run_agent(
        "loop forever", store=store, locations=locations, client=_client(scripted), max_steps=4
    )

    assert "could not make progress" in result.message.lower()
    assert result.plan == []
    assert len(scripted.bodies) == 2


def test_agent_stops_when_model_repeats_the_same_read(tmp_path) -> None:
    store, locations = _stores(tmp_path)
    scripted = Scripted(
        [_tool_calls([("search_items", {"query": "x"})]) for _ in range(4)]
    )

    result = run_agent(
        "find x", store=store, locations=locations, client=_client(scripted)
    )

    assert "could not make progress" in result.message.lower()
    assert result.usage["requests"] == 2
    assert len(scripted.bodies) == 2


def test_confirm_endpoint_applies_plan(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 2,
                "rows_bottom_to_top": [{"size": "small", "bin_count": 2, "spans": [1, 1]}],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=2,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )
    client = TestClient(create_app(cfg))

    response = client.post(
        "/assistant/confirm",
        json={
            "command": "put spare fuses in a new spare bag",
            "plan": [
                {"tool": "create_location", "arguments": {"type": "pouch", "label": "Spare Bag", "parent_id": "B001"}},
                {"tool": "put_item", "arguments": {"item": "spare fuses", "location_id": "spare-bag", "quantity": 10}},
            ]
        },
    )

    assert response.status_code == 200
    assert response.json()["applied"][0] == {"created_location": "spare-bag"}
    assert response.json()["applied"][1]["location_id"] == "spare-bag"
    assert response.json()["applied"][1]["item_id"]
    memory = (tmp_path / "placement-memory.md").read_text(encoding="utf-8")
    assert "put spare fuses" in memory
    assert "spare-bag" in memory
