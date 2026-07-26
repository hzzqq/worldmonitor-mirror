"""资讯抓取与解析模块（多源聚合 + 离线兜底）。

数据源：
  1. Hacker News Algolia API（JSON，无需鉴权）
  2. RSS（feedparser 解析，默认阮一峰 / 少数派）
  3. 内置 mock 示例数据（网络不可用时兜底）

对外主接口：get_news(force_refresh=False) -> (list[dict], str)
  返回 (资讯列表, 数据来源说明)
  每条资讯结构：
    {
      "title": str,
      "link": str,
      "source": str,
      "published": datetime,
      "summary": str,
    }
"""

from __future__ import annotations

import csv
import datetime as _dt
import difflib
import json
import logging
import os
import re
import time
from typing import Dict, List, Tuple

import requests

from log_utils import setup_logging

log = logging.getLogger("worldmonitor")

try:  # feedparser 为可选依赖，缺失时 RSS 源走 mock
    import feedparser
except Exception:  # pragma: no cover
    feedparser = None

# R1 新能力：诊断日志（抓取失败/汇总）写 stderr 或 WORLDMONITOR_LOG_FILE，
# 级别由 WORLDMONITOR_LOG_LEVEL 控制；默认 INFO。抓取失败除在 UI 提示外，
# 同时落日志便于后台/定时运行排障。
try:
    setup_logging(
        os.getenv("WORLDMONITOR_LOG_LEVEL", "INFO"),
        os.getenv("WORLDMONITOR_LOG_FILE"),
    )
except Exception:
    pass


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
HN_API = "http://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage=50"
RSS_SOURCES = {
    "阮一峰的网络日志": "https://www.ruanyifeng.com/blog/atom.xml",
    "少数派": "https://sspai.com/feed",
}

REQUEST_TIMEOUT = 8  # 秒
_HEADERS = {"User-Agent": "worldmonitor-mirror/0.1 (+https://example.com)"}


# ---------------------------------------------------------------------------
# 情绪词表（极简关键词命中法）
# ---------------------------------------------------------------------------
POSITIVE_WORDS = [
    "突破", "增长", "上涨", "成功", "发布", "开源", "创新", "领先", "盈利",
    "利好", "飙升", "里程碑", "获奖", "合作", "win", "launch", "breakthrough",
    "growth", "surge", "success", "open source", "record",
]
NEGATIVE_WORDS = [
    "暴跌", "下跌", "亏损", "裁员", "风险", "危机", "漏洞", "攻击", "封禁",
    "失败", "警告", "下滑", "利空", "崩盘", "crash", "down", "loss", "layoff",
    "breach", "risk", "fail", "warning", "decline",
]


def _sentiment_word_hits(word: str, low_text: str) -> bool:
    """判断情绪词是否命中文本（隐性正确性修复）。

    原实现对英文词用朴素子串匹配，导致 'win' 误命中 'window'、
    'down' 误命中 'download' 等假阳性，进而错判情绪。这里：
    - 中文词（含 CJK）仍用子串匹配（多字词，误命中概率低）；
    - 英文词用单词边界精确匹配，杜绝子串误命中。
    """
    w = word.lower()
    if re.search(r"[一-鿿]", w):  # 含中文：子串匹配
        return w in low_text
    # 英文（可能含空格短语如 'open source'）：整体单词边界匹配
    return re.search(rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])", low_text) is not None


def _word_match(query: str, text: str) -> bool:
    """单词边界感知匹配（与 _sentiment_word_hits 同一套规则）。

    中文（含 CJK）用子串；英文词用单词边界，避免 'cat' 误命中
    'category'、'in' 误命中 'include' 等子串假阳性。
    """
    q = query.lower()
    if re.search(r"[一-鿿]", q):
        return q in text
    return re.search(rf"(?<![a-z0-9]){re.escape(q)}(?![a-z0-9])", text) is not None


def _count_word_matches(query: str, text: str) -> int:
    """统计 query 在 text 中的命中次数，规则与 _word_match 一致
    （中文子串 / 英文单词边界），用于检索相关性排序。

    R2 修复（排序口径一致性）：原 search_news 用 `text.count(q)` 做相关性
    计分，对英文是按「裸子串」计数——'open' 会把它在 'opencode' 内的出现
    也算一次命中，使并未真正匹配的词被错误加权。现改用与「是否命中」同一套
    单词边界规则计数，排序得分与命中判定口径统一。
    """
    q = query.strip().lower()
    if not q:
        return 0
    if re.search(r"[一-鿿]", q):
        return text.count(q)  # 中文：子串计数
    pat = re.compile(rf"(?<![a-z0-9]){re.escape(q)}(?![a-z0-9])")
    return len(pat.findall(text))


def classify_sentiment(text) -> str:
    """基于关键词命中判断情绪：正面 / 负面 / 中性。

    命中规则：取正面与负面词命中数量较多者；若均未命中则为中性。

    R2 修复（隐性类型健壮性缺陷）：原签名与实现假定 `text` 必为 str，但
    上游 `build_dataframe` / `summarize_news` 经 `_news_text` 取字段，当标题或
    摘要意外是非字符串（None / 数字 / 畸形对象）时，`text.lower()` 会抛
    AttributeError 直接中断看板渲染。现对「非 str」做兜底归一（None→空串，
    其余转 str），保证情绪判定永不因字段类型异常而崩溃。
    """
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    if not text:
        return "中性"
    low = text.lower()
    pos = sum(1 for w in POSITIVE_WORDS if _sentiment_word_hits(w, low))
    neg = sum(1 for w in NEGATIVE_WORDS if _sentiment_word_hits(w, low))
    if pos > neg:
        return "正面"
    if neg > pos:
        return "负面"
    return "中性"


def _dedup_key(item: Dict) -> str:
    """去重键：优先用链接归一，否则用标题归一。"""
    link = _normalize_link(item.get("link"))
    title = (item.get("title") or "").strip().lower()
    return link or title


def _normalize_link(link) -> str:
    """把链接规整为去重键：去片段(#)与查询参数(?)，避免同一文章因

    `?utm_source=x` / `#ref` 等跟踪参数被误判为不同链接而漏去重
    （R2 隐性去重缺陷：原实现直接对原始 link 做 lower，导致
    `a.com/news` 与 `a.com/news?ref=1` 被视为两篇不同资讯并存）。
    """
    if not link:
        return ""
    s = str(link).strip().lower()
    s = s.split("#", 1)[0]
    s = s.split("?", 1)[0]
    return s.rstrip("/")


def _dedup_news(items: List[Dict]) -> List[Dict]:
    """按链接/标题去除重复资讯（多源聚合时的隐性重复问题）。"""
    seen = set()
    out: List[Dict] = []
    for it in items:
        key = _dedup_key(it)
        if key and key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def _title_similarity(a: str, b: str) -> float:
    """两标题的序列相似度（0~1）。

    用 difflib.SequenceMatcher 的比率，对「同题改写 / 增删标点 / 调换词序」
    等近似重复更鲁棒，比纯集合交并比更能反映「是否同一篇报道」。
    """
    a = (a or "").strip().lower()
    b = (b or "").strip().lower()
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def dedup_similar_news(news: List[Dict], threshold: float = 0.85) -> List[Dict]:
    """按标题相似度去除近似重复资讯（R1 新能力）。

    与 `_dedup_news`（仅按链接/标题「精确」归一去重）互补：多源转发同一
    条新闻时，链接带不同跟踪参数、标题被改写为「XX 突发」「刚刚！XX」等，
    精确键无法命中而漏去重（R2 隐性去重缺陷——近似重复仍会在看板并列出现、
    既占版面又放大权重）。这里用序列相似度对「保留下来的」结果再做一遍
    软去重，相似度 >= threshold 的后续条目被丢弃，保留首次出现者。

    - 始终保留第一项（无前驱可比对）；
    - 已通过 `_dedup_key` 精确去重的项仍可能互为近似，故在精确去重之后调用；
    - 相似度阈值 0~1，越大越严格（越难被判为重复）；threshold<=0 视为关闭；
    - 不修改入参，返回新列表。
    """
    if threshold <= 0:
        return list(news)
    out: List[Dict] = []
    kept_titles: list[str] = []
    for n in news:
        title = (n.get("title") or "").strip().lower()
        if any(_title_similarity(title, t) >= threshold for t in kept_titles):
            continue
        out.append(n)
        if title:
            kept_titles.append(title)
    return out


def group_by_sentiment(news: List[Dict]) -> Dict[str, List[Dict]]:
    """按情绪将资讯分组，便于看板做「正面/负面/中性」聚合展示。

    返回 {"正面": [...], "负面": [...], "中性": [...]}；未命中三类时归入中性。
    """
    groups: Dict[str, List[Dict]] = {"正面": [], "负面": [], "中性": []}
    for n in news:
        text = _news_text(n)
        s = classify_sentiment(text)
        groups.setdefault(s, groups["中性"]).append(n)
    return groups


def _tokens(text: str) -> "set[str]":
    """把文本切分为去停用词后的 token 集合（供 related_news 计算重叠度）。

    - 英文 / 数字：正则切分；
    - 中文（CJK）：单字 + 二元语法（不引入 jieba，离线可用）；
    - 全部小写、剔除停用词，保证跨条目可比。
    """
    text = (text or "").lower()
    toks: "set[str]" = set(re.findall(r"[a-z0-9]+", text))
    for seg in re.findall(r"[一-鿿]+", text):
        for i in range(len(seg)):
            toks.add(seg[i])  # 单字
            if i + 1 < len(seg):
                toks.add(seg[i:i + 2])  # 二元
    return {t for t in toks if t and t not in _STOPWORDS}


def related_news(news: List[Dict], ref: "str | Dict", top_k: int = 5) -> List[Dict]:
    """返回与给定参考（文本或单条资讯）最相关的资讯（按词重叠数降序）。

    R1 新能力：看板「相关阅读」组件——用户阅读某条资讯时，推荐语义相近的
    其它资讯（如同一事件的后续报道、同主题深度分析）。ref 可为字符串或单条
    资讯 dict（自动取标题 + 摘要）；空 news / 空 ref 返回空列表；重叠为 0 的
    条目不计入（绝不返回完全不相关的结果，避免「相关」名不副实）。
    """
    if not news:
        return []
    ref_text = _news_text(ref) if isinstance(ref, dict) else (ref or "")
    ref_tokens = _tokens(ref_text)
    if not ref_tokens:
        return []
    scored: List[Tuple[int, Dict]] = []
    for n in news:
        toks = _tokens(_news_text(n))
        if not toks:
            continue
        overlap = len(ref_tokens & toks)
        if overlap > 0:
            scored.append((overlap, n))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [n for _, n in scored[:top_k]]


def filter_news_by_sentiment(news: List[Dict], sentiment: str) -> List[Dict]:
    """按情绪标签过滤资讯（正面 / 负面 / 中性）。

    R1 新能力：为看板提供「只看某类情绪」的入口（如只看负面以
    快速识别风险资讯）。sentiment 为空 / None / 非法时原样返回。
    """
    if not sentiment:
        return list(news)
    want = sentiment.strip()
    out: List[Dict] = []
    for n in news:
        text = _news_text(n)
        if classify_sentiment(text) == want:
            out.append(n)
    return out


def _news_text(n: Dict) -> str:
    """把单条资讯的「标题 + 摘要」拼为小写文本（R2 去重：此前该拼接在
    classify_sentiment / filter / 导出等多处重复出现，未来一旦口径不一致
    会导致「检索命中但导出漏掉」等隐性偏差；现统一为单一来源）。
    """
    return f"{n.get('title', '')} {n.get('summary', '')}"


def filter_news_by_keyword(news: List[Dict], keyword: str) -> List[Dict]:
    """按关键词过滤资讯（标题 + 摘要，大小写不敏感）。

    R1 新能力：补充 search_news 之外的纯函数式关键词过滤，便于在已取得的
    列表上做轻量筛选（如「只看含某关键词的资讯」），无需触发检索打分。
    空 / 纯空白 keyword 原样返回全部。

    R2 修复（一致性）：原实现用朴素子串 `kw in text`，导致英文词 'cat' 误命中
    'category'、'open' 误命中 'opencode' 等假阳性，与 search_news 的边界匹配
    行为不一致。现改用与 search_news 同源的 `_word_match`（中文子串 / 英文
    单词边界），保证两处检索口径统一。
    """
    if not keyword or not keyword.strip():
        return list(news)
    kw = keyword.strip()
    out: List[Dict] = []
    for n in news:
        if _word_match(kw, _news_text(n).lower()):
            out.append(n)
    return out


def _filter_by_keyword(news: List[Dict], keyword: "str | None") -> List[Dict]:
    """按关键词过滤（与 _filter_by_source 对称的私有实现），供 get_news 聚合层复用。

    R1 新能力：让 get_news 的关键词预过滤与 source / sentiment 走同一套
    `_apply_news_filters` 入口，保证「来源 + 情绪 + 关键词」三者在 limit 截断前
    按统一顺序应用，缓存命中与全新抓取两条路径行为一致。
    keyword 为空 / None 时原样返回。
    """
    if not keyword:
        return news
    return filter_news_by_keyword(news, keyword)


def filter_news_by_time(news: List[Dict], hours: "int | float | None" = None) -> List[Dict]:
    """按时间窗口过滤资讯（保留最近 N 小时内的条目）。

    R1 新能力：把「时间范围」这一常用筛选从 UI 的 pandas 逻辑里抽离成
    纯函数，便于后端 API / 检索叠加复用，也更容易单测。

    - hours <= 0 或为空时原样返回全部（不做时间淘汰）；
    - published 非 datetime（缺时间信息）的条目保守保留，避免被误删；
    - 兼容带时区的 published（统一按 tzinfo=None 处理，与 get_news 返回
      的 naive datetime 对齐），杜绝 naive/aware 比较抛 TypeError。
    """
    if not hours or hours <= 0:
        return list(news)
    now = _dt.datetime.now()
    cutoff = now - _dt.timedelta(hours=hours)
    out: List[Dict] = []
    for n in news:
        pub = n.get("published")
        if not isinstance(pub, _dt.datetime):
            out.append(n)  # 无时间信息：保守保留
            continue
        try:
            if getattr(pub, "tzinfo", None) is not None:
                pub = pub.replace(tzinfo=None)
        except Exception:
            pass
        if pub >= cutoff:
            out.append(n)
    return out


def export_news_csv(news: List[Dict]) -> str:
    """把资讯列表序列化为 CSV 文本（含表头），用于导出 / 下载。

    使用 csv 模块做字段引用，避免标题/摘要中的逗号、换行破坏结构
    （CSV 注入 / 截断防护——隐性健壮性问题）。
    """
    import io

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["title", "link", "source", "published", "summary", "sentiment"])
    for n in news:
        published = n.get("published")
        pub = published.isoformat() if isinstance(published, _dt.datetime) else str(published or "")
        text = _news_text(n)
        writer.writerow([
            n.get("title", ""),
            n.get("link", ""),
            n.get("source", ""),
            pub,
            n.get("summary", ""),
            classify_sentiment(text),
        ])
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 抓取：Hacker News
# ---------------------------------------------------------------------------
def fetch_hacker_news() -> List[Dict]:
    """从 HN Algolia 拉取首页热点，返回资讯 dict 列表。"""
    out: List[Dict] = []
    resp = requests.get(HN_API, headers=_HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    for hit in data.get("hits", []):
        title = hit.get("title") or hit.get("story_title") or "(无标题)"
        link = hit.get("url") or (
            f"https://news.ycombinator.com/item?id={hit.get('objectID')}"
            if hit.get("objectID") else ""
        )
        ts = hit.get("created_at")
        published = _parse_iso(ts) if ts else _dt.datetime.now()
        out.append({
            "title": title,
            "link": link,
            "source": "Hacker News",
            "published": published,
            "summary": hit.get("story_text") or hit.get("comment_text") or "",
        })
    return out


# ---------------------------------------------------------------------------
# 抓取：RSS
# ---------------------------------------------------------------------------
def fetch_rss(source_name: str, url: str) -> List[Dict]:
    """用 feedparser 解析单个 RSS 源。"""
    if feedparser is None:
        raise RuntimeError("feedparser 不可用")
    parsed = feedparser.parse(url)
    out: List[Dict] = []
    for entry in getattr(parsed, "entries", []):
        published = _parse_feed_time(entry) or _dt.datetime.now()
        summary = getattr(entry, "summary", "") or ""
        out.append({
            "title": getattr(entry, "title", "(无标题)"),
            "link": getattr(entry, "link", ""),
            "source": source_name,
            "published": published,
            "summary": summary,
        })
    return out


def _parse_feed_time(entry) -> _dt.datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        val = getattr(entry, key, None)
        if val:
            try:
                return _dt.datetime(*val[:6])
            except Exception:
                continue
    raw = getattr(entry, "published", None) or getattr(entry, "updated", None)
    if raw:
        return _parse_iso(raw)
    return None


def _parse_iso(s: str) -> _dt.datetime | None:
    try:
        return _dt.datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        try:
            return _dt.datetime.strptime(s, "%a, %d %b %Y %H:%M:%S %z").replace(tzinfo=None)
        except Exception:
            return None


# ---------------------------------------------------------------------------
# Mock 兜底数据
# ---------------------------------------------------------------------------
def _mock_news() -> List[Dict]:
    """内置示例资讯，保证离线可跑、不报错。"""
    now = _dt.datetime.now()
    samples = [
        ("国产大模型开源生态迎来突破，社区贡献量创新高", "https://example.com/a1", "示例·科技前线", "开源 突破 增长"),
        ("某芯片厂商发布新一代 AI 加速卡，性能飙升", "https://example.com/a2", "示例·硬件观察", "发布 飙升 创新"),
        ("全球云计算市场持续增长，头部厂商盈利扩大", "https://example.com/a3", "示例·行业研究", "增长 盈利 领先"),
        ("某社交平台遭网络攻击，用户数据面临风险", "https://example.com/a4", "示例·安全晚报", "攻击 风险 漏洞"),
        ("新能源车企季度亏损扩大，股价下跌", "https://example.com/a5", "示例·财经速递", "亏损 下跌 利空"),
        ("开源数据库项目获里程碑式合作，社区欢呼", "https://example.com/a6", "示例·开发者周刊", "合作 里程碑 开源"),
        ("某电商巨头宣布裁员，市场担忧情绪升温", "https://example.com/a7", "示例·商业观察", "裁员 风险 下滑"),
        ("量子计算研究取得 breakthrough，论文登顶期刊", "https://example.com/a8", "示例·前沿科学", "breakthrough 创新 成功"),
        ("操作系统新版发布，启动速度 record 提升", "https://example.com/a9", "示例·软件更新", "launch record 增长"),
        ("某银行系统 crash 引发 warning，监管介入调查", "https://example.com/a10", "示例·金融科技", "crash warning 风险"),
    ]
    out = []
    for i, (title, link, source, summary) in enumerate(samples):
        out.append({
            "title": title,
            "link": link,
            "source": source,
            "published": now - _dt.timedelta(hours=i * 3),
            "summary": summary,
        })
    return out


# ---------------------------------------------------------------------------
# 轻量 TTL 缓存（与 market 模块一致，避免重复网络抓取 / 刷新语义模糊）
# ---------------------------------------------------------------------------
_NEWS_CACHE_TTL = float(os.getenv("NEWS_CACHE_TTL", "30"))
_NEWS_CACHE: Dict[str, Tuple[object, float]] = {}


def _news_cache_get(key: str):
    item = _NEWS_CACHE.get(key)
    if item and (time.time() - item[1]) < _NEWS_CACHE_TTL:
        return item[0]
    return None


def _news_cache_set(key: str, value) -> None:
    _NEWS_CACHE[key] = (value, time.time())


def clear_news_cache() -> None:
    """清空资讯缓存（便于测试与手动刷新，与 market.clear_market_cache 对齐）。"""
    _NEWS_CACHE.clear()


# ---------------------------------------------------------------------------
# 聚合主接口
# ---------------------------------------------------------------------------
def sort_news(news: List[Dict], order: str = "desc") -> List[Dict]:
    """按发布时间排序（desc=最新优先，asc=最旧优先）。

    R1 新能力：把「按时间排序」从 get_news 的 inline lambda 抽成可复用、可单测
    的纯函数，供后端 API / 看板 / 检索叠加统一复用，避免各处重复实现排序逻辑。

    R2 稳健性：原 inline 写法 `x.get("published") or _dt.datetime.min` 在
    published 为「非 datetime 但为真值」的对象（如字符串、带时区 datetime）时
    会返回该对象，参与比较可能因类型不兼容抛 TypeError；现对每条规整为可比较的
    naive datetime——缺 published / 非 datetime 一律视为最旧（落末尾），带时区
    统一去 tzinfo，排序永不抛错。不修改入参。
    """
    def _key(n: Dict) -> _dt.datetime:
        pub = n.get("published")
        if not isinstance(pub, _dt.datetime):
            return _dt.datetime.min
        try:
            if getattr(pub, "tzinfo", None) is not None:
                pub = pub.replace(tzinfo=None)
        except Exception:
            return _dt.datetime.min
        return pub

    reverse = order != "asc"
    return sorted(news, key=_key, reverse=reverse)


def top_news(news: List[Dict], n: int = 5, sentiment: "str | None" = None) -> List[Dict]:
    """返回最近 N 条资讯（按发布时间倒序），可选按情绪过滤。

    R1 新能力：为看板「最新 N 条」头部组件 / 摘要区提供统一便捷入口，
    避免各处重复做「过滤 + 排序 + 截断」三连（此前 app 里时间排序依赖
    pandas，纯后端场景需自己再写一遍）。

    R2 一致性：直接复用上方 sort_news 纯函数做时间排序，避免再内联一次
    `sorted(...)`，造成「缺 published 处理 / 带时区规整」口径与 sort_news
    漂移（c96 已统一该口径）。
    """
    out = news
    if sentiment:
        out = filter_news_by_sentiment(out, sentiment)
    out = sort_news(out, "desc")
    if n and n > 0:
        out = out[:n]
    return out


def filter_news_by_source(news: List[Dict], source: "str | None") -> List[Dict]:
    """按来源名称子串过滤资讯（与 filter_news_by_sentiment 对称的公开 API）。

    R1 新能力：此前来源过滤只有私有 `_filter_by_source`，调用方（如看板后端
    / 检索叠加）想对已取得列表做纯函数式来源筛选时只能依赖私有实现。
    这里暴露为公开函数，便于在 app 之外复用，且语义与 `_filter_by_source` 一致。
    source 为空 / None 时原样返回。
    """
    return _filter_by_source(news, source)


def _apply_news_filters(news: List[Dict], source: "str | None" = None,
                        sentiment: "str | None" = None,
                        keywords: "str | None" = None,
                        hours: "int | float | None" = None) -> List[Dict]:
    """对资讯列表统一应用「来源 + 情绪 + 关键词 + 时间窗口」过滤（R1 + R2）。

    R2 修复（隐性一致性缺陷）：此前来源过滤在 get_news 两处分支里各自内联，
    且情绪过滤、关键词过滤从未接入聚合层——调用方若想「按来源 + 情绪 + 关键词 +
    时间」组合筛选，只能自行在 get_news 返回后再补调 filter，既重复又容易在
    limit 截断顺序上不一致（先截还是先筛结果不同）。现抽出统一入口，保证四类
    过滤均在 limit 截断之前按「来源 → 情绪 → 关键词 → 时间」顺序应用，缓存命中
    与全新抓取两条路径行为完全一致。
    """
    news = _filter_by_source(news, source)
    if sentiment:
        news = filter_news_by_sentiment(news, sentiment.strip())
    news = _filter_by_keyword(news, keywords)
    if hours:
        # R1 新能力：时间窗口过滤接入聚合层（复用 filter_news_by_time 纯函数）
        news = filter_news_by_time(news, hours)
    return news


def get_news(force_refresh: bool = False, limit: "int | None" = None,
             source: "str | None" = None,
             sentiment: "str | None" = None,
             keywords: "str | None" = None,
             hours: "int | float | None" = None) -> Tuple[List[Dict], str]:
    """聚合多源资讯，网络失败时回退 mock。

    返回 (资讯列表, 来源说明文本)。来源说明用于 UI 友好提示。

    force_refresh：忽略缓存强制重新拉取。
    limit：返回条数上限（按时间倒序截取前 N 条），None 表示不限。
    source：按来源名称子串过滤（不区分大小写），None 表示不过滤。
    keywords：按关键词过滤（标题 + 摘要，边界匹配），None 表示不过滤。
    hours：时间窗口过滤（仅保留最近 N 小时资讯），None / <=0 表示不过滤。
    """
    cache_key = "news"
    log.info("开始聚合资讯 force_refresh=%s source=%s sentiment=%s hours=%s", force_refresh, source, sentiment, hours)
    if not force_refresh:
        cached = _news_cache_get(cache_key)
        if cached is not None:
            collected, notes = cached
            # R2 修复（隐性别名/可变性缺陷）：此前直接把缓存里的列表对象返回，
            # 调用方若对返回列表做 in-place 修改（如 sort / pop / 覆盖元素）会
            # 污染缓存，导致后续命中缓存的请求拿到被篡改的数据。现返回独立副本。
            # R1 统一过滤：来源 + 情绪 + 关键词均在 limit 截断前应用（缓存命中路径）。
            collected = _apply_news_filters(collected, source, sentiment, keywords, hours)
            if limit is None or limit < 0:
                return list(collected), notes
            return list(collected[:limit]), notes

    collected: List[Dict] = []
    notes: List[str] = []

    # 1) Hacker News
    try:
        hn = fetch_hacker_news()
        collected.extend(hn)
        notes.append(f"Hacker News {len(hn)} 条")
    except Exception as exc:  # 网络/解析失败 -> 提示但不崩溃
        notes.append(f"Hacker News 抓取失败（{type(exc).__name__}），已跳过")
        log.warning("Hacker News 抓取失败：%s", exc)

    # 2) RSS 源
    for name, url in RSS_SOURCES.items():
        try:
            items = fetch_rss(name, url)
            collected.extend(items)
            notes.append(f"{name} {len(items)} 条")
        except Exception as exc:
            notes.append(f"{name} 抓取失败（{type(exc).__name__}），已跳过")
            log.warning("%s 抓取失败：%s", name, exc)

    # 3) 兜底：若全部失败则使用 mock
    if not collected:
        collected = _mock_news()
        notes.append("⚠️ 全部网络源不可用，已回退内置示例数据（离线模式）")
    elif len(collected) < 5:
        # 部分成功也补充少量示例，保证 UI 不空
        collected.extend(_mock_news()[:5])
        notes.append("已补充部分示例数据以丰富展示")

    # 按时间倒序
    collected = _dedup_news(collected)
    collected = sort_news(collected, "desc")

    notes_str = "；".join(notes)
    # 缓存独立副本，避免后续对返回值的修改反向污染缓存
    _news_cache_set(cache_key, (list(collected), notes_str))
    # R1 统一过滤：来源 + 情绪 + 关键词均在 limit 截断前应用（全新抓取路径）。
    collected = _apply_news_filters(collected, source, sentiment, keywords, hours)
    if limit is not None and limit >= 0:
        collected = collected[:limit]
    log.info("资讯聚合完成 条数=%d 说明=%s", len(collected), notes_str)
    return list(collected), notes_str


def _filter_by_source(news: List[Dict], source: "str | None") -> List[Dict]:
    """按来源名称子串过滤（不区分大小写）；source 为空/None 时原样返回。"""
    if not source:
        return news
    key = source.lower()
    return [n for n in news if key in (n.get("source") or "").lower()]


def export_news_json(news: List[Dict]) -> str:
    """把资讯列表序列化为 JSON 文本（紧凑、确保 ASCII 安全），供完整机读导出 / 下载。

    R1 新能力：与 export_news_csv（人读表格）互补——CSV 利于表格软件，
    JSON 利于程序化消费与跨系统对接（如喂给下游分析管线），二者覆盖不同场景。

    R2 修复（一致性缺陷）：此前 JSON 导出直接 dumps 原始 news，不含 sentiment
    字段，而 export_news_csv 会附 sentiment 列——两条导出管道口径不一致，下游
    消费 JSON 的流水线拿不到情绪标签。现与 CSV 对齐，为每条资讯补 sentiment。
    """
    enriched = []
    for n in news:
        item = dict(n)
        text = _news_text(n)
        item["sentiment"] = classify_sentiment(text)
        # R2 修复（隐性崩溃）：实际资讯里 published 是 datetime 对象，
        # json.dumps 会抛 TypeError: Object of type datetime is not
        # JSON serializable，导致真实数据导出时整条链路崩溃。现统一规整为
        # ISO 字符串（与 export_news_csv 的口径一致）。仅改副本，不动入参。
        pub = item.get("published")
        if isinstance(pub, _dt.datetime):
            item["published"] = pub.isoformat()
        enriched.append(item)
    return json.dumps(enriched, ensure_ascii=False, indent=2)


def export_news_markdown(news: List[Dict]) -> str:
    """把资讯列表序列化为 Markdown 文本（人读 / 分享友好），与 CSV / JSON 互补。

    R1 新能力：CSV 利于表格软件、JSON 利于程序化消费，Markdown 则适合直接贴进
    文档 / 即时通讯 / 看板静态展示。每条含标题（链接）、来源、时间、情绪标签。
    """
    lines = ["# 资讯导出", ""]
    if not news:
        lines.append("_（无资讯）_")
        return "\n".join(lines) + "\n"
    for i, n in enumerate(news, 1):
        title = n.get("title", "(无标题)")
        link = n.get("link", "")
        source = n.get("source", "")
        published = n.get("published")
        pub = published.isoformat() if isinstance(published, _dt.datetime) else str(published or "")
        text = _news_text(n)
        sentiment = classify_sentiment(text)
        title_md = f"[{title}]({link})" if link else title
        lines.append(f"{i}. {title_md}")
        meta = f"   来源：{source}　时间：{pub}　情绪：{sentiment}"
        lines.append(meta)
    return "\n".join(lines) + "\n"


def export_news_html(news: List[Dict]) -> str:
    """把资讯列表序列化为独立 HTML 文档（人读 / 单文件分享），与 CSV/JSON/MD 互补。

    R1 新能力：HTML 便于直接浏览器打开、贴进富文本或存档；每条含标题（链接）、
    来源、时间、情绪标签。所有用户文本经 HTML 转义，避免标题/来源中的特殊字符
    破坏结构或被注入（R2 安全）。
    """
    def esc(s):
        return (str(s).replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;"))

    parts = ['<!DOCTYPE html>', '<html lang="zh-CN">', '<head>',
             '<meta charset="utf-8">', '<title>资讯导出</title>', '</head>',
             '<body>', '<h1>资讯导出</h1>']
    if not news:
        parts.append('<p>（无资讯）</p>')
    else:
        parts.append('<ul>')
        for n in news:
            title = n.get("title", "(无标题)")
            link = n.get("link", "")
            source = n.get("source", "")
            published = n.get("published")
            pub = published.isoformat() if isinstance(published, _dt.datetime) else str(published or "")
            text = _news_text(n)
            sentiment = classify_sentiment(text)
            title_html = f'<a href="{esc(link)}">{esc(title)}</a>' if link else esc(title)
            parts.append(
                f'<li>{title_html}<br><small>来源：{esc(source)}　时间：{esc(pub)}'
                f'　情绪：{esc(sentiment)}</small></li>'
            )
        parts.append('</ul>')
    parts.append('</body></html>')
    return "\n".join(parts) + "\n"


# 趋势词停用词表（极简，避免高频虚词污染「热门话题」）
_STOPWORDS = {
    "的", "了", "和", "与", "在", "是", "为", "对", "及", "等", "也", "并",
    "将", "已", "该", "其", "此", "中", "上", "下", "新", "公司", "平台",
    "我们", "你们", "他们", "一个", "可以", "通过", "已经", "表示",
    "the", "and", "for", "with", "that", "this", "have", "from", "are",
    "will", "your", "been", "were", "they", "their", "about", "would",
}


def trending_keywords(news: List[Dict], top_k: int = 10, min_len: int = 2,
                     max_ngram: int = 2) -> List[Tuple[str, int]]:
    """提取资讯语料中的热门关键词（供看板「趋势话题」组件）。

    R1 新能力：看板需要一眼看出近期舆论焦点，但整套模块此前只有
    「按情绪/来源/关键词过滤」与「检索」，缺少对全量语料的「高频词提炼」。
    这里实现无外部分词依赖的轻量提取：
      - 英文 / 数字词：正则切分后按词计数（长度 >= min_len）；
      - 中文（CJK）：连续段按 n 元语法（n-gram）切分计数（不引入 jieba 依赖，
        在离线/受限环境也能跑），单字默认跳过；
      - 命中停用词表的词被剔除，避免「的/公司/平台」等高频虚词霸榜；
      - 返回 [(词, 次数), ...] 按次数降序（次数相同按词升序稳定），取前 top_k。
    纯函数、无副作用、不修改入参，便于单测。

    R1 新能力（max_ngram）：默认 2（二元语法），可设更大以额外统计三元及以上
    语法（如「大模型」「开源生态」），捕捉更长的真实短语，趋势词更有信息量。
    R2 修复（min_len 一致性）：此前 min_len 仅对英文 token 生效，中文二元语法
    恒为 2 字、绕过 min_len，导致 `trending_keywords(..., min_len=3)` 仍混入
    2 字中文碎片。现对中文 n-gram 统一应用 min_len（短于此的 n-gram 跳过），
    参数口径与英文一致。
    """
    from collections import Counter

    counter: Counter = Counter()
    for n in news:
        text = _news_text(n).lower()
        # 英文 / 数字词
        for w in re.findall(r"[a-z0-9]+", text):
            # R2 修复（隐性噪声缺陷）：纯数字 token（如 "2024" / "2023"）此前
            # 会被当作热门关键词计入趋势榜，污染「热门话题」图表、稀释真正有意义的
            # 词。年份 / 数字串不具备话题语义，这里整体跳过纯数字 token。
            if w.isdigit():
                continue
            if len(w) >= min_len and w not in _STOPWORDS:
                counter[w] += 1
        # 中文：CJK 连续段按 n 元语法（2..max_ngram）切分
        for seg in re.findall(r"[一-鿿]+", text):
            if len(seg) < 2:
                continue
            max_n = min(max_ngram, len(seg))
            for k in range(2, max_n + 1):
                if k < min_len:  # R2 修复：min_len 对中文 n-gram 统一生效
                    continue
                for i in range(len(seg) - k + 1):
                    ng = seg[i:i + k]
                    if ng in _STOPWORDS:
                        continue
                    counter[ng] += 1
    return sorted(counter.items(), key=lambda x: (-x[1], x[0]))[:top_k]


def available_sources(news: List[Dict]) -> List[str]:
    """返回资讯中出现过的去重来源名称（排序），供 UI 构造来源过滤选项。"""
    srcs = {n.get("source", "") for n in news if n.get("source")}
    return sorted(srcs)


def group_by_source(news: List[Dict]) -> Dict[str, List[Dict]]:
    """按来源将资讯分组，返回 {来源: [资讯...]}，供看板做「来源分布」聚合。

    R1 新能力：与 group_by_sentiment（按情绪分组）对称，补齐「按来源
    分组」维度——看板可据此绘制各来源的产出量、或在某一来源下
    单独折叠/展开。分组保持各分组内相对顺序与入参一致（稳定），
    空列表返回空 dict；无 source 字段的条目归入 "" 键（不丢失）。
    纯函数、无副作用、不修改入参。
    """
    groups: Dict[str, List[Dict]] = {}
    for n in news:
        key = n.get("source", "") or ""
        groups.setdefault(key, []).append(n)
    return groups


def news_by_hour(news: List[Dict], window_hours: int = 24) -> List[Tuple[str, int]]:
    """统计最近 window_hours 个整点桶内每个小时的资讯发布量，供看板「发布时间分布」图。

    R1 新需求：看板已有「按天时间线」与「情绪趋势（按天）」，但缺少更细的
    「按小时分布」，难以观察资讯在一天内的集中发布时段（如开盘 / 收盘前后、
    夜间突发事件）。这里生成最近 window_hours 个小时桶的发布计数，含 0 桶，
    保证图表连续、不丢时段。

    - 返回 [(时段标签, 条数), ...] 按时间升序（最旧桶在前），共 window_hours 个桶；
    - 时段标签形如 "14:00"（本地时钟整点）；
    - 无 published（非 datetime）的条目保守跳过，避免把缺时间的数据错算进某桶；
    - 带时区的 published 统一去 tzinfo 后再分桶，避免 naive/aware 比较抛 TypeError；
    - 落在窗口之前的旧资讯忽略，窗口内的归入对应整点桶；不修改入参，纯函数可单测。
    """
    if window_hours <= 0:
        window_hours = 24
    now = _dt.datetime.now().replace(minute=0, second=0, microsecond=0)
    base = now - _dt.timedelta(hours=window_hours - 1)  # 最早桶起点（整点）
    counts = [0] * window_hours
    for n in news:
        pub = n.get("published")
        if not isinstance(pub, _dt.datetime):
            continue  # 缺时间信息：保守跳过
        try:
            if getattr(pub, "tzinfo", None) is not None:
                pub = pub.replace(tzinfo=None)
        except Exception:
            continue
        if pub < base:  # 早于窗口起点：忽略
            continue
        delta_h = int((pub - base).total_seconds() // 3600)
        if 0 <= delta_h < window_hours:
            counts[delta_h] += 1
    return [( (base + _dt.timedelta(hours=h)).strftime("%H:00"), counts[h] )
            for h in range(window_hours) ]


def sentiment_counts(news: List[Dict]) -> Dict:
    """统计资讯的情绪分布：{"正面": n, "负面": n, "中性": n, "total": n}。

    R1 新能力：此前看板只在 `summarize_news` 里内联做了一遍情绪计数，
    无法被其它调用方（CLI 汇总、单测、导出）复用。抽成独立纯函数后，
    `summarize_news` 与其余入口可共享同一套计数口径，也更易单测。
    非字符串字段经 `classify_sentiment` 内部兜底，不会中断。
    """
    counts = {"正面": 0, "负面": 0, "中性": 0, "total": 0}
    for n in news:
        counts[classify_sentiment(_news_text(n))] = counts.get(
            classify_sentiment(_news_text(n)), 0
        ) + 1
        counts["total"] += 1
    return counts


def summarize_news(news: List[Dict]) -> Dict:
    """聚合统计：总量、按来源计数、按情绪计数，供看板做概览指标。

    返回结构：
      {"total": int, "by_source": {src: n}, "by_sentiment": {"正面":..,"负面":..,"中性":..}}
    纯统计、无副作用，便于在 UI 顶部以 metric 形式呈现「舆情分布」。

    R3 去重（DRY）：情绪计数逻辑收敛到 `sentiment_counts`，避免统计口径
    在多处散落、未来一处改了另一处漏改导致「概览指标」与「列表情绪」对不上。
    """
    from collections import Counter

    by_source: Counter = Counter()
    for n in news:
        by_source[n.get("source", "")] += 1
    by_sent = {k: v for k, v in sentiment_counts(news).items() if k != "total"}
    return {
        "total": len(news),
        "by_source": dict(by_source),
        "by_sentiment": by_sent,
    }


def search_news(query: str, news: "List[Dict] | None" = None,
                source: "str | None" = None,
                sentiment: "str | None" = None,
                hours: "int | float | None" = None) -> List[Dict]:
    """全文检索资讯（标题 + 摘要中匹配 query 子串，不区分大小写）。

    R1 新能力：为看板提供可搜索的资讯入口。
    - query 为空 / 纯空白时返回空列表（避免「空查询 = 全量」的隐性歧义，
      否则前端搜索框清空会误把全部资讯当结果）；
    - news 为 None 时自动拉取 get_news()（带缓存，离线走 mock，无网络依赖）；
    - source 子串过滤可与检索叠加；
    - sentiment 情绪过滤可与检索叠加（R1 补齐：与 get_news 的
      source+sentiment+keywords+hours 三件套对称，此前 search 缺 sentiment，
      调用方无法在「检索 + 只看负面」这类场景复用）；
    - hours 时间窗口过滤（与 get_news 聚合层对称）可与检索叠加，支持
      「只搜最近 N 小时」的资讯，避免检索混入陈旧旧闻；
    - 返回按相关性（命中次数）降序，便于优先展示最相关条目。
    """
    if not query or not query.strip():
        return []
    if news is None:
        news, _ = get_news()
    # R1 新能力：hours 时间窗口过滤（与 get_news 聚合层对称）。
    if hours:
        news = filter_news_by_time(news, hours)
    # R1 补齐：sentiment 情绪过滤与检索叠加（与 get_news 对称）。
    # R2 一致性：须在「打分/排序」之前应用，保证检索结果集先用
    # 情绪收窄，再对相关条目打分——与 get_news 的「先过滤后截断」口径一致，
    # 避免出现「过滤顺序不同导致检索结果与聚合获取结果不一致」。
    if sentiment:
        news = filter_news_by_sentiment(news, sentiment.strip())
    q = query.strip().lower()
    scored: List[Tuple[int, Dict]] = []
    for n in news:
        text = _news_text(n).lower()
        # R2 修复：原实现用朴素子串 `q in text`，导致 'cat' 误命中
        # 'category'、'in' 误命中 'include' 等假阳性；改用单词边界匹配。
        if _word_match(q, text) and (source is None or source.lower() in (n.get("source") or "").lower()):
            scored.append((_count_word_matches(q, text), n))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [n for _, n in scored]


def paginate_news(news: List[Dict], page: int = 1, page_size: int = 10) -> Dict:
    """对资讯列表做内存分页，返回当期页与分页元数据。

    R1 新能力：看板在资讯量大时无需一次性渲染全部，可按页加载（与
    openwebui-lite 的会话消息分页思路一致，这里作用于资讯流）。

    - page 从 1 开始；page / page_size 非正或非法时回退默认（1 / 10），
      避免调用方传 0 / 负数导致切片异常或空结果（隐性健壮性）；
    - 超出范围（page 大于总页数）会被夹到最后一页而非返回空。
    """
    page = page if isinstance(page, int) and page > 0 else 1
    page_size = page_size if isinstance(page_size, int) and page_size > 0 else 10
    total = len(news)
    pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, pages)
    start = (page - 1) * page_size
    return {
        "items": news[start:start + page_size],
        "page": page,
        "page_size": page_size,
        "total": total,
        "pages": pages,
    }


if __name__ == "__main__":
    news, msg = get_news()
    print(msg)
    for n in news[:5]:
        print(f"[{n['source']}] {n['title']} -> {classify_sentiment(n['title'])}")
