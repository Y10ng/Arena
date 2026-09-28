"""固定 Playwright 正文补丁必须精确匹配，实际取证行为由隔离浏览器验收。"""

import json
from pathlib import Path
import runpy

import pytest


CONTAINER = Path(__file__).resolve().parents[2] / "container"


def make_core(tmp_path, patch, source, version=None):
    (tmp_path / "package.json").write_text(json.dumps({"version": version or patch["CORE_VERSION"]}))
    (tmp_path / "lib").mkdir()
    bundle = tmp_path / "lib" / "coreBundle.js"
    bundle.write_text(source)
    return bundle


def test_response_body_patch_only_changes_fixed_read_entry(tmp_path):
    patch = runpy.run_path(str(CONTAINER / "patch-playwright-response.py"))
    original = "before\n" + patch["ORIGINAL"] + "\nafter\n"
    bundle = make_core(tmp_path, patch, original)
    patch["patch_response_body"](tmp_path)
    assert bundle.read_text() == "before\n" + patch["REPLACEMENT"] + "\nafter\n"
    assert 'request2.timing().responseEnd < 0' in patch["REPLACEMENT"]
    assert 'throw new Error("response_body_incomplete:' in patch["REPLACEMENT"]
    dockerfile = (CONTAINER / "Dockerfile").read_text()
    assert "python3 /tmp/cairn-patch-playwright-response.py /usr/local/lib/node_modules/@playwright/cli/node_modules/playwright-core" in dockerfile
    assert "node --check /usr/local/lib/node_modules/@playwright/cli/node_modules/playwright-core/lib/coreBundle.js" in dockerfile


@pytest.mark.parametrize("drift", ["missing", "duplicate", "version", "already-patched"])
def test_response_body_patch_rejects_drift_without_write(tmp_path, drift):
    patch = runpy.run_path(str(CONTAINER / "patch-playwright-response.py"))
    source = {
        "missing": "different source",
        "duplicate": patch["ORIGINAL"] * 2,
        "version": patch["ORIGINAL"],
        "already-patched": patch["REPLACEMENT"],
    }[drift]
    bundle = make_core(tmp_path, patch, source, "0.0.0" if drift == "version" else None)
    with pytest.raises(ValueError, match="unexpected Playwright"):
        patch["patch_response_body"](tmp_path)
    assert bundle.read_text() == source
