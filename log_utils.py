"""worldmonitor-mirror 结构化日志工具（R1 新能力：抓取/行情可观测性）。

诊断日志（抓取失败/汇总）统一走 logging，默认写 stderr，不污染 stdout。
级别由 WORLDMONITOR_LOG_LEVEL 控制，落盘路径由 WORLDMONITOR_LOG_FILE 指定。
"""
from __future__ import annotations

import io
import logging
import sys
from contextlib import contextmanager

DEFAULT_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

_CONFIGURED = False


def setup_logging(
    level: str = "INFO",
    log_file: str | None = None,
    fmt: str = DEFAULT_FORMAT,
    force: bool = True,
) -> logging.Logger:
    """配置根日志（只配置一次，除非 force=True）。"""
    global _CONFIGURED
    level = (level or "INFO").upper()
    numeric = getattr(logging, level, logging.INFO)
    handler: logging.Handler
    if log_file:
        handler = logging.FileHandler(log_file, encoding="utf-8")
    else:
        handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(fmt))
    root = logging.getLogger()
    if force or not _CONFIGURED:
        root.handlers = [handler]
        root.setLevel(numeric)
        _CONFIGURED = True
    else:
        root.addHandler(handler)
        root.setLevel(min(root.level, numeric))
    return root


@contextmanager
def capture_logs(level: str = "DEBUG"):
    """测试辅助：捕获块内日志，yield 可读 StringIO。"""
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    root = logging.getLogger()
    old_level = root.level
    old_handlers = root.handlers[:]
    root.handlers = [handler]
    root.setLevel(getattr(logging, level, logging.DEBUG))
    try:
        yield buf
    finally:
        root.handlers = old_handlers
        root.setLevel(old_level)
