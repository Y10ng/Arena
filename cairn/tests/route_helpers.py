"""测试按有效路径和继承依赖检查路由，不依赖 FastAPI 内部挂载表示。"""

from fastapi import routing


def iter_app_routes(app):
    route_contexts = getattr(routing, "iter_route_contexts", None)
    if route_contexts is None:
        # 旧版 FastAPI 在 include_router 时已将路径和依赖平铺到 app.routes。
        yield from app.routes
    else:
        yield from route_contexts(app.routes)
