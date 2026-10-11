#!/usr/bin/env python3
"""Build a Magpie wheel from a clean, current-source staging directory."""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]


def stage_source(root: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for filename in ("pyproject.toml", "README.md"):
        shutil.copy2(root / filename, destination / filename)
    package_source = root / "src/searchable_storage_led"
    package_destination = destination / "src/searchable_storage_led"
    package_destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(package_source, package_destination)


def build_wheel(root: Path, output: Path) -> Path:
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    before = set(output.glob("*.whl"))
    with tempfile.TemporaryDirectory(prefix="magpie-wheel-") as temporary:
        source = Path(temporary) / "source"
        stage_source(root, source)
        uv = shutil.which("uv")
        command = (
            [uv, "build", "--wheel", "--out-dir", str(output)]
            if uv
            else [
                sys.executable,
                "-m",
                "pip",
                "wheel",
                ".",
                "--no-deps",
                "--wheel-dir",
                str(output),
            ]
        )
        subprocess.run(command, cwd=source, check=True)
    created = sorted(set(output.glob("*.whl")) - before)
    if len(created) != 1:
        raise RuntimeError(
            f"expected one new wheel in {output}, found {len(created)}"
        )
    return created[0]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build a Magpie wheel without reusing stale local build output."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "dist",
        help="wheel output directory (default: ./dist)",
    )
    args = parser.parse_args()
    wheel = build_wheel(ROOT, args.output)
    print(wheel)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
