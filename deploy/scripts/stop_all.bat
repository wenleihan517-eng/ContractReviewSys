@echo off
REM D:/ContractReviewSys-合同审批智能审查项目/deploy/scripts/stop_all.bat
REM
REM 一键停止全部应用服务（Windows 双击即可运行）
REM
REM 职责：
REM   调用 devctl.py 按逆序停止 frontend → backend → mock-approval，
REM   并清理残留的 PID 文件。
REM
REM 说明：
REM   进程终止使用 taskkill /T /F 递归杀进程树，
REM   避免 uvicorn --reload / vite 派生的子进程变成孤儿占用端口。

setlocal
chcp 65001 >nul 2>&1

set "SCRIPT_DIR=%~dp0"
set "PROJECT_ROOT=%SCRIPT_DIR%..\.."
set "BACKEND_VENV=%PROJECT_ROOT%\backend\.venv\Scripts\python.exe"

set "PY=python"
if exist "%BACKEND_VENV%" set "PY=%BACKEND_VENV%"

"%PY%" --version >nul 2>&1
if errorlevel 1 (
    echo [FAIL] 无法执行 Python：%PY%
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   停止合同审批智能审查系统
echo ============================================================
echo.

"%PY%" "%SCRIPT_DIR%devctl.py" stop %*
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
    echo [OK] 全部服务已停止。
) else (
    echo [FAIL] 部分服务未能完全停止，请查看上方输出。
    echo        可手工检查端口占用：
    echo            netstat -ano ^| findstr ":8000 :5173 :9100"
)
echo.
pause
exit /b %RC%
