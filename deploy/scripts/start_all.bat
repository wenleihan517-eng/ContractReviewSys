@echo off
REM D:/ContractReviewSys-合同审批智能审查项目/deploy/scripts/start_all.bat
REM
REM 一键启动全部应用服务（Windows 双击即可运行）
REM
REM 职责：
REM   调用 devctl.py 拉起 mock-approval → backend → frontend 三个服务，
REM   并在启动完成后打印访问地址。
REM
REM 说明：
REM   1. 优先使用项目自带虚拟环境 backend\.venv\Scripts\python.exe；
REM      找不到则回退到 PATH 中的 python。
REM   2. 服务在后台独立进程运行，关掉本窗口不会影响服务。
REM      停止服务请运行 stop_all.bat。

setlocal
chcp 65001 >nul 2>&1

set "SCRIPT_DIR=%~dp0"
set "PROJECT_ROOT=%SCRIPT_DIR%..\.."
set "BACKEND_VENV=%PROJECT_ROOT%\backend\.venv\Scripts\python.exe"

REM --- 选择 Python 解释器 ---
set "PY=python"
if exist "%BACKEND_VENV%" (
    set "PY=%BACKEND_VENV%"
    echo [INFO] 使用项目虚拟环境：%BACKEND_VENV%
) else (
    echo [WARN] 未找到 backend\.venv，使用 PATH 中的 python
    echo        建议先创建虚拟环境：
    echo            cd backend
    echo            python -m venv .venv
    echo            .venv\Scripts\pip install -r requirements.txt
    echo.
)

REM --- 检查解释器可用 ---
"%PY%" --version >nul 2>&1
if errorlevel 1 (
    echo [FAIL] 无法执行 Python：%PY%
    echo        请安装 Python 3.10 并加入 PATH，或先创建 backend\.venv
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   启动合同审批智能审查系统
echo ============================================================
echo.

REM 把剩余参数透传给 devctl.py（如 start_all.bat backend）
"%PY%" "%SCRIPT_DIR%devctl.py" start %*
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
    echo [OK] 启动流程完成，请用浏览器访问 http://127.0.0.1:5173
) else (
    echo [FAIL] 启动流程存在失败项，请查看上方输出与日志目录：
    echo        %SCRIPT_DIR%..\runtime\logs
)
echo.
pause
exit /b %RC%
