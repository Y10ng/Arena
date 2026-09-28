from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_knowledge_root(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setenv("CAIRN_KNOWLEDGE_ROOT", str(tmp_path / "knowledge"))
    monkeypatch.setenv("CAIRN_PROMPTS_ROOT", str(tmp_path / "prompts"))
