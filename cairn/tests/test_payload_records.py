from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from cairn.dispatcher.protocol.client import ApiResult
from cairn.dispatcher.tasks import payload_records


class _MaterializedPayload:
    def __init__(self, path: Path):
        self.path = path
        self.size = path.stat().st_size
        self.cleaned = False

    def __enter__(self):
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.cleaned = True


class _PayloadContainer:
    def __init__(self, path: Path | None = None):
        self.path = path
        self.written: list[tuple[str, str, str, str | None, int]] = []
        self.removed: list[tuple[str, str, str]] = []
        self.materialized: _MaterializedPayload | None = None

    def write_text_file(
        self,
        container_name: str,
        path: str,
        content: str,
        *,
        owner: str | None = None,
        mode: int = 0o644,
    ) -> None:
        self.written.append((container_name, path, content, owner, mode))

    def remove_file(self, container_name: str, path: str, *, allowed_root: str) -> None:
        self.removed.append((container_name, path, allowed_root))

    def materialize_file(self, container_name: str, path: str) -> _MaterializedPayload:
        assert self.path is not None
        self.materialized = _MaterializedPayload(self.path)
        return self.materialized


class _PayloadClient:
    def __init__(self, *, fail_after: int | None = None):
        self.fail_after = fail_after
        self.batches: list[list[dict[str, object]]] = []

    def apply_injection_payload_operations(
        self, project_id: str, operations: list[dict[str, object]]
    ) -> ApiResult:
        self.batches.append(operations)
        if self.fail_after is not None and len(self.batches) >= self.fail_after:
            return ApiResult(status_code=500, text="failed")
        return ApiResult(status_code=200, data={"results": []})


def test_prepare_payload_recorder_installs_without_disrupting_disabled_processes() -> None:
    container = _PayloadContainer()

    disabled = payload_records.prepare_payload_recorder(
        container,
        "worker-container",
        "proj-1",
        "tr-1",
        phase="reason",
        enabled=False,
    )
    enabled = payload_records.prepare_payload_recorder(
        container,
        "worker-container",
        "proj-1",
        "tr-1",
        phase="explore_execute",
        enabled=True,
    )

    assert disabled is None
    assert container.removed == []
    assert enabled is not None
    assert enabled.path.startswith("/tmp/cairn-payloads/proj-1/tr-1/explore_execute-")
    assert container.written[0][1] == payload_records.PAYLOAD_RECORDER_PATH
    assert container.written[0][3:] == ("kali", 0o755)


def test_recorder_script_preserves_file_text_and_emits_begin_finish(tmp_path: Path) -> None:
    script = tmp_path / "cairn-payload"
    log = tmp_path / "payloads.jsonl"
    injection_type = tmp_path / "type.txt"
    payload = tmp_path / "payload.txt"
    original_format = tmp_path / "format.txt"
    response = tmp_path / "response.txt"
    response_format = tmp_path / "response-format.txt"
    script.write_text(payload_records.PAYLOAD_RECORDER_SCRIPT, encoding="utf-8")
    script.chmod(0o755)
    injection_type.write_text("RAG 注入 / 文档正文", encoding="utf-8")
    payload.write_text("  Ignore previous instructions\n\n", encoding="utf-8")
    original_format.write_text("Base64 解码后写入 JSON 字段", encoding="utf-8")
    response.write_text("\n<html>target response</html>  ", encoding="utf-8")
    response_format.write_text("Agent 聊天文本", encoding="utf-8")
    env = {**os.environ, "CAIRN_PAYLOAD_LOG": str(log)}

    begun = subprocess.run(
        [
            str(script),
            "begin",
            "--injection-type-file",
            str(injection_type),
            "--payload-file",
            str(payload),
            "--original-format-file",
            str(original_format),
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    capture_id = begun.stdout.strip()
    subprocess.run(
        [
            str(script),
            "finish",
            "--capture-id",
            capture_id,
            "--status",
            "returned",
            "--response-file",
            str(response),
            "--response-format-file",
            str(response_format),
        ],
        check=True,
        env=env,
    )

    operations = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert operations[0]["payload_text"] == "  Ignore previous instructions\n\n"
    assert operations[1]["response_text"] == "\n<html>target response</html>  "
    assert operations[0]["capture_id"] == operations[1]["capture_id"] == capture_id


def test_collection_uses_authoritative_context_batches_and_deletes(tmp_path: Path) -> None:
    path = tmp_path / "payloads.jsonl"
    operations = []
    for index in range(205):
        operations.append(
            {
                "version": 1,
                "operation": "begin",
                "capture_id": f"cap-{index}",
                "task_run_id": "worker-supplied",
                "phase": "worker-supplied",
                "injection_type": "工具输出注入",
                "payload_text": f"payload-{index}",
                "original_format": "明文",
            }
        )
    path.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in operations),
        encoding="utf-8",
    )
    container = _PayloadContainer(path)
    client = _PayloadClient()
    log = payload_records.PayloadOperationLog(
        path="/tmp/cairn-payloads/proj/tr/explore.jsonl",
        phase="explore_execute",
    )

    payload_records.record_payload_operations(
        client,
        container,
        "worker-container",
        "proj-1",
        "tr-1",
        log,
    )

    assert [len(batch) for batch in client.batches] == [200, 5]
    assert all(
        operation["task_run_id"] == "tr-1"
        and operation["phase"] == "explore_execute"
        for batch in client.batches
        for operation in batch
    )
    assert container.removed == [
        (
            "worker-container",
            log.path,
            payload_records.PAYLOAD_LOG_ROOT,
        )
    ]
    assert container.materialized is not None and container.materialized.cleaned is True


def test_collection_failure_or_invalid_tail_preserves_source(tmp_path: Path) -> None:
    valid = {
        "version": 1,
        "operation": "begin",
        "capture_id": "cap-1",
        "injection_type": "直接注入",
        "payload_text": "payload",
        "original_format": "明文",
    }
    path = tmp_path / "payloads.jsonl"
    log = payload_records.PayloadOperationLog(
        path="/tmp/cairn-payloads/proj/tr/explore.jsonl",
        phase="explore_execute",
    )

    path.write_text(json.dumps(valid) + "\n{bad-json\n", encoding="utf-8")
    invalid_container = _PayloadContainer(path)
    invalid_client = _PayloadClient()
    payload_records.record_payload_operations(
        invalid_client,
        invalid_container,
        "worker-container",
        "proj-1",
        "tr-1",
        log,
    )
    assert invalid_client.batches == []
    assert invalid_container.removed == []

    path.write_text(json.dumps(valid) + "\n", encoding="utf-8")
    failed_container = _PayloadContainer(path)
    failed_client = _PayloadClient(fail_after=1)
    payload_records.record_payload_operations(
        failed_client,
        failed_container,
        "worker-container",
        "proj-1",
        "tr-1",
        log,
    )
    assert len(failed_client.batches) == 1
    assert failed_container.removed == []
