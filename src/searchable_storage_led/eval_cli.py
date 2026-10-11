from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any
from urllib import error, request
from urllib.parse import urlsplit

from .machine_auth import cloudflare_access_headers


DEFAULT_BASE_URL = "http://127.0.0.1:8765"
EVAL_SCENARIOS = ("putaway", "rules", "correction", "photo", "organization")


def _endpoint(base_url: str) -> str:
    return base_url.rstrip("/") + "/assistant/evals/replay"


def replay_evals(
    *,
    base_url: str,
    limit: int,
    minimum_cases: int,
    required_scenarios: list[str],
    kind: str | None,
    run_model: bool,
    timeout: float,
    auth_token: str = "",
    workspace_id: str = "",
    cf_access_client_id: str = "",
    cf_access_client_secret: str = "",
) -> dict[str, Any]:
    if minimum_cases < 1 or minimum_cases > 25:
        raise ValueError("minimum_cases must be between 1 and 25")
    invalid_scenarios = sorted(set(required_scenarios) - set(EVAL_SCENARIOS))
    if invalid_scenarios:
        raise ValueError(
            f"unknown required scenarios: {', '.join(invalid_scenarios)}"
        )
    payload = {
        "limit": limit,
        "minimum_cases": minimum_cases,
        "required_scenarios": list(dict.fromkeys(required_scenarios)),
        "kind": kind,
        "run_model": run_model,
    }
    data = json.dumps(payload).encode("utf-8")
    machine_headers = cloudflare_access_headers(
        cf_access_client_id,
        cf_access_client_secret,
    )
    clean_token = auth_token.strip()
    parsed_url = urlsplit(base_url.strip())
    if (clean_token or machine_headers) and parsed_url.scheme != "https" and parsed_url.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise ValueError("the authenticated Magpie eval API must use HTTPS")
    headers = {
        "content-type": "application/json",
        **machine_headers,
    }
    if clean_token:
        headers["Authorization"] = f"Bearer {clean_token}"
    if clean_workspace := workspace_id.strip():
        headers["X-Magpie-Workspace"] = clean_workspace
    req = request.Request(
        _endpoint(base_url),
        data=data,
        headers=headers,
        method="POST",
    )
    with request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _print_summary(result: dict[str, Any]) -> None:
    print(
        "Magpie eval replay: "
        f"{result.get('count', 0)} example(s), "
        f"run_model={str(result.get('run_model', False)).lower()}, "
        f"passed={result.get('passed', 0)}, "
        f"ready={result.get('ready', 0)}, "
        f"failed={result.get('failed', 0)}, "
        f"errors={result.get('errors', 0)}, "
        f"skipped={result.get('skipped', 0)}, "
        f"gate_passed={str(result.get('gate_passed', False)).lower()}"
    )
    for row in result.get("results", []):
        if not isinstance(row, dict):
            continue
        status = str(row.get("status", "unknown"))
        if status in {"passed", "ready", "skipped"}:
            continue
        command = str(row.get("command", ""))
        expected = ", ".join(str(tool) for tool in row.get("expected_tools", []))
        actual = ", ".join(str(tool) for tool in row.get("actual_tools", []))
        detail = str(row.get("error") or row.get("reason") or "")
        suffix = f" ({detail})" if detail else ""
        print(
            f"- #{row.get('id', '?')} {status}: {command} "
            f"expected=[{expected}] actual=[{actual}]{suffix}"
        )
    if not result.get("gate_passed", False):
        mode = "evaluated" if result.get("run_model") else "replay-ready"
        evidence_count = int(result.get("evidence_count") or 0)
        minimum_cases = int(result.get("minimum_cases") or 1)
        if evidence_count < minimum_cases:
            print(
                "Evidence floor not met: "
                f"{evidence_count} {mode} case(s); "
                f"minimum required is {minimum_cases}."
            )
        missing = result.get("missing_required_scenarios") or []
        if missing:
            print(f"Missing required scenarios: {', '.join(str(row) for row in missing)}.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="searchable-storage-evals",
        description="Replay Magpie approved examples against the running API.",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("MAGPIE_API_URL", DEFAULT_BASE_URL),
        help=f"Magpie API base URL. Default: {DEFAULT_BASE_URL}",
    )
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument(
        "--min-cases",
        type=int,
        default=1,
        help="Minimum replay-ready or evaluated cases required for a passing gate (1-25).",
    )
    parser.add_argument(
        "--require-scenario",
        action="append",
        choices=EVAL_SCENARIOS,
        default=[],
        help="Require evidence for this workflow; repeat for multiple scenarios.",
    )
    parser.add_argument(
        "--kind",
        default=None,
        help="Optionally limit replay to one example kind. Default: all reviewed kinds.",
    )
    parser.add_argument("--run-model", action="store_true")
    parser.add_argument(
        "--workspace-id",
        default=os.environ.get("MAGPIE_WORKSPACE_ID", ""),
        help="Workspace to replay. Defaults to MAGPIE_WORKSPACE_ID.",
    )
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--json", action="store_true", help="Print raw replay JSON.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = replay_evals(
            base_url=args.base_url,
            limit=args.limit,
            minimum_cases=args.min_cases,
            required_scenarios=args.require_scenario,
            kind=args.kind,
            run_model=args.run_model,
            timeout=args.timeout,
            auth_token=os.environ.get("MAGPIE_AUTH_TOKEN", ""),
            workspace_id=args.workspace_id,
            cf_access_client_id=os.environ.get("MAGPIE_CF_ACCESS_CLIENT_ID", ""),
            cf_access_client_secret=os.environ.get(
                "MAGPIE_CF_ACCESS_CLIENT_SECRET",
                "",
            ),
        )
    except (OSError, ValueError, error.URLError, json.JSONDecodeError) as exc:
        print(f"Magpie eval replay failed: {exc}", file=sys.stderr)
        return 2

    failed = int(result.get("failed") or 0)
    errors = int(result.get("errors") or 0)
    evidence_count = (
        int(result.get("passed") or 0) + failed + errors
        if args.run_model
        else int(result.get("ready") or 0)
    )
    result.setdefault("minimum_cases", args.min_cases)
    result.setdefault("evidence_count", evidence_count)
    result.setdefault("required_scenarios", list(dict.fromkeys(args.require_scenario)))
    if "missing_required_scenarios" not in result:
        evidence_statuses = (
            {"passed", "failed", "error"} if args.run_model else {"ready"}
        )
        observed_scenarios = {
            str(scenario)
            for row in result.get("results", [])
            if isinstance(row, dict)
            and row.get("status") in evidence_statuses
            for scenario in row.get("scenarios", [])
        }
        result["missing_required_scenarios"] = [
            scenario
            for scenario in result["required_scenarios"]
            if scenario not in observed_scenarios
        ]
    if "gate_passed" in result:
        gate_passed = bool(result["gate_passed"])
    else:
        gate_passed = (
            evidence_count >= args.min_cases
            and not failed
            and not errors
            and not result["missing_required_scenarios"]
        )
        result["gate_passed"] = gate_passed

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        _print_summary(result)

    return 0 if gate_passed and not failed and not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
