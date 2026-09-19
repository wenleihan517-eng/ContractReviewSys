@echo off
REM D:/ContractReviewSys-合同审批智能审查项目/deploy/scripts/init_all.bat
REM
REM 一键初始化（Windows 双击即可运行）
REM
REM 职责：
REM   按正确顺序执行 MongoDB → Milvus → 规则向量化 → 全链路自检。
REM
REM 说明：
REM   初始化是幂等的，重复执行不会产生重复数据。
REM   如要清空重建，用命令行传参：
REM       init_all.bat --recreate

setlocal
chcp 65001 >nul 2>&1

set "SCRIPT_DIR=%~dp0"
set "PROJECT_ROOT=%SCRIPT_DIR%..\.."
set "BACKEND_VENV=%PROJECT_ROOT%\backend\.venv\Scripts\python.exe"

set "PY=python"
if exist "%BACKEND_VENV%" (
    set "PY=%BACKEND_VENV%"
    echo [INFO] 使用项目虚拟环境：%BACKEND_VENV%
) else (
    echo [WARN] 未找到 backend\.venv，使用 PATH 中的 python
    echo.
)

"%PY%" --version >nul 2>&1
if errorlevel 1 (
    echo [FAIL] 无法执行 Python：%PY%
    echo        请安装 Python 3.10 并加入 PATH，或先创建 backend\.venv
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   初始化合同审批智能审查系统
echo ============================================================
echo.

"%PY%" "%SCRIPT_DIR%init_all.py" %*
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
    echo [OK] 初始化完成。
) else (
    echo [FAIL] 初始化存在失败项，请按上方提示修复后重跑。
)
echo.
pause
exit /b %RC%
