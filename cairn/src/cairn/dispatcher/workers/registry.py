"""仅允许生产 pi 执行；测试通过依赖注入提供 fake。"""

from __future__ import annotations

from cairn.dispatcher.workers.adapters.pi import PiDriver


DRIVERS: dict[str, PiDriver] = {"pi": PiDriver()}


def get_driver(name: str) -> PiDriver:
    return DRIVERS[name]
