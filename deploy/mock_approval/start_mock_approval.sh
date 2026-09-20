#!/usr/bin/env bash
# D:/ContractReviewSys-合同审批智能审查项目/deploy/mock_approval/start_mock_approval.sh
#
# 启动模拟审批系统（Git Bash / WSL / Linux / macOS，前台运行）
#
# 职责：
#   在 127.0.0.1:9100 启动一个 FastAPI 应用，模拟企业审批系统的
#   评论接口，用于演示"审查意见回写审批评论区"的完整链路。
#
# 重要说明：
#   本模拟系统同时是一个**权责红线的验证器**：
#     允许：POST /comments、POST /comments/batch
#     强制拒绝（403）：/approve、/reject、/transfer、/withdraw、
#                      /reassign、/sign、/complete、/terminate
#   即使用户绕过前端直接调接口，动作类请求也会被硬拒绝，
#   用来证明系统绝不会越权执行审批动作。

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

cat <<'BANNER'

============================================================
  模拟审批系统  (Mock Approval System)
============================================================
  地址：http://127.0.0.1:9100
  文档：http://127.0.0.1:9100/docs
  审计：http://127.0.0.1:9100/audit

  注意：本服务为演示用途，数据存于内存，重启即清空。
        动作类接口（approve/reject 等）一律返回 403，
        用于验证系统不会越权执行审批动作。

  按 Ctrl+C 停止服务。
============================================================

BANNER

cd "${SCRIPT_DIR}"
exec "${PY}" -m uvicorn main:app --host 127.0.0.1 --port 9100
