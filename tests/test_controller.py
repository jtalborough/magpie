import time

from searchable_storage_led.config import LedConfig
from searchable_storage_led.controller import (
    LedController,
    PixelUpdate,
    WorkspaceLedController,
    encode_pixels,
    rgb_to_hex,
)


class FakeWriter:
    def __init__(self) -> None:
        self.buffers: list[bytearray] = []
        self.closed = False

    def write_pixels(self, pixels, config) -> None:
        self.buffers.append(encode_pixels(pixels, config.color_order))

    def close(self) -> None:
        self.closed = True


class FakeWorkspaceWriter:
    def __init__(self) -> None:
        self.frames = []

    def write_pixels(
        self, device_id, pixels, *, pixel_count, workspace_id=""
    ) -> None:
        self.frames.append((workspace_id, device_id, list(pixels), pixel_count))


def test_encode_rbg_order() -> None:
    assert encode_pixels([(1, 2, 3)], "RBG") == bytearray([1, 3, 2])


def test_set_pixel_sends_full_frame_with_clear_others() -> None:
    writer = FakeWriter()
    controller = LedController(
        LedConfig(pixel_count=3, color_order="RBG", default_brightness=1.0),
        writer=writer,
    )

    controller.set_pixel(1, (10, 20, 30), clear_others=True)

    assert writer.buffers[-1] == bytearray([0, 0, 0, 10, 30, 20, 0, 0, 0])


def test_brightness_scales_pixels() -> None:
    writer = FakeWriter()
    controller = LedController(
        LedConfig(pixel_count=1, color_order="RBG", default_brightness=0.5),
        writer=writer,
    )

    controller.fill((100, 50, 10))

    assert writer.buffers[-1] == bytearray([50, 5, 25])


def test_rgb_to_hex() -> None:
    assert rgb_to_hex((255, 16, 0)) == "FF1000"


def test_set_pixels_for_clears_after_timeout() -> None:
    writer = FakeWriter()
    controller = LedController(
        LedConfig(pixel_count=3, color_order="RBG", default_brightness=1.0),
        writer=writer,
    )

    controller.set_pixels_for(
        [PixelUpdate(index=1, color=(10, 20, 30))],
        clear_others=True,
        brightness=1.0,
        seconds=0.01,
    )
    time.sleep(0.05)

    assert controller.pixels == [(0, 0, 0), (0, 0, 0), (0, 0, 0)]
    assert writer.buffers[-1] == bytearray([0, 0, 0, 0, 0, 0, 0, 0, 0])


def test_new_light_command_cancels_previous_timeout() -> None:
    writer = FakeWriter()
    controller = LedController(
        LedConfig(pixel_count=3, color_order="RBG", default_brightness=1.0),
        writer=writer,
    )

    controller.set_pixels_for(
        [PixelUpdate(index=1, color=(10, 20, 30))],
        clear_others=True,
        brightness=1.0,
        seconds=0.01,
    )
    controller.set_pixel(2, (1, 2, 3), clear_others=True)
    time.sleep(0.05)

    assert controller.pixels == [(0, 0, 0), (0, 0, 0), (1, 2, 3)]


def test_pulse_pixels_blinks_destination_and_settles_on_target() -> None:
    writer = FakeWriter()
    controller = LedController(
        LedConfig(pixel_count=8, color_order="RGB", default_brightness=1.0),
        writer=writer,
    )

    controller.pulse_pixels(
        [6],
        (40, 255, 120),
        seconds=1,
        brightness=1.0,
        pulses=2,
        on_seconds=0,
        off_seconds=0,
    )

    frames = [list(buffer) for buffer in writer.buffers]
    assert frames[0][18:21] == [40, 255, 120]
    assert not any(frames[1])
    assert frames[2][18:21] == [40, 255, 120]
    assert not any(frames[3])
    assert frames[-1][18:21] == [40, 255, 120]
    assert controller.pixels[6] == (40, 255, 120)
    assert sum(any(pixel) for pixel in controller.pixels) == 1


def test_pulse_pixels_rejects_invalid_destination() -> None:
    controller = LedController(
        LedConfig(pixel_count=3, default_brightness=1.0),
        writer=FakeWriter(),
    )

    try:
        controller.pulse_pixels([3], (0, 255, 0), seconds=1, on_seconds=0, off_seconds=0)
    except IndexError as exc:
        assert "outside 0..2" in str(exc)
    else:
        raise AssertionError("expected an invalid destination to fail")


def test_workspace_controller_isolates_overlapping_pixels_by_workspace_and_target() -> None:
    writer = FakeWorkspaceWriter()
    controller = WorkspaceLedController(
        LedConfig(pixel_count=0, backend="none"), writer=writer
    )

    controller.set_pixels(
        [PixelUpdate(index=0, color=(255, 0, 0))],
        target="cabinet-a",
        pixel_count=2,
        workspace_id="workspace-a",
        clear_others=True,
        brightness=1.0,
    )
    controller.set_pixels(
        [PixelUpdate(index=0, color=(0, 0, 255))],
        target="cabinet-a",
        pixel_count=2,
        workspace_id="workspace-b",
        clear_others=True,
        brightness=1.0,
    )
    controller.set_pixels(
        [PixelUpdate(index=0, color=(0, 255, 0))],
        target="cabinet-b",
        pixel_count=1,
        workspace_id="workspace-a",
        clear_others=True,
        brightness=1.0,
    )

    assert writer.frames == [
        ("workspace-a", "cabinet-a", [(255, 0, 0), (0, 0, 0)], 2),
        ("workspace-b", "cabinet-a", [(0, 0, 255), (0, 0, 0)], 2),
        ("workspace-a", "cabinet-b", [(0, 255, 0)], 1),
    ]


def test_workspace_controller_timeout_clears_original_workspace() -> None:
    writer = FakeWorkspaceWriter()
    controller = WorkspaceLedController(
        LedConfig(pixel_count=0, backend="none"), writer=writer
    )

    controller.set_pixels_for(
        [PixelUpdate(index=1, color=(10, 20, 30))],
        target="cabinet-a",
        pixel_count=2,
        workspace_id="workspace-a",
        clear_others=True,
        brightness=1.0,
        seconds=0.01,
    )
    time.sleep(0.05)

    assert writer.frames[-1] == (
        "workspace-a",
        "cabinet-a",
        [(0, 0, 0), (0, 0, 0)],
        2,
    )
