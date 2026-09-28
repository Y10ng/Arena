from __future__ import annotations

from pathlib import Path

from cairn.paths import resolve_source_relative_path


def test_resolve_source_relative_path_uses_checkout_root() -> None:
    repository_root = Path(__file__).resolve().parents[2]

    resolved = resolve_source_relative_path(
        "datas/cairn/knowledge",
        anchor=Path(__file__),
    )

    assert resolved == repository_root / "datas/cairn/knowledge"


def test_resolve_source_relative_path_uses_release_bundle_root(
    tmp_path: Path,
    monkeypatch,
) -> None:
    release_root = tmp_path / "cairny-release"
    (release_root / "cairn").mkdir(parents=True)
    (release_root / "deploy").mkdir()
    (release_root / "cairn/pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (release_root / "deploy/dispatch.yaml.example").write_text("workers: []\n", encoding="utf-8")
    working_directory = tmp_path / "working"
    working_directory.mkdir()
    monkeypatch.chdir(working_directory)

    resolved = resolve_source_relative_path(
        "datas/cairn/knowledge",
        anchor=release_root / "cairn/src/cairn/server/db.py",
    )

    assert resolved == release_root / "datas/cairn/knowledge"


def test_resolve_source_relative_path_falls_back_to_cwd(
    tmp_path: Path,
    monkeypatch,
) -> None:
    working_directory = tmp_path / "installed"
    working_directory.mkdir()
    monkeypatch.chdir(working_directory)

    resolved = resolve_source_relative_path(
        "datas/cairn/knowledge",
        anchor=tmp_path / "site-packages/cairn/server/db.py",
    )

    assert resolved == working_directory / "datas/cairn/knowledge"


def test_resolve_source_relative_path_preserves_absolute_path(tmp_path: Path) -> None:
    configured = tmp_path / "knowledge"

    resolved = resolve_source_relative_path(configured, anchor=Path(__file__))

    assert resolved == configured.resolve()
