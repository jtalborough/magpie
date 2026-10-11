#!/usr/bin/env python3
"""Render a deployable hosted config from the checked-in safe template."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from urllib.parse import urlparse


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--template", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--supabase-url", required=True)
    parser.add_argument("--workspace-id", default="")
    parser.add_argument("--public-origin", default="")
    parser.add_argument("--edge-origin", default="")
    args = parser.parse_args()

    supabase_url = args.supabase_url.strip().rstrip("/")
    parsed = urlparse(supabase_url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.path:
        raise SystemExit("Supabase URL must be an HTTPS origin without a path")

    template_path = Path(args.template)
    output_path = Path(args.output)
    config = json.loads(template_path.read_text(encoding="utf-8"))
    workspace_id = args.workspace_id.strip()
    public_origin = args.public_origin.strip().rstrip("/")
    edge_origin = args.edge_origin.strip().rstrip("/")
    for label, origin in (
        ("Public origin", public_origin),
        ("Edge origin", edge_origin),
    ):
        if origin:
            parsed_origin = urlparse(origin)
            if (
                parsed_origin.scheme != "https"
                or not parsed_origin.netloc
                or parsed_origin.path
            ):
                raise SystemExit(f"{label} must be an HTTPS origin without a path")

    storage = config.setdefault("storage", {})
    auth = config.setdefault("auth", {})
    storage.update(
        {
            "backend": "supabase",
            "supabase_url": supabase_url,
            "workspace_id": workspace_id,
        }
    )
    auth.update(
        {
            "mode": "supabase",
            "supabase_url": supabase_url,
            "workspace_id": workspace_id,
            "multi_workspace": not bool(workspace_id),
        }
    )
    config["backend"] = "none"
    config["pixel_count"] = 0
    if public_origin:
        config["public_origin"] = public_origin
        config.setdefault("zebra", {})["base_url"] = public_origin
    config.setdefault("edge", {})["machine_url"] = edge_origin

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(output_path)


if __name__ == "__main__":
    main()
