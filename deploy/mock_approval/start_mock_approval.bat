@echo off
REM D:/ContractReviewSys-合同审批智能审查项目/deploy/mock_approval/start_mock_approval.bat
REM
REM 启动模拟审批系统（Windows，前台运行）
REM
REM 职责：
REM   在 127.0.0.1:9100 启动一个 FastAPI 应用，模拟企业审批系统的
REM   评论接口，用于演示"审查意见回写审批评论区"的完整链路。
REM
REM 重要说明：
REM   本模拟系统同时是一个**权责红线的验证器**：
REM     允许：POST /comments、POST /comments/batch
REM     强制拒绝（403）：/approve、/reject、/transfer、/withdraw、
REM                      /reassign、/sign、/complete、/terminate
REM   也就是说，即使前端/后端被误改而发出"批准/驳回"类请求，
REM   也会在模拟系统中被硬拒绝，用来验证系统绝不会越权做审批动作。
REM
REM 启动后访问：
REM   http://127.0.0.1:9100/docs    接口文档
REM   http://127.0.0.1:9100/audit   审计记录（含红线验证结论）
REM   http://127.0.0.1:9100/health  健康检查

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
echo   模拟审批系统  (Mock Approval System)
echo ============================================================
echo   地址：http://127.0.0.1:9100
echo   文档：http://127.0.0.1:9100/docs
echo   审计：http://127.0.0.1:9100/audit
echo.
echo   注意：本服务为演示用途，数据存于内存，重启即清空。
echo         动作类接口（approve/reject 等）一律返回 403，
echo         用于验证系统不会越权执行审批动作。
echo.
echo   按 Ctrl+C 停止服务。
echo ============================================================
echo.

cd /d "%SCRIPT_DIR%"
"%PY%" -m uvicorn main:app --host 127.0.0.1 --port 9100

echo.
echo 服务已退出。
pause
