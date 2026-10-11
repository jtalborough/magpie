from __future__ import annotations

import json
import os
import subprocess
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

from . import __version__


@dataclass(frozen=True)
class BuildInfo:
    version: str
    commit: str = ""
    branch: str = ""
    dirty: bool = False
    built_at: str = ""
    source: str = "runtime"

    def as_dict(self) -> dict[str, object]:
        return asdict(self)

    @property
    def short_commit(self) -> str:
        return self.commit[:7] if self.commit else ""

    def label(self) -> str:
        parts = [f"v{self.version}"]
        if self.short_commit:
            parts.append(self.short_commit + ("+" if self.dirty else ""))
        if self.built_at:
            parts.append(self.built_at)
        return " ".join(parts)


def build_info() -> BuildInfo:
    for candidate in build_files():
        if info := read_build_file(candidate):
            return info
    if info := git_build_info(Path.cwd()):
        return info
    return BuildInfo(version=__version__, built_at=utc_now(), source="fallback")


def build_files() -> list[Path]:
    candidates: list[Path] = []
    if path := os.environ.get("BUILD_INFO_PATH"):
        candidates.append(Path(path))
    candidates.append(Path.cwd() / "build.json")
    try:
        candidates.append(Path(resources.files("searchable_storage_led").joinpath("build.json")))
    except (FileNotFoundError, TypeError):
        pass
    return candidates


def read_build_file(path: Path) -> BuildInfo | None:
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return BuildInfo(
        version=str(raw.get("version") or __version__),
        commit=str(raw.get("commit") or ""),
        branch=str(raw.get("branch") or ""),
        dirty=bool(raw.get("dirty", False)),
        built_at=str(raw.get("built_at") or ""),
        source=str(raw.get("source") or f"file:{path}"),
    )


def git_build_info(cwd: Path) -> BuildInfo | None:
    commit = git(["rev-parse", "HEAD"], cwd=cwd)
    if not commit:
        return None
    branch = git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd) or ""
    status = git(["status", "--porcelain"], cwd=cwd) or ""
    return BuildInfo(
        version=__version__,
        commit=commit,
        branch=branch,
        dirty=bool(status.strip()),
        built_at=utc_now(),
        source="git",
    )


def git(args: list[str], *, cwd: Path) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
