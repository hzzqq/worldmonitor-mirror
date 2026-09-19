"""worldmonitor-mirror：实时情报 / 市场监控看板（Streamlit MVP）。

功能：
  - 多源资讯聚合（Hacker News API + RSS），离线回退内置示例
  - Plotly 图表：发布量时间线 / 来源分布 / 情绪占比与趋势
  - 侧边栏筛选：关键词 / 来源 / 时间范围
  - A 股市场模块（akshare 优先 + mock 兜底）
  - 全部网络抓取 try/except，app 永不崩溃

运行：streamlit run app.py
"""

from __future__ import annotations

import datetime as _dt
from collections import Counter

import pandas as pd
import plotly.express as px
import streamlit as st

import data_feed
import market
from app_helpers import build_dataframe, highlight_keyword, trending_chart_data, hourly_chart_data, search_and_paginate


# ---------------------------------------------------------------------------
# 页面配置
# ---------------------------------------------------------------------------
st.set_page_config(page_title="worldmonitor 情报看板", page_icon="📡", layout="wide")

REFRESH_TTL = 600  # 秒：资讯缓存 10 分钟


@st.cache_data(ttl=REFRESH_TTL)
def load_news() -> tuple[list[dict], str]:
    return data_feed.get_news()


@st.cache_data(ttl=REFRESH_TTL)
def load_indices() -> tuple[list[dict], str]:
    return market.get_indices()


def main() -> None:
    st.title("📡 worldmonitor 实时情报看板")
    st.caption("多源科技/资讯聚合 + 可选 A 股市场模块 · 离线可用")

    # ---- 侧边栏筛选 ----
    st.sidebar.header("🔧 筛选")
    keyword = st.sidebar.text_input("关键词（标题/摘要匹配）", value="")
    time_range = st.sidebar.slider(
        "时间范围（最近 N 小时）", min_value=1, max_value=72, value=72, step=1,
    )
    sort_order = st.sidebar.selectbox("排序方式", ["最新优先", "最旧优先"], index=0)
    force_refresh = st.sidebar.button("🔄 重新抓取")

    # ---- 抓取数据（带缓存；点刷新则清空对应缓存后重新拉取） ----
    if force_refresh:
        load_news.clear()
        load_indices.clear()
        market.clear_all_caches()  # 清空行情+资讯全部内部缓存，刷新才真正生效
    with st.spinner("正在聚合资讯..."):
        news_items, news_note = load_news()

    st.sidebar.info(news_note)
    st.sidebar.caption(f"本地展示时间：{_dt.datetime.now():%Y-%m-%d %H:%M:%S}")

    # ---- 数据健康态（R1 监控可观测性：哪些源失败 / 是否离线 / 上次抓取） ----
    health = data_feed.get_news_health()
    if health.get("fetched_at"):
        st.sidebar.caption(f"🕓 上次抓取：{health['fetched_at']}")
    if health.get("offline"):
        st.sidebar.error("⚠️ 全部资讯源抓取失败，当前展示内置示例数据（离线模式）")
    elif health.get("any_failed"):
        st.sidebar.warning("部分资讯源抓取失败，数据可能不完整")
    failed = [s for s in health.get("sources", []) if not s["ok"]]
    for s in failed:
        st.sidebar.caption(f"❌ {s['source']}：{s.get('error') or '失败'}")
    ok = [s for s in health.get("sources", []) if s["ok"]]
    if ok:
        st.sidebar.caption("✅ " + "、".join(f"{s['source']}({s['count']})" for s in ok))

    # 时间范围筛选：复用后端纯函数 filter_news_by_time，兼容缺时间 /
    # 带时区的条目，避免直接用 pandas 对「发布时间」列做 `>=` 比较时
    # 因个别条目非 datetime 而隐性丢数据或抛错（R2 稳健性 + 复用既有能力）。
    time_filtered = data_feed.filter_news_by_time(news_items, time_range)
    df = build_dataframe(time_filtered)
    # 用后端 available_sources 构造来源选项（即便 df 为空/未建也能给出完整来源）
    src_opts = ["全部"] + data_feed.available_sources(news_items)
    selected_source = st.sidebar.selectbox("来源", src_opts)

    # ---- 应用筛选 ----
    filtered = df.copy()
    if not filtered.empty:
        if keyword:
            mask = filtered["标题"].str.lower().str.contains(keyword.lower(), na=False)
            mask |= filtered["情绪"].str.lower().str.contains(keyword.lower(), na=False)
            filtered = filtered[mask]
        if selected_source != "全部":
            filtered = filtered[filtered["来源"] == selected_source]
        # 排序：最新 / 最旧
        if sort_order == "最旧优先":
            filtered = filtered.sort_values("发布时间", ascending=True)
        else:
            filtered = filtered.sort_values("发布时间", ascending=False)

    # ---- 主区：Tab 切分 ----
    tab_news, tab_market, tab_stock = st.tabs(
        ["📰 资讯看板", "📈 A 股市场", "📊 个股查询"]
    )

    # ===================== 资讯看板 =====================
    with tab_news:
        # 舆情概览指标（基于全量 news_items，而非筛选后结果）
        summ = data_feed.summarize_news(news_items)
        sc1, sc2, sc3, sc4 = st.columns(4)
        sc1.metric("资讯总量", summ["total"])
        sc2.metric("正面", summ["by_sentiment"].get("正面", 0))
        sc3.metric("负面", summ["by_sentiment"].get("负面", 0))
        sc4.metric("中性", summ["by_sentiment"].get("中性", 0))

        # ---- 全文检索（基于后端 data_feed.search_news，标题+摘要） ----
        search_q = st.text_input(
            "🔍 全文检索（标题 + 摘要）",
            placeholder="输入关键词，回车检索全部资讯",
            key="news_search",
        )
        if search_q:
            # R1 新能力：检索结果分页（资讯量大时按页浏览，而非一次性铺满整屏）
            # R2 一致性：search_and_paginate 对空查询短路为「展示全部」，
            # 避免 search 清空时整页空白；这里 search_q 非空才进入此分支。
            page = st.number_input("检索结果页码", min_value=1, value=1, step=1,
                                    key="search_page")
            res = search_and_paginate(news_items, query=search_q, page=int(page),
                                      page_size=10)
            st.success(f"检索「{search_q}」命中 {res['total']} 条"
                       f"（第 {res['page']}/{res['pages']} 页）")
            for m in res["items"]:
                mtext = f"{m.get('title', '')} {m.get('summary', '')}"
                emoji = {"正面": "🟢", "负面": "🔴", "中性": "⚪"}.get(
                    data_feed.classify_sentiment(mtext), "⚪"
                )
                with st.container():
                    st.markdown(f"**{highlight_keyword(m.get('title', ''), search_q)}** {emoji}")
                    st.caption(f"{m.get('source', '')} · {m.get('link', '')}")
                    st.divider()
            st.stop()  # 检索结果优先展示，下方常规列表不再渲染

        st.subheader(f"资讯列表（共 {len(filtered)} 条）")

        # ---- 导出当前筛选结果 ----
        if not filtered.empty:
            col_exp1, col_exp2 = st.columns(2)
            with col_exp1:
                csv_buf = filtered.to_csv(index=False).encode("utf-8-sig")
                st.download_button("⬇️ 导出 CSV", csv_buf, "worldmonitor_news.csv", "text/csv")
            with col_exp2:
                json_buf = filtered.to_json(
                    orient="records", force_ascii=False, date_format="iso"
                ).encode("utf-8")
                st.download_button("⬇️ 导出 JSON", json_buf, "worldmonitor_news.json", "application/json")

        if filtered.empty:
            st.warning("当前筛选条件下没有匹配的资讯，试试放宽时间范围或更换关键词。")
        else:
            col1, col2 = st.columns(2)
            # 图1：发布量时间线（按天）
            with col1:
                tl = (
                    filtered.dropna(subset=["日期"])
                    .groupby("日期").size().reset_index(name="发布量")
                )
                fig_tl = px.line(
                    tl, x="日期", y="发布量", markers=True,
                    title="资讯发布量时间线（按天）",
                )
                fig_tl.update_layout(margin=dict(l=20, r=20, t=40, b=20), height=320)
                st.plotly_chart(fig_tl, use_container_width=True)

            # 图2：来源分布柱状
            with col2:
                sc = filtered.groupby("来源").size().reset_index(name="条数").sort_values("条数", ascending=False)
                fig_src = px.bar(sc, x="来源", y="条数", title="来源分布（按条数）", color="来源")
                fig_src.update_layout(margin=dict(l=20, r=20, t=40, b=20), height=320)
                st.plotly_chart(fig_src, use_container_width=True)

            col3, col4 = st.columns(2)
            # 图3：情绪占比
            with col3:
                sent = filtered.groupby("情绪").size().reset_index(name="条数")
                color_map = {"正面": "#2ca02c", "负面": "#d62728", "中性": "#7f7f7f"}
                fig_sent = px.pie(
                    sent, names="情绪", values="条数", title="情绪占比",
                    color="情绪", color_discrete_map=color_map,
                )
                fig_sent.update_layout(margin=dict(l=20, r=20, t=40, b=20), height=320)
                st.plotly_chart(fig_sent, use_container_width=True)

            # 图4：情绪趋势（按天堆叠）
            with col4:
                trend = (
                    filtered.dropna(subset=["日期"])
                    .groupby(["日期", "情绪"]).size().reset_index(name="条数")
                )
                fig_trend = px.bar(
                    trend, x="日期", y="条数", color="情绪", barmode="stack",
                    title="情绪趋势（按天堆叠）", color_discrete_map=color_map,
                )
                fig_trend.update_layout(margin=dict(l=20, r=20, t=40, b=20), height=320)
                st.plotly_chart(fig_trend, use_container_width=True)

            # 图5：热门话题（基于后端 trending_keywords，c111 已提供但此前未在看板落地）
            trend_df = trending_chart_data(time_filtered, top_k=12)
            if not trend_df.empty:
                fig_tr = px.bar(trend_df, x="关键词", y="次数", title="热门话题 TOP12", color="次数")
                fig_tr.update_layout(margin=dict(l=20, r=20, t=40, b=20), height=320)
                st.plotly_chart(fig_tr, use_container_width=True)

            # 图5b：发布时间分布（按小时，基于后端 news_by_hour）
            hourly_df = hourly_chart_data(time_filtered, window_hours=24)
            if not hourly_df.empty:
                fig_hr = px.bar(
                    hourly_df, x="时段", y="条数",
                    title="资讯发布时间分布（近 24 小时·按小时）", color="条数",
                )
                fig_hr.update_layout(margin=dict(l=20, r=20, t=40, b=20), height=320)
                st.plotly_chart(fig_hr, use_container_width=True)

        # 资讯列表
        st.divider()
        st.subheader("资讯列表")
        for _, row in filtered.iterrows():
            pub_str = row["发布时间"].strftime("%Y-%m-%d %H:%M") if isinstance(row["发布时间"], _dt.datetime) else ""
            emoji = {"正面": "🟢", "负面": "🔴", "中性": "⚪"}.get(row["情绪"], "⚪")
            with st.container():
                left, right = st.columns([0.86, 0.14])
                with left:
                    st.markdown(f"**[{highlight_keyword(row['标题'], keyword)}]({row['链接']})**")
                    st.caption(f"{row['来源']} · {pub_str}")
                with right:
                    st.markdown(f"{emoji} {row['情绪']}")
                st.divider()

    # ===================== A 股市场 =====================
    with tab_market:
        st.subheader("A 股主要指数概览")
        with st.spinner("加载指数行情..."):
            indices, idx_note = load_indices()
        st.info(idx_note)

        # 涨跌平概览 + 领涨/领跌榜（summarize_market 一次取数，保证同源一致）
        summ_mkt = market.summarize_market(indices)
        oc1, oc2, oc3 = st.columns(3)
        oc1.metric("上涨", summ_mkt["up"], help="涨跌幅 > 0 的指数数")
        oc2.metric("下跌", summ_mkt["down"], help="涨跌幅 < 0 的指数数")
        oc3.metric("平盘", summ_mkt["flat"], help="涨跌幅 = 0 的指数数")

        # 领涨 / 领跌榜（来自 summarize_market，一眼看当日强弱方向）
        movers = summ_mkt
        mg, ml = st.columns(2)
        with mg.expander("🔼 领涨 TOP3", expanded=False):
            for m in movers["gainers"]:
                st.write(f"{m['名称']}　{market.format_change_pct(m['涨跌幅'])}")
        with ml.expander("🔻 领跌 TOP3", expanded=False):
            for m in movers["losers"]:
                st.write(f"{m['名称']}　{market.format_change_pct(m['涨跌幅'])}")

        idf = pd.DataFrame(indices)
        if not idf.empty:
            # 着色涨跌幅
            def color_pct(val):
                if val > 0:
                    return "color: #d62728"  # A股红涨
                if val < 0:
                    return "color: #2ca02c"  # 绿跌
                return ""
            show = idf[["名称", "代码", "最新价", "涨跌幅", "涨跌额", "成交额"]].copy()
            show["成交额"] = (show["成交额"] / 1e8).round(2)  # 转亿元
            show = show.rename(columns={"成交额": "成交额(亿)"})
            st.dataframe(
                show.style.applymap(color_pct, subset=["涨跌幅"]),
                use_container_width=True, hide_index=True,
            )

            # 涨跌幅柱状图
            fig_idx = px.bar(
                idf, x="名称", y="涨跌幅", color="涨跌幅",
                color_continuous_scale=["green", "grey", "red"],
                title="指数涨跌幅 (%)",
            )
            fig_idx.update_layout(margin=dict(l=20, r=20, t=40, b=20), height=340)
            st.plotly_chart(fig_idx, use_container_width=True)

            # ---- 导出指数 ----
            csv_idx = show.to_csv(index=False).encode("utf-8-sig")
            st.download_button("⬇️ 导出指数 CSV", csv_idx, "worldmonitor_indices.csv", "text/csv")
        else:
            st.warning("暂无可展示的指数数据。")

    # ===================== 个股查询 =====================
    with tab_stock:
        st.subheader("A 股个股行情（输入代码或名称）")
        st.caption("例如：600000 / 000001 / 贵州茅台；优先 akshare 实时，离线回退示例")
        sym = st.text_input("股票代码 / 名称", placeholder="600000")
        if st.button("查询", type="primary"):
            if not sym.strip():
                st.error("请输入股票代码或名称。")
            else:
                with st.spinner("查询个股行情..."):
                    quote, note = market.get_stock_quote(sym.strip())
                st.info(note)
                if quote:
                    c1, c2, c3, c4 = st.columns(4)
                    c1.metric("最新价", quote.get("最新价", 0))
                    c2.metric(
                        "涨跌幅", f"{quote.get('涨跌幅', 0):+}%",
                        delta=f"{quote.get('涨跌额', 0):+}",
                    )
                    c3.metric("今开", quote.get("今开", 0))
                    c4.metric("昨收", quote.get("昨收", 0))
                    c5, c6 = st.columns(2)
                    c5.metric("最高", quote.get("最高", 0))
                    c6.metric("最低", quote.get("最低", 0))
                    # 当日 OHLC 迷你柱
                    import pandas as _pd

                    ohlc = _pd.DataFrame([
                        {"项": "今开", "价": quote.get("今开", 0)},
                        {"项": "最高", "价": quote.get("最高", 0)},
                        {"项": "最低", "价": quote.get("最低", 0)},
                        {"项": "最新价", "价": quote.get("最新价", 0)},
                    ])
                    fig_o = px.bar(
                        ohlc, x="项", y="价", title="当日 OHLC 概览",
                        color="项",
                    )
                    fig_o.update_layout(margin=dict(l=20, r=20, t=40, b=20), height=300)
                    st.plotly_chart(fig_o, use_container_width=True)
                else:
                    st.warning("未能获取该个股数据。")


if __name__ == "__main__":
    import sys
    from streamlit.web.cli import main as _st_main
    sys.argv = ["streamlit", "run", __file__, "--server.port", "8501"]
    _st_main()
