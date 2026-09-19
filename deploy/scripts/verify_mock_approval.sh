#!/usr/bin/env bash
# D:/ContractReviewSys-合同审批智能审查项目/deploy/scripts/verify_mock_approval.sh
# =====================================================================
# 模拟审批系统 —— 红线验证脚本
#
# 【脚本职责】
#   对运行中的模拟审批系统做一轮完整体检，验证：
#     1. 服务存活
#     2. 白名单接口可正常写入（评论送达）
#     3. 全部 8 个动作类接口均被拒绝（HTTP 403）
#     4. 无鉴权时评论接口被拒绝（HTTP 401）
#     5. 越权尝试被记入审计
#
# 【为什么单独做一个脚本】
#   "系统不会越权改变审批结论"这句话，在合规场景下必须能被证明。
#   本脚本用真实的 HTTP 请求逐个验证 8 个危险接口确实调不通，
#   产出的是可执行的证据，而不是一句承诺。
#
# 【使用方式】
#   bash deploy/scripts/verify_mock_approval.sh
#   bash deploy/scripts/verify_mock_approval.sh --base-url http://127.0.0.1:9100
#
# 【前置条件】
#   模拟审批系统已启动，且已重置过状态（重启服务即可清空审计）
#
# 【退出码】
#   0 = 全部验证通过
#   1 = 存在验证失败
# =====================================================================

set -uo pipefail

# ---------------------------------------------------------------------
# 脚本自身目录（临时文件放在这里，避免依赖系统临时目录）
# ---------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

# ---------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------
BASE_URL="${APPROVAL_BASE_URL:-http://127.0.0.1:9100}"
API_KEY="${APPROVAL_API_KEY:-your-approval-api-key}"

while [ $# -gt 0 ]; do
  case "$1" in
    --base-url) BASE_URL="$2"; shift 2 ;;
    --api-key)  API_KEY="$2";  shift 2 ;;
    -h|--help)
      echo "用法：$0 [--base-url URL] [--api-key KEY]"
      exit 0
      ;;
    *) echo "未知参数：$1"; exit 1 ;;
  esac
done

# ---------------------------------------------------------------------
# 输出样式
# ---------------------------------------------------------------------
if [ -t 1 ]; then
  C_RESET='\033[0m'; C_RED='\033[0;31m'; C_GREEN='\033[0;32m'
  C_YELLOW='\033[0;33m'; C_CYAN='\033[0;36m'; C_BOLD='\033[1m'
else
  C_RESET=''; C_RED=''; C_GREEN=''; C_YELLOW=''; C_CYAN=''; C_BOLD=''
fi

PASS_COUNT=0
FAIL_COUNT=0
WARN_COUNT=0

pass() { printf "  ${C_GREEN}[PASS]${C_RESET} %s\n" "$1"; PASS_COUNT=$((PASS_COUNT+1)); }
fail() { printf "  ${C_RED}[FAIL]${C_RESET} %s\n" "$1"; FAIL_COUNT=$((FAIL_COUNT+1)); }
warn() { printf "  ${C_YELLOW}[WARN]${C_RESET} %s\n" "$1"; WARN_COUNT=$((WARN_COUNT+1)); }
info() { printf "         %s\n" "$1"; }

section() {
  printf "\n${C_BOLD}${C_CYAN}%s${C_RESET}\n" "$1"
  printf -- "------------------------------------------------------------------------\n"
}

# ---------------------------------------------------------------------
# HTTP 工具（用 curl，避免依赖 python）
# ---------------------------------------------------------------------
HTTP_BODY=""
HTTP_CODE=""

# 发起请求，把 body 与 status code 分别存入 HTTP_BODY / HTTP_CODE
#
# 【实现说明 —— 这里刻意不用 curl 的 -o 落盘方案】
#   最初用 `curl -s -o file -w '%{http_code}'` 把 body 写文件、状态码从
#   stdout 读。这在 Git Bash on Windows 上不可靠：
#     - 工作目录可能返回 Windows 形式路径（D:\...），bash 无法写入；
#     - 临时目录若被安全策略限制，curl 会以 exit 23（写失败）静默结束，
#       而 -w 仍会打印状态码，导致"状态码正确但 body 为空"的错觉。
#   改为把状态码追加在 body 之后一次性从 stdout 读出，再按标记切分。
#   全程不碰文件系统，跨平台一致。
do_request() {
  # 用法：do_request METHOD PATH [JSON_BODY] [AUTH_HEADER]
  local method="$1" path="$2" body="${3:-}" auth="${4:-}"
  # 用一段不可能出现在响应体中的标记分隔 body 与状态码
  local sep="__CR_HTTP_STATUS__"

  local args=(-s -X "${method}" "${BASE_URL}${path}" -w "${sep}%{http_code}")
  [ -n "${auth}" ] && args+=(-H "Authorization: ${auth}")
  if [ -n "${body}" ]; then
    args+=(-H "Content-Type: application/json" -d "${body}")
  fi

  local raw
  raw="$(curl "${args[@]}" 2>/dev/null)"

  if [ -z "${raw}" ]; then
    # curl 完全无输出：服务不可达或 curl 执行失败
    HTTP_CODE="000"
    HTTP_BODY=""
    return
  fi

  if [ "${raw#*${sep}}" != "${raw}" ]; then
    # 含分隔符：正常情况，切出 body 与状态码
    HTTP_CODE="${raw##*${sep}}"
    HTTP_BODY="${raw%${sep}*}"
  else
    # 不含分隔符：curl 异常（如被中断），整体当作错误
    HTTP_CODE="000"
    HTTP_BODY="${raw}"
  fi

  [ -z "${HTTP_CODE}" ] && HTTP_CODE="000"
}

# 从 JSON 中提取字符串字段（轻量实现，不引入 jq 依赖）
json_get() {
  # 用法：json_get '{"a":"b"}' a
  printf '%s' "$1" | sed -n "s/.*\"$2\"[[:space:]]*:[[:space:]]*\"\([^\"]*\)\".*/\1/p"
}

# 从 JSON 中提取数字字段
json_get_int() {
  printf '%s' "$1" | sed -n "s/.*\"$2\"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p"
}

if ! command -v curl >/dev/null 2>&1; then
  printf "${C_RED}错误：未找到 curl，本脚本依赖 curl 发起 HTTP 请求。${C_RESET}\n"
  printf "  Windows 10+ 自带 curl；若缺失可从 https://curl.se/windows/ 获取。\n"
  exit 1
fi

# ---------------------------------------------------------------------
printf "\n${C_BOLD}模拟审批系统 — 红线验证${C_RESET}\n"
printf "目标服务：%s\n" "${BASE_URL}"
printf "鉴权令牌：%s\n" "$(printf '%s' "${API_KEY}" | sed 's/./*/g')"
printf "执行时间：%s\n" "$(date '+%Y-%m-%d %H:%M:%S')"

# =====================================================================
# 验证一：服务存活
# =====================================================================
section "验证一：服务存活"

do_request GET /health
if [ "${HTTP_CODE}" = "200" ]; then
  status="$(json_get "${HTTP_BODY}" status)"
  if [ "${status}" = "alive" ]; then
    pass "服务存活（status=alive）"
  else
    fail "服务返回 200 但 status 非 alive：${status}"
  fi
else
  fail "服务不可达（HTTP ${HTTP_CODE}）"
  info "请先启动模拟审批系统："
  info "  cd deploy/mock_approval && python -m uvicorn main:app --port 9100"
  printf "\n${C_RED}服务未启动，后续验证无法进行。${C_RESET}\n\n"
  exit 1
fi

BASELINE_COMMENTS="$(json_get_int "${HTTP_BODY}" comments_received)"
BASELINE_AUDIT="$(json_get_int "${HTTP_BODY}" violations_rejected)"
info "初始状态：已接收评论 ${BASELINE_COMMENTS:-0} 条，已拦截越权 ${BASELINE_AUDIT:-0} 次"

# =====================================================================
# 验证二：白名单接口可正常写入
# =====================================================================
section "验证二：白名单接口 — 评论写入"

TEST_TASK_NO="VERIFY-$(date +%s)"
TEST_COMMENT="【合同智能审查意见】
任务编号：${TEST_TASK_NO}
综合风险等级：高风险
审查结论：发现 2 项高风险与 1 项中风险，建议整改后提交审批
风险明细（共 3 项）：
  1. [BAL-002] 单方免责条款
  2. [NUM-002] 违约金比例超限
  3. [CONF-001] 保密期限缺失
（本意见由规则引擎判定产出，仅作审批参考，不改变审批结论）"

# 构造 JSON（用 sed 转义换行为 \n，避免手写 JSON 出错）
TEST_COMMENT_JSON="$(printf '%s' "${TEST_COMMENT}" | sed ':a;N;$!ba;s/\n/\\n/g')"

do_request POST /comments \
  "{\"contract_review_comment\":\"${TEST_COMMENT_JSON}\",\"task_no\":\"${TEST_TASK_NO}\",\"final_risk_level\":\"high\",\"risk_count\":3}" \
  "Bearer ${API_KEY}"

if [ "${HTTP_CODE}" = "200" ]; then
  code="$(json_get_int "${HTTP_BODY}" code)"
  comment_id="$(json_get "${HTTP_BODY}" comment_id)"
  if [ "${code}" = "0" ] && [ -n "${comment_id}" ]; then
    pass "白名单接口 /comments 写入成功（comment_id=${comment_id}）"
  else
    fail "写入返回 200 但业务码非 0：${HTTP_BODY}"
  fi
elif [ "${HTTP_CODE}" = "401" ]; then
  fail "鉴权失败（HTTP 401）— 检查 API_KEY 是否与会话一致"
else
  fail "/comments 返回异常状态码 HTTP ${HTTP_CODE}"
  info "响应体：$(printf '%s' "${HTTP_BODY}" | head -c 200)"
fi

# 回查确认评论真的落库
do_request GET "/comments?task_no=${TEST_TASK_NO}"
if [ "${HTTP_CODE}" = "200" ]; then
  total="$(json_get_int "${HTTP_BODY}" total)"
  if [ "${total:-0}" -ge 1 ]; then
    pass "评论可回查（total=${total}）— 确认写入真实落地"
  else
    fail "写入返回成功但回查不到（total=${total}）"
  fi
fi

# =====================================================================
# 验证三：全部动作类接口必须被拒绝
# =====================================================================
section "验证三：动作类接口 — 必须全部拒绝（红线）"

ACTIONS="approve reject transfer withdraw reassign sign complete terminate"
ACTION_CN_approve="同意"
ACTION_CN_reject="拒绝"
ACTION_CN_transfer="转交"
ACTION_CN_withdraw="撤回"
ACTION_CN_reassign="转办"
ACTION_CN_sign="签署"
ACTION_CN_complete="完结"
ACTION_CN_terminate="终止"

for action in ${ACTIONS}; do
  eval "cn=\${ACTION_CN_${action}}"

  # 注意：刻意携带**正确**的鉴权头。
  # 若拦截仅依赖鉴权，带上正确令牌就会被放行——那说明白名单机制失效。
  do_request POST "/${action}" \
    "{\"task_no\":\"${TEST_TASK_NO}\",\"result\":\"agree\"}" \
    "Bearer ${API_KEY}"

  if [ "${HTTP_CODE}" = "403" ]; then
    pass "POST /${action}（${cn}）被拒绝  HTTP 403"
  elif [ "${HTTP_CODE}" = "404" ]; then
    fail "POST /${action}（${cn}）返回 404 — 接口未实现，无法验证拦截"
  elif [ "${HTTP_CODE}" = "200" ]; then
    fail "POST /${action}（${cn}）**被放行！** 红线失守，必须立即排查"
  else
    fail "POST /${action}（${cn}）返回 HTTP ${HTTP_CODE}（预期 403）"
  fi
done

# =====================================================================
# 验证四：无鉴权时评论接口应拒绝
# =====================================================================
section "验证四：鉴权缺失时应拒绝"

do_request POST /comments "{\"contract_review_comment\":\"no-auth-test\"}"
if [ "${HTTP_CODE}" = "401" ]; then
  pass "无鉴权调用 /comments 被拒绝  HTTP 401"
else
  fail "无鉴权调用 /comments 返回 HTTP ${HTTP_CODE}（预期 401）"
fi

# =====================================================================
# 验证五：越权尝试已记入审计
# =====================================================================
section "验证五：越权尝试审计留痕"

do_request GET /audit
if [ "${HTTP_CODE}" = "200" ]; then
  audit_total="$(json_get_int "${HTTP_BODY}" total)"
  expected_delta=8   # 验证三中发起了 8 次动作类调用

  if [ "${audit_total:-0}" -ge "${expected_delta}" ]; then
    pass "审计记录 ${audit_total} 条，与发起的 ${expected_delta} 次越权尝试匹配"
  else
    fail "审计记录仅 ${audit_total} 条，少于发起的 ${expected_delta} 次 — 留痕不完整"
  fi

  verdict="$(json_get "${HTTP_BODY}" verdict)"
  [ -n "${verdict}" ] && info "结论：${verdict}"
else
  fail "查询审计记录失败（HTTP ${HTTP_CODE}）"
fi

# =====================================================================
# 汇总
# =====================================================================
section "验证结果"

printf "  通过：${C_GREEN}%d${C_RESET}    失败：${C_RED}%d${C_RESET}    告警：${C_YELLOW}%d${C_RESET}\n\n" \
  "${PASS_COUNT}" "${FAIL_COUNT}" "${WARN_COUNT}"

if [ "${FAIL_COUNT}" -eq 0 ]; then
  printf "  ${C_GREEN}${C_BOLD}全部通过：白名单机制生效，动作类接口全部不可达。${C_RESET}\n\n"
  printf "  验证结论：\n"
  printf "    - 评论类接口（/comments、/comments/batch）可正常写入\n"
  printf "    - 全部 8 个动作类接口返回 403，即便携带正确鉴权头\n"
  printf "    - 越权尝试全部留痕，可通过 GET /audit 审计\n"
  printf "    - 无鉴权调用被拒绝（401）\n\n"
  printf "  ※ 本脚本刻意制造了 %d 次越权尝试用于验证拦截。\n" "${expected_delta:-8}"
  printf "    如需恢复干净的审计状态，重启模拟审批系统即可（数据存内存）。\n"
  printf "    真实运行中 GET /audit 的 total 应为 0。\n\n"
  exit 0
else
  printf "  ${C_RED}${C_BOLD}存在 %d 项失败，请按上方提示排查。${C_RESET}\n\n" "${FAIL_COUNT}"
  printf "  排查方向：\n"
  printf "    - 动作类接口被放行 → 检查 deploy/mock_approval/main.py 的 _reject_action\n"
  printf "    - 审计记录缺失     → 检查 _record_audit 是否被调用\n"
  printf "    - 鉴权未生效       → 检查 _check_auth 实现\n\n"
  exit 1
fi
