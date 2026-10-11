#!/usr/bin/env python3
"""Prepare and validate a hosted Magpie deployment without changing runtime state."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from searchable_storage_led.supabase_keys import is_supabase_publishable_key


DEFAULT_TEMPLATE = ROOT / "config/container-supabase.example.json"
DEFAULT_OUTPUT = ROOT / "config/container-hosted.json"
IMMUTABLE_IMAGE = re.compile(
    r"(?:^|:)sha-[0-9a-fA-F]{7,64}$|@sha256:[0-9a-fA-F]{64}$"
)


def required_environment(environ: dict[str, str]) -> list[str]:
    missing: list[str] = []
    for name in ("SUPABASE_PUBLISHABLE_KEY", "SUPABASE_SERVICE_ROLE_KEY"):
        if not environ.get(name, "").strip():
            missing.append(name)
    if not any(
        environ.get(name, "").strip()
        for name in (
            "OPENAI_API_KEY",
            "OPENROUTER_API_KEY",
            "CLOUDFLARE_AI_API_TOKEN",
        )
    ):
        missing.append(
            "one of OPENAI_API_KEY, OPENROUTER_API_KEY, "
            "or CLOUDFLARE_AI_API_TOKEN"
        )
    if (
        environ.get("CLOUDFLARE_AI_API_TOKEN", "").strip()
        and not environ.get("CLOUDFLARE_ACCOUNT_ID", "").strip()
    ):
        missing.append("CLOUDFLARE_ACCOUNT_ID")
    return missing


def _redact_environment_values(detail: str, environ: dict[str, str]) -> str:
    redacted = detail
    for name in (
        "SUPABASE_SERVICE_ROLE_KEY",
        "SUPABASE_PUBLISHABLE_KEY",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "CLOUDFLARE_AI_API_TOKEN",
    ):
        value = environ.get(name, "").strip()
        if value:
            redacted = redacted.replace(value, "[redacted]")
    return redacted


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Render and validate a hardware-neutral, multi-workspace Magpie "
            "hosted configuration. This command does not start containers."
        )
    )
    parser.add_argument(
        "--supabase-url",
        default=os.environ.get("MAGPIE_SUPABASE_URL", ""),
        help="Supabase HTTPS origin (or set MAGPIE_SUPABASE_URL)",
    )
    parser.add_argument(
        "--public-origin",
        default=os.environ.get("MAGPIE_PUBLIC_ORIGIN", ""),
        help="Public Magpie HTTPS origin (or set MAGPIE_PUBLIC_ORIGIN)",
    )
    parser.add_argument(
        "--edge-origin",
        default=os.environ.get("MAGPIE_EDGE_ORIGIN", ""),
        help="Optional protected edge HTTPS origin (or set MAGPIE_EDGE_ORIGIN)",
    )
    parser.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--image",
        default=os.environ.get("MAGPIE_IMAGE", ""),
        help="Immutable sha-* image tag or sha256 digest (or set MAGPIE_IMAGE)",
    )
    parser.add_argument(
        "--existing-workspace-id",
        default=os.environ.get("MAGPIE_EXISTING_WORKSPACE_ID", ""),
        help=(
            "Existing workspace UUID that must pass the read-only attachment "
            "check (or set MAGPIE_EXISTING_WORKSPACE_ID)"
        ),
    )
    args = parser.parse_args()

    errors: list[str] = []
    if not args.supabase_url.strip():
        errors.append("MAGPIE_SUPABASE_URL or --supabase-url is required")
    if not args.public_origin.strip():
        errors.append("MAGPIE_PUBLIC_ORIGIN or --public-origin is required")
    if not args.image.strip():
        errors.append("MAGPIE_IMAGE or --image is required")
    elif not IMMUTABLE_IMAGE.search(args.image.strip()):
        errors.append("MAGPIE_IMAGE must use an immutable sha-* tag or sha256 digest")
    existing_workspace_id = args.existing_workspace_id.strip()
    if existing_workspace_id:
        try:
            uuid.UUID(existing_workspace_id)
        except ValueError:
            errors.append(
                "MAGPIE_EXISTING_WORKSPACE_ID or --existing-workspace-id "
                "must be a UUID"
            )
    errors.extend(f"{name} is required" for name in required_environment(os.environ))
    publishable_key = os.environ.get("SUPABASE_PUBLISHABLE_KEY", "").strip()
    if publishable_key and not is_supabase_publishable_key(publishable_key):
        errors.append(
            "SUPABASE_PUBLISHABLE_KEY must contain a Supabase publishable key "
            "or legacy anon key"
        )
    if errors:
        for error in errors:
            print(f"prepare-hosted: {error}", file=sys.stderr)
        return 1

    render_output = args.output
    temporary_output: Path | None = None
    if existing_workspace_id:
        temporary_output = args.output.with_suffix(
            args.output.suffix + ".attachment-check.tmp"
        )
        temporary_output.unlink(missing_ok=True)
        render_output = temporary_output

    try:
        command = [
            sys.executable,
            str(ROOT / "scripts/render-hosted-config.py"),
            "--template",
            str(args.template),
            "--output",
            str(render_output),
            "--supabase-url",
            args.supabase_url,
            "--public-origin",
            args.public_origin,
            "--edge-origin",
            args.edge_origin,
        ]
        result = subprocess.run(command, text=True, capture_output=True, check=False)
        if result.returncode != 0:
            detail = (
                result.stderr.strip()
                or result.stdout.strip()
                or "config render failed"
            )
            detail = _redact_environment_values(detail, os.environ)
            print(f"prepare-hosted: {detail}", file=sys.stderr)
            return result.returncode

        if existing_workspace_id:
            check_environment = dict(os.environ)
            source_path = str(ROOT / "src")
            current_pythonpath = check_environment.get("PYTHONPATH", "")
            check_environment["PYTHONPATH"] = (
                f"{source_path}{os.pathsep}{current_pythonpath}"
                if current_pythonpath
                else source_path
            )
            check_command = [
                sys.executable,
                "-m",
                "searchable_storage_led.supabase_check",
                "--config",
                str(render_output),
                "--verify-attachment",
                "--workspace-id",
                existing_workspace_id,
            ]
            check_result = subprocess.run(
                check_command,
                env=check_environment,
                text=True,
                capture_output=True,
                check=False,
            )
            if check_result.returncode != 0:
                detail = (
                    check_result.stderr.strip()
                    or check_result.stdout.strip()
                    or "existing workspace attachment check failed"
                )
                detail = _redact_environment_values(detail, os.environ)
                print(f"prepare-hosted: {detail}", file=sys.stderr)
                return check_result.returncode
            os.replace(render_output, args.output)
            temporary_output = None
            print(
                "Existing workspace attachment verified: "
                f"{existing_workspace_id}"
            )
    finally:
        if temporary_output is not None:
            temporary_output.unlink(missing_ok=True)

    print(f"Hosted config ready: {args.output}")
    print("Environment ready: Supabase, agent provider, and immutable image")
    print("No containers were started or changed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
