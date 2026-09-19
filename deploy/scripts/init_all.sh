#!/usr/bin/env bash
# D:/ContractReviewSys-合同审批智能审查项目/deploy/scripts/init_all.sh
#
# 一键初始化（Git Bash / WSL / Linux / macOS）
#
# 职责：
#   按正确顺序执行 MongoDB → Milvus → 规则向量化 → 全链路自检。
#
# 说明：
#   初始化幂等，可安全重复执行。
#   清空重建：./init_all.sh --recreate

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
echo "  初始化合同审批智能审查系统"
echo "============================================================"
echo "  解释器：${PY}"
echo

set +e
"${PY}" "${SCRIPT_DIR}/init_all.py" "$@"
RC=$?
set -e

echo
if [ "${RC}" -eq 0 ]; then
    echo "[OK] 初始化完成。下一步：./start_all.sh"
else
    echo "[FAIL] 初始化存在失败项，请按上方提示修复后重跑。"
fi
exit "${RC}"
