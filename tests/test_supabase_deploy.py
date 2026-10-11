from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "apply-supabase-migrations.py"
MIGRATIONS = Path(__file__).parents[1] / "supabase" / "migrations"
SPEC = importlib.util.spec_from_file_location("apply_supabase_migrations", SCRIPT)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class RecordingApi:
    def __init__(self) -> None:
        self.queries: list[tuple[str, bool]] = []

    def query(self, sql: str, *, read_only: bool = False):
        self.queries.append((sql, read_only))
        return []


def test_project_ref_requires_canonical_supabase_url() -> None:
    assert (
        module.project_ref_from_url("https://abcdefghijklmnopqrst.supabase.co")
        == "abcdefghijklmnopqrst"
    )
    with pytest.raises(ValueError, match="supabase.co"):
        module.project_ref_from_url("http://localhost:54321")


def test_apply_pending_runs_each_new_migration_once(tmp_path: Path) -> None:
    first = tmp_path / "20260101000000_first.sql"
    second = tmp_path / "20260102000000_second.sql"
    first.write_text("create table first_table (id int);", encoding="utf-8")
    second.write_text("alter table first_table add column name text;", encoding="utf-8")
    api = RecordingApi()

    completed = module.apply_pending(
        api,
        [first, second],
        {first.name: module.checksum(first)},
        commit="abc123",
    )

    assert completed == [second.name]
    assert len(api.queries) == 1
    assert "begin;" in api.queries[0][0]
    assert "alter table first_table" in api.queries[0][0]
    assert second.name in api.queries[0][0]
    assert "abc123" in api.queries[0][0]


def test_apply_pending_rejects_changed_applied_migration(tmp_path: Path) -> None:
    migration = tmp_path / "20260101000000_first.sql"
    migration.write_text("select 1;", encoding="utf-8")

    with pytest.raises(RuntimeError, match="checksum changed"):
        module.apply_pending(
            RecordingApi(),
            [migration],
            {migration.name: "old-checksum"},
            commit="abc123",
        )


def test_pending_migrations_is_read_only_and_lists_exact_candidate_order(
    tmp_path: Path,
) -> None:
    first = tmp_path / "20260101000000_first.sql"
    second = tmp_path / "20260102000000_second.sql"
    third = tmp_path / "20260103000000_third.sql"
    first.write_text("select 1;", encoding="utf-8")
    second.write_text("select 2;", encoding="utf-8")
    third.write_text("select 3;", encoding="utf-8")

    pending = module.pending_migrations(
        [first, second, third],
        {first.name: module.checksum(first)},
    )

    assert pending == [second, third]


def test_pending_migrations_rejects_database_history_missing_from_candidate(
    tmp_path: Path,
) -> None:
    migration = tmp_path / "20260101000000_first.sql"
    migration.write_text("select 1;", encoding="utf-8")

    with pytest.raises(RuntimeError, match="missing from this candidate"):
        module.pending_migrations(
            [migration],
            {
                migration.name: module.checksum(migration),
                "20260102000000_future.sql": "future-checksum",
            },
        )


def test_applied_signup_and_workspace_migrations_remain_in_release_history() -> None:
    expected = {
        "20260802133000_restrict_magpie_signups.sql": (
            "39f2d345fd788d4415925c2e2d39f656fc41ddbfd18ddc06f65d6669b343a5b1"
        ),
        "20260803123000_workspace_claims.sql": (
            "99e9f357cb33d214c1f2dadecde0174eed839a3b02fc84edd2f3311c143ad92e"
        ),
    }

    available = {
        path.name: module.checksum(path)
        for path in module.migration_files(MIGRATIONS)
    }

    assert {name: available.get(name) for name in expected} == expected


def test_plan_mode_reads_history_without_writing(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    first = tmp_path / "20260101000000_first.sql"
    second = tmp_path / "20260102000000_second.sql"
    first.write_text("select 1;", encoding="utf-8")
    second.write_text("select 2;", encoding="utf-8")
    calls: list[tuple[str, bool]] = []

    class PlanApi:
        def __init__(self, **_kwargs) -> None:
            pass

        def query(self, sql: str, *, read_only: bool = False):
            calls.append((sql, read_only))
            assert read_only is True
            return [{"filename": first.name, "checksum": module.checksum(first)}]

    monkeypatch.setattr(module, "ManagementApi", PlanApi)
    monkeypatch.setenv("SUPABASE_ACCESS_TOKEN", "test-access-token")
    monkeypatch.setenv(
        "MAGPIE_SUPABASE_URL",
        "https://abcdefghijklmnopqrst.supabase.co",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [str(SCRIPT), "--plan", "--directory", str(tmp_path)],
    )

    module.main()

    assert len(calls) == 1
    assert second.name in capsys.readouterr().out
