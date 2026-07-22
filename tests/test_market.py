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
