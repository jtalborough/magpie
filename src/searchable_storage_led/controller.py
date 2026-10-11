from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Iterable
from urllib import request
from urllib.error import URLError
import json

from .config import LedConfig


RGB = tuple[int, int, int]


@dataclass(frozen=True)
class PixelUpdate:
    index: int
    color: RGB


class LedController:
    def __init__(self, config: LedConfig, *, writer: object | None = None) -> None:
        self.config = config.validated()
        self._lock = threading.Lock()
        self._pixels: list[RGB] = [(0, 0, 0)] * self.config.pixel_count
        self._writer = writer or create_writer(self.config)
        self._clear_timer: threading.Timer | None = None
        self._clear_token = 0

    @property
    def pixels(self) -> list[RGB]:
        with self._lock:
            return list(self._pixels)

    def close(self) -> None:
        with self._lock:
            self._cancel_clear_locked()
        close = getattr(self._writer, "close", None)
        if callable(close):
            close()

    def clear(self) -> None:
        self.fill((0, 0, 0))

    def fill(self, color: RGB, *, brightness: float | None = None) -> None:
        normalized = normalize_rgb(color, brightness, self.config.default_brightness)
        with self._lock:
            self._cancel_clear_locked()
            self._pixels = [normalized] * self.config.pixel_count
            self._show_locked()

    def set_pixel(
        self,
        index: int,
        color: RGB,
        *,
        clear_others: bool = False,
        brightness: float | None = None,
    ) -> None:
        self.set_pixels(
            [PixelUpdate(index=index, color=color)],
            clear_others=clear_others,
            brightness=brightness,
        )

    def set_pixels(
        self,
        updates: Iterable[PixelUpdate],
        *,
        clear_others: bool = False,
        brightness: float | None = None,
    ) -> None:
        self._set_pixels(
            updates,
            clear_others=clear_others,
            brightness=brightness,
            seconds=None,
        )

    def set_pixels_for(
        self,
        updates: Iterable[PixelUpdate],
        *,
        seconds: float,
        clear_others: bool = False,
        brightness: float | None = None,
    ) -> None:
        if seconds <= 0:
            raise ValueError("seconds must be positive")
        self._set_pixels(
            updates,
            clear_others=clear_others,
            brightness=brightness,
            seconds=seconds,
        )

    def _set_pixels(
        self,
        updates: Iterable[PixelUpdate],
        *,
        clear_others: bool,
        brightness: float | None,
        seconds: float | None,
    ) -> None:
        with self._lock:
            self._cancel_clear_locked()
            pixels = (
                [(0, 0, 0)] * self.config.pixel_count
                if clear_others
                else list(self._pixels)
            )
            for update in updates:
                self._validate_index(update.index)
                pixels[update.index] = normalize_rgb(
                    update.color, brightness, self.config.default_brightness
                )
            self._pixels = pixels
            if seconds is not None:
                self._schedule_clear_locked(seconds)
            self._show_locked()

    def flash_pixel(
        self,
        index: int,
        color: RGB,
        *,
        seconds: float = 1.0,
        brightness: float | None = None,
    ) -> None:
        self.set_pixels_for(
            [PixelUpdate(index=index, color=color)],
            seconds=seconds,
            clear_others=True,
            brightness=brightness,
        )

    def pulse_pixels(
        self,
        indexes: Iterable[int],
        color: RGB,
        *,
        seconds: float,
        brightness: float | None = None,
        pulses: int = 2,
        on_seconds: float = 0.3,
        off_seconds: float = 0.15,
    ) -> None:
        """Pulse the destination, then leave it lit for the requested timeout."""
        targets = sorted(set(indexes))
        if not targets:
            raise ValueError("at least one destination pixel is required")
        if seconds <= 0:
            raise ValueError("seconds must be positive")
        if pulses < 1:
            raise ValueError("pulses must be positive")
        if on_seconds < 0 or off_seconds < 0:
            raise ValueError("pulse timing must not be negative")
        for index in targets:
            self._validate_index(index)

        with self._lock:
            self._cancel_clear_locked()
            for _ in range(pulses):
                pixels = [(0, 0, 0)] * self.config.pixel_count
                for index in targets:
                    pixels[index] = normalize_rgb(
                        color,
                        brightness,
                        self.config.default_brightness,
                    )
                self._pixels = pixels
                self._show_locked()
                if on_seconds:
                    time.sleep(on_seconds)
                self._pixels = [(0, 0, 0)] * self.config.pixel_count
                self._show_locked()
                if off_seconds:
                    time.sleep(off_seconds)

            self._pixels = [(0, 0, 0)] * self.config.pixel_count
            for index in targets:
                self._pixels[index] = normalize_rgb(
                    color,
                    brightness,
                    self.config.default_brightness,
                )
            self._schedule_clear_locked(seconds)
            self._show_locked()

    def _validate_index(self, index: int) -> None:
        if not 0 <= index < self.config.pixel_count:
            raise IndexError(
                f"pixel index {index} is outside 0..{self.config.pixel_count - 1}"
            )

    def _show_locked(self) -> None:
        self._writer.write_pixels(self._pixels, self.config)

    def _schedule_clear_locked(self, seconds: float) -> None:
        self._clear_token += 1
        token = self._clear_token
        timer = threading.Timer(seconds, self._clear_if_current, args=(token,))
        timer.daemon = True
        self._clear_timer = timer
        timer.start()

    def _cancel_clear_locked(self) -> None:
        self._clear_token += 1
        if self._clear_timer is not None:
            self._clear_timer.cancel()
            self._clear_timer = None

    def _clear_if_current(self, token: int) -> None:
        with self._lock:
            if token != self._clear_token:
                return
            self._clear_timer = None
            self._pixels = [(0, 0, 0)] * self.config.pixel_count
            try:
                self._show_locked()
            except Exception:
                return


class WorkspaceLedController:
    """Independent queued LED state for each controller in the bound workspace."""

    def __init__(self, config: LedConfig, *, writer: object) -> None:
        self.config = config.validated()
        self._writer = writer
        self._lock = threading.RLock()
        self._pixels: dict[tuple[str, str], list[RGB]] = {}
        self._clear_timers: dict[tuple[str, str], threading.Timer] = {}
        self._clear_tokens: dict[tuple[str, str], int] = {}

    @property
    def pixels(self) -> list[RGB]:
        return []

    def close(self) -> None:
        with self._lock:
            for timer in self._clear_timers.values():
                timer.cancel()
            self._clear_timers.clear()

    def clear(
        self, *, target: str = "", pixel_count: int = 0, workspace_id: str = ""
    ) -> None:
        key, clean_count = self._target(workspace_id, target, pixel_count)
        with self._lock:
            self._cancel_clear_locked(key)
            self._pixels[key] = [(0, 0, 0)] * clean_count
            self._show_locked(key)

    def set_pixels(
        self,
        updates: Iterable[PixelUpdate],
        *,
        clear_others: bool = False,
        brightness: float | None = None,
        target: str = "",
        pixel_count: int = 0,
        workspace_id: str = "",
    ) -> None:
        self._set_pixels(
            updates,
            clear_others=clear_others,
            brightness=brightness,
            seconds=None,
            target=target,
            pixel_count=pixel_count,
            workspace_id=workspace_id,
        )

    def set_pixels_for(
        self,
        updates: Iterable[PixelUpdate],
        *,
        seconds: float,
        clear_others: bool = False,
        brightness: float | None = None,
        target: str = "",
        pixel_count: int = 0,
        workspace_id: str = "",
    ) -> None:
        if seconds <= 0:
            raise ValueError("seconds must be positive")
        self._set_pixels(
            updates,
            clear_others=clear_others,
            brightness=brightness,
            seconds=seconds,
            target=target,
            pixel_count=pixel_count,
            workspace_id=workspace_id,
        )

    def pulse_pixels(
        self,
        indexes: Iterable[int],
        color: RGB,
        *,
        seconds: float,
        brightness: float | None = None,
        target: str = "",
        pixel_count: int = 0,
        workspace_id: str = "",
        **_: object,
    ) -> None:
        self.set_pixels_for(
            [PixelUpdate(index=index, color=color) for index in sorted(set(indexes))],
            clear_others=True,
            brightness=brightness,
            seconds=seconds,
            target=target,
            pixel_count=pixel_count,
            workspace_id=workspace_id,
        )

    def _set_pixels(
        self,
        updates: Iterable[PixelUpdate],
        *,
        clear_others: bool,
        brightness: float | None,
        seconds: float | None,
        target: str,
        pixel_count: int,
        workspace_id: str,
    ) -> None:
        key, clean_count = self._target(workspace_id, target, pixel_count)
        with self._lock:
            self._cancel_clear_locked(key)
            current = self._pixels.get(key, [])
            pixels = (
                [(0, 0, 0)] * clean_count
                if clear_others or len(current) != clean_count
                else list(current)
            )
            for update in updates:
                if not 0 <= update.index < clean_count:
                    raise IndexError(
                        f"pixel index {update.index} is outside 0..{clean_count - 1}"
                    )
                pixels[update.index] = normalize_rgb(
                    update.color,
                    brightness,
                    self.config.default_brightness,
                )
            self._pixels[key] = pixels
            if seconds is not None:
                self._schedule_clear_locked(key, clean_count, seconds)
            self._show_locked(key)

    @staticmethod
    def _target(
        workspace_id: str, target: str, pixel_count: int
    ) -> tuple[tuple[str, str], int]:
        clean_workspace = workspace_id.strip()
        if not clean_workspace or clean_workspace == "request-scoped":
            raise ValueError("a concrete workspace is required for workspace lighting")
        clean_target = target.strip()
        if not clean_target:
            raise ValueError("location light_target is required for workspace lighting")
        clean_count = int(pixel_count)
        if clean_count <= 0 or clean_count > 10000:
            raise ValueError("workspace controller pixel_count must be between 1 and 10000")
        return (clean_workspace, clean_target), clean_count

    def _show_locked(self, key: tuple[str, str]) -> None:
        workspace_id, target = key
        pixels = self._pixels[key]
        self._writer.write_pixels(
            target,
            pixels,
            pixel_count=len(pixels),
            workspace_id=workspace_id,
        )

    def _schedule_clear_locked(
        self, key: tuple[str, str], pixel_count: int, seconds: float
    ) -> None:
        token = self._clear_tokens.get(key, 0) + 1
        self._clear_tokens[key] = token
        timer = threading.Timer(
            seconds,
            self._clear_if_current,
            args=(key, pixel_count, token),
        )
        timer.daemon = True
        self._clear_timers[key] = timer
        timer.start()

    def _cancel_clear_locked(self, key: tuple[str, str]) -> None:
        self._clear_tokens[key] = self._clear_tokens.get(key, 0) + 1
        timer = self._clear_timers.pop(key, None)
        if timer is not None:
            timer.cancel()

    def _clear_if_current(
        self, key: tuple[str, str], pixel_count: int, token: int
    ) -> None:
        with self._lock:
            if token != self._clear_tokens.get(key):
                return
            self._clear_timers.pop(key, None)
            self._pixels[key] = [(0, 0, 0)] * pixel_count
            try:
                self._show_locked(key)
            except Exception:
                return


def create_writer(config: LedConfig) -> object:
    if config.backend == "none":
        return NullWriter()
    if config.backend == "wled":
        assert config.wled_url is not None
        return WledWriter(config.wled_url)
    return Pi5PioWriter(config.gpio_pin)


class NullWriter:
    """Hardware-neutral writer for the central/containerized Magpie service."""

    def write_pixels(self, pixels: list[RGB], config: LedConfig) -> None:
        return None


class Pi5PioWriter:
    def __init__(self, gpio_pin: int) -> None:
        import board
        from adafruit_raspberry_pi5_neopixel_write import free_pio, neopixel_write

        self._pin = getattr(board, f"D{gpio_pin}")
        self._free_pio = free_pio
        self._neopixel_write = neopixel_write

    def write_pixels(self, pixels: Iterable[RGB], config: LedConfig) -> None:
        self._neopixel_write(self._pin, encode_pixels(pixels, config.color_order))

    def close(self) -> None:
        self._free_pio()


class WledWriter:
    def __init__(self, base_url: str) -> None:
        self._state_url = base_url.rstrip("/") + "/json/state"

    def write_pixels(self, pixels: Iterable[RGB], config: LedConfig) -> None:
        pixel_list = list(pixels)
        segment = {
            "id": 0,
            "start": config.wled_start,
            "stop": config.wled_start + config.pixel_count,
            "on": True,
            "bri": 255,
            "fx": 0,
            "i": [rgb_to_hex(pixel) for pixel in pixel_list],
        }
        payload = {
            "on": True,
            "bri": 255,
            "transition": 0,
            "mainseg": 0,
            "seg": [{"id": index, "stop": 0} for index in range(1, 32)] + [segment],
        }
        body = json.dumps(payload).encode("utf-8")
        req = request.Request(
            self._state_url,
            data=body,
            headers={"content-type": "application/json"},
            method="POST",
        )
        last_error: Exception | None = None
        for _ in range(3):
            try:
                with request.urlopen(req, timeout=8) as response:
                    response.read()
                return
            except URLError as exc:
                last_error = exc
                time.sleep(0.2)
        if last_error is not None:
            raise last_error

    def close(self) -> None:
        return None


def normalize_rgb(
    color: RGB, brightness: float | None, default_brightness: float
) -> RGB:
    level = default_brightness if brightness is None else brightness
    if not 0 <= level <= 1:
        raise ValueError("brightness must be between 0 and 1")
    if len(color) != 3:
        raise ValueError("color must be a 3-item RGB tuple")
    normalized = []
    for value in color:
        if not 0 <= int(value) <= 255:
            raise ValueError("RGB values must be between 0 and 255")
        normalized.append(round(int(value) * level))
    return tuple(normalized)  # type: ignore[return-value]


def encode_pixels(pixels: Iterable[RGB], color_order: str) -> bytearray:
    channels = {"R": 0, "G": 1, "B": 2}
    order = [channels[channel] for channel in color_order.upper()]
    encoded = bytearray()
    for pixel in pixels:
        if len(pixel) != 3:
            raise ValueError("each pixel must be a 3-item RGB tuple")
        encoded.extend(int(pixel[channel]) for channel in order)
    return encoded


def rgb_to_hex(pixel: RGB) -> str:
    return "".join(f"{int(channel):02X}" for channel in pixel)
