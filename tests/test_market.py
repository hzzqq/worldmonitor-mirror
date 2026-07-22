"""worldmonitor market 单元测试（无 akshare/网络依赖，纯 Python 可跑）。

通过 monkeypatch builtins.__import__ 让 akshare 不可导入，强制走 mock 兜底分支。

运行：pytest worldmonitor-mirror/tests
"""

from __future__ import annotations

import builtins
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import market  # noqa: E402


@pytest.fixture
def no_akshare(monkeypatch):
    """让 market 内部的 `import akshare` 抛 ImportError，强制走 mock 兜底。"""
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "akshare" or name.startswith("akshare."):
            raise ImportError("no akshare in test env")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    market.clear_market_cache()


def test_get_stock_quote_empty():
    market.clear_market_cache()
    data, note = market.get_stock_quote("")
    assert data == {}
    assert "未提供" in note


def test_get_stock_quote_mock_deterministic(no_akshare):
    a, note_a = market.get_stock_quote("600000")
    b, _ = market.get_stock_quote("600000")
    # 同一代码多次调用结果一致（确定性、可复现）
    assert a == b
    assert a["代码"] == "600000"
    for k in ("最新价", "涨跌幅", "涨跌额", "成交量", "成交额", "今开", "昨收", "最高", "最低"):
        assert k in a
    assert "离线" in note_a or "回退" in note_a


def test_get_indices_mock_structure(no_akshare):
    indices, note = market.get_indices()
    assert isinstance(indices, list) and len(indices) >= 1
    for it in indices:
        for k in ("名称", "代码", "最新价", "涨跌幅"):
            assert k in it
    assert "回退" in note or "离线" in note


def test_indices_cache_avoids_refetch(no_akshare, monkeypatch):
    calls = {"n": 0}
    orig = market._get_via_akshare

    def spy():
        calls["n"] += 1
        return orig()

    monkeypatch.setattr(market, "_get_via_akshare", spy)
    market.clear_market_cache()
    market.get_indices()
    market.get_indices()
    # 第二次应命中 TTL 缓存，不再真正抓取
    assert calls["n"] == 1


def test_market_overview_structure(no_akshare):
    ov = market.get_market_overview()
    assert "indices" in ov and "up" in ov and "down" in ov and "flat" in ov
    assert ov["up"] + ov["down"] + ov["flat"] == len(ov["indices"])
    assert "source" in ov


def test_get_indices_sorted_descending(no_akshare):
    """R2 隐性展示优化：指数应按涨跌幅降序（领涨在前）。"""
    market.clear_market_cache()
    indices, _ = market.get_indices()
    pcts = [i["涨跌幅"] for i in indices]
    assert pcts == sorted(pcts, reverse=True)


def test_get_indices_limit(no_akshare):
    """R1 新需求对齐：get_indices 支持 limit 截娶前 N 条。"""
    market.clear_market_cache()
    limited, _ = market.get_indices(limit=2)
    assert len(limited) == 2
    full, _ = market.get_indices(limit=None)
    assert len(full) >= 2


def test_get_stock_quotes_batch(no_akshare):
    """R1 新需求：批量行情查询，返回 {symbol: (quote, source)}。"""
    market.clear_market_cache()
    out = market.get_stock_quotes(["600000", "000001"])
    assert set(out.keys()) == {"600000", "000001"}
    for sym, (quote, note) in out.items():
        assert quote["代码"] == sym
        assert isinstance(note, str)


def test_get_indices_cache_hit_still_sorted(no_akshare):
    """R2 修复验证：缓存命中后仍按涨跌幅降序（领涨在前）。

    关键：不能在两次调用间 clear_market_cache——否则只会重算、永远走
    不到「缓存命中返回未排序数据」的路径。这里首次填充缓存后，
    直接第二次调用命中缓存；修复前缓存里存的是抓取原始顺序（未排序），
    命中会返回乱序；修复后每次命中也重排。
    """
    market.clear_market_cache()
    first, _ = market.get_indices()            # 首次：填充缓存（存的是未排序原始顺序）
    assert [i["涨跌幅"] for i in first] == sorted(
        [i["涨跌幅"] for i in first], reverse=True)
    second, _ = market.get_indices()           # 命中缓存（不清缓存）
    pcts = [i["涨跌幅"] for i in second]
    assert pcts == sorted(pcts, reverse=True)     # 命中缓存也保持排序


def test_get_top_movers(no_akshare):
    """R1 新需求验证：get_top_movers 返回领涨/领跌榜。"""
    market.clear_market_cache()
    indices, _ = market.get_indices()
    movers = market.get_top_movers(indices, top_n=2)
    gainers = movers["gainers"]
    losers = movers["losers"]
    assert gainers[0]["涨跌幅"] >= gainers[1]["涨跌幅"]      # 领涨降序
    assert losers[0]["涨跌幅"] <= losers[1]["涨跌幅"]      # 领跌升序
    # 领涨第一 不得出现在领跌里（无交集）
    g0 = gainers[0]["代码"]
    assert all(l["代码"] != g0 for l in losers)
