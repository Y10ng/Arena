"""FastAPI 应用装配入口，连接数据库、业务路由和前端静态资源。"""

from contextlib import asynccontextmanager
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from cairn import __version__
from cairn.server import db
from cairn.server.routers import (
    admin,
    audit,
    auth,
    candidates,
    coverage_slots,
    execution_events,
    export,
    health,
    hints,
    infra,
    injection_payloads,
    intents,
    knowledge,
    libraries,
    monitoring,
    models,
    project_resources,
    project_schedules,
    project_runtime,
    projects,
    prompt_groups,
    report_templates,
    reports,
    scenarios,
    settings,
    tenant_workers,
    vulnerabilities,
    verification_requests,
    worker_templates,
)

STATIC_DIR = Path(os.environ.get("CAIRN_STATIC_DIR", Path(__file__).parent / "static"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 被 CLI serve 预先配置过时保持幂等；直接导入 app 时读取环境变量或使用默认 SQLite 路径。
    db.configure()
    from cairn.server.services.project_resources import cleanup_expired_project_resource_batches

    cleanup_expired_project_resource_batches()
    yield


app = FastAPI(
    title="Arena",
    description="Fact-graph based collaborative exploration protocol",
    version=__version__,
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

app.include_router(health.router)
app.include_router(auth.router)
app.include_router(candidates.router)
app.include_router(execution_events.router)
app.include_router(admin.router)
app.include_router(settings.router)
app.include_router(scenarios.router)
app.include_router(infra.catalog_router)
app.include_router(infra.router)
app.include_router(libraries.router)
app.include_router(report_templates.catalog_router)
app.include_router(report_templates.router)
app.include_router(reports.router)
app.include_router(knowledge.router)
app.include_router(prompt_groups.router)
app.include_router(project_runtime.router)
app.include_router(projects.router)
app.include_router(project_schedules.router)
app.include_router(verification_requests.router)
app.include_router(project_resources.router)
app.include_router(worker_templates.router)
app.include_router(models.router)
app.include_router(tenant_workers.router)
app.include_router(tenant_workers.dispatcher_router)
app.include_router(hints.router)
app.include_router(injection_payloads.router)
app.include_router(intents.router)
app.include_router(coverage_slots.router)
app.include_router(vulnerabilities.router)
app.include_router(audit.router)
app.include_router(monitoring.router)
app.include_router(export.router)

# SPA 的 hash 路由由静态前端接管，后端只负责根页面和 /static 资源。

@app.get("/", include_in_schema=False)
def index():
    # HTML 入口不能长期缓存，否则升级后会继续引用已替换的懒加载 chunk。
    return FileResponse(
        STATIC_DIR / "index.html",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
