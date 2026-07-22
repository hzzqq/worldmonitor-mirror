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
import os
import time
from typing import Dict, List, Tuple


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


def get_indices() -> Tuple[List[Dict], str]:
    """获取 A 股主要指数概览，失败回退 mock。

    返回 (指数列表, 数据来源说明)。
    """
    cached = _cache_get("indices")
    if cached is not None:
        return cached
    try:
        data = _get_via_akshare()
        if data:
            result = (data, "数据来源：akshare（东方财富实时行情）")
            _cache_set("indices", result)
            return result
    except Exception as exc:
        result = _mock_indices(), (
            f"⚠️ akshare 抓取失败（{type(exc).__name__}），已回退内置示例数据（离线模式）"
        )
        _cache_set("indices", result)
        return result
    result = _mock_indices(), "⚠️ akshare 未返回有效数据，已回退内置示例数据（离线模式）"
    _cache_set("indices", result)
    return result


def get_market_overview() -> Dict:
    """聚合概览：指数列表 + 涨跌平统计 + 数据来源，便于看板头部展示。

    返回结构：
      {"indices": [...], "up": int, "down": int, "flat": int, "source": str}
    """
    indices, source = get_indices()
    up = sum(1 for i in indices if i.get("涨跌幅", 0) > 0)
    down = sum(1 for i in indices if i.get("涨跌幅", 0) < 0)
    flat = len(indices) - up - down
    return {"indices": indices, "up": up, "down": down, "flat": flat, "source": source}


# ---------------------------------------------------------------------------
# 单只股票行情（akshare 优先 + mock 兜底）
# ---------------------------------------------------------------------------
def _mock_stock(symbol: str) -> Dict:
    """内置示例个股行情（离线兜底）。

    用 symbol 派生种子，保证同一代码多次调用结果一致（可复现、可测试）。
    """
    import random

    rnd = random.Random(abs(hash(symbol)) % (2**32))
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


if __name__ == "__main__":
    idx, msg = get_indices()
    print(msg)
    for it in idx:
        print(f"{it['名称']}: {it['最新价']} ({it['涨跌幅']:+}%)")
