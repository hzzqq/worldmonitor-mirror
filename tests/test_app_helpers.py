"""app_helpers 纯函数单测：抽出自 app.py 的看板辅助逻辑，独立验证。

覆盖：
  - _to_dt：datetime 透传 / 字符串解析 / 非法值归 None（R2 隐性 bug 修复点）
  - highlight_keyword：大小写不敏感高亮 / 空关键词原样返回
  - build_dataframe：列齐全 / 情绪计算 / 字符串时间被正确解析（不再静默丢条目）
  - trending_chart_data：把后端 trending_keywords 转成图表数据（R1 落地点）
"""
from __future__ import annotations

import datetime as _dt

import app_helpers as ah


def _sample(news=None):
    return news or [
        {"title": "AI 大模型发布", "summary": "人工智能 大模型 开源", "source": "S1",
         "link": "http://x/1", "published": "2024-01-01T10:00:00"},
        {"title": "OpenLLM 评测", "summary": "大模型 评测", "source": "S2",
         "link": "http://x/2", "published": "2024-01-02T12:30:00"},
    ]


def test_to_dt_passthrough():
    now = _dt.datetime(2024, 5, 1, 9, 0, 0)
    assert ah._to_dt(now) is now


def test_to_dt_string_iso():
    out = ah._to_dt("2024-01-01T10:00:00")
    assert isinstance(out, _dt.datetime)
    assert out.year == 2024 and out.month == 1 and out.day == 1


def test_to_dt_invalid_returns_none():
    assert ah._to_dt("不是时间") is None
    assert ah._to_dt(None) is None
    assert ah._to_dt("") is None


def test_highlight_keyword_case_insensitive():
    out = ah.highlight_keyword("Hello World", "world")
    # 大小写不敏感命中，但保留原文大小写
    assert "**World**" in out


def test_highlight_keyword_empty_returns_title():
    assert ah.highlight_keyword("标题", "") == "标题"


def test_build_dataframe_columns_and_sentiment():
    df = ah.build_dataframe(_sample())
    for col in ("标题", "来源", "链接", "发布时间", "日期", "小时", "情绪"):
        assert col in df.columns
    # 情绪列由 classify_sentiment 计算，且非空
    assert df["情绪"].notna().all()


def test_build_dataframe_string_published_coerced():
    # R2 修复点：字符串时间必须被解析成 datetime，时间图表不再丢条目
    items = [{"title": "t", "summary": "s", "source": "S", "link": "l",
              "published": "2024-03-04T08:15:00"}]
    df = ah.build_dataframe(items)
    pub = df.iloc[0]["发布时间"]
    assert isinstance(pub, _dt.datetime)
    assert df.iloc[0]["日期"] == _dt.date(2024, 3, 4)


def test_build_dataframe_invalid_published_is_none_not_crash():
    items = [{"title": "t", "summary": "s", "source": "S", "link": "l",
              "published": "乱码时间"}]
    df = ah.build_dataframe(items)
    assert df.iloc[0]["日期"] is None  # 解析失败归 None，而非抛错


def test_trending_chart_data_shape():
    # R1 落地点：后端 trending_keywords 经此转图表数据
    news = [
        {"title": "人工智能 大模型 人工智能", "summary": "大模型 开源",
         "source": "S", "link": "l", "published": "2024-01-01T00:00:00"},
        {"title": "大模型 评测", "summary": "人工智能 评测",
         "source": "S", "link": "l", "published": "2024-01-01T00:00:00"},
    ]
    df = ah.trending_chart_data(news, top_k=12)
    assert list(df.columns) == ["关键词", "次数"]
    assert len(df) > 0
    assert (df["次数"] > 0).all()
