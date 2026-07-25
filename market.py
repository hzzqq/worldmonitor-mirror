"""A 股市场模块（akshare 优先 + mock 兜底）。

主接口：get_indices() -> (list[dict], str)
  返回 (指数概览列表, 数据来源说明)
  每条指数结构：
    {
      "名称": str,        # 如 "上证指数"
      "代码": str,        # 如 "sh000001"
      "最新价": float,
      "涨跌幅": float,     # 百分比，正为涨
      "涨跌额": float,
      "成交量": float,     # 手
      "成交额": float,     # 元
    }

离线兜底：akshare 未安装或抓取失败时，返回内置 mock 指数数据，app 不报错。
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import logging
import os
import time
from typing import Dict, List, Tuple

from log_utils import setup_logging

log = logging.getLogger("worldmonitor")


# 常见 A 股指数（用于 mock 与 akshare 过滤）
TARGET_INDICES = {
    "上证指数": "sh000001",
    "深证成指": "sz399001",
    "创业板指": "sz399006",
    "沪深300": "sh000300",
    "科创50": "sh000688",
}


# ---------------------------------------------------------------------------
# 轻量 TTL 缓存（隐式性能悬崖修复：避免每次刷新都直连 akshare）
# ---------------------------------------------------------------------------
_CACHE_TTL = float(os.getenv("MARKET_CACHE_TTL", "30"))
_MARKET_CACHE: Dict[str, Tuple[object, float]] = {}


def _cache_get(key: str):
    item = _MARKET_CACHE.get(key)
    if item and (time.time() - item[1]) < _CACHE_TTL:
        return item[0]
    return None


def _cache_set(key: str, value) -> None:
    _MARKET_CACHE[key] = (value, time.time())


def clear_market_cache() -> None:
    """清空行情缓存（便于测试与手动刷新）。"""
    _MARKET_CACHE.clear()


def clear_all_caches() -> None:
    """一键清空行情与资讯的全部内部缓存（刷新场景的统一入口）。

    R1 新能力：此前 app 刷新需分别调用 market.clear_market_cache() 与
    data_feed.clear_news_cache()，新增缓存类型时容易漏清；现收敛为单一入口，
    刷新只要调用它即可保证所有缓存被清空。延迟导入 data_feed 避免模块级
    循环依赖（data_feed 不依赖 market，此处仅函数内导入保持解耦）。
    """
    clear_market_cache()
    from data_feed import clear_news_cache

    clear_news_cache()


def _mock_indices() -> List[Dict]:
    """内置示例指数数据（离线兜底）。"""
    base = {
        "上证指数": (3120.45, 0.62),
        "深证成指": (9876.12, -0.34),
        "创业板指": (1987.63, 1.18),
        "沪深300": (3654.21, 0.41),
        "科创50": (876.54, -0.92),
    }
    out = []
    for name, code in TARGET_INDICES.items():
        price, pct = base.get(name, (1000.0, 0.0))
        out.append({
            "名称": name,
            "代码": code,
            "最新价": round(price, 2),
            "涨跌幅": round(pct, 2),
            "涨跌额": round(price * pct / 100, 2),
            "成交量": 1_000_000 + int(abs(pct) * 1_000_000),
            "成交额": round(price * (8_000_000 + abs(pct) * 5_000_000), 2),
        })
    return out


def _get_via_akshare() -> List[Dict]:
    """通过 akshare 获取东方财富指数实时行情。"""
    import akshare as ak  # 延迟导入，缺失时不致命
    df = ak.stock_zh_index_spot_em()
    # 列名可能为：代码/名称/最新价/涨跌幅/涨跌额/成交量/成交额 ...
    wanted = set(TARGET_INDICES.values())
    out: List[Dict] = []
    for _, row in df.iterrows():
        code = str(row.get("代码", ""))
        if code in wanted:
            out.append({
                "名称": str(row.get("名称", "")),
                "代码": code,
                "最新价": float(row.get("最新价", 0) or 0),
                "涨跌幅": float(row.get("涨跌幅", 0) or 0),
                "涨跌额": float(row.get("涨跌额", 0) or 0),
                "成交量": float(row.get("成交量", 0) or 0),
                "成交额": float(row.get("成交额", 0) or 0),
            })
    if not out:  # 抓到了但没匹配上，退回全部前若干条
        for _, row in df.head(5).iterrows():
            out.append({
                "名称": str(row.get("名称", "")),
                "代码": str(row.get("代码", "")),
                "最新价": float(row.get("最新价", 0) or 0),
                "涨跌幅": float(row.get("涨跌幅", 0) or 0),
                "涨跌额": float(row.get("涨跌额", 0) or 0),
                "成交量": float(row.get("成交量", 0) or 0),
                "成交额": float(row.get("成交额", 0) or 0),
            })
    return out


def get_indices(limit: "int | None" = None) -> Tuple[List[Dict], str]:
    """获取 A 股主要指数概览，失败回退 mock。

    返回 (指数列表, 数据来源说明)。列表按涨跌幅降序排列（领涨在前），
    limit 可截取前 N 条（None 表示不限，与 get_news 对齐）。
    """
    cached = _cache_get("indices")
    if cached is not None:
        data, note = cached
        # R2 修复：缓存里存的是「抓取时的原始顺序」，若直接返回会丢失排序；
        # 这里在每次命中时也按涨跌幅降序重排，保证缓存命中后依旧「领涨在前」。
        data = sorted(data, key=lambda x: x.get("涨跌幅", 0), reverse=True)
        if limit is None or limit < 0:
            return data, note
        return data[:limit], note
    try:
        log.info("开始获取 A 股指数行情(akshare)")
        data = _get_via_akshare()
        if data:
            result = (data, "数据来源：akshare（东方财富实时行情）")
            _cache_set("indices", result)
            log.info("A 股指数行情获取完成 条数=%d", len(data))
            data = sorted(data, key=lambda x: x.get("涨跌幅", 0), reverse=True)
            if limit is not None and limit >= 0:
                data = data[:limit]
            return data, result[1]
    except Exception as exc:
        result = _mock_indices(), (
            f"⚠️ akshare 抓取失败（{type(exc).__name__}），已回退内置示例数据（离线模式）"
        )
        log.warning("akshare 指数行情抓取失败：%s", exc)
        _cache_set("indices", result)
        data = sorted(result[0], key=lambda x: x.get("涨跌幅", 0), reverse=True)
        if limit is not None and limit >= 0:
            data = data[:limit]
        return data, result[1]
    raw = _mock_indices()
    result = raw, "⚠️ akshare 未返回有效数据，已回退内置示例数据（离线模式）"
    _cache_set("indices", result)
    data = sorted(raw, key=lambda x: x.get("涨跌幅", 0), reverse=True)
    if limit is not None and limit >= 0:
        data = data[:limit]
    return data, result[1]


def get_market_overview(indices: "List[Dict] | None" = None) -> Dict:
    """聚合概览：指数列表 + 涨跌平统计 + 数据来源，便于看板头部展示。

    返回结构：
      {"indices": [...], "up": int, "down": int, "flat": int, "source": str}

    R2 一致性修复（隐性双数据来源缺陷）：原实现每次都重新调用 get_indices()，
    与看板已加载并展示的 `indices` 来自两条独立取数链路——当 `load_indices`
    （Streamlit 缓存 600s）与 market 内部缓存（默认 30s）刷新时机不一致时，
    头部「上涨/下跌/平盘」指标可能与下方指数表对不上。现允许调用方传入已加载的
    `indices`，使概览与表格始终基于同一份数据；不传时仍自行拉取（离线/mock 兜底）。
    """
    source = ""
    if indices is None:
        indices, source = get_indices()
    up = sum(1 for i in indices if i.get("涨跌幅", 0) > 0)
    down = sum(1 for i in indices if i.get("涨跌幅", 0) < 0)
    flat = len(indices) - up - down
    return {"indices": indices, "up": up, "down": down, "flat": flat, "source": source}


def format_change_pct(pct: "float | None") -> str:
    """统一涨跌幅展示格式：正数带 +、负数带 -、None 兜底为 '-'。

    R1 新能力：看板多处展示涨跌幅（指标卡 / 领涨领跌 / 个股），此前各自内联
    f'{pct:+}%' 或 '0' 兜底，口径不一致；抽出纯函数保证全站统一且可单测。
    """
    if pct is None:
        return "-"
    s = f"{pct:+}"  # 保留原始精度：+1.2 / -0.34 / +0 / +2.0
    # 整值（如 0、2.0）补 .0，保证与带小数展示口径一致（避免 +0% 这类不一致）
    if "." not in s and "e" not in s and "E" not in s:
        s += ".0"
    return s + "%"


def get_top_movers(indices: "List[Dict] | None" = None, top_n: int = 3) -> Dict:
    """涨幅 / 跌幅榜（看板「领涨 / 领跌」面板）。

    R1 新能力：从指数（或传入的行情列表）中分别取涨跌幅最高与最低的
    top_n 条，便于一眼看出当日强弱方向。
    - gainers：涨跌幅 > 0 的项，按降序取前 top_n（领涨）
    - losers：涨跌幅 < 0 的项，按升序（最跌优先）取前 top_n（领跌）
    - flat：涨跌幅恰为 0 的项
    indices 为 None 时自动拉取 get_indices()（带缓存、离线走 mock）。

    R2 修复（隐性正确性缺陷）：原实现 gainers=ranked[:top_n]、
    losers=reversed(ranked[-top_n:])，当指数总数不多时两个切片会重叠——
    涨跌幅恰好为 0 / 接近 0 的「中间项」既出现在领涨榜又出现在领跌榜，
    看板出现同一条指数同时标红又标绿的矛盾。现改为按「正 / 负」严格分组，
    两组天然互斥（正数与负数集合不相交），flat 单独列出，杜绝重叠。
    """
    if indices is None:
        indices, _ = get_indices()
    ranked = sorted(indices, key=lambda x: x.get("涨跌幅", 0), reverse=True)
    pos = [i for i in ranked if i.get("涨跌幅", 0) > 0]   # 已降序
    neg = [i for i in ranked if i.get("涨跌幅", 0) < 0]   # 已降序，反转后最跌在前
    return {
        "gainers": pos[:top_n],
        "losers": list(reversed(neg))[:top_n],
        "flat": [i for i in ranked if i.get("涨跌幅", 0) == 0],
    }


def summarize_market(indices: "List[Dict] | None" = None) -> Dict:
    """一站式市场概览：涨跌平统计 + 领涨/领跌榜，供看板头部一次性取数。

    R1 新能力：看板此前分别调用 get_market_overview() 与 get_top_movers()，
    逻辑分散且两次各自可能取数；这里合并为单入口，复用同一份 indices，
    既减少调用面也保证「概览指标」与「涨跌榜」永远基于同一快照（R2 一致性）。
    """
    if indices is None:
        indices, _ = get_indices()
    ov = get_market_overview(indices)
    movers = get_top_movers(indices)
    return {
        "up": ov["up"],
        "down": ov["down"],
        "flat": ov["flat"],
        "source": ov["source"],
        "gainers": movers["gainers"],
        "losers": movers["losers"],
        "flat_list": movers["flat"],
    }


# ---------------------------------------------------------------------------
# 行情机读导出（与 data_feed 的 export_news_csv/json 对称，便于流水线消费）
# ---------------------------------------------------------------------------
_MARKET_FIELDS = ["名称", "代码", "最新价", "涨跌幅", "涨跌额", "成交量", "成交额"]


def export_market_csv(indices: List[Dict]) -> str:
    """把指数行情列表导出为 CSV 文本（表头 + 每行一条），供下游脚本 / 管线消费。

    R1 新能力：此前指数行情只能在看板手动下载（依赖 pandas），缺少一个
    不依赖 Streamlit / 前端、可单测的纯函数导出入口；与 data_feed 的
    export_news_csv 对称，统一「资讯 / 行情」两类数据源的导出能力。
    用 csv 模块引用包裹，避免标题 / 数值中含逗号或换行时破坏结构。
    """
    import csv
    import io

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_MARKET_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for it in indices:
        row = {k: it.get(k, "") for k in _MARKET_FIELDS}
        writer.writerow(row)
    return buf.getvalue()


def export_market_json(indices: List[Dict]) -> str:
    """把指数行情列表导出为 JSON 文本（机读，便于程序化消费 / 迁移）。

    R1 新能力：与 export_market_csv 互补——CSV 利于表格工具，JSON 利于
    程序消费。对所有字段做 `float` 规整（None / 非数回退 0.0），保证
    下游 json.loads 稳定、不出现 null 干扰统计。
    """
    import json as _json

    def _num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    out = []
    for it in indices:
        out.append({
            "名称": it.get("名称", ""),
            "代码": it.get("代码", ""),
            "最新价": _num(it.get("最新价")),
            "涨跌幅": _num(it.get("涨跌幅")),
            "涨跌额": _num(it.get("涨跌额")),
            "成交量": _num(it.get("成交量")),
            "成交额": _num(it.get("成交额")),
        })
    return _json.dumps(out, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# 单只股票行情（akshare 优先 + mock 兜底）
# ---------------------------------------------------------------------------
def _mock_stock(symbol: str) -> Dict:
    """内置示例个股行情（离线兜底）。

    用 symbol 派生种子，保证同一代码多次调用结果一致（可复现、可测试）。
    """
    import random

    # R2 修复（隐性可复现性缺陷）：原实现用内置 hash(symbol) 派生随机种子，
    # 但 CPython 对 str 的 hash 受 PYTHONHASHSEED 影响、每次进程启动都不同，
    # 导致「离线 mock 个股行情」在不同运行间结果不一致，破坏可复现性与测试稳定性。
    # 改用 md5 派生确定性种子，保证同一代码在任何进程/环境下都得到相同 mock。
    seed = int(hashlib.md5(symbol.encode("utf-8")).hexdigest(), 16) % (2**32)
    rnd = random.Random(seed)
    base = 100.0
    price = round(base + rnd.uniform(-10, 20), 2)
    pct = round(rnd.uniform(-5, 5), 2)
    return {
        "名称": symbol,
        "代码": symbol,
        "最新价": price,
        "涨跌幅": pct,
        "涨跌额": round(price * pct / 100, 2),
        "成交量": int(rnd.uniform(1e5, 5e6)),
        "成交额": round(rnd.uniform(1e8, 5e9), 2),
        "今开": round(price * (1 - rnd.uniform(0, 0.02)), 2),
        "昨收": round(price * (1 - pct / 100), 2),
        "最高": round(price * (1 + rnd.uniform(0, 0.03)), 2),
        "最低": round(price * (1 - rnd.uniform(0, 0.03)), 2),
    }


def get_stock_quote(symbol: str) -> Tuple[Dict, str]:
    """单只股票行情，akshare 优先，失败回退 mock。

    返回 (个股 dict, 数据来源说明)。symbol 形如 600000 或 sh600000。
    """
    symbol = (symbol or "").strip()
    if not symbol:
        return {}, "未提供股票代码/名称"
    cache_key = f"stock:{symbol}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    try:
        import akshare as ak

        # 优先用沪 A 实时spot，再退化到全市场筛选
        df = ak.stock_zh_a_spot_em()
        code = symbol if symbol.lower().startswith(("sh", "sz", "bj")) else f"sh{symbol}"
        row = df[df["代码"] == code]
        if row.empty and not symbol.lower().startswith(("sh", "sz", "bj")):
            row = df[df["代码"].str.endswith(symbol)]
        if not row.empty:
            r = row.iloc[0]
            result = {
                "名称": str(r.get("名称", symbol)),
                "代码": str(r.get("代码", code)),
                "最新价": float(r.get("最新价", 0) or 0),
                "涨跌幅": float(r.get("涨跌幅", 0) or 0),
                "涨跌额": float(r.get("涨跌额", 0) or 0),
                "成交量": float(r.get("成交量", 0) or 0),
                "成交额": float(r.get("成交额", 0) or 0),
                "今开": float(r.get("今开", 0) or 0),
                "昨收": float(r.get("昨收", 0) or 0),
                "最高": float(r.get("最高", 0) or 0),
                "最低": float(r.get("最低", 0) or 0),
            }, "数据来源：akshare（东方财富实时行情）"
            _cache_set(cache_key, result)
            return result
    except Exception as exc:
        result = _mock_stock(symbol), (
            f"⚠️ 个股行情抓取失败（{type(exc).__name__}），已回退内置示例数据（离线模式）：{symbol}"
        )
        _cache_set(cache_key, result)
        return result
    result = _mock_stock(symbol), f"⚠️ 未匹配到个股，已回退内置示例数据（离线模式）：{symbol}"
    _cache_set(cache_key, result)
    return result


def get_stock_quotes(symbols: List[str]) -> Dict[str, Tuple[Dict, str]]:
    """批量查询多只股票行情（自选/看板场景）。

    返回 {symbol: (个股 dict, 数据来源说明)}，逐只复用 get_stock_quote 的缓存与兜底。
    """
    out: Dict[str, Tuple[Dict, str]] = {}
    for sym in symbols:
        out[sym] = get_stock_quote(sym)
    return out


def get_index_quote(query: str):
    """按名称或代码查询单条指数行情（看板「单指数」小组件用，R1 新能力）。

    优先精确匹配 名称 / 代码；未命中返回 None。
    底层复用 get_indices()（带缓存、离线走 mock），不额外打上游。
    """
    query = (query or "").strip()
    if not query:
        return None
    indices, _ = get_indices()
    for it in indices:
        if it.get("名称") == query or it.get("代码") == query:
            return it
    return None


def search_indices(query: str) -> "list[dict]":
    """按名称或代码模糊检索指数行情（看板「指数搜索/联想」入口，R1 新能力）。

    与 get_index_quote（精确单条）互补：返回所有 名称/代码 包含 query 的
    指数（大小写不敏感），便于前端做下拉联想与多匹配展示。空查询返回全部。
    复用 get_indices()（带缓存、离线走 mock），不额外打上游。
    """
    query = (query or "").strip()
    indices, _ = get_indices()
    if not query:
        return list(indices)
    q = query.lower()
    return [
        it for it in indices
        if q in str(it.get("名称", "")).lower()
        or q in str(it.get("代码", "")).lower()
    ]


if __name__ == "__main__":
    idx, msg = get_indices()
    print(msg)
    for it in idx:
        print(f"{it['名称']}: {it['最新价']} ({it['涨跌幅']:+}%)")
