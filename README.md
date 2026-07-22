# worldmonitor-mirror

一个模仿开源项目 **worldmonitor** 思路、但做成「通用科技/资讯实时情报看板 + 可选 A 股市场模块」的 Streamlit MVP。

> 定位：实时聚合多源资讯、做可视化与情绪分析；A 股模块为可选增强（不依赖、不照抄任何第三方项目本体）。所有网络抓取均带离线兜底，**断网也能跑、不崩溃**。

## 功能

- **多源资讯聚合**：Hacker News Algolia API（JSON）+ RSS（阮一峰网络日志 / 少数派），解析标题、链接、来源、时间。
- **Plotly 图表**：
  1. 资讯发布量时间线（按天）
  2. 来源分布柱状图
  3. 情绪占比饼图（基于关键词命中的正面/负面/中性）
  4. 情绪趋势堆叠图（按天）
- **侧边栏筛选**：关键词、来源、时间范围（最近 N 小时），实时过滤列表与图表。
- **A 股市场模块**：Tab 展示主要指数概览（上证/深证/创业板/沪深300/科创50）。
  - 优先用 `akshare`（`stock_zh_index_spot_em`）抓取真实行情；
  - 若 `akshare` 未安装或抓取失败，自动回退内置 mock 数据。
- **离线兜底**：所有抓取用 `try/except`，失败时回退示例数据并给出友好提示。

## 目录结构

```
worldmonitor-mirror/
├── app.py          # Streamlit 主程序（入口）
├── data_feed.py    # 资讯抓取与解析（API/RSS + mock 兜底）
├── market.py       # A 股指数模块（akshare 优先 + mock 兜底）
├── requirements.txt
└── README.md
```

## 运行

### 1. 安装依赖

```bash
pip install -r requirements.txt
# 或按需安装核心轻量依赖
pip install streamlit plotly requests feedparser pandas
# A 股模块可选
pip install akshare
```

### 2. 启动

```bash
streamlit run app.py
```

浏览器打开终端提示的本地地址（默认 http://localhost:8501）。

## 使用说明

- 顶部 Tab 切换「资讯看板 / A 股市场」。
- 左侧边栏输入关键词、选择来源、拖动时间范围滑块过滤；点「重新抓取」可强制刷新。
- 资讯列表每条可点击跳转原文，右侧标注情绪（🟢正面 / 🔴负面 / ⚪中性）。

## 离线模式

当 HN / RSS 全部不可达时，看板自动使用内置示例资讯；当 `akshare` 不可用时，A 股模块显示内置示例指数。页面会显示黄色提示说明数据来源，应用不会报错退出。

## 技术栈

Python · Streamlit · Plotly · requests · feedparser · akshare（可选）

## 近期迭代（自驱动开发 10 轮）

- 资讯看板新增「筛选结果 CSV / JSON」一键导出，A 股指数新增 CSV 导出
- 修复刷新逻辑：`🔄 重新抓取` 改为**作用域缓存清理**（仅清新闻/指数缓存，不再误清全局），并始终走缓存函数保证拿到最新数据
- 新增本地展示时间戳，离线兜底（断网自动回退内置示例），app 永不崩溃
- 附 `run.sh` / `run.bat` 一键启动

## 依赖与降级

- **akshare 版本脆弱性警告**：A 股行情模块依赖 `akshare`，该包版本迭代极快、API 易变；如安装版本不兼容或抓取失败，模块**自动回退内置 mock 行情**，看板其余功能不受影响。
- **离线兜底**：所有网络抓取（Hacker News / RSS）均带 `try/except`，失败时回退示例数据并给出友好提示，**断网也能跑、不崩溃**。
- A 股模块为可选增强，可完全不依赖、关闭后仍为通用资讯情报看板。开发/测试依赖见 `requirements-dev.txt`。

