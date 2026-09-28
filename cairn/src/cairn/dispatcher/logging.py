"""Dispatcher 日志配置，提供适合长驻进程和健康检查的两种输出形态。"""

from __future__ import annotations

import logging


class DispatcherLogFormatter(logging.Formatter):
    _PREFIX = "cairn.dispatcher."

    def format(self, record: logging.LogRecord) -> str:
        name = record.name
        if name.startswith(self._PREFIX):
            # 日志中显示短模块名，减少长包名前缀噪声。
            shortname = name[len(self._PREFIX):]
        else:
            shortname = name
        setattr(record, "shortname", shortname)
        return super().format(record)


def configure_logging(level: str = "INFO", *, bare: bool = False, verbose: bool = False) -> None:
    # verbose 覆盖 level 为 DEBUG，便于调试；同时放开 requests/urllib3 日志，看到 HTTP 连接与重试细节。
    effective_level = "DEBUG" if verbose else level
    handler = logging.StreamHandler()
    if bare:
        # startup-healthcheck-only 面向脚本输出，裸格式更便于 CI/人工复制。
        handler.setFormatter(logging.Formatter(fmt="%(message)s"))
    else:
        handler.setFormatter(
            DispatcherLogFormatter(
                fmt="[%(asctime)s] %(levelname)s %(shortname)s %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
    logging.basicConfig(level=getattr(logging, effective_level.upper(), logging.INFO), handlers=[handler], force=True)
    # verbose 时放到底层库自身级别，否则维持 WARNING 避免连接噪声刷屏。
    lib_level = logging.INFO if verbose else logging.WARNING
    logging.getLogger("requests").setLevel(lib_level)
    logging.getLogger("urllib3").setLevel(lib_level)
