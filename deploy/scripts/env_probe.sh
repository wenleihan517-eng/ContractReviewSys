#!/usr/bin/env bash
# D:/ContractReviewSys-合同审批智能审查项目/deploy/scripts/env_probe.sh
# =====================================================================
# 环境预检脚本（Linux / macOS / Git Bash）
#
# 【脚本职责】
#   在部署前快速确认"本机是否具备跑起本系统的最小条件"：
#     1. 运行时版本（Python / Node / npm）
#     2. 依赖服务端口连通性（MongoDB / MinIO / Milvus / Embedding / LLM / 审批系统）
#     3. 关键文件与目录是否存在
#     4. 端口占用情况（确认 8000 / 5173 没被别的进程占着）
#
# 【为什么需要它】
#   本系统的启动依赖 6 个外部服务。新手最常见的挫败是"敲了一堆命令，
#   最后在某一步失败，但看不出是哪个环节的问题"。先跑一遍预检，
#   能把"环境问题"和"程序问题"彻底分开。
#
# 【使用方式】
#   bash deploy/scripts/env_probe.sh
#
# 【退出码】
#   0 = 全部必需项通过（可能有告警）
#   1 = 存在必需项失败
# =====================================================================

set -uo pipefail

# ---------------------------------------------------------------------
# 输出样式
# ---------------------------------------------------------------------
if [ -t 1 ]; then
  C_RESET='\033[0m'; C_RED='\033[0;31m'; C_GREEN='\033[0;32m'
  C_YELLOW='\033[0;33m'; C_CYAN='\033[0;36m'; C_BOLD='\033[1m'
else
  C_RESET=''; C_RED=''; C_GREEN=''; C_YELLOW=''; C_CYAN=''; C_BOLD=''
fi

FAIL_COUNT=0
WARN_COUNT=0

ok()   { printf "  ${C_GREEN}[OK]${C_RESET}   %s\n" "$1"; }
warn() { printf "  ${C_YELLOW}[WARN]${C_RESET} %s\n" "$1"; WARN_COUNT=$((WARN_COUNT+1)); }
fail() { printf "  ${C_RED}[FAIL]${C_RESET} %s\n" "$1"; FAIL_COUNT=$((FAIL_COUNT+1)); }
info() { printf "         %s\n" "$1"; }

section() {
  printf "\n${C_BOLD}${C_CYAN}%s${C_RESET}\n" "$1"
  printf -- "------------------------------------------------------------------------\n"
}

# ---------------------------------------------------------------------
# 定位项目根目录（本脚本位于 deploy/scripts/ 下，向上两级）
# ---------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

printf "\n${C_BOLD}合同审批智能审查系统 — 环境预检${C_RESET}\n"
printf "项目根目录：%s\n" "${PROJECT_ROOT}"
printf "检测时间：%s\n" "$(date '+%Y-%m-%d %H:%M:%S')"

# ---------------------------------------------------------------------
# 一、运行时版本
# ---------------------------------------------------------------------
section "一、运行时版本"

# --- Python ---
# 说明：需要区分两个概念——
#   PY_CMD     ：满足"版本为 3.10.x"要求的解释器，用于提示用户建环境
#   PY_PROBE   ：任意可用 Python，仅用于本脚本内部做 TCP 探测与 JSON 解析
# 不能混用：本机若是 Python 3.13，PY_CMD 会为空，但 TCP 探测仍应正常工作，
# 否则所有服务都会被误报为"无法探测"。
PY_CMD=""
PY_PROBE=""
PY_FOUND_CMD=""
PY_FOUND_VER=""

for candidate in python3.10 python3 python; do
  command -v "$candidate" >/dev/null 2>&1 || continue

  ver="$("$candidate" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null || echo "")"
  [ -z "${ver}" ] && continue

  # 任意可用 Python 都可以做探测，取第一个找到的即可
  [ -z "${PY_PROBE}" ] && PY_PROBE="$candidate"

  major="${ver%%.*}"; rest="${ver#*.}"; minor="${rest%%.*}"
  if [ "${major}" = "3" ] && [ "${minor}" = "10" ] && [ -z "${PY_CMD}" ]; then
    PY_CMD="$candidate"
    ok "Python ${ver}  （${candidate}，符合要求 3.10.x）"
    break
  elif [ -z "${PY_FOUND_VER}" ]; then
    PY_FOUND_CMD="$candidate"
    PY_FOUND_VER="$ver"
  fi
done

if [ -z "${PY_CMD}" ]; then
  if [ -n "${PY_FOUND_VER}" ]; then
    warn "Python ${PY_FOUND_VER}（${PY_FOUND_CMD}）— 版本不是 3.10.x"
    info "本项目技术栈固定 Python 3.10，其它版本可能出现依赖不兼容"
    info "建议用 conda 创建：conda create -n contract-review python=3.10 -y"
    info "本脚本后续探测将临时使用 ${PY_PROBE} 执行（不影响判定结果）"
  else
    fail "未找到 Python"
    info "安装：https://www.python.org/downloads/release/python-31011/"
  fi
fi

# --- Node ---
if command -v node >/dev/null 2>&1; then
  node_ver="$(node -v)"
  node_major="$(printf '%s' "${node_ver}" | sed 's/^v//' | cut -d. -f1)"
  if [ "${node_major}" -ge 18 ] 2>/dev/null; then
    ok "Node ${node_ver}（要求 ≥ 18）"
  else
    warn "Node ${node_ver} — 低于 18，构建可能失败"
    info "建议升级到 Node 18 或 20 LTS"
  fi
else
  fail "未找到 node"
  info "安装：https://nodejs.org/（选 18 LTS 或 20 LTS）"
fi

if command -v npm >/dev/null 2>&1; then
  ok "npm v$(npm -v)"
else
  warn "未找到 npm（随 Node 一起安装，通常不会缺失）"
fi

# ---------------------------------------------------------------------
# 二、依赖服务连通性
# ---------------------------------------------------------------------
section "二、依赖服务连通性"

# 读取 backend/.env 中的连接配置，保证检测目标与程序实际使用的一致
ENV_FILE="${PROJECT_ROOT}/backend/.env"
ENV_EXAMPLE="${PROJECT_ROOT}/backend/.env.example"

read_env() {
  # 用法：read_env KEY DEFAULT
  local key="$1" default="$2" value=""
  if [ -f "${ENV_FILE}" ]; then
    value="$(grep -E "^${key}=" "${ENV_FILE}" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '\r' | sed 's/^"//; s/"$//')"
  fi
  if [ -z "${value}" ]; then
    if [ -f "${ENV_EXAMPLE}" ]; then
      value="$(grep -E "^${key}=" "${ENV_EXAMPLE}" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '\r' | sed 's/^"//; s/"$//')"
    fi
  fi
  [ -z "${value}" ] && value="${default}"
  printf '%s' "${value}"
}

if [ ! -f "${ENV_FILE}" ]; then
  warn "backend/.env 不存在，将使用 .env.example 的默认值做检测"
  info "请先执行：cd backend && cp .env.example .env"
fi

# TCP 连通性探测（不依赖 nc/telnet，用 bash 内建 /dev/tcp 可能不可用，
# 优先用 python 做探测，最可靠）
probe_tcp() {
  # 用法：probe_tcp NAME HOST PORT [是否必需]
  local name="$1" host="$2" port="$3" required="${4:-yes}"
  local result

  if [ -n "${PY_PROBE}" ]; then
    result="$("${PY_PROBE}" - "${host}" "${port}" <<'PYEOF' 2>/dev/null
import socket, sys
host, port = sys.argv[1], int(sys.argv[2])
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(2.0)
try:
    s.connect((host, port))
    print("OK")
except Exception as e:
    print(f"ERR:{type(e).__name__}")
finally:
    s.close()
PYEOF
)"
  elif command -v nc >/dev/null 2>&1; then
    if nc -z -w 2 "${host}" "${port}" >/dev/null 2>&1; then
      result="OK"
    else
      result="ERR:ConnectFail"
    fi
  else
    result="UNKNOWN"
  fi

  case "${result}" in
    OK)
      ok "${name} 可达  ${host}:${port}"
      return 0
      ;;
    UNKNOWN)
      warn "${name} 无法探测（缺少 python 与 nc）  目标 ${host}:${port}"
      return 1
      ;;
    *)
      if [ "${required}" = "yes" ]; then
        fail "${name} 不可达  ${host}:${port}  （${result#ERR:}）"
      else
        warn "${name} 不可达  ${host}:${port}  （${result#ERR:}，可选）"
      fi
      return 1
      ;;
  esac
}

# --- MongoDB ---
MONGO_URI="$(read_env MONGO_URI 'mongodb://127.0.0.1:27017')"
# 从 URI 中提取 host:port
MONGO_HOSTPORT="$(printf '%s' "${MONGO_URI}" | sed -E 's#^mongodb(\+srv)?://##; s#/.*$##; s#.*@##')"
MONGO_HOST="${MONGO_HOSTPORT%%:*}"
MONGO_PORT="${MONGO_HOSTPORT##*:}"
[ "${MONGO_PORT}" = "${MONGO_HOST}" ] && MONGO_PORT=27017
probe_tcp "MongoDB" "${MONGO_HOST}" "${MONGO_PORT}" "yes" || {
  info "启动方式（Docker）：docker run -d --name cr-mongodb -p 27017:27017 mongo:7.0"
  info "启动方式（本机）：net start MongoDB"
}

# --- MinIO ---
MINIO_ENDPOINT="$(read_env MINIO_ENDPOINT '127.0.0.1:9000')"
MINIO_HOST="${MINIO_ENDPOINT%%:*}"
MINIO_PORT="${MINIO_ENDPOINT##*:}"
probe_tcp "MinIO  " "${MINIO_HOST}" "${MINIO_PORT}" "yes" || {
  info "启动方式：cd deploy/docker && docker compose up -d minio"
}

# --- Milvus ---
MILVUS_HOST="$(read_env MILVUS_HOST '192.168.10.130')"
MILVUS_PORT="$(read_env MILVUS_PORT '7000')"
probe_tcp "Milvus " "${MILVUS_HOST}" "${MILVUS_PORT}" "yes" || {
  info "Milvus 为外置服务，请确认虚拟机 192.168.10.130 已启动且端口开放"
  info "本机联调可用：解开 deploy/docker/docker-compose.yml 中被注释的 milvus 服务"
}

# --- Embedding ---
EMBED_URL="$(read_env EMBEDDING_BASE_URL 'http://127.0.0.1:9997/v1')"
EMBED_HOSTPORT="$(printf '%s' "${EMBED_URL}" | sed -E 's#^https?://##; s#/.*$##')"
EMBED_HOST="${EMBED_HOSTPORT%%:*}"
EMBED_PORT="${EMBED_HOSTPORT##*:}"
[ "${EMBED_PORT}" = "${EMBED_HOST}" ] && EMBED_PORT=80
probe_tcp "BGE-M3 " "${EMBED_HOST}" "${EMBED_PORT}" "yes" || {
  info "向量化服务，Milvus 初始化与语义规则召回依赖它"
}

# --- LLM ---
LLM_URL="$(read_env LLM_BASE_URL 'http://127.0.0.1:8001/v1')"
LLM_HOSTPORT="$(printf '%s' "${LLM_URL}" | sed -E 's#^https?://##; s#/.*$##')"
LLM_HOST="${LLM_HOSTPORT%%:*}"
LLM_PORT="${LLM_HOSTPORT##*:}"
[ "${LLM_PORT}" = "${LLM_HOST}" ] && LLM_PORT=80
probe_tcp "LLM    " "${LLM_HOST}" "${LLM_PORT}" "no" || {
  info "本地大模型服务。系统在无 LLM 时仍可完成规则判定（降级运行），"
  info "但条款语义解析与建议初稿能力不可用。"
}

# --- 审批系统（模拟） ---
APPROVAL_URL="$(read_env APPROVAL_BASE_URL 'http://127.0.0.1:9100')"
APPROVAL_HOSTPORT="$(printf '%s' "${APPROVAL_URL}" | sed -E 's#^https?://##; s#/.*$##')"
APPROVAL_HOST="${APPROVAL_HOSTPORT%%:*}"
APPROVAL_PORT="${APPROVAL_HOSTPORT##*:}"
[ "${APPROVAL_PORT}" = "${APPROVAL_HOST}" ] && APPROVAL_PORT=80
probe_tcp "审批系统" "${APPROVAL_HOST}" "${APPROVAL_PORT}" "no" || {
  info "演示用模拟审批系统，启动方式："
  info "  cd deploy/mock_approval && python -m uvicorn main:app --port 9100"
}

# ---------------------------------------------------------------------
# 三、关键文件与目录
# ---------------------------------------------------------------------
section "三、关键文件与目录"

check_file() {
  # 用法：check_file 相对路径 说明 [是否必需]
  local rel="$1" desc="$2" required="${3:-yes}"
  if [ -f "${PROJECT_ROOT}/${rel}" ]; then
    ok "${rel}  — ${desc}"
  else
    if [ "${required}" = "yes" ]; then
      fail "${rel} 缺失  — ${desc}"
    else
      warn "${rel} 缺失  — ${desc}（可选）"
    fi
  fi
}

check_dir() {
  local rel="$1" desc="$2" required="${3:-yes}"
  if [ -d "${PROJECT_ROOT}/${rel}" ]; then
    ok "${rel}/  — ${desc}"
  else
    if [ "${required}" = "yes" ]; then
      fail "${rel}/ 缺失  — ${desc}"
    else
      warn "${rel}/ 缺失  — ${desc}（可选，会自动创建）"
    fi
  fi
}

check_file "backend/requirements.txt"       "后端依赖清单"
check_file "backend/.env.example"           "环境变量模板"
check_file "backend/.env"                   "环境变量（本地配置）" "no"
check_file "backend/config/risk_rules.yaml" "风险规则配置（规则引擎的判定依据）"
check_file "backend/app/main.py"            "后端应用入口"
check_file "frontend/package.json"          "前端依赖清单"
check_file "frontend/vite.config.ts"        "前端构建配置"
check_file "deploy/docker/docker-compose.yml" "本地依赖编排"

check_dir  "storage/logs" "日志目录" "no"
check_dir  "storage/tmp"  "临时目录" "no"

# ---------------------------------------------------------------------
# 四、端口占用
# ---------------------------------------------------------------------
section "四、待用端口占用情况"

check_port() {
  # 用法：check_port PORT 用途
  local port="$1" desc="$2"
  local occupied=""

  if [ -n "${PY_PROBE}" ]; then
    occupied="$("${PY_PROBE}" - "${port}" <<'PYEOF' 2>/dev/null
import socket, sys
port = int(sys.argv[1])
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(1.0)
try:
    s.bind(("127.0.0.1", port))
    print("FREE")
except OSError:
    print("BUSY")
finally:
    s.close()
PYEOF
)"
  fi

  case "${occupied}" in
    FREE) ok "端口 ${port} 空闲  — ${desc}" ;;
    BUSY) warn "端口 ${port} 被占用  — ${desc}（如非本系统进程，需改配置）" ;;
    *)    info "端口 ${port} 未检测  — ${desc}" ;;
  esac
}

check_port 8000 "后端 FastAPI"
check_port 5173 "前端 Vite 开发服务器"
check_port 9100 "模拟审批系统"

# ---------------------------------------------------------------------
# 五、汇总
# ---------------------------------------------------------------------
section "预检结果"

printf "  失败项：${C_RED}%d${C_RESET}    告警项：${C_YELLOW}%d${C_RESET}\n\n" "${FAIL_COUNT}" "${WARN_COUNT}"

if [ "${FAIL_COUNT}" -eq 0 ]; then
  printf "  ${C_GREEN}${C_BOLD}必需项全部通过，可以继续部署。${C_RESET}\n\n"
  printf "  后续步骤：\n"
  printf "    1. cd backend && pip install -r requirements.txt\n"
  printf "    2. python scripts/init_mongodb.py\n"
  printf "    3. python scripts/init_milvus_vectors.py\n"
  printf "    4. python scripts/system_check.py\n"
  printf "\n  详细说明见 docs/DEPLOYMENT.md\n\n"
  exit 0
else
  printf "  ${C_RED}${C_BOLD}存在 %d 项必需依赖未满足，请按上方提示逐项修复后重跑本脚本。${C_RESET}\n\n" "${FAIL_COUNT}"
  printf "  排查指引见 docs/DEPLOYMENT.md 第八章「常见问题排查」\n\n"
  exit 1
fi
