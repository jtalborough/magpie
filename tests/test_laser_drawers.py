from pathlib import Path

import pytest

from searchable_storage_led.laser_drawers import (
    DrawerSpec,
    RowSpec,
    drawer_panels,
    generate_drawer_files,
    generate_row_files,
    pack_sheets,
    row_panels,
)


def test_small_drawer_profiles_are_closed_and_dimensioned() -> None:
    spec = DrawerSpec(1, 3, 5)
    panels = drawer_panels(spec)

    assert [panel.name for panel in panels] == [
        "drawer_1x3x5_front_handle_slot",
        "drawer_1x3x5_back",
        "drawer_1x3x5_rear_stop_lip",
        "drawer_1x3x5_left_side",
        "drawer_1x3x5_right_side",
        "drawer_1x3x5_bottom",
    ]
    assert panels[0].width == pytest.approx(spec.outer_width)
    assert panels[0].height == pytest.approx(spec.outer_height)
    assert len(panels[0].cutouts) == 1
    assert panels[2].width == pytest.approx(spec.inner_width)
    assert panels[2].height == pytest.approx(spec.rear_stop_lip_height)
    assert panels[3].width == pytest.approx(spec.outer_depth)
    assert panels[5].width == pytest.approx(spec.inner_width)
    assert panels[5].height == pytest.approx(spec.inner_depth)

    for panel in panels:
        assert panel.outline[0] != panel.outline[-1]
        assert len(panel.outline) >= 4
        for a, b in zip(panel.outline, panel.outline[1:]):
            assert a != b


def test_profiles_do_not_immediately_backtrack() -> None:
    for panel in drawer_panels(DrawerSpec(1, 3, 5)):
        closed = panel.outline + [panel.outline[0]]
        for a, b, c in zip(closed, closed[1:], closed[2:]):
            ab = (round(b[0] - a[0], 6), round(b[1] - a[1], 6))
            bc = (round(c[0] - b[0], 6), round(c[1] - b[1], 6))
            assert ab != (-bc[0], -bc[1])


def test_front_panel_has_rounded_handle_slot_cutout() -> None:
    front = drawer_panels(DrawerSpec(1, 3, 5))[0]
    handle = front.cutouts[0]

    assert len(handle) > 8
    assert min(x for x, _ in handle) == pytest.approx(4.0)
    assert max(x for x, _ in handle) == pytest.approx(31.5)
    assert min(y for _, y in handle) == pytest.approx(6.0)
    assert max(y for _, y in handle) == pytest.approx(12.0)


def test_default_drawer_set_fits_one_bed_each() -> None:
    for spec in [DrawerSpec(1, 3, 5), DrawerSpec(2, 3, 5), DrawerSpec(3, 3, 10)]:
        sheets = pack_sheets(f"drawer_{spec.name}", drawer_panels(spec))
        assert len(sheets) >= 1
        for sheet in sheets:
            for panel in sheet.panels:
                x0, y0, x1, y1 = panel.bounds
                assert 0 <= x0 < x1 <= 400
                assert 0 <= y0 < y1 <= 400


def test_large_drawer_splits_across_beds() -> None:
    sheets = pack_sheets("drawer_6x3x15", drawer_panels(DrawerSpec(6, 3, 15)))

    assert len(sheets) > 1
    for sheet in sheets:
        for panel in sheet.panels:
            x0, y0, x1, y1 = panel.bounds
            assert 0 <= x0 < x1 <= 400
            assert 0 <= y0 < y1 <= 400


def test_generator_writes_svg_and_dxf(tmp_path: Path) -> None:
    written = generate_drawer_files([DrawerSpec(1, 3, 5)], tmp_path)

    names = {path.name for path in written}
    assert names == {"drawer_1x3x5_sheet_1.svg", "drawer_1x3x5_sheet_1.dxf"}
    dxf = (tmp_path / "drawer_1x3x5_sheet_1.dxf").read_text(encoding="ascii")
    assert "$INSUNITS" in dxf
    assert "POLYLINE" in dxf
    assert "CUT" in dxf


def test_generator_can_render_png_preview(tmp_path: Path) -> None:
    written = generate_drawer_files([DrawerSpec(1, 3, 5)], tmp_path, render=True)

    names = {path.name for path in written}
    assert "drawer_1x3x5_sheet_1.png" in names
    assert "contact_sheet.png" in names
    assert (tmp_path / "drawer_1x3x5_sheet_1.png").stat().st_size > 0
    assert (tmp_path / "contact_sheet.png").stat().st_size > 0


def test_row_layout_generates_carcase_bay_and_led_rail_parts() -> None:
    spec = RowSpec(row_layout=(1, 2, 3))
    panels = row_panels(spec)

    names = [panel.name for panel in panels]
    assert names[:6] == [
        "row_1-2-3_shelf_slide_surface",
        "row_1-2-3_left_side_panel",
        "row_1-2-3_right_side_panel",
        "row_1-2-3_back_panel",
        "row_1-2-3_bay_divider_1",
        "row_1-2-3_bay_divider_2",
    ]
    assert names[6:8] == [
        "row_1-2-3_led_raceway_spacer",
        "row_1-2-3_front_fascia_diffuser_windows",
    ]
    assert not [name for name in names if "front_separator" in name]
    assert len([name for name in names if "diffuser" in name]) == 4
    assert panels[0].width == pytest.approx(258.0)
    assert panels[0].bounds[1] == pytest.approx(0.0)
    assert panels[0].height == pytest.approx(141.0)
    assert len(panels[0].cutouts) == 14
    assert panels[1].width == pytest.approx(138.0)
    assert panels[1].height == pytest.approx(64.0)
    assert len(panels[1].cutouts) == 0
    assert panels[2].width == pytest.approx(138.0)
    assert panels[2].height == pytest.approx(64.0)
    assert len(panels[2].cutouts) == 0
    assert panels[3].width == pytest.approx(258.0)
    assert panels[3].height == pytest.approx(64.0)
    assert panels[4].width == pytest.approx(138.0)
    assert panels[4].height == pytest.approx(67.0)
    assert len(panels[6].cutouts) == 1
    assert len(panels[7].cutouts) == 3


def test_row_layout_must_fill_fixed_width() -> None:
    with pytest.raises(ValueError, match="sum to units_per_column"):
        RowSpec(row_layout=(1, 2, 2))


def test_row_generator_writes_rendered_outputs(tmp_path: Path) -> None:
    written = generate_row_files([RowSpec(row_layout=(2, 2, 2))], tmp_path, render=True)
    names = {path.name for path in written}

    assert "row_2-2-2_sheet_1.svg" in names
    assert "row_2-2-2_sheet_1.dxf" in names
    assert "row_2-2-2_sheet_1.png" in names
    assert "row_contact_sheet.png" in names
    assert "row_2-2-2_assembly_preview.png" in names
    assert (tmp_path / "row_2-2-2_assembly_preview.png").stat().st_size > 0
    dxf = "".join(path.read_text(encoding="ascii") for path in written if path.suffix == ".dxf")
    assert dxf.count("POLYLINE") > len(row_panels(RowSpec(row_layout=(2, 2, 2))))
