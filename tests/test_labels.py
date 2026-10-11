from searchable_storage_led.config import ZebraConfig
from searchable_storage_led.labels import LabelPayload, render_location_zpl, render_qr_svg, zpl_escape
from searchable_storage_led.locations import Location


def test_render_location_zpl_includes_title_contents_and_qr() -> None:
    payload = LabelPayload(
        location=Location(
            id="roaster-parts-bin",
            type="box",
            label="Roaster Parts Bin",
            capacity_class="large",
            purpose_tags=["roaster", "parts", "spares"],
        ),
        path=[
            Location(id="attic", type="room", label="Attic"),
            Location(id="roaster-parts-bin", type="box", label="Roaster Parts Bin"),
        ],
        contents=[
            _item("spare fans"),
            _item("spare burner"),
            _item("misc roaster hardware"),
        ],
        base_url="http://magpie.example.com:8765",
        workspace_id="local",
    )

    zpl = render_location_zpl(payload, ZebraConfig(width_dots=1200, length_dots=1194, dpi=300))

    assert "^XA" in zpl
    assert "^PW1200" in zpl
    assert "^A0N,79,79" in zpl
    assert "^A0N,45,45" in zpl
    assert "ROASTER PARTS BIN" in zpl
    assert "spare fans" in zpl
    assert "^BQN" in zpl
    assert (
        "http://magpie.example.com:8765/scan/local/l/roaster-parts-bin"
        in zpl
    )
    assert zpl.endswith("^XZ\n")


def test_zpl_escape_removes_control_delimiters() -> None:
    assert zpl_escape("bad^field~x\nnext") == "bad field x next"


def test_render_qr_svg_embeds_scan_url() -> None:
    svg = render_qr_svg("http://magpie.example.com:8765/l/B001")

    assert "<svg" in svg
    assert "<path" in svg
    assert "xmlns=\"http://www.w3.org/2000/svg\"" in svg


def test_multi_workspace_label_uses_workspace_qualified_scan_shell() -> None:
    payload = LabelPayload(
        location=Location(id="blue box", type="box", label="Blue Box"),
        path=[],
        contents=[],
        base_url="https://magpie.example",
        workspace_id="11111111-1111-1111-1111-111111111111",
    )

    assert payload.url == (
        "https://magpie.example/scan/11111111-1111-1111-1111-111111111111"
        "/l/blue%20box"
    )


def _item(name: str):
    from searchable_storage_led.store import SearchResult

    return SearchResult(
        item_id=1,
        name=name,
        description="",
        category="",
        aliases=[],
        bin_id="roaster-parts-bin",
        quantity=1,
        unit="each",
        needs_review=False,
        review_reason="",
        photos=[],
    )
