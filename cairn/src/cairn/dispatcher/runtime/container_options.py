"""容器运行参数归一化和签名。"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json

from cairn.dispatcher.advisory_bundle import ADVISORY_CONTAINER_PATH, AdvisoryBundle


@dataclass(frozen=True, slots=True)
class ContainerRuntimeOptions:
    """场景解析后的容器运行参数；runtime_signature 用于识别复用容器是否匹配。"""

    image: str
    network_mode: str
    dns: tuple[str, ...]
    cap_add: tuple[str, ...]
    features: dict[str, object]
    scenario_key: str
    mem_limit: int | None
    memswap_limit: int | None
    pids_limit: int | None
    advisory_mount_source: str | None
    advisory_mount_target: str | None
    advisory_digest: str | None
    advisory_format_version: int | None
    advisory_legacy_key: str | None
    advisory_resource_count: int
    advisory_manifest: dict[str, object] | None
    runtime_signature: str

    @classmethod
    def build(
        cls,
        *,
        image: str,
        network_mode: str,
        dns: list[str] | tuple[str, ...],
        cap_add: list[str] | tuple[str, ...],
        features: dict[str, object] | None,
        scenario_key: str,
        mem_limit: int | None = None,
        memswap_limit: int | None = None,
        pids_limit: int | None = None,
        advisory_bundle: AdvisoryBundle | None = None,
    ) -> "ContainerRuntimeOptions":
        cleaned_caps = tuple(sorted({cap.strip() for cap in cap_add if cap.strip()}))
        cleaned_dns = tuple(dict.fromkeys(address.strip() for address in dns if address.strip()))
        cleaned_features = dict(features or {})
        advisory_mount_source = advisory_bundle.source_path if advisory_bundle is not None else None
        advisory_mount_target = ADVISORY_CONTAINER_PATH if advisory_bundle is not None else None
        advisory_digest = advisory_bundle.digest if advisory_bundle is not None else None
        advisory_format_version = 1 if advisory_bundle is not None else None
        advisory_legacy_key = advisory_bundle.legacy_key if advisory_bundle is not None else None
        advisory_resource_count = advisory_bundle.resource_count if advisory_bundle is not None else 0
        advisory_manifest = dict(advisory_bundle.manifest) if advisory_bundle is not None else None
        # 签名只包含会影响容器运行语义的字段，label 对比即可判断是否需要重建。
        payload = {
            "image": image,
            "network_mode": network_mode,
            "dns": list(cleaned_dns),
            "cap_add": list(cleaned_caps),
            "features": cleaned_features,
            "scenario_key": scenario_key,
            "mem_limit": mem_limit,
            "memswap_limit": memswap_limit,
            "pids_limit": pids_limit,
            "advisory_mount_target": advisory_mount_target,
            "advisory_digest": advisory_digest,
            "advisory_format_version": advisory_format_version,
        }
        signature = sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        return cls(
            image=image,
            network_mode=network_mode,
            dns=cleaned_dns,
            cap_add=cleaned_caps,
            features=cleaned_features,
            scenario_key=scenario_key,
            mem_limit=mem_limit,
            memswap_limit=memswap_limit,
            pids_limit=pids_limit,
            advisory_mount_source=advisory_mount_source,
            advisory_mount_target=advisory_mount_target,
            advisory_digest=advisory_digest,
            advisory_format_version=advisory_format_version,
            advisory_legacy_key=advisory_legacy_key,
            advisory_resource_count=advisory_resource_count,
            advisory_manifest=advisory_manifest,
            runtime_signature=signature,
        )
