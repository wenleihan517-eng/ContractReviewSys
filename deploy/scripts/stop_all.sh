#!/usr/bin/env bash
# D:/ContractReviewSys-合同审批智能审查项目/deploy/scripts/stop_all.sh
#
# 一键停止全部应用服务（Git Bash / WSL / Linux / macOS）
#
# 职责：
#   调用 devctl.py 按逆序停止 frontend → backend → mock-approval。
#
# 说明：
#   进程终止会先发 SIGTERM 并宽限 5 秒，仍未退出才 SIGKILL，
#   保证 uvicorn / vite 有机会写完日志与释放端口。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

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
    exit 1
fi

echo
echo "============================================================"
echo "  停止合同审批智能审查系统"
echo "============================================================"
echo

set +e
"${PY}" "${SCRIPT_DIR}/devctl.py" stop "$@"
RC=$?
set -e

echo
if [ "${RC}" -eq 0 ]; then
    echo "[OK] 全部服务已停止。"
else
    echo "[FAIL] 部分服务未能完全停止，请检查端口占用："
    echo "       ss -ltnp 2>/dev/null | grep -E ':(8000|5173|9100)' \\"
    echo "         || netstat -ano | findstr \":8000 :5173 :9100\""
fi
exit "${RC}"
