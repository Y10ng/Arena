from __future__ import annotations

from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
import sqlite3
import stat

import pytest
from fastapi import HTTPException
import yaml

from cairn.dispatcher.config import DispatchConfig
from cairn.server import db
from cairn.server.models import (
    DispatcherConfigUpdate,
    DispatcherControlUpdate,
    DispatcherCredentialUpdate,
    DispatcherRevisionRequest,
)
from cairn.server.routers import infra
from cairn.server.services.dispatcher_config_store import provider_credential_ref, redact_dispatcher_config


ROOT = Path(__file__).resolve().parents[2]


def make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(db.SCHEMA)
    db._ensure_schema_migrations(conn)
    return conn


def configure_server_first_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    config_path = runtime / "dispatch.yaml"
    config_path.write_text(
        (ROOT / "deploy/dispatch.yaml.example").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    conn = make_conn()

    @contextmanager
    def fake_get_conn():
        yield conn

    monkeypatch.setenv("CAIRN_DISPATCH_CONFIG", str(config_path))
    monkeypatch.setenv("CAIRN_DISPATCH_YAML_ALLOWED_ROOTS", str(runtime))
    monkeypatch.setattr(infra, "get_conn", fake_get_conn)
    return conn, runtime, config_path


def test_provider_model_worker_crud_and_plaintext_runtime_resolution(tmp_path: Path, monkeypatch) -> None:
    conn, runtime, config_path = configure_server_first_store(tmp_path, monkeypatch)
    try:
        providers = infra.list_dispatcher_providers()
        assert providers.items == []

        created = infra.create_dispatcher_provider(
            DispatcherControlUpdate(
                revision=providers.revision,
                item={
                    "id": "provider-a",
                    "base_url": "https://api.example.test",
                    "provider_api": "anthropic-messages",
                    "inference_endpoint": "https://api.example.test/v1/messages",
                    "credential_ref": "PROVIDER_A_API_KEY",
                },
            )
        )
        credential_ref = "PROVIDER_A_API_KEY"
        assert created.config["providers"][0]["credential_ref"] == credential_ref
        assert stat.S_IMODE((runtime / "locks").stat().st_mode) == 0o700
        assert stat.S_IMODE((runtime / "locks/dispatch-config.lock").stat().st_mode) == 0o600
        assert stat.S_IMODE(config_path.stat().st_mode) == 0o600

        credential_status = infra.put_dispatcher_provider_credential(
            "provider-a",
            DispatcherCredentialUpdate(revision=created.revision, credential="top-secret-token"),
        )
        assert credential_status.configured is True
        assert credential_status.credential_ref == credential_ref
        assert credential_status.revision != created.revision

        credential_ref = "PROVIDER_A_ROTATED_API_KEY"
        updated_provider = infra.update_dispatcher_provider(
            "provider-a",
            DispatcherControlUpdate(
                revision=credential_status.revision,
                item={
                    "id": "provider-a",
                    "base_url": "https://api.example.test",
                    "provider_api": "anthropic-messages",
                    "inference_endpoint": "https://api.example.test/v1/messages",
                    "credential_ref": credential_ref,
                },
            ),
        )
        assert updated_provider.config["common_env"][credential_ref] == "[REDACTED]"
        assert "top-secret-token" not in updated_provider.content
        assert "PROVIDER_A_API_KEY" not in updated_provider.config["common_env"]

        provider_list = infra.list_dispatcher_providers()
        provider = provider_list.items[0]
        assert provider.credential_ref == credential_ref
        assert provider.credential_configured is True
        assert provider.credential_env_override is False
        assert "top-secret-token" not in str(provider.model_dump())

        model_doc = infra.create_dispatcher_model(
            DispatcherControlUpdate(
                revision=provider_list.revision,
                item={
                    "id": "model-a",
                    "provider_id": "provider-a",
                    "upstream_model": "claude-test",
                },
            )
        )
        worker_doc = infra.create_dispatcher_worker(
            DispatcherControlUpdate(
                revision=model_doc.revision,
                item={
                    "id": "worker-a",
                    "engine": "pi",
                    "model_id": "model-a",
                    "task_types": ["reason"],
                    "concurrency": 1,
                    "priority": 0,
                    "enabled": True,
                },
            )
        )

        api_config = infra.get_dispatcher_config()
        assert "top-secret-token" not in api_config.content
        assert api_config.config["common_env"][credential_ref] == "[REDACTED]"

        loaded = DispatchConfig.load(config_path)
        worker = loaded.workers[0]
        assert worker.type == "pi"
        assert worker.model_binding is not None
        assert worker.model_binding.api_key == "top-secret-token"
        assert worker.model_binding.protocol == "anthropic-messages"
        assert worker.model_binding.inference_endpoint == "https://api.example.test/v1/messages"
        assert not any(key.startswith("ANTHROPIC_") for key in worker.env)

        with pytest.raises(HTTPException) as provider_delete:
            infra.delete_dispatcher_provider(
                "provider-a",
                DispatcherRevisionRequest(revision=worker_doc.revision),
            )
        assert provider_delete.value.status_code == 409

        with pytest.raises(HTTPException) as model_delete:
            infra.delete_dispatcher_model(
                "model-a",
                DispatcherRevisionRequest(revision=worker_doc.revision),
            )
        assert model_delete.value.status_code == 409
    finally:
        conn.close()


def test_dispatcher_responses_redact_url_userinfo_and_preserve_redacted_round_trip(
    tmp_path: Path,
    monkeypatch,
) -> None:
    conn, _runtime, config_path = configure_server_first_store(tmp_path, monkeypatch)
    known_secret = "known-password@internal-suffix"
    credentialed_url = f"https://known-user:{known_secret}@api.example.test/v1"
    try:
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        config["providers"] = [
            {
                "id": "provider-secret-url",
                "base_url": credentialed_url,
                "provider_api": "anthropic-messages",
            }
        ]
        config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

        document = infra.get_dispatcher_config()
        provider = infra.get_dispatcher_provider("provider-secret-url")

        assert known_secret not in document.content
        assert "internal-suffix" not in str(document.config)
        assert document.config["providers"][0]["base_url"] == "https://[REDACTED]@api.example.test/v1"
        assert provider.item.base_url == "https://[REDACTED]@api.example.test/v1"
        assert "internal-suffix" not in str(provider.model_dump())

        saved = infra.put_dispatcher_config(
            DispatcherConfigUpdate(config=document.config, revision=document.revision)
        )

        assert known_secret not in saved.content
        on_disk = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert on_disk["providers"][0]["base_url"] == credentialed_url

        arbitrary = redact_dispatcher_config({"callback_url": credentialed_url})
        assert arbitrary["callback_url"] == "https://[REDACTED]@api.example.test/v1"
        assert "internal-suffix" not in str(arbitrary)
    finally:
        conn.close()


def test_provider_get_reports_environment_override_without_leaking_value(tmp_path: Path, monkeypatch) -> None:
    conn, _runtime, _config_path = configure_server_first_store(tmp_path, monkeypatch)
    try:
        initial = infra.list_dispatcher_providers()
        created = infra.create_dispatcher_provider(
            DispatcherControlUpdate(
                revision=initial.revision,
                provider={
                    "id": "provider-a",
                    "base_url": "https://api.example.test",
                    "provider_api": "anthropic-messages",
                },
            )
        )
        ref = created.config["providers"][0]["credential_ref"]
        status = infra.put_dispatcher_provider_credential(
            "provider-a",
            DispatcherCredentialUpdate(revision=created.revision, credential="yaml-value"),
        )
        monkeypatch.setenv(ref, "environment-value-must-not-leak")

        provider = infra.get_dispatcher_provider("provider-a").item
        assert provider.credential_configured is True
        assert provider.credential_env_override is True
        assert "yaml-value" not in str(provider.model_dump())
        assert "运行环境中的凭据已覆盖" in (provider.credential_warning or "")
        assert ref not in (provider.credential_warning or "")
        assert "environment-value-must-not-leak" not in str(provider.model_dump())
        assert status.revision == infra.list_dispatcher_providers().revision
    finally:
        conn.close()


def test_revision_conflict_and_legacy_config_write_are_rejected(tmp_path: Path, monkeypatch) -> None:
    conn, _runtime, _config_path = configure_server_first_store(tmp_path, monkeypatch)
    try:
        initial = infra.get_dispatcher_config()
        updated_config = dict(initial.config)
        updated_config["dispatcher_id"] = "revision-one"
        updated = infra.put_dispatcher_config(
            DispatcherConfigUpdate(config=updated_config, revision=initial.revision)
        )

        stale_config = dict(updated.config)
        stale_config["dispatcher_id"] = "stale-write"
        with pytest.raises(HTTPException) as conflict:
            infra.put_dispatcher_config(
                DispatcherConfigUpdate(config=stale_config, revision=initial.revision)
            )
        assert conflict.value.status_code == 409

        plaintext_config = dict(updated.config)
        plaintext_config["common_env"] = {"ANTHROPIC_AUTH_TOKEN": "plain-common-value"}
        plaintext_config["workers"] = [
            {
                "id": "legacy-worker",
                "type": "claudecode",
                "task_types": ["reason"],
                "max_running": 1,
                "priority": 0,
                "env": {
                    "ANTHROPIC_MODEL": "legacy-model",
                    "ANTHROPIC_BASE_URL": "https://api.example.test",
                    "ANTHROPIC_AUTH_TOKEN": "plain-worker-value",
                },
            }
        ]
        with pytest.raises(HTTPException) as legacy:
            infra.put_dispatcher_config(
                DispatcherConfigUpdate(config=plaintext_config, revision=updated.revision)
            )
        assert legacy.value.status_code == 422
        assert "plain-common-value" not in str(legacy.value.detail)
        assert "plain-worker-value" not in str(legacy.value.detail)
        assert infra.get_dispatcher_config().revision == updated.revision
    finally:
        conn.close()


def test_raw_yaml_get_redacts_credentials_without_touching_file(tmp_path: Path, monkeypatch) -> None:
    conn, _runtime, config_path = configure_server_first_store(tmp_path, monkeypatch)
    try:
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        config["common_env"] = {"ANTHROPIC_AUTH_TOKEN": "raw-yaml-secret"}
        source = "# 原始注释只保留在磁盘文件中\n" + yaml.safe_dump(config, sort_keys=False)
        config_path.write_text(source, encoding="utf-8")
        before = config_path.stat().st_mtime_ns

        document = infra.get_dispatcher_yaml()

        assert "raw-yaml-secret" not in document.content
        assert "[REDACTED]" in document.content
        assert document.revision == sha256(source.encode("utf-8")).hexdigest()
        assert config_path.read_text(encoding="utf-8") == source
        assert config_path.stat().st_mtime_ns == before
    finally:
        conn.close()


@pytest.mark.parametrize("engine", ["claudecode", "codex", "mock", "future-engine"])
def test_raw_yaml_put_rejects_unsupported_legacy_worker_types(tmp_path: Path, monkeypatch, engine: str) -> None:
    conn, _runtime, config_path = configure_server_first_store(tmp_path, monkeypatch)
    try:
        initial = infra.get_dispatcher_yaml()
        original_content = config_path.read_text(encoding="utf-8")
        config = yaml.safe_load(initial.content)
        config["providers"] = [
            {
                "id": "provider-openai",
                "base_url": "https://api.example.test",
                "provider_api": "openai-responses",
                "credential_ref": "LOCAL_OPENAI_KEY",
            }
        ]
        config["models"] = [
            {
                "id": "model-codex",
                "provider_id": "provider-openai",
                "upstream_model": "gpt-test",
            }
        ]
        config["common_env"] = {"LOCAL_OPENAI_KEY": "plaintext-key"}
        config["workers"] = [
            {
                "name": "legacy-codex",
                "type": engine,
                "model_id": "model-codex",
                "task_types": ["reason"],
                "max_running": 1,
                "priority": 0,
                "env": {"CODEX_EXTRA": "legacy-value"},
            }
        ]
        raw = "# legacy worker\n" + yaml.safe_dump(config, sort_keys=False)

        with pytest.raises(HTTPException) as exc:
            infra.put_dispatcher_yaml(
                infra.DispatcherYamlUpdate(
                    content=raw,
                    revision=initial.revision,
                )
            )

        assert exc.value.status_code == 422
        assert "Invalid dispatch.yaml" in str(exc.value.detail)
        assert config_path.read_text(encoding="utf-8") == original_content
    finally:
        conn.close()


def test_structured_worker_reads_redact_all_environment_values(tmp_path: Path, monkeypatch) -> None:
    conn, _runtime, config_path = configure_server_first_store(tmp_path, monkeypatch)
    try:
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        config["workers"] = [
            {
                "id": "worker-secret",
                "type": "claudecode",
                "task_types": ["reason"],
                "max_running": 1,
                "priority": 0,
                "env": {
                    "AUTH": "unexpected-secret-name",
                    "ANTHROPIC_MODEL": "model-name-is-also-hidden",
                },
            }
        ]
        config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

        collection = infra.list_dispatcher_workers()
        item = infra.get_dispatcher_worker("worker-secret")

        assert collection.items[0]["env"] == {
            "AUTH": "[REDACTED]",
            "ANTHROPIC_MODEL": "[REDACTED]",
        }
        assert "unexpected-secret-name" not in str(collection.items)
        assert "model-name-is-also-hidden" not in str(item.item)
    finally:
        conn.close()


@pytest.mark.parametrize("engine", ["claudecode", "codex", "mock", "future-engine"])
def test_structured_worker_api_only_accepts_pi_without_env(tmp_path: Path, monkeypatch, engine: str) -> None:
    conn, _runtime, _config_path = configure_server_first_store(tmp_path, monkeypatch)
    try:
        revision = infra.list_dispatcher_workers().revision
        base = {
            "id": "worker-a",
            "model_id": "model-a",
            "task_types": ["reason"],
            "concurrency": 1,
            "priority": 0,
        }
        with pytest.raises(HTTPException) as engine_error:
            infra.create_dispatcher_worker(
                DispatcherControlUpdate(revision=revision, worker=base | {"engine": engine})
            )
        assert engine_error.value.status_code == 422

        with pytest.raises(HTTPException) as env_error:
            infra.create_dispatcher_worker(
                DispatcherControlUpdate(
                    revision=revision,
                    worker=base | {"engine": "pi", "env": {"ANY": "value"}},
                )
            )
        assert env_error.value.status_code == 422
    finally:
        conn.close()


def test_credential_delete_rejects_shared_reference(tmp_path: Path, monkeypatch) -> None:
    conn, _runtime, _config_path = configure_server_first_store(tmp_path, monkeypatch)
    try:
        initial = infra.get_dispatcher_config()
        shared_ref = "SHARED_PROVIDER_KEY"
        config = dict(initial.config)
        config["providers"] = [
            {
                "id": "provider-a",
                "base_url": "https://a.example.test",
                "provider_api": "anthropic-messages",
                "credential_ref": shared_ref,
            },
            {
                "id": "provider-b",
                "base_url": "https://b.example.test",
                "provider_api": "anthropic-messages",
                "credential_ref": shared_ref,
            },
        ]
        config["common_env"] = {shared_ref: "shared-value"}
        saved = infra.put_dispatcher_config(
            DispatcherConfigUpdate(config=config, revision=initial.revision)
        )

        with pytest.raises(HTTPException) as shared:
            infra.delete_dispatcher_provider_credential(
                "provider-a",
                DispatcherRevisionRequest(revision=saved.revision),
            )
        assert shared.value.status_code == 409
        assert infra.get_dispatcher_config().config["common_env"][shared_ref] == "[REDACTED]"
    finally:
        conn.close()


def test_provider_credential_rejects_blank_values() -> None:
    with pytest.raises(ValueError, match="credential must not be blank"):
        DispatcherCredentialUpdate(revision="revision", credential="   ")


def test_provider_ref_is_generated_on_create_and_preserved_when_update_omits_it(
    tmp_path: Path,
    monkeypatch,
) -> None:
    conn, _runtime, _config_path = configure_server_first_store(tmp_path, monkeypatch)
    try:
        initial = infra.list_dispatcher_providers()
        created = infra.create_dispatcher_provider(
            DispatcherControlUpdate(
                revision=initial.revision,
                item={
                    "id": "provider-auto",
                    "base_url": "https://old.example.test",
                    "provider_api": "anthropic-messages",
                },
            )
        )
        generated_ref = provider_credential_ref("provider-auto")
        assert created.config["providers"][0]["credential_ref"] == generated_ref

        credential = infra.put_dispatcher_provider_credential(
            "provider-auto",
            DispatcherCredentialUpdate(revision=created.revision, credential="direct-key-value"),
        )
        updated = infra.update_dispatcher_provider(
            "provider-auto",
            DispatcherControlUpdate(
                revision=credential.revision,
                item={
                    "id": "provider-auto",
                    "base_url": "https://new.example.test",
                    "provider_api": "anthropic-messages",
                },
            ),
        )
        assert updated.config["providers"][0]["credential_ref"] == generated_ref
        assert updated.config["common_env"] == {generated_ref: "[REDACTED]"}
        assert "direct-key-value" not in updated.content
    finally:
        conn.close()


def test_provider_update_without_ref_preserves_shared_legacy_reference(tmp_path: Path, monkeypatch) -> None:
    conn, _runtime, _config_path = configure_server_first_store(tmp_path, monkeypatch)
    try:
        initial = infra.get_dispatcher_config()
        shared_ref = "SHARED_LEGACY_KEY"
        config = dict(initial.config)
        config["providers"] = [
            {
                "id": "provider-a",
                "base_url": "https://a.example.test",
                "provider_api": "anthropic-messages",
                "credential_ref": shared_ref,
            },
            {
                "id": "provider-b",
                "base_url": "https://b.example.test",
                "provider_api": "anthropic-messages",
                "credential_ref": shared_ref,
            },
        ]
        config["common_env"] = {shared_ref: "shared-key"}
        saved = infra.put_dispatcher_config(
            DispatcherConfigUpdate(config=config, revision=initial.revision)
        )

        updated = infra.update_dispatcher_provider(
            "provider-a",
            DispatcherControlUpdate(
                revision=saved.revision,
                item={
                    "id": "provider-a",
                    "base_url": "https://changed.example.test",
                    "provider_api": "anthropic-messages",
                },
            ),
        )
        assert {item["credential_ref"] for item in updated.config["providers"]} == {shared_ref}
        assert updated.config["common_env"] == {shared_ref: "[REDACTED]"}
        assert "shared-key" not in updated.content
    finally:
        conn.close()


def test_disabled_worker_can_be_saved_before_provider_credential(tmp_path: Path, monkeypatch) -> None:
    conn, _runtime, config_path = configure_server_first_store(tmp_path, monkeypatch)
    try:
        providers = infra.list_dispatcher_providers()
        provider = infra.create_dispatcher_provider(
            DispatcherControlUpdate(
                revision=providers.revision,
                item={
                    "id": "provider-draft",
                    "base_url": "https://api.example.test",
                    "provider_api": "anthropic-messages",
                    "credential_ref": "PROVIDER_DRAFT_KEY",
                    "inference_endpoint": "https://api.example.test/v1/messages",
                },
            )
        )
        model = infra.create_dispatcher_model(
            DispatcherControlUpdate(
                revision=provider.revision,
                item={
                    "id": "model-draft",
                    "provider_id": "provider-draft",
                    "upstream_model": "claude-test",
                },
            )
        )
        worker_payload = {
            "id": "worker-draft",
            "engine": "pi",
            "model_id": "model-draft",
            "task_types": ["bootstrap", "reason", "explore"],
            "concurrency": 1,
            "priority": 0,
            "enabled": False,
        }
        with pytest.raises(HTTPException) as unknown_model:
            infra.create_dispatcher_worker(
                DispatcherControlUpdate(
                    revision=model.revision,
                    item=worker_payload | {"model_id": "missing-model"},
                )
            )
        assert unknown_model.value.status_code == 422

        worker = infra.create_dispatcher_worker(
            DispatcherControlUpdate(revision=model.revision, item=worker_payload)
        )
        assert DispatchConfig.load(config_path).workers[0].enabled is False

        with pytest.raises(HTTPException) as missing_credential:
            infra.update_dispatcher_worker(
                "worker-draft",
                DispatcherControlUpdate(
                    revision=worker.revision,
                    item=worker_payload | {"enabled": True},
                ),
            )
        assert missing_credential.value.status_code == 422

        credential = infra.put_dispatcher_provider_credential(
            "provider-draft",
            DispatcherCredentialUpdate(revision=worker.revision, credential="draft-token"),
        )
        enabled = infra.update_dispatcher_worker(
            "worker-draft",
            DispatcherControlUpdate(
                revision=credential.revision,
                item=worker_payload | {"enabled": True},
            ),
        )
        loaded = DispatchConfig.load(config_path)
        assert enabled.config["workers"][0]["enabled"] is True
        assert loaded.workers[0].model_binding is not None
        assert loaded.workers[0].model_binding.api_key == "draft-token"
    finally:
        conn.close()


@pytest.mark.parametrize("field", ["engine", "type"])
@pytest.mark.parametrize("engine", ["claudecode", "codex", "mock", "future-engine"])
def test_historical_worker_reads_preserve_engine_and_updates_cannot_convert_it(
    tmp_path: Path, monkeypatch, field: str, engine: str,
) -> None:
    conn, _runtime, config_path = configure_server_first_store(tmp_path, monkeypatch)
    try:
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        config["workers"] = [{"id": "historical", field: engine, "task_types": ["reason"]}]
        original = yaml.safe_dump(config, sort_keys=False)
        config_path.write_text(original, encoding="utf-8")
        revision = infra.get_dispatcher_config().revision

        assert infra.list_dispatcher_workers().items[0][field] == engine
        assert infra.get_dispatcher_worker("historical").item[field] == engine
        assert yaml.safe_load(infra.get_dispatcher_yaml().content)["workers"][0][field] == engine
        assert config_path.read_text(encoding="utf-8") == original
        for extra in ({}, {"engine": "pi"}):
            with pytest.raises(HTTPException) as rejected:
                infra.update_dispatcher_worker(
                    "historical",
                    DispatcherControlUpdate(
                        revision=revision,
                        item={"id": "historical", "task_types": ["reason"]} | extra,
                    ),
                )
            assert rejected.value.status_code == 422
            assert "engine_unsupported" in str(rejected.value.detail)
            assert config_path.read_text(encoding="utf-8") == original
    finally:
        conn.close()


def test_new_structured_worker_defaults_to_pi(tmp_path: Path, monkeypatch) -> None:
    conn, _runtime, config_path = configure_server_first_store(tmp_path, monkeypatch)
    try:
        revision = infra.list_dispatcher_workers().revision
        created = infra.create_dispatcher_worker(
            DispatcherControlUpdate(
                revision=revision,
                item={"id": "candidate", "task_types": ["reason"], "priority": 0, "concurrency": 1, "enabled": False},
            )
        )
        assert created.config["workers"][0]["engine"] == "pi"
        assert DispatchConfig.load(config_path).workers[0].type == "pi"
    finally:
        conn.close()


@pytest.mark.parametrize("raw_yaml", [False, True])
def test_pi_binding_and_custom_headers_are_redacted_and_round_trip_by_identity(
    tmp_path: Path, monkeypatch, raw_yaml: bool,
) -> None:
    conn, _runtime, config_path = configure_server_first_store(tmp_path, monkeypatch)
    try:
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        config["providers"] = [{
            "id": "provider-headers", "base_url": "https://api.example.test",
            "custom_headers": [{"name": "X-Provider-Token", "value": "provider-test-value"}],
        }]
        config["workers"] = [{
            "id": worker_id, "type": "pi", "task_types": ["reason"], "priority": 0, "max_running": 1,
            "model_binding": {
                "protocol": "openai-responses", "model_name": "test-model",
                "base_url": "https://api.example.test",
                "inference_endpoint": "https://api.example.test/custom/infer?version=test",
                "auth_header": "Authorization", "api_key": f"test-key-{worker_id}",
                "custom_headers": [
                    {"name": "X-First", "value": f"test-first-{worker_id}"},
                    {"name": "X-Second", "value": f"test-second-{worker_id}"},
                ],
            },
        } for worker_id in ("worker-a", "worker-b")]
        config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
        original = config_path.read_bytes()
        document = infra.get_dispatcher_config()
        assert config_path.read_bytes() == original
        assert document.config["workers"][0]["model_binding"]["api_key"] == "[REDACTED]"
        assert document.config["providers"][0]["custom_headers"][0]["value"] == "[REDACTED]"
        for text in (document.content, str(infra.list_dispatcher_workers().items)):
            assert "test-key-" not in text
            assert "test-first-" not in text
            assert "test-second-" not in text
            assert "provider-test-value" not in text

        # 重排 Worker/头并编辑同级字段，确保占位符按身份还原而非按位置串线。
        edited = document.config
        edited["workers"].reverse()
        for worker in edited["workers"]:
            binding = worker["model_binding"]
            binding["model_name"] = "edited-model"
            binding["custom_headers"].reverse()
            binding["custom_headers"][0]["value"] = "new-explicit-header"
        edited["providers"][0]["base_url"] = "https://edited.example.test"
        if raw_yaml:
            saved = infra.put_dispatcher_yaml(infra.DispatcherYamlUpdate(
                content=yaml.safe_dump(edited), revision=document.revision,
            ))
        else:
            saved = infra.put_dispatcher_config(DispatcherConfigUpdate(
                config=edited, revision=document.revision,
            ))
        for secret in ("test-key-", "test-first-", "provider-test-value", "new-explicit-header"):
            assert secret not in saved.content
        loaded = DispatchConfig.load(config_path)
        assert loaded.providers[0].custom_headers[0].value == "provider-test-value"
        for worker in loaded.workers:
            binding = worker.model_binding
            assert binding is not None
            assert binding.api_key == f"test-key-{worker.id}"
            assert binding.model_name == "edited-model"
            assert [(header.name, header.value) for header in binding.custom_headers] == [
                ("X-Second", "new-explicit-header"), ("X-First", f"test-first-{worker.id}"),
            ]
    finally:
        conn.close()
