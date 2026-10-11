#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.request


TRACKING_SCHEMA = "magpie_internal"
TRACKING_TABLE = f"{TRACKING_SCHEMA}.schema_migrations"
PROJECT_REF_PATTERN = re.compile(r"^https://([a-z0-9]+)\.supabase\.co/?$")


def project_ref_from_url(url: str) -> str:
    match = PROJECT_REF_PATTERN.fullmatch(url.strip())
    if not match:
        raise ValueError("MAGPIE_SUPABASE_URL must be a project .supabase.co URL")
    return match.group(1)


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


class ManagementApi:
    def __init__(self, *, project_ref: str, access_token: str) -> None:
        self.url = (
            f"https://api.supabase.com/v1/projects/{project_ref}/database/query"
        )
        self.access_token = access_token

    def query(self, sql: str, *, read_only: bool = False) -> list[dict[str, object]]:
        request = urllib.request.Request(
            self.url,
            data=json.dumps(
                {"query": sql, "parameters": [], "read_only": read_only}
            ).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json",
                "User-Agent": "Magpie-Schema-Migrator/0.1",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"Supabase migration query failed ({exc.code}): {detail}"
            ) from exc
        if not isinstance(payload, list):
            raise RuntimeError("Supabase migration query returned an invalid response")
        return [row for row in payload if isinstance(row, dict)]


def migration_files(directory: Path) -> list[Path]:
    return sorted(path for path in directory.glob("*.sql") if path.is_file())


def checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ensure_tracking_table(api: ManagementApi) -> None:
    api.query(
        f"""
        create schema if not exists {TRACKING_SCHEMA};
        revoke all on schema {TRACKING_SCHEMA} from public, anon, authenticated;
        create table if not exists {TRACKING_TABLE} (
          filename text primary key,
          checksum text not null,
          applied_commit text not null default '',
          applied_at timestamptz not null default now()
        );
        """
    )


def applied_migrations(api: ManagementApi) -> dict[str, str]:
    rows = api.query(
        f"select filename, checksum from {TRACKING_TABLE} order by filename",
        read_only=True,
    )
    return {
        str(row["filename"]): str(row["checksum"])
        for row in rows
        if row.get("filename") and row.get("checksum")
    }


def record_baseline(
    api: ManagementApi,
    files: list[Path],
    *,
    commit: str,
) -> None:
    if not files:
        return
    values = ",\n".join(
        f"({sql_literal(path.name)}, {sql_literal(checksum(path))}, "
        f"{sql_literal(commit)})"
        for path in files
    )
    api.query(
        f"""
        insert into {TRACKING_TABLE} (filename, checksum, applied_commit)
        values {values}
        on conflict (filename) do nothing;
        """
    )


def pending_migrations(
    files: list[Path],
    applied: dict[str, str],
) -> list[Path]:
    candidate_names = {path.name for path in files}
    unknown_applied = sorted(set(applied) - candidate_names)
    if unknown_applied:
        raise RuntimeError(
            "database contains migrations missing from this candidate: "
            + ", ".join(unknown_applied)
        )
    pending: list[Path] = []
    for path in files:
        existing = applied.get(path.name)
        if existing and existing != checksum(path):
            raise RuntimeError(f"applied migration checksum changed: {path.name}")
        if not existing:
            pending.append(path)
    return pending


def apply_pending(
    api: ManagementApi,
    files: list[Path],
    applied: dict[str, str],
    *,
    commit: str,
) -> list[str]:
    completed: list[str] = []
    for path in pending_migrations(files, applied):
        digest = checksum(path)
        sql = path.read_text(encoding="utf-8").strip()
        api.query(
            f"""
            begin;
            {sql}
            insert into {TRACKING_TABLE} (filename, checksum, applied_commit)
            values ({sql_literal(path.name)}, {sql_literal(digest)}, {sql_literal(commit)});
            commit;
            """
        )
        completed.append(path.name)
    return completed


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Apply checked-in Magpie migrations through the Supabase Management API."
    )
    parser.add_argument(
        "--directory",
        type=Path,
        default=Path("supabase/migrations"),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--baseline",
        action="store_true",
        help="Record the existing migration files without executing them.",
    )
    mode.add_argument(
        "--plan",
        action="store_true",
        help="Read and validate migration history, then list pending files without writing.",
    )
    args = parser.parse_args()
    access_token = os.environ.get("SUPABASE_ACCESS_TOKEN", "").strip()
    if not access_token:
        raise SystemExit("SUPABASE_ACCESS_TOKEN is required")
    try:
        project_ref = project_ref_from_url(
            os.environ.get("MAGPIE_SUPABASE_URL", "")
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    files = migration_files(args.directory)
    if not files:
        raise SystemExit(f"no migrations found in {args.directory}")
    commit = os.environ.get("GITHUB_SHA", "").strip()
    api = ManagementApi(project_ref=project_ref, access_token=access_token)
    if args.plan:
        applied = applied_migrations(api)
        if not applied:
            raise SystemExit(
                "migration history is empty; verify the existing schema and run once with --baseline"
            )
        pending = pending_migrations(files, applied)
        if pending:
            print(
                f"Migration plan: {len(pending)} pending: "
                + ", ".join(path.name for path in pending)
            )
        else:
            print(f"Migration plan: schema is current ({len(applied)} applied).")
        return
    ensure_tracking_table(api)
    applied = applied_migrations(api)
    if not applied and not args.baseline:
        raise SystemExit(
            "migration history is empty; verify the existing schema and run once with --baseline"
        )
    if args.baseline:
        record_baseline(api, files, commit=commit)
        print(f"Baselined {len(files)} Supabase migrations.")
        return
    completed = apply_pending(api, files, applied, commit=commit)
    if completed:
        print("Applied Supabase migrations: " + ", ".join(completed))
    else:
        print(f"Supabase schema is current ({len(applied)} migrations).")


if __name__ == "__main__":
    main()
