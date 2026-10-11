import json

from searchable_storage_led.measurement import (
    measure_calibrated_item,
    normalize_measurement,
    render_calibration_grid_pdf,
)
from searchable_storage_led.store import PhotoRecord


class MeasurementClient:
    def __init__(self, response):
        self.response = response
        self.call = None

    def complete_json(self, *, system, user_text, images=None):
        self.call = {"system": system, "user_text": user_text, "images": images}
        return self.response


def test_calibrated_measurement_sends_repository_photo_and_normalizes_result() -> None:
    client = MeasurementClient(
        {
            "reference_detected": True,
            "bounds_mm": {"left_mm": 10, "top_mm": 20, "right_mm": 91.24, "bottom_mm": 42.96},
            "dimensions": {"length_mm": 81.24, "width_mm": 22.96},
            "confidence": 0.87,
            "reason": "All reference corners visible.",
        }
    )
    photo = PhotoRecord(7, "part.jpg", "image/jpeg", "remote/path.jpg", "/photos/7")

    result = measure_calibrated_item(
        item="blue pry tool",
        photos=[photo],
        client=client,
        photo_loader=lambda photo_id: b"photo" if photo_id == 7 else b"",
    )

    assert result["dimensions"] == {"length_mm": 81.2, "width_mm": 23.0}
    assert result["bounds_mm"] == {
        "left_mm": 10.0,
        "top_mm": 20.0,
        "right_mm": 91.2,
        "bottom_mm": 43.0,
    }
    assert result["confidence"] == 0.87
    assert client.call["images"][0].startswith("data:image/jpeg;base64,")
    assert json.loads(client.call["user_text"])["reference"]["cell_size_mm"] == 10


def test_measurement_rejects_unreliable_or_incomplete_reference() -> None:
    result = normalize_measurement(
        {
            "reference_detected": True,
            "bounds_mm": {},
            "dimensions": {"length_mm": 50},
            "confidence": 0.4,
            "reason": "One corner was hidden.",
        }
    )

    assert result["reference_detected"] is False
    assert result["dimensions"] == {}


def test_grid_bounds_override_inconsistent_model_dimensions() -> None:
    result = normalize_measurement(
        {
            "reference_detected": True,
            "bounds_mm": {"left_mm": 20, "top_mm": 40, "right_mm": 80, "bottom_mm": 60},
            "dimensions": {"length_mm": 50, "width_mm": 20},
            "confidence": 1,
            "reason": "Measured against labeled edges.",
        }
    )

    assert result["dimensions"] == {"length_mm": 60.0, "width_mm": 20.0}
    assert result["confidence"] == 0.9


def test_calibration_grid_pdf_has_exact_reference_metadata() -> None:
    pdf = render_calibration_grid_pdf()

    assert pdf.startswith(b"%PDF-1.4")
    assert b"100 mm x 100 mm - 10 mm cells" in pdf
    assert b"magpie-100mm-grid-v1" in pdf
    assert b"(100) Tj" in pdf
    assert pdf.endswith(b"%%EOF\n")
