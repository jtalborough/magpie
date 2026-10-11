from searchable_storage_led.layout import StorageLayout


def test_layout_generates_expected_bin_count_and_origin() -> None:
    layout = StorageLayout.from_path("config/storage-layout.json")

    assert len(layout.bins) == 128
    assert layout.get("B001").led_indexes == [84]
    assert layout.get("B014").led_indexes == [97]
    assert layout.get("B015").led_indexes == [98]


def test_gpio16_strip_starts_at_upper_small_row_far_right() -> None:
    layout = StorageLayout.from_path("config/storage-layout.json")

    assert layout.get("B112").led_indexes == [0]
    assert layout.get("B111").led_indexes == [1]
    assert layout.get("B100").led_indexes == [12]
    assert layout.get("B099").led_indexes == [13]
    assert layout.get("B116").led_indexes == [14, 15, 16, 17]
    assert layout.get("B113").led_indexes == [25, 26, 27]


def test_layout_uses_unique_contiguous_bin_leds() -> None:
    layout = StorageLayout.from_path("config/storage-layout.json")
    indexes = sorted(index for bin in layout.bins for index in bin.led_indexes)

    assert indexes == list(range(182))


def test_large_bins_span_multiple_leds() -> None:
    layout = StorageLayout.from_path("config/storage-layout.json")

    assert layout.get("B128").size == "large"
    assert len(layout.get("B128").led_indexes) == 7


def test_layout_exposes_relative_size_profiles() -> None:
    layout = StorageLayout.from_path("config/storage-layout.json")

    assert layout.size_profiles["small"]["typical_span_units"] == 1
    assert layout.size_profiles["medium"]["typical_span_units"] == "3-4"
    assert layout.size_profiles["large"]["typical_span_units"] == 7
