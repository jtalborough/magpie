from __future__ import annotations

import argparse
import json
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlsplit


def merge_runtime_config(
    source: dict[str, object],
    installed: dict[str, object],
) -> dict[str, object]:
    merged = deepcopy(source)
    installed_storage = installed.get("storage")
    if (
        isinstance(installed_storage, dict)
        and str(installed_storage.get("backend") or "").lower() == "supabase"
    ):
        merged["storage"] = deepcopy(installed_storage)

    installed_zebra = installed.get("zebra")
    public_origin = (
        str(installed_zebra.get("base_url") or "").strip()
        if isinstance(installed_zebra, dict)
        else ""
    )
    if urlsplit(public_origin).scheme.lower() == "https":
        source_zebra = merged.get("zebra")
        zebra = deepcopy(source_zebra) if isinstance(source_zebra, dict) else {}
        zebra["base_url"] = public_origin.rstrip("/")
        merged["zebra"] = zebra
    return merged


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Merge deploy-time Magpie config with persistent runtime settings."
    )
    parser.add_argument("--source", required=True)
    parser.add_argument("--installed", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    source = json.loads(Path(args.source).read_text(encoding="utf-8"))
    installed = json.loads(Path(args.installed).read_text(encoding="utf-8"))
    merged = merge_runtime_config(source, installed)
    Path(args.output).write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
