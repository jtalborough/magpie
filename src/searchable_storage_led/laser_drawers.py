"""Laser-cut Gridfinity drawer profile generator.

The geometry here is intentionally 2D-first: each panel is a single closed
outline that can be exported to DXF/SVG for laser cutting. The generated drawer
is a glued finger-joint wood tray sized from Gridfinity units.
"""

from __future__ import annotations

import argparse
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from xml.sax.saxutils import escape


Point = tuple[float, float]
Polygon = list[Point]


@dataclass(frozen=True)
class DrawerSpec:
    width_units: int
    depth_units: int = 3
    height_units: int = 5
    gu: float = 42.0
    zu: float = 7.0
    ply_thickness: float = 3.0
    width_clearance: float = 0.5
    depth_clearance: float = 0.5
    joint_fit: float = 0.15
    finger_count: int = 5
    handle_slot_height: float = 6.0
    handle_slot_side_margin: float = 4.0
    handle_slot_bottom_margin: float = 6.0
    handle_slot_max_width: float = 86.0
    rear_stop_lip_height: float = 12.0

    def __post_init__(self) -> None:
        if not 1 <= self.width_units <= 6:
            raise ValueError("width_units must be in the 1..6 range")
        if self.depth_units < 1:
            raise ValueError("depth_units must be positive")
        if self.height_units < 1:
            raise ValueError("height_units must be positive")
        if self.ply_thickness <= 0:
            raise ValueError("ply_thickness must be positive")
        if self.finger_count < 3 or self.finger_count % 2 == 0:
            raise ValueError("finger_count must be an odd integer >= 3")

    @property
    def outer_width(self) -> float:
        return self.width_units * self.gu - self.width_clearance

    @property
    def outer_depth(self) -> float:
        return self.depth_units * self.gu - self.depth_clearance

    @property
    def outer_height(self) -> float:
        return self.height_units * self.zu

    @property
    def inner_width(self) -> float:
        return self.outer_width - 2 * self.ply_thickness

    @property
    def inner_depth(self) -> float:
        return self.outer_depth - 2 * self.ply_thickness

    @property
    def name(self) -> str:
        return f"{self.width_units}x{self.depth_units}x{self.height_units}"


@dataclass(frozen=True)
class Panel:
    name: str
    outline: Polygon
    thickness: float
    role: str = "cut"
    cutouts: tuple[Polygon, ...] = ()

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        xs = [p[0] for p in self.outline]
        ys = [p[1] for p in self.outline]
        return min(xs), min(ys), max(xs), max(ys)

    @property
    def width(self) -> float:
        x0, _, x1, _ = self.bounds
        return x1 - x0

    @property
    def height(self) -> float:
        _, y0, _, y1 = self.bounds
        return y1 - y0

    def translated(self, dx: float, dy: float) -> "Panel":
        return Panel(
            name=self.name,
            outline=[(x + dx, y + dy) for x, y in self.outline],
            thickness=self.thickness,
            role=self.role,
            cutouts=tuple(
                [(x + dx, y + dy) for x, y in cutout]
                for cutout in self.cutouts
            ),
        )


@dataclass(frozen=True)
class RowSpec:
    """One fixed-width carcass row cassette.

    ``row_layout`` is the drawer fill pattern in Gridfinity units. For a
    six-unit column, examples are ``(1, 1, 1, 1, 1, 1)``, ``(2, 2, 2)``,
    ``(3, 3)``, ``(1, 2, 3)``, and ``(6,)``.
    """

    row_layout: tuple[int, ...]
    units_per_column: int = 6
    gu: float = 42.0
    drawer_depth_units: int = 3
    ply_thickness: float = 3.0
    shelf_extra_depth: float = 12.0
    front_rail_height: float = 18.0
    bay_height: float = 64.0
    diffuser_reveal_height: float = 5.0
    diffuser_margin: float = 4.0
    led_channel_height: float = 8.0
    bay_divider_thickness: float = 3.0
    side_margin: float = 0.0
    joint_fit: float = 0.15
    joinery_tab_width: float = 10.0

    def __post_init__(self) -> None:
        if not self.row_layout:
            raise ValueError("row_layout must not be empty")
        if any(unit < 1 for unit in self.row_layout):
            raise ValueError("row_layout values must be positive")
        if sum(self.row_layout) != self.units_per_column:
            raise ValueError("row_layout must sum to units_per_column")
        if self.ply_thickness <= 0:
            raise ValueError("ply_thickness must be positive")

    @property
    def row_width(self) -> float:
        return self.units_per_column * self.gu + 2 * self.side_margin

    @property
    def shelf_depth(self) -> float:
        return self.drawer_depth_units * self.gu + self.shelf_extra_depth

    @property
    def name(self) -> str:
        return "row_" + "-".join(str(unit) for unit in self.row_layout)


@dataclass(frozen=True)
class Sheet:
    name: str
    panels: tuple[Panel, ...]
    bed_width: float
    bed_height: float


def _dedupe(points: Iterable[Point]) -> Polygon:
    out: Polygon = []
    for point in points:
        if not out or point != out[-1]:
            out.append(point)
    if len(out) > 1 and out[0] == out[-1]:
        out.pop()
    return out


def _rounded_rect_polygon(x: float, y: float, width: float, height: float, segments: int = 6) -> Polygon:
    radius = min(width, height) / 2
    cx0 = x + radius
    cx1 = x + width - radius
    cy = y + radius
    pts: Polygon = []
    for i in range(segments + 1):
        angle = math.pi / 2 + math.pi * i / segments
        pts.append((cx0 + radius * math.cos(angle), cy + radius * math.sin(angle)))
    for i in range(segments + 1):
        angle = 3 * math.pi / 2 + math.pi * i / segments
        pts.append((cx1 + radius * math.cos(angle), cy + radius * math.sin(angle)))
    return _dedupe(pts)


def _front_back_panel(
    spec: DrawerSpec,
    *,
    name: str,
    include_handle: bool,
) -> Panel:
    t = spec.ply_thickness
    w = spec.inner_width
    h = spec.outer_height
    pitch = h / spec.finger_count
    handle_width = min(
        spec.handle_slot_max_width,
        max(1.0, w - 2 * spec.handle_slot_side_margin),
    )
    handle_height = min(spec.handle_slot_height, max(1.0, h - 2 * spec.handle_slot_bottom_margin))
    handle_x = (w - handle_width) / 2
    handle_y = min(spec.handle_slot_bottom_margin, max(0.0, h - handle_height))

    pts: Polygon = [(0.0, 0.0), (w, 0.0)]

    for i in range(spec.finger_count):
        y0 = i * pitch
        y1 = (i + 1) * pitch
        if i % 2 == 0:
            pts.extend(
                [
                    (w, y0 + spec.joint_fit / 2),
                    (w + t, y0 + spec.joint_fit / 2),
                    (w + t, y1 - spec.joint_fit / 2),
                    (w, y1 - spec.joint_fit / 2),
                ]
            )
        pts.append((w, y1))

    pts.append((0.0, h))
    for i in reversed(range(spec.finger_count)):
        y0 = i * pitch
        y1 = (i + 1) * pitch
        if i % 2 == 0:
            pts.extend(
                [
                    (0.0, y1 - spec.joint_fit / 2),
                    (-t, y1 - spec.joint_fit / 2),
                    (-t, y0 + spec.joint_fit / 2),
                    (0.0, y0 + spec.joint_fit / 2),
                ]
            )
        pts.append((0.0, y0))

    cutouts = ()
    if include_handle:
        cutouts = (_rounded_rect_polygon(handle_x, handle_y, handle_width, handle_height),)

    return Panel(name=name, outline=_dedupe(pts), thickness=t, cutouts=cutouts)


def _side_panel(spec: DrawerSpec, *, name: str) -> Panel:
    t = spec.ply_thickness
    w = spec.outer_depth
    h = spec.outer_height
    slot_depth = t + spec.joint_fit
    pitch = h / spec.finger_count

    pts: Polygon = [(0.0, 0.0), (w, 0.0)]
    for i in range(spec.finger_count):
        y0 = i * pitch
        y1 = (i + 1) * pitch
        if i % 2 == 0:
            pts.extend(
                [
                    (w, y0),
                    (w - slot_depth, y0),
                    (w - slot_depth, y1),
                    (w, y1),
                ]
            )
        pts.append((w, y1))

    pts.append((0.0, h))
    for i in reversed(range(spec.finger_count)):
        y0 = i * pitch
        y1 = (i + 1) * pitch
        if i % 2 == 0:
            pts.extend(
                [
                    (0.0, y1),
                    (slot_depth, y1),
                    (slot_depth, y0),
                    (0.0, y0),
                ]
            )
        pts.append((0.0, y0))

    return Panel(name=name, outline=_dedupe(pts), thickness=t)


def _rectangle_panel(name: str, width: float, height: float, thickness: float, role: str = "cut") -> Panel:
    return Panel(
        name=name,
        outline=[(0.0, 0.0), (width, 0.0), (width, height), (0.0, height)],
        thickness=thickness,
        role=role,
    )


def _rect_polygon(x: float, y: float, width: float, height: float) -> Polygon:
    return [(x, y), (x + width, y), (x + width, y + height), (x, y + height)]


def _front_open_channel_panel(
    name: str,
    width: float,
    depth: float,
    thickness: float,
    channels: Iterable[tuple[float, float, float]],
) -> Panel:
    """Panel with U-shaped channels opening from its front edge.

    The channels are encoded in the outer contour instead of as cutouts that
    touch the edge, keeping the exported DXF as one closed contour.
    """
    pts: Polygon = [(0.0, 0.0)]
    cursor = 0.0
    for x0, x1, length in sorted(channels):
        x0 = max(0.0, min(width, x0))
        x1 = max(0.0, min(width, x1))
        if x1 <= x0 or x0 < cursor:
            continue
        pts.extend([(x0, 0.0), (x0, length), (x1, length), (x1, 0.0)])
        cursor = x1
    pts.extend([(width, 0.0), (width, depth), (0.0, depth)])
    return Panel(name=name, outline=_dedupe(pts), thickness=thickness)


def _tab_centers(span: float, count: int, margin: float) -> list[float]:
    if count <= 0:
        return []
    usable = span - 2 * margin
    if usable <= 0:
        return [span / 2]
    if count == 1:
        return [span / 2]
    return [margin + usable * i / (count - 1) for i in range(count)]


def _bottom_tabbed_panel(
    name: str,
    width: float,
    height: float,
    thickness: float,
    *,
    tab_count: int = 3,
    tab_width: float = 14.0,
    tab_depth: float | None = None,
) -> Panel:
    depth = thickness if tab_depth is None else tab_depth
    centers = _tab_centers(width, tab_count, tab_width)
    intervals = [(max(0.0, c - tab_width / 2), min(width, c + tab_width / 2)) for c in centers]
    intervals.sort()

    pts: Polygon = [(0.0, 0.0)]
    cursor = 0.0
    for x0, x1 in intervals:
        pts.extend([(x0, 0.0), (x0, -depth), (x1, -depth), (x1, 0.0)])
        cursor = x1
    pts.extend([(width, 0.0), (width, height), (0.0, height)])
    return Panel(name=name, outline=_dedupe(pts), thickness=thickness)


def _bottom_and_side_tabbed_panel(
    name: str,
    width: float,
    height: float,
    thickness: float,
    *,
    bottom_tab_count: int = 3,
    side_tab_count: int = 2,
    tab_width: float = 14.0,
) -> Panel:
    panel = _bottom_tabbed_panel(
        name,
        width,
        height,
        thickness,
        tab_count=bottom_tab_count,
        tab_width=tab_width,
    )
    base = panel.outline
    # Start from the bottom-tabbed lower edge, then replace the two vertical
    # sides with tabbed sides. This keeps the outline readable and symmetric.
    lower = [point for point in base if point[1] <= 0.0]
    right_intervals = [
        (max(0.0, c - tab_width / 2), min(height, c + tab_width / 2))
        for c in _tab_centers(height, side_tab_count, tab_width)
    ]
    right_intervals.sort()

    pts: Polygon = []
    pts.extend(lower)
    pts.append((width, 0.0))
    for y0, y1 in right_intervals:
        pts.extend([(width, y0), (width + thickness, y0), (width + thickness, y1), (width, y1)])
    pts.extend([(width, height), (0.0, height)])
    for y0, y1 in reversed(right_intervals):
        pts.extend([(0.0, y1), (-thickness, y1), (-thickness, y0), (0.0, y0)])
    pts.append((0.0, 0.0))
    return Panel(name=name, outline=_dedupe(pts), thickness=thickness)


def _slot_cutouts_along_x(
    span: float,
    y: float,
    thickness: float,
    *,
    tab_count: int = 3,
    tab_width: float = 14.0,
    fit: float = 0.15,
) -> list[Polygon]:
    slot_h = thickness + fit
    return [
        _rect_polygon(c - (tab_width + fit) / 2, y, tab_width + fit, slot_h)
        for c in _tab_centers(span, tab_count, tab_width)
    ]


def _slot_cutouts_along_y(
    x: float,
    span: float,
    thickness: float,
    *,
    tab_count: int = 3,
    tab_width: float = 14.0,
    fit: float = 0.15,
) -> list[Polygon]:
    slot_w = thickness + fit
    return [
        _rect_polygon(x, c - (tab_width + fit) / 2, slot_w, tab_width + fit)
        for c in _tab_centers(span, tab_count, tab_width)
    ]


def _notched_bottom_panel(
    name: str,
    width: float,
    height: float,
    thickness: float,
    *,
    notch_count: int,
    notch_width: float,
    fit: float = 0.15,
) -> Panel:
    notch_w = notch_width + fit
    pts: Polygon = [(0.0, 0.0)]
    for center in _tab_centers(width, notch_count, notch_width):
        x0 = max(0.0, center - notch_w / 2)
        x1 = min(width, center + notch_w / 2)
        pts.extend([(x0, 0.0), (x0, thickness + fit), (x1, thickness + fit), (x1, 0.0)])
    pts.extend([(width, 0.0), (width, height), (0.0, height)])
    return Panel(name=name, outline=_dedupe(pts), thickness=thickness)


def _notched_bottom_and_right_panel(
    name: str,
    width: float,
    height: float,
    thickness: float,
    *,
    bottom_notch_count: int,
    right_notch_count: int,
    notch_width: float,
    fit: float = 0.15,
) -> Panel:
    notch_w = notch_width + fit
    pts: Polygon = [(0.0, 0.0)]
    for center in _tab_centers(width, bottom_notch_count, notch_width):
        x0 = max(0.0, center - notch_w / 2)
        x1 = min(width, center + notch_w / 2)
        pts.extend([(x0, 0.0), (x0, thickness + fit), (x1, thickness + fit), (x1, 0.0)])
    pts.append((width, 0.0))
    for center in _tab_centers(height, right_notch_count, notch_width):
        y0 = max(0.0, center - notch_w / 2)
        y1 = min(height, center + notch_w / 2)
        pts.extend([(width, y0), (width - thickness - fit, y0), (width - thickness - fit, y1), (width, y1)])
    pts.extend([(width, height), (0.0, height)])
    return Panel(name=name, outline=_dedupe(pts), thickness=thickness)


def _tabbed_side_panel(
    name: str,
    width: float,
    height: float,
    thickness: float,
    *,
    side_tab_count: int,
    tab_width: float,
) -> Panel:
    pts: Polygon = [(0.0, 0.0), (width, 0.0)]
    for center in _tab_centers(height, side_tab_count, tab_width):
        y0 = max(0.0, center - tab_width / 2)
        y1 = min(height, center + tab_width / 2)
        pts.extend([(width, y0), (width + thickness, y0), (width + thickness, y1), (width, y1)])
    pts.extend([(width, height), (0.0, height)])
    for center in reversed(_tab_centers(height, side_tab_count, tab_width)):
        y0 = max(0.0, center - tab_width / 2)
        y1 = min(height, center + tab_width / 2)
        pts.extend([(0.0, y1), (-thickness, y1), (-thickness, y0), (0.0, y0)])
    return Panel(name=name, outline=_dedupe(pts), thickness=thickness)


def _tabbed_shelf_panel(
    name: str,
    width: float,
    depth: float,
    thickness: float,
    *,
    side_tab_count: int,
    back_tab_count: int,
    front_tab_count: int,
    tab_width: float,
) -> Panel:
    pts: Polygon = [(0.0, 0.0)]
    for center in _tab_centers(width, front_tab_count, tab_width):
        x0 = max(0.0, center - tab_width / 2)
        x1 = min(width, center + tab_width / 2)
        pts.extend([(x0, 0.0), (x0, -thickness), (x1, -thickness), (x1, 0.0)])
    pts.append((width, 0.0))
    for center in _tab_centers(depth, side_tab_count, tab_width):
        y0 = max(0.0, center - tab_width / 2)
        y1 = min(depth, center + tab_width / 2)
        pts.extend([(width, y0), (width + thickness, y0), (width + thickness, y1), (width, y1)])
    pts.append((width, depth))
    for center in reversed(_tab_centers(width, back_tab_count, tab_width)):
        x0 = max(0.0, center - tab_width / 2)
        x1 = min(width, center + tab_width / 2)
        pts.extend([(x1, depth), (x1, depth + thickness), (x0, depth + thickness), (x0, depth)])
    pts.append((0.0, depth))
    for center in reversed(_tab_centers(depth, side_tab_count, tab_width)):
        y0 = max(0.0, center - tab_width / 2)
        y1 = min(depth, center + tab_width / 2)
        pts.extend([(0.0, y1), (-thickness, y1), (-thickness, y0), (0.0, y0)])
    return Panel(name=name, outline=_dedupe(pts), thickness=thickness)


def drawer_panels(spec: DrawerSpec) -> tuple[Panel, ...]:
    """Return unpositioned panel outlines for one drawer."""
    return (
        _front_back_panel(spec, name=f"drawer_{spec.name}_front_handle_slot", include_handle=True),
        _front_back_panel(spec, name=f"drawer_{spec.name}_back", include_handle=False),
        _rectangle_panel(
            f"drawer_{spec.name}_rear_stop_lip",
            spec.inner_width,
            spec.rear_stop_lip_height,
            spec.ply_thickness,
        ),
        _side_panel(spec, name=f"drawer_{spec.name}_left_side"),
        _side_panel(spec, name=f"drawer_{spec.name}_right_side"),
        _rectangle_panel(
            f"drawer_{spec.name}_bottom",
            spec.inner_width,
            spec.inner_depth,
            spec.ply_thickness,
        ),
    )


def row_panels(spec: RowSpec) -> tuple[Panel, ...]:
    """Return unpositioned laser parts for one drawer row cassette."""
    width = spec.row_width
    shelf_depth = spec.shelf_depth
    t = spec.ply_thickness

    cursor = spec.side_margin
    window_cutouts: list[Polygon] = []
    diffuser_parts: list[Panel] = []
    divider_slots: list[Polygon] = []
    divider_parts: list[Panel] = []

    for index, units in enumerate(spec.row_layout):
        lane_width = units * spec.gu
        window_x = cursor + spec.diffuser_margin
        window_width = max(1.0, lane_width - 2 * spec.diffuser_margin)
        window_y = (spec.front_rail_height - spec.diffuser_reveal_height) / 2
        window_cutouts.append(_rect_polygon(window_x, window_y, window_width, spec.diffuser_reveal_height))
        diffuser_parts.append(
            _rectangle_panel(
                f"{spec.name}_diffuser_{index + 1}_{units}gu",
                window_width,
                spec.diffuser_reveal_height,
                1.5,
                role="reference",
            )
        )
        cursor += lane_width
        if index < len(spec.row_layout) - 1:
            slot_x = cursor - (spec.bay_divider_thickness + spec.joint_fit) / 2
            divider_slots.extend(
                _slot_cutouts_along_y(
                    slot_x,
                    shelf_depth,
                    t,
                    tab_count=7,
                    tab_width=spec.joinery_tab_width,
                    fit=spec.joint_fit,
                )
            )
            divider_parts.append(
                _bottom_tabbed_panel(
                    f"{spec.name}_bay_divider_{index + 1}",
                    shelf_depth,
                    spec.bay_height,
                    t,
                    tab_count=7,
                    tab_width=spec.joinery_tab_width,
                )
            )

    side_joint_count = 7
    back_joint_count = 11

    shelf_base = _tabbed_shelf_panel(
        name=f"{spec.name}_shelf_slide_surface",
        width=width,
        depth=shelf_depth,
        thickness=t,
        side_tab_count=side_joint_count,
        back_tab_count=back_joint_count,
        front_tab_count=0,
        tab_width=spec.joinery_tab_width,
    )
    shelf = Panel(
        name=shelf_base.name,
        outline=shelf_base.outline,
        thickness=shelf_base.thickness,
        cutouts=tuple(divider_slots),
    )
    left_side = _notched_bottom_and_right_panel(
        f"{spec.name}_left_side_panel",
        shelf_depth,
        spec.bay_height,
        t,
        bottom_notch_count=side_joint_count,
        right_notch_count=2,
        notch_width=spec.joinery_tab_width,
        fit=spec.joint_fit,
    )
    right_side = _notched_bottom_and_right_panel(
        f"{spec.name}_right_side_panel",
        shelf_depth,
        spec.bay_height,
        t,
        bottom_notch_count=side_joint_count,
        right_notch_count=2,
        notch_width=spec.joinery_tab_width,
        fit=spec.joint_fit,
    )
    back_base = _notched_bottom_panel(
        f"{spec.name}_back_panel",
        width,
        spec.bay_height,
        t,
        notch_count=back_joint_count,
        notch_width=spec.joinery_tab_width,
        fit=spec.joint_fit,
    )
    back_sides = _tabbed_side_panel(
        back_base.name,
        width,
        spec.bay_height,
        t,
        side_tab_count=2,
        tab_width=spec.joinery_tab_width,
    )
    back_panel = Panel(
        name=back_base.name,
        outline=_dedupe(back_base.outline[:-2] + back_sides.outline[2:]),
        thickness=t,
    )
    led_spacer = Panel(
        name=f"{spec.name}_led_raceway_spacer",
        outline=_rect_polygon(0.0, 0.0, width, spec.front_rail_height),
        thickness=t,
        cutouts=(
            _rect_polygon(
                spec.diffuser_margin,
                (spec.front_rail_height - spec.led_channel_height) / 2,
                width - 2 * spec.diffuser_margin,
                spec.led_channel_height,
            ),
        ),
    )
    front_fascia_base = _rectangle_panel(
        name=f"{spec.name}_front_fascia_diffuser_windows",
        width=width,
        height=spec.front_rail_height,
        thickness=t,
    )
    front_fascia = Panel(
        name=front_fascia_base.name,
        outline=front_fascia_base.outline,
        thickness=front_fascia_base.thickness,
        cutouts=tuple(window_cutouts),
    )

    return (shelf, left_side, right_side, back_panel, *divider_parts, led_spacer, front_fascia, *diffuser_parts)


def pack_sheets(
    name: str,
    panels: Iterable[Panel],
    *,
    bed_width: float = 400.0,
    bed_height: float = 400.0,
    margin: float = 10.0,
    gap: float = 8.0,
) -> tuple[Sheet, ...]:
    """Greedy shelf-pack panels into one or more laser bed sheets."""
    sheets: list[list[Panel]] = [[]]
    sheet_index = 0
    x = margin
    y = margin
    row_height = 0.0

    for panel in panels:
        px0, py0, px1, py1 = panel.bounds
        width = px1 - px0
        height = py1 - py0
        if width + 2 * margin > bed_width or height + 2 * margin > bed_height:
            raise ValueError(f"{panel.name} does not fit on a {bed_width:g}x{bed_height:g} mm bed")

        if x + width > bed_width - margin:
            x = margin
            y += row_height + gap
            row_height = 0.0

        if y + height > bed_height - margin:
            sheets.append([])
            sheet_index += 1
            x = margin
            y = margin
            row_height = 0.0

        sheets[sheet_index].append(panel.translated(x - px0, y - py0))
        x += width + gap
        row_height = max(row_height, height)

    return tuple(
        Sheet(
            name=f"{name}_sheet_{i + 1}",
            panels=tuple(sheet_panels),
            bed_width=bed_width,
            bed_height=bed_height,
        )
        for i, sheet_panels in enumerate(sheets)
        if sheet_panels
    )


def _format_num(value: float) -> str:
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return text or "0"


def write_svg(sheet: Sheet, path: Path, *, include_bed: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{_format_num(sheet.bed_width)}mm" '
            f'height="{_format_num(sheet.bed_height)}mm" viewBox="0 0 '
            f'{_format_num(sheet.bed_width)} {_format_num(sheet.bed_height)}">'
        ),
        "<style>.cut{fill:none;stroke:#d11;stroke-width:0.1}.reference{fill:none;stroke:#06c;stroke-width:0.1;stroke-dasharray:2 2}.bed{fill:none;stroke:#999;stroke-width:0.1}.label{font:4px sans-serif;fill:#333}</style>",
    ]
    if include_bed:
        parts.append(f'<rect class="bed" x="0" y="0" width="{sheet.bed_width}" height="{sheet.bed_height}"/>')
    for panel in sheet.panels:
        pts = " ".join(f"{_format_num(x)},{_format_num(y)}" for x, y in panel.outline)
        parts.append(f'<polygon class="{escape(panel.role)}" points="{pts}"/>')
        for cutout in panel.cutouts:
            cutout_pts = " ".join(f"{_format_num(x)},{_format_num(y)}" for x, y in cutout)
            parts.append(f'<polygon class="cut" points="{cutout_pts}"/>')
        x0, y0, _, _ = panel.bounds
        parts.append(f'<text class="label" x="{_format_num(x0)}" y="{_format_num(max(4, y0 - 1))}">{escape(panel.name)}</text>')
    parts.append("</svg>")
    path.write_text("\n".join(parts) + "\n", encoding="utf-8")


def write_dxf(sheet: Sheet, path: Path) -> None:
    """Write simple millimeter R12-style DXF with one closed polyline per panel."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = [
        "0",
        "SECTION",
        "2",
        "HEADER",
        "9",
        "$INSUNITS",
        "70",
        "4",
        "0",
        "ENDSEC",
        "0",
        "SECTION",
        "2",
        "TABLES",
        "0",
        "TABLE",
        "2",
        "LAYER",
        "0",
        "LAYER",
        "2",
        "CUT",
        "70",
        "0",
        "62",
        "1",
        "6",
        "CONTINUOUS",
        "0",
        "ENDTAB",
        "0",
        "ENDSEC",
        "0",
        "SECTION",
        "2",
        "ENTITIES",
    ]

    def add_polyline(points: Polygon, layer: str = "CUT") -> None:
        lines.extend(["0", "POLYLINE", "8", layer, "66", "1", "70", "1"])
        for x, y in points:
            lines.extend(["0", "VERTEX", "8", layer, "10", _format_num(x), "20", _format_num(y), "30", "0"])
        lines.extend(["0", "SEQEND", "8", layer])

    for panel in sheet.panels:
        if panel.role == "cut":
            add_polyline(panel.outline)
        for cutout in panel.cutouts:
            add_polyline(cutout)

    lines.extend(["0", "ENDSEC", "0", "EOF"])
    path.write_text("\n".join(lines) + "\n", encoding="ascii")


def render_sheet_png(sheet: Sheet, path: Path, *, pixels_per_mm: float = 2.0) -> None:
    """Render a sheet preview PNG.

    PNG rendering is intentionally optional so DXF/SVG generation stays free of
    image dependencies in production environments.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:  # pragma: no cover - exercised only without Pillow
        raise RuntimeError("PNG rendering requires Pillow to be installed") from exc

    path.parent.mkdir(parents=True, exist_ok=True)
    width_px = round(sheet.bed_width * pixels_per_mm)
    height_px = round(sheet.bed_height * pixels_per_mm)
    image = Image.new("RGB", (width_px, height_px), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()

    def xy(point: Point) -> Point:
        return (point[0] * pixels_per_mm, point[1] * pixels_per_mm)

    draw.rectangle([(0, 0), (width_px - 1, height_px - 1)], outline=(180, 180, 180), width=1)
    for panel in sheet.panels:
        color = (200, 20, 20) if panel.role == "cut" else (30, 90, 200)
        points = [xy(point) for point in panel.outline]
        draw.line(points + [points[0]], fill=color, width=2)
        for cutout in panel.cutouts:
            cutout_points = [xy(point) for point in cutout]
            draw.line(cutout_points + [cutout_points[0]], fill=(200, 20, 20), width=2)
        x0, y0, _, _ = panel.bounds
        draw.text((x0 * pixels_per_mm, max(0, y0 * pixels_per_mm - 12)), panel.name, fill=(40, 40, 40), font=font)

    image.save(path)


def render_contact_sheet(image_paths: Iterable[Path], path: Path, *, columns: int = 2, padding: int = 24) -> None:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:  # pragma: no cover - exercised only without Pillow
        raise RuntimeError("PNG rendering requires Pillow to be installed") from exc

    images = [(image_path, Image.open(image_path).convert("RGB")) for image_path in image_paths]
    if not images:
        return

    thumb_width = 520
    title_height = 24
    thumbs: list[tuple[Path, Image.Image]] = []
    for image_path, image in images:
        ratio = thumb_width / image.width
        thumb = image.resize((thumb_width, round(image.height * ratio)))
        thumbs.append((image_path, thumb))

    rows = (len(thumbs) + columns - 1) // columns
    cell_height = max(thumb.height for _, thumb in thumbs) + title_height
    sheet_width = columns * thumb_width + (columns + 1) * padding
    sheet_height = rows * cell_height + (rows + 1) * padding
    contact = Image.new("RGB", (sheet_width, sheet_height), "white")
    draw = ImageDraw.Draw(contact)
    font = ImageFont.load_default()

    for index, (image_path, thumb) in enumerate(thumbs):
        col = index % columns
        row = index // columns
        x = padding + col * (thumb_width + padding)
        y = padding + row * (cell_height + padding)
        draw.text((x, y), image_path.name, fill=(20, 20, 20), font=font)
        contact.paste(thumb, (x, y + title_height))

    path.parent.mkdir(parents=True, exist_ok=True)
    contact.save(path)


def render_row_assembly_preview(spec: RowSpec, path: Path, *, pixels_per_mm: float = 3.0) -> None:
    """Render an assembled row concept, not a cut sheet.

    This is for design review. It shows how drawer lanes, side/back panels, the
    shelf, and the hidden LED reveal relate after assembly.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:  # pragma: no cover - exercised only without Pillow
        raise RuntimeError("PNG rendering requires Pillow to be installed") from exc

    path.parent.mkdir(parents=True, exist_ok=True)
    width_px = 1200
    height_px = 820
    image = Image.new("RGB", (width_px, height_px), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()

    red = (205, 35, 35)
    blue = (35, 95, 210)
    green = (45, 130, 70)
    gray = (85, 85, 85)
    pale = (245, 248, 252)

    def mmx(value: float, origin: float = 70.0) -> float:
        return origin + value * pixels_per_mm

    row_width = spec.row_width
    row_scale = min(950 / row_width, 3.2)

    def fx(value: float) -> float:
        return 95 + value * row_scale

    # Front assembled view.
    front_y = 70
    shelf_y = front_y + 175
    rail_y = shelf_y + 18
    draw.text((90, 35), f"Assembled row concept: {spec.name} fixed {spec.units_per_column} GU width", fill=(20, 20, 20), font=font)
    draw.rectangle((fx(0), shelf_y, fx(row_width), shelf_y + 14), fill=(230, 230, 230), outline=gray)

    cursor = spec.side_margin
    drawer_h = 92
    for index, units in enumerate(spec.row_layout):
        lane_w = units * spec.gu
        x0 = fx(cursor)
        x1 = fx(cursor + lane_w)
        draw.rectangle((x0 + 2, front_y + 45, x1 - 2, front_y + 45 + drawer_h), fill=pale, outline=gray, width=2)
        draw.text((x0 + 8, front_y + 78), f"{units} GU drawer", fill=(20, 20, 20), font=font)
        slot_w = min(74, max(28, (x1 - x0) - 24))
        slot_x0 = x0 + ((x1 - x0) - slot_w) / 2
        draw.rounded_rectangle((slot_x0, front_y + 112, slot_x0 + slot_w, front_y + 122), radius=5, outline=gray, width=2)
        # Soft uplight glow under drawer.
        glow_y = front_y + 45 + drawer_h + 8
        draw.rectangle((x0 + 8, glow_y, x1 - 8, glow_y + 12), fill=(255, 236, 145), outline=None)
        draw.rectangle((x0 + 8, glow_y + 3, x1 - 8, glow_y + 8), fill=(255, 210, 55), outline=None)
        cursor += lane_w
        if index < len(spec.row_layout) - 1:
            sx = fx(cursor)
            divider_px = max(2, round(spec.bay_divider_thickness * row_scale))
            draw.rectangle((sx - divider_px / 2, front_y + 45, sx + divider_px / 2, front_y + 45 + drawer_h), fill=(210, 210, 210), outline=gray)

    draw.rectangle((fx(0), rail_y, fx(row_width), rail_y + 26), fill=(235, 235, 235), outline=red, width=2)
    draw.text((fx(0), rail_y + 34), "applied laminated LED rail under front edge; hidden strip behind flush diffuser windows", fill=red, font=font)

    # Side section.
    sx = 105
    sy = 390
    draw.text((sx, sy - 34), "Side section through one drawer lane", fill=(20, 20, 20), font=font)
    # Shelf.
    draw.rectangle((sx, sy + 145, sx + 410, sy + 165), fill=(230, 230, 230), outline=gray, width=2)
    draw.text((sx + 420, sy + 145), "solid shelf with divider slots", fill=gray, font=font)
    # Drawer.
    draw.rectangle((sx + 40, sy + 48, sx + 350, sy + 142), fill=pale, outline=gray, width=2)
    draw.rectangle((sx + 332, sy + 25, sx + 350, sy + 142), fill=(225, 225, 225), outline=gray, width=2)
    draw.text((sx + 65, sy + 82), "drawer slides on shelf", fill=(20, 20, 20), font=font)
    draw.text((sx + 355, sy + 30), "tall rear stop lip", fill=gray, font=font)
    # LED rail under front.
    draw.rectangle((sx, sy + 166, sx + 410, sy + 208), fill=(245, 245, 245), outline=red, width=2)
    draw.rectangle((sx + 28, sy + 174, sx + 350, sy + 184), fill=(255, 222, 80), outline=blue, width=2)
    draw.text((sx + 28, sy + 214), "flush diffuser reveal; LED hidden behind it, washing the handle area", fill=blue, font=font)
    # Pull area.
    draw.arc((sx + 58, sy + 122, sx + 130, sy + 178), 180, 360, fill=green, width=3)
    draw.text((sx + 140, sy + 128), "front slot handle / glow area", fill=green, font=font)

    # Parts list / outputs.
    px = 660
    py = 392
    draw.text((px, py - 34), "Generated row / carcass-bay parts", fill=(20, 20, 20), font=font)
    parts = [
        ("shelf_slide_surface", "solid structural floor with divider slots"),
        ("left_side_panel / right_side_panel", "row side walls"),
        ("back_panel", "rear wall; also the stop surface behind drawers"),
        ("bay_divider_N", "3mm dividers that capture drawers laterally"),
        ("led_raceway_spacer", "bonded spacer strip creates LED/wire channel"),
        ("front_fascia_diffuser_windows", "outer cover strip with diffuser reveal windows"),
        ("diffuser_N", "reference acrylic strips, not included in DXF cuts"),
    ]
    for i, (name, desc) in enumerate(parts):
        y = py + i * 46
        draw.rectangle((px, y, px + 22, y + 22), outline=red if i < 4 else blue, width=2)
        draw.text((px + 34, y), name, fill=(20, 20, 20), font=font)
        draw.text((px + 34, y + 17), desc, fill=gray, font=font)

    image.save(path)


def generate_drawer_files(
    specs: Iterable[DrawerSpec],
    output_dir: Path,
    *,
    bed_width: float = 400.0,
    bed_height: float = 400.0,
    render: bool = False,
) -> tuple[Path, ...]:
    written: list[Path] = []
    pngs: list[Path] = []
    for spec in specs:
        sheets = pack_sheets(
            f"drawer_{spec.name}",
            drawer_panels(spec),
            bed_width=bed_width,
            bed_height=bed_height,
        )
        for sheet in sheets:
            svg_path = output_dir / f"{sheet.name}.svg"
            dxf_path = output_dir / f"{sheet.name}.dxf"
            write_svg(sheet, svg_path)
            write_dxf(sheet, dxf_path)
            written.extend([svg_path, dxf_path])
            if render:
                png_path = output_dir / f"{sheet.name}.png"
                render_sheet_png(sheet, png_path)
                pngs.append(png_path)
                written.append(png_path)
    if render and pngs:
        contact_path = output_dir / "contact_sheet.png"
        render_contact_sheet(pngs, contact_path)
        written.append(contact_path)
    return tuple(written)


def generate_row_files(
    specs: Iterable[RowSpec],
    output_dir: Path,
    *,
    bed_width: float = 400.0,
    bed_height: float = 400.0,
    render: bool = False,
) -> tuple[Path, ...]:
    written: list[Path] = []
    pngs: list[Path] = []
    for spec in specs:
        sheets = pack_sheets(
            spec.name,
            row_panels(spec),
            bed_width=bed_width,
            bed_height=bed_height,
        )
        for sheet in sheets:
            svg_path = output_dir / f"{sheet.name}.svg"
            dxf_path = output_dir / f"{sheet.name}.dxf"
            write_svg(sheet, svg_path)
            write_dxf(sheet, dxf_path)
            written.extend([svg_path, dxf_path])
            if render:
                png_path = output_dir / f"{sheet.name}.png"
                render_sheet_png(sheet, png_path)
                pngs.append(png_path)
                written.append(png_path)
    if render and pngs:
        contact_path = output_dir / "row_contact_sheet.png"
        render_contact_sheet(pngs, contact_path)
        written.append(contact_path)
        for spec in specs:
            assembly_path = output_dir / f"{spec.name}_assembly_preview.png"
            render_row_assembly_preview(spec, assembly_path)
            written.append(assembly_path)
    return tuple(written)


def _parse_size(raw: str) -> DrawerSpec:
    parts = raw.lower().split("x")
    if len(parts) != 3:
        raise argparse.ArgumentTypeError("sizes must look like WIDTHxDEPTHxHEIGHT, e.g. 1x3x5")
    try:
        width, depth, height = (int(part) for part in parts)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("drawer size units must be integers") from exc
    return DrawerSpec(width_units=width, depth_units=depth, height_units=height)


def _parse_row_layout(raw: str) -> RowSpec:
    try:
        layout = tuple(int(part.strip()) for part in raw.split(",") if part.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError("row layout must be comma-separated integers, e.g. 1,2,3") from exc
    try:
        return RowSpec(row_layout=layout)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate laser-cut wood Gridfinity drawer DXF/SVG files.")
    parser.add_argument(
        "--size",
        action="append",
        type=_parse_size,
        dest="sizes",
        help="Drawer size as WIDTHxDEPTHxHEIGHT units. Can be repeated.",
    )
    parser.add_argument(
        "--row-layout",
        action="append",
        type=_parse_row_layout,
        dest="row_layouts",
        help="Fixed-width row drawer fill, e.g. 1,2,3 or 2,2,2. Can be repeated.",
    )
    parser.add_argument("--out", type=Path, default=Path("build/laser-drawers"))
    parser.add_argument("--bed-width", type=float, default=400.0)
    parser.add_argument("--bed-height", type=float, default=400.0)
    parser.add_argument("--render", action="store_true", help="Also render PNG previews and a contact sheet.")
    args = parser.parse_args(argv)

    written: tuple[Path, ...]
    if args.row_layouts:
        written = generate_row_files(
            args.row_layouts,
            args.out,
            bed_width=args.bed_width,
            bed_height=args.bed_height,
            render=args.render,
        )
    else:
        specs = args.sizes or [
            DrawerSpec(1, 3, 5),
            DrawerSpec(2, 3, 5),
            DrawerSpec(3, 3, 10),
        ]
        written = generate_drawer_files(
            specs,
            args.out,
            bed_width=args.bed_width,
            bed_height=args.bed_height,
            render=args.render,
        )
    for path in written:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
