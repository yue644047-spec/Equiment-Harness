@echo off
chcp 65001 >nul
setlocal
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
  echo [1/3] First run: generating demo data ^(mock, no credentials needed^) ...
  %PYEXE% eval_service\cli.py demo --db "%DB%" --quiet
  echo [2/3] Running attribution ^(document vs system^) ...
  %PYEXE% eval_service\cli.py attribute --db "%DB%"
  echo [3/3] Building document quality scorecard ...
  %PYEXE% eval_service\cli.py scorecard --db "%DB%"
)

echo.
echo Governance dashboard: http://127.0.0.1:8848/   ^(Ctrl+C to stop^)
echo [治理看板] 浏览器将自动打开；按 Ctrl+C 停止服务。
echo [数据源] %DB%
echo.
%PYEXE% eval_service\cli.py dashboard --db "%DB%" --open
pause
