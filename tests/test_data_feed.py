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
