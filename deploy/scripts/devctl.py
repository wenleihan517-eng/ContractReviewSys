# D:/ContractReviewSys-合同审批智能审查项目/deploy/scripts/devctl.py
"""
开发环境进程管理器 — 一键启动/停止/查看全系统服务

【脚本职责】
    合同审批智能审查系统在开发时需要同时运行多个服务：

        ┌─────────────────────────────────────────────────────────┐
        │ 基础设施（需外部提供，本脚本只做连通性探测，不负责拉起）  │
        │   MongoDB   127.0.0.1:27017                             │
        │   MinIO     127.0.0.1:9000 (控制台 9001)                │
        │   Milvus    192.168.10.130:7000                         │
        │   Embedding 127.0.0.1:9997                              │
        │   LLM       127.0.0.1:8001                              │
        ├─────────────────────────────────────────────────────────┤
        │ 应用服务（本脚本负责生命周期）                            │
        │   backend        127.0.0.1:8000   FastAPI + LangGraph    │
        │   frontend       127.0.0.1:5173   Vite Dev Server        │
        │   mock-approval  127.0.0.1:9100   模拟审批系统（演示用）  │
        └─────────────────────────────────────────────────────────┘

【为什么需要这个脚本】
    在 Windows 上用 Git Bash / CMD 手工同时起三个服务很麻烦：
        - 要开三个终端窗口，关的时候容易漏
        - 后台跑起来后难以找回 PID，杀不干净
        - 不知道日志跑哪去了

    本脚本用统一的 PID 文件 + 日志文件目录管理，做到：
        start  → 顺序拉起，逐个做健康检查，失败立即报错并回滚
        stop   → 按 PID 精确终止进程树，清理 PID 文件
        status → 一次性看清谁在跑、端口通不通
        logs   → 直接 tail 指定服务的日志

【设计要点】
    1. **只用标准库**：subprocess / socket / urllib / json / signal，
       不引入 psutil 等第三方依赖，保证任何 Python 3.10+ 都能直接跑。
    2. **PID 文件 + 端口双重判定**：PID 文件可能因为强杀而残留，
       因此 status 判定以"端口是否可连"为准，PID 文件仅用于 stop 定位。
    3. **子进程脱离父进程**：Windows 上用 DETACHED_PROCESS + CREATE_NEW_PROCESS_GROUP，
       POSIX 上用 start_new_session，避免终端关闭导致子进程被带走。
    4. **启动即验证**：每个服务启动后轮询健康检查，超时则报错并提示看日志，
       而不是"启动命令返回了就当成功"。
    5. **不静默失败**：任何一步失败都给出具体原因与下一步动作。

【运行方式】
    cd deploy/scripts

    # 查看状态
    python devctl.py status

    # 启动全部（backend + frontend + mock-approval）
    python devctl.py start

    # 只启动后端
    python devctl.py start backend

    # 停止全部
    python devctl.py stop

    # 停止并清理日志/PID
    python devctl.py stop --clean

    # 重启某个服务
    python devctl.py restart backend

    # 查看日志
    python devctl.py logs backend
    python devctl.py logs backend -n 200
    python devctl.py logs -f backend

    # 跳过应用服务，只做基础设施连通性探测
    python devctl.py probe

【退出码】
    0 = 操作成功
    1 = 操作失败（含启动超时、端口被占用等）
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

# ======================================================================
# 一、路径与常量
# ======================================================================

# 本脚本所在目录
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# deploy 目录
DEPLOY_DIR = os.path.dirname(SCRIPT_DIR)

# 项目根目录
PROJECT_ROOT = os.path.dirname(DEPLOY_DIR)

# backend / frontend 目录
BACKEND_DIR = os.path.join(PROJECT_ROOT, "backend")
FRONTEND_DIR = os.path.join(PROJECT_ROOT, "frontend")
MOCK_APPROVAL_DIR = os.path.join(DEPLOY_DIR, "mock_approval")

# 运行时目录：PID 文件与日志
RUNTIME_DIR = os.path.join(DEPLOY_DIR, "runtime")
PID_DIR = os.path.join(RUNTIME_DIR, "pids")
LOG_DIR = os.path.join(RUNTIME_DIR, "logs")

# 判定 Windows 平台（路径与进程创建方式不同）
IS_WINDOWS = os.name == "nt"


# ======================================================================
# 二、服务定义（权威配置表）
# ======================================================================
# 每个服务的字段说明：
#   label          —— 展示名
#   port           —— 用于状态判定的监听端口
#   cwd            —— 工作目录
#   cmd_builder    —— 返回启动命令列表的函数（延迟求值，便于读取虚拟环境路径）
#   health_url     —— 健康检查地址；None 表示仅用端口判定
#   infra          —— True 表示是外部基础设施，本脚本不负责启动
#   startup_hint   —— 启动失败时的提示
#
# 说明：把命令写成函数而不是常量，是为了在真正启动时才去解析
#       Python/Node 解释器路径——这样 status/probe 这类只读操作
#       即使在解释器缺失的情况下也能正常工作。

def _python_exe() -> str:
    """
    返回后端应使用的 Python 解释器路径

    优先级：
        1. backend/.venv（项目自带的虚拟环境，推荐）
        2. 当前运行本脚本的解释器（sys.executable）
        3. 裸 python（依赖 PATH）

    返回：
        解释器绝对路径或命令名
    """
    candidates = [
        os.path.join(BACKEND_DIR, ".venv", "Scripts", "python.exe"),
        os.path.join(BACKEND_DIR, ".venv", "bin", "python"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path

    # 退回到当前解释器（说明开发者在用全局环境）
    if sys.executable:
        return sys.executable

    return "python"


def _npm_cmd() -> str:
    """
    返回 npm 命令

    Windows 上 npm 是 npm.cmd，直接调用 "npm" 在 subprocess 里会报
    FileNotFoundError，因此需要显式加后缀。

    返回：
        npm 命令名
    """
    return "npm.cmd" if IS_WINDOWS else "npm"


def _backend_cmd() -> List[str]:
    """后端启动命令：uvicorn 加载 app.main:app，开启热重载"""
    return [
        _python_exe(),
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        "8000",
        "--reload",
    ]


def _frontend_cmd() -> List[str]:
    """前端启动命令：Vite 开发服务器"""
    return [_npm_cmd(), "run", "dev"]


def _mock_approval_cmd() -> List[str]:
    """模拟审批系统启动命令：独立 FastAPI 应用"""
    return [
        _python_exe(),
        "-m",
        "uvicorn",
        "main:app",
        "--host",
        "127.0.0.1",
        "--port",
        "9100",
    ]


# 应用服务（本脚本管理生命周期）
APP_SERVICES: Dict[str, Dict[str, Any]] = {
    "mock-approval": {
        "label": "模拟审批系统",
        "port": 9100,
        "cwd": MOCK_APPROVAL_DIR,
        "cmd_builder": _mock_approval_cmd,
        "health_url": "http://127.0.0.1:9100/health",
        "infra": False,
        # 先起模拟审批：后端启动时若审批系统已在，能少一轮重试
        "start_order": 1,
        "startup_hint": (
            "确认 deploy/mock_approval/main.py 存在；\n"
            "  确认 fastapi 与 uvicorn 已安装"
        ),
    },
    "backend": {
        "label": "后端服务",
        "port": 8000,
        "cwd": BACKEND_DIR,
        "cmd_builder": _backend_cmd,
        "health_url": "http://127.0.0.1:8000/health",
        "infra": False,
        "start_order": 2,
        "startup_hint": (
            "最常见原因是基础设施未就绪：\n"
            "  先执行 python devctl.py probe 确认 MongoDB/MinIO/Milvus 可达；\n"
            "  再执行 backend/scripts/system_check.py 获取详细诊断"
        ),
    },
    "frontend": {
        "label": "前端服务",
        "port": 5173,
        "cwd": FRONTEND_DIR,
        "cmd_builder": _frontend_cmd,
        "health_url": None,  # Vite 无统一 /health，用端口判定
        "infra": False,
        "start_order": 3,
        "startup_hint": (
            "确认已安装依赖：cd frontend && npm install\n"
            "  首次启动 Vite 需要预构建依赖，等待 10~30 秒属正常"
        ),
    },
}

# 基础设施（只探测，不管理生命周期）
INFRA_SERVICES: Dict[str, Dict[str, Any]] = {
    "mongodb": {
        "label": "MongoDB",
        "host": "127.0.0.1",
        "port": 27017,
        "startup_hint": "net start MongoDB  或  cd deploy/docker && docker compose up -d mongodb",
    },
    "minio": {
        "label": "MinIO",
        "host": "127.0.0.1",
        "port": 9000,
        "startup_hint": "cd deploy/docker && docker compose up -d minio",
    },
    "milvus": {
        "label": "Milvus",
        "host": "192.168.10.130",
        "port": 7000,
        "startup_hint": "在虚拟机 192.168.10.130 上确认 Milvus 容器已启动，且 7000 端口已映射",
    },
    "embedding": {
        "label": "Embedding(BGE-M3)",
        "host": "127.0.0.1",
        "port": 9997,
        "startup_hint": "启动本地 BGE-M3 向量化服务，需兼容 OpenAI /v1/embeddings 协议",
    },
    "llm": {
        "label": "LLM",
        "host": "127.0.0.1",
        "port": 8001,
        "startup_hint": "启动本地大模型服务（OpenAI 兼容协议）。注意：LLM 不可用不阻塞核心审查流程",
    },
}


# ======================================================================
# 三、通用工具
# ======================================================================

def _ensure_dirs() -> None:
    """确保运行时目录存在（PID 与日志）"""
    os.makedirs(PID_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)


def _console_encoding() -> str:
    """
    返回外部命令输出应使用的解码编码

    背景：
        Windows 上 tasklist / netstat / taskkill 输出的是**本地代码页**字节，
        在中文系统上是 GBK（cp936），而不是 UTF-8。
        如果 subprocess 用默认 UTF-8 解码，会抛：
            UnicodeDecodeError: 'utf-8' codec can't decode byte 0xbb ...
        而且这个异常发生在 subprocess 内部的读取线程里，
        表现为"输出丢失 + 后台线程抛一堆 traceback"，
        很难从错误信息定位到真正原因。

        实测触发点：第 3 行开始的中文进程描述
        （如 "信息: 没有运行的任务匹配指定标准。"）。

    处理：
        优先取 Windows 的 ANSI 代码页（GetACP），拿不到则退回 locale 编码，
        最后兜底 utf-8。配合 errors="replace"，即使编码判断失误也不会崩。

    返回：
        编码名称字符串
    """
    if not IS_WINDOWS:
        # POSIX 下外部命令基本都输出 UTF-8
        return "utf-8"

    # Windows：优先用系统的 ANSI 代码页
    try:
        import ctypes

        acp = ctypes.windll.kernel32.GetACP()
        if acp:
            return f"cp{acp}"
    except Exception:  # noqa: BLE001
        pass

    # 退回到 locale 首选编码
    try:
        import locale

        enc = locale.getpreferredencoding(False)
        if enc:
            return enc
    except Exception:  # noqa: BLE001
        pass

    return "utf-8"


def _pid_file(service: str) -> str:
    """返回指定服务的 PID 文件绝对路径"""
    return os.path.join(PID_DIR, f"{service}.pid")


def _log_file(service: str) -> str:
    """返回指定服务的日志文件绝对路径"""
    return os.path.join(LOG_DIR, f"{service}.log")


def _read_pid(service: str) -> Optional[int]:
    """
    读取 PID 文件

    说明：
        PID 文件可能因强杀而残留，因此读到 PID 后**不代表进程还活着**，
        调用方仍需用 _is_pid_alive() 或端口探测二次确认。

    返回：
        PID 整数；文件不存在或内容非法时返回 None
    """
    path = _pid_file(service)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fp:
            return int(fp.read().strip())
    except (ValueError, OSError):
        return None


def _write_pid(service: str, pid: int) -> None:
    """写入 PID 文件"""
    _ensure_dirs()
    with open(_pid_file(service), "w", encoding="utf-8") as fp:
        fp.write(str(pid))


def _remove_pid(service: str) -> None:
    """删除 PID 文件（幂等）"""
    path = _pid_file(service)
    if os.path.isfile(path):
        try:
            os.remove(path)
        except OSError:
            pass


def _is_pid_alive(pid: int) -> bool:
    """
    判断 PID 对应进程是否存活

    说明：
        Windows 与 POSIX 的判定方式不同：
            POSIX   —— os.kill(pid, 0) 不发信号只做权限/存在性检查
            Windows —— os.kill(pid, 0) 行为不可靠，
                       改用 tasklist 过滤 PID（无需 psutil）

    参数：
        pid —— 待检查的进程号

    返回：
        True 表示存活
    """
    if pid <= 0:
        return False

    if IS_WINDOWS:
        try:
            # /FI 过滤 + /NH 不要表头，CSV 输出便于判定
            completed = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                capture_output=True,
                # Windows 的 tasklist/netstat 输出本地代码页字节（中文系统为 GBK），
                # 用 UTF-8 硬解会抛 UnicodeDecodeError；此处显式指定编码并容错。
                encoding=_console_encoding(),
                errors="replace",
                timeout=10,
                check=False,
            )
            out = (completed.stdout or "").strip()
            # 进程不存在时输出为 "信息: 没有运行的任务匹配指定标准。" 或空
            return str(pid) in out
        except Exception:  # noqa: BLE001
            return False

    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # 进程存在但无权限发信号 → 视为存活
        return True
    except OSError:
        return False


def _process_name(pid: int) -> str:
    """
    查询进程名

    说明：
        用于区分"端口被本项目的服务占用"与"端口被无关程序占用"。
        后者是常见现象，例如 VMware 的 vmnat.exe 会占用 8000 端口，
        若不加以区分，工具会误报"后端已在运行"。

    参数：
        pid —— 进程号

    返回：
        进程名（如 python.exe / node.exe）；查询失败返回空字符串
    """
    if pid <= 0:
        return ""

    try:
        if IS_WINDOWS:
            completed = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                capture_output=True,
                # Windows 的 tasklist/netstat 输出本地代码页字节（中文系统为 GBK），
                # 用 UTF-8 硬解会抛 UnicodeDecodeError；此处显式指定编码并容错。
                encoding=_console_encoding(),
                errors="replace",
                timeout=10,
                check=False,
            )
            out = (completed.stdout or "").strip()
            # CSV 形如："python.exe","19920","Console","1","22,648 K"
            if out.startswith('"'):
                return out.split('","')[0].strip('"')
            return ""
        # POSIX
        completed = subprocess.run(
            ["ps", "-p", str(pid), "-o", "comm="],
            capture_output=True,
            encoding=_console_encoding(),
            errors="replace",
            timeout=10,
            check=False,
        )
        return (completed.stdout or "").strip()

    except Exception:  # noqa: BLE001
        return ""


def _looks_like_project_process(pid: int) -> bool:
    """
    粗略判断某 PID 是否可能是本项目的服务进程

    说明：
        本项目的服务由 python（后端/模拟审批）与 node（前端）启动，
        因此进程名落在 {python, python.exe, python3, node, node.exe}
        即认为"可能是自己人"。

        这是一个**启发式**判断，只用于生成更准确的状态描述，
        不参与任何终止决策——终止永远优先使用 PID 文件。

    参数：
        pid —— 进程号

    返回：
        True 表示进程名符合本项目服务的特征
    """
    name = _process_name(pid).lower()
    if not name:
        return False
    # 去掉可能的扩展名后比较
    stem = name.rsplit(".", 1)[0]
    return stem in ("python", "python3", "node")


def _port_open(host: str, port: int, timeout: float = 1.5) -> bool:
    """
    TCP 探测端口是否可连接

    参数：
        host    —— 目标主机
        port    —— 目标端口
        timeout —— 连接超时秒数

    返回：
        True 表示可建立 TCP 连接
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, socket.timeout):
        return False


def _http_ok(url: str, timeout: float = 5.0) -> Tuple[bool, str]:
    """
    请求健康检查地址

    说明：
        把任何 HTTP 响应（含 4xx/5xx）都视为"服务已在监听"——
        因为我们的目的是判断进程是否起来了，而不是判断业务是否正常。
        真正的业务健康判断由 backend/scripts/system_check.py 负责。

    参数：
        url     —— 健康检查地址
        timeout —— 超时秒数

    返回：
        (是否可用, 描述文本)
    """
    try:
        request = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return True, f"HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        # 有 HTTP 响应就说明服务已监听
        return True, f"HTTP {exc.code}"
    except urllib.error.URLError as exc:
        return False, str(exc.reason)[:120] if exc.reason else "URLError"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {str(exc)[:120]}"


def _kill_tree(pid: int, label: str) -> bool:
    """
    终止进程及其子进程

    说明：
        uvicorn --reload 与 vite 都会派生子进程，
        只杀父进程会留下孤儿进程继续占端口，因此必须杀进程树。

        Windows —— taskkill /T /F 递归终止
        POSIX   —— 对进程组发 SIGTERM，宽限后 SIGKILL

    参数：
        pid   —— 目标进程号
        label —— 展示名，仅用于日志

    返回：
        True 表示终止命令执行成功
    """
    if IS_WINDOWS:
        try:
            completed = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                # Windows 的 tasklist/netstat 输出本地代码页字节（中文系统为 GBK），
                # 用 UTF-8 硬解会抛 UnicodeDecodeError；此处显式指定编码并容错。
                encoding=_console_encoding(),
                errors="replace",
                timeout=30,
                check=False,
            )
            ok = completed.returncode == 0
            if not ok:
                err = (completed.stderr or completed.stdout or "").strip()
                print(f"    taskkill 返回 {completed.returncode}：{err[:200]}")
            return ok
        except Exception as exc:  # noqa: BLE001
            print(f"    taskkill 执行异常：{exc}")
            return False

    # POSIX：先温和后强硬
    try:
        pgid = os.getpgid(pid)
    except OSError:
        pgid = None

    try:
        if pgid is not None:
            os.killpg(pgid, signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        print(f"    SIGTERM 失败：{exc}")

    # 宽限 5 秒等待优雅退出
    for _ in range(50):
        if not _is_pid_alive(pid):
            return True
        time.sleep(0.1)

    try:
        if pgid is not None:
            os.killpg(pgid, signal.SIGKILL)
        else:
            os.kill(pid, signal.SIGKILL)
    except OSError:
        pass

    return True


def _resolve_listen_pid(
    launcher_pid: int,
    port: int,
    timeout: float = 15.0,
) -> int:
    """
    解析出真正在监听目标端口的进程 PID

    【为什么需要这个函数 —— 这是一个已实测确认的坑】
        `python -m uvicorn` 并非直接监听端口：
        它会先启动一个"启动器"进程，再由启动器创建真正的 server 进程。
        两者 PID 不同。实测（本机 Python 3.13 + uvicorn）：

            Popen.pid          = 14448   ← 启动器（很快退出/不再监听）
            实际监听 9101 的进程 = 4752    ← 真正的服务进程

        后果：
            如果 PID 文件记录的是 Popen.pid（启动器），
            那么 stop 时杀掉启动器，真正的 server 进程会变成孤儿继续占端口，
            表现为"执行了 stop，端口却还通"——这正是最难排查的一类问题。

        因此启动后必须按端口反查真实 PID 并写入 PID 文件。

    处理策略：
        1. 先按端口反查监听进程（最可靠）
        2. 反查不到时退回 launcher_pid，至少保留一个可终止的目标

    参数：
        launcher_pid —— Popen 返回的启动器 PID
        port         —— 目标端口
        timeout      —— 反查等待上限（秒），因为监听建立需要时间

    返回：
        真实监听进程 PID；反查失败则返回 launcher_pid
    """
    deadline = time.time() + timeout

    while time.time() < deadline:
        found = _find_pid_by_port(port)
        if found and found > 0:
            return found
        time.sleep(0.3)

    # 反查失败：退回启动器 PID，并在调用方提示
    return launcher_pid


def _spawn(cmd: List[str], cwd: str, service: str) -> subprocess.Popen:
    """
    以脱离父进程的方式启动子进程

    说明：
        后端与前端都是长期运行的服务，必须脱离当前终端，
        否则关掉启动它所在的终端会把服务一起带走。

        Windows —— DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
                   （两者组合使子进程不继承控制台，也不受 Ctrl+C 影响）
        POSIX   —— start_new_session=True（等价于 setsid）

        标准输出与错误统一重定向到日志文件，便于事后排查。

    参数：
        cmd     —— 命令参数列表
        cwd     —— 工作目录
        service —— 服务名（决定日志文件路径）

    返回：
        Popen 对象

    异常：
        FileNotFoundError —— 可执行文件不存在
        OSError           —— 其他启动失败
    """
    _ensure_dirs()
    log_path = _log_file(service)

    # 以追加方式打开日志；每行带时间戳的说明写在日志头部
    log_fp = open(log_path, "a", encoding="utf-8", errors="replace")
    log_fp.write(
        f"\n\n{'=' * 70}\n"
        f"[devctl] 启动服务 {service} | {datetime.now():%Y-%m-%d %H:%M:%S}\n"
        f"[devctl] 工作目录：{cwd}\n"
        f"[devctl] 启动命令：{' '.join(cmd)}\n"
        f"{'=' * 70}\n"
    )
    log_fp.flush()

    creationflags = 0
    kwargs: Dict[str, Any] = {}

    if IS_WINDOWS:
        # DETACHED_PROCESS：不继承父进程控制台
        # CREATE_NEW_PROCESS_GROUP：独立进程组，便于整组终止
        creationflags = 0x00000008 | 0x00000200
    else:
        kwargs["start_new_session"] = True

    # 关键：传递环境变量标记，供服务自身识别（如后端可据此调整日志级别）
    env = os.environ.copy()
    env["CR_DEVCTL"] = "1"

    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdout=log_fp,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        creationflags=creationflags,
        env=env,
        **kwargs,
    )

    # 日志文件句柄交给子进程接管后，父进程侧可以关闭引用
    log_fp.close()

    return proc


def _wait_ready(service: str, cfg: Dict[str, Any], timeout: float) -> Tuple[bool, str]:
    """
    轮询等待服务就绪

    判定顺序：
        1. 若配置了 health_url，优先做 HTTP 探测
        2. 否则用端口探测

    参数：
        service —— 服务名
        cfg     —— 服务配置字典
        timeout —— 最长等待秒数

    返回：
        (是否就绪, 描述文本)
    """
    host = "127.0.0.1"
    port = cfg["port"]
    health_url = cfg.get("health_url")

    deadline = time.time() + timeout
    last_detail = "尚无响应"

    while time.time() < deadline:
        if health_url:
            ok, detail = _http_ok(health_url, timeout=3.0)
            last_detail = detail
            if ok:
                return True, f"{health_url} → {detail}"
        else:
            if _port_open(host, port):
                return True, f"{host}:{port} 已监听"

        time.sleep(0.5)

    return False, last_detail


# ======================================================================
# 四、probe — 基础设施连通性探测
# ======================================================================

def cmd_probe() -> int:
    """
    探测全部基础设施的端口连通性

    说明：
        本命令只做 TCP 探测，不判断业务是否健康。
        目的是在启动应用服务**之前**快速确认依赖是否就绪，
        避免后端启动后才发现 MongoDB 连不上。

    返回：
        0 = 全部可达；1 = 存在不可达项
    """
    print()
    print("=" * 74)
    print("  基础设施连通性探测")
    print("=" * 74)

    unreachable: List[str] = []

    for name, cfg in INFRA_SERVICES.items():
        host = cfg["host"]
        port = cfg["port"]
        label = cfg["label"]

        started = time.perf_counter()
        ok = _port_open(host, port)
        elapsed_ms = (time.perf_counter() - started) * 1000

        if ok:
            print(f"  [ OK ] {label:<22} {host}:{port:<6} 可达（{elapsed_ms:.0f}ms）")
        else:
            print(f"  [FAIL] {label:<22} {host}:{port:<6} 不可达")
            print(f"         → {cfg['startup_hint']}")
            unreachable.append(label)

    print("-" * 74)

    if not unreachable:
        print("  全部基础设施可达，可以启动应用服务。")
        return 0

    print(f"  存在 {len(unreachable)} 项不可达：{', '.join(unreachable)}")
    print()
    print("  提示：LLM 不可用不阻塞核心审查流程（规则引擎独立判定）；")
    print("        其余组件不可用会导致后端启动失败或功能缺失。")
    return 1


# ======================================================================
# 五、status — 查看服务状态
# ======================================================================

def cmd_status() -> int:
    """
    打印全系统状态：基础设施 + 应用服务

    说明：
        应用服务的"运行中"判定以**端口可连**为准，而不是 PID 文件存在，
        因为强杀后 PID 文件会残留，只看文件会误报。

    返回：
        0 = 全部正常；1 = 存在未运行项
    """
    print()
    print("=" * 74)
    print("  系统状态总览")
    print(f"  检查时间：{datetime.now():%Y-%m-%d %H:%M:%S}")
    print("=" * 74)

    # --- 基础设施 ---
    print("\n-- 基础设施（需外部提供）" + "-" * 48)
    infra_down: List[str] = []
    for name, cfg in INFRA_SERVICES.items():
        ok = _port_open(cfg["host"], cfg["port"])
        mark = "[ OK ]" if ok else "[DOWN]"
        print(f"  {mark} {cfg['label']:<22} {cfg['host']}:{cfg['port']}")
        if not ok:
            infra_down.append(cfg["label"])

    # --- 应用服务 ---
    print("\n-- 应用服务（本脚本管理）" + "-" * 50)
    app_down: List[str] = []
    foreign: List[str] = []

    for name, cfg in APP_SERVICES.items():
        port = cfg["port"]
        port_ok = _port_open("127.0.0.1", port)
        pid = _read_pid(name)
        pid_alive = _is_pid_alive(pid) if pid else False

        if not port_ok:
            if pid_alive:
                # PID 活着但端口不通：多为启动中或启动失败后卡住
                mark = "[WAIT]"
                extra = f"PID={pid} 存活但端口未监听（可能仍在启动）"
                app_down.append(name)
            else:
                mark = "[STOP]"
                extra = "未运行"
                app_down.append(name)
            print(f"  {mark} {cfg['label']:<22} 127.0.0.1:{port:<6} {extra}")
            continue

        # 端口在监听：分辨是本项目的服务还是别的程序
        if pid_alive:
            print(
                f"  [RUN ] {cfg['label']:<22} 127.0.0.1:{port:<6} "
                f"PID={pid}（由本脚本启动）"
            )
            continue

        owner = _find_pid_by_port(port)
        owner_name = _process_name(owner) if owner else ""

        if owner and not _looks_like_project_process(owner):
            # 端口被无关程序占用，这是必须让用户知道的信息：
            # 它会让本项目的服务根本起不来（端口冲突）。
            print(
                f"  [BUSY] {cfg['label']:<22} 127.0.0.1:{port:<6} "
                f"被 {owner_name}(PID={owner}) 占用"
            )
            foreign.append(f"{cfg['label']}（{owner_name}）")
            app_down.append(name)
        else:
            print(
                f"  [RUN ] {cfg['label']:<22} 127.0.0.1:{port:<6} "
                "端口监听中（非本脚本启动，PID 未知）"
            )

    print("\n-- 访问入口" + "-" * 60)
    print("  前端界面    http://127.0.0.1:5173")
    print("  后端接口    http://127.0.0.1:8000/docs")
    print("  模拟审批    http://127.0.0.1:9100/docs")

    print("-" * 74)

    if not infra_down and not app_down:
        print("  全部服务运行正常。")
        return 0

    if infra_down:
        print(f"  基础设施未就绪（{len(infra_down)} 项）：{', '.join(infra_down)}")
    if app_down:
        print(f"  应用服务未运行（{len(app_down)} 项）：{', '.join(app_down)}")
        print("  启动命令：python devctl.py start")
    if foreign:
        print()
        print("  ⚠️  端口被无关程序占用（会导致本项目服务无法启动）：")
        for item in foreign:
            print(f"      · {item}")
        print("      → 处理方式：终止占用进程，或修改本项目端口配置")

    return 1


# ======================================================================
# 六、start — 启动服务
# ======================================================================

def _start_one(name: str, cfg: Dict[str, Any], timeout: float) -> bool:
    """
    启动单个服务并等待就绪

    参数：
        name    —— 服务名
        cfg     —— 服务配置
        timeout —— 就绪等待上限（秒）

    返回：
        True 表示已就绪
    """
    label = cfg["label"]
    print(f"\n  ▶ 启动 {label}（{name}）")

    # --- 前置检查 1：工作目录存在 ---
    cwd = cfg["cwd"]
    if not os.path.isdir(cwd):
        print(f"    [FAIL] 工作目录不存在：{cwd}")
        return False

    # --- 前置检查 2：端口是否已被占用 ---
    if _port_open("127.0.0.1", cfg["port"]):
        port = cfg["port"]
        own_pid = _read_pid(name)

        if own_pid and _is_pid_alive(own_pid):
            print(f"    [SKIP] 端口 {port} 已由本脚本启动的服务监听（PID={own_pid}）")
            return True

        owner = _find_pid_by_port(port)
        owner_name = _process_name(owner) if owner else ""

        if owner and not _looks_like_project_process(owner):
            # 关键：端口被无关程序占用时，必须明确报错而不是误判为"已运行"。
            # 否则用户会以为服务起来了，实际访问到的是别的程序。
            print(f"    [FAIL] 端口 {port} 被无关程序占用：{owner_name}(PID={owner})")
            print("           本项目的服务无法在该端口启动。")
            print("           处理方式（二选一）：")
            print(f"             a) 终止占用进程：taskkill /PID {owner} /T /F")
            print("                （请先确认该进程确实与业务无关）")
            print(f"             b) 修改本项目端口配置，避开 {port}")
            return False

        # 端口被某个 python/node 占用但 PID 文件缺失：
        # 可能是上一次非本脚本启动的实例，或 PID 文件被清理过。
        print(f"    [WARN] 端口 {port} 已被占用（"
              f"{owner_name or '未知进程'}{f', PID=' + str(owner) if owner else ''}）")
        print("           若确为上次残留实例，可先执行：python devctl.py stop")
        return False

    # --- 前置检查 3：前端需先有 node_modules ---
    if name == "frontend":
        nm = os.path.join(cwd, "node_modules")
        if not os.path.isdir(nm):
            print("    [FAIL] 未安装前端依赖（node_modules 不存在）")
            print("           → cd frontend && npm install")
            return False

    # --- 前置检查 4：后端依赖是否可导入 ---
    if name == "backend":
        py = _python_exe()
        probe = subprocess.run(
            [py, "-c", "import fastapi, uvicorn, langgraph, motor, minio, pymilvus"],
            cwd=cwd,
            capture_output=True,
            encoding=_console_encoding(),
            errors="replace",
            timeout=60,
            check=False,
        )
        if probe.returncode != 0:
            err = (probe.stderr or "").strip().split("\n")[-1][:200]
            print(f"    [FAIL] 后端依赖不完整：{err}")
            print("           → cd backend && pip install -r requirements.txt")
            return False

    # --- 启动 ---
    cmd = cfg["cmd_builder"]()
    print(f"    命令：{' '.join(cmd)}")

    try:
        proc = _spawn(cmd, cwd, name)
    except FileNotFoundError as exc:
        print(f"    [FAIL] 可执行文件不存在：{exc}")
        print(f"           → {cfg['startup_hint']}")
        return False
    except Exception as exc:  # noqa: BLE001
        print(f"    [FAIL] 启动失败：{type(exc).__name__}: {exc}")
        print(f"           → {cfg['startup_hint']}")
        return False

    # 注意：不能直接把 Popen.pid 写进 PID 文件。
    # `python -m uvicorn` 会派生子进程，Popen.pid 指向的是启动器，
    # 真正监听端口的是它的子进程。按端口反查真实 PID 才能保证 stop 有效。
    listen_pid = _resolve_listen_pid(proc.pid, cfg["port"], timeout=min(15.0, timeout))

    if listen_pid != proc.pid:
        print(f"    启动器 PID={proc.pid}，实际监听进程 PID={listen_pid}")
    else:
        # 反查失败或恰好相同：给出提示，便于排查 stop 不生效的情况
        print(f"    PID={proc.pid}（未能按端口反查到独立子进程）")

    _write_pid(name, listen_pid)
    print(f"    等待就绪（最长 {timeout:.0f}s）...")

    ok, detail = _wait_ready(name, cfg, timeout)

    # 子进程若已退出，说明启动即失败，直接给出日志尾部
    if proc.poll() is not None:
        _remove_pid(name)
        print(f"    [FAIL] 进程已退出（退出码 {proc.returncode}）")
        _print_log_tail(name, lines=25)
        print(f"           → {cfg['startup_hint']}")
        return False

    if ok:
        print(f"    [ OK ] 已就绪：{detail}")
        return True

    print(f"    [WARN] 超时未就绪（{detail}）")
    print(f"           进程仍在运行（监听 PID={listen_pid}），可能是启动较慢")
    _print_log_tail(name, lines=25)
    print(f"           → {cfg['startup_hint']}")
    return False


def _print_log_tail(service: str, lines: int = 30) -> None:
    """
    打印指定服务日志的末尾若干行

    参数：
        service —— 服务名
        lines   —— 打印行数
    """
    path = _log_file(service)
    if not os.path.isfile(path):
        print("           （暂无日志文件）")
        return

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fp:
            content = fp.readlines()
    except OSError as exc:
        print(f"           （日志读取失败：{exc}）")
        return

    tail = content[-lines:]
    print(f"           ---- {os.path.basename(path)} 末尾 {len(tail)} 行 ----")
    for line in tail:
        print(f"           | {line.rstrip()}")
    print("           " + "-" * 40)


def _service_needs_infra(name: str) -> bool:
    """
    判断某个应用服务是否依赖外部基础设施

    说明：
        mock-approval 是一个纯内存的独立 FastAPI 应用，
        不连接 MongoDB / MinIO / Milvus / Embedding / LLM。
        因此在只启动它时，不应该因为"基础设施不可达"而警告或要求确认——
        那会让用户误以为必须先跑通全部基础设施，实际上是多余的阻碍。

    参数：
        name —— 应用服务名

    返回：
        True 表示该服务连接外部基础设施
    """
    # 目前只有 mock-approval 是自包含的；后端与前端都依赖基础设施
    return name != "mock-approval"


def _prompt_yes_no(question: str, default_no: bool = True) -> bool:
    """
    交互式确认（对非交互环境安全）

    说明：
        在 CI / 重定向 / 无 TTY 的环境下，input() 会抛 EOFError。
        默认做法是"按默认值处理并提示"，而不是让脚本崩掉——
        崩掉会掩盖真正的业务逻辑，也会让自动化脚本无法使用。

    参数：
        question   —— 提问文本
        default_no —— 读不到输入时是否按"否"处理

    返回：
        True 表示用户确认
    """
    try:
        answer = input(question).strip().lower()
    except EOFError:
        # 非交互环境：按默认值处理，并明确告知
        fallback = "否" if default_no else "是"
        print(f"（非交互环境，按默认值「{fallback}」处理）")
        return not default_no

    return answer in ("y", "yes")


def cmd_start(targets: Optional[List[str]], timeout: float, skip_probe: bool) -> int:
    """
    启动应用服务

    参数：
        targets    —— 指定服务名列表；None 表示全部
        timeout    —— 每个服务的就绪等待上限（秒）
        skip_probe —— True 表示跳过基础设施预探测

    返回：
        0 = 全部成功；1 = 存在失败
    """
    print()
    print("=" * 74)
    print("  启动应用服务")
    print("=" * 74)

    # --- 确定启动顺序（放在探测之前，因为探测是否必要取决于目标服务）---
    if targets:
        unknown = [t for t in targets if t not in APP_SERVICES]
        if unknown:
            print(f"\n  [FAIL] 未知服务名：{', '.join(unknown)}")
            print(f"         可选值：{', '.join(APP_SERVICES.keys())}")
            return 1
        ordered = sorted(targets, key=lambda n: APP_SERVICES[n]["start_order"])
    else:
        ordered = sorted(APP_SERVICES.keys(), key=lambda n: APP_SERVICES[n]["start_order"])

    # 仅当本次要启动的服务中**有依赖基础设施的**，预探测才有意义
    needs_infra = any(_service_needs_infra(n) for n in ordered)

    # --- 基础设施预探测（非阻塞，仅提示）---
    if not skip_probe and needs_infra:
        print("\n  步骤 1/2：基础设施预探测")
        infra_bad: List[str] = []
        for name, cfg in INFRA_SERVICES.items():
            ok = _port_open(cfg["host"], cfg["port"])
            mark = "OK  " if ok else "DOWN"
            print(f"    [{mark}] {cfg['label']:<22} {cfg['host']}:{cfg['port']}")
            if not ok:
                infra_bad.append(cfg["label"])

        if infra_bad:
            print()
            print(f"    [WARN] {len(infra_bad)} 项基础设施不可达：{', '.join(infra_bad)}")
            print("           待启动的服务中包含依赖这些组件的服务，")
            print("           它们启动时会因依赖初始化失败而退出。")
            print("           建议先修复，或加 --skip-probe 跳过本检查。")
            print()
            if not _prompt_yes_no("           仍要继续启动吗？[y/N] ", default_no=True):
                print("           已取消。")
                return 1
        else:
            print("    全部基础设施可达。")
    elif not skip_probe:
        print("\n  步骤 1/2：基础设施预探测")
        print("    待启动的服务均为自包含组件，无需外部基础设施，已跳过探测。")

    print(f"\n  步骤 2/2：启动服务（顺序：{' → '.join(ordered)}）")

    results: Dict[str, bool] = {}
    for name in ordered:
        results[name] = _start_one(name, APP_SERVICES[name], timeout)

    # --- 汇总 ---
    print()
    print("=" * 74)
    print("  启动结果")
    print("=" * 74)

    failed = [n for n, ok in results.items() if not ok]
    for name in ordered:
        cfg = APP_SERVICES[name]
        ok = results[name]
        mark = "成功" if ok else "失败"
        print(f"  {cfg['label']:<16} {mark:<6} 端口 {cfg['port']}")

    print("-" * 74)

    if failed:
        print(f"  {len(failed)} 个服务启动失败，日志目录：{LOG_DIR}")
        for name in failed:
            print(f"    查看 {name} 日志：python devctl.py logs {name} -n 50")
        return 1

    print("  全部服务已启动。")
    print()
    print("  前端界面    http://127.0.0.1:5173")
    print("  后端接口    http://127.0.0.1:8000/docs")
    print("  模拟审批    http://127.0.0.1:9100/docs")
    print(f"  日志目录    {LOG_DIR}")
    print()
    print("  停止全部：python devctl.py stop")
    return 0


# ======================================================================
# 七、stop — 停止服务
# ======================================================================

def _stop_one(name: str, cfg: Dict[str, Any]) -> bool:
    """
    停止单个服务

    终止顺序：
        1. 优先用 PID 文件精确终止（含进程树）
        2. PID 文件缺失/失效时，按端口反查 PID 兜底
        3. 两者都无 → 已停止，视为成功

    参数：
        name —— 服务名
        cfg  —— 服务配置

    返回：
        True 表示已停止（含本来就未运行）
    """
    label = cfg["label"]
    port = cfg["port"]
    pid = _read_pid(name)

    # --- 情况 1：PID 有效 ---
    if pid and _is_pid_alive(pid):
        print(f"  ■ 停止 {label}（PID={pid}，端口 {port}）")
        _kill_tree(pid, label)
        _remove_pid(name)

        # 等待端口释放
        for _ in range(40):
            if not _port_open("127.0.0.1", port, timeout=0.5):
                print("    已停止。")
                return True
            time.sleep(0.25)

        print(f"    [WARN] 进程已终止但端口 {port} 仍被占用")
        print(f"           → 可能被其他程序占用，用 netstat -ano | findstr :{port} 查看")
        return True

    # --- 情况 2：PID 文件残留或缺失，但端口在监听 ---
    if _port_open("127.0.0.1", port):
        print(f"  ■ 停止 {label}（PID 文件无效，按端口 {port} 反查）")
        found = _find_pid_by_port(port)
        if found:
            print(f"    找到占用进程 PID={found}")
            _kill_tree(found, label)
            _remove_pid(name)
            time.sleep(1.5)
            if not _port_open("127.0.0.1", port, timeout=0.5):
                print("    已停止。")
                return True
            print(f"    [FAIL] 端口 {port} 仍被占用（可能需管理员权限）")
            print("           → 手工执行：taskkill /PID "
                  f"{found} /T /F")
            return False

        print(f"    [FAIL] 未能定位端口 {port} 的占用进程")
        print("           → 手工执行：netstat -ano | findstr :"
              f"{port}  然后 taskkill /PID <PID> /T /F")
        return False

    # --- 情况 3：本来就未运行 ---
    _remove_pid(name)
    print(f"  · {label} 未运行（端口 {port} 空闲）")
    return True


def _find_pid_by_port(port: int) -> Optional[int]:
    """
    按端口反查监听进程 PID

    说明：
        Windows 用 netstat -ano 解析，POSIX 用 lsof。
        两者都可能因权限不足而失败，失败时返回 None。

    参数：
        port —— 目标端口

    返回：
        PID 或 None
    """
    try:
        if IS_WINDOWS:
            completed = subprocess.run(
                ["netstat", "-ano"],
                capture_output=True,
                # Windows 的 tasklist/netstat 输出本地代码页字节（中文系统为 GBK），
                # 用 UTF-8 硬解会抛 UnicodeDecodeError；此处显式指定编码并容错。
                encoding=_console_encoding(),
                errors="replace",
                timeout=20,
                check=False,
            )
            for line in (completed.stdout or "").split("\n"):
                # 只关心 LISTENING 行
                if "LISTENING" not in line.upper():
                    continue
                parts = line.split()
                if len(parts) < 5:
                    continue
                # 形如：TCP  127.0.0.1:8000  0.0.0.0:0  LISTENING  12345
                local_addr = parts[1]
                if local_addr.rsplit(":", 1)[-1] == str(port):
                    try:
                        return int(parts[-1])
                    except ValueError:
                        continue
            return None

        # POSIX
        completed = subprocess.run(
            ["lsof", "-ti", f"tcp:{port}", "-s", "TCP:LISTEN"],
            capture_output=True,
            encoding=_console_encoding(),
            errors="replace",
            timeout=20,
            check=False,
        )
        out = (completed.stdout or "").strip()
        if out:
            return int(out.split("\n")[0].strip())
        return None

    except Exception:  # noqa: BLE001
        return None


def cmd_stop(targets: Optional[List[str]], clean: bool) -> int:
    """
    停止应用服务

    参数：
        targets —— 指定服务名列表；None 表示全部
        clean   —— True 表示同时清理 PID 与日志文件

    返回：
        0 = 全部停止成功；1 = 存在失败
    """
    print()
    print("=" * 74)
    print("  停止应用服务")
    print("=" * 74)

    if targets:
        unknown = [t for t in targets if t not in APP_SERVICES]
        if unknown:
            print(f"\n  [FAIL] 未知服务名：{', '.join(unknown)}")
            return 1
        # 停止时按逆序（先停前端，再停后端，最后停模拟审批）
        ordered = sorted(
            targets, key=lambda n: APP_SERVICES[n]["start_order"], reverse=True
        )
    else:
        ordered = sorted(
            APP_SERVICES.keys(),
            key=lambda n: APP_SERVICES[n]["start_order"],
            reverse=True,
        )

    print()
    results: Dict[str, bool] = {}
    for name in ordered:
        results[name] = _stop_one(name, APP_SERVICES[name])

    # --- 清理 ---
    if clean:
        print("\n  清理运行时文件 ...")
        _clean_runtime(keep_logs=False)

    failed = [n for n, ok in results.items() if not ok]

    print()
    print("-" * 74)
    if failed:
        print(f"  {len(failed)} 个服务未能完全停止：{', '.join(failed)}")
        return 1

    print("  全部服务已停止。")
    return 0


def _clean_runtime(keep_logs: bool) -> None:
    """
    清理 PID 与日志文件

    参数：
        keep_logs —— True 表示保留日志，只清 PID
    """
    removed = 0

    if os.path.isdir(PID_DIR):
        for name in os.listdir(PID_DIR):
            if name.endswith(".pid"):
                try:
                    os.remove(os.path.join(PID_DIR, name))
                    removed += 1
                except OSError:
                    pass

    if not keep_logs and os.path.isdir(LOG_DIR):
        for name in os.listdir(LOG_DIR):
            if name.endswith(".log"):
                try:
                    os.remove(os.path.join(LOG_DIR, name))
                    removed += 1
                except OSError:
                    pass

    print(f"    已清理 {removed} 个运行时文件。")


# ======================================================================
# 八、logs — 查看日志
# ======================================================================

def cmd_logs(service: str, lines: int, follow: bool) -> int:
    """
    查看指定服务的日志

    参数：
        service —— 服务名
        lines   —— 打印末尾行数
        follow  —— True 表示持续跟踪（Ctrl+C 退出）

    返回：
        0 = 成功；1 = 日志文件不存在
    """
    if service not in APP_SERVICES:
        print(f"[FAIL] 未知服务名：{service}")
        print(f"       可选值：{', '.join(APP_SERVICES.keys())}")
        return 1

    path = _log_file(service)
    if not os.path.isfile(path):
        print(f"[FAIL] 日志文件不存在：{path}")
        print("       该服务可能尚未启动过。启动命令：python devctl.py start")
        return 1

    print(f"\n-- {APP_SERVICES[service]['label']} 日志：{path} " + "-" * 20)
    print()

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fp:
            content = fp.readlines()
            tail = content[-lines:]
            for line in tail:
                sys.stdout.write(line)
            sys.stdout.flush()

            if follow:
                print("\n-- 持续跟踪中（Ctrl+C 退出）--")
                while True:
                    line = fp.readline()
                    if line:
                        sys.stdout.write(line)
                        sys.stdout.flush()
                    else:
                        time.sleep(0.3)

    except KeyboardInterrupt:
        print("\n已退出日志跟踪。")
        return 0
    except OSError as exc:
        print(f"[FAIL] 读取日志失败：{exc}")
        return 1

    return 0


# ======================================================================
# 九、restart — 重启
# ======================================================================

def cmd_restart(targets: Optional[List[str]], timeout: float) -> int:
    """
    重启服务（先停后启）

    参数：
        targets —— 指定服务名列表；None 表示全部
        timeout —— 每个服务的就绪等待上限（秒）

    返回：
        0 = 成功；1 = 失败
    """
    print()
    print("=" * 74)
    print("  重启应用服务")
    print("=" * 74)

    stop_code = cmd_stop(targets, clean=False)
    if stop_code != 0:
        print("\n  [WARN] 停止阶段存在问题，仍将尝试启动。")
        print("         如需强制清理，可先手工终止占用端口的进程。")

    return cmd_start(targets, timeout, skip_probe=True)


# ======================================================================
# 十、入口
# ======================================================================

def parse_args() -> argparse.Namespace:
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        prog="devctl",
        description="合同审批智能审查系统 — 开发环境进程管理器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python devctl.py status              # 查看状态\n"
            "  python devctl.py probe               # 探测基础设施\n"
            "  python devctl.py start               # 启动全部应用服务\n"
            "  python devctl.py start backend       # 只启动后端\n"
            "  python devctl.py stop                # 停止全部\n"
            "  python devctl.py stop --clean        # 停止并清理日志\n"
            "  python devctl.py restart backend     # 重启后端\n"
            "  python devctl.py logs backend -n 100 # 查看后端日志\n"
            "  python devctl.py logs -f backend     # 持续跟踪后端日志\n"
        ),
    )

    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    # status
    sub.add_parser("status", help="查看全系统状态")

    # probe
    sub.add_parser("probe", help="探测基础设施端口连通性")

    # start
    p_start = sub.add_parser("start", help="启动应用服务")
    p_start.add_argument(
        "services",
        nargs="*",
        help=f"要启动的服务，可多选；省略则启动全部（可选：{', '.join(APP_SERVICES)}）",
    )
    p_start.add_argument(
        "--timeout",
        type=float,
        default=90.0,
        help="每个服务的就绪等待上限（秒），默认 90",
    )
    p_start.add_argument(
        "--skip-probe",
        action="store_true",
        help="跳过启动前的基础设施探测（用于明知依赖缺失仍要启动的场景）",
    )

    # stop
    p_stop = sub.add_parser("stop", help="停止应用服务")
    p_stop.add_argument("services", nargs="*", help="要停止的服务；省略则停止全部")
    p_stop.add_argument("--clean", action="store_true", help="同时清理 PID 与日志文件")

    # restart
    p_restart = sub.add_parser("restart", help="重启应用服务")
    p_restart.add_argument("services", nargs="*", help="要重启的服务；省略则全部")
    p_restart.add_argument("--timeout", type=float, default=90.0, help="就绪等待上限（秒）")

    # logs
    p_logs = sub.add_parser("logs", help="查看服务日志")
    p_logs.add_argument("service", help=f"服务名（{', '.join(APP_SERVICES)}）")
    p_logs.add_argument("-n", "--lines", type=int, default=50, help="打印末尾行数，默认 50")
    p_logs.add_argument("-f", "--follow", action="store_true", help="持续跟踪输出")

    return parser.parse_args()


def main() -> int:
    """命令行入口"""
    args = parse_args()

    if not args.command:
        # 无子命令时默认打印状态，比报错更友好
        return cmd_status()

    if args.command == "status":
        return cmd_status()

    if args.command == "probe":
        return cmd_probe()

    if args.command == "start":
        targets = args.services or None
        return cmd_start(targets, args.timeout, args.skip_probe)

    if args.command == "stop":
        targets = args.services or None
        return cmd_stop(targets, args.clean)

    if args.command == "restart":
        targets = args.services or None
        return cmd_restart(targets, args.timeout)

    if args.command == "logs":
        return cmd_logs(args.service, args.lines, args.follow)

    print(f"[FAIL] 未知命令：{args.command}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
