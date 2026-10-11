from __future__ import annotations

import argparse
import time

from .config import LedConfig
from .controller import LedController


COLORS = {
    "off": (0, 0, 0),
    "red": (255, 0, 0),
    "green": (0, 255, 0),
    "blue": (0, 0, 255),
    "white": (255, 255, 255),
}


def main() -> None:
    parser = argparse.ArgumentParser(prog="searchable-storage-led")
    parser.add_argument("--config")
    subcommands = parser.add_subparsers(dest="command", required=True)

    fill = subcommands.add_parser("fill")
    fill.add_argument("color", choices=COLORS)
    fill.add_argument("--brightness", type=float)

    pixel = subcommands.add_parser("pixel")
    pixel.add_argument("index", type=int)
    pixel.add_argument("color", choices=COLORS)
    pixel.add_argument("--brightness", type=float)
    pixel.add_argument("--clear-others", action="store_true")

    chase = subcommands.add_parser("chase")
    chase.add_argument("--color", choices=COLORS, default="white")
    chase.add_argument("--seconds", type=float, default=0.5)
    chase.add_argument("--brightness", type=float)

    subcommands.add_parser("clear")

    args = parser.parse_args()
    cfg = LedConfig.from_path(args.config)
    controller = LedController(cfg)
    try:
        if args.command == "fill":
            controller.fill(COLORS[args.color], brightness=args.brightness)
        elif args.command == "pixel":
            controller.set_pixel(
                args.index,
                COLORS[args.color],
                clear_others=args.clear_others,
                brightness=args.brightness,
            )
        elif args.command == "chase":
            for index in range(cfg.pixel_count):
                controller.set_pixel(
                    index,
                    COLORS[args.color],
                    clear_others=True,
                    brightness=args.brightness,
                )
                time.sleep(args.seconds)
            controller.clear()
        elif args.command == "clear":
            controller.clear()
    finally:
        controller.close()


if __name__ == "__main__":
    main()
