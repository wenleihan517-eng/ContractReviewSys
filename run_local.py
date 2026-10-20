# D:/ContractReviewSys-合同审批智能审查项目/run_local.py
"""
本地一键启动脚本 — 合同审批智能审查系统

【用途】
    在 Windows 本地环境一键拉起试用所需的全部服务，并做启动前后自检。
    适用于"想快速看看系统长什么样"的场景，不用于生产部署。

【会启动的服务】
    1. MongoDB   （Docker 容器 cr-mongodb ，127.0.0.1:27017）
    2. MinIO     （Docker 容器 cr-minio   ，127.0.0.1:9000）
    3. Milvus    （Docker 容器 cr-etcd + cr-milvus，gRPC 127.0.0.1:19531）
    4. 模拟审批系统（本机 Python 进程，127.0.0.1:9100）
    5. 后端 API   （本机 Python 进程，127.0.0.1:8100）
    6. 前端 Vite  （本机 Node 进程，127.0.0.1:5173）

    说明：默认端口 8000 在本机被 VMware NAT（vmnat.exe）占用，
          因此后端改用 8100，并通过环境变量让前端代理指向它。
          若你的机器 8000 空闲，可把 BACKEND_PORT 改回 8000，
          并去掉前端的 VITE_API_PROXY_TARGET 覆盖。

【前置条件】
    - Docker Desktop 已启动
    - 已创建 Docker 容器（首次运行见 --init-containers）
    - backend/.env 已配置（Milvus 指向 127.0.0.1:19531）
    - frontend/node_modules 已安装

【用法】
    python run_local.py                 # 启动全部服务
    python run_local.py --init-containers  # 首次：创建 Docker 容器（Mongo/MinIO/Milvus）
    python run_local.py --check         # 只做健康自检，不启动
    python run_local.py --stop          # 停止本脚本启动的进程
    python run_local.py --smoke         # 启动后跑一遍核心链路冒烟

【退出】
    Ctrl+C 会停止由本脚本启动的 Python/Node 进程；
    Docker 容器默认保留（用 --stop 可一并停止）。
"""

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# ======================================================================
# 路径与常量
# ======================================================================
ROOT = Path(__file__).resolve().parent
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend"
LOGDIR = ROOT / "storage" / "logs" / "local_run"
PIDFILE = ROOT / "storage" / "local_run_pids.json"

def _resolve_first_existing(candidates, label):
    """按顺序返回第一个存在的路径；全部不存在则报人话错误。"""
    for p in candidates:
        if p.exists():
            return p
    raise FileNotFoundError(
        f"找不到 {label}，已尝试以下路径：\n  " + "\n  ".join(str(p) for p in candidates)
    )

# 托管 Node 的构建号会随升级变化（如 22.22.2-2 -> 22.22.2-6），
# 因此优先用稳定的 current 入口，再回落到系统 Node。
NODE_BIN = _resolve_first_existing([
    Path(r"C:/Users/Lenovo/.workbuddy/binaries/node/versions/current/node.exe"),
    Path(r"C:/Program Files/nodejs/node.exe"),
    *(sorted(Path(r"C:/Users/Lenovo/.workbuddy/binaries/node/versions").glob("*/node.exe"), reverse=True)
      if Path(r"C:/Users/Lenovo/.workbuddy/binaries/node/versions").is_dir() else []),
], "node.exe")

VENV_PY = _resolve_first_existing([
    Path(r"C:/Users/Lenovo/.workbuddy/binaries/python/envs/default/Scripts/python.exe"),
    Path(r"D:/Python/python.exe"),
], "python.exe（venv）")

BACKEND_PORT = 8100
FRONTEND_PORT = 5173
MOCK_PORT = 9100

# Comma-free marker: services that must be reachable before starting app layer
REQUIRED_PORTS = {
    "MongoDB": 27017,
    "MinIO": 9000,
    "Milvus": 19531,
}


def log(msg: str) -> None:
    """打印带前缀的日志"""
    print(f"[run_local] {msg}", flush=True)


# ======================================================================
# 端口探测
# ======================================================================
def port_open(host: str, port: int, timeout: float = 1.5) -> bool:
    """探测 TCP 端口是否可连接"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        return sock.connect_ex((host, port)) == 0
    finally:
        sock.close()


def wait_port(host: str, port: int, name: str, seconds: int = 60) -> bool:
    """等待端口就绪，超时返回 False"""
    deadline = time.time() + seconds
    while time.time() < deadline:
        if port_open(host, port):
            log(f"{name} 已就绪（{host}:{port}）")
            return True
        time.sleep(1.5)
    log(f"× {name} 未在 {seconds}s 内就绪（{host}:{port}）")
    return False


# ======================================================================
# Docker 容器
# ======================================================================
def docker_running(name: str) -> bool:
    """判断容器是否正在运行"""
    try:
        out = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", name],
            capture_output=True, text=True, timeout=20,
        )
        return out.stdout.strip() == "true"
    except Exception:  # noqa: BLE001
        return False


def ensure_containers() -> None:
    """确保三个基础容器处于运行状态（不自动创建）"""
    for name in ("cr-etcd", "cr-milvus", "cr-mongodb", "cr-minio"):
        if docker_running(name):
            log(f"容器已运行：{name}")
            continue
        if name == "cr-etcd":
            # etcd 是 Milvus 的依赖，必须先起
            log(f"正在启动容器：{name}")
            subprocess.run(["docker", "start", name], capture_output=True, timeout=60)
            time.sleep(3)
            continue
        log(f"正在启动容器：{name}")
        subprocess.run(["docker", "start", name], capture_output=True, timeout=60)


def stop_containers() -> None:
    """停止本项目的容器"""
    for name in ("cr-milvus", "cr-etcd", "cr-mongodb", "cr-minio"):
        if docker_running(name):
            log(f"停止容器：{name}")
            subprocess.run(["docker", "stop", name], capture_output=True, timeout=90)


# ======================================================================
# 子进程管理
# ======================================================================
def load_pids() -> dict:
    """读取已记录的进程 PID"""
    if PIDFILE.exists():
        try:
            return json.loads(PIDFILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}
    return {}


def save_pids(pids: dict) -> None:
    """写入进程 PID 记录"""
    PIDFILE.parent.mkdir(parents=True, exist_ok=True)
    PIDFILE.write_text(json.dumps(pids, indent=2), encoding="utf-8")


def kill_pid(pid: int) -> None:
    """强制结束进程（含子进程）"""
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, timeout=20)
    except Exception:  # noqa: BLE001
        pass


def start_process(name: str, cmd: list, cwd: Path, env: dict = None) -> int:
    """
    以分离方式启动进程并返回 PID

    【关键：DETACHED_PROCESS】
        直接 Popen 出的子进程会随父进程退出而被终止，导致"脚本跑完服务就没了"。
        此处使用 DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP，让子进程脱离
        当前控制台独立运行；日志通过文件句柄重定向，不依赖父进程的 stdout。

    参数：
        name: 进程名（用于日志文件命名）
        cmd: 命令列表
        cwd: 工作目录
        env: 追加的环境变量
    返回：
        进程 PID
    """
    LOGDIR.mkdir(parents=True, exist_ok=True)
    logfile = LOGDIR / f"{name}.log"
    merged = os.environ.copy()
    if env:
        merged.update(env)
    fh = open(logfile, "w", encoding="utf-8", errors="replace")
    # DETACHED_PROCESS=0x00000008，CREATE_NEW_PROCESS_GROUP=0x00000200
    flags = 0x00000008 | 0x00000200
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        flags |= subprocess.CREATE_NO_WINDOW
    proc = subprocess.Popen(cmd, cwd=str(cwd), stdout=fh, stderr=subprocess.STDOUT,
                            env=merged, stdin=subprocess.DEVNULL, creationflags=flags)
    log(f"已启动 {name} | PID={proc.pid} | 日志={logfile}")
    return proc.pid


# ======================================================================
# 应用层启动
# ======================================================================
def start_mock_approval() -> int:
    """启动模拟审批系统"""
    return start_process(
        "mock_approval",
        [str(VENV_PY), "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", str(MOCK_PORT)],
        ROOT / "deploy" / "mock_approval",
    )


def start_backend() -> int:
    """启动后端 API"""
    return start_process(
        "backend",
        [str(VENV_PY), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(BACKEND_PORT)],
        BACKEND,
    )


def start_frontend() -> int:
    """启动前端 Vite 开发服务器（代理指向后端实际端口）"""
    return start_process(
        "frontend",
        [str(NODE_BIN), "node_modules/vite/bin/vite.js"],
        FRONTEND,
        env={"VITE_API_PROXY_TARGET": f"http://127.0.0.1:{BACKEND_PORT}"},
    )


# ======================================================================
# 自检
# ======================================================================
def http_get(url: str, timeout: int = 15):
    """GET 请求，返回 (状态码, 响应体)"""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return 0, f"{type(exc).__name__}: {exc}"


def health_check() -> bool:
    """对已启动的服务做健康自检"""
    log("=" * 62)
    log("健康自检")
    log("=" * 62)
    ok = True

    for name, port in REQUIRED_PORTS.items():
        alive = port_open("127.0.0.1", port)
        log(f"  {'√' if alive else '×'} {name:10s} 127.0.0.1:{port}")
        ok = ok and alive

    status, _ = http_get(f"http://127.0.0.1:{MOCK_PORT}/health")
    log(f"  {'√' if status == 200 else '×'} 模拟审批   127.0.0.1:{MOCK_PORT}")
    ok = ok and status == 200

    status, body = http_get(f"http://127.0.0.1:{BACKEND_PORT}/")
    log(f"  {'√' if status == 200 else '×'} 后端 API   127.0.0.1:{BACKEND_PORT}")
    ok = ok and status == 200

    status, body = http_get(f"http://127.0.0.1:{FRONTEND_PORT}/")
    log(f"  {'√' if status == 200 else '×'} 前端页面   127.0.0.1:{FRONTEND_PORT}")
    ok = ok and status == 200

    # 经前端代理验证链路贯通
    if status == 200:
        status2, body2 = http_get(f"http://127.0.0.1:{FRONTEND_PORT}/api/v1/health")
        log(f"  {'√' if status2 == 200 else '×'} 代理链路   /api/v1/health")
        ok = ok and status2 == 200

    return ok


# ======================================================================
# 主流程
# ======================================================================
def main() -> int:
    parser = argparse.ArgumentParser(description="本地一键启动脚本")
    parser.add_argument("--check", action="store_true", help="只做健康自检")
    parser.add_argument("--stop", action="store_true", help="停止本脚本启动的进程")
    parser.add_argument("--smoke", action="store_true", help="启动后运行核心链路冒烟")
    parser.add_argument("--init-containers", action="store_true",
                        help="仅检查 Docker 容器状态（首次需手工创建，见 README）")
    args = parser.parse_args()

    if args.stop:
        pids = load_pids()
        for name, pid in pids.items():
            log(f"停止 {name} | PID={pid}")
            kill_pid(pid)
        save_pids({})
        stop_containers()
        log("已停止")
        return 0

    if args.check:
        return 0 if health_check() else 1

    if args.init_containers:
        ensure_containers()
        return 0

    # ---------------- 1. 基础容器 ----------------
    log("步骤 1/5：检查并启动基础容器（MongoDB / MinIO / Milvus）")
    ensure_containers()
    if not wait_port("127.0.0.1", 27017, "MongoDB", 90):
        return 1
    if not wait_port("127.0.0.1", 9000, "MinIO", 60):
        return 1
    if not wait_port("127.0.0.1", 19531, "Milvus", 120):
        log("提示：Milvus 未就绪时后端会拒绝启动（这是刻意的强依赖设计）")
        return 1

    pids = load_pids()

    # ---------------- 2. 模拟审批系统 ----------------
    log("步骤 2/5：启动模拟审批系统")
    if not port_open("127.0.0.1", MOCK_PORT):
        pids["mock_approval"] = start_mock_approval()
        wait_port("127.0.0.1", MOCK_PORT, "模拟审批系统", 45)
    else:
        log("模拟审批系统已在运行，跳过")

    # ---------------- 3. 后端 ----------------
    log("步骤 3/5：启动后端 API")
    if not port_open("127.0.0.1", BACKEND_PORT):
        pids["backend"] = start_backend()
        if not wait_port("127.0.0.1", BACKEND_PORT, "后端 API", 90):
            log(f"后端启动失败，请查看日志：{LOGDIR / 'backend.log'}")
            save_pids(pids)
            return 1
    else:
        log("后端已在运行，跳过")

    # ---------------- 4. 前端 ----------------
    log("步骤 4/5：启动前端开发服务器")
    if not port_open("127.0.0.1", FRONTEND_PORT):
        pids["frontend"] = start_frontend()
        wait_port("127.0.0.1", FRONTEND_PORT, "前端", 90)
    else:
        log("前端已在运行，跳过")

    save_pids(pids)

    # ---------------- 5. 自检 ----------------
    log("步骤 5/5：健康自检")
    ok = health_check()

    # ---------------- 冒烟 ----------------
    if args.smoke:
        log("=" * 62)
        log("运行核心链路冒烟")
        log("=" * 62)
        subprocess.run(
            [str(VENV_PY), "scripts/smoke_local_run.py", "--base", f"http://127.0.0.1:{BACKEND_PORT}"],
            cwd=str(BACKEND),
        )

    log("=" * 62)
    if ok:
        log("全部服务就绪，可以开始试用")
        log(f"  前端入口：http://127.0.0.1:{FRONTEND_PORT}/")
        log(f"  接口文档：http://127.0.0.1:{BACKEND_PORT}/docs")
        log("  内置账号：user_business / user_law / user_admin（密码均为 123456）")
    else:
        log("存在未就绪的服务，请查看上面的自检结果")
    log("=" * 62)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
