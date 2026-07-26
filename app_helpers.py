"""worldmonitor 看板纯函数辅助（从 app.py 抽出，便于单测 + 复用）。

R3（模块化 / DRY）：原 highlight_keyword / build_dataframe 直接写在 Streamlit
入口脚本里，无法被 pytest 覆盖（import app 会触发 st.set_page_config）。抽到
这里后，纯逻辑可独立验证，app.py 仅负责编排渲染。

R2（隐性数据正确性 bug 修复）：原 build_dataframe 对 `published` 字段只做
`it.get("published") or now`，若来源返回的是「字符串时间」（部分 RSS / 示例数据），
pub 为 str，后续 `pub.date()` 走 `else None`，导致这类条目在「发布量时间线」
被 dropna 静默丢弃——时间图表莫名缺数据却无报错。现统一用 _to_dt 把字符串
时间解析成 datetime（解析失败才归 None），时间图表不再丢条目。
"""
from __future__ import annotations

import datetime as _dt
import re as _re

import pandas as pd

import data_feed


def highlight_keyword(title: str, keyword: str) -> str:
    """在标题中高亮命中的关键词（忽略大小写），用于资讯列表。"""
    if not keyword:
        return title
    return _re.sub(f"(?i)({_re.escape(keyword)})", r"**\1**", title)


def _to_dt(value) -> "_dt.datetime | None":
    """把 published 字段统一成 datetime。

    - datetime 原样返回；
    - 非空字符串用 pandas 宽松解析（兼容 ISO / 含 Z / 空格分隔等多种格式）；
    - 缺失 / 空串 / 解析失败 → 返回 None（绝不让非法时间悄悄挤掉图表数据）。
    """
    if isinstance(value, _dt.datetime):
        return value
    if isinstance(value, str) and value.strip():
        try:
            ts = pd.to_datetime(value, errors="coerce")
        except Exception:
            return None
        if pd.isna(ts):
            return None
        return ts.to_pydatetime()
    return None


def build_dataframe(items: list) -> "pd.DataFrame":
    """把资讯条目列表构造成看板用的 DataFrame（含来源/时间/情绪等列）。"""
    rows = []
    for it in items:
        pub = _to_dt(it.get("published"))
        text = f"{it.get('title', '')} {it.get('summary', '')}"
        rows.append({
            "标题": it.get("title", ""),
            "来源": it.get("source", ""),
            "链接": it.get("link", ""),
            "发布时间": pub,
            "日期": pub.date() if pub else None,
            "小时": pub.hour if pub else 0,
            "情绪": data_feed.classify_sentiment(text),
        })
    return pd.DataFrame(rows)


def trending_chart_data(items: list, top_k: int = 10) -> "pd.DataFrame":
    """返回热门关键词 DataFrame（词, 次数），供 Plotly 柱状图。

    R1（暴露后端能力）：data_feed.trending_keywords（c111 已提供）此前未在
    看板落地，用户看不到「近期舆论焦点」。这里把它转成图表数据。
    """
    pairs = data_feed.trending_keywords(items, top_k=top_k)
    return pd.DataFrame(pairs, columns=["关键词", "次数"])


def paginate_dataframe(df: "pd.DataFrame", page: int = 1, page_size: int = 10) -> "tuple[pd.DataFrame, dict]":
    """对看板 DataFrame 做内存分页，返回 (当期页 DataFrame, 分页元数据)。

    R1 新能力：资讯量大时无需一次性渲染全部，按页加载（与 openwebui-lite
    会话消息分页、data_feed.paginate_news 思路一致）。
    R2 隐性健壮性：page / page_size 非正或非法时回退默认（1 / 10），
    超出范围被夹到最后一页而非抛错或返回空（避免调用方传 0 / 负数导致
    切片异常或空结果）。
    """
    page = page if isinstance(page, int) and page > 0 else 1
    page_size = page_size if isinstance(page_size, int) and page_size > 0 else 10
    total = len(df)
    pages = max(1, (total + page_size - 1) // page_size) if total else 1
    page = min(page, pages)
    start = (page - 1) * page_size
    page_df = df.iloc[start:start + page_size]
    meta = {
        "page": page,
        "page_size": page_size,
        "total": total,
        "pages": pages,
    }
    return page_df, meta


def search_and_paginate(news_items: list, query: str = "", page: int = 1,
                        page_size: int = 10, source: str = None,
                        sentiment: str = None, hours: "int | float | None" = None) -> dict:
    """组合「检索 + 分页」的看板入口（便于前端一次性拿到展示子集与分页元数据）。

    R1 新能力：把 data_feed.search_news 与 paginate_news 收敛为一个看板调用，
    减少 app.py 编排重复。
    R2 一致性修复（隐性 UX 缺陷）：data_feed.search_news 在空查询时返回 []，
    若前端直接拿它渲染，会导致「搜索框清空 = 整页空白」，用户误以为数据丢失。
    这里对空查询短路为「不做检索、展示全部」，再分页——空搜索应等价于
    「浏览全部资讯」，而非「零结果」。source/sentiment/hours 过滤与检索叠加。
    """
    if query and query.strip():
        base = data_feed.search_news(
            query, news_items, source=source, sentiment=sentiment, hours=hours
        )
    else:
        # R2：空查询 → 展示全部（不调用 search_news，避免返回空列表）
        base = list(news_items)
    paginated = data_feed.paginate_news(base, page=page, page_size=page_size)
    paginated["query"] = query or ""
    return paginated
