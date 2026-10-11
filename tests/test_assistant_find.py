import json

from fastapi.testclient import TestClient

from searchable_storage_led.api import create_app
from searchable_storage_led.config import LedConfig


def _client(tmp_path) -> TestClient:
    layout = tmp_path / "layout.json"
    layout.write_text(
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
        pixel_count=8,
        backend="wled",
        wled_url="http://127.0.0.1:1",  # dead on purpose; lighting must fail soft
        layout_path=str(layout),
        database_path=str(tmp_path / "inventory.db"),
    )
    return TestClient(create_app(cfg))


def test_find_resolves_path_and_lit_ancestor_for_cabinet_bin(tmp_path) -> None:
    client = _client(tmp_path)
    added = client.post("/inventory", json={"name": "o-rings", "bin_id": "B001", "quantity": 3}).json()

    res = client.post("/assistant/find", json={"query": "o-rings"}).json()

    assert res["results"][0]["item_id"] == added["item_id"]
    assert res["results"][0]["location_id"] == "B001"
    assert [p["label"] for p in res["results"][0]["path"]][-1] == "B001"
    # B001 is a cabinet bin -> it is itself the nearest lit location.
    assert res["results"][0]["lit_location"] == "B001"


def test_ask_finds_when_you_have_it(tmp_path) -> None:
    client = _client(tmp_path)
    client.post("/inventory", json={"name": "washers", "bin_id": "B001", "quantity": 5})

    res = client.post("/assistant/ask", json={"query": "washers"}).json()

    assert res["action"] == "found"
    assert res["results"][0]["location_id"] == "B001"


def test_ask_puts_away_to_existing_empty_bin_when_you_dont(tmp_path) -> None:
    client = _client(tmp_path)
    client.post("/inventory", json={"name": "washers", "bin_id": "B001", "quantity": 5})

    res = client.post("/assistant/ask", json={"query": "m8 lock nuts"}).json()

    assert res["action"] == "put_away"
    # picks the remaining empty cabinet bin, not a brand-new drawer
    assert res["destination"]["id"] == "B002"
    # returns a fileable draft (deterministic fallback when no LLM key)
    assert res["item"]["name"]
    assert "unit" in res["item"] and "aliases" in res["item"]


def test_ask_puts_bulky_canvas_kit_in_semantic_drawer_not_small_bin(tmp_path) -> None:
    client = _client(tmp_path)
    client.post(
        "/locations",
        json={
            "type": "cabinet",
            "label": "PrinterCab",
            "capacity_class": "large",
            "purpose_tags": ["printer", "tools"],
        },
    )
    client.post(
        "/locations",
        json={
            "type": "drawer",
            "label": "Soldering Drawer",
            "parent_id": "printercab",
            "capacity_class": "medium",
            "purpose_tags": ["soldering", "tools"],
        },
    )

    res = client.post(
        "/assistant/ask",
        json={"query": "red canvas bag with soldering gear"},
    ).json()

    assert res["action"] == "put_away"
    assert res["destination"]["id"] == "soldering-drawer"


def test_fit_correction_moves_selected_item_to_printer_cabinet(tmp_path) -> None:
    client = _client(tmp_path)
    client.post(
        "/locations",
        json={
            "type": "cabinet",
            "label": "PrinterCab",
            "capacity_class": "large",
            "purpose_tags": ["printer", "tools"],
        },
    )
    client.post(
        "/locations",
        json={
            "type": "drawer",
            "label": "Soldering Drawer",
            "parent_id": "printercab",
            "capacity_class": "medium",
            "purpose_tags": ["soldering", "tools"],
        },
    )
    item = client.post(
        "/inventory",
        json={"name": "Soldering Tool Kit", "bin_id": "B001", "quantity": 1},
    ).json()

    res = client.post(
        "/assistant/ask",
        json={
            "query": "that won't fit in the main cabinet has to go in the printer cabinet",
            "current_location_id": "B001",
        },
    ).json()

    assert res["action"] == "moved"
    assert res["destination"]["id"] == "soldering-drawer"
    assert client.get("/locations/B001").json()["items"] == []
    moved = client.get("/locations/soldering-drawer").json()["items"]
    assert moved[0]["item_id"] == item["item_id"]


def test_find_degrades_to_words_in_an_unlit_unit(tmp_path) -> None:
    client = _client(tmp_path)
    client.post("/locations", json={"type": "shelf-unit", "label": "Shelf Unit A", "parent_id": None})
    client.post("/locations", json={"type": "pouch", "label": "Blue Pouch", "parent_id": "shelf-unit-a"})
    client.post("/inventory", json={"name": "spare fuses", "bin_id": "blue-pouch", "quantity": 10})

    res = client.post("/assistant/find", json={"query": "fuses"}).json()
    first = res["results"][0]

    assert first["location_id"] == "blue-pouch"
    assert [p["label"] for p in first["path"]] == ["Shelf Unit A", "Blue Pouch"]
    assert first["lit_location"] is None  # no lights -> words carry it
