@echo off
REM worldmonitor 情报看板 —— 一键启动
where python >nul 2>nul || (echo [错误] 未检测到 python，请先安装 Python 并勾选 "Add to PATH"。 & pause & exit /b)
streamlit run app.py --server.port 8501
pause
