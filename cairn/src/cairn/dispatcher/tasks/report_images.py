"""报告的可选图片独立发布；任何展示故障都不改变候选正文或原件合同。"""
from __future__ import annotations

import base64
from hashlib import sha256
import json
import logging
import re

from .artifact_records import _open_container_artifact

LOG = logging.getLogger(__name__)


def capture_report_images(client, container_manager, container_name: str, *, manifest_path: str,
                          project_id: str, task_run_id: str, snapshot_artifact_id: str, packet: dict) -> list[dict]:
    images, cleanup = [], [manifest_path]
    missing = {"unavailable_reason": "可选数据包截图生成或发布失败；正文与原始证据仍可交付。"}
    try:
        with _open_container_artifact(container_manager, container_name, manifest_path) as stream:
            raw = stream.read(65537)
        if not raw:
            return []
        if len(raw) > 65536:
            raise ValueError("http_image_manifest_too_large")
        manifest = json.loads(raw)
        if (not isinstance(manifest, dict) or manifest.get("version") != 1
                or not isinstance(manifest.get("images"), list) or len(manifest["images"]) > 32):
            raise ValueError("http_image_manifest_invalid")
        if manifest.get("unavailable") is True:
            images.append(dict(missing))
        for image in manifest["images"]:
            try:
                path, key = image["path"], image["source_key"]
                if not isinstance(path, str) or not re.fullmatch(r"/tmp/cairn-prompts/http-evidence-[A-Za-z0-9_-]+/[1-4]\.png", path):
                    raise ValueError("http_image_path_invalid")
                cleanup.append(path)
                source = packet["http_evidence_render_sources"][key]["source"]
                if type(image["size"]) is not int or not 24 <= image["size"] <= 2 * 1024 * 1024:
                    raise ValueError("http_image_size_invalid")
                with _open_container_artifact(container_manager, container_name, path) as stream:
                    content = stream.read(image["size"] + 1)
                if len(content) != image["size"] or sha256(content).hexdigest() != image["sha256"]:
                    raise ValueError("http_image_digest_mismatch")
                response = client.publish_http_evidence_image(project_id, {
                    "task_run_id": task_run_id,
                    "source": {"kind": "frozen", "snapshot_artifact_id": snapshot_artifact_id, "source_key": key},
                    "expected_source": source, "page": image["page"], "page_count": image["page_count"],
                    "truncated": image["truncated"], "content_base64": base64.b64encode(content).decode("ascii"),
                })
                if (not response.ok or not isinstance(response.data, dict)
                        or not re.fullmatch(r"[A-Za-z0-9_-]+", response.data.get("id", ""))
                        or response.data.get("sha256") != image["sha256"]):
                    raise ValueError("http_image_publish_failed")
                images.append({"artifact_id": response.data["id"], "source_key": key,
                               "caption": f"HTTP 数据包展示 · 第 {image['page']}/{image['page_count']} 页",
                               "content_type": "image/png"})
            except (OSError, RuntimeError, ValueError, KeyError, TypeError):
                images.append(dict(missing))
    except (OSError, RuntimeError, ValueError, KeyError, TypeError):
        images.append(dict(missing))
    finally:
        for path in cleanup:
            try:
                container_manager.remove_file(container_name, path, allowed_root="/tmp/cairn-prompts")
            except (OSError, RuntimeError, ValueError):
                LOG.warning("optional report image cleanup deferred project=%s task_run=%s", project_id, task_run_id)
    return images
