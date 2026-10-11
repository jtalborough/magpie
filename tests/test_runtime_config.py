import json

from searchable_storage_led.runtime_config import main, merge_runtime_config


def test_merge_preserves_active_supabase_and_https_origin() -> None:
    source = {
        "pixel_count": 200,
        "storage": {"backend": "sqlite"},
        "zebra": {"host": "printer", "base_url": "http://magpie:8765"},
    }
    installed = {
        "pixel_count": 182,
        "storage": {
            "backend": "supabase",
            "supabase_url": "https://project.supabase.co",
            "workspace_id": "magpie",
        },
        "zebra": {"host": "old-printer", "base_url": "https://magpie.example/"},
    }

    merged = merge_runtime_config(source, installed)

    assert merged["pixel_count"] == 200
    assert merged["storage"] == installed["storage"]
    assert merged["zebra"] == {
        "host": "printer",
        "base_url": "https://magpie.example",
    }


def test_merge_does_not_preserve_local_sqlite_runtime_config() -> None:
    source = {
        "storage": {"backend": "sqlite", "photo_bucket": "new"},
        "zebra": {"base_url": "http://new.example"},
    }
    installed = {
        "storage": {"backend": "sqlite", "photo_bucket": "old"},
        "zebra": {"base_url": "http://old.example"},
    }

    assert merge_runtime_config(source, installed) == source


def test_runtime_config_cli_writes_merged_json(tmp_path, monkeypatch) -> None:
    source = tmp_path / "source.json"
    installed = tmp_path / "installed.json"
    output = tmp_path / "output.json"
    source.write_text(
        json.dumps({"storage": {"backend": "sqlite"}}), encoding="utf-8"
    )
    installed.write_text(
        json.dumps(
            {
                "storage": {
                    "backend": "supabase",
                    "workspace_id": "magpie",
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "runtime-config",
            "--source",
            str(source),
            "--installed",
            str(installed),
            "--output",
            str(output),
        ],
    )

    main()

    assert json.loads(output.read_text(encoding="utf-8"))["storage"] == {
        "backend": "supabase",
        "workspace_id": "magpie",
    }
