# D:/ContractReviewSys-合同审批智能审查项目/backend/app/services/task_service.py
"""
任务管理服务 — 任务创建、查询、重试与阻塞处理的编排层

【模块定位】
    本模块是"任务管理接口"背后的业务编排服务，负责：
        1. 创建任务（编号/会话/目录生成 + 路由决策 + 持久化）
        2. 任务列表与详情查询
        3. 任务重试（重置阻塞标记，回到可执行状态）
        4. 处理阻塞任务（重跑 / 驳回）
        5. 拉取审批待办

【分层约束】
    本服务只做业务编排，数据读写通过 repositories 层，不直接操作 HTTP。
    路由决策复用 app.graph.routes.decide_route_type（与图内决策一致）。

返回值说明：
    - create_task()：返回 (task_id, task_no)
    - list_tasks()：返回 (items, total)
"""

from typing import Any, Dict, List, Optional, Tuple

from app.constants import ReviewTaskStatus
from app.core.logging_config import get_logger
from app.models.documents.review_task import ReviewTask, RouteType
from app.utils.id_generator import (
    build_work_dir,
    generate_task_no,
    generate_thread_id,
)

logger = get_logger(__name__)


# ======================================================================
# 一、任务创建
# ======================================================================
async def create_task(request: Any, owner: Optional[str] = None) -> Tuple[str, str]:
    """
    创建审查任务

    流程：
        1. 生成任务编号 / 会话 ID / 工作目录
        2. 执行差异化路由决策
        3. 构造 ReviewTask 并持久化

    参数：
        request: TaskCreateRequest（含合同字段）
        owner: 任务提交人标识（登录用户名）。必须传登录用户名而非申请人姓名，
               因为报告/评论等接口的所有者校验统一比对 created_by 与
               user.username / user.user_id。缺省时回退到 request.applicant，
               以兼容脚本直连等无登录态的调用场景。

    返回：
        (task_id, task_no) 元组

    异常：
        MongoDBException：持久化失败
    """
    from app.repositories import task_repo

    task_no = generate_task_no()
    thread_id = generate_thread_id()
    work_dir = build_work_dir(thread_id)

    # 路由决策：复用图内决策逻辑（保证与图内路由一致）
    route_type = _decide_route_type(
        contract_amount=request.contract_amount,
        contract_type=request.contract_type,
        template_flag=request.template_flag if hasattr(request, "template_flag") else False,
        currency=request.currency,
    )

    # 提交人归属：优先取登录用户名，保证后续越权校验能正确匹配
    owner_ref = (owner or "").strip() or (getattr(request, "applicant", "") or "")

    task = ReviewTask(
        task_no=task_no,
        contract_id=request.contract_id,
        status=ReviewTaskStatus.PENDING.value,
        route_type=route_type,
        thread_id=thread_id,
        work_dir=work_dir,
        created_by=owner_ref,
        # 合同原件引用：审查流程后续需要据此读取原文
        object_key=(getattr(request, "file_object_key", "") or "") or None,
        source_type=(getattr(request, "source_type", "") or "digital"),
        file_name=(getattr(request, "file_name", "") or ""),
    )

    task_id = await task_repo.create_task(task)

    logger.info(
        "审查任务创建完成 | task_no=%s | task_id=%s | 路由=%s",
        task_no, task_id, route_type,
    )
    return (task_id, task_no)


def _decide_route_type(
    contract_amount: float,
    contract_type: str,
    template_flag: bool,
    currency: str = "CNY",
) -> str:
    """
    执行差异化路由决策（与 app.graph.routes.decide_route_type 对齐）

    说明：
        复制路由决策逻辑，避免在服务层构造完整的 ReviewTaskState。
        决策优先级：deep（大额/高风险类型）> fast（模板合同）> standard。

    参数：
        contract_amount: 合同金额
        contract_type: 合同类型
        template_flag: 是否模板合同
        currency: 币种

    返回：
        路由类型字符串
    """
    from app.core.config import settings

    # 深度复核：大额或高风险类型
    if contract_amount >= settings.ROUTE_DEEP_AMOUNT_THRESHOLD:
        return RouteType.DEEP.value
    if contract_type in settings.deep_force_type_list:
        return RouteType.DEEP.value

    # 快速通道：模板合同
    if template_flag and settings.ROUTE_FAST_ENABLED:
        return RouteType.FAST.value

    return RouteType.STANDARD.value


# ======================================================================
# 二、任务查询
# ======================================================================
async def list_tasks(
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
    keyword: Optional[str] = None,
    failure_type: Optional[str] = None,
    owner_username: Optional[str] = None,
) -> Tuple[List[ReviewTask], int]:
    """
    分页查询任务列表

    参数：
        status: 可选，按状态过滤
        page: 页码
        page_size: 每页条数
        keyword: 可选关键字（匹配 task_no / contract_title）
        failure_type: 可选，按失败类型过滤（异常任务筛选维度，2026-09-20 新增）
        owner_username: 可选，按提交人过滤。业务经办人（business_user）只能
            查看自己创建的任务（PERM_TASK_VIEW_OWN），调用方传入其用户名；
            法务/管理员传 None 查看全部。

    返回：
        (任务列表, 总条数)
    """
    from app.repositories import task_repo

    filter_dict: Dict[str, Any] = {}
    if status:
        filter_dict["status"] = status
    if failure_type:
        filter_dict["failure_type"] = failure_type
    if owner_username:
        filter_dict["created_by"] = owner_username
    if keyword:
        filter_dict["$or"] = [
            {"task_no": {"$regex": keyword}},
            {"contract_title": {"$regex": keyword}},
            {"contract_id": {"$regex": keyword}},
        ]

    skip = (page - 1) * page_size
    return await task_repo.list_tasks(
        filter_dict=filter_dict or None,
        skip=skip,
        limit=page_size,
    )


async def get_task_detail(task_no: str) -> Optional[ReviewTask]:
    """
    查询任务详情

    参数：
        task_no: 任务编号

    返回：
        ReviewTask 实例；不存在返回 None
    """
    from app.repositories import task_repo

    return await task_repo.find_by_task_no(task_no)


async def get_task_by_id(task_id: str) -> Optional[ReviewTask]:
    """
    按任务 ID 查询任务

    参数：
        task_id: 任务 ID

    返回：
        ReviewTask 实例；不存在返回 None
    """
    from app.repositories import task_repo

    return await task_repo.find_by_task_id(task_id)


# ======================================================================
# 三、任务重试与阻塞处理
# ======================================================================
async def retry_task(task_no: str) -> Tuple[bool, str]:
    """
    重试任务（失败或阻塞任务回到可执行状态）

    说明：
        重置阻塞标记与重试计数，将任务状态置回 pending，
        由触发审查接口重新执行图流程。

    参数：
        task_no: 任务编号

    返回：
        (是否成功, 说明)
    """
    from app.repositories import task_repo

    task = await task_repo.find_by_task_no(task_no)
    if task is None:
        return (False, f"任务不存在：{task_no}")

    updates = {
        "status": ReviewTaskStatus.PENDING.value,
        "blocked_reason": None,
        "blocked_detail": None,
        "retry_count": 0,
        "current_node": None,
    }
    ok = await task_repo.update_task_by_no(task_no, updates)
    if ok:
        logger.info("任务已重置待重试 | task_no=%s", task_no)
        return (True, f"任务 {task_no} 已重置，可重新触发审查")
    return (False, f"任务 {task_no} 重置失败")


async def resolve_blocked_task(task_no: str, action: str, reason: str = "") -> Tuple[bool, str]:
    """
    处理阻塞任务

    说明：
        - retry：重置阻塞标记，任务回到可执行状态（等价于 retry_task）
        - dismiss：驳回，任务状态置为 failed（人工确认不再处理）

    参数：
        task_no: 任务编号
        action: 处理动作（retry / dismiss）
        reason: 处理说明

    返回：
        (是否成功, 说明)
    """
    from app.repositories import task_repo

    task = await task_repo.find_by_task_no(task_no)
    if task is None:
        return (False, f"任务不存在：{task_no}")

    if action == "retry":
        return await retry_task(task_no)

    if action == "dismiss":
        updates = {
            "status": ReviewTaskStatus.FAILED.value,
            "blocked_detail": f"人工驳回处理：{reason or '未说明原因'}",
        }
        ok = await task_repo.update_task_by_no(task_no, updates)
        if ok:
            logger.info("阻塞任务已驳回 | task_no=%s | 原因=%s", task_no, reason)
            return (True, f"任务 {task_no} 已驳回")
        return (False, f"任务 {task_no} 驳回失败")

    return (False, f"不支持的处理动作：{action}（仅支持 retry / dismiss）")


# ======================================================================
# 四、审批待办
# ======================================================================
async def list_approval_pending(page: int = 1, page_size: int = 20) -> Tuple[List[ReviewTask], int]:
    """
    拉取审批待办（规则引擎已判定完成、可进入人工审批环节的任务）

    参数：
        page: 页码
        page_size: 每页条数

    返回：
        (任务列表, 总条数)
    """
    from app.repositories import task_repo

    # 已完成审查（completed 状态）且未回写或回写待处理的任务
    filter_dict = {"status": ReviewTaskStatus.COMPLETED.value}
    skip = (page - 1) * page_size
    return await task_repo.list_tasks(
        filter_dict=filter_dict,
        skip=skip,
        limit=page_size,
    )


# ======================================================================
# 任务处理操作留痕（2026-09-20 数据与流程闭环）
# ======================================================================
async def recover_interrupted_tasks() -> int:
    """
    启动时清理僵尸任务：把上次服务运行中断的任务统一置为失败

    【为什么需要（2026-09-20 修复）】
        后端重启/被杀时，正处于 running / parsing 的任务会永远停在
        "审查中 0%"——没有任何进程会再更新它们的状态（僵尸任务）。
        僵尸任务既不是 failed 也不是 blocked，导致使用者既不能重试
        也看不到「重新上传」入口，只能干等。

        本函数在应用启动（存储就绪后）调用一次，把这些中断任务
        统一标记为 failed 并写明原因，使其重新进入可重试 /
        可重新上传的处置链路。

    返回：
        被标记为失败的任务条数
    """
    from datetime import datetime

    from app.constants import MongoCollection, FailureType, ReviewTaskStatus
    from app.storage.mongo_client import mongo_client

    collection = mongo_client.get_collection(MongoCollection.REVIEW_TASK)
    result = await collection.update_many(
        {"status": ReviewTaskStatus.RUNNING.value},
        {
            "$set": {
                "status": ReviewTaskStatus.FAILED.value,
                "parse_status": "failed",
                "failure_type": FailureType.OTHER.value,
                "failure_reason": (
                    "服务重启导致任务中断，请点击重试或重新上传"
                ),
                "updated_at": datetime.now(),
            }
        },
    )
    if result.modified_count > 0:
        logger.warning(
            "启动清理：发现 %d 条服务重启中断的僵尸任务，已统一置为失败",
            result.modified_count,
        )
    return result.modified_count


async def record_task_operation(
    task_no: str,
    operator: str,
    action: str,
    result: str,
    detail: str = "",
    failure_type: str = "",
) -> None:
    """
    记录一次任务处理操作（重试 / 重新上传 / 自动重试 / 失败通知）

    说明：
        每次重试、重新上传均留存操作记录（操作人 / 操作时间 / 处理结果），
        形成"异常 -> 处置 -> 结果"的闭环证据链。写入失败仅记录日志，
        绝不阻断主流程（留痕是旁路能力）。

    参数：
        task_no: 任务编号
        operator: 操作人（用户名；系统自动动作用 system）
        action: 操作类型 retry / replace_file / auto_retry / notify
        result: 处理结果 success / failed / accepted
        detail: 补充说明（如失败原因、替换后的文件名）
        failure_type: 关联的失败类型（重试场景下为被处置的失败）
    """
    from app.constants import MongoCollection
    from app.storage.mongo_client import mongo_client

    try:
        collection = mongo_client.get_collection(MongoCollection.TASK_OPERATION_LOG)
        await collection.insert_one(
            {
                "task_no": task_no,
                "operator": operator,
                "action": action,
                "result": result,
                "detail": detail,
                "failure_type": failure_type,
                "created_at": datetime.now(),
            }
        )
        logger.info(
            "任务操作留痕 | 任务=%s | 操作人=%s | 动作=%s | 结果=%s",
            task_no, operator, action, result,
        )
    except Exception as exc:  # noqa: BLE001 - 留痕失败不阻断主流程
        logger.error("任务操作留痕写入失败 | 任务=%s | 异常=%s", task_no, exc)
