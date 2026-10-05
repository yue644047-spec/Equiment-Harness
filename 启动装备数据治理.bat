@echo off
chcp 65001 >nul
title 装备数据治理
cd /d %~dp0
echo 正在启动 装备数据治理 ...

rem ---- 本地重排服务（模型角色 · rerank，ADR-D53）----
rem 未在跑就先隐藏拉起：没它时产品检索会自动回落纯混合排序（不报错），
rem 但门户「服务状态」会显示「重排模型 不可用」、系统设置会显示缺失项 RERANK。
set RERANK_PY=E:\llm-stack\src\rerank-py\.venv\Scripts\pythonw.exe
netstat -ano -p tcp | findstr /c:":8196 " | findstr /c:"LISTENING" >nul 2>&1
if not errorlevel 1 (
  echo   重排服务已在运行（8196）
) else if exist "%RERANK_PY%" (
  echo   启动本地重排服务（8196，bge-reranker-v2-m3）...
  start "" "%RERANK_PY%" "%~dp0platform\rerank_server.py"
) else (
  echo   [警告] 未找到重排 venv：%RERANK_PY%
  echo           检索将走纯混合排序；需要重排请先按 docs\16 ADR-D53 建好该 venv
)

"D:\llm-stack\src\portal\bff\.venv\Scripts\python.exe" "%~dp0platform\serve.py" %*
pause
