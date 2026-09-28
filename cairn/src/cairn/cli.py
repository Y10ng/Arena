"""命令行入口，负责把本地运维命令转接到 Server 和 Dispatcher。"""

import json
import os
from pathlib import Path
import stat

import click
from fastapi import HTTPException
from pydantic import BaseModel
import uvicorn

from cairn import __version__
from cairn.dispatcher.logging import configure_logging
from cairn.dispatcher.scheduler.loop import DispatcherBootstrap, DispatcherLoop
from cairn.server import database_admin, db
from cairn.server.auth import (
    claim_legacy_tenant,
    create_machine_principal,
    create_user_with_personal_tenant,
    ensure_local_runtime_principals,
    rotate_machine_principal,
)
from cairn.server.services import cleanup_legacy_presets


@click.group()
def main():
    """Cairn - Fact-graph based collaborative exploration protocol."""


def _server_openapi_schema() -> dict:
    # 延迟导入 FastAPI 应用，避免仅使用 dispatcher 命令时也初始化 Server 路由。
    from cairn.server.app import app

    return app.openapi()


def _server_json_schemas() -> dict:
    # 直接遍历 server.models 中的 Pydantic 模型，导出给前端或外部工具消费的协议快照。
    from cairn.server import models as server_models

    schemas = {}
    for name in sorted(dir(server_models)):
        value = getattr(server_models, name)
        if (
            isinstance(value, type)
            and issubclass(value, BaseModel)
            and value.__module__ == server_models.__name__
        ):
            schemas[name] = value.model_json_schema(ref_template="#/$defs/{model}")
    return {
        "title": "Cairn Server JSON Schemas",
        "version": __version__,
        "schemas": schemas,
    }


@main.group()
def schema():
    """Export server protocol schemas."""


@schema.command("export")
@click.option(
    "--format",
    "schema_format",
    type=click.Choice(["openapi", "json-schema"]),
    default="openapi",
    show_default=True,
    help="Schema format to export",
)
@click.option(
    "--output",
    "output_path",
    type=click.Path(dir_okay=False, path_type=Path),
    help="Write schema JSON to this file instead of stdout",
)
def export_schema(schema_format: str, output_path: Path | None):
    """Export the current Cairn server protocol contract."""
    payload = _server_openapi_schema() if schema_format == "openapi" else _server_json_schemas()
    content = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if output_path:
        output_path.write_text(content, encoding="utf-8")
        return
    click.echo(content, nl=False)


@main.group("auth")
def authentication_commands():
    """Bootstrap and maintain authenticated users."""


@authentication_commands.command("bootstrap-admin")
@click.option("--login", prompt=True, help="Super administrator login")
@click.option("--password-env", default=None, help="Read password from this environment variable")
@click.option("--db-path", type=click.Path(path_type=Path), default=None)
@click.option("--database-url", envvar=db.DEFAULT_DATABASE_URL_ENV, default=None)
@click.pass_context
def bootstrap_admin(
    ctx: click.Context,
    login: str,
    password_env: str | None,
    db_path: Path | None,
    database_url: str | None,
):
    """Create the first super administrator and personal Tenant."""
    import os

    password = os.environ.get(password_env, "") if password_env else click.prompt(
        "Password", hide_input=True, confirmation_prompt=True
    )
    if db_path is not None and ctx.get_parameter_source("database_url") == click.core.ParameterSource.ENVIRONMENT:
        database_url = None
    try:
        db.configure(db_path, database_url=database_url)
        user_id, tenant_id = create_user_with_personal_tenant(login, password, role="super_admin")
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps({"user_id": user_id, "tenant_id": tenant_id}, ensure_ascii=False))


def _read_private_secret_file(path: Path) -> str:
    """读取 owner-only secret 文件，拒绝 symlink 与宽松权限。"""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise click.ClickException(f"无法读取 secret 文件: {path}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            raise click.ClickException(f"secret 文件权限不安全（要求 0600）: {path}")
        if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
            raise click.ClickException(f"secret 文件 owner 与当前用户不一致: {path}")
        if info.st_size > 4096:
            raise click.ClickException(f"secret 文件内容过长: {path}")
        raw = os.read(fd, 4097)
        if len(raw) > 4096:
            raise click.ClickException(f"secret 文件内容过长: {path}")
    finally:
        os.close(fd)
    try:
        value = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise click.ClickException(f"secret 文件必须使用 UTF-8: {path}") from exc
    if value.endswith("\n"):
        value = value[:-1]
    if not value or "\n" in value or "\r" in value:
        raise click.ClickException(f"secret 文件内容无效: {path}")
    return value


@authentication_commands.command("bootstrap-runtime")
@click.option("--admin-login", default="admin", show_default=True)
@click.option(
    "--admin-password-file",
    required=True,
    type=click.Path(dir_okay=False, path_type=Path),
    help="Read the initial administrator password from an owner-only file",
)
@click.option("--dispatcher-name", default="dispatcher-main", show_default=True)
@click.option("--dispatcher-token-env", required=True, help="Read the Dispatcher token from this environment variable")
@click.option("--db-path", type=click.Path(path_type=Path), default=None)
@click.option("--database-url", envvar=db.DEFAULT_DATABASE_URL_ENV, default=None)
@click.pass_context
def bootstrap_runtime(
    ctx: click.Context,
    admin_login: str,
    admin_password_file: Path,
    dispatcher_name: str,
    dispatcher_token_env: str,
    db_path: Path | None,
    database_url: str | None,
):
    """幂等初始化脚本部署所需的管理员与 Dispatcher machine principal。"""
    admin_password = _read_private_secret_file(admin_password_file)
    dispatcher_token = os.environ.get(dispatcher_token_env)
    if dispatcher_token is None:
        raise click.ClickException(f"Environment variable is not set: {dispatcher_token_env}")
    if db_path is not None and ctx.get_parameter_source("database_url") == click.core.ParameterSource.ENVIRONMENT:
        database_url = None
    try:
        db.configure(db_path, database_url=database_url)
        result = ensure_local_runtime_principals(
            admin_login=admin_login,
            admin_password=admin_password,
            dispatcher_name=dispatcher_name,
            dispatcher_token=dispatcher_token,
        )
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(result, ensure_ascii=False, sort_keys=True))


@authentication_commands.command("claim-legacy-tenant")
@click.option("--login", required=True, help="Existing user's login whose personal Tenant receives legacy resources")
@click.option("--db-path", type=click.Path(path_type=Path), default=None)
@click.option("--database-url", envvar=db.DEFAULT_DATABASE_URL_ENV, default=None)
@click.pass_context
def claim_legacy_tenant_command(
    ctx: click.Context,
    login: str,
    db_path: Path | None,
    database_url: str | None,
):
    """将 tenant_legacy 中的升级遗留资源显式归入现有个人 Tenant。"""
    if db_path is not None and ctx.get_parameter_source("database_url") == click.core.ParameterSource.ENVIRONMENT:
        database_url = None
    try:
        db.configure(db_path, database_url=database_url)
        result = claim_legacy_tenant(login)
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(result, ensure_ascii=False, sort_keys=True))


@authentication_commands.command("bootstrap-dispatcher")
@click.option("--name", required=True, help="Dispatcher machine principal name")
@click.option("--token-env", default=None, help="Read an existing token from this environment variable")
@click.option("--db-path", type=click.Path(path_type=Path), default=None)
@click.option("--database-url", envvar=db.DEFAULT_DATABASE_URL_ENV, default=None)
@click.pass_context
def bootstrap_dispatcher(
    ctx: click.Context,
    name: str,
    token_env: str | None,
    db_path: Path | None,
    database_url: str | None,
):
    """创建 Dispatcher machine principal，并且仅在本次调用中输出 token。"""
    import os

    token = os.environ.get(token_env) if token_env else None
    if token_env and token is None:
        raise click.ClickException(f"Environment variable is not set: {token_env}")
    if db_path is not None and ctx.get_parameter_source("database_url") == click.core.ParameterSource.ENVIRONMENT:
        database_url = None
    try:
        db.configure(db_path, database_url=database_url)
        principal_id, credential = create_machine_principal(name, token)
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps({"principal_id": principal_id, "token": credential}, ensure_ascii=False))


@authentication_commands.command("rotate-dispatcher")
@click.option("--principal-id", required=True, help="Dispatcher machine principal ID")
@click.option("--token-env", default=None, help="Read replacement token from this environment variable")
@click.option("--db-path", type=click.Path(path_type=Path), default=None)
@click.option("--database-url", envvar=db.DEFAULT_DATABASE_URL_ENV, default=None)
@click.pass_context
def rotate_dispatcher(
    ctx: click.Context,
    principal_id: str,
    token_env: str | None,
    db_path: Path | None,
    database_url: str | None,
):
    """轮换 Dispatcher machine credential，并仅输出新的 token。"""
    import os

    token = os.environ.get(token_env) if token_env else None
    if token_env and token is None:
        raise click.ClickException(f"Environment variable is not set: {token_env}")
    if db_path is not None and ctx.get_parameter_source("database_url") == click.core.ParameterSource.ENVIRONMENT:
        database_url = None
    try:
        db.configure(db_path, database_url=database_url)
        credential = rotate_machine_principal(principal_id, token)
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps({"principal_id": principal_id, "token": credential}, ensure_ascii=False))


@main.group("db")
def database_commands():
    """Inspect and migrate Cairn databases."""


def _echo_admin_result(result: database_admin.AdminResult) -> None:
    click.echo(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))


@database_commands.command("status")
@click.option("--db-path", type=click.Path(path_type=Path), default=None)
@click.option("--database-url", envvar=db.DEFAULT_DATABASE_URL_ENV, default=None)
@click.option("--artifact-root", envvar=db.DEFAULT_ARTIFACT_ROOT_ENV, type=click.Path(path_type=Path), default=None)
@click.pass_context
def database_status(
    ctx: click.Context,
    db_path: Path | None,
    database_url: str | None,
    artifact_root: Path | None,
):
    """Read database status without creating or migrating it."""
    # 显式兼容路径应覆盖环境 URL；Click 会把 envvar 也填入参数，需要按来源消歧。
    if db_path is not None and ctx.get_parameter_source("database_url") == click.core.ParameterSource.ENVIRONMENT:
        database_url = None
    try:
        _echo_admin_result(
            database_admin.status(
                database_url=database_url,
                db_path=db_path,
                artifact_root=artifact_root,
            )
        )
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc


@database_commands.command("migrate")
@click.option("--database-url", envvar=db.DEFAULT_DATABASE_URL_ENV, required=True)
@click.option("--dry-run", is_flag=True, help="Run migrations and roll back the transaction")
def database_migrate(database_url: str, dry_run: bool):
    """Apply PostgreSQL migrations under the Cairn advisory lock."""
    try:
        _echo_admin_result(database_admin.migrate(database_url=database_url, dry_run=dry_run))
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc


@main.command()
@click.option("--host", default="0.0.0.0", show_default=True, help="Bind host")
@click.option("--port", default=8000, show_default=True, help="Bind port")
@click.option(
    "--db-path",
    type=click.Path(path_type=Path),
    default=None,
    help="SQLite database path (compatibility option)",
)
@click.option(
    "--database-url",
    envvar=db.DEFAULT_DATABASE_URL_ENV,
    default=None,
    help="Database URL; supports sqlite:///path and postgresql://...",
)
@click.option(
    "--artifact-root",
    envvar=db.DEFAULT_ARTIFACT_ROOT_ENV,
    type=click.Path(path_type=Path),
    default=None,
    help="Artifact storage root",
)
@click.option("--log-level", default="info", show_default=True, help="Uvicorn log level")
@click.option("--access-log/--no-access-log", default=True, show_default=True, help="Enable Uvicorn access log")
def serve(
    host: str,
    port: int,
    db_path: Path | None,
    database_url: str | None,
    artifact_root: Path | None,
    log_level: str,
    access_log: bool,
):
    """Start the Cairn API server."""
    # CLI 配置优先生效；未指定时 FastAPI lifespan 使用环境变量或默认 SQLite 路径兜底。
    db.configure(db_path, database_url=database_url, artifact_path=artifact_root)
    from cairn.server.app import app

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level=log_level.lower(),
        access_log=access_log,
    )


@main.command()
@click.option(
    "--config",
    "config_path",
    envvar="CAIRN_DISPATCH_CONFIG",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="dispatch.yaml path",
)
@click.option(
    "--hard-cap",
    envvar="CAIRN_DISPATCHER_HARD_CAP",
    default=64,
    show_default=True,
    type=click.IntRange(min=1),
    help="Physical thread-pool capacity; runtime.max_workers must not exceed it",
)
@click.option("--once", is_flag=True, help="Run one scheduling iteration and exit")
@click.option(
    "--startup-healthcheck-only",
    is_flag=True,
    help="Run startup worker healthchecks and exit",
)
@click.option("--log-level", default="INFO", show_default=True, help="Log level")
@click.option("-v", "--verbose", is_flag=True, help="完整调试日志：DEBUG 级别 + 放开 requests/urllib3 + 不截断 worker 输出")
def dispatch(config_path: Path, hard_cap: int, once: bool, startup_healthcheck_only: bool, log_level: str, verbose: bool):
    """Run the Cairn dispatcher."""
    # Dispatcher 是长生命周期进程，启动前先完成日志形态；运行配置从本地 YAML 加载。
    configure_logging(log_level, bare=startup_healthcheck_only, verbose=verbose)
    if verbose:
        # 放开 worker stdout/stderr 在日志里的截断，便于看到完整错误（如 pi 的 404 JSONL）。
        from cairn.dispatcher.tasks.common_base import set_preview_verbose
        set_preview_verbose(True)
    try:
        loop = DispatcherLoop(
            DispatcherBootstrap(
                config_path=config_path,
                hard_cap=hard_cap,
            )
        )
        if startup_healthcheck_only:
            loop.run_startup_healthchecks_only()
            return
        loop.run(once=once)
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc


@main.group()
def infra():
    """Run legacy maintenance commands."""


@infra.command("cleanup-presets")
@click.option(
    "--db-path",
    type=click.Path(),
    default=str(db.DEFAULT_DB),
    show_default=True,
    help="SQLite database path",
)
def cleanup_presets(db_path: str):
    """Remove legacy non-default scenarios and orphan preset resources."""
    db.configure(Path(db_path))
    try:
        with db.get_conn() as conn:
            result = cleanup_legacy_presets(conn)
    except HTTPException as exc:
        raise click.ClickException(str(exc.detail)) from exc
    click.echo(
        "Removed legacy presets: "
        + ", ".join(f"{key}={len(value)}" for key, value in result.items())
    )
