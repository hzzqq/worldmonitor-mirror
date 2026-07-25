"""log_utils + 抓取失败日志测试（R1 新能力：worldmonitor-mirror 可观测性）。"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from log_utils import setup_logging, capture_logs
import data_feed


def test_setup_logging_writes_to_file(tmp_path):
    log_file = tmp_path / "wm.log"
    setup_logging("INFO", log_file=str(log_file))
    logging.getLogger("worldmonitor").info("行情抓取启动")
    assert log_file.exists()
    assert "行情抓取启动" in log_file.read_text(encoding="utf-8")


def test_capture_logs_captures_records():
    with capture_logs() as buf:
        logging.getLogger("worldmonitor").error("fetch failed")
    assert "fetch failed" in buf.getvalue()


def test_get_news_logs_fetch_failure(monkeypatch):
    # R2 验收：抓取失败时除 UI 提示外，必须落日志便于后台排障。
    def boom():
        raise RuntimeError("network down")

    monkeypatch.setattr(data_feed, "fetch_hacker_news", boom)
    # RSS 也强制失败
    def boom_rss(name, url):
        raise RuntimeError("rss down")

    monkeypatch.setattr(data_feed, "fetch_rss", boom_rss)
    with capture_logs("WARNING") as buf:
        items, note = data_feed.get_news(force_refresh=True)
    # 离线兜底仍应返回数据，且失败被记入日志
    assert items
    assert "network down" in buf.getvalue()
    assert "rss down" in buf.getvalue()
