#!/usr/bin/env bash
# D:/ContractReviewSys-合同审批智能审查项目/deploy/scripts/start_all.sh
#
# 一键启动全部应用服务（Git Bash / WSL / Linux / macOS）
#
# 职责：
#   调用 devctl.py 拉起 mock-approval → backend → frontend，并打印访问地址。
#
# 说明：
#   1. 脚本会自动探测并优先使用 backend/.venv 中的解释器。
#   2. 服务以独立会话（setsid / start_new_session）运行，
#      关闭本终端不会中断服务。
#   3. 停止服务请运行 stop_all.sh。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

# --- 选择 Python 解释器 ---
PY="python"
for candidate in \
    "${PROJECT_ROOT}/backend/.venv/Scripts/python.exe" \
    "${PROJECT_ROOT}/backend/.venv/bin/python"
do
    if [ -f "${candidate}" ]; then
        PY="${candidate}"
        break
    fi
done

if ! command -v "${PY}" >/dev/null 2>&1 && [ ! -x "${PY}" ]; then
    echo "[FAIL] 无法执行 Python：${PY}"
    echo "       请安装 Python 3.10，或先创建虚拟环境："
    echo "           cd backend && python -m venv .venv"
    echo "           source .venv/Scripts/activate   # Git Bash"
    echo "           pip install -r requirements.txt"
    exit 1
fi

echo
echo "============================================================"
echo "  启动合同审批智能审查系统"
echo "============================================================"
echo "  解释器：${PY}"
echo

set +e
"${PY}" "${SCRIPT_DIR}/devctl.py" start "$@"
RC=$?
set -e

echo
if [ "${RC}" -eq 0 ]; then
    echo "[OK] 启动流程完成，请访问 http://127.0.0.1:5173"
else
    echo "[FAIL] 启动流程存在失败项，日志目录："
    echo "       ${SCRIPT_DIR}/../runtime/logs"
fi
exit "${RC}"
