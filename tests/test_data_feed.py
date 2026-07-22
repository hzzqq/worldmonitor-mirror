"""worldmonitor data_feed 单元测试（纯 Python，无网络依赖）。

运行：pytest worldmonitor-mirror/tests
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import data_feed  # noqa: E402


def test_sentiment_positive():
    assert data_feed.classify_sentiment("国产大模型开源突破") == "正面"


def test_sentiment_negative():
    assert data_feed.classify_sentiment("新能源车企亏损下跌") == "负面"


def test_sentiment_neutral():
    assert data_feed.classify_sentiment("今日例行会议") == "中性"


def test_sentiment_empty():
    assert data_feed.classify_sentiment("") == "中性"


def test_sentiment_tie_favors_neutral():
    # 正负面词各命中一个，应判中性
    assert data_feed.classify_sentiment("突破 风险") == "中性"


def test_get_news_returns_list():
    # get_news 离线应回退 mock，返回 (list, str)
    news, note = data_feed.get_news()
    assert isinstance(news, list)
    assert len(news) >= 1
    assert all("title" in n and "link" in n for n in news)


def test_dedup_removes_duplicate_links():
    items = [
        {"title": "A", "link": "https://x.com/1"},
        {"title": "A copy", "link": "https://x.com/1"},  # 同链接
        {"title": "B", "link": "https://x.com/2"},
    ]
    out = data_feed._dedup_news(items)
    assert len(out) == 2
    links = {i["link"] for i in out}
    assert "https://x.com/1" in links and "https://x.com/2" in links


def test_get_news_has_no_duplicate_links():
    news, _ = data_feed.get_news()
    links = [n.get("link") for n in news if n.get("link")]
    assert len(links) == len(set(links))  # 链接级唯一


def test_group_by_sentiment_sums_to_total():
    news, _ = data_feed.get_news()
    groups = data_feed.group_by_sentiment(news)
    total = sum(len(v) for v in groups.values())
    assert total == len(news)
    # 每个分组键都应有定义
    assert set(groups.keys()) == {"正面", "负面", "中性"}


def _force_mock(monkeypatch):
    """让所有网络抓取抛错，从而强制走内置 mock 分支（无网络依赖）。"""
    def _boom(*a, **k):
        raise RuntimeError("offline")
    monkeypatch.setattr(data_feed, "fetch_hacker_news", _boom)
    monkeypatch.setattr(data_feed, "fetch_rss", _boom)


def test_get_news_limit_truncates(monkeypatch):
    """R1 新需求：limit 应按时序截取前 N 条。"""
    _force_mock(monkeypatch)
    data_feed.clear_news_cache()
    items, _ = data_feed.get_news(limit=3)
    assert len(items) == 3
    # 默认（不限）应多于 limit
    all_items, _ = data_feed.get_news(limit=None)
    assert len(all_items) >= 3


def test_get_news_caches_and_clear(monkeypatch):
    """R2 隐性性能/一致性：模块级 TTL 缓存应复用结果；clear 后重新拉取。"""
    _force_mock(monkeypatch)
    calls = {"n": 0}
    orig = data_feed._mock_news

    def spy():
        calls["n"] += 1
        return orig()

    monkeypatch.setattr(data_feed, "_mock_news", spy)
    data_feed.clear_news_cache()
    data_feed.get_news()
    data_feed.get_news()  # 命中缓存，不应再次抓取
    assert calls["n"] == 1
    data_feed.clear_news_cache()
    data_feed.get_news()
    assert calls["n"] == 2
