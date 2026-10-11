from __future__ import annotations

import base64
import json

from fastapi.testclient import TestClient

from searchable_storage_led.api import create_app
from searchable_storage_led.config import LedConfig


def confirm_setup(client: TestClient, command: str) -> dict[str, object]:
    proposed = client.post(
        "/assistant/chat",
        json={"text": command, "mode": "setup_new", "source": "mobile"},
    ).json()
    assert proposed["action"] == "proposed"
    confirmed = client.post(
        "/assistant/confirm",
        json={
            "command": command,
            "source_interaction_id": proposed["interaction_id"],
            "plan": proposed["plan"],
        },
    )
    assert confirmed.status_code == 200
    return confirmed.json()


def test_software_first_storage_journey_is_composable(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {"name": "software-only", "columns": 1, "rows_bottom_to_top": []}
        ),
        encoding="utf-8",
    )
    client = TestClient(
        create_app(
            LedConfig(
                pixel_count=0,
                backend="none",
                layout_path=str(layout_path),
                database_path=str(tmp_path / "inventory.db"),
            )
        )
    )

    garage = confirm_setup(
        client,
        "Set up Smoke Garage as a room. Inside it: smoke tool box",
    )
    attic = confirm_setup(
        client,
        "Set up Smoke Attic as a room. Inside it: smoke jewelry box",
    )
    garage_id = garage["receipt"]["focus_location_id"]
    attic_id = attic["receipt"]["focus_location_id"]
    locations = client.get("/locations").json()["locations"]
    tool_box = next(row for row in locations if row["label"] == "Smoke Tool Box")
    jewelry_box = next(row for row in locations if row["label"] == "Smoke Jewelry Box")
    assert tool_box["parent_id"] == garage_id
    assert jewelry_box["parent_id"] == attic_id

    photo = client.post(
        "/photos",
        json={
            "filename": "smoke.jpg",
            "content_type": "image/jpeg",
            "data_base64": base64.b64encode(b"smoke-image").decode("ascii"),
        },
    ).json()["photo"]
    stored = client.post(
        f"/locations/{tool_box['id']}/quick-put",
        json={"text": "smoke screwdriver", "photo_ids": [photo["id"]]},
    ).json()["item"]
    assert stored["photo_ids"] == [photo["id"]]

    moved = client.post(
        "/inventory/move",
        json={
            "item_id": stored["item_id"],
            "from_bin": tool_box["id"],
            "to_bin": jewelry_box["id"],
        },
    ).json()
    assert moved["to_bin"] == jewelry_box["id"]
    assert client.get(f"/locations/{attic_id}/contents").json()["total"] == 1
    undone = client.post("/assistant/undo").json()["undone"][0]
    assert undone == {
        "moved_item": stored["item_id"],
        "from_location_id": jewelry_box["id"],
        "to_location_id": tool_box["id"],
    }

    learned = client.post(
        "/assistant/confirm",
        json={
            "command": "I always keep heirloom jewelry in the smoke jewelry box",
            "plan": [
                {
                    "tool": "learn_rule",
                    "arguments": {
                        "rule_text": "Heirloom jewelry goes in the smoke jewelry box.",
                        "trigger_terms": ["heirloom", "jewelry"],
                        "target_location_id": jewelry_box["id"],
                    },
                }
            ],
        },
    )
    assert learned.status_code == 200
    proposal = client.post(
        "/assistant/chat",
        json={"text": "put away heirloom jewelry", "source": "mobile"},
    ).json()
    decision = proposal["decision"]
    assert decision["destination"]["id"] == jewelry_box["id"]
    assert decision["learning"]["rule_adherence"][0]["followed"] is True
    confirmed = client.post(
        "/assistant/confirm",
        json={
            "command": "put away heirloom jewelry",
            "source_interaction_id": proposal["interaction_id"],
            "plan": decision["plan"],
        },
    )
    assert confirmed.status_code == 200
    assert client.get("/assistant/learning/adherence").json()["followed"] == 1

    labels_html = client.get(f"/locations/{attic_id}/labels.html")
    labels_pdf = client.get(f"/locations/{attic_id}/labels.pdf")
    location_label_pdf = client.get(f"/locations/{attic_id}/label.pdf")
    location_qr = client.get(f"/locations/{attic_id}/qr.svg")
    item_pdf = client.get(f"/items/{stored['item_id']}/label.pdf")
    scan_shell = client.get(f"/scan/smoke/l/{jewelry_box['id']}")
    assert labels_html.status_code == 200 and "<svg" in labels_html.text
    assert labels_pdf.headers["content-type"] == "application/pdf"
    assert labels_pdf.content.startswith(b"%PDF")
    assert location_label_pdf.headers["content-type"] == "application/pdf"
    assert location_label_pdf.content.startswith(b"%PDF")
    assert location_qr.headers["content-type"].startswith("image/svg+xml")
    assert "<svg" in location_qr.text
    assert item_pdf.headers["content-type"] == "application/pdf"
    assert item_pdf.content.startswith(b"%PDF")
    assert "magpie.auth.workspace.v1" in scan_shell.text

    lifecycle = client.post(
        "/locations",
        json={
            "type": "box",
            "label": "Lifecycle Box",
            "parent_id": attic_id,
        },
    ).json()
    assert client.post(f"/locations/{lifecycle['id']}/archive").status_code == 200
    archived = client.get("/locations?include_archived=true").json()["locations"]
    assert next(row for row in archived if row["id"] == lifecycle["id"])["active"] is False
    assert client.post(f"/locations/{lifecycle['id']}/restore").status_code == 200
    active = client.get("/locations").json()["locations"]
    assert next(row for row in active if row["id"] == lifecycle["id"])["active"] is True
