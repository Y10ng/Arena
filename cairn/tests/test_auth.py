from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

from click.testing import CliRunner
from fastapi import HTTPException, Request, Response
import pytest

from cairn.cli import main
from cairn.server import db
from cairn.server.app import app
from route_helpers import iter_app_routes
from cairn.server.auth import (
    DEFAULT_PROVISIONED_PASSWORD,
    RequestPrincipal,
    claim_legacy_tenant,
    create_machine_principal,
    create_user_with_personal_tenant,
    hash_secret,
    protect_auth_cookie_request,
    protect_cookie_request,
    require_dispatcher,
    require_tenant_project,
    require_user,
    require_user_tenant_project,
    require_super_admin,
    resolve_session,
    rotate_machine_principal,
    write_security_audit,
)
from cairn.server.routers.auth import (
    LoginRequest,
    PasswordChangeRequest,
    csrf_route,
    login_route,
    logout_route,
    password_change_route,
    session_route,
)


@pytest.fixture(autouse=True)
def configured_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db._reset_for_tests()
    database_path = tmp_path / "auth.db"
    monkeypatch.setenv("CAIRN_DATABASE_URL", f"sqlite:///{database_path}")
    monkeypatch.setenv("CAIRN_TRUSTED_ORIGINS", "http://testserver")
    db.configure(database_path)
    yield database_path
    db._reset_for_tests()


def _bootstrap(database_path: Path, password: str = "correct horse battery staple"):
    result = CliRunner().invoke(
        main,
        [
            "auth", "bootstrap-admin", "--login", "Admin",
            "--password-env", "TEST_ADMIN_PASSWORD", "--db-path", str(database_path),
        ],
        env={"TEST_ADMIN_PASSWORD": password},
    )
    assert result.exit_code == 0, result.output


def test_runtime_bootstrap_is_idempotent_and_keeps_credentials(
    configured_database: Path,
) -> None:
    password_file = configured_database.parent / "initial-admin-password"
    password_file.write_text("first generated password\n", encoding="utf-8")
    password_file.chmod(0o600)
    args = [
        "auth", "bootstrap-runtime",
        "--admin-login", "Admin",
        "--admin-password-file", str(password_file),
        "--dispatcher-token-env", "TEST_DISPATCHER_TOKEN",
        "--db-path", str(configured_database),
    ]
    first = CliRunner().invoke(
        main,
        args,
        env={"TEST_DISPATCHER_TOKEN": "stable-dispatcher-token"},
    )
    assert first.exit_code == 0, first.output
    assert json.loads(first.output)["admin_created"] is True
    assert json.loads(first.output)["dispatcher_created"] is True
    assert "first generated password" not in first.output
    assert "stable-dispatcher-token" not in first.output

    password_file.write_text("unused replacement password\n", encoding="utf-8")
    second = CliRunner().invoke(
        main,
        args,
        env={"TEST_DISPATCHER_TOKEN": "stable-dispatcher-token"},
    )
    assert second.exit_code == 0, second.output
    assert json.loads(second.output)["admin_created"] is False
    assert json.loads(second.output)["dispatcher_created"] is False

    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) AS count FROM users").fetchone()["count"] == 1
        assert conn.execute("SELECT COUNT(*) AS count FROM machine_principals").fetchone()["count"] == 1
        password_hash = conn.execute("SELECT password_hash FROM users").fetchone()["password_hash"]
        assert conn.execute("SELECT token_hash FROM machine_principals").fetchone()["token_hash"] == hash_secret(
            "stable-dispatcher-token"
        )
    from cairn.server.auth import verify_password
    assert verify_password(password_hash, "first generated password")
    assert not verify_password(password_hash, "unused replacement password")


def test_runtime_bootstrap_rejects_insecure_admin_password_file(configured_database: Path) -> None:
    password_file = configured_database.parent / "initial-admin-password"
    password_file.write_text("secret-password\n", encoding="utf-8")
    password_file.chmod(0o644)

    result = CliRunner().invoke(
        main,
        [
            "auth", "bootstrap-runtime",
            "--admin-password-file", str(password_file),
            "--dispatcher-token-env", "TEST_DISPATCHER_TOKEN",
            "--db-path", str(configured_database),
        ],
        env={"TEST_DISPATCHER_TOKEN": "stable-dispatcher-token"},
    )

    assert result.exit_code != 0
    assert "权限不安全" in result.output
    assert "secret-password" not in result.output

    password_file.chmod(0o700)
    result = CliRunner().invoke(
        main,
        [
            "auth", "bootstrap-runtime",
            "--admin-password-file", str(password_file),
            "--dispatcher-token-env", "TEST_DISPATCHER_TOKEN",
            "--db-path", str(configured_database),
        ],
        env={"TEST_DISPATCHER_TOKEN": "stable-dispatcher-token"},
    )
    assert result.exit_code != 0
    assert "权限不安全" in result.output

    password_file.write_text("x" * 4097, encoding="utf-8")
    password_file.chmod(0o600)
    result = CliRunner().invoke(
        main,
        [
            "auth", "bootstrap-runtime",
            "--admin-password-file", str(password_file),
            "--dispatcher-token-env", "TEST_DISPATCHER_TOKEN",
            "--db-path", str(configured_database),
        ],
        env={"TEST_DISPATCHER_TOKEN": "stable-dispatcher-token"},
    )
    assert result.exit_code != 0
    assert "内容过长" in result.output
    assert "x" * 128 not in result.output


def test_runtime_bootstrap_rejects_mismatched_dispatcher_token(
    configured_database: Path,
) -> None:
    _bootstrap(configured_database)
    create_machine_principal("dispatcher-main", "registered-token")

    password_file = configured_database.parent / "initial-admin-password"
    password_file.write_text("unused-password\n", encoding="utf-8")
    password_file.chmod(0o600)
    result = CliRunner().invoke(
        main,
        [
            "auth", "bootstrap-runtime",
            "--admin-password-file", str(password_file),
            "--dispatcher-token-env", "TEST_DISPATCHER_TOKEN",
            "--db-path", str(configured_database),
        ],
        env={"TEST_DISPATCHER_TOKEN": "different-token"},
    )

    assert result.exit_code != 0
    assert "does not match" in result.output


def test_auth_cli_explicit_db_path_overrides_database_url_environment(
    configured_database: Path,
) -> None:
    result = CliRunner().invoke(
        main,
        [
            "auth", "bootstrap-admin", "--login", "Admin", "--password-env", "TEST_ADMIN_PASSWORD",
            "--db-path", str(configured_database),
        ],
        env={
            "TEST_ADMIN_PASSWORD": "environment override test",
            db.DEFAULT_DATABASE_URL_ENV: "postgresql://ignored.invalid/cairn",
        },
    )

    assert result.exit_code == 0, result.output
    with db.get_conn() as conn:
        assert conn.execute("SELECT login FROM users").fetchone()["login"] == "admin"


def _seed_legacy_resources() -> None:
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO project_groups (id, name, owner, created_at, tenant_id) "
            "VALUES ('grp_legacy', 'Legacy group', 'legacy', '2026-08-13T00:00:00Z', 'tenant_legacy')"
        )
        conn.execute(
            "INSERT INTO projects (id, title, status, created_at, group_id, tenant_id) "
            "VALUES ('proj_legacy', 'Legacy project', 'active', '2026-08-13T00:00:00Z', "
            "'grp_legacy', 'tenant_legacy')"
        )
        conn.execute(
            "INSERT INTO worker_templates (id, name, owner, worker_names_json, created_at, tenant_id) "
            "VALUES ('wt_legacy', 'Legacy template', 'legacy', '[]', "
            "'2026-08-13T00:00:00Z', 'tenant_legacy')"
        )


def test_claim_legacy_tenant_cli_transfers_only_resources_and_audits_counts(
    configured_database: Path,
) -> None:
    _bootstrap(configured_database)
    _seed_legacy_resources()
    with db.get_conn() as conn:
        target = conn.execute(
            "SELECT u.id AS user_id, tm.tenant_id FROM users u "
            "JOIN tenant_memberships tm ON tm.user_id = u.id WHERE u.login = 'admin'"
        ).fetchone()
        conn.execute(
            "INSERT INTO projects (id, title, status, created_at, tenant_id) "
            "VALUES ('proj_personal', 'Personal project', 'active', '2026-08-13T00:00:00Z', ?)",
            (target["tenant_id"],),
        )
        conn.execute(
            "INSERT INTO auth_sessions "
            "(id, token_hash, csrf_token_hash, user_id, created_at, expires_at) "
            "VALUES ('ses_keep', 'session-hash', 'csrf-hash', ?, "
            "'2026-08-13T00:00:00Z', '2099-01-01T00:00:00Z')",
            (target["user_id"],),
        )
        conn.execute(
            "INSERT INTO machine_principals (id, name, token_hash, created_at) "
            "VALUES ('mch_keep', 'dispatcher', 'machine-hash', '2026-08-13T00:00:00Z')"
        )

    result = CliRunner().invoke(
        main,
        ["auth", "claim-legacy-tenant", "--login", " ADMIN ", "--db-path", str(configured_database)],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload == {
        "counts": {"project_groups": 1, "projects": 1, "worker_templates": 1},
        "login": "admin",
        "tenant_id": target["tenant_id"],
        "user_id": target["user_id"],
    }
    with db.get_conn() as conn:
        for table in ("projects", "project_groups", "worker_templates"):
            assert conn.execute(
                f"SELECT COUNT(*) AS count FROM {table} WHERE tenant_id = 'tenant_legacy'"
            ).fetchone()["count"] == 0
        assert conn.execute(
            "SELECT tenant_id FROM projects WHERE id = 'proj_personal'"
        ).fetchone()["tenant_id"] == target["tenant_id"]
        assert conn.execute("SELECT COUNT(*) AS count FROM auth_sessions").fetchone()["count"] == 1
        assert conn.execute("SELECT COUNT(*) AS count FROM machine_principals").fetchone()["count"] == 1
        audit = conn.execute(
            "SELECT actor_type, actor_id, tenant_id, action, resource_type, resource_id, "
            "outcome, metadata_json FROM security_audit_events "
            "WHERE action = 'tenant.legacy.claim'"
        ).fetchone()
    assert dict(audit) == {
        "actor_type": "system",
        "actor_id": None,
        "tenant_id": target["tenant_id"],
        "action": "tenant.legacy.claim",
        "resource_type": "tenant",
        "resource_id": "tenant_legacy",
        "outcome": "success",
        "metadata_json": json.dumps(
            {"counts": {"project_groups": 1, "projects": 1, "worker_templates": 1}},
            ensure_ascii=False,
            sort_keys=True,
        ),
    }


def test_claim_legacy_tenant_is_idempotent_when_no_resources_remain(
    configured_database: Path,
) -> None:
    _bootstrap(configured_database)
    _seed_legacy_resources()

    first = claim_legacy_tenant("admin")
    second = claim_legacy_tenant("ADMIN")

    assert first["counts"] == {"projects": 1, "project_groups": 1, "worker_templates": 1}
    assert second["counts"] == {"projects": 0, "project_groups": 0, "worker_templates": 0}


def test_claim_legacy_tenant_fails_closed_for_invalid_target_or_legacy_kind(
    configured_database: Path,
) -> None:
    _bootstrap(configured_database)
    _seed_legacy_resources()

    with pytest.raises(ValueError, match="User or personal tenant not found"):
        claim_legacy_tenant("missing")
    with db.get_conn() as conn:
        assert conn.execute(
            "SELECT COUNT(*) AS count FROM projects WHERE tenant_id = 'tenant_legacy'"
        ).fetchone()["count"] == 1
        conn.execute("UPDATE tenants SET kind = 'personal' WHERE id = 'tenant_legacy'")

    with pytest.raises(ValueError, match="missing or is not marked legacy"):
        claim_legacy_tenant("admin")
    with db.get_conn() as conn:
        assert conn.execute(
            "SELECT COUNT(*) AS count FROM projects WHERE tenant_id = 'tenant_legacy'"
        ).fetchone()["count"] == 1
        assert conn.execute(
            "SELECT COUNT(*) AS count FROM security_audit_events "
            "WHERE action = 'tenant.legacy.claim'"
        ).fetchone()["count"] == 0


def test_claim_legacy_tenant_rolls_back_if_any_resource_update_fails(
    configured_database: Path,
) -> None:
    _bootstrap(configured_database)
    _seed_legacy_resources()
    with db.get_conn() as conn:
        conn.execute(
            "CREATE TRIGGER reject_worker_template_claim BEFORE UPDATE OF tenant_id ON worker_templates "
            "BEGIN SELECT RAISE(ABORT, 'simulated claim failure'); END"
        )

    with pytest.raises(Exception, match="simulated claim failure"):
        claim_legacy_tenant("admin")
    with db.get_conn() as conn:
        for table in ("projects", "project_groups", "worker_templates"):
            assert conn.execute(
                f"SELECT COUNT(*) AS count FROM {table} WHERE tenant_id = 'tenant_legacy'"
            ).fetchone()["count"] == 1
        assert conn.execute(
            "SELECT COUNT(*) AS count FROM security_audit_events "
            "WHERE action = 'tenant.legacy.claim'"
        ).fetchone()["count"] == 0


def test_claim_legacy_tenant_rejects_unassigned_resource_partial_state(
    configured_database: Path,
) -> None:
    _bootstrap(configured_database)
    _seed_legacy_resources()
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO projects (id, title, status, created_at, tenant_id) "
            "VALUES ('proj_unassigned', 'Unassigned', 'active', '2026-08-13T00:00:00Z', NULL)"
        )

    with pytest.raises(ValueError, match="ownership is incomplete in projects"):
        claim_legacy_tenant("admin")
    with db.get_conn() as conn:
        assert conn.execute(
            "SELECT tenant_id FROM projects WHERE id = 'proj_legacy'"
        ).fetchone()["tenant_id"] == "tenant_legacy"
        assert conn.execute(
            "SELECT COUNT(*) AS count FROM security_audit_events "
            "WHERE action = 'tenant.legacy.claim'"
        ).fetchone()["count"] == 0


def test_claim_legacy_tenant_rejects_ambiguous_membership_without_changes(
    configured_database: Path,
) -> None:
    _bootstrap(configured_database)
    _seed_legacy_resources()
    with db.get_conn() as conn:
        user_id = conn.execute("SELECT id FROM users WHERE login = 'admin'").fetchone()["id"]
        conn.execute(
            "INSERT INTO tenants (id, kind, name, created_at) "
            "VALUES ('tenant_conflict', 'personal', 'conflict', '2026-08-13T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO tenant_memberships (tenant_id, user_id, role, created_at) "
            "VALUES ('tenant_conflict', ?, 'viewer', '2026-08-13T00:00:00Z')",
            (user_id,),
        )

    with pytest.raises(ValueError, match="ambiguous tenant memberships"):
        claim_legacy_tenant("admin")
    with db.get_conn() as conn:
        assert conn.execute(
            "SELECT COUNT(*) AS count FROM projects WHERE tenant_id = 'tenant_legacy'"
        ).fetchone()["count"] == 1


def test_claim_legacy_tenant_cli_explicit_db_path_overrides_environment(
    configured_database: Path,
) -> None:
    _bootstrap(configured_database)
    result = CliRunner().invoke(
        main,
        ["auth", "claim-legacy-tenant", "--login", "admin", "--db-path", str(configured_database)],
        env={db.DEFAULT_DATABASE_URL_ENV: "postgresql://ignored.invalid/cairn"},
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["counts"] == {
        "project_groups": 0,
        "projects": 0,
        "worker_templates": 0,
    }


def test_bootstrap_login_session_hash_and_logout(configured_database: Path) -> None:
    _bootstrap(configured_database)

    with db.get_conn() as conn:
        user = conn.execute("SELECT login, password_hash FROM users").fetchone()
        assert user["login"] == "admin"
        assert "correct horse battery staple" not in user["password_hash"]
        assert user["password_hash"].startswith("$argon2")

    response = Response()
    view = login_route(
        LoginRequest(login="admin", password="correct horse battery staple"),
        response,
    )
    cookie_header = response.headers["set-cookie"]
    session_cookie = cookie_header.split("cairn_session=", 1)[1].split(";", 1)[0]
    assert view.csrf_token
    assert "HttpOnly" in cookie_header
    assert "SameSite=lax" in cookie_header

    with db.get_conn() as conn:
        session = conn.execute("SELECT token_hash, csrf_token_hash FROM auth_sessions").fetchone()
        assert session["token_hash"] == hash_secret(session_cookie)
        assert session["csrf_token_hash"] == hash_secret(view.csrf_token)
        assert session_cookie not in str(dict(session))

    resolved = resolve_session(session_cookie)
    assert resolved is not None
    assert session_route(resolved[0]).tenant_id == view.tenant_id

    logout_response = Response()
    logout_route(logout_response, resolved[0], session_cookie)
    assert resolve_session(session_cookie) is None


def test_forced_password_session_is_limited_then_change_preserves_only_current_session() -> None:
    user_id, _ = create_user_with_personal_tenant(
        "provisioned",
        DEFAULT_PROVISIONED_PASSWORD,
        must_change_password=True,
    )
    first_response = Response()
    first_view = login_route(
        LoginRequest(login="provisioned", password=DEFAULT_PROVISIONED_PASSWORD),
        first_response,
    )
    first_cookie = first_response.headers["set-cookie"].split("cairn_session=", 1)[1].split(";", 1)[0]
    second_response = Response()
    login_route(
        LoginRequest(login="provisioned", password=DEFAULT_PROVISIONED_PASSWORD),
        second_response,
    )
    second_cookie = second_response.headers["set-cookie"].split("cairn_session=", 1)[1].split(";", 1)[0]
    logout_response = Response()
    logout_view = login_route(
        LoginRequest(login="provisioned", password=DEFAULT_PROVISIONED_PASSWORD),
        logout_response,
    )
    logout_cookie = logout_response.headers["set-cookie"].split("cairn_session=", 1)[1].split(";", 1)[0]

    assert first_view.must_change_password is True
    resolved = resolve_session(first_cookie)
    assert resolved is not None and resolved[0].must_change_password is True
    assert session_route(resolved[0]).must_change_password is True
    get_request = Request({"type": "http", "method": "GET", "headers": []})
    assert protect_auth_cookie_request(get_request, first_cookie).subject_id == user_id
    with pytest.raises(HTTPException, match="Password change required") as blocked_get:
        protect_cookie_request(get_request, first_cookie)
    assert blocked_get.value.status_code == 403
    with pytest.raises(HTTPException, match="Password change required") as blocked_dependency:
        require_user(first_cookie)
    assert blocked_dependency.value.status_code == 403

    csrf_request = Request(
        {
            "type": "http",
            "method": "POST",
            "headers": [(b"origin", b"http://testserver")],
        }
    )
    rotated = csrf_route(csrf_request, Response(), first_cookie)
    assert rotated.must_change_password is True
    assert protect_auth_cookie_request(csrf_request, first_cookie, rotated.csrf_token).subject_id == user_id
    with pytest.raises(HTTPException, match="Password change required") as blocked_post:
        protect_cookie_request(csrf_request, first_cookie, rotated.csrf_token)
    assert blocked_post.value.status_code == 403

    logout_route(Response(), logout_view, logout_cookie)
    assert resolve_session(logout_cookie) is None

    changed = password_change_route(
        PasswordChangeRequest(
            current_password=DEFAULT_PROVISIONED_PASSWORD,
            new_password="replacement-user-secret",
        ),
        resolved[0],
        first_cookie,
    )

    assert changed.must_change_password is False
    current = resolve_session(first_cookie)
    assert current is not None and current[0].must_change_password is False
    assert resolve_session(second_cookie) is None
    with db.get_conn() as conn:
        user = conn.execute(
            "SELECT password_hash, must_change_password FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        audit = conn.execute(
            "SELECT metadata_json FROM security_audit_events WHERE action = 'auth.password.change'"
        ).fetchone()["metadata_json"]
    from cairn.server.auth import verify_password
    assert verify_password(user["password_hash"], "replacement-user-secret")
    assert user["must_change_password"] == 0
    assert DEFAULT_PROVISIONED_PASSWORD not in audit
    assert "replacement-user-secret" not in audit
    with pytest.raises(HTTPException):
        login_route(
            LoginRequest(login="provisioned", password=DEFAULT_PROVISIONED_PASSWORD),
            Response(),
        )
    assert login_route(
        LoginRequest(login="provisioned", password="replacement-user-secret"),
        Response(),
    ).must_change_password is False


def test_password_change_rejects_wrong_same_and_provisioned_passwords_without_revocation() -> None:
    create_user_with_personal_tenant(
        "forced-custom",
        "custom-current-secret",
        must_change_password=True,
    )
    response = Response()
    view = login_route(
        LoginRequest(login="forced-custom", password="custom-current-secret"),
        response,
    )
    cookie = response.headers["set-cookie"].split("cairn_session=", 1)[1].split(";", 1)[0]

    cases = [
        ("wrong-current", "new-secret", 401),
        ("custom-current-secret", "custom-current-secret", 422),
        ("custom-current-secret", DEFAULT_PROVISIONED_PASSWORD, 422),
    ]
    for current_password, new_password, status_code in cases:
        with pytest.raises(HTTPException) as exc:
            password_change_route(
                PasswordChangeRequest(
                    current_password=current_password,
                    new_password=new_password,
                ),
                view,
                cookie,
            )
        assert exc.value.status_code == status_code
        assert resolve_session(cookie) is not None

    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT outcome, metadata_json FROM security_audit_events "
            "WHERE action = 'auth.password.change' ORDER BY created_at, id"
        ).fetchall()
    assert len(rows) == 3
    assert all(row["outcome"] == "failure" for row in rows)
    persisted = "\n".join(row["metadata_json"] for row in rows)
    assert "wrong-current" not in persisted
    assert "custom-current-secret" not in persisted
    assert DEFAULT_PROVISIONED_PASSWORD not in persisted


def test_login_cookie_secure_setting(configured_database: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _bootstrap(configured_database)
    monkeypatch.setenv("CAIRN_SESSION_COOKIE_SECURE", "1")
    secure_response = Response()
    login_route(
        LoginRequest(login="admin", password="correct horse battery staple"),
        secure_response,
    )
    assert "Secure" in secure_response.headers["set-cookie"]

    monkeypatch.setenv("CAIRN_SESSION_COOKIE_SECURE", "0")
    insecure_response = Response()
    login_route(
        LoginRequest(login="admin", password="correct horse battery staple"),
        insecure_response,
    )
    assert "Secure" not in insecure_response.headers["set-cookie"]


def test_login_rejects_wrong_password(configured_database: Path) -> None:
    _bootstrap(configured_database, "valid password")
    with pytest.raises(HTTPException) as exc:
        login_route(LoginRequest(login="admin", password="wrong"), Response())
    assert exc.value.status_code == 401


def test_csrf_route_rotates_session_token_with_no_store_and_audit(
    configured_database: Path,
) -> None:
    _bootstrap(configured_database)
    login_response = Response()
    login_view = login_route(
        LoginRequest(login="admin", password="correct horse battery staple"),
        login_response,
    )
    session_cookie = login_response.headers["set-cookie"].split("cairn_session=", 1)[1].split(";", 1)[0]
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "headers": [(b"origin", b"http://testserver")],
        }
    )
    response = Response()

    rotated = csrf_route(request, response, session_cookie)

    assert rotated.csrf_token
    assert rotated.csrf_token != login_view.csrf_token
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    protected_request = Request(
        {
            "type": "http",
            "method": "POST",
            "headers": [(b"origin", b"http://testserver")],
        }
    )
    with pytest.raises(HTTPException) as stale:
        protect_cookie_request(protected_request, session_cookie, login_view.csrf_token)
    assert stale.value.status_code == 403
    assert protect_cookie_request(protected_request, session_cookie, rotated.csrf_token).subject_id == rotated.user_id

    with db.get_conn() as conn:
        session = conn.execute("SELECT csrf_token_hash FROM auth_sessions").fetchone()
        audit = conn.execute(
            "SELECT action, actor_id, resource_type, outcome FROM security_audit_events "
            "WHERE action = 'auth.csrf.rotate'"
        ).fetchone()
    assert session["csrf_token_hash"] == hash_secret(rotated.csrf_token)
    assert audit is not None
    assert dict(audit) == {
        "action": "auth.csrf.rotate",
        "actor_id": rotated.user_id,
        "resource_type": "session",
        "outcome": "success",
    }


@pytest.mark.parametrize("origin", [None, "http://untrusted.test"])
def test_csrf_route_rejects_missing_or_untrusted_origin(
    configured_database: Path,
    origin: str | None,
) -> None:
    _bootstrap(configured_database)
    login_response = Response()
    login_route(
        LoginRequest(login="admin", password="correct horse battery staple"),
        login_response,
    )
    session_cookie = login_response.headers["set-cookie"].split("cairn_session=", 1)[1].split(";", 1)[0]
    headers = [] if origin is None else [(b"origin", origin.encode())]
    request = Request({"type": "http", "method": "POST", "headers": headers})

    with pytest.raises(HTTPException) as exc:
        csrf_route(request, Response(), session_cookie)
    assert exc.value.status_code == 403


def test_csrf_and_protected_request_accept_same_host_origin_on_non_default_port(
    configured_database: Path,
) -> None:
    _bootstrap(configured_database)
    login_response = Response()
    login_view = login_route(
        LoginRequest(login="admin", password="correct horse battery staple"),
        login_response,
    )
    session_cookie = login_response.headers["set-cookie"].split("cairn_session=", 1)[1].split(";", 1)[0]
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "headers": [
                (b"origin", b"http://127.0.0.1:18743"),
                (b"host", b"127.0.0.1:18743"),
            ],
        }
    )

    rotated = csrf_route(request, Response(), session_cookie)
    assert rotated.csrf_token != login_view.csrf_token
    assert protect_cookie_request(request, session_cookie, rotated.csrf_token).subject_id == login_view.user_id


def test_same_origin_fallback_rejects_host_mismatch_and_forwarded_host_spoof(
    configured_database: Path,
) -> None:
    _bootstrap(configured_database)
    login_response = Response()
    login_view = login_route(
        LoginRequest(login="admin", password="correct horse battery staple"),
        login_response,
    )
    session_cookie = login_response.headers["set-cookie"].split("cairn_session=", 1)[1].split(";", 1)[0]
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "headers": [
                (b"origin", b"https://public.example.test"),
                (b"host", b"127.0.0.1:8000"),
                (b"x-forwarded-host", b"public.example.test"),
            ],
        }
    )

    with pytest.raises(HTTPException) as exc:
        protect_cookie_request(request, session_cookie, login_view.csrf_token)
    assert exc.value.status_code == 403


def test_csrf_route_accepts_configured_origin_without_trailing_slash(
    configured_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CAIRN_TRUSTED_ORIGINS", "https://one.test/, https://two.test")
    _bootstrap(configured_database)
    login_response = Response()
    login_route(
        LoginRequest(login="admin", password="correct horse battery staple"),
        login_response,
    )
    session_cookie = login_response.headers["set-cookie"].split("cairn_session=", 1)[1].split(";", 1)[0]
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "headers": [(b"origin", b"https://one.test")],
        }
    )

    assert csrf_route(request, Response(), session_cookie).csrf_token


def test_csrf_route_rejects_missing_session() -> None:
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "headers": [(b"origin", b"http://testserver")],
        }
    )

    with pytest.raises(HTTPException) as exc:
        csrf_route(request, Response(), None)
    assert exc.value.status_code == 401


def test_anonymous_http_routes_are_explicitly_limited() -> None:
    """新增 API 必须显式声明认证依赖，避免匿名面静默扩大。"""
    anonymous_routes: set[tuple[str, str]] = set()
    manual_session_routes = {("/auth/csrf", "POST")}
    for route in iter_app_routes(app):
        if not hasattr(route, "dependant") or not getattr(route, "methods", None):
            continue
        if not route.dependant.dependencies:
            anonymous_routes.update((route.path, method) for method in route.methods)

    assert anonymous_routes == {
        ("/", "GET"),
        ("/auth/csrf", "POST"),
        ("/auth/login", "POST"),
        ("/health", "GET"),
    }
    assert manual_session_routes <= anonymous_routes


def test_http_schema_endpoints_are_disabled_but_cli_export_remains_available() -> None:
    assert app.docs_url is None
    assert app.redoc_url is None
    assert app.openapi_url is None
    assert not any(route.path in {"/docs", "/redoc", "/openapi.json"} for route in iter_app_routes(app))

    result = CliRunner().invoke(main, ["schema", "export", "--format", "openapi"])
    assert result.exit_code == 0, result.output
    schema = json.loads(result.output)
    assert schema["info"]["title"] == "Cairn"
    assert "/auth/csrf" in schema["paths"]


def test_security_audit_metadata_redacts_known_secrets_without_mutating_input() -> None:
    metadata: dict[str, object] = {
        "apiKey": "known-api-key",
        "nested": {
            "Authorization": "Bearer known-bearer",
            "note": "header=Basic known-basic",
            "endpoint": (
                "https://known-user:known-password@example.test/path"
                "?token=known-query&mode=safe"
            ),
        },
        "items": [{"session_token": "known-session"}, "safe"],
    }
    original = deepcopy(metadata)

    with db.get_conn() as conn:
        write_security_audit(
            conn,
            actor_type="system",
            actor_id=None,
            tenant_id=None,
            action="test.metadata",
            resource_type=None,
            resource_id=None,
            outcome="success",
            metadata=metadata,
        )
        stored = conn.execute(
            "SELECT metadata_json FROM security_audit_events WHERE action = ?",
            ("test.metadata",),
        ).fetchone()

    assert metadata == original
    assert stored is not None
    raw = stored["metadata_json"]
    assert all(
        secret not in raw
        for secret in (
            "known-api-key",
            "known-bearer",
            "known-basic",
            "known-user",
            "known-password",
            "known-query",
            "known-session",
        )
    )
    safe = json.loads(raw)
    assert safe["apiKey"] == "[REDACTED]"
    assert safe["nested"]["Authorization"] == "[REDACTED]"
    assert safe["nested"]["note"] == "header=Basic [REDACTED]"
    assert safe["nested"]["endpoint"] == (
        "https://[REDACTED]@example.test/path?token=[REDACTED]&mode=safe"
    )
    assert safe["items"] == [{"session_token": "[REDACTED]"}, "safe"]


def test_security_audit_metadata_redacts_password_containing_at_without_suffix_leak() -> None:
    known_secret = "known-password@internal-suffix"
    endpoint = f"https://known-user:{known_secret}@example.test/path"

    with db.get_conn() as conn:
        write_security_audit(
            conn,
            actor_type="system",
            actor_id=None,
            tenant_id=None,
            action="test.credentialed-url",
            resource_type=None,
            resource_id=None,
            outcome="failure",
            metadata={"endpoint": endpoint},
        )
        stored = conn.execute(
            "SELECT metadata_json FROM security_audit_events WHERE action = ?",
            ("test.credentialed-url",),
        ).fetchone()

    assert stored is not None
    raw = stored["metadata_json"]
    assert known_secret not in raw
    assert "internal-suffix" not in raw
    assert json.loads(raw)["endpoint"] == "https://[REDACTED]@example.test/path"


def test_security_audit_metadata_bounds_deep_large_and_unsupported_values() -> None:
    metadata: dict[str, object] = {
        "deep": {"a": {"b": {"c": {"d": {"e": {"secret": "too-deep"}}}}}},
        "large": list(range(55)),
        "unsupported": object(),
        "non_finite": float("inf"),
    }
    metadata["cycle"] = metadata

    with db.get_conn() as conn:
        write_security_audit(
            conn,
            actor_type="system",
            actor_id=None,
            tenant_id=None,
            action="test.bounded-metadata",
            resource_type=None,
            resource_id=None,
            outcome="success",
            metadata=metadata,
        )
        stored = conn.execute(
            "SELECT metadata_json FROM security_audit_events WHERE action = ?",
            ("test.bounded-metadata",),
        ).fetchone()

    assert stored is not None
    raw = stored["metadata_json"]
    assert "too-deep" not in raw
    safe = json.loads(raw)
    assert safe["cycle"] == "[UNSUPPORTED]"
    assert safe["deep"]["a"]["b"]["c"]["d"]["e"] == "[TRUNCATED]"
    assert safe["large"][-1] == "[TRUNCATED]"
    assert len(safe["large"]) == 51
    assert safe["unsupported"] == "[UNSUPPORTED]"
    assert safe["non_finite"] == "[UNSUPPORTED]"


def test_bootstrap_dispatcher_stores_only_hash_and_authenticates_machine_routes(
    configured_database: Path,
) -> None:
    result = CliRunner().invoke(
        main,
        [
            "auth", "bootstrap-dispatcher", "--name", "dispatcher-east",
            "--token-env", "TEST_DISPATCHER_TOKEN", "--db-path", str(configured_database),
        ],
        env={"TEST_DISPATCHER_TOKEN": "dispatcher-secret-token"},
    )
    assert result.exit_code == 0, result.output
    principal_id = result.output.strip().split('"principal_id": "', 1)[1].split('"', 1)[0]

    with db.get_conn() as conn:
        machine = conn.execute(
            "SELECT id, name, token_hash FROM machine_principals"
        ).fetchone()
        assert machine["id"] == principal_id
        assert machine["name"] == "dispatcher-east"
        assert machine["token_hash"] == hash_secret("dispatcher-secret-token")
        assert "dispatcher-secret-token" not in str(dict(machine))

    from cairn.server.auth import require_dispatcher
    from cairn.server.models import DispatcherHeartbeatRequest
    from cairn.server.routers.monitoring import update_dispatcher_heartbeat

    principal = require_dispatcher("Bearer dispatcher-secret-token")
    assert principal.kind == "machine"
    assert principal.subject_id == principal_id
    with db.get_conn() as conn:
        audit_count_before = conn.execute(
            "SELECT COUNT(*) AS count FROM security_audit_events"
        ).fetchone()["count"]
    heartbeat = update_dispatcher_heartbeat(
        DispatcherHeartbeatRequest(dispatcher_id="spoofed", status="idle"),
        principal,
    )
    assert heartbeat.dispatcher_id == principal_id
    with db.get_conn() as conn:
        audit_count_after = conn.execute(
            "SELECT COUNT(*) AS count FROM security_audit_events"
        ).fetchone()["count"]
    assert audit_count_after == audit_count_before


def test_machine_principal_can_use_dispatcher_protocol_but_not_admin_management(
    configured_database: Path,
) -> None:
    from cairn.server.auth import require_dispatcher_protocol, require_super_admin

    principal_id, token = create_machine_principal("dispatcher-protocol", "protocol-secret")
    request = Request({"type": "http", "method": "GET", "headers": []})
    principal = require_dispatcher_protocol(request, f"Bearer {token}")
    assert principal.subject_id == principal_id
    assert principal.tenant_id is None
    with pytest.raises(HTTPException) as denied:
        require_super_admin(principal)
    assert denied.value.status_code == 403


def test_dispatcher_credential_rotation_invalidates_old_token(configured_database: Path) -> None:
    create = CliRunner().invoke(
        main,
        ["auth", "bootstrap-dispatcher", "--name", "rotate-me", "--db-path", str(configured_database)],
    )
    assert create.exit_code == 0, create.output
    payload = __import__("json").loads(create.output)

    rotate = CliRunner().invoke(
        main,
        [
            "auth", "rotate-dispatcher", "--principal-id", payload["principal_id"],
            "--token-env", "NEXT_DISPATCHER_TOKEN", "--db-path", str(configured_database),
        ],
        env={"NEXT_DISPATCHER_TOKEN": "rotated-secret"},
    )
    assert rotate.exit_code == 0, rotate.output
    assert __import__("json").loads(rotate.output)["token"] == "rotated-secret"
    with pytest.raises(HTTPException):
        require_dispatcher(f"Bearer {payload['token']}")
    assert require_dispatcher("Bearer rotated-secret").subject_id == payload["principal_id"]


@pytest.mark.parametrize("token", ["", "   ", " token", "token ", "x" * 1025])
def test_machine_credential_creation_rejects_unusable_explicit_tokens(token: str) -> None:
    with pytest.raises(ValueError, match="token must"):
        create_machine_principal("invalid-token", token)


@pytest.mark.parametrize("token", ["", "   ", " token", "token ", "x" * 1025])
def test_machine_credential_rotation_rejects_unusable_explicit_tokens(token: str) -> None:
    principal_id, original = create_machine_principal("rotation-validation", "original-token")

    with pytest.raises(ValueError, match="token must"):
        rotate_machine_principal(principal_id, token)

    assert require_dispatcher(f"Bearer {original}").subject_id == principal_id


def test_machine_credential_accepts_authentication_length_boundary() -> None:
    token = "x" * 1024
    principal_id, credential = create_machine_principal("boundary-token", token)

    assert credential == token
    assert require_dispatcher(f"Bearer {token}").subject_id == principal_id


def test_machine_route_rejects_missing_or_user_credentials(configured_database: Path) -> None:
    with pytest.raises(HTTPException) as missing:
        require_dispatcher(None)
    assert missing.value.status_code == 401

    with pytest.raises(HTTPException) as malformed:
        require_dispatcher("Basic dispatcher-secret-token")
    assert malformed.value.status_code == 401


def test_super_admin_dependency_rejects_regular_user() -> None:
    principal = RequestPrincipal("user", "usr_1", "tenant_1", "user")
    with pytest.raises(HTTPException) as exc:
        require_super_admin(principal)
    assert exc.value.status_code == 403


def test_super_admin_dependency_accepts_super_admin() -> None:
    principal = RequestPrincipal("user", "usr_admin", "tenant_admin", "super_admin")
    assert require_super_admin(principal) is principal


def test_project_dependencies_allow_admin_global_but_keep_user_and_machine_boundaries() -> None:
    admin_id, admin_tenant = create_user_with_personal_tenant(
        "project-admin",
        "admin password",
        role="super_admin",
    )
    user_id, user_tenant = create_user_with_personal_tenant(
        "project-user",
        "user password",
    )
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO projects (id, title, status, created_at, tenant_id) "
            "VALUES ('project-foreign', 'Foreign', 'active', '2026-08-19T00:00:00Z', ?)",
            (user_tenant,),
        )

    admin = RequestPrincipal("user", admin_id, admin_tenant, "super_admin")
    user = RequestPrincipal("user", user_id, user_tenant, "user")
    other_user = RequestPrincipal("user", "usr_other", "tenant_other", "user")
    machine = RequestPrincipal("machine", "mch_dispatcher")

    assert require_tenant_project("project-foreign", admin) == admin
    assert require_user_tenant_project("project-foreign", admin) == admin
    assert require_tenant_project("project-foreign", user) == user
    assert require_tenant_project("project-foreign", machine) == machine

    with pytest.raises(HTTPException) as hidden:
        require_tenant_project("project-foreign", other_user)
    assert hidden.value.status_code == 404
    with pytest.raises(HTTPException) as hidden_mutation:
        require_user_tenant_project("project-foreign", other_user)
    assert hidden_mutation.value.status_code == 404
    with pytest.raises(HTTPException) as machine_mutation:
        require_user_tenant_project("project-foreign", machine)
    assert machine_mutation.value.status_code == 403


def test_route_policy_marks_project_and_admin_surfaces() -> None:
    route_dependencies = {
        (route.path, next(iter(route.methods))): {
            dependency.call for dependency in route.dependant.dependencies
        }
        for route in iter_app_routes(app)
        if hasattr(route, "dependant") and getattr(route, "methods", None)
    }
    from cairn.server.auth import (
        protect_cookie_request,
        require_dispatcher_or_super_admin,
        require_dispatcher_protocol,
        require_tenant_project,
        require_user_tenant_project,
    )

    assert require_tenant_project in route_dependencies[("/projects/{project_id}", "GET")]
    assert require_user_tenant_project in route_dependencies[("/projects/{project_id}", "DELETE")]
    assert require_tenant_project in route_dependencies[("/projects/{project_id}/artifacts", "POST")]
    assert require_tenant_project in route_dependencies[
        ("/projects/{project_id}/injection-payloads", "GET")
    ]
    payload_write_dependencies = route_dependencies[
        ("/projects/{project_id}/injection-payloads/operations", "POST")
    ]
    assert require_tenant_project in payload_write_dependencies
    assert require_dispatcher in payload_write_dependencies
    assert require_tenant_project in route_dependencies[
        ("/projects/{project_id}/artifacts/archive", "GET")
    ]
    assert require_tenant_project in route_dependencies[
        ("/projects/{project_id}/assessment-report-packet", "GET")
    ]
    assert require_tenant_project in route_dependencies[
        ("/projects/{project_id}/assessment-report-finalization", "GET")
    ]
    assert require_tenant_project in route_dependencies[
        ("/projects/{project_id}/assessment-report-finalization/claim", "POST")
    ]
    assert require_tenant_project in route_dependencies[
        ("/projects/{project_id}/assessment-report-finalization/finish", "POST")
    ]
    assert require_tenant_project in route_dependencies[
        ("/projects/{project_id}/assessment-report-finalization/finish-replay", "POST")
    ]
    assert require_user_tenant_project in route_dependencies[
        ("/projects/{project_id}/assessment-report-finalization/request", "POST")
    ]
    assert require_tenant_project in route_dependencies[
        ("/projects/{project_id}/assessment-report-finalization/snapshot", "POST")
    ]
    assert protect_cookie_request in route_dependencies[("/report-template-catalog", "GET")]
    assert require_super_admin in route_dependencies[("/report-templates", "GET")]
    assert require_super_admin in route_dependencies[("/report-templates", "POST")]
    assert require_dispatcher_or_super_admin in route_dependencies[("/settings", "GET")]
    assert require_super_admin in route_dependencies[("/settings", "PUT")]
    assert protect_cookie_request in route_dependencies[("/scenario-catalog", "GET")]
    assert require_dispatcher_protocol in route_dependencies[("/scenarios", "GET")]
    assert require_super_admin in route_dependencies[("/scenarios", "PUT")]
    assert require_super_admin in route_dependencies[("/scenarios/validate", "POST")]
    assert require_super_admin in route_dependencies[("/scenarios/options", "GET")]

    assert protect_cookie_request in route_dependencies[("/monitoring/summary", "GET")]
    assert require_user_tenant_project in route_dependencies[
        ("/projects/{project_id}/monitoring/summary", "GET")
    ]
    assert require_user_tenant_project in route_dependencies[
        ("/projects/{project_id}/management-options", "GET")
    ]
    assert require_super_admin in route_dependencies[("/admin/monitoring", "GET")]
    assert require_dispatcher in route_dependencies[("/dispatcher/monitoring/summary", "GET")]
    assert require_user_tenant_project in route_dependencies[("/projects/{project_id}/vulnerabilities", "POST")]
    assert require_tenant_project in route_dependencies[("/projects/{project_id}/vulnerabilities/{vuln_id}", "PATCH")]
    assert require_tenant_project in route_dependencies[
        (
            "/projects/{project_id}/vulnerabilities/{vuln_id}/provenance",
            "GET",
        )
    ]
    assert require_tenant_project in route_dependencies[
        (
            "/projects/{project_id}/vulnerability-observations/{observation_id}/verifier-target",
            "GET",
        )
    ]
    assert require_tenant_project in route_dependencies[
        ("/projects/{project_id}/vulnerability-chain-candidates", "GET")
    ]
    assert require_tenant_project in route_dependencies[
        (
            "/projects/{project_id}/vulnerabilities/{vuln_id}/duplicate-candidates",
            "GET",
        )
    ]
