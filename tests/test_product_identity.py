import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_package_and_readme_present_magpie_as_software_first() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert 'description = "Agent-led software for organizing and finding physical storage."' in pyproject
    assert readme.startswith("# Magpie\n")
    assert "The product is software-first:" in readme
    assert "Optional edge devices" in readme
    assert "docs/container-deployment.md" in readme
    assert "Raspberry Pi 5 WS2812 LED controller API" not in pyproject


def test_wheel_builder_stages_only_current_package_sources(tmp_path) -> None:
    script_path = ROOT / "scripts/build-wheel.py"
    spec = importlib.util.spec_from_file_location("magpie_build_wheel", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    source = tmp_path / "checkout"
    package = source / "src/searchable_storage_led"
    package.mkdir(parents=True)
    (source / "pyproject.toml").write_text("[project]\nname='magpie-test'\n")
    (source / "README.md").write_text("# Magpie\n")
    (package / "__init__.py").write_text("")
    stale = source / "build/lib/searchable_storage_led/static"
    stale.mkdir(parents=True)
    (stale / "deleted-page.html").write_text("stale")

    staged = tmp_path / "staged"
    module.stage_source(source, staged)

    assert (staged / "src/searchable_storage_led/__init__.py").exists()
    assert not (staged / "build").exists()
    assert not any(staged.rglob("deleted-page.html"))
