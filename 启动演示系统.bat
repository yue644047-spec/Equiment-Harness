@echo off
chcp 65001 >nul
setlocal
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
cd /d "%~dp0"

set "PYEXE="
if exist "D:\ProgramData\anaconda3\python.exe" set "PYEXE=D:\ProgramData\anaconda3\python.exe"
if not defined PYEXE (
  where py >nul 2>nul && set "PYEXE=py -3"
)
if not defined PYEXE (
  where python >nul 2>nul && set "PYEXE=python"
)
if not defined PYEXE (
  echo [ERROR] Python 3 not found. Install Python 3 first, or run manually:
  echo         python eval_service\cli.py dashboard --open
  pause
  exit /b 1
)

set "DB=eval_service\data\eval.db"

if not exist "%DB%" (
  echo [1/5] Generating demo data ^(mock, no credentials needed^) ...
  %PYEXE% eval_service\cli.py demo --db "%DB%" --quiet
)
echo [2/5] Running attribution ^(document vs system^) ...
%PYEXE% eval_service\cli.py attribute --db "%DB%"
echo [3/5] Building document quality scorecard ...
%PYEXE% eval_service\cli.py scorecard --db "%DB%"
echo [4/5] Preparing demo assets and business database ...
if not exist "assets\assets.json" %PYEXE% tools\make_demo_assets.py --force
if not exist "demo_data\business.db" %PYEXE% tools\make_demo_business_db.py --force
echo [5/5] Starting retrieval contract layer and governance dashboard ...
start "kb_bridge 9380" /min %PYEXE% kb_bridge\bridge_server.py --port 9380
timeout /t 2 >nul
echo.
echo Dashboard: http://127.0.0.1:8848/     Bridge: http://127.0.0.1:9380/
echo [演示系统] 浏览器将自动打开；关闭本窗口即停止看板，检索契约层在另一个窗口。
echo.
%PYEXE% eval_service\cli.py dashboard --db "%DB%" --open
pause
