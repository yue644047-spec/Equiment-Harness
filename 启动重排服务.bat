@echo off
chcp 65001 >nul
title 装备数据治理 · 重排服务
cd /d %~dp0
echo 正在启动 本地重排服务（BGE Reranker v2 m3，/v1/rerank）...
echo 端口与模型路径取自 platform\config\equipment.env 的 RERANK_* 段
"E:\llm-stack\src\rerank-py\.venv\Scripts\python.exe" "%~dp0platform\rerank_server.py" %*
pause
