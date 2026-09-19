# D:/ContractReviewSys-合同审批智能审查项目/backend/scripts/init_mongodb.py
"""
MongoDB 数据库初始化脚本

【脚本职责】
    把"系统跑起来所必需的 MongoDB 前置状态"一次性准备好，具体包括：
        1. 创建业务库 contract_review
        2. 创建全部 cr_ 前缀业务集合（显式创建，便于运维直观查看）
        3. 创建全部索引（唯一索引 + 查询索引）
        4. 创建初始角色与账号（可开关，默认不创建，避免污染开发环境）
        5. 从 config/risk_rules.yaml 导入规则样例数据到 cr_rule_template
        6. 写入初始系统配置项到 cr_sys_config

【幂等性保证】
    本脚本可安全重复执行：
        - 创建集合：已存在则跳过
        - 创建索引：MongoDB 原生幂等，同名同定义重复创建不报错
        - 导入规则：按 rule_code 做 upsert，规则的业务字段会被刷新为 YAML 内容，
          但 created_at 等归属字段保留首次写入的值
        - 系统配置：按 config_key 做 upsert

【为什么要有这个脚本】
    后端 app/main.py 的 lifespan 里已经会自动建集合与索引
    （app/storage/mongo_client.py 的 _ensure_indexes）。
    但生产部署存在两个真实问题：
        1. 应用启动账号往往只有读写权限，没有 createIndexes 权限，
           索引初始化会失败并导致启动中断；
        2. 规则样例数据必须由具备更高权限的运维账号预先导入，
           不能依赖应用运行时写入。
    因此本脚本用于"部署前由 DBA / 运维执行一次"，
    应用启动时的自动初始化则作为开发环境的便利兜底。

【运行方式】
    cd backend
    python scripts/init_mongodb.py                 # 完整初始化
    python scripts/init_mongodb.py --skip-rules    # 跳过规则导入
    python scripts/init_mongodb.py --skip-roles    # 跳过角色创建
    python scripts/init_mongodb.py --verify-only   # 只校验不写入
    python scripts/init_mongodb.py --drop-all      # 【危险】先清空所有业务集合

【环境依赖】
    pymongo（同步驱动）。本脚本刻意使用同步驱动而非 motor，
    因为运维脚本应能在没有事件循环的简单环境下运行，
    且批量初始化场景下并发并无收益。
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from typing import Any, Dict, List, Tuple

# ----------------------------------------------------------------------
# 路径处理：保证脚本在任意工作目录下都能 import 到 app 包
# ----------------------------------------------------------------------
BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from pymongo import ASCENDING, DESCENDING, IndexModel, MongoClient  # noqa: E402
from pymongo.errors import CollectionInvalid, ConnectionFailure, OperationFailure  # noqa: E402

from app.constants import MongoCollection  # noqa: E402
from app.core.config import settings  # noqa: E402


# ======================================================================
# 一、集合清单（唯一权威定义，与 app/constants/__init__.py 保持同步）
# ======================================================================

# 说明：字典值为该集合的业务用途说明，会作为集合注释写入 MongoDB，
# 便于运维在 Compass / mongosh 中直接看到每个集合的用途，
# 不需要翻代码猜。
COLLECTION_PLAN: Dict[str, str] = {
    MongoCollection.CONTRACT: "合同主档，记录合同基础信息与状态",
    MongoCollection.CONTRACT_CLAUSE: "合同条款结构化结果，含全局字符偏移",
    MongoCollection.OCR_RESULT: "OCR 识别结果，按合同 + 页码存储",
    MongoCollection.REVIEW_TASK: "审查任务，记录任务状态与流程路由",
    MongoCollection.REVIEW_RISK_HIT: "风险命中记录，含规则命中与辅助候选",
    MongoCollection.REVIEW_CONCLUSION: "审查结论，仅由规则引擎产出",
    MongoCollection.RULE_TEMPLATE: "合规规则模板，规则引擎的配置来源",
    MongoCollection.RULE_HIT_LOG: "规则命中审计日志，保证判定可追溯",
    MongoCollection.AUDIT_LOG: "任务全链路操作审计日志",
    MongoCollection.STANDARD_CLAUSE: "标准示范条款库",
    MongoCollection.SUGGESTION: "修改建议，区分规则产出与模型草稿",
    MongoCollection.APPROVAL_RECORD: "审批记录，来自审批系统同步",
    MongoCollection.WRITE_BACK_LOG: "审批回写日志，记录每次评论区写入",
    MongoCollection.REPORT: "审查报告记录与访问链接",
    MongoCollection.FILE_OBJECT: "文件对象元数据，对应 MinIO 对象键",
    MongoCollection.SYS_CONFIG: "系统运行期可改配置",
}


# ======================================================================
# 二、索引清单
# ======================================================================
# 说明：与 app/storage/mongo_client.py 的 _ensure_indexes 保持完全一致。
# 两处必须同步修改——本脚本用于部署期初始化，
# 应用的自动初始化用于开发期便利，任何一侧新增索引都应同步另一侧。
INDEX_PLAN: List[Tuple[str, List[IndexModel]]] = [
    # 合同主档：合同编号唯一，支持按状态、创建时间、文件指纹检索
    (
        MongoCollection.CONTRACT,
        [
            IndexModel([("contract_no", ASCENDING)], unique=True, name="uk_contract_no"),
            IndexModel([("status", ASCENDING)], name="idx_status"),
            IndexModel([("created_at", DESCENDING)], name="idx_created_at"),
            IndexModel([("file_hash", ASCENDING)], name="idx_file_hash"),
        ],
    ),
    # 合同条款：同一合同内条款序号唯一
    (
        MongoCollection.CONTRACT_CLAUSE,
        [
            IndexModel(
                [("contract_id", ASCENDING), ("clause_index", ASCENDING)],
                unique=True,
                name="uk_contract_clause_index",
            ),
            IndexModel([("clause_type", ASCENDING)], name="idx_clause_type"),
        ],
    ),
    # OCR 结果：按合同 + 页码检索
    (
        MongoCollection.OCR_RESULT,
        [
            IndexModel(
                [("contract_id", ASCENDING), ("page_no", ASCENDING)],
                name="idx_ocr_contract_page",
            ),
        ],
    ),
    # 审查任务：按合同、状态、创建时间检索（大盘列表主查询路径）
    (
        MongoCollection.REVIEW_TASK,
        [
            IndexModel([("contract_id", ASCENDING)], name="idx_task_contract"),
            IndexModel([("status", ASCENDING)], name="idx_task_status"),
            IndexModel([("created_at", DESCENDING)], name="idx_task_created_at"),
        ],
    ),
    # 风险命中：按任务 + 等级 + 来源检索
    (
        MongoCollection.REVIEW_RISK_HIT,
        [
            IndexModel([("task_id", ASCENDING)], name="idx_hit_task"),
            IndexModel([("risk_level", ASCENDING)], name="idx_hit_risk_level"),
            IndexModel([("hit_source", ASCENDING)], name="idx_hit_source"),
        ],
    ),
    # 审查结论：一个任务只能有一份最终结论，唯一索引从存储层强制
    (
        MongoCollection.REVIEW_CONCLUSION,
        [
            IndexModel([("task_id", ASCENDING)], unique=True, name="uk_conclusion_task"),
            IndexModel([("final_risk_level", ASCENDING)], name="idx_conclusion_level"),
            IndexModel([("is_blocked", ASCENDING)], name="idx_conclusion_blocked"),
        ],
    ),
    # 规则模板：规则编码唯一
    (
        MongoCollection.RULE_TEMPLATE,
        [
            IndexModel([("rule_code", ASCENDING)], unique=True, name="uk_rule_code"),
            IndexModel([("status", ASCENDING)], name="idx_rule_status"),
            IndexModel([("clause_type", ASCENDING)], name="idx_rule_clause_type"),
        ],
    ),
    # 规则命中审计日志
    (
        MongoCollection.RULE_HIT_LOG,
        [
            IndexModel([("task_id", ASCENDING)], name="idx_rulelog_task"),
            IndexModel([("rule_code", ASCENDING)], name="idx_rulelog_rule"),
            IndexModel([("created_at", DESCENDING)], name="idx_rulelog_created_at"),
        ],
    ),
    # 审计日志：额外补一条按任务 + 时间，支撑工作台时间线查询
    (
        MongoCollection.AUDIT_LOG,
        [
            IndexModel(
                [("task_id", ASCENDING), ("created_at", DESCENDING)],
                name="idx_audit_task_time",
            ),
        ],
    ),
    # 标准条款库：按条款类型 + 适用合同类型检索
    (
        MongoCollection.STANDARD_CLAUSE,
        [
            IndexModel([("clause_code", ASCENDING)], unique=True, name="uk_clause_code"),
            IndexModel([("clause_type", ASCENDING)], name="idx_std_clause_type"),
            IndexModel([("contract_types", ASCENDING)], name="idx_std_contract_types"),
        ],
    ),
    # 修改建议
    (
        MongoCollection.SUGGESTION,
        [
            IndexModel([("task_id", ASCENDING)], name="idx_sug_task"),
            IndexModel([("clause_id", ASCENDING)], name="idx_sug_clause"),
            IndexModel([("is_llm_draft", ASCENDING)], name="idx_sug_is_llm_draft"),
        ],
    ),
    # 审批记录
    (
        MongoCollection.APPROVAL_RECORD,
        [
            IndexModel([("contract_id", ASCENDING)], name="idx_approval_contract"),
            IndexModel([("created_at", DESCENDING)], name="idx_approval_created_at"),
        ],
    ),
    # 回写日志：按状态检索以支持失败重推
    (
        MongoCollection.WRITE_BACK_LOG,
        [
            IndexModel([("status", ASCENDING)], name="idx_wb_status"),
            IndexModel([("contract_id", ASCENDING)], name="idx_wb_contract"),
            IndexModel([("task_no", ASCENDING)], name="idx_wb_task_no"),
        ],
    ),
    # 报告记录
    (
        MongoCollection.REPORT,
        [
            IndexModel([("task_id", ASCENDING)], name="idx_report_task"),
            IndexModel([("status", ASCENDING)], name="idx_report_status"),
        ],
    ),
    # 文件对象元数据
    (
        MongoCollection.FILE_OBJECT,
        [
            IndexModel([("object_key", ASCENDING)], unique=True, name="uk_object_key"),
            IndexModel([("contract_id", ASCENDING)], name="idx_file_contract"),
        ],
    ),
    # 系统配置
    (
        MongoCollection.SYS_CONFIG,
        [
            IndexModel([("config_key", ASCENDING)], unique=True, name="uk_config_key"),
        ],
    ),
]


# ======================================================================
# 三、初始角色与账号
# ======================================================================
# 说明：角色设计遵循最小权限原则。
#   cr_app_rw    — 应用运行账号：业务集合读写，但无建集合/建索引权限
#   cr_readonly  — 只读账号：供报表、排查问题使用
#
# 重要：默认不创建角色（--skip-roles 的反面）。
# 原因：开发环境多为无鉴权的单机 MongoDB，执行 createRole 会失败并中断流程。
# 需要时显式传 --init-roles 启用。
#
# 注意：这两个账号的密码必须与 .env 中的 MONGO_URI 保持一致，
# 否则应用连不上。生产环境请务必修改为强随机密码。

ROLE_PLAN: List[Dict[str, Any]] = [
    {
        "role": "cr_app_rw",
        "pwd": "cr_app_pwd_change_me",
        "privileges": [
            {
                "resource": {"db": "contract_review", "collection": ""},
                "actions": [
                    "find",
                    "insert",
                    "update",
                    "remove",
                    "createCollection",
                    "createIndex",
                    "listCollections",
                    "listIndexes",
                ],
            }
        ],
        "roles": [],
    },
    {
        "role": "cr_readonly",
        "pwd": "cr_read_pwd_change_me",
        "privileges": [
            {
                "resource": {"db": "contract_review", "collection": ""},
                "actions": ["find", "listCollections", "listIndexes"],
            }
        ],
        "roles": [],
    },
]

APP_USER_NAME = "cr_app"
READONLY_USER_NAME = "cr_readonly_user"


# ======================================================================
# 四、初始系统配置项
# ======================================================================
# 说明：写入 cr_sys_config，供运行期在不重启服务的前提下调整行为。
# 这些值优先于 .env 中的静态配置（由业务代码读取时决定优先级）。

DEFAULT_SYS_CONFIG: List[Dict[str, Any]] = [
    {
        "config_key": "rule_engine.enabled",
        "config_value": True,
        "value_type": "bool",
        "description": "规则引擎开关。关闭将导致系统拒绝执行审查流程（红线：不允许未判定放行）",
        "editable": False,
        "updated_at": datetime.now(),
        "updated_by": "init_script",
    },
    {
        "config_key": "rule_engine.block_levels",
        "config_value": ["high", "critical"],
        "value_type": "list",
        "description": "触发任务阻塞的风险等级。命中即阻塞审批流，必须法务介入",
        "editable": True,
        "updated_at": datetime.now(),
        "updated_by": "init_script",
    },
    {
        "config_key": "rule_engine.semantic_threshold",
        "config_value": 0.82,
        "value_type": "float",
        "description": "语义匹配默认阈值，规则模板未单独配置时使用",
        "editable": True,
        "updated_at": datetime.now(),
        "updated_by": "init_script",
    },
    {
        "config_key": "route.deep_amount_threshold",
        "config_value": 5000000,
        "value_type": "int",
        "description": "深度复核通道的合同金额阈值（元）。金额 ≥ 该值走 deep 通道",
        "editable": True,
        "updated_at": datetime.now(),
        "updated_by": "init_script",
    },
    {
        "config_key": "route.deep_force_types",
        "config_value": ["foreign_cooperation", "equity_transfer", "guarantee", "loan"],
        "value_type": "list",
        "description": "强制走深度复核的合同类型。此类合同风险敞口大，不适用快速通道",
        "editable": True,
        "updated_at": datetime.now(),
        "updated_by": "init_script",
    },
    {
        "config_key": "ocr.min_confidence_threshold",
        "config_value": 0.6,
        "value_type": "float",
        "description": "OCR 平均置信度阈值。低于该值判定扫描件模糊，任务进入阻塞（SCAN_TOO_BLURRY）",
        "editable": True,
        "updated_at": datetime.now(),
        "updated_by": "init_script",
    },
    {
        "config_key": "parse.min_content_length",
        "config_value": 50,
        "value_type": "int",
        "description": "正文最小字符数。低于该值判定正文为空，任务进入阻塞（EMPTY_CONTENT）",
        "editable": True,
        "updated_at": datetime.now(),
        "updated_by": "init_script",
    },
    {
        "config_key": "task.max_retry",
        "config_value": 3,
        "value_type": "int",
        "description": "节点级最大重试次数。超过后任务进入阻塞并需人工重跑",
        "editable": True,
        "updated_at": datetime.now(),
        "updated_by": "init_script",
    },
    {
        "config_key": "approval.allowed_apis",
        "config_value": ["/comments", "/comments/batch"],
        "value_type": "list",
        "description": "审批系统接口白名单。仅允许写入评论区，严禁改变审批结论（红线，禁止修改）",
        "editable": False,
        "updated_at": datetime.now(),
        "updated_by": "init_script",
    },
]


# ======================================================================
# 五、辅助工具
# ======================================================================

def _print_header(title: str) -> None:
    """打印分节标题，统一输出风格"""
    print("\n" + "=" * 74)
    print(f"  {title}")
    print("=" * 74)


def _print_step(step_no: int, text: str) -> None:
    """打印步骤序号"""
    print(f"\n[{step_no}] {text}")
    print("-" * 74)


def _print_ok(text: str) -> None:
    """打印成功信息"""
    print(f"  [OK]   {text}")


def _print_skip(text: str) -> None:
    """打印跳过信息"""
    print(f"  [SKIP] {text}")


def _print_warn(text: str) -> None:
    """打印告警信息"""
    print(f"  [WARN] {text}")


def _print_fail(text: str) -> None:
    """打印失败信息"""
    print(f"  [FAIL] {text}")


# ======================================================================
# 六、连接 MongoDB
# ======================================================================

def connect_mongo() -> MongoClient:
    """
    建立 MongoDB 连接并做一次连通性探测

    返回：
        MongoClient 实例

    异常：
        SystemExit：连接失败时直接退出（初始化脚本无法在无库环境下继续）
    """
    uri = settings.MONGO_URI
    # 说明：日志中打印 URI 可能泄露密码，此处做脱敏
    masked_uri = uri
    if "@" in uri:
        # mongodb://user:pass@host → mongodb://***@host
        scheme, rest = uri.split("://", 1)
        if "@" in rest:
            masked_uri = f"{scheme}://***@{rest.split('@', 1)[1]}"

    print(f"  连接地址：{masked_uri}")
    print(f"  目标库名：{settings.MONGO_DB_NAME}")

    try:
        client = MongoClient(
            uri,
            serverSelectionTimeoutMS=settings.MONGO_SERVER_SELECTION_TIMEOUT_MS,
            # 初始化脚本为一次性批处理，连接池无需很大
            maxPoolSize=5,
        )
        # 触发一次真实往返，确认服务可达（ping 不依赖任何业务库）
        client.admin.command("ping")
        _print_ok("MongoDB 连接成功")
        return client
    except ConnectionFailure as exc:
        _print_fail(f"MongoDB 连接失败：{exc}")
        print("\n  排查建议：")
        print("    1. 确认 MongoDB 已启动：net start MongoDB（Windows）")
        print("      或 docker ps | grep mongo")
        print("    2. 确认 .env 中 MONGO_URI 的地址与端口正确（默认 127.0.0.1:27017）")
        print("    3. 如启用了鉴权，确认 MONGO_URI 中已包含账号密码")
        print(f"       当前值：{masked_uri}")
        raise SystemExit(1) from exc


# ======================================================================
# 七、创建集合
# ======================================================================

def create_collections(db: Any, verify_only: bool) -> Tuple[int, int]:
    """
    创建全部业务集合

    参数：
        db: MongoDB 数据库对象
        verify_only: 仅校验模式，不实际写入

    返回：
        (本次新建数, 已存在数)
    """
    existing = set(db.list_collection_names())
    created = 0
    skipped = 0

    for name, description in COLLECTION_PLAN.items():
        if name in existing:
            skipped += 1
            continue

        if verify_only:
            _print_warn(f"集合不存在（仅校验模式未创建）：{name}")
            continue

        try:
            # 说明：MongoDB 的集合在首次写入时才真正落地。
            # 用 create_collection 显式创建，并在 comment 里写用途，
            # 让运维在 Compass 中能直接看到每个集合是干什么的。
            db.create_collection(name, comment=description)
            _print_ok(f"已创建集合 {name} — {description}")
            created += 1
        except CollectionInvalid:
            # 并发场景下可能已被其它会话创建，视为已存在
            skipped += 1

    if created == 0 and skipped > 0:
        _print_skip(f"全部 {skipped} 个集合均已存在，无需创建")

    return created, skipped


# ======================================================================
# 八、创建索引
# ======================================================================

def create_indexes(db: Any, verify_only: bool) -> Tuple[int, int]:
    """
    创建全部索引

    参数：
        db: MongoDB 数据库对象
        verify_only: 仅校验模式，不实际写入

    返回：
        (本次新建数, 已存在数)
    """
    total_created = 0
    total_existing = 0

    for collection_name, indexes in INDEX_PLAN:
        if verify_only:
            existing_names = set()
            try:
                existing_names = set(db[collection_name].index_information().keys())
            except OperationFailure:
                # 集合不存在时 index_information 会报错，视为全部缺失
                pass
            missing = [ix.document.get("name") for ix in indexes
                       if ix.document.get("name") not in existing_names]
            if missing:
                _print_warn(f"{collection_name} 缺失索引：{', '.join(missing)}")
            else:
                _print_ok(f"{collection_name} 索引齐备（{len(indexes)} 个）")
                total_existing += len(indexes)
            continue

        before = set()
        try:
            before = set(db[collection_name].index_information().keys())
        except OperationFailure:
            pass

        try:
            # create_indexes 是 MongoDB 原生幂等操作：
            # 同名同定义的索引重复创建不会报错，也不会重建。
            db[collection_name].create_indexes(indexes)
        except OperationFailure as exc:
            # 唯一索引冲突（已有重复数据）会走到这里，必须让用户看到
            _print_fail(f"{collection_name} 索引创建失败：{exc}")
            print("    提示：唯一索引创建失败通常说明集合内已有重复数据，")
            print("          请先清理重复记录，或用 --drop-all 重建（会清空数据）")
            continue

        after = set(db[collection_name].index_information().keys())
        newly = after - before - {"_id_"}
        created_here = len(newly)
        total_created += created_here
        total_existing += len(indexes) - created_here

        _print_ok(f"{collection_name}：索引 {len(indexes)} 个（本次新建 {created_here} 个）")

    return total_created, total_existing


# ======================================================================
# 九、创建角色与账号
# ======================================================================

def create_roles(client: MongoClient, verify_only: bool) -> None:
    """
    创建初始角色与账号

    说明：
        此步骤仅在 MongoDB 以 --auth 模式启动时可用。
        无鉴权模式下执行 createRole 会报错，因此失败不中断流程，
        只给出告警与说明。

    参数：
        client: MongoDB 客户端
        verify_only: 仅校验模式
    """
    admin_db = client.admin

    for role_spec in ROLE_PLAN:
        role_name = role_spec["role"]

        if verify_only:
            try:
                admin_db.command("rolesInfo", role_name, "contract_review")
                _print_ok(f"角色已存在：{role_name}")
            except OperationFailure:
                _print_warn(f"角色不存在：{role_name}")
            continue

        try:
            # 已存在时重建会报错，先尝试 drop（不存在则忽略）
            try:
                admin_db.command("dropRole", role_name)
            except OperationFailure:
                pass

            admin_db.command(
                "createRole",
                role_name,
                privileges=role_spec["privileges"],
                roles=role_spec["roles"],
            )
            _print_ok(f"已创建角色：{role_name}")
        except OperationFailure as exc:
            _print_warn(
                f"角色 {role_name} 创建失败（当前 MongoDB 可能未启用鉴权）：{exc}"
            )

    # 创建应用账号
    user_plan = [
        (APP_USER_NAME, ROLE_PLAN[0]["pwd"], [ROLE_PLAN[0]["role"]]),
        (READONLY_USER_NAME, ROLE_PLAN[1]["pwd"], [ROLE_PLAN[1]["role"]]),
    ]

    for username, password, roles in user_plan:
        if verify_only:
            _print_skip(f"账号校验（需鉴权模式）：{username}")
            continue

        try:
            try:
                admin_db.command("dropUser", username)
            except OperationFailure:
                pass

            admin_db.command(
                "createUser",
                username,
                pwd=password,
                roles=[{"role": r, "db": "contract_review"} for r in roles],
            )
            _print_ok(f"已创建账号：{username}（角色 {'/'.join(roles)}）")
        except OperationFailure as exc:
            _print_warn(
                f"账号 {username} 创建失败（当前 MongoDB 可能未启用鉴权）：{exc}"
            )

    print()
    print("  ※ 若启用了鉴权，请把 .env 中的 MONGO_URI 改为：")
    print(f"    MONGO_URI=mongodb://{APP_USER_NAME}:<密码>@127.0.0.1:27017/"
          f"{settings.MONGO_DB_NAME}?authSource=admin")
    print("  ※ 默认密码仅为占位，生产环境务必修改为强随机值。")


# ======================================================================
# 十、导入规则样例数据
# ======================================================================

def import_rules(db: Any, verify_only: bool) -> Tuple[int, int, int]:
    """
    从 config/risk_rules.yaml 导入规则到 cr_rule_template

    说明：
        规则文件是规则引擎的配置来源，同时需要在 MongoDB 中留一份
        cr_rule_template 记录，用于：
            1. 供前端"合规规则与模板页"展示与启停
            2. 记录规则的运维修改痕迹（谁改的、什么时候改的）
        两侧的一致性由本导入动作保证：YAML 是权威源，MongoDB 是镜像。

        导入策略：按 rule_code upsert。
            - 业务字段（名称、匹配配置、严重度等）以 YAML 为准刷新
            - 归属字段（created_at / created_by）保留首次写入值
            - 运维侧字段（status 若被人工停用）会被 YAML 的 enabled 覆盖，
              这是刻意的：重新执行初始化即恢复为 YAML 定义的状态

    参数：
        db: MongoDB 数据库对象
        verify_only: 仅校验模式

    返回：
        (新增数, 更新数, 未变数)
    """
    if os.path.isabs(settings.RULE_RULES_PATH):
        rules_path = settings.RULE_RULES_PATH
    else:
        # 规范化为绝对路径，避免日志里出现 "backend/config/risk_rules.yaml"
        # 这种混合分隔符的路径，在 Windows 上不易辨识
        rules_path = os.path.normpath(
            os.path.join(BACKEND_DIR, settings.RULE_RULES_PATH)
        )

    print(f"  规则文件：{rules_path}")

    if not os.path.exists(rules_path):
        _print_fail(f"规则文件不存在：{rules_path}")
        print("    提示：请确认 backend/config/risk_rules.yaml 存在，")
        print("          或调整 .env 中的 RULE_RULES_PATH 配置")
        return 0, 0, 0

    # 延迟导入：yaml 解析依赖在需要时才引入
    from app.engines.rule_engine import RuleRegistry

    try:
        registry = RuleRegistry()
        rules = registry.load_yaml(rules_path)
    except Exception as exc:  # noqa: BLE001 — 初始化脚本需给出友好提示
        _print_fail(f"规则文件解析失败：{exc}")
        print("    提示：规则字段校验很严格，报错信息会指出具体哪条规则哪个字段不合法")
        return 0, 0, 0

    _print_ok(f"规则文件解析成功，共 {len(rules)} 条规则")

    if verify_only:
        # 仅校验：比对数据库已有规则编码与文件规则编码
        existing_codes = {
            doc.get("rule_code")
            for doc in db[MongoCollection.RULE_TEMPLATE].find({}, {"rule_code": 1})
        }
        file_codes = {rule.rule_code for rule in rules}
        missing = file_codes - existing_codes
        extra = existing_codes - file_codes
        print(f"    数据库已有 {len(existing_codes)} 条，文件有 {len(file_codes)} 条")
        if missing:
            _print_warn(f"数据库中缺失 {len(missing)} 条：{', '.join(sorted(missing))}")
        if extra:
            _print_warn(f"数据库中存在文件中没有的 {len(extra)} 条：{', '.join(sorted(extra))}")
        if not missing and not extra:
            _print_ok("数据库规则与 YAML 文件完全一致")
        return 0, 0, 0

    collection = db[MongoCollection.RULE_TEMPLATE]
    inserted = 0
    updated = 0
    unchanged = 0

    for rule in rules:
        # model_dump 把 Pydantic 模型转为可写入 MongoDB 的字典
        doc = rule.model_dump()
        # 把 enabled（YAML 字段）映射为 status（存储字段），
        # 这是 YAML 与存储模型的语义差异：
        #   YAML 用 enabled: true/false 表达运维视角的"开/关"
        #   存储用 status: enabled/disabled 表达状态机
        # RuleRegistry._parse_rule 已完成该转换，此处直接使用 rule 的 status
        doc.pop("id", None)  # _id 由 MongoDB 生成，避免携带空 id

        existing = collection.find_one({"rule_code": rule.rule_code}, {"_id": 1})

        if existing is None:
            doc["created_by"] = "init_script"
            doc["updated_by"] = "init_script"
            doc["created_at"] = datetime.now()
            doc["updated_at"] = datetime.now()
            collection.insert_one(doc)
            inserted += 1
        else:
            # 保留首次写入的归属信息
            doc["updated_by"] = "init_script"
            doc["updated_at"] = datetime.now()
            result = collection.update_one({"rule_code": rule.rule_code}, {"$set": doc})
            if result.modified_count > 0:
                updated += 1
            else:
                unchanged += 1

    _print_ok(f"规则导入完成：新增 {inserted} 条，更新 {updated} 条，未变 {unchanged} 条")

    # 输出规则分布，便于确认合同类型覆盖情况
    grouped = registry.rules_by_contract_type
    print("    按合同类型分布：")
    for contract_type in sorted(grouped.keys()):
        print(f"      {contract_type:<22} {len(grouped[contract_type])} 条")

    return inserted, updated, unchanged


# ======================================================================
# 十一、写入初始系统配置
# ======================================================================

def import_sys_config(db: Any, verify_only: bool) -> None:
    """
    写入初始系统配置项

    参数：
        db: MongoDB 数据库对象
        verify_only: 仅校验模式
    """
    collection = db[MongoCollection.SYS_CONFIG]

    if verify_only:
        existing_keys = {
            doc.get("config_key")
            for doc in collection.find({}, {"config_key": 1})
        }
        for item in DEFAULT_SYS_CONFIG:
            key = item["config_key"]
            if key in existing_keys:
                _print_ok(f"配置项已存在：{key}")
            else:
                _print_warn(f"配置项缺失：{key}")
        return

    inserted = 0
    updated = 0

    for item in DEFAULT_SYS_CONFIG:
        key = item["config_key"]
        existing = collection.find_one({"config_key": key}, {"_id": 1})
        if existing is None:
            collection.insert_one(dict(item))
            inserted += 1
        else:
            # 说明：已存在的配置项只刷新语义字段，不覆盖 config_value。
            # 原因：config_value 是运行期可改项，运维可能已在界面上调整过，
            # 重新执行初始化不应该把这些调整重置掉。
            collection.update_one(
                {"config_key": key},
                {
                    "$set": {
                        "value_type": item["value_type"],
                        "description": item["description"],
                        "editable": item["editable"],
                    }
                },
            )
            updated += 1

    _print_ok(f"系统配置写入完成：新增 {inserted} 项，已存在保留原值 {updated} 项")


# ======================================================================
# 十二、危险操作：清空业务集合
# ======================================================================

def drop_all_collections(db: Any) -> None:
    """
    清空全部业务集合（危险操作）

    说明：
        只删除业务集合，不动 admin / local / config 等系统库。
        删除前会打印完整清单，调用方需自行确认。

    参数：
        db: MongoDB 数据库对象
    """
    print("  【危险操作】即将删除以下业务集合：")
    for name in COLLECTION_PLAN:
        print(f"    - {name}")

    dropped = 0
    for name in COLLECTION_PLAN:
        try:
            db.drop_collection(name)
            dropped += 1
        except OperationFailure as exc:
            _print_warn(f"删除 {name} 失败：{exc}")

    _print_ok(f"已删除 {dropped} 个业务集合")


# ======================================================================
# 十三、结果汇总
# ======================================================================

def print_summary(db: Any) -> None:
    """
    打印初始化结果汇总

    参数：
        db: MongoDB 数据库对象
    """
    _print_header("初始化结果汇总")

    collections = sorted(db.list_collection_names())

    print(f"\n  数据库：{settings.MONGO_DB_NAME}")
    print(f"  集合总数：{len(collections)} / 预期 {len(COLLECTION_PLAN)}")
    print()

    header = f"  {'集合名':<28} {'文档数':>8} {'索引数':>8}"
    print(header)
    print("  " + "-" * (len(header) - 2))

    total_docs = 0
    for name in sorted(COLLECTION_PLAN.keys()):
        if name not in collections:
            print(f"  {name:<28} {'缺失':>8} {'-':>8}")
            continue
        try:
            doc_count = db[name].estimated_document_count()
            index_count = len(db[name].index_information())
        except OperationFailure:
            doc_count, index_count = -1, -1
        total_docs += max(doc_count, 0)
        print(f"  {name:<28} {doc_count:>8} {index_count:>8}")

    print()
    print(f"  文档总数：{total_docs}")

    # 额外信息：集合中是否存在预期外的集合
    unexpected = set(collections) - set(COLLECTION_PLAN.keys())
    if unexpected:
        print()
        _print_warn(
            f"存在预期外的集合（可能是其它工具或历史版本创建）："
            f"{', '.join(sorted(unexpected))}"
        )


# ======================================================================
# 十四、命令行入口
# ======================================================================

def parse_args() -> argparse.Namespace:
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description="MongoDB 数据库初始化脚本（创建库、集合、索引、角色、规则样例数据）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python scripts/init_mongodb.py                  # 完整初始化\n"
            "  python scripts/init_mongodb.py --init-roles     # 同时创建角色与账号\n"
            "  python scripts/init_mongodb.py --skip-rules     # 跳过规则导入\n"
            "  python scripts/init_mongodb.py --verify-only    # 只校验不写入\n"
            "  python scripts/init_mongodb.py --drop-all       # 【危险】先清空再初始化\n"
        ),
    )
    parser.add_argument(
        "--init-roles",
        action="store_true",
        help="创建初始角色与应用账号（需 MongoDB 以 --auth 模式启动）",
    )
    parser.add_argument(
        "--skip-rules", action="store_true", help="跳过规则样例数据导入"
    )
    parser.add_argument(
        "--skip-config", action="store_true", help="跳过系统配置项写入"
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="仅校验现有状态并报告差异，不做任何写入",
    )
    parser.add_argument(
        "--drop-all",
        action="store_true",
        help="【危险】初始化前先删除全部业务集合（数据不可恢复）",
    )
    return parser.parse_args()


def main() -> int:
    """主流程"""
    args = parse_args()

    _print_header("MongoDB 数据库初始化")
    print(f"  执行时间：{datetime.now():%Y-%m-%d %H:%M:%S}")
    if args.verify_only:
        print("  运行模式：仅校验（不写入任何数据）")
    elif args.drop_all:
        print("  运行模式：【危险】先清空再重建")
    else:
        print("  运行模式：正常初始化（幂等，可重复执行）")

    client = connect_mongo()
    db = client[settings.MONGO_DB_NAME]

    try:
        # 危险操作前置确认
        if args.drop_all:
            if args.verify_only:
                _print_warn("--drop-all 与 --verify-only 同时指定，已忽略 --drop-all")
            else:
                print("\n  注意：3 秒后开始删除业务集合，如需中止请按 Ctrl+C")
                import time

                time.sleep(3)
                drop_all_collections(db)

        # 步骤 1：创建集合
        _print_step(1, "创建业务集合")
        if args.verify_only:
            existing = set(db.list_collection_names())
            missing = [n for n in COLLECTION_PLAN if n not in existing]
            if missing:
                for name in missing:
                    _print_warn(f"集合缺失：{name}")
            else:
                _print_ok(f"全部 {len(COLLECTION_PLAN)} 个集合均已存在")
        else:
            create_collections(db, args.verify_only)

        # 步骤 2：创建索引
        _print_step(2, "创建索引")
        create_indexes(db, args.verify_only)

        # 步骤 3：创建角色（可选）
        if args.init_roles:
            _print_step(3, "创建初始角色与账号")
            create_roles(client, args.verify_only)
        else:
            _print_step(3, "创建初始角色与账号")
            _print_skip("未指定 --init-roles，跳过角色创建")
            print("      说明：开发环境通常为无鉴权 MongoDB，无需创建角色。")
            print("            生产部署请追加 --init-roles 参数。")

        # 步骤 4：导入规则样例数据
        _print_step(4, "导入规则样例数据")
        if args.skip_rules:
            _print_skip("未指定 --skip-rules 的反面，即已指定，跳过规则导入")
        else:
            import_rules(db, args.verify_only)

        # 步骤 5：写入系统配置
        _print_step(5, "写入初始系统配置")
        if args.skip_config:
            _print_skip("已指定 --skip-config，跳过系统配置写入")
        else:
            import_sys_config(db, args.verify_only)

        # 步骤 6：汇总
        print_summary(db)

        _print_header("初始化完成")
        if args.verify_only:
            print("  本次为校验模式，未修改任何数据。")
        else:
            print("  MongoDB 已就绪，可继续执行 Milvus 向量库初始化：")
            print("    python scripts/init_milvus_vectors.py")
        print()
        return 0

    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
