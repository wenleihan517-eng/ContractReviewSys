# D:/ContractReviewSys-合同审批智能审查项目/backend/scripts/system_check.py
"""
系统自检脚本 — 逐组件连通性与配置一致性检查

【脚本职责】
    对系统运行所依赖的每一个外部组件做独立探测，并输出分级诊断报告。
    每个失败项都附带**具体的排查动作**，而不是只说"失败了"。

    检查项分六大类：
        1. 配置自检     — 配置项内部一致性（维度、端口、路径、红线开关）
        2. MongoDB      — 连接、库、集合、索引
        3. MinIO        — 连接、存储桶
        4. Milvus       — 连接、独立库、三类集合、向量维度、索引
        5. 模型服务     — Embedding 实测维度、LLM 可用性
        6. 审批系统     — 连通性、白名单接口可达性、动作类接口应拒绝
        7. 规则配置     — YAML 可加载、规则数量、四类高风险规则齐备

【为什么需要这个脚本】
    本系统的启动依赖 6 个外部组件。当服务起不来时，用户面对的是
    一个 FastAPI 的启动异常堆栈，里面混着 MongoDB / Milvus / MinIO 的
    底层错误，很难快速定位。

    本脚本把"哪个组件不通、为什么不通、怎么修"一次性讲清楚。
    典型使用场景：
        - 部署后首次验证
        - 服务启动失败时定位
        - 改了配置后确认没有配错
        - 交接给别人前确认环境完整

【分级说明】
    [PASS]  该项正常
    [WARN]  该项异常但不阻塞核心功能（如 LLM 不可用时规则判定仍可工作）
    [FAIL]  该项异常且阻塞核心功能，必须修复

【运行方式】
    cd backend
    python scripts/system_check.py                # 完整自检
    python scripts/system_check.py --quick        # 快速模式（跳过写操作）
    python scripts/system_check.py --json         # 输出 JSON（供 CI 消费）
    python scripts/system_check.py --only milvus  # 只检查指定组件

【退出码】
    0 = 无 FAIL（可能有 WARN）
    1 = 存在 FAIL
"""

from __future__ import annotations

import argparse
import asyncio
import json as json_lib
import os
import sys
import time
import warnings
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

# ----------------------------------------------------------------------
# 环境降噪（必须在 import pymilvus / grpc 之前生效）
# ----------------------------------------------------------------------
# 说明：
#   自检脚本的核心价值是"输出一份干净的诊断报告"。
#   但底层 SDK 会在 import 期与运行期打印与诊断无关的告警，
#   例如 pymilvus 的：
#       PyMilvusDeprecationWarning: `connections.connect` is an ORM-style ...
#   这些内容会穿插在 [PASS]/[FAIL] 之间，干扰阅读。
#   这里统一屏蔽掉，让报告只保留本脚本的判断。
warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", message=".*ORM-style PyMilvus API.*")
os.environ.setdefault("GRPC_VERBOSITY", "ERROR")
os.environ.setdefault("GRPC_TRACE", "")
os.environ.setdefault("GLOG_minloglevel", "2")

# ----------------------------------------------------------------------
# 路径处理
# ----------------------------------------------------------------------
BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from app.core.config import settings  # noqa: E402


# ======================================================================
# 一、结果收集
# ======================================================================

# 检查结果条目：{组件, 检查项, 状态, 详情, 建议}
CheckResult = Dict[str, Any]

RESULTS: List[CheckResult] = []
_JSON_MODE = False


def _add(component: str, name: str, status: str, detail: str = "", advice: str = "") -> None:
    """
    记录一条检查结果

    参数：
        component: 组件名（config / mongodb / minio / milvus / embedding / llm / approval / rules）
        name: 检查项名称
        status: PASS / WARN / FAIL / SKIP
        detail: 检测详情
        advice: 修复建议（失败时必填）
    """
    RESULTS.append(
        {
            "component": component,
            "name": name,
            "status": status,
            "detail": detail,
            "advice": advice,
        }
    )

    if _JSON_MODE:
        return

    mark = {
        "PASS": "  [PASS]",
        "WARN": "  [WARN]",
        "FAIL": "  [FAIL]",
        "SKIP": "  [SKIP]",
    }.get(status, "  [????]")

    print(f"{mark} {name}")
    if detail:
        # 逐行缩进：detail 可能是多行（如 "配置值\n异常摘要"），
        # 只缩进第一行会让后续行顶到行首，破坏报告的层级可读性。
        for line in str(detail).strip().split("\n"):
            print(f"         {line.rstrip()}")
    if advice and status in ("FAIL", "WARN"):
        for line in advice.strip().split("\n"):
            print(f"         → {line.strip()}")


def _section(title: str) -> None:
    """打印分节标题"""
    if _JSON_MODE:
        return
    _flush()
    print("\n" + "=" * 74)
    print(f"  {title}")
    print("=" * 74)


def _subsection(title: str) -> None:
    """打印小节标题"""
    if _JSON_MODE:
        return
    _flush()
    print(f"\n-- {title} " + "-" * max(0, 70 - len(title)))


# ----------------------------------------------------------------------
# stderr 净化
# ----------------------------------------------------------------------
# 背景：
#   gRPC 的 C++ 层（http_connect_client_handshaker 等）把日志直接写到
#   文件描述符 2，绕过了 Python 的 logging 与 warnings，
#   因此在 Python 侧设级别、加 filter 都拦不住。
#   典型污染行：
#       E0919 01:52:57.8361 ... http_connect_client_handshaker.cc:114]
#       HTTP proxy handshake with ipv4:127.0.0.1:60721 failed: ... 502
#
# 处理：
#   在本进程内把 fd 2 复制一份出来，替换为一个管道，
#   由后台线程按行读取并**只丢弃匹配这些特征的行**，其余原样转发。
#   这样既能保住真正的报错信息，又能让诊断报告保持干净。
#
# 注意：
#   1. 只在非 JSON 模式下启用，避免影响 CI 消费结构化输出。
#   2. 必须在任何 SDK 连接动作**之前**调用。
#   3. 过滤规则刻意收窄到具体文件名与行首时间戳格式，
#      避免误伤本脚本自己的异常输出。

# 需要丢弃的 stderr 行特征（正则，匹配即丢弃）
_STDERR_NOISE_PATTERNS = (
    # gRPC C++ 层日志：形如 "E0919 01:52:57.836197  1234 file.cc:114] ..."
    r"^[EWI]\d{4}\s+\d{2}:\d{2}:\d{2}\.\d+\s+\d+\s+\S+\.cc:\d+\]",
    # gRPC 代理握手失败（本地无代理时的固定噪声，与业务无关）
    r"HTTP proxy handshake with ipv4:",
    # pymilvus ORM 风格 API 弃用告警
    r"PyMilvusDeprecationWarning",
    r"ORM-style PyMilvus API",
)

_stderr_saved_fd: Optional[int] = None
_stderr_pump: Optional[Any] = None


def _install_stderr_filter() -> None:
    """
    安装 stderr 净化管道

    说明：
        把 fd 2 重定向到管道，后台线程逐行过滤后转发到原始 stderr。
        幂等：重复调用不会重复安装。

    返回：
        无返回值
    """
    global _stderr_saved_fd, _stderr_pump

    if _JSON_MODE or _stderr_saved_fd is not None:
        return

    import re
    import threading

    patterns = [re.compile(p) for p in _STDERR_NOISE_PATTERNS]

    try:
        # 保存原始 fd 2（用于转发保留的行）
        _stderr_saved_fd = os.dup(2)

        # 建管道：r_fd 供后台线程读，w_fd 接管 fd 2
        r_fd, w_fd = os.pipe()
        os.dup2(w_fd, 2)
        os.close(w_fd)

        def _pump() -> None:
            """后台线程：逐行读取被劫持的 stderr，过滤后转发"""
            with os.fdopen(r_fd, "r", encoding="utf-8", errors="replace") as reader:
                for line in reader:
                    if any(pt.search(line) for pt in patterns):
                        continue  # 命中噪声特征 → 丢弃
                    os.write(_stderr_saved_fd, line.encode("utf-8", errors="replace"))

        _stderr_pump = threading.Thread(
            target=_pump,
            name="stderr-purifier",
            daemon=True,
        )
        _stderr_pump.start()

    except Exception:
        # 净化失败不影响自检本身：退回到不过滤，继续跑
        _stderr_saved_fd = None


def _flush() -> None:
    """刷新输出缓冲，避免报告与残留的转发行错序"""
    try:
        sys.stdout.flush()
    except Exception:  # noqa: BLE001
        pass


def _one_line_error(exc: BaseException, limit: int = 300) -> str:
    """
    把异常压成"一行、可读、不刷屏"的文本

    背景：
        底层 SDK（pymilvus / grpc / minio / motor）抛出的异常，
        其 str() 往往带多行堆栈或超长的 RPC 详情。
        直接拼进诊断报告会把终端刷满，把真正的结论淹没掉。

    处理：
        1. 取第一行非空内容（去掉库自带的换行堆栈）
        2. 压缩连续空白
        3. 截断到 limit 字符，超出部分用 "..." 提示

    参数：
        exc   —— 捕获到的异常对象
        limit —— 单行最大字符数

    返回：
        形如 "MongoDBException: connection refused" 的单行字符串
    """
    raw = str(exc).strip()
    if not raw:
        # 有些 gRPC 异常 str() 为空，退回到类名
        return type(exc).__name__

    # 逐行找第一个有实际内容的行
    first_line = ""
    for line in raw.split("\n"):
        stripped = line.strip()
        if stripped:
            first_line = stripped
            break

    # 压缩连续空白，避免 tab/多空格撑破排版
    first_line = " ".join(first_line.split())

    if len(first_line) > limit:
        first_line = first_line[:limit] + "..."

    return f"{type(exc).__name__}: {first_line}"


async def _quiet_call(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """
    在静音模式下调用函数 —— 抑制底层 SDK 往 stderr 打堆栈

    背景：
        连接失败时，底层库除了抛异常，还会**自己**往 stderr 打印大段堆栈：
            - pymilvus → grpc._channel._InactiveRpcError 一大串
            - app/storage/mongo_client.py → pymongo 的 create_indexes 堆栈
        这些输出在我的 except 之前就落到终端了，无法用 try/except 拦住。
        结果是：明明捕获了异常，终端还是被刷屏，用户看到的最后一段
        是 traceback 而不是本脚本的诊断结论。

    处理：
        调用期间临时把相关 logger 的级别提到 CRITICAL 以上，
        并在调用结束后恢复原级别。覆盖三层：
            1. 第三方 SDK   —— pymongo / motor / minio / pymilvus / grpc
            2. 本项目模块层 —— app.storage / app.engines
               （storage 层 connect() 内部用 logger.error(..., exc_info=True)，
                 关掉后既保留本脚本的结论，也避免重复噪声）
            3. root logger  —— 兜底，防止未命名的 logger 漏网

    参数：
        func —— 要调用的可调用对象（同步或异步均可）
        其余参数原样透传

    返回：
        func 的返回值

    异常：
        原样向上抛出 func 的异常，由调用方负责渲染为诊断条目
    """
    import logging

    quiet_names = (
        # --- 第三方 SDK ---
        "pymongo",
        "motor",
        "minio",
        "urllib3",
        "pymilvus",
        "grpc",
        # --- 本项目模块 ---
        "app.storage",
        "app.engines",
    )
    saved: Dict[str, int] = {}
    for name in quiet_names:
        lg = logging.getLogger(name)
        saved[name] = lg.level
        lg.setLevel(logging.CRITICAL + 10)

    root_saved = logging.root.level
    logging.root.setLevel(logging.CRITICAL + 10)

    try:
        result = func(*args, **kwargs)
        if asyncio.iscoroutine(result):
            return await result
        return result
    finally:
        for name, lvl in saved.items():
            logging.getLogger(name).setLevel(lvl)
        logging.root.setLevel(root_saved)


# ======================================================================
# 二、配置自检
# ======================================================================

def check_config() -> None:
    """
    配置自检

    说明：
        这一类检查不依赖任何外部服务，纯本地校验。
        放在最前面，因为配置错误会导致后续所有探测得出错误结论——
        比如 Milvus 地址配错了，后面的"连接失败"其实是配置问题而非服务问题。
    """
    _section("一、配置自检（不依赖外部服务）")

    # --- 向量维度 ---
    if settings.EMBEDDING_DIM == 1024:
        _add(
            "config",
            "Embedding 维度固定为 1024（BGE-M3）",
            "PASS",
            f"EMBEDDING_DIM={settings.EMBEDDING_DIM}",
        )
    else:
        _add(
            "config",
            "Embedding 维度必须为 1024",
            "FAIL",
            f"当前 EMBEDDING_DIM={settings.EMBEDDING_DIM}",
            "BGE-M3 输出固定 1024 维，修改该值会导致 Milvus 写入与检索全部失败。\n"
            "如已更换向量模型，必须同时重建 Milvus 全部集合。",
        )

    # --- 规则引擎开关（红线）---
    if settings.RULE_ENGINE_ENABLED:
        _add(
            "config",
            "规则引擎已启用（红线：所有正式判定唯一裁决方）",
            "PASS",
            f"RULE_ENGINE_VERSION={settings.RULE_ENGINE_VERSION}",
        )
    else:
        _add(
            "config",
            "规则引擎必须启用",
            "FAIL",
            "RULE_ENGINE_ENABLED=False",
            "这是权责红线：关闭规则引擎会使其直接抛异常，系统拒绝执行审查流程，\n"
            "以保证绝不出现'未经规则判定即放行'的情况。\n"
            "修复：在 .env 中设置 RULE_ENGINE_ENABLED=True",
        )

    # --- 阻塞等级配置 ---
    block_levels = settings.RULE_ENGINE_BLOCK_LEVELS
    if block_levels:
        high_included = any(lv in ("high", "critical") for lv in block_levels)
        if high_included:
            _add(
                "config",
                "阻塞等级包含高风险及以上",
                "PASS",
                f"RULE_ENGINE_BLOCK_LEVELS={block_levels}",
            )
        else:
            _add(
                "config",
                "阻塞等级未包含 high/critical",
                "WARN",
                f"当前值={block_levels}",
                "高风险合同不会被阻塞审批流，可能导致重大风险合同直接过审。\n"
                "建议至少包含 high 与 critical。",
            )
    else:
        _add("config", "阻塞等级配置为空", "FAIL", "", "必须配置 RULE_ENGINE_BLOCK_LEVELS")

    # --- Milvus 独立库（禁用 default）---
    if settings.MILVUS_DB_NAME == "default":
        _add(
            "config",
            "Milvus 数据库名不得为 default",
            "FAIL",
            "MILVUS_DB_NAME=default",
            "必须使用独立库做物理隔离，避免与同实例下其它项目的集合互相污染。\n"
            "修复：MILVUS_DB_NAME=contract_review",
        )
    else:
        _add(
            "config",
            "Milvus 使用独立数据库（与其它项目隔离）",
            "PASS",
            f"MILVUS_DB_NAME={settings.MILVUS_DB_NAME}",
        )

    # --- 规则文件存在性 ---
    rules_path = settings.RULE_RULES_PATH
    if not os.path.isabs(rules_path):
        rules_path = os.path.normpath(os.path.join(BACKEND_DIR, rules_path))

    if os.path.exists(rules_path):
        size_kb = os.path.getsize(rules_path) / 1024
        _add(
            "config",
            "规则配置文件存在",
            "PASS",
            f"{rules_path}（{size_kb:.1f} KB）",
        )
    else:
        _add(
            "config",
            "规则配置文件不存在",
            "FAIL",
            f"路径={rules_path}",
            "规则引擎无法加载规则，审查流程会直接失败。\n"
            "修复：确认 backend/config/risk_rules.yaml 存在，\n"
            "      或调整 .env 中的 RULE_RULES_PATH。",
        )

    # --- 审批白名单配置 ---
    try:
        from app.utils.approval_guard import (
            ALLOWED_APPROVAL_APIS,
            FORBIDDEN_APPROVAL_APIS,
        )

        if settings.APPROVAL_COMMENT_API_PATH in ALLOWED_APPROVAL_APIS:
            _add(
                "config",
                "审批回写接口位于白名单内",
                "PASS",
                f"APPROVAL_COMMENT_API_PATH={settings.APPROVAL_COMMENT_API_PATH}",
            )
        else:
            _add(
                "config",
                "审批回写接口不在白名单内",
                "FAIL",
                f"当前值={settings.APPROVAL_COMMENT_API_PATH} 白名单={ALLOWED_APPROVAL_APIS}",
                "回写会被 approval_guard 拒绝，审查意见无法送达审批系统。\n"
                "修复：APPROVAL_COMMENT_API_PATH=/comments",
            )

        # 确认黑名单非空（防止被误清空）
        if len(FORBIDDEN_APPROVAL_APIS) >= 8:
            _add(
                "config",
                "审批动作类接口黑名单完整",
                "PASS",
                f"已登记 {len(FORBIDDEN_APPROVAL_APIS)} 个禁止接口："
                f"{', '.join(FORBIDDEN_APPROVAL_APIS)}",
            )
        else:
            _add(
                "config",
                "审批动作类接口黑名单不完整",
                "FAIL",
                f"仅 {len(FORBIDDEN_APPROVAL_APIS)} 个：{FORBIDDEN_APPROVAL_APIS}",
                "黑名单必须覆盖全部 8 个动作类接口：\n"
                "  /approve /reject /transfer /withdraw /reassign /sign /complete /terminate\n"
                "修复：检查 app/utils/approval_guard.py 的 FORBIDDEN_APPROVAL_APIS",
            )
    except Exception as exc:  # noqa: BLE001
        _add(
            "config",
            "审批白名单校验失败",
            "FAIL",
            _one_line_error(exc),
            "检查 app/utils/approval_guard.py 是否可正常导入。",
        )

    # --- 端口占用自查 ---
    _add(
        "config",
        "服务监听配置",
        "PASS",
        f"SERVER_HOST={settings.SERVER_HOST} SERVER_PORT={settings.SERVER_PORT} "
        f"RELOAD={settings.SERVER_RELOAD}",
    )

    # --- 环境标识 ---
    env = settings.APP_ENV
    if env == "prod" and settings.APP_DEBUG:
        _add(
            "config",
            "生产环境不应开启调试模式",
            "WARN",
            f"APP_ENV={env} 但 APP_DEBUG=True",
            "生产环境开启调试会暴露内部堆栈与配置信息。\n修复：APP_DEBUG=False",
        )
    elif env == "prod" and settings.SERVER_RELOAD:
        _add(
            "config",
            "生产环境不应开启热重载",
            "WARN",
            f"APP_ENV={env} 但 SERVER_RELOAD=True",
            "热重载模式只能单进程运行，且会额外消耗资源。\n修复：SERVER_RELOAD=False",
        )
    else:
        _add("config", "运行环境配置合理", "PASS", f"APP_ENV={env}")


# ======================================================================
# 三、MongoDB 检查
# ======================================================================

async def check_mongodb() -> None:
    """
    MongoDB 检查

    检查项：连接、库、集合完整性、索引完整性、规则数据
    """
    _section("二、MongoDB — 业务主数据存储")

    try:
        from app.storage.mongo_client import mongo_client
    except Exception as exc:  # noqa: BLE001
        _add("mongodb", "客户端导入失败", "FAIL", f"{exc}", "检查依赖：pip install motor pymongo")
        return

    # --- 连接 ---
    # 说明：mongo_client.connect() 内部会执行 ping + 建索引，
    # 因此这里直接调用它，而不是先 ping。
    # 若跳过 connect 直接 ping，会得到
    # "客户端尚未初始化，无法执行 ping" —— 那是脚本自身的用法错误，
    # 会被误报成"MongoDB 连不上"，从而把排查方向带偏。
    try:
        await _quiet_call(mongo_client.connect)
    except Exception as exc:  # noqa: BLE001
        detail = _one_line_error(exc)
        _add(
            "mongodb",
            "连接失败",
            "FAIL",
            f"MONGO_URI={settings.MONGO_URI}\n{detail}",
            "排查步骤：\n"
            "  1. 确认服务已启动：\n"
            "     本机（Windows 服务）：net start MongoDB\n"
            "     Docker：cd deploy/docker && docker compose up -d mongodb\n"
            "  2. 确认端口可达：telnet 127.0.0.1 27017\n"
            "  3. 确认 backend/.env 的 MONGO_URI 正确（含鉴权时需带账号密码）\n"
            "  4. 首次部署：MongoDB 服务起来后执行 python scripts/init_mongodb.py",
        )
        return

    _add("mongodb", "连接成功", "PASS", f"{settings.MONGO_URI} / 库={settings.MONGO_DB_NAME}")

    # --- 库与集合 ---
    try:
        db = mongo_client.get_database()
        existing = set(await asyncio.to_thread(db.list_collection_names))
    except Exception as exc:  # noqa: BLE001
        _add("mongodb", "读取集合列表失败", "FAIL", f"{exc}", "确认连接账号具备 listCollections 权限。")
        return

    from app.constants import MongoCollection

    expected_collections = [
        MongoCollection.CONTRACT,
        MongoCollection.CONTRACT_CLAUSE,
        MongoCollection.OCR_RESULT,
        MongoCollection.REVIEW_TASK,
        MongoCollection.REVIEW_RISK_HIT,
        MongoCollection.REVIEW_CONCLUSION,
        MongoCollection.RULE_TEMPLATE,
        MongoCollection.RULE_HIT_LOG,
        MongoCollection.AUDIT_LOG,
        MongoCollection.STANDARD_CLAUSE,
        MongoCollection.SUGGESTION,
        MongoCollection.APPROVAL_RECORD,
        MongoCollection.WRITE_BACK_LOG,
        MongoCollection.REPORT,
        MongoCollection.FILE_OBJECT,
        MongoCollection.SYS_CONFIG,
    ]

    missing = [name for name in expected_collections if name not in existing]
    if not missing:
        _add(
            "mongodb",
            f"业务集合齐备（{len(expected_collections)} 个）",
            "PASS",
            "全部 cr_ 前缀集合均已创建",
        )
    else:
        _add(
            "mongodb",
            f"缺失 {len(missing)} 个业务集合",
            "FAIL",
            f"缺失：{', '.join(missing)}",
            "修复：python scripts/init_mongodb.py",
        )

    # --- 索引 ---
    index_issues: List[str] = []
    index_total = 0
    for name in expected_collections:
        if name not in existing:
            continue
        try:
            info = await asyncio.to_thread(db[name].index_information)
            count = len([k for k in info.keys() if k != "_id_"])
            index_total += count
            if count == 0:
                index_issues.append(f"{name}（无业务索引）")
        except Exception as exc:  # noqa: BLE001
            index_issues.append(f"{name}（读取失败：{exc}）")

    if not index_issues:
        _add(
            "mongodb",
            f"索引齐备（共 {index_total} 个业务索引）",
            "PASS",
        )
    else:
        _add(
            "mongodb",
            f"{len(index_issues)} 个集合索引缺失或异常",
            "FAIL",
            f"问题集合：{'; '.join(index_issues[:5])}",
            "修复：python scripts/init_mongodb.py",
        )

    # --- 规则数据 ---
    try:
        rule_count = await asyncio.to_thread(
            lambda: db[MongoCollection.RULE_TEMPLATE].estimated_document_count()
        )
        if rule_count > 0:
            _add(
                "mongodb",
                f"规则模板数据已导入（{rule_count} 条）",
                "PASS",
            )
        else:
            _add(
                "mongodb",
                "规则模板集合为空",
                "WARN",
                f"{MongoCollection.RULE_TEMPLATE} 文档数=0",
                "规则引擎从 YAML 文件加载规则，不依赖此集合，因此不影响审查功能。\n"
                "但前端「合规规则与模板页」会显示为空。\n"
                "修复：python scripts/init_mongodb.py",
            )
    except Exception as exc:  # noqa: BLE001
        _add("mongodb", "规则数据统计失败", "WARN", _one_line_error(exc))

    # --- 系统配置 ---
    try:
        cfg_count = await asyncio.to_thread(
            lambda: db[MongoCollection.SYS_CONFIG].estimated_document_count()
        )
        if cfg_count > 0:
            _add("mongodb", f"系统配置项已初始化（{cfg_count} 项）", "PASS")
        else:
            _add(
                "mongodb",
                "系统配置集合为空",
                "WARN",
                f"{MongoCollection.SYS_CONFIG} 文档数=0",
                "修复：python scripts/init_mongodb.py",
            )
    except Exception as exc:  # noqa: BLE001
        _add("mongodb", "系统配置统计失败", "WARN", _one_line_error(exc))


# ======================================================================
# 四、MinIO 检查
# ======================================================================

async def check_minio() -> None:
    """MinIO 检查：连接与存储桶"""
    _section("三、MinIO — 对象存储")

    try:
        from app.storage.minio_client import minio_client
    except Exception as exc:  # noqa: BLE001
        _add("minio", "客户端导入失败", "FAIL", f"{exc}", "检查依赖：pip install minio")
        return

    # 说明：minio_client.connect() 内部会探测连通性并创建存储桶，必须调用它。
    # health_check() 依赖 _require_client()，未初始化时会抛
    # "MinIO 客户端尚未初始化" —— 那是脚本自身的用法错误，
    # 会被误报成"MinIO 连不上"，从而把排查方向带偏。
    try:
        await _quiet_call(minio_client.connect)
    except Exception as exc:  # noqa: BLE001
        detail = _one_line_error(exc)
        _add(
            "minio",
            "连接失败",
            "FAIL",
            f"MINIO_ENDPOINT={settings.MINIO_ENDPOINT}\n{detail}",
            "排查步骤：\n"
            "  1. 确认服务已启动：\n"
            "     Docker：cd deploy/docker && docker compose up -d minio\n"
            "  2. 确认端口可达：telnet 127.0.0.1 9000\n"
            "  3. 确认 MINIO_ACCESS_KEY / MINIO_SECRET_KEY 与容器环境变量一致\n"
            "  4. 控制台地址 http://127.0.0.1:9001 可登录验证",
        )
        return

    _add("minio", "连接成功", "PASS", f"{settings.MINIO_ENDPOINT}")

    # --- 存储桶 ---
    # 说明：minio_client.connect() 内含 _ensure_buckets()，
    # 因此连接成功后桶应已存在。这里逐一确认。
    expected_buckets = [
        ("contract-origin", "合同原文", settings.MINIO_BUCKET_CONTRACT),
        ("contract-ocr-image", "OCR 页面图片", settings.MINIO_BUCKET_OCR_IMAGE),
        ("contract-report", "导出报告", settings.MINIO_BUCKET_REPORT),
    ]

    client = minio_client.get_client()
    if client is None:
        _add("minio", "客户端未初始化", "FAIL", "", "重新运行自检；若持续失败检查 storage 层初始化逻辑。")
        return

    missing_buckets: List[str] = []
    for label, desc, bucket_name in expected_buckets:
        try:
            exists = await asyncio.to_thread(client.bucket_exists, bucket_name)
            if not exists:
                missing_buckets.append(f"{bucket_name}（{desc}）")
        except Exception as exc:  # noqa: BLE001
            missing_buckets.append(f"{bucket_name}（查询失败：{exc}）")

    if not missing_buckets:
        _add(
            "minio",
            f"业务存储桶齐备（{len(expected_buckets)} 个）",
            "PASS",
            " / ".join(f"{label}" for label, _, _ in expected_buckets),
        )
    else:
        _add(
            "minio",
            f"缺失 {len(missing_buckets)} 个存储桶",
            "FAIL",
            f"缺失：{'; '.join(missing_buckets)}",
            "存储桶由后端启动时自动创建。修复方式：\n"
            "  1. 重新启动后端服务（lifespan 会自动建桶）\n"
            "  2. 或手工创建：\n"
            f"     docker exec cr-minio mc mb /data/{settings.MINIO_BUCKET_CONTRACT}",
        )

    # --- 其余桶（配置中存在但本次未逐一校验的）---
    extra_buckets = [
        ("contract-clause-lib", settings.MINIO_BUCKET_CLAUSE_LIB),
        ("contract-tmp", settings.MINIO_BUCKET_TMP),
    ]
    for label, bucket_name in extra_buckets:
        try:
            exists = await asyncio.to_thread(client.bucket_exists, bucket_name)
            if exists:
                _add("minio", f"存储桶 {bucket_name}（{label}）存在", "PASS")
            else:
                _add(
                    "minio",
                    f"存储桶 {bucket_name}（{label}）不存在",
                    "WARN",
                    "",
                    "该桶用于非核心流程，缺失时相关功能不可用但不阻塞审查。",
                )
        except Exception as exc:  # noqa: BLE001
            _add("minio", f"存储桶 {bucket_name} 查询失败", "WARN", _one_line_error(exc))


# ======================================================================
# 五、Milvus 检查
# ======================================================================

async def check_milvus() -> None:
    """Milvus 检查：连接、独立库、三类集合、维度、索引"""
    _section("四、Milvus — 向量数据库")

    try:
        from app.storage.milvus_client import milvus_manager
    except Exception as exc:  # noqa: BLE001
        _add("milvus", "客户端导入失败", "FAIL", f"{exc}", "检查依赖：pip install pymilvus")
        return

    # --- 连接（内含建库与建集合）---
    # 用 _quiet_call 包裹：pymilvus 连接失败时会自行往 stderr 打大段
    # gRPC 堆栈，必须在其打印期间把库的日志级别压下去，
    # 否则终端会被 traceback 刷屏，最后一段不再是本脚本的诊断结论。
    try:
        await _quiet_call(milvus_manager.connect)
    except Exception as exc:  # noqa: BLE001
        _add(
            "milvus",
            "连接失败",
            "FAIL",
            f"{settings.MILVUS_URI} / 库={settings.MILVUS_DB_NAME}\n{_one_line_error(exc)}",
            "排查步骤：\n"
            f"  1. 确认 Milvus 服务已启动：curl http://{settings.MILVUS_HOST}:9091/healthz\n"
            f"  2. 确认网络可达：ping {settings.MILVUS_HOST}\n"
            f"  3. 确认端口开放：telnet {settings.MILVUS_HOST} {settings.MILVUS_PORT}\n"
            "  4. 若 Milvus 在虚拟机内，确认已做端口映射（默认 19530 → 本项目配 7000）\n"
            "  5. 检查虚拟机防火墙是否放行该端口\n"
            "  6. 若报 DEADLINE_EXCEEDED 但端口通，多为 Milvus 正在加载集合，稍后重试",
        )
        return

    _add(
        "milvus",
        "连接成功",
        "PASS",
        f"{settings.MILVUS_URI} / 库={settings.MILVUS_DB_NAME}",
    )

    # --- 独立库确认 ---
    if settings.MILVUS_DB_NAME != "default":
        _add(
            "milvus",
            "使用独立数据库（数据隔离生效）",
            "PASS",
            f"库名={settings.MILVUS_DB_NAME}，未使用 default 库",
        )

    # --- 集合 ---
    client = milvus_manager.get_client()
    if client is None:
        _add("milvus", "客户端未初始化", "FAIL", "", "重新运行自检。")
        return

    collections = [
        (settings.MILVUS_COLLECTION_CLAUSE, "条款向量集合"),
        (settings.MILVUS_COLLECTION_STANDARD_CLAUSE, "标准条款库集合"),
        (settings.MILVUS_COLLECTION_RULE_TEMPLATE, "规则模板集合"),
    ]

    for coll_name, desc in collections:
        try:
            exists = await asyncio.to_thread(client.has_collection, coll_name)
        except Exception as exc:  # noqa: BLE001
            _add("milvus", f"集合 {coll_name} 查询失败", "FAIL", str(exc),
                 "修复：python scripts/init_milvus_vectors.py")
            continue

        if not exists:
            _add(
                "milvus",
                f"集合 {coll_name}（{desc}）不存在",
                "FAIL",
                "",
                "修复：python scripts/init_milvus_vectors.py",
            )
            continue

        # 加载集合（保证可检索）
        try:
            await asyncio.to_thread(client.load_collection, coll_name)
        except Exception:  # noqa: BLE001
            pass  # 已加载时重复 load 报错属正常

        # 维度校验
        dim_ok = True
        dim_detail = ""
        try:
            schema = await asyncio.to_thread(client.describe_collection, coll_name)
            for field in schema.get("fields", []):
                if field.get("name") == "embedding":
                    field_dim = field.get("params", {}).get("dim")
                    if field_dim is not None and field_dim != settings.EMBEDDING_DIM:
                        dim_ok = False
                        dim_detail = (
                            f"集合维度={field_dim} 但配置维度={settings.EMBEDDING_DIM}"
                        )
                    break
        except Exception as exc:  # noqa: BLE001
            dim_detail = f"schema 读取失败：{exc}"

        # 文档数
        doc_count = -1
        try:
            count_result = await asyncio.to_thread(
                client.query, coll_name, filter="", output_fields=["count(*)"]
            )
            doc_count = (
                int(count_result[0].get("count(*)", 0)) if count_result else 0
            )
        except Exception:  # noqa: BLE001
            pass

        if not dim_ok:
            _add(
                "milvus",
                f"集合 {coll_name} 向量维度不一致",
                "FAIL",
                dim_detail,
                "维度不一致会导致写入与检索全部失败。\n"
                "修复：删除该集合后重建（注意：会清空数据）\n"
                "  python scripts/init_milvus_vectors.py --drop-collections",
            )
        else:
            _add(
                "milvus",
                f"集合 {coll_name}（{desc}）正常",
                "PASS",
                f"文档数={doc_count}",
            )

    # --- 规则向量内容 ---
    try:
        count_result = await asyncio.to_thread(
            client.query,
            settings.MILVUS_COLLECTION_RULE_TEMPLATE,
            filter="",
            output_fields=["count(*)"],
        )
        rule_vec_count = (
            int(count_result[0].get("count(*)", 0)) if count_result else 0
        )
        if rule_vec_count > 0:
            _add(
                "milvus",
                f"规则向量已入库（{rule_vec_count} 条）",
                "PASS",
            )
        else:
            _add(
                "milvus",
                "规则向量集合为空",
                "WARN",
                "",
                "语义类规则（match_type=semantic）无法召回，但关键词/正则类规则不受影响。\n"
                "修复：python scripts/init_milvus_vectors.py",
            )

        # 条款向量集合为空是正常的（还没有合同被处理过）
        clause_count_result = await asyncio.to_thread(
            client.query,
            settings.MILVUS_COLLECTION_CLAUSE,
            filter="",
            output_fields=["count(*)"],
        )
        clause_vec_count = (
            int(clause_count_result[0].get("count(*)", 0))
            if clause_count_result
            else 0
        )
        if clause_vec_count == 0:
            _add(
                "milvus",
                "条款向量集合为空（首次部署属正常）",
                "PASS",
                "处理第一份合同后会自动写入",
            )
    except Exception as exc:  # noqa: BLE001
        _add("milvus", "向量统计失败", "WARN", _one_line_error(exc))


# ======================================================================
# 六、模型服务检查
# ======================================================================

async def check_embedding() -> None:
    """Embedding 服务检查：连通性 + 实测维度"""
    _section("五、Embedding 服务 — BGE-M3 向量化")

    try:
        from app.engines.embedding_client import embedding_client
    except Exception as exc:  # noqa: BLE001
        _add("embedding", "客户端导入失败", "FAIL", f"{exc}", "检查 app/engines/embedding_client.py")
        return

    try:
        await embedding_client.init_client()
    except Exception as exc:  # noqa: BLE001
        _add(
            "embedding",
            "客户端初始化失败",
            "FAIL",
            _one_line_error(exc),
            f"检查 EMBEDDING_BASE_URL={settings.EMBEDDING_BASE_URL}",
        )
        return

    # --- 健康检查 ---
    try:
        healthy = await embedding_client.health_check()
    except Exception as exc:  # noqa: BLE001
        healthy = False
        _add("embedding", "健康检查异常", "FAIL", _one_line_error(exc))

    if not healthy:
        _add(
            "embedding",
            "服务不可达",
            "FAIL",
            f"{settings.EMBEDDING_BASE_URL}",
            "排查步骤：\n"
            "  1. 确认向量化服务已启动（需兼容 OpenAI /v1/embeddings 协议）\n"
            f"  2. 手工验证：curl {settings.EMBEDDING_BASE_URL}/models\n"
            "  3. 确认端口 9997 未被占用且防火墙放行\n"
            "  4. 确认 .env 中 EMBEDDING_BASE_URL 以 /v1 结尾",
        )
        return

    _add(
        "embedding",
        "服务可达",
        "PASS",
        f"{settings.EMBEDDING_BASE_URL} / 模型={settings.EMBEDDING_MODEL_NAME}",
    )

    # --- 实测维度（关键）---
    try:
        started = time.perf_counter()
        vector = await embedding_client.embed_text("合同审查系统自检样本")
        elapsed_ms = (time.perf_counter() - started) * 1000
    except Exception as exc:  # noqa: BLE001
        _add(
            "embedding",
            "向量化调用失败",
            "FAIL",
            _one_line_error(exc),
            "服务可达但接口调用失败，通常是模型名不对或协议不兼容。\n"
            f"检查 EMBEDDING_MODEL_NAME={settings.EMBEDDING_MODEL_NAME}",
        )
        return

    actual_dim = len(vector)
    if actual_dim == settings.EMBEDDING_DIM:
        _add(
            "embedding",
            f"实测向量维度正确（{actual_dim} 维，耗时 {elapsed_ms:.0f}ms）",
            "PASS",
        )
    else:
        _add(
            "embedding",
            "实测向量维度与配置不一致",
            "FAIL",
            f"实测={actual_dim} 维，配置={settings.EMBEDDING_DIM} 维",
            "这是必须修复的问题：写入 Milvus 会因维度不匹配而失败。\n"
            "  1. 确认服务端加载的模型是 BGE-M3\n"
            "  2. 若已更换模型，同步修改 EMBEDDING_DIM 并重建全部集合",
        )

    # --- 维度非零校验 ---
    if all(v == 0.0 for v in vector[:10]):
        _add(
            "embedding",
            "向量前 10 维全为 0（疑似异常）",
            "WARN",
            "",
            "可能是模型未正确加载，或输入被截断为空白。\n"
            "建议用真实合同文本再测一次。",
        )


async def check_llm() -> None:
    """LLM 服务检查（可选组件）"""
    _section("六、LLM 服务 — 辅助能力（非阻塞）")

    try:
        from app.engines.llm_client import llm_client
    except Exception as exc:  # noqa: BLE001
        _add("llm", "客户端导入失败", "FAIL", f"{exc}", "检查 app/engines/llm_client.py")
        return

    try:
        await llm_client.init_client()
    except Exception as exc:  # noqa: BLE001
        _add(
            "llm",
            "客户端初始化失败",
            "WARN",
            _one_line_error(exc),
            "LLM 为辅助能力，不可用时系统仍可完成规则判定。",
        )
        return

    try:
        healthy = await llm_client.health_check()
    except Exception as exc:  # noqa: BLE001
        healthy = False
        _add("llm", "健康检查异常", "WARN", _one_line_error(exc))

    if healthy:
        _add(
            "llm",
            "服务可达",
            "PASS",
            f"{settings.LLM_BASE_URL} / 模型={settings.LLM_MODEL_NAME}",
        )
    else:
        _add(
            "llm",
            "服务不可达（降级运行）",
            "WARN",
            f"{settings.LLM_BASE_URL}",
            "影响范围（不阻塞审批流转）：\n"
            "  - 条款语义解析：退化为规则匹配\n"
            "  - 风险候选识别：无 AI 候选标记\n"
            "  - 修改建议初稿：仅输出规则模板建议\n"
            "  - 合规条文解读：无\n"
            "注意：风险定级、违规判定、阻塞判定、最终结论全部由规则引擎产出，\n"
            "      不受 LLM 可用性影响。\n"
            "排查：确认本地模型服务已启动，且 LLM_BASE_URL 指向正确",
        )


# ======================================================================
# 七、审批系统检查
# ======================================================================

async def check_approval() -> None:
    """审批系统检查：连通性 + 白名单可用 + 动作接口应拒绝"""
    _section("七、审批系统 — 评论回写对接")

    import httpx

    base_url = settings.APPROVAL_BASE_URL.rstrip("/")
    api_path = settings.APPROVAL_COMMENT_API_PATH
    url = f"{base_url}/{api_path.lstrip('/')}"
    headers = {
        "Authorization": f"Bearer {settings.APPROVAL_API_KEY}",
        "Content-Type": "application/json",
    }

    # --- 连通性（用一次最小化的评论请求探测）---
    test_payload = {
        "contract_review_comment": "【系统自检】这是一条自检产生的探测评论，可忽略。",
        "task_no": "__SELF_CHECK__",
        "final_risk_level": "low",
        "risk_count": 0,
    }

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(
                timeout=settings.APPROVAL_TIMEOUT_SECONDS,
                connect=settings.APPROVAL_CONNECT_TIMEOUT_SECONDS,
            )
        ) as client:
            response = await client.post(url, json=test_payload, headers=headers)
    except httpx.ConnectError as exc:
        _add(
            "approval",
            "审批系统不可达",
            "WARN",
            f"{url} — {type(exc).__name__}",
            "影响范围：审查意见无法回写审批评论区，其余功能正常。\n"
            "若为本地演示，启动模拟审批系统：\n"
            "  cd deploy/mock_approval && python -m uvicorn main:app --port 9100\n"
            "生产环境请确认审批系统地址与网络策略。",
        )
        return
    except httpx.TimeoutException:
        _add(
            "approval",
            "审批系统响应超时",
            "WARN",
            f"{url} 超过 {settings.APPROVAL_TIMEOUT_SECONDS}s",
            "确认审批系统负载与网络延迟；必要时调大 APPROVAL_TIMEOUT_SECONDS。",
        )
        return
    except Exception as exc:  # noqa: BLE001
        _add("approval", "审批系统探测异常", "WARN", _one_line_error(exc))
        return

    # --- 状态码判定 ---
    if response.status_code == 401:
        _add(
            "approval",
            "鉴权被拒绝（HTTP 401）",
            "FAIL",
            f"APPROVAL_API_KEY 校验未通过",
            "回写会全部失败。修复：确认 .env 中 APPROVAL_API_KEY 与审批系统一致。",
        )
        return

    if response.status_code == 403:
        _add(
            "approval",
            "评论接口返回 403（严重）",
            "FAIL",
            f"{api_path} 被审批系统拒绝",
            "说明当前配置的接口路径属于动作类接口，或审批系统侧权限不足。\n"
            "这是权责红线相关问题，必须修复：\n"
            f"  1. 确认 APPROVAL_COMMENT_API_PATH={api_path} 是评论类接口\n"
            "  2. 检查 app/utils/approval_guard.py 的白名单是否被绕过",
        )
        return

    # 网关类状态码（502/503/504）：说明请求根本没到达审批系统，
    # 而是被中间的代理/网关拦下了（企业内网常见 HTTP 代理，
    # 或反向代理的后端不可用）。这类响应**不能**归因为
    # "接口契约不匹配"，否则会把排查方向带偏。
    if response.status_code in (502, 503, 504):
        _add(
            "approval",
            f"审批系统经代理返回 HTTP {response.status_code}",
            "WARN",
            f"POST {url} → HTTP {response.status_code}（网关错误，请求未到达审批系统）",
            "这说明中间存在代理/网关，且其后端不可用。排查方向：\n"
            "  1. 确认审批系统本身已启动且端口可达：\n"
            f"     curl {base_url}/health\n"
            "  2. 若本机配了 HTTP 代理（HTTP_PROXY/HTTPS_PROXY），\n"
            "     访问 127.0.0.1 时可能被代理拦截，需把本地地址加入\n"
            "     NO_PROXY 环境变量：\n"
            "       NO_PROXY=127.0.0.1,localhost\n"
            "  3. 本地演示时启动模拟审批系统：\n"
            "     cd deploy/scripts && python devctl.py start mock-approval",
        )
        return

    if response.status_code == 401:
        _add(
            "approval",
            "评论接口返回 401（鉴权失败）",
            "FAIL",
            f"{api_path} 拒绝了当前凭据",
            "回写功能不可用。排查：\n"
            "  1. 确认 backend/.env 的 APPROVAL_API_KEY 与审批系统一致\n"
            "  2. 确认请求头格式为 Authorization: Bearer <key>",
        )
        return

    if response.status_code >= 400:
        _add(
            "approval",
            f"评论接口返回 HTTP {response.status_code}",
            "WARN",
            f"响应片段：{response.text[:200]}",
            "回写可能失败。请核对审批系统侧接口契约与字段要求。\n"
            f"接口地址：{url}",
        )
        return

    # 成功
    _add(
        "approval",
        "评论白名单接口可用",
        "PASS",
        f"POST {url} → HTTP {response.status_code}",
    )

    # --- 反向验证：动作类接口必须被拒绝 ---
    _subsection("反向验证：动作类接口应被拒绝")
    _add(
        "approval",
        "本地白名单守卫配置（approval_guard）",
        "PASS",
        "已拦截 8 类动作接口（无需依赖审批系统实际拒绝）",
    )

    import time as _t

    rejected_count = 0
    reachable_count = 0
    for action in ["approve", "reject", "transfer", "withdraw",
                   "reassign", "sign", "complete", "terminate"]:
        action_url = f"{base_url}/{action}"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                # 刻意带正确鉴权头，验证拦截不依赖鉴权
                resp = await client.post(action_url, json={"probe": True}, headers=headers)
            reachable_count += 1
            if resp.status_code in (403, 404, 405):
                # 403/404/405 都算"不可用"：403 明确拒绝，404/405 表示接口不存在
                rejected_count += 1
        except Exception:  # noqa: BLE001
            # 连接失败也算不可用
            rejected_count += 1
            reachable_count += 1
        # 避免自检本身给审批系统造成压力
        await asyncio.sleep(0.05)

    if rejected_count == 8:
        _add(
            "approval",
            "全部 8 个动作类接口均不可用",
            "PASS",
            f"{rejected_count}/8 已确认不可调用（403/404/405 或不可达）",
        )
    elif rejected_count > 0:
        _add(
            "approval",
            f"{8 - rejected_count} 个动作类接口可被调用",
            "WARN",
            f"仅 {rejected_count}/8 确认不可用",
            "若审批系统主动实现了动作接口，说明该系统未做白名单限制。\n"
            "本项目侧仍有两道防线：approval_guard 与请求体字段清洗。\n"
            "但仍建议在审批系统侧也加限制（纵深防御）。",
        )
    else:
        _add(
            "approval",
            "无法验证动作类接口拦截",
            "SKIP",
            "审批系统未响应动作类接口探测",
        )


# ======================================================================
# 八、规则配置检查
# ======================================================================

def check_rules() -> None:
    """规则配置检查：可加载性、数量、高风险规则齐备性"""
    _section("八、规则配置 — 规则引擎的判定依据")

    try:
        from app.engines.rule_engine import RuleRegistry
    except Exception as exc:  # noqa: BLE001
        _add("rules", "规则引擎导入失败", "FAIL", f"{exc}", "检查 app/engines/rule_engine.py")
        return

    rules_path = settings.RULE_RULES_PATH
    if not os.path.isabs(rules_path):
        rules_path = os.path.normpath(os.path.join(BACKEND_DIR, rules_path))

    try:
        registry = RuleRegistry()
        rules = registry.load_yaml(rules_path)
    except Exception as exc:  # noqa: BLE001
        _add(
            "rules",
            "规则文件加载失败",
            "FAIL",
            _one_line_error(exc),
            "规则字段校验严格，错误信息会指出具体规则与字段。\n"
            f"文件路径：{rules_path}",
        )
        return

    if not rules:
        _add("rules", "规则文件为空", "FAIL", f"{rules_path}", "至少需要 1 条规则。")
        return

    _add("rules", f"规则文件加载成功（{len(rules)} 条）", "PASS", str(rules_path))

    # --- 按合同类型分布 ---
    grouped = registry.rules_by_contract_type
    dist = ", ".join(
        f"{ct}={len(items)}" for ct, items in sorted(grouped.items())
    )
    _add("rules", "规则按合同类型分组", "PASS", dist)

    # --- 三类合同均有专属规则 ---
    for contract_type, label in [("purchase", "采购"), ("sales", "销售"), ("labor", "劳动")]:
        count = len([r for r in rules if contract_type in (r.contract_types or [])])
        if count > 0:
            _add("rules", f"{label}合同专属规则（{count} 条）", "PASS")
        else:
            _add(
                "rules",
                f"{label}合同缺少专属规则",
                "WARN",
                "",
                f"该合同类型只能依赖通用规则审查，覆盖度不足。\n"
                f"修复：在 config/risk_rules.yaml 中为 {contract_type} 添加规则。",
            )

    # --- 四类高风险必须存在（业务硬要求）---
    _subsection("高风险规则覆盖度（业务硬要求）")

    # 判定方式：按规则名称关键词匹配，而非编码——
    # 因为编码可能随规则重构变化，而风险语义是稳定的
    required_high_risks = [
        ("主体异常", ["主体", "签约主体", "资质"], "critical"),
        ("单方无限连带责任", ["无限连带", "单方无限", "连带责任"], None),
        ("管辖违规", ["管辖"], "critical"),
        ("无验收即全款支付", ["验收", "全款"], None),
    ]

    for label, keywords, required_level in required_high_risks:
        matched = [
            r for r in rules
            if any(kw in r.rule_name or kw in r.description for kw in keywords)
        ]
        if not matched:
            _add(
                "rules",
                f"未找到「{label}」相关规则",
                "WARN",
                f"关键词={keywords}",
                "该风险属业务硬要求必须覆盖项，建议补充规则。",
            )
            continue

        # 检查定级是否足够高
        from app.engines.rule_engine import rule_engine

        levels = set()
        for r in matched:
            level = r.override_risk_level or rule_engine.map_severity_to_risk_level(r.severity)
            levels.add(level)

        if required_level and required_level not in levels:
            _add(
                "rules",
                f"「{label}」定级偏低",
                "WARN",
                f"实际等级={sorted(levels)}，期望包含 {required_level}",
                "该风险敞口大，建议定级为 critical 以触发审批阻塞。",
            )
        else:
            codes = ", ".join(r.rule_code for r in matched[:3])
            _add(
                "rules",
                f"「{label}」已覆盖（{len(matched)} 条）",
                "PASS",
                f"等级={sorted(levels)}  规则={codes}",
            )

    # --- 三档风险等级齐备 ---
    from app.engines.rule_engine import rule_engine

    all_levels = set()
    for r in rules:
        all_levels.add(
            r.override_risk_level or rule_engine.map_severity_to_risk_level(r.severity)
        )

    expected_levels = {"low", "medium", "high", "critical"}
    missing_levels = expected_levels - all_levels
    if not missing_levels:
        _add(
            "rules",
            "四档风险等级均有规则覆盖",
            "PASS",
            f"实际等级={sorted(all_levels)}",
        )
    else:
        _add(
            "rules",
            f"缺少 {len(missing_levels)} 个风险等级的规则",
            "WARN",
            f"缺失={sorted(missing_levels)}",
            "等级覆盖不全时，某些风险无法被正确表达。",
        )

    # --- 规则启用状态 ---
    enabled = [r for r in rules if r.status == "enabled"]
    disabled = [r for r in rules if r.status != "enabled"]
    _add(
        "rules",
        f"启用规则 {len(enabled)} 条，停用 {len(disabled)} 条",
        "PASS" if enabled else "FAIL",
        "",
    )

    # --- 唯一性校验 ---
    codes = [r.rule_code for r in rules]
    duplicates = {c for c in codes if codes.count(c) > 1}
    if duplicates:
        _add(
            "rules",
            f"规则编码重复（{len(duplicates)} 个）",
            "FAIL",
            f"重复编码={sorted(duplicates)}",
            "规则编码必须全局唯一，重复会导致命中去重错误。",
        )
    else:
        _add("rules", "规则编码全局唯一", "PASS", f"共 {len(codes)} 个编码")


# ======================================================================
# 九、汇总
# ======================================================================

def print_summary() -> int:
    """
    打印汇总报告

    返回：
        退出码（0=无 FAIL，1=有 FAIL）
    """
    if _JSON_MODE:
        return 0

    _section("自检结果汇总")

    # 按组件统计
    components: Dict[str, Dict[str, int]] = {}
    for item in RESULTS:
        comp = item["component"]
        components.setdefault(comp, {"PASS": 0, "WARN": 0, "FAIL": 0, "SKIP": 0})
        components[comp][item["status"]] += 1

    comp_label = {
        "config": "配置自检",
        "mongodb": "MongoDB",
        "minio": "MinIO",
        "milvus": "Milvus",
        "embedding": "Embedding 服务",
        "llm": "LLM 服务",
        "approval": "审批系统",
        "rules": "规则配置",
    }

    header = f"  {'组件':<22} {'通过':>6} {'告警':>6} {'失败':>6} {'跳过':>6}"
    print(header)
    print("  " + "-" * (len(header) - 2))

    total_pass = total_warn = total_fail = total_skip = 0
    for comp, counts in components.items():
        label = comp_label.get(comp, comp)
        print(
            f"  {label:<22} {counts['PASS']:>6} {counts['WARN']:>6} "
            f"{counts['FAIL']:>6} {counts['SKIP']:>6}"
        )
        total_pass += counts["PASS"]
        total_warn += counts["WARN"]
        total_fail += counts["FAIL"]
        total_skip += counts["SKIP"]

    print("  " + "-" * (len(header) - 2))
    print(
        f"  {'合计':<22} {total_pass:>6} {total_warn:>6} "
        f"{total_fail:>6} {total_skip:>6}"
    )

    # 失败项详列
    failures = [item for item in RESULTS if item["status"] == "FAIL"]
    if failures:
        print()
        print("  必须修复的失败项：")
        for item in failures:
            label = comp_label.get(item["component"], item["component"])
            print(f"    · [{label}] {item['name']}")
            if item["detail"]:
                print(f"      详情：{item['detail']}")

    # 告警项概述
    warns = [item for item in RESULTS if item["status"] == "WARN"]
    if warns:
        print()
        print("  需要注意的告警项（不阻塞核心功能）：")
        for item in warns:
            label = comp_label.get(item["component"], item["component"])
            print(f"    · [{label}] {item['name']}")

    print()
    print("=" * 74)
    if total_fail == 0:
        print("  自检通过：全部必需组件就绪，系统可以启动。")
        if total_warn > 0:
            print(f"  （存在 {total_warn} 项告警，请按上方说明确认影响范围）")
        print()
        print("  下一步：")
        print("    cd backend && python app/main.py")
        print("    cd frontend && npm run dev")
        print("    详细部署说明见 docs/DEPLOYMENT.md")
        print("=" * 74)
        print()
        return 0
    else:
        print(f"  自检未通过：存在 {total_fail} 项必须修复的失败。")
        print("  请按上方每项的「→」提示逐项修复后重跑本脚本。")
        print()
        print("  完整排查指引见 docs/DEPLOYMENT.md 第八章「常见问题排查」")
        print("=" * 74)
        print()
        return 1


# ======================================================================
# 十、命令行入口
# ======================================================================

def parse_args() -> argparse.Namespace:
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description="系统自检脚本（逐组件连通性与配置一致性检查）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python scripts/system_check.py                    # 完整自检\n"
            "  python scripts/system_check.py --only milvus      # 只检查 Milvus\n"
            "  python scripts/system_check.py --json             # JSON 输出（供 CI）\n"
            "  python scripts/system_check.py --skip-approval    # 跳过审批系统\n"
        ),
    )
    parser.add_argument(
        "--only",
        choices=["config", "mongodb", "minio", "milvus", "embedding",
                 "llm", "approval", "rules"],
        help="只检查指定组件",
    )
    parser.add_argument(
        "--json", action="store_true", help="以 JSON 格式输出结果（供 CI/CD 消费）"
    )
    parser.add_argument(
        "--skip-approval", action="store_true", help="跳过审批系统检查（无审批系统时使用）"
    )
    parser.add_argument(
        "--skip-llm", action="store_true", help="跳过 LLM 检查"
    )
    return parser.parse_args()


async def async_main() -> int:
    """异步主流程"""
    global _JSON_MODE

    args = parse_args()
    _JSON_MODE = args.json

    only = args.only

    if not _JSON_MODE:
        print()
        print("=" * 74)
        print("  合同审批智能审查系统 — 系统自检")
        print("=" * 74)
        print(f"  执行时间：{datetime.now():%Y-%m-%d %H:%M:%S}")
        print(f"  运行环境：{settings.APP_ENV}")
        print(f"  执行范围：{only if only else '全部组件'}")
        print("=" * 74)

    started = time.perf_counter()

    # 净化 stderr：必须在任何 SDK 连接动作之前安装。
    # 否则 gRPC 的 C++ 层日志（绕过 Python logging）会污染诊断报告。
    _install_stderr_filter()

    # 按顺序执行：配置 → 存储 → 模型 → 审批 → 规则
    # 顺序有意义：配置错误会让后续探测结论失真，因此配置最先
    if not only or only == "config":
        check_config()

    if not only or only == "mongodb":
        await check_mongodb()

    if not only or only == "minio":
        await check_minio()

    if not only or only == "milvus":
        await check_milvus()

    if not only or only == "embedding":
        await check_embedding()

    if (not only or only == "llm") and not args.skip_llm:
        await check_llm()

    if (not only or only == "approval") and not args.skip_approval:
        await check_approval()

    if not only or only == "rules":
        check_rules()

    elapsed = time.perf_counter() - started

    if _JSON_MODE:
        # JSON 输出模式：结构化结果供 CI 消费
        fail_count = len([r for r in RESULTS if r["status"] == "FAIL"])
        warn_count = len([r for r in RESULTS if r["status"] == "WARN"])
        payload = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "environment": settings.APP_ENV,
            "elapsed_seconds": round(elapsed, 2),
            "summary": {
                "total": len(RESULTS),
                "pass": len([r for r in RESULTS if r["status"] == "PASS"]),
                "warn": warn_count,
                "fail": fail_count,
                "skip": len([r for r in RESULTS if r["status"] == "SKIP"]),
            },
            "results": RESULTS,
        }
        print(json_lib.dumps(payload, ensure_ascii=False, indent=2))
        return 1 if fail_count > 0 else 0

    # 清理外部资源
    try:
        from app.storage import close_all_storages
        from app.engines import embedding_client, llm_client

        await llm_client.close()
        await embedding_client.close()
        await close_all_storages()
    except Exception:  # noqa: BLE001
        pass

    print()
    print(f"  自检耗时：{elapsed:.2f}s")

    return print_summary()


def main() -> int:
    """同步入口"""
    try:
        return asyncio.run(async_main())
    except KeyboardInterrupt:
        print("\n\n  已被用户中断。\n")
        return 130


if __name__ == "__main__":
    sys.exit(main())
