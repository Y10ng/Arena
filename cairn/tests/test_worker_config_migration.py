from __future__ import annotations

import importlib.util
from pathlib import Path
import stat
import sys

import pytest
import yaml

from cairn.server.services.dispatcher_config_store import provider_credential_ref


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "migrate_worker_config", ROOT / "deploy/migrate_worker_config.py"
)
assert SPEC and SPEC.loader
migrate_worker_config = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = migrate_worker_config
SPEC.loader.exec_module(migrate_worker_config)


def _runtime(tmp_path: Path, *, reference: str = "secret://provider-main") -> tuple[Path, Path]:
    runtime = tmp_path / "runtime"
    secrets = runtime / "secrets"
    secrets.mkdir(parents=True)
    config_path = runtime / "dispatch.yaml"
    config = yaml.safe_load((ROOT / "deploy/dispatch.yaml.example").read_text(encoding="utf-8"))
    config["providers"] = [
        {
            "id": "provider-main",
            "base_url": "https://api.example.test",
            "provider_api": "anthropic-messages",
            "credential_ref": reference,
        }
    ]
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    config_path.chmod(0o600)
    return config_path, secrets


def test_migrates_provider_secret_with_backup_and_is_idempotent(tmp_path: Path) -> None:
    config_path, secrets = _runtime(tmp_path)
    secret_path = secrets / "provider-main.secret"
    secret_path.write_text("top-secret-value\n", encoding="utf-8")
    secret_path.chmod(0o600)

    result = migrate_worker_config.migrate(config_path=config_path, secrets_dir=secrets)

    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    credential_ref = provider_credential_ref("provider-main")
    assert result.provider_count == 1
    assert config["providers"][0]["credential_ref"] == credential_ref
    assert config["common_env"][credential_ref] == "top-secret-value"
    assert result.backup_path is not None and result.backup_path.exists()
    assert stat.S_IMODE(result.backup_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
    assert secret_path.read_text(encoding="utf-8") == "top-secret-value\n"

    before = config_path.read_bytes()
    second = migrate_worker_config.migrate(config_path=config_path, secrets_dir=secrets)
    assert second.changed is False
    assert config_path.read_bytes() == before
    assert len(list(config_path.parent.glob("dispatch.yaml.pre-worker-migration.bak*"))) == 1


def test_shared_legacy_secret_gets_provider_specific_refs(tmp_path: Path) -> None:
    config_path, secrets = _runtime(tmp_path, reference="secret://shared")
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["providers"].append(
        {
            "id": "provider-secondary",
            "base_url": "https://secondary.example.test",
            "provider_api": "anthropic-messages",
            "credential_ref": "secret://shared",
        }
    )
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    (secrets / "shared.secret").write_text("shared-value", encoding="utf-8")

    migrate_worker_config.migrate(config_path=config_path, secrets_dir=secrets)

    migrated = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    refs = [provider["credential_ref"] for provider in migrated["providers"]]
    assert refs == [
        provider_credential_ref("provider-main"),
        provider_credential_ref("provider-secondary"),
    ]
    assert {migrated["common_env"][ref] for ref in refs} == {"shared-value"}


def test_missing_referenced_secret_changes_nothing(tmp_path: Path) -> None:
    config_path, secrets = _runtime(tmp_path)
    before_yaml = config_path.read_bytes()

    with pytest.raises(migrate_worker_config.MigrationError, match="缺少 secret 文件"):
        migrate_worker_config.migrate(config_path=config_path, secrets_dir=secrets)

    assert config_path.read_bytes() == before_yaml
    assert not list(config_path.parent.glob("dispatch.yaml.pre-worker-migration.bak*"))


def test_rejects_secret_symlink_without_exposing_value(tmp_path: Path) -> None:
    config_path, secrets = _runtime(tmp_path)
    outside = tmp_path / "outside.secret"
    outside.write_text("must-not-appear", encoding="utf-8")
    (secrets / "provider-main.secret").symlink_to(outside)

    with pytest.raises(migrate_worker_config.MigrationError) as error:
        migrate_worker_config.migrate(config_path=config_path, secrets_dir=secrets)
    assert "must-not-appear" not in str(error.value)
