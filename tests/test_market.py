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
import data_feed  # noqa: E402


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


def test_mock_stock_not_dependent_on_builtin_hash(no_akshare, monkeypatch):
    """R2 隐性可复现性验证：即便内置 hash() 抖动（模拟不同进程/PYTHONHASHSEED），
    mock 个股行情仍应稳定一致（修复前用 hash(symbol) 会随进程变化）。"""
    counter = {"n": 0}
    orig_hash = builtins.hash

    def jitter_hash(obj):
        counter["n"] += 1
        return orig_hash((obj, counter["n"]))  # 每次调用结果都不同

    monkeypatch.setattr(builtins, "hash", jitter_hash)
    a, _ = market.get_stock_quote("600000")
    b, _ = market.get_stock_quote("600000")
    assert a == b  # 即便内置 hash 抖动，mock 行情仍应稳定


def test_get_index_quote_by_name_and_code(no_akshare):
    """R1 新需求验证：get_index_quote 按名称或代码查到单条指数行情。"""
    by_name = market.get_index_quote("上证指数")
    assert by_name is not None
    assert by_name["名称"] == "上证指数"
    by_code = market.get_index_quote("sh000001")
    assert by_code is not None
    assert by_code["代码"] == "sh000001"
    assert market.get_index_quote("") is None
    assert market.get_index_quote("不存在的指数xyz") is None


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


def test_search_indices_fuzzy_match(no_akshare):
    """R1 新能力验证：search_indices 按名称/代码模糊匹配（大小写不敏感）。"""
    market.clear_market_cache()
    res = market.search_indices("上证")
    assert any(it["名称"] == "上证指数" for it in res)
    # 代码子串匹配（如 000001 命中 sh000001）
    res2 = market.search_indices("000001")
    assert any(it["代码"] == "sh000001" for it in res2)
    # 大小写不敏感
    res3 = market.search_indices("SH")
    assert len(res3) >= 1


def test_search_indices_empty_returns_all(no_akshare):
    """search_indices 空查询返回全部指数（与 get_indices 数量一致）。"""
    market.clear_market_cache()
    all_idx, _ = market.get_indices()
    assert len(market.search_indices("")) == len(all_idx)


def test_search_indices_no_match_returns_empty(no_akshare):
    """search_indices 无匹配时返回空列表（不抛异常）。"""
    market.clear_market_cache()
    assert market.search_indices("不存在的指数zzz") == []


def test_market_overview_accepts_preloaded_indices(no_akshare):
    """R2 一致性修复验证：传入已加载 indices 后，概览 up/down/flat 与
    该份 indices 严格对应，不再另起取数链路（消除头部指标与表格不一致）。"""
    market.clear_market_cache()
    indices, _ = market.get_indices()
    ov = market.get_market_overview(indices)
    up = sum(1 for i in indices if i.get("涨跌幅", 0) > 0)
    down = sum(1 for i in indices if i.get("涨跌幅", 0) < 0)
    assert ov["up"] == up
    assert ov["down"] == down
    assert ov["flat"] == len(indices) - up - down
    # 传入 indices 时不应再触发网络/mock 取数（source 由调用方提供，可为空串）
    assert ov["indices"] is indices


def test_market_overview_none_fetches_self(no_akshare):
    """不传 indices 时仍自行取数（离线/mock 兜底），结构完整。"""
    market.clear_market_cache()
    ov = market.get_market_overview()
    assert "indices" in ov and "source" in ov
    assert ov["up"] + ov["down"] + ov["flat"] == len(ov["indices"])


def test_format_change_pct():
    """R2 精度一致性：固定 1 位小数（正带+ / 负带- / None 兜底 '-'），
    此前 0 与 2.0 会丢失小数位（'+0%' / '+2%'），与 1.2 的 '+1.2%' 口径不一致。"""
    assert market.format_change_pct(1.2) == "+1.2%"
    assert market.format_change_pct(-0.34) == "-0.34%"
    assert market.format_change_pct(0) == "+0.0%"
    assert market.format_change_pct(2.0) == "+2.0%"
    assert market.format_change_pct(None) == "-"


def test_clear_all_caches(no_akshare):
    """R1 新能力验证：clear_all_caches 一次性清空行情与资讯两类内部缓存。"""
    market.clear_market_cache()
    data_feed.clear_news_cache()
    market.get_indices()  # 填行情缓存
    data_feed.get_news()  # 填资讯缓存
    assert market._MARKET_CACHE.get("indices") is not None
    assert data_feed._NEWS_CACHE  # 非空
    market.clear_all_caches()
    assert market._MARKET_CACHE.get("indices") is None
    assert not data_feed._NEWS_CACHE  # 资讯缓存一并清空


def test_summarize_market_one_call(no_akshare):
    """R1 新能力验证：summarize_market 一次取数同时给出概览 + 涨跌榜，
    且概览与榜单基于同一份 indices（同源一致）。"""
    market.clear_market_cache()
    indices, _ = market.get_indices()
    s = market.summarize_market(indices)
    for k in ("up", "down", "flat", "source", "gainers", "losers", "flat_list"):
        assert k in s
    assert s["up"] + s["down"] + s["flat"] == len(indices)
    if s["gainers"]:
        assert s["gainers"][0]["涨跌幅"] >= 0  # 领涨在前
    # 不传 indices 也能工作（自行取数）
    s2 = market.summarize_market()
    assert "gainers" in s2 and "up" in s2



def test_get_top_movers_gainers_losers_disjoint(no_akshare):
    """R2 验证：领涨榜与领跌榜严格按正/负分组，互不相交（中间项不重复出现）。"""
    market.clear_market_cache()
    indices, _ = market.get_indices()
    movers = market.get_top_movers(indices, top_n=3)
    gain_codes = {i["代码"] for i in movers["gainers"]}
    loss_codes = {i["代码"] for i in movers["losers"]}
    # 两组代码集合不应相交
    assert gain_codes.isdisjoint(loss_codes)
    # 领涨项涨跌幅必须 > 0，领跌项必须 < 0
    for g in movers["gainers"]:
        assert g["涨跌幅"] > 0
    for l in movers["losers"]:
        assert l["涨跌幅"] < 0
    # 平盘项涨跌幅恰为 0
    for f in movers["flat"]:
        assert f["涨跌幅"] == 0


def test_export_market_csv_and_json(no_akshare):
    """R1 验证：export_market_csv / export_market_json 产出结构正确、可解析。"""
    market.clear_market_cache()
    indices, _ = market.get_indices()
    csv_text = market.export_market_csv(indices)
    lines = csv_text.strip().splitlines()
    assert lines[0] == "名称,代码,最新价,涨跌幅,涨跌额,成交量,成交额"
    assert len(lines) == len(indices) + 1  # 表头 + 每行一条

    json_text = market.export_market_json(indices)
    data = __import__("json").loads(json_text)
    assert isinstance(data, list) and len(data) == len(indices)
    # 数值字段被规整为 float（无 null），便于下游消费
    for row in data:
        assert isinstance(row["最新价"], float)
        assert isinstance(row["涨跌幅"], float)


def test_match_stock_three_tier_and_safe_float():
    """R2 修复（c166）：带前缀代码/名称输入的三级匹配（修复前必落空回退
    mock 假行情）+ NaN/脏值防护。"""
    import pandas as pd

    df = pd.DataFrame({
        "代码": ["600000", "000001", "600519"],
        "名称": ["浦发银行", "平安银行", "贵州茅台"],
        "最新价": [10.0, float("nan"), "-"],
    })
    assert market.match_stock(df, "600000")["名称"] == "浦发银行"      # 纯代码
    assert market.match_stock(df, "sz000001")["名称"] == "平安银行"    # 带前缀
    assert market.match_stock(df, "SH600519")["名称"] == "贵州茅台"    # 大写前缀
    assert market.match_stock(df, "浦发银行")["代码"] == "600000"      # 名称精确
    assert market.match_stock(df, "贵州")["代码"] == "600519"          # 唯一前缀
    assert market.match_stock(df, "银行") is None                      # 歧义前缀 -> None
    assert market.match_stock(df, "999999") is None
    assert market.match_stock(df, "") is None
    assert market._safe_float(float("nan")) == 0.0
    assert market._safe_float("-") == 0.0
    assert market._safe_float(None) == 0.0
    assert market._safe_float("3.5") == 3.5


def _fake_spot_df():
    import pandas as pd
    return pd.DataFrame({
        "代码": ["600000", "000001"],
        "名称": ["浦发银行", "平安银行"],
        "最新价": [10.0, 11.0],
        "涨跌幅": [1.5, -0.5],
        "涨跌额": [0.15, -0.06],
        "成交量": [100.0, 200.0],
        "成交额": [1e8, 2e8],
        "今开": [9.9, 11.1],
        "昨收": [9.85, 11.06],
        "最高": [10.2, 11.2],
        "最低": [9.8, 10.9],
    })


def test_stock_cache_returns_copy_and_normalized_key(monkeypatch):
    """R2/R1（c168）：缓存命中返回副本（原地修改不污染）；缓存键去前缀归一
    （600000 / sh600000 / SZ600000 共享，同股不再重复打上游）。"""
    import pandas as pd
    calls = {"n": 0}

    def fake_fetch(retries=1):
        calls["n"] += 1
        return _fake_spot_df(), None

    monkeypatch.setattr(market, "_fetch_spot_df", fake_fetch)
    market.clear_all_caches()
    q1, src1 = market.get_stock_quote("600000")
    assert "akshare" in src1 and calls["n"] == 1
    q1["最新价"] = 999.0                      # 调用方原地修改
    q2, _ = market.get_stock_quote("sh600000")   # 归一键命中缓存
    assert q2["最新价"] == 10.0, "缓存命中必须返回副本"
    q3, _ = market.get_stock_quote("SZ600000")
    assert q3["最新价"] == 10.0 and calls["n"] == 1, "不同写法共享缓存、不重复抓取"
    market.clear_all_caches()


def test_get_stock_quotes_single_fetch(monkeypatch):
    """R2 性能修复（c168）：批量查询只抓一次全市场表（原先 N 只=N 次全量 HTTP）。"""
    calls = {"n": 0}

    def fake_fetch(retries=1):
        calls["n"] += 1
        return _fake_spot_df(), None

    monkeypatch.setattr(market, "_fetch_spot_df", fake_fetch)
    market.clear_all_caches()
    out = market.get_stock_quotes(["600000", "000001", "sh600000"])
    assert calls["n"] == 1, "3 只股票（含 1 只缓存等价写法）只应抓取一次"
    assert out["600000"][0]["名称"] == "浦发银行"
    assert out["000001"][0]["名称"] == "平安银行"
    assert out["sh600000"][0]["代码"] == "600000"
    market.clear_all_caches()


def test_stock_quote_retries_on_transient(monkeypatch):
    """R1 韧性（c168）：spot 接口偶发断连时重试一次（c167 集成冒烟实测上游不稳）。
    patch akshare 层（而非 _fetch_spot_df），以覆盖其内部重试循环。"""
    import akshare

    calls = {"n": 0}

    def flaky_spot():
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("Remote end closed connection without response")
        return _fake_spot_df()

    monkeypatch.setattr(akshare, "stock_zh_a_spot_em", flaky_spot)
    market.clear_all_caches()
    q, src = market.get_stock_quote("600000")
    assert calls["n"] == 2 and "akshare" in src and q["代码"] == "600000"
    market.clear_all_caches()


def test_stock_quote_falls_back_after_retries_exhausted(monkeypatch):
    """重试耗尽仍失败 -> 明确的「抓取失败」mock 兜底（降级路径保持可用）。"""

    def broken_fetch(retries=1):
        raise ConnectionError("down")

    monkeypatch.setattr(market, "_fetch_spot_df", broken_fetch)
    market.clear_all_caches()
    q, src = market.get_stock_quote("600000")
    assert "抓取失败" in src and "ConnectionError" in src
    market.clear_all_caches()
