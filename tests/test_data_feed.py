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


def test_export_news_csv_structure_and_escaping():
    """R1 新需求：导出 CSV；含逗号/换行的标题不应破坏行结构（CSV 引用）。"""
    news = [
        {"title": "A, B 公司", "link": "http://x", "source": "S",
         "published": "2024-01-01T12:00:00", "summary": "摘要一行"},
        {"title": "C", "link": "http://y", "source": "S2",
         "published": "2024-01-02", "summary": "多\n行摘要"},
    ]
    out = data_feed.export_news_csv(news)
    assert out.startswith("title,link")
    # 用 csv 读回，验证字段未被逗号/换行拆散
    import csv as _csv, io
    parsed = list(_csv.reader(io.StringIO(out)))
    assert len(parsed) == 3  # 表头 + 2 条
    assert parsed[1][0] == "A, B 公司"
    assert parsed[2][4] == "多\n行摘要"


def test_get_news_source_filter(tmp_path, monkeypatch):
    """R1 新需求：get_news(source=...) 按来源子串过滤；available_sources 列出来源。"""
    # 强制走 mock，避免网络；用临时缓存隔离
    monkeypatch.setattr(data_feed, "fetch_hacker_news", lambda: [])
    monkeypatch.setattr(data_feed, "fetch_rss", lambda name, url: data_feed._mock_news())
    data_feed.clear_news_cache()

    all_news, _ = data_feed.get_news()
    srcs = data_feed.available_sources(all_news)
    assert srcs  # 至少含 mock 来源
    # 选一个真实存在的来源做子串过滤
    target = srcs[0]
    filtered, _ = data_feed.get_news(source=target)
    assert filtered
    assert all(target.lower() in (n.get("source") or "").lower() for n in filtered)
    # 不存在的来源应返回空
    none_res, _ = data_feed.get_news(source="__no_such_source__")
    assert none_res == []


def test_available_sources_dedup_and_sorted():
    news = [
        {"source": "B 源"}, {"source": "A 源"}, {"source": "B 源"}, {"source": ""},
    ]
    assert data_feed.available_sources(news) == ["A 源", "B 源"]


def test_summarize_news_counts_by_source_and_sentiment():
    """R1 新需求：summarize_news 聚合总量/来源/情绪分布，供看板概览指标。"""
    news = [
        {"title": "开源突破增长", "summary": "", "source": "科技前线"},
        {"title": "亏损下跌利空", "summary": "", "source": "财经速递"},
        {"title": "例行会议", "summary": "", "source": "科技前线"},
    ]
    s = data_feed.summarize_news(news)
    assert s["total"] == 3
    assert s["by_source"]["科技前线"] == 2
    assert s["by_source"]["财经速递"] == 1
    assert s["by_sentiment"]["正面"] == 1
    assert s["by_sentiment"]["负面"] == 1
    assert s["by_sentiment"]["中性"] == 1
