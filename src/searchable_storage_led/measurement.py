from __future__ import annotations

import json
from typing import Callable

from .llm_client import LlmClient, LlmError
from .openai_assistant import photo_data_url
from .store import PhotoRecord


CALIBRATION_REFERENCE = {
    "id": "magpie-100mm-grid-v1",
    "outer_width_mm": 100,
    "outer_height_mm": 100,
    "cell_size_mm": 10,
}

MEASUREMENT_INSTRUCTIONS = (
    "Measure one physical item using the Magpie calibration target visible in the photo. "
    "The target is a 100 mm by 100 mm black outer square divided into 10 mm cells. "
    "Correct for perspective using the four outer corners. The target edges are labeled from "
    "0 to 100 mm. Measure the item itself, not its shadow or the target. First report the item's "
    "left_mm, top_mm, right_mm, and bottom_mm boundaries in target coordinates. Count every "
    "10 mm interval between the two item edges, including grid lines hidden by the item. Then "
    "report length_mm as the longer boundary span and width_mm as the shorter boundary span. "
    "Double-check that the dimensions equal the differences between the reported boundaries. "
    "Only report height_mm when a side view or other clear evidence supports it. If all four "
    "target corners are not visible, the item is not on the same plane, "
    "or the measurement is otherwise unreliable, set reference_detected false and return empty "
    "dimensions. Never guess from object type. Return only JSON with reference_detected, "
    "bounds_mm, dimensions, confidence from 0 to 1, and reason."
)


def measure_calibrated_item(
    *,
    item: str,
    photos: list[PhotoRecord],
    client: LlmClient,
    photo_loader: Callable[[int], bytes],
) -> dict[str, object]:
    if not photos:
        raise ValueError("at least one photo is required")
    images = [
        data_url
        for photo in photos[:2]
        if (data_url := photo_data_url(photo, photo_loader)) is not None
    ]
    if not images:
        raise LlmError("no usable measurement photos were loaded")
    raw = client.complete_json(
        system=MEASUREMENT_INSTRUCTIONS,
        user_text=json.dumps(
            {
                "item": item.strip() or "item",
                "reference": CALIBRATION_REFERENCE,
            }
        ),
        images=images,
    )
    return normalize_measurement(raw)


def normalize_measurement(raw: dict[str, object]) -> dict[str, object]:
    detected = raw.get("reference_detected") is True
    bounds = _measurement_bounds(raw.get("bounds_mm")) if detected else {}
    dimensions_raw = raw.get("dimensions")
    dimensions: dict[str, float] = {}
    if detected and isinstance(dimensions_raw, dict):
        for key in ("length_mm", "width_mm", "height_mm"):
            value = _positive_number(dimensions_raw.get(key))
            if value is not None:
                dimensions[key] = round(value, 1)
    if bounds:
        horizontal = abs(bounds["right_mm"] - bounds["left_mm"])
        vertical = abs(bounds["bottom_mm"] - bounds["top_mm"])
        dimensions["length_mm"] = round(max(horizontal, vertical), 1)
        dimensions["width_mm"] = round(min(horizontal, vertical), 1)
    if len(dimensions) < 2:
        detected = False
        bounds = {}
        dimensions = {}
    confidence = _number(raw.get("confidence"))
    confidence = max(0.0, min(1.0, confidence if confidence is not None else 0.0))
    confidence = min(confidence, 0.9 if bounds else 0.7)
    reason = str(raw.get("reason") or "").strip()
    if not detected and not reason:
        reason = "Calibration grid was not detected reliably."
    return {
        "reference_id": CALIBRATION_REFERENCE["id"],
        "reference_detected": detected,
        "bounds_mm": bounds,
        "dimensions": dimensions,
        "confidence": round(confidence, 2),
        "reason": reason,
    }


def render_calibration_grid_pdf() -> bytes:
    points_per_mm = 72 / 25.4
    page_width, page_height = 612.0, 792.0
    grid_size = 100 * points_per_mm
    cell = 10 * points_per_mm
    left = (page_width - grid_size) / 2
    bottom = (page_height - grid_size) / 2
    content = ["0 G", "0 g"]
    for index in range(11):
        offset = index * cell
        width = 1.6 if index in {0, 5, 10} else 0.45
        content.extend(
            [
                f"{width:.2f} w",
                f"{left + offset:.2f} {bottom:.2f} m {left + offset:.2f} {bottom + grid_size:.2f} l S",
                f"{left:.2f} {bottom + offset:.2f} m {left + grid_size:.2f} {bottom + offset:.2f} l S",
            ]
        )
    marker = 5 * points_per_mm
    for x, y in (
        (left, bottom),
        (left + grid_size - marker, bottom),
        (left, bottom + grid_size - marker),
        (left + grid_size - marker, bottom + grid_size - marker),
    ):
        content.append(f"{x:.2f} {y:.2f} {marker:.2f} {marker:.2f} re f")
    for index in range(11):
        value = index * 10
        x = left + index * cell
        y = bottom + index * cell
        content.append(f"BT /F1 6 Tf {x - 4:.2f} {bottom + grid_size + 8:.2f} Td ({value}) Tj ET")
        content.append(f"BT /F1 6 Tf {left - 20:.2f} {y - 2:.2f} Td ({value}) Tj ET")
    content.extend(
        [
            "BT /F1 16 Tf 72 720 Td (Magpie Measurement Grid) Tj ET",
            "BT /F1 10 Tf 72 700 Td (100 mm x 100 mm - 10 mm cells) Tj ET",
            "BT /F1 9 Tf 72 680 Td (Print at Actual Size / 100 percent. Do not Fit to Page.) Tj ET",
            f"BT /F1 8 Tf {left:.2f} {bottom - 18:.2f} Td (Reference: magpie-100mm-grid-v1) Tj ET",
        ]
    )
    stream = "\n".join(content).encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {page_width:.0f} {page_height:.0f}] "
            "/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        ).encode("ascii"),
        b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode("ascii"))
        output.extend(obj)
        output.extend(b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    output.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(output)


def _number(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _positive_number(value: object) -> float | None:
    number = _number(value)
    if number is None or number <= 0 or number > 5000:
        return None
    return number


def _measurement_bounds(value: object) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    bounds: dict[str, float] = {}
    for key in ("left_mm", "top_mm", "right_mm", "bottom_mm"):
        number = _number(value.get(key))
        if number is None or number < -5 or number > 105:
            return {}
        bounds[key] = round(number, 1)
    if bounds["left_mm"] == bounds["right_mm"] or bounds["top_mm"] == bounds["bottom_mm"]:
        return {}
    return bounds
