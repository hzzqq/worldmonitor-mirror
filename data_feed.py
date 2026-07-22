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
import os
import time
from typing import Dict, List, Tuple

import requests

try:  # feedparser 为可选依赖，缺失时 RSS 源走 mock
    import feedparser
except Exception:  # pragma: no cover
    feedparser = None


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


def classify_sentiment(text: str) -> str:
    """基于关键词命中判断情绪：正面 / 负面 / 中性。

    命中规则：取正面与负面词命中数量较多者；若均未命中则为中性。
    """
    if not text:
        return "中性"
    low = text.lower()
    pos = sum(1 for w in POSITIVE_WORDS if w.lower() in low)
    neg = sum(1 for w in NEGATIVE_WORDS if w.lower() in low)
    if pos > neg:
        return "正面"
    if neg > pos:
        return "负面"
    return "中性"


def _dedup_key(item: Dict) -> str:
    """去重键：优先用链接归一，否则用标题归一。"""
    link = (item.get("link") or "").strip().lower()
    title = (item.get("title") or "").strip().lower()
    return link or title


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


def group_by_sentiment(news: List[Dict]) -> Dict[str, List[Dict]]:
    """按情绪将资讯分组，便于看板做「正面/负面/中性」聚合展示。

    返回 {"正面": [...], "负面": [...], "中性": [...]}；未命中三类时归入中性。
    """
    groups: Dict[str, List[Dict]] = {"正面": [], "负面": [], "中性": []}
    for n in news:
        text = f"{n.get('title', '')} {n.get('summary', '')}"
        s = classify_sentiment(text)
        groups.setdefault(s, groups["中性"]).append(n)
    return groups


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
        text = f"{n.get('title', '')} {n.get('summary', '')}"
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
def get_news(force_refresh: bool = False, limit: "int | None" = None,
             source: "str | None" = None) -> Tuple[List[Dict], str]:
    """聚合多源资讯，网络失败时回退 mock。

    返回 (资讯列表, 来源说明文本)。来源说明用于 UI 友好提示。

    force_refresh：忽略缓存强制重新拉取。
    limit：返回条数上限（按时间倒序截取前 N 条），None 表示不限。
    source：按来源名称子串过滤（不区分大小写），None 表示不过滤。
    """
    cache_key = "news"
    if not force_refresh:
        cached = _news_cache_get(cache_key)
        if cached is not None:
            collected, notes = cached
            collected = _filter_by_source(collected, source)
            if limit is None or limit < 0:
                return collected, notes
            return collected[:limit], notes

    collected: List[Dict] = []
    notes: List[str] = []

    # 1) Hacker News
    try:
        hn = fetch_hacker_news()
        collected.extend(hn)
        notes.append(f"Hacker News {len(hn)} 条")
    except Exception as exc:  # 网络/解析失败 -> 提示但不崩溃
        notes.append(f"Hacker News 抓取失败（{type(exc).__name__}），已跳过")

    # 2) RSS 源
    for name, url in RSS_SOURCES.items():
        try:
            items = fetch_rss(name, url)
            collected.extend(items)
            notes.append(f"{name} {len(items)} 条")
        except Exception as exc:
            notes.append(f"{name} 抓取失败（{type(exc).__name__}），已跳过")

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
    collected.sort(key=lambda x: x.get("published") or _dt.datetime.min, reverse=True)

    notes_str = "；".join(notes)
    _news_cache_set(cache_key, (collected, notes_str))
    collected = _filter_by_source(collected, source)
    if limit is not None and limit >= 0:
        collected = collected[:limit]
    return collected, notes_str


def _filter_by_source(news: List[Dict], source: "str | None") -> List[Dict]:
    """按来源名称子串过滤（不区分大小写）；source 为空/None 时原样返回。"""
    if not source:
        return news
    key = source.lower()
    return [n for n in news if key in (n.get("source") or "").lower()]


def available_sources(news: List[Dict]) -> List[str]:
    """返回资讯中出现过的去重来源名称（排序），供 UI 构造来源过滤选项。"""
    srcs = {n.get("source", "") for n in news if n.get("source")}
    return sorted(srcs)


if __name__ == "__main__":
    news, msg = get_news()
    print(msg)
    for n in news[:5]:
        print(f"[{n['source']}] {n['title']} -> {classify_sentiment(n['title'])}")
