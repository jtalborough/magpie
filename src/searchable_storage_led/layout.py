from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class Bin:
    id: str
    number: int
    size: str
    row: int
    col: int
    col_span: int
    led_indexes: list[int]

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


class StorageLayout:
    def __init__(self, bins: list[Bin], *, columns: int, name: str) -> None:
        self.bins = bins
        self.columns = columns
        self.name = name
        self._by_id = {bin.id: bin for bin in bins}
        self.size_profiles = size_profiles_for_bins(bins)

    @classmethod
    def from_path(cls, path: str | Path) -> "StorageLayout":
        source = Path(path)
        with source.open("r", encoding="utf-8") as fh:
            raw = json.load(fh)
        return cls.from_spec(raw)

    @classmethod
    def from_spec(cls, raw: dict[str, object]) -> "StorageLayout":
        columns = int(raw["columns"])
        rows = raw["rows_bottom_to_top"]
        if not isinstance(rows, list):
            raise ValueError("rows_bottom_to_top must be a list")

        bins: list[Bin] = []
        number = 1
        for row_index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise ValueError("row entries must be objects")
            size = str(row["size"])
            spans = [int(span) for span in row["spans"]]
            if sum(spans) != columns:
                raise ValueError(f"row {row_index} spans must add to {columns}")
            if int(row["bin_count"]) != len(spans):
                raise ValueError(f"row {row_index} bin_count does not match spans")

            col = 0
            row_bins: list[Bin] = []
            for span in spans:
                led_indexes = [
                    led_index_for(row_index, col + offset, columns)
                    for offset in range(span)
                ]
                row_bins.append(
                    Bin(
                        id=f"B{number:03d}",
                        number=number,
                        size=size,
                        row=row_index,
                        col=col,
                        col_span=span,
                        led_indexes=led_indexes,
                    )
                )
                col += span

            # Human numbering follows the actual serpentine LED path from bottom-right.
            if row_index % 2 == 0:
                row_bins = list(reversed(row_bins))
            for bin in row_bins:
                bins.append(
                    Bin(
                        id=f"B{number:03d}",
                        number=number,
                        size=bin.size,
                        row=bin.row,
                        col=bin.col,
                        col_span=bin.col_span,
                        led_indexes=bin.led_indexes,
                    )
                )
                number += 1

        remap = raw.get("led_remap")
        if isinstance(remap, dict):
            bins = [remap_bin_leds(bin, remap) for bin in bins]

        return cls(bins, columns=columns, name=str(raw.get("name", "storage")))

    def get(self, bin_id: str) -> Bin | None:
        normalized = normalize_bin_id(bin_id)
        return self._by_id.get(normalized)

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "columns": self.columns,
            "size_profiles": self.size_profiles,
            "bins": [bin.as_dict() for bin in self.bins],
        }


def led_index_for(row: int, col: int, columns: int) -> int:
    base = row * columns
    if row % 2 == 0:
        return base + (columns - 1 - col)
    return base + col


def normalize_bin_id(value: str) -> str:
    raw = value.strip().upper()
    if raw.startswith("B") and raw[1:].isdigit():
        return f"B{int(raw[1:]):03d}"
    if raw.isdigit():
        return f"B{int(raw):03d}"
    return raw


def normalize_inventory_location_id(value: str) -> str:
    """Canonicalize legacy cabinet bins without rewriting graph location IDs."""
    raw = value.strip()
    upper = raw.upper()
    if raw.isdigit() or (upper.startswith("B") and upper[1:].isdigit()):
        return normalize_bin_id(raw)
    return raw


def remap_bin_leds(bin: Bin, remap: dict[str, object]) -> Bin:
    return Bin(
        id=bin.id,
        number=bin.number,
        size=bin.size,
        row=bin.row,
        col=bin.col,
        col_span=bin.col_span,
        led_indexes=[remap_led_index(index, remap) for index in bin.led_indexes],
    )


def remap_led_index(index: int, remap: dict[str, object]) -> int:
    if remap.get("mode") != "sections":
        return index
    sections = remap.get("sections", [])
    if not isinstance(sections, list):
        return index
    for section in sections:
        if not isinstance(section, dict):
            continue
        source_start = int(section["source_start"])
        length = int(section["length"])
        target_start = int(section["target_start"])
        if source_start <= index < source_start + length:
            if bool(section.get("reverse")):
                return target_start + (source_start + length - 1 - index)
            return target_start + (index - source_start)
    return index


def size_profiles_for_bins(bins: list[Bin]) -> dict[str, dict[str, object]]:
    profiles: dict[str, dict[str, object]] = {}
    for bin in bins:
        profile = profiles.setdefault(
            bin.size,
            {
                "count": 0,
                "min_span_units": bin.col_span,
                "max_span_units": bin.col_span,
                "typical_span_units": bin.col_span,
            },
        )
        profile["count"] = int(profile["count"]) + 1
        profile["min_span_units"] = min(int(profile["min_span_units"]), bin.col_span)
        profile["max_span_units"] = max(int(profile["max_span_units"]), bin.col_span)

    for profile in profiles.values():
        min_span = int(profile["min_span_units"])
        max_span = int(profile["max_span_units"])
        profile["typical_span_units"] = (
            min_span if min_span == max_span else f"{min_span}-{max_span}"
        )
        profile["relative_capacity_note"] = capacity_note(min_span, max_span)
    return profiles


def capacity_note(min_span: int, max_span: int) -> str:
    if max_span <= 1:
        return "single-column drawer; use for loose tiny parts and compact items only"
    if max_span <= 4:
        return "multi-column drawer; use for packages, kits, bundles, and item groups that exceed one small drawer"
    return "wide drawer; use for bulky boxes, rolls, spools, long items, or anything that would overflow a medium drawer"
