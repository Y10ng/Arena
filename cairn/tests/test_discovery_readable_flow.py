"""仅用私有 PostgreSQL 与合成原件贯通发现到导出，不调用模型或目标。"""

from __future__ import annotations

import base64
from contextlib import contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path

from fastapi import HTTPException
import pytest

from cairn.dispatcher.contracts import extract_vulnerability, validate_explore_payload
from cairn.dispatcher.tasks.common_base import ProcessArtifactRecord
from cairn.dispatcher.tasks.conclude_writer import build_vulnerability_for_conclude
from cairn.dispatcher.worker_protocol.envelope import canonical_result_example
from cairn.dispatcher.worker_protocol.submission import Finding
from cairn.evidence_contracts import EvidenceItem
from cairn.report_readable import readable_markdown
from cairn.server.artifact_paths import resolve_artifact_path
from cairn.server.models import ConcludeRequest, CreateArtifactRequest, EvidenceRef
from cairn.server.report_template_defaults_v60 import DEFAULT_REPORT_TEMPLATE_KEY_V60
from cairn.server.routers import audit as audit_router, intents as intents_router
from cairn.server.services import (
    assessment_reports, audit_artifacts, model_credentials, report_templates, vulnerabilities, vulnerability_reports,
)
from test_report_templates import defaults_v60_conn, private_postgres, _migrate_defaults_v60


PROJECT = "controlled-discovery"
IDENTITY = {
    "vulnerability_class": "authorization",
    "asset": {"protocol": "https", "host": "fixture.invalid", "port": 443},
    "entrypoint": {"method": "GET", "route": "/objects/{id}", "locus": "include"},
    "vector": {"source_role": "viewer", "object_type": "fixture-object"},
}
DESCRIPTION = "只读角色对合成对象 fixture-42 添加 include=detail 后绕过对象归属检查；已保留拒绝访问的基线。"
REMEDIATION = "在 include 分支之前统一检查对象归属，并以同一身份和对象回归两种请求。"
TAIL = "受控尾部结果：object_id=fixture-42；owner=other-fixture；读取成功。"
NOISE = "无关采集流水账，不进入发现正文。"
HISTORIC_STEPS = "历史人工步骤，不冒充本次发现。"
HISTORIC_REMEDIATION = "历史人工建议，来源是漏洞记录。"


@pytest.fixture
def discovery_state(defaults_v60_conn, monkeypatch, tmp_path):
    conn = defaults_v60_conn
    _migrate_defaults_v60(conn, max_version=62)
    conn.execute(
        """INSERT INTO scenarios (key, label, profile_id, prompt_group, knowledge, features_json)
        VALUES ('controlled-discovery', '受控发现', 'default', 'default', '', '{}')"""
    )
    conn.execute("INSERT INTO tenants VALUES ('controlled-tenant', 'personal', '受控合成租户', 'synthetic')")
    conn.execute(
        """INSERT INTO projects (id, title, status, created_at, scenario_key, scenario_snapshot_json, tenant_id)
        VALUES (?, '受控发现', 'active', 'synthetic', 'controlled-discovery', ?, 'controlled-tenant')""",
        (PROJECT, json.dumps({"features": {"execution_contract_version": 1, "execution_engine": "pi"}})),
    )
    conn.execute(
        "INSERT INTO facts (id, project_id, description) VALUES ('historic-fact', ?, '历史只留下问题说明，没有操作过程。')",
        (PROJECT,),
    )
    conn.execute(
        """INSERT INTO vulnerabilities
        (id, project_id, title, severity, source_fact_id, repro_steps, remediation, created_at, updated_at)
        VALUES ('historic-vulnerability', ?, '历史记录', 'low', 'historic-fact', ?, ?, 'synthetic', 'synthetic')""",
        (PROJECT, HISTORIC_STEPS, HISTORIC_REMEDIATION),
    )
    conn.execute(
        """INSERT INTO vulnerability_observations
        (id, project_id, vulnerability_id, source_fact_id, severity, confidence, observed_at)
        VALUES ('historic-observation', ?, 'historic-vulnerability', 'historic-fact', 'low', 0.5, 'synthetic')""",
        (PROJECT,),
    )
    historic_before = dict(conn.execute("SELECT * FROM vulnerabilities WHERE id = 'historic-vulnerability'").fetchone())
    _migrate_defaults_v60(conn, max_version=63)
    assert conn.execute("SELECT max(version) AS version FROM schema_migrations").fetchone()["version"] == 63
    assert dict(conn.execute("SELECT * FROM vulnerabilities WHERE id = 'historic-vulnerability'").fetchone()) == historic_before
    # 交付检查真实核验当次凭据来源；临时随机材料不接触应用 keyring 或模型端点。
    keyring = tmp_path / "synthetic-keyring.json"
    keyring.write_text(json.dumps({"active_version": "test", "keys": {
        "test": base64.b64encode(os.urandom(32)).decode("ascii"),
    }}), encoding="utf-8")
    keyring.chmod(0o600)
    monkeypatch.setenv(model_credentials.KEYRING_FILE_ENV, str(keyring))
    tenant_id = conn.execute("SELECT tenant_id FROM projects WHERE id = ?", (PROJECT,)).fetchone()["tenant_id"]
    conn.execute("""INSERT INTO users (id, login, password_hash, role, created_at, updated_at)
        VALUES ('controlled-owner', 'controlled-owner', 'not-a-login-hash', 'user', 'synthetic', 'synthetic')""")
    conn.execute("""INSERT INTO tenant_models
        (id, tenant_id, display_name, protocol, base_url, model_name, created_by, created_at, updated_at)
        VALUES ('controlled-model', ?, '合成来源', 'openai-responses', 'https://fixture.invalid/',
                'never-called', 'controlled-owner', 'synthetic', 'synthetic')""", (tenant_id,))
    model_credentials.set_or_rotate_credential(
        conn, tenant_id=tenant_id, model_id="controlled-model", plaintext=os.urandom(32).hex(),
    )
    _, credential = model_credentials.decrypt_active_credential(conn, tenant_id=tenant_id, model_id="controlled-model")
    binding = {"model_id": "controlled-model", "model_revision": 1,
               "credential_version_id": credential["credential_version_id"],
               "credential_fingerprint": credential["credential_fingerprint"]}
    report_templates.create_report_template(
        conn, "controlled-step-report", "受控步骤模板", "以逐步操作展示同一发现。",
        """# {{ report_title }}
{{#findings}}
## 问题与范围：{{ finding.title }}
{{ finding.description }}
{{ finding.limitations }}
## 按步骤复现
{{ finding.recorded_steps }}
## 关键原件
{{ finding.key_evidence }}
## 修复建议
{{ finding.remediation }}
{{/findings}}
""",
    )
    conn.commit()
    root = tmp_path / "artifacts"

    @contextmanager
    def get_conn():
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    for module in (intents_router, audit_artifacts):
        monkeypatch.setattr(module, "get_conn", get_conn)
    for module in (audit_artifacts, assessment_reports):
        monkeypatch.setattr(module, "artifact_root", lambda: root)
    return {"conn": conn, "root": root, "originals": {}, "binding": binding}


@pytest.fixture
def goal_driven_manual_state(defaults_v60_conn, monkeypatch, tmp_path):
    from cairn.server import db

    conn = defaults_v60_conn
    # 空 schema 先迁移，避免把报告历史夹具的合成用户带入固定 Worker 数据迁移。
    db.run_migrations(conn, "postgresql")
    conn.execute(
        """INSERT INTO scenarios (key, label, profile_id, prompt_group, knowledge, features_json)
        VALUES ('controlled-manual', '受控人工证据', 'default', 'goal-driven-v1', '', '{}')"""
    )
    conn.execute(
        """INSERT INTO projects (id, title, status, created_at, scenario_key, scenario_snapshot_json)
        VALUES (?, '受控人工证据', 'active', 'synthetic', 'controlled-manual', ?)""",
        (PROJECT, json.dumps({"features": {
            "execution_contract_version": 1, "execution_engine": "pi",
        }})),
    )
    conn.execute(
        "INSERT INTO facts (id, project_id, description) VALUES ('historic-fact', ?, '合成来源')",
        (PROJECT,),
    )
    conn.commit()
    root = tmp_path / "manual-artifacts"

    @contextmanager
    def get_conn():
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    for module in (intents_router, audit_artifacts):
        monkeypatch.setattr(module, "get_conn", get_conn)
    monkeypatch.setattr(audit_artifacts, "artifact_root", lambda: root)
    return {"conn": conn, "root": root, "originals": {}, "binding": {}}


def _submit_finding(
    state, name, *, exchanges, steps, description, remediation, identity_enabled,
    source="capture", require_current_run_reproduction=True, assessment_record=None,
):
    conn = state["conn"]
    intent_id, run_id = f"intent-{name}", f"run-{name}"
    conn.execute(
        """INSERT INTO intents (id, project_id, description, creator, worker, created_at)
        VALUES (?, ?, '受控操作', 'synthetic', 'fixture-worker', 'synthetic')""",
        (intent_id, PROJECT),
    )
    conn.execute(
        """INSERT INTO task_runs
        (id, project_id, intent_id, task_type, worker_name, worker_type, status, session_id, started_at, runtime_context_json)
        VALUES (?, ?, ?, 'explore', 'fixture-worker', 'pi', 'running', ?, 'synthetic', ?)""",
        (run_id, PROJECT, intent_id, f"session-{name}", json.dumps({"execution_binding": state["binding"]})),
    )
    conn.execute(
        "INSERT INTO intent_sources (intent_id, project_id, fact_id) VALUES (?, ?, 'historic-fact')",
        (intent_id, PROJECT),
    )
    handles, items, declarations = {}, [], []
    for item_id, sides in exchanges:
        item = {"id": item_id, "kind": "http", "source": source, "title": item_id}
        for side, text in sides.items():
            raw = text.encode("utf-8")
            handle = f"{name}-{item_id}-{side}"
            artifact = audit_router.create_artifact(PROJECT, CreateArtifactRequest(
                task_run_id=run_id, kind="file", category="evidence", filename=f"{handle}.http", content=text,
                metadata={"declared_by_worker": True, "registered_sha256": sha256(raw).hexdigest(),
                          "registered_size_bytes": len(raw)},
            ))
            handles[handle] = artifact.id
            item[side] = {"artifact_handle": handle}
            declarations.append({"handle": handle, "kind": "file", "path": f"/tmp/controlled/{handle}.http",
                                 "source_path": f"/tmp/controlled/{handle}.http",
                                 "sha256": artifact.sha256, "size_bytes": len(raw)})
            state["originals"][artifact.id] = (artifact.model_dump(), raw)
            if side == "response" and TAIL in text:
                start = raw.index(TAIL.encode("utf-8"))
                item["excerpts"] = [{"side": "response", "pointer": f"utf8-bytes={start}:{start + len(TAIL.encode('utf-8'))}"}]
        items.append(item)
    finding = Finding.model_validate({
        "schema_version": 2, "title": "对象归属检查绕过", "severity": "high", "vuln_type": "IDOR",
        "target": "GET /objects/{id}?include=detail", "identity": IDENTITY, "confidence": 0.8,
        "remediation": remediation, "reproduction_steps": steps, "evidence_items": items,
    })
    payload = canonical_result_example("explore_result", {
        "fact": {"description": description, "vulnerability": finding.model_dump(exclude_none=True)},
        "artifacts": declarations,
    })
    assert validate_explore_payload(payload, require_vulnerability_identity=identity_enabled) == ("fact", description)
    extracted = extract_vulnerability(payload)
    assert extracted["reproduction_steps"] == steps
    declared = ProcessArtifactRecord(
        refs=[EvidenceRef(artifact_id=value) for value in handles.values()], artifact_ids_by_handle=handles,
    )
    wire = build_vulnerability_for_conclude(
        payload, declared,
        require_current_run_reproduction=require_current_run_reproduction,
    )
    assert wire["reproduction_steps"] == steps
    assert all(ref["artifact_id"] in handles.values() for item in wire["evidence_items"]
               for side in ("request", "response") if (ref := item.get(side)) is not None)
    result = intents_router.conclude(PROJECT, intent_id, ConcludeRequest(
        worker="fixture-worker", description=description, task_run_id=run_id, vulnerability=wire,
        assessment_record=assessment_record,
    ))
    observation = conn.execute(
        "SELECT * FROM vulnerability_observations WHERE project_id = ? AND source_fact_id = ?",
        (PROJECT, result.fact.id),
    ).fetchone()
    assert observation is not None and observation["task_run_id"] == run_id
    assert json.loads(observation["reproduction_steps_json"]) == steps
    assert observation["remediation"] == remediation
    assert json.loads(observation["evidence_items_json"]) == wire["evidence_items"]
    return observation


def test_goal_driven_manual_evidence_uses_server_ownership_validation(goal_driven_manual_state):
    state, conn = goal_driven_manual_state, goal_driven_manual_state["conn"]
    snapshot = json.loads(conn.execute(
        "SELECT scenario_snapshot_json FROM projects WHERE id = ?", (PROJECT,),
    ).fetchone()["scenario_snapshot_json"])
    snapshot["features"].update({
        "assessment_protocol": "goal-driven-v1", "assessment_contract_version": 2,
        "outcome_contract_version": 2, "verifier_contract_version": 1,
    })
    conn.execute(
        "UPDATE projects SET assessment_protocol = 'goal-driven-v1', "
        "scenario_snapshot_json = ?, vulnerability_fingerprint_version = 1 WHERE id = ?",
        (json.dumps(snapshot), PROJECT),
    )
    conn.commit()
    observation = _submit_finding(
        state, "manual",
        exchanges=[("manual-current", {
            "request": "GET /manual-fixture HTTP/1.1\r\nHost: fixture.invalid\r\n\r\n",
            "response": "HTTP/1.1 200 OK\r\n\r\n合成人工证据",
        })],
        steps=[{"action": "在当前 Run 登记合成人工原件。",
                "observation": "Server 按项目和 Run 归属接受。",
                "evidence_item_ids": ["manual-current"]}],
        description="goal-driven 当前 Run 的 manual Artifact",
        remediation="仅用于合成回归。", identity_enabled=True, source="manual",
        require_current_run_reproduction=False,
        assessment_record={"version": 1, "kind": "experiment",
                           "hypothesis": "当前人工原件可证明合成发现",
                           "outcome": "finding", "observations": ["人工原件属于当前 Run"]},
    )
    items = json.loads(observation["evidence_items_json"])
    assert [item["source"] for item in items] == ["manual"]
    canonical = conn.execute(
        "SELECT status FROM vulnerabilities WHERE id = ?", (observation["vulnerability_id"],),
    ).fetchone()
    assert canonical["status"] == "new"

    current_items = [EvidenceItem.model_validate(item) for item in items]
    with pytest.raises(HTTPException, match="discovery_evidence_current_run_required"):
        vulnerabilities._validate_evidence_items(
            conn, PROJECT, current_items, discovery=True, task_run_id="another-run",
        )

    conn.execute(
        "INSERT INTO projects (id, title, status, created_at) "
        "VALUES ('other-project', '其他合成项目', 'active', 'synthetic')"
    )
    conn.execute(
        """INSERT INTO task_runs
        (id, project_id, task_type, worker_name, worker_type, status, started_at)
        VALUES ('other-run', 'other-project', 'explore', 'fixture-worker', 'pi', 'running', 'synthetic')"""
    )
    other = [
        audit_router.create_artifact("other-project", CreateArtifactRequest(
            task_run_id="other-run", kind="file", category="evidence",
            filename=f"other-{index}.txt", content="其他项目合成原件",
        ))
        for index in range(3)
    ][-1]
    assert other.id not in {
        ref["artifact_id"] for item in items for side in ("request", "response")
        if (ref := item.get(side)) is not None
    }
    cross_project = [EvidenceItem.model_validate({
        "id": "cross-project", "kind": "artifact", "source": "manual",
        "artifact": {"artifact_id": other.id},
    })]
    with pytest.raises(HTTPException):
        vulnerabilities._validate_evidence_items(
            conn, PROJECT, cross_project, discovery=True, task_run_id="run-manual",
        )
    invalid_flow = [EvidenceItem.model_validate({
        "id": "invalid-flow", "kind": "http", "source": "capture",
        "flow_id": "missing-flow",
    })]
    with pytest.raises(HTTPException):
        vulnerabilities._validate_evidence_items(
            conn, PROJECT, invalid_flow, discovery=True, task_run_id="run-manual",
        )


@pytest.mark.parametrize("identity_enabled", [False, True], ids=["create", "register"])
def test_finding_conclude_observation_detail_and_direct_export(discovery_state, identity_enabled):
    state, conn = discovery_state, discovery_state["conn"]
    conn.execute("UPDATE projects SET vulnerability_fingerprint_version = ? WHERE id = ?",
                 (1 if identity_enabled else None, PROJECT))
    steps = [
        {"action": "以只读角色列出合成对象，取响应中的 object_id=fixture-42 作为后续参数。",
         "observation": "列表返回 fixture-42；仅使用受控对象标识。", "evidence_item_ids": ["parameter-source"]},
        {"action": "保持只读身份，使用第一步对象标识请求基础详情，作为授权基线。",
         "observation": "基线响应为 403 Forbidden。", "evidence_item_ids": ["baseline"]},
        {"action": "沿用前两步身份及 fixture-42，只增加 include=detail，与基线对照。",
         "observation": "响应变为 200，决定结论的受控读取结果位于大响应尾部。", "evidence_item_ids": ["variant"]},
    ]
    request = "GET /objects/fixture-42 HTTP/1.1\r\nHost: fixture.invalid\r\nX-Fixture-Role: viewer\r\n\r\n"
    exchanges = [
        ("parameter-source", {"request": "GET /objects HTTP/1.1\r\nHost: fixture.invalid\r\n\r\n",
                              "response": 'HTTP/1.1 200 OK\r\n\r\n{"object_id":"fixture-42"}'}),
        ("baseline", {"request": request, "response": "HTTP/1.1 403 Forbidden\r\n\r\n受控基线：拒绝访问。"}),
        ("variant", {"request": request.replace("/fixture-42 ", "/fixture-42?include=detail "),
                     "response": "HTTP/1.1 200 OK\r\n\r\n" + NOISE * 5000 + "\n" + TAIL + "\r\n"}),
    ]
    first = _submit_finding(state, "first", exchanges=exchanges, steps=steps, description=DESCRIPTION,
                            remediation=REMEDIATION, identity_enabled=identity_enabled)
    vuln_id = first["vulnerability_id"]
    canonical = conn.execute("SELECT * FROM vulnerabilities WHERE project_id = ? AND id = ?", (PROJECT, vuln_id)).fetchone()
    assert canonical["remediation"] == canonical["repro_steps"] == "" and canonical["evidence_items_json"] == "[]"
    # 保留可独立访问的旧 canonical 来源，但不得把它当作本次 Observation 的过程或建议。
    conn.execute("UPDATE vulnerabilities SET repro_steps = ?, remediation = ? WHERE project_id = ? AND id = ?",
                 (HISTORIC_STEPS, HISTORIC_REMEDIATION, PROJECT, vuln_id))
    alternate_description, alternate_remediation = "另一次发现只记录请求，未取得响应。", "另一次发现的独立建议。"
    alternate_request = "GET /alternate-only HTTP/1.1\r\nHost: fixture.invalid\r\n\r\n"
    second = _submit_finding(state, "second", exchanges=[("alternate-only", {"request": alternate_request})],
        steps=[{"action": "仅记录另一次发现请求，不从前次补响应。", "observation": "该次未保存响应，结果未知。",
                "evidence_item_ids": ["alternate-only"]}], description=alternate_description,
        remediation=alternate_remediation, identity_enabled=identity_enabled)
    assert (second["vulnerability_id"] == vuln_id) is identity_enabled
    canonical = conn.execute("SELECT * FROM vulnerabilities WHERE project_id = ? AND id = ?", (PROJECT, vuln_id)).fetchone()
    assert canonical["repro_steps"] == HISTORIC_STEPS and canonical["remediation"] == HISTORIC_REMEDIATION
    before = {table: conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
              for table in ("task_runs", "assessment_report_finalizations", "vulnerabilities", "vulnerability_observations")}
    # 总报告直接消费新发现，不以单漏洞 accepted 为前提，也不回写 canonical 或 Observation。
    assert not before["assessment_report_finalizations"]
    report_observations = {row["id"]: row for row in assessment_reports._report_discovery_rows(
        conn, PROJECT, observations=True,
    )}
    report_vulnerabilities = {row["id"]: row for row in assessment_reports._report_discovery_rows(conn, PROJECT)}
    for stored in (first, second):
        projected = report_observations[stored["id"]]
        assert projected["reproduction_steps"] == json.loads(stored["reproduction_steps_json"])
        assert projected["remediation"] == stored["remediation"]
        assert projected["source_fact_id"] == stored["source_fact_id"]
    original_records = {row["id"]: row for row in before["vulnerabilities"]}
    for record_id in {first["vulnerability_id"], second["vulnerability_id"]}:
        projected = report_vulnerabilities[record_id]
        selected_input = report_observations[projected["observation_id"]]
        for field in ("reproduction_steps", "remediation", "source_fact_id"):
            assert projected[field] == selected_input[field]
        assert projected["repro_steps"] == ""
        assert projected["record_repro_steps"] == original_records[record_id]["repro_steps"]
        assert projected["record_remediation"] == original_records[record_id]["remediation"]
    assert report_vulnerabilities[vuln_id]["record_repro_steps"] == HISTORIC_STEPS
    assert report_vulnerabilities[vuln_id]["record_remediation"] == HISTORIC_REMEDIATION
    first_detail = vulnerabilities.get_vulnerability_detail(conn, PROJECT, vuln_id, first["id"])
    assert first_detail.readable.observation_id == first["id"]
    assert first_detail.source_fact.id == first["source_fact_id"]
    assert first_detail.source_fact.description == DESCRIPTION
    selected = next(item for item in first_detail.observations if item.id == first["id"])
    assert [step.model_dump() for step in selected.reproduction_steps] == steps
    assert selected.remediation == REMEDIATION
    readable = first_detail.readable
    assert readable.description_markdown == readable_markdown(DESCRIPTION)
    for step in steps:
        assert readable_markdown(step["action"]) in readable.reproduction_markdown and readable_markdown(step["observation"]) in readable.reproduction_markdown
    assert "步骤 1" in readable.key_evidence_markdown and "步骤 2" in readable.key_evidence_markdown and "步骤 3" in readable.key_evidence_markdown
    assert TAIL in readable.key_evidence_markdown and "关键节选" in readable.key_evidence_markdown
    assert "403 Forbidden" in readable.key_evidence_markdown and NOISE not in readable.key_evidence_markdown
    assert alternate_request not in readable.key_evidence_markdown
    rendered = [vulnerability_reports.render_vulnerability_report(
        conn, PROJECT, vuln_id, template_key=DEFAULT_REPORT_TEMPLATE_KEY_V60, observation_id=first["id"],
    )[0] for _ in range(2)]
    assert rendered[0] == rendered[1]
    assert readable_markdown(DESCRIPTION) in rendered[0] and REMEDIATION in rendered[0] and TAIL in rendered[0]
    assert NOISE not in rendered[0] and alternate_description not in rendered[0] and alternate_request not in rendered[0]
    assert all(readable_markdown(step["action"]) in rendered[0] and readable_markdown(step["observation"]) in rendered[0] for step in steps)
    second_detail = vulnerabilities.get_vulnerability_detail(conn, PROJECT, second["vulnerability_id"], second["id"])
    second_markdown = vulnerability_reports.render_vulnerability_report(
        conn, PROJECT, second["vulnerability_id"], template_key=DEFAULT_REPORT_TEMPLATE_KEY_V60, observation_id=second["id"],
    )[0]
    assert second_detail.readable.description_markdown == alternate_description
    assert alternate_description in second_markdown and alternate_remediation in second_markdown and alternate_request in second_markdown
    assert TAIL not in second_markdown and "403 Forbidden" not in second_markdown and DESCRIPTION not in second_markdown
    assert "未记录响应" in second_detail.readable.key_evidence_markdown
    if not identity_enabled:
        with pytest.raises(HTTPException) as wrong_observation:
            vulnerabilities.get_vulnerability_detail(conn, PROJECT, vuln_id, second["id"])
        assert wrong_observation.value.status_code == 404
    for table, rows in before.items():
        assert conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall() == rows
    assert not before["assessment_report_finalizations"]
    assert conn.execute("SELECT status FROM projects WHERE id = ?", (PROJECT,)).fetchone()["status"] == "active"
    for artifact_id, (artifact, raw) in state["originals"].items():
        path = resolve_artifact_path(state["root"], artifact)
        assert path.read_bytes() == raw
        assert sha256(path.read_bytes()).hexdigest() == artifact["sha256"]
        assert conn.execute("SELECT sha256 FROM artifacts WHERE project_id = ? AND id = ?", (PROJECT, artifact_id)).fetchone()["sha256"] == artifact["sha256"]
    historical = vulnerabilities.get_vulnerability_detail(conn, PROJECT, "historic-vulnerability", "historic-observation")
    assert historical.observations[0].reproduction_steps == [] and historical.observations[0].remediation is None
    assert historical.readable.reproduction_markdown.startswith("该次发现未记录独立复现步骤")
    assert historical.readable.remediation_markdown.startswith("该次历史发现未独立记录修复建议")
    assert "不归属于所选发现" in historical.readable.reproduction_markdown
    assert "不归属于所选发现" in historical.readable.remediation_markdown
    assert historical.repro_steps == HISTORIC_STEPS and historical.remediation == HISTORIC_REMEDIATION
    alternative_markdown = vulnerability_reports.render_vulnerability_report(
        conn, PROJECT, vuln_id, template_key="controlled-step-report", observation_id=first["id"],
    )[0]
    assert alternative_markdown != rendered[0]
    assert TAIL in alternative_markdown and all(readable_markdown(step["action"]) in alternative_markdown for step in steps)
    for table, rows in before.items():
        assert conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall() == rows
    # 显式启用时才输出合成页面材料；默认测试不留下额外文件。
    preview_path = os.environ.get("CAIRN_TEST_READABLE_PREVIEW_PATH")
    if identity_enabled and preview_path:
        templates = []
        for key, markdown in ((DEFAULT_REPORT_TEMPLATE_KEY_V60, rendered[0]),
                              ("controlled-step-report", alternative_markdown)):
            template = report_templates.get_report_template(conn, key)
            templates.append({"key": key, "name": template.name, "markdown": markdown,
                              "markdown_content": template.markdown_content})
        Path(preview_path).write_text(json.dumps({
            "detail": first_detail.model_dump(mode="json"), "templates": templates,
        }, ensure_ascii=False, indent=2), encoding="utf-8")


def test_excerpt_checks_only_selected_utf8_bytes():
    assert vulnerability_reports._excerpt_text(b"\xffOK", "utf8-bytes=1:3") == "OK"
    for pointer, expected in (("utf8-bytes=0:99", "evidence_excerpt_range_out_of_bounds"),
                              ("utf8-bytes=1:3", "evidence_excerpt_utf8_boundary"),
                              ("utf8-bytes=0:2", "evidence_excerpt_utf8_boundary")):
        with pytest.raises(HTTPException) as rejected:
            vulnerability_reports._excerpt_text("中OK".encode("utf-8"), pointer)
        assert rejected.value.status_code == 422 and rejected.value.detail == expected
