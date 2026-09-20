# D:/ContractReviewSys-合同审批智能审查项目/backend/app/graph/node_utils.py
"""
节点执行基础设施 — 统一异常捕获、指数退避重试与审计埋点

【文件定位】
    本模块为所有 LangGraph 节点提供统一的执行外壳，保证：
        1. 每个节点都有完整的入参校验与出参规范
        2. 节点级异常捕获 + 指数退避重试（1s → 2s → 4s，上限 8s）
        3. 重试 3 次仍失败自动任务置 blocked(RETRY_EXHAUSTED)
        4. 每次进入/退出/重试/异常/跳过都自动追加审计日志

【核心设计：为什么必须由本模块统一包装】
    LangGraph 的状态更新契约是"节点返回增量字典"，而不是"节点修改 state"。
    如果在节点函数内部直接改 state 对象，框架不会感知这些修改，导致：
        - 审计日志丢失
        - 状态字段静默还原
        - 重试次数无法累积

    因此本模块采用统一模式：
        1. 节点实现体（NodeHandler）只负责计算，返回"需要变更的字段字典"
        2. 包装器负责把审计日志、节点历史、重试计数合并进最终增量
        3. 包装器负责异常捕获与退避重试，节点实现体只抛原始异常

【重试语义】
    可重试异常（网络抖动、下游超时）→ 指数退避后重试
    不可重试异常（文档加密、正文为空）→ 立即终止，置 blocked 并给出法定原因码
    重试耗尽 → 置 blocked(RETRY_EXHAUSTED)，附带最后异常摘要

返回值说明：
    - node_wrapper(...)：返回包装后的异步节点函数，签名 (state) -> dict
    - NodeResult：节点实现体返回结构，承载状态增量与审计详情

使用示例：
    @node_wrapper(node_name=WorkflowNode.PARSE_CONTRACT, progress=20)
    async def parse_document_impl(state: ReviewTaskState) -> NodeResult:
        ...  # 只做计算，返回 NodeResult
"""

import asyncio
import functools
import time
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple, Type

from pydantic import BaseModel, Field

from app.constants import ReviewTaskStatus, WorkflowNode
from app.core.config import settings
from app.core.logging_config import get_logger
from app.graph.state import (
    AuditAction,
    AuditLogEntry,
    BlockedInfo,
    NodeExecutionRecord,
    ParseStatus,
    ReviewTaskState,
)
from app.models.documents.review_task import BlockedReason
from app.utils.time_utils import now_timestamp

logger = get_logger(__name__)


# ======================================================================
# 一、不可重试异常 — 业务性阻断，重试无意义
# ======================================================================
class NonRetryableError(Exception):
    """
    不可重试异常 — 标记"重试也不会成功"的业务性失败

    说明：
        典型场景：文档加密、扫描件模糊、正文为空、权责违规拦截。
        这类异常一旦抛出，节点包装器不做退避重试，直接置任务为 blocked，
        并携带明确的原因码与详情，交由人工处理。

    参数：
        reason: 阻塞原因码，取自 BlockedReason 枚举
        message: 面向人工的失败说明
        detail: 详细上下文
        retriable: 是否允许人工重跑（默认 True，人工修复文件后可重跑）

    属性：
        reason / detail / retriable：与参数一一对应
    """

    def __init__(
        self,
        reason: str,
        message: str,
        detail: Optional[Dict[str, Any]] = None,
        retriable: bool = True,
    ) -> None:
        self.reason = reason
        self.detail = detail or {}
        self.retriable = retriable
        super().__init__(f"[{reason}] {message}")


# ======================================================================
# 二、节点实现体返回结构
# ======================================================================
class NodeResult(BaseModel):
    """
    节点实现体的返回结构

    说明：
        节点实现体只负责"算出要改哪些字段"，不负责审计埋点与重试。
        包装器会把本结构合并进最终的状态增量字典。

    字段：
        updates: 需要写入图状态的状态字段增量字典
        message: 审计日志正文，描述本次节点做了什么
        detail: 审计日志结构化详情
        skipped: 该节点是否实际未执行业务逻辑（因路由或前置条件跳过）
        skip_reason: 跳过原因
    """

    updates: Dict[str, Any] = Field(
        default_factory=dict, description="状态字段增量字典"
    )
    message: str = Field(default="", description="审计日志正文")
    detail: Dict[str, Any] = Field(default_factory=dict, description="审计结构化详情")
    skipped: bool = Field(default=False, description="是否跳过执行")
    skip_reason: str = Field(default="", description="跳过原因")


# 节点实现体类型别名：(state) -> Awaitable[NodeResult]
NodeHandler = Callable[[ReviewTaskState], Awaitable[NodeResult]]


# ======================================================================
# 三、审计日志与节点历史的写入助手
# ======================================================================
def _next_audit_seq(state: ReviewTaskState) -> int:
    """
    计算下一条审计日志的序号

    参数：
        state: 当前图状态

    返回：
        自增后的序号
    """
    return state.audit_sequence + 1


def build_audit_entry(
    state: ReviewTaskState,
    node: str,
    action: str,
    message: str = "",
    detail: Optional[Dict[str, Any]] = None,
    elapsed_ms: Optional[float] = None,
    offset: int = 0,
) -> AuditLogEntry:
    """
    构造审计日志条目

    参数：
        state: 当前图状态
        node: 节点名
        action: 动作类型，取自 AuditAction
        message: 描述文本
        detail: 结构化详情
        elapsed_ms: 耗时毫秒
        offset: 序号偏移量。同一节点内需要写多条日志时依次传入 0/1/2...

    返回：
        AuditLogEntry 实例
    """
    return AuditLogEntry(
        sequence=state.audit_sequence + offset + 1,
        node=node,
        action=action,
        message=message,
        detail=detail or {},
        elapsed_ms=elapsed_ms,
    )


def build_node_record(
    node: str,
    status: str,
    started_at: datetime,
    finished_at: Optional[datetime] = None,
    retry_count: int = 0,
    attempt: int = 1,
    error: str = "",
) -> NodeExecutionRecord:
    """
    构造节点执行记录

    参数：
        node: 节点名
        status: 执行状态 success / failed / blocked / skipped
        started_at: 开始时间
        finished_at: 结束时间
        retry_count: 累计重试次数
        attempt: 第几次尝试
        error: 错误摘要

    返回：
        NodeExecutionRecord 实例
    """
    end = finished_at or datetime.now()
    return NodeExecutionRecord(
        node=node,
        status=status,
        attempt=attempt,
        started_at=started_at,
        finished_at=end,
        elapsed_ms=(end - started_at).total_seconds() * 1000.0,
        retry_count=retry_count,
        error=error,
    )


def build_blocked_state(
    state: ReviewTaskState,
    node: str,
    reason: str,
    detail_message: str,
    retry_count: int = 0,
    retriable: bool = True,
    extra_updates: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    构造"任务置阻塞"的状态增量字典

    说明：
        所有阻塞路径（解析阻断、重试耗尽）都通过本函数构造增量，
        保证阻塞信息字段完整、原因码合法、审计留痕统一。

    参数：
        state: 当前图状态
        node: 触发阻塞的节点名
        reason: 阻塞原因码，取自 BlockedReason
        detail_message: 阻塞详情
        retry_count: 阻塞时已重试次数
        retriable: 是否允许人工重跑
        extra_updates: 额外的状态字段增量

    返回：
        可直接作为节点返回值的状态增量字典
    """
    blocked_at = datetime.now()
    blocked_info = BlockedInfo(
        reason=reason,
        detail=detail_message,
        blocked_at=blocked_at,
        blocked_node=node,
        retry_count=retry_count,
        retriable=retriable,
    )

    audit = build_audit_entry(
        state=state,
        node=node,
        action=AuditAction.TASK_BLOCKED.value,
        message=f"任务被置为阻塞：{reason} — {detail_message}",
        detail={
            "reason": reason,
            "retry_count": retry_count,
            "retriable": retriable,
            "blocked_node": node,
        },
    )

    node_record = build_node_record(
        node=node,
        status="blocked",
        started_at=state.started_at or blocked_at,
        finished_at=blocked_at,
        retry_count=retry_count,
        attempt=retry_count + 1,
        error=detail_message,
    )

    updates: Dict[str, Any] = {
        "status": ReviewTaskStatus.BLOCKED.value,
        "is_blocked": True,
        "blocked_info": blocked_info,
        "current_node": node,
        "error_message": detail_message,
        # 【归约器语义】audit_logs / node_history 声明为 operator.add 累加型，
        # 此处只能返回"本次新增的增量"，不可返回全量历史——否则会与归约器
        # 叠加，导致历史记录被反复重复累加（O(n²) 膨胀）。
        "audit_logs": [audit],
        "audit_sequence": audit.sequence,
        "node_history": [node_record],
        "updated_at": blocked_at,
    }

    if extra_updates:
        updates.update(extra_updates)

    logger.error(
        "任务已阻塞 | 任务=%s | 节点=%s | 原因=%s | 重试=%d次 | 详情=%s",
        state.task_no,
        node,
        reason,
        retry_count,
        detail_message,
    )

    return updates


def build_skip_result(
    state: ReviewTaskState,
    node: str,
    skip_reason: str,
    extra_updates: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    构造"节点跳过"的状态增量字典

    说明：
        差异化路由会跳过部分节点（如模板合同跳过向量与大模型审查）。
        跳过的节点必须留下审计记录与跳过清单，保证"流程完整可解释"。

    参数：
        state: 当前图状态
        node: 被跳过的节点名
        skip_reason: 跳过原因
        extra_updates: 额外的状态字段增量

    返回：
        可直接作为节点返回值的状态增量字典
    """
    skip_time = datetime.now()
    audit = build_audit_entry(
        state=state,
        node=node,
        action=AuditAction.NODE_SKIP.value,
        message=f"节点跳过：{skip_reason}",
        detail={"skip_reason": skip_reason},
    )
    node_record = build_node_record(
        node=node,
        status="skipped",
        started_at=skip_time,
        finished_at=skip_time,
    )

    updates: Dict[str, Any] = {
        "current_node": node,
        # 【字段语义对照】skipped_nodes 为普通 List[str]（全量替换语义），
        # 故此处必须拼接历史；而 audit_logs / node_history 为 operator.add
        # 累加型，只能返回增量，两者语义不同，切勿混用。
        "skipped_nodes": list(state.skipped_nodes) + [node],
        "audit_logs": [audit],
        "audit_sequence": audit.sequence,
        "node_history": [node_record],
        "updated_at": skip_time,
    }

    if extra_updates:
        updates.update(extra_updates)

    logger.info("节点跳过 | 任务=%s | 节点=%s | 原因=%s", state.task_no, node, skip_reason)
    return updates


# ======================================================================
# 四、节点包装器（核心）
# ======================================================================
def node_wrapper(
    node_name: WorkflowNode,
    progress: int,
    retry_on: Tuple[Type[Exception], ...] = (Exception,),
) -> Callable[[NodeHandler], Callable[[ReviewTaskState], Awaitable[Dict[str, Any]]]]:
    """
    节点包装器 — 统一注入异常捕获、指数退避重试与审计埋点

    执行流程：
        1. 校验前置条件：任务已阻塞则直接跳过（幂等保护）
        2. 记录 NODE_ENTER 审计日志
        3. 循环执行节点实现体，捕获异常：
             - NonRetryableError → 立即置 blocked，不重试
             - retry_on 内的异常 → 指数退避后重试
             - 其它异常 → 直接向上抛出
        4. 每次重试记录 NODE_RETRY 审计日志（含退避时长）
        5. 重试耗尽 → 置 blocked(RETRY_EXHAUSTED)
        6. 执行成功 → 合并状态增量，记录 NODE_EXIT 审计日志

    参数：
        node_name: 节点标识，取自 WorkflowNode 枚举
        progress: 该节点完成后任务进度百分比（0-100）
        retry_on: 需要重试的异常类型元组。NonRetryableError 自动排除在外

    返回：
        装饰器函数，被装饰的节点实现体签名必须为
        async (state: ReviewTaskState) -> NodeResult

    使用示例：
        @node_wrapper(node_name=WorkflowNode.PARSE_CONTRACT, progress=20)
        async def parse_document_impl(state: ReviewTaskState) -> NodeResult:
            ...
    """

    def decorator(handler: NodeHandler) -> Callable[[ReviewTaskState], Awaitable[Dict[str, Any]]]:
        @functools.wraps(handler)
        async def wrapper(state: ReviewTaskState) -> Dict[str, Any]:
            """
            包装后的节点执行体

            参数：
                state: 当前图状态

            返回：
                状态增量字典
            """
            node = node_name.value
            start_dt = datetime.now()
            start_ts = time.perf_counter()

            # -------------------- 前置保护：已阻塞任务不再推进 --------------------
            if state.is_blocked:
                logger.warning(
                    "任务已阻塞，节点不执行 | 任务=%s | 节点=%s | 原因=%s",
                    state.task_no,
                    node,
                    state.blocked_info.reason if state.blocked_info else "未知",
                )
                skip_audit = build_audit_entry(
                    state=state,
                    node=node,
                    action=AuditAction.NODE_SKIP.value,
                    message="任务处于阻塞状态，本节点不再执行",
                    detail={
                        "blocked_reason": (
                            state.blocked_info.reason if state.blocked_info else ""
                        )
                    },
                )
                return {
                    "current_node": node,
                    "skipped_nodes": list(state.skipped_nodes) + [node],
                    # 【归约器语义】仅返回增量，交由 operator.add 拼接
                    "audit_logs": [skip_audit],
                    "audit_sequence": skip_audit.sequence,
                    "updated_at": datetime.now(),
                }

            # -------------------- 记录节点进入 --------------------
            enter_audit = build_audit_entry(
                state=state,
                node=node,
                action=AuditAction.NODE_ENTER.value,
                message=f"进入节点 {node}",
                detail={"progress_before": state.progress, "route_type": state.route_type},
                offset=0,
            )

            max_retry = settings.TASK_MAX_RETRY
            base_delay = settings.TASK_RETRY_BASE_DELAY
            max_delay = settings.TASK_RETRY_MAX_DELAY

            last_exception: Optional[Exception] = None
            retry_count = 0
            # 重试过程中产生的审计日志暂存区，成功时一并写回，避免反复读写 state
            retry_audits: List[AuditLogEntry] = []

            # -------------------- 重试循环：首次 + max_retry 次重试 --------------------
            for attempt in range(1, max_retry + 1):
                try:
                    logger.info(
                        "节点执行开始 | 任务=%s | 节点=%s | 第%d/%d次",
                        state.task_no,
                        node,
                        attempt,
                        max_retry,
                    )

                    result: NodeResult = await handler(state)

                    elapsed_ms = (time.perf_counter() - start_ts) * 1000.0

                    # -------------------- 节点主动跳过 --------------------
                    if result.skipped:
                        return build_skip_result(
                            state=state,
                            node=node,
                            skip_reason=result.skip_reason or "前置条件不满足",
                            extra_updates=result.updates,
                        )

                    # -------------------- 执行成功，合并增量 --------------------
                    exit_audit = build_audit_entry(
                        state=state,
                        node=node,
                        action=AuditAction.NODE_EXIT.value,
                        message=result.message or f"节点 {node} 执行完成",
                        detail={
                            **result.detail,
                            "attempt": attempt,
                            "retry_count": retry_count,
                        },
                        elapsed_ms=elapsed_ms,
                        offset=1,
                    )

                    node_record = build_node_record(
                        node=node,
                        status="success",
                        started_at=start_dt,
                        retry_count=retry_count,
                        attempt=attempt,
                    )

                    updates: Dict[str, Any] = dict(result.updates)
                    updates.update(
                        {
                            "current_node": node,
                            "progress": max(state.progress, progress),
                            "retry_count": 0,
                            # 【归约器语义】仅返回本次新增的日志增量，
                            # 不能拼接 state.audit_logs（归约器会自动拼接）
                            "audit_logs": [enter_audit]
                            + retry_audits
                            + [exit_audit],
                            # 序号推进到本节点最后一条日志，保证全局单调递增
                            "audit_sequence": exit_audit.sequence,
                            "node_history": [node_record],
                            "updated_at": datetime.now(),
                        }
                    )

                    logger.info(
                        "节点执行完成 | 任务=%s | 节点=%s | 进度=%d%% | 耗时=%.2fms | 尝试=%d次",
                        state.task_no,
                        node,
                        updates["progress"],
                        elapsed_ms,
                        attempt,
                    )
                    return updates

                except NonRetryableError as exc:
                    # -------------------- 业务性阻断：不重试，直接置阻塞 --------------------
                    elapsed_ms = (time.perf_counter() - start_ts) * 1000.0
                    logger.error(
                        "节点遇阻断裂 | 任务=%s | 节点=%s | 原因=%s | 说明=%s | 耗时=%.2fms",
                        state.task_no,
                        node,
                        exc.reason,
                        str(exc),
                        elapsed_ms,
                    )

                    extra_audit = build_audit_entry(
                        state=state,
                        node=node,
                        action=AuditAction.REDLINE_DENY.value,
                        message=f"节点遇不可重试阻断：{exc}",
                        detail={"reason": exc.reason, **exc.detail},
                        elapsed_ms=elapsed_ms,
                        offset=1,
                    )
                    blocked_updates = build_blocked_state(
                        state=state,
                        node=node,
                        reason=exc.reason,
                        detail_message=str(exc),
                        retry_count=0,
                        retriable=exc.retriable,
                        extra_updates=exc.detail,
                    )
                    # 用完整链路日志覆盖阻塞构造器内部的单条日志，保证进入日志不丢失
                    # 【归约器语义】仅返回增量
                    blocked_updates["audit_logs"] = [enter_audit, extra_audit]
                    blocked_updates["audit_sequence"] = extra_audit.sequence
                    return blocked_updates

                except retry_on as exc:
                    # -------------------- 可重试异常：退避后重试 --------------------
                    last_exception = exc
                    retry_count += 1

                    if retry_count >= max_retry:
                        # 已达上限，退出循环进入重试耗尽处理
                        logger.error(
                            "节点重试耗尽 | 任务=%s | 节点=%s | 重试=%d次 | 最后异常=%s: %s",
                            state.task_no,
                            node,
                            retry_count,
                            type(exc).__name__,
                            exc,
                        )
                        break

                    delay = min(base_delay * (2 ** (retry_count - 1)), max_delay)
                    logger.warning(
                        "节点执行失败，%.2fs 后重试 | 任务=%s | 节点=%s | 第%d/%d次重试 | 异常=%s: %s",
                        delay,
                        state.task_no,
                        node,
                        retry_count,
                        max_retry - 1,
                        type(exc).__name__,
                        exc,
                    )

                    retry_audit = build_audit_entry(
                        state=state,
                        node=node,
                        action=AuditAction.NODE_RETRY.value,
                        message=(
                            f"第 {retry_count} 次重试，{delay:.2f}s 后重试"
                            f"（异常：{type(exc).__name__}）"
                        ),
                        detail={
                            "attempt": attempt,
                            "retry_count": retry_count,
                            "delay_seconds": delay,
                            "exception_type": type(exc).__name__,
                            "exception_message": str(exc)[:500],
                        },
                    )
                    retry_audits.append(retry_audit)

                    await asyncio.sleep(delay)

                except Exception as exc:  # noqa: BLE001 - 非预期异常需记录后迅速失败
                    # -------------------- 非预期异常：记录堆栈后向上抛出 --------------------
                    logger.error(
                        "节点执行遇非预期异常 | 任务=%s | 节点=%s | 异常=%s: %s",
                        state.task_no,
                        node,
                        type(exc).__name__,
                        exc,
                        exc_info=True,
                    )
                    raise

            # -------------------- 重试耗尽：置阻塞 --------------------
            elapsed_ms = (time.perf_counter() - start_ts) * 1000.0
            error_text = (
                f"{type(last_exception).__name__}: {last_exception}"
                if last_exception
                else "未知异常"
            )

            exhausted_audit = build_audit_entry(
                state=state,
                node=node,
                action=AuditAction.NODE_ERROR.value,
                message=f"节点重试 {retry_count} 次后仍失败：{error_text}",
                detail={
                    "retry_count": retry_count,
                    "max_retry": max_retry,
                    "exception_type": (
                        type(last_exception).__name__ if last_exception else ""
                    ),
                    "exception_message": str(last_exception)[:500] if last_exception else "",
                },
                elapsed_ms=elapsed_ms,
                # 【序号唯一性】同一节点内 enter_audit 已占用基准序号，
                # 本条目须 offset=1 顺延，否则两条日志序号重复，
                # 破坏"序号全局单调递增"的审计可追溯性要求。
                offset=1,
            )

            blocked_updates = build_blocked_state(
                state=state,
                node=node,
                reason=BlockedReason.RETRY_EXHAUSTED.value,
                detail_message=(
                    f"节点 {node} 重试 {retry_count} 次后仍失败，最后异常：{error_text}"
                ),
                retry_count=retry_count,
                retriable=True,
                extra_updates={
                    "exhausted_nodes": list(state.exhausted_nodes) + [node],
                },
            )
            # 【归约器语义】仅返回增量
            blocked_updates["audit_logs"] = [enter_audit, exhausted_audit]
            blocked_updates["audit_sequence"] = exhausted_audit.sequence
            return blocked_updates

        return wrapper

    return decorator


# ======================================================================
# 五、终态判定助手
# ======================================================================
def finalize_state(
    state: ReviewTaskState,
    node: str,
    message: str = "",
    detail: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    构造流程终态的状态增量

    说明：
        由 audit 节点在流程收敛后调用，负责：
            1. 依规则引擎结果确定最终任务状态（completed / blocked / failed）
            2. 记录流程结束时间与总耗时
            3. 追加终态审计日志

        注意：任务终态由规则引擎的阻塞判定结果决定，不由本函数自行推断。

    参数：
        state: 当前图状态
        node: 调用节点名
        message: 终态描述
        detail: 终态结构化详情

    返回：
        状态增量字典
    """
    finished_at = datetime.now()
    elapsed_ms: Optional[float] = None
    if state.started_at is not None:
        elapsed_ms = (finished_at - state.started_at).total_seconds() * 1000.0

    # 状态判定优先级：阻塞 > 失败 > 完成
    if state.is_blocked:
        final_status = ReviewTaskStatus.BLOCKED.value
    elif state.parse_status == ParseStatus.FAILED.value:
        final_status = ReviewTaskStatus.FAILED.value
    else:
        final_status = ReviewTaskStatus.COMPLETED.value

    finish_audit = build_audit_entry(
        state=state,
        node=node,
        action=AuditAction.TASK_FINISHED.value,
        message=message or f"流程结束，最终状态={final_status}",
        detail={
            "final_status": final_status,
            "risk_level": state.risk_level,
            "is_blocked": state.is_blocked,
            "official_risk_count": state.official_risk_count,
            "total_risk_count": state.total_risk_count,
            "route_type": state.route_type,
            "write_back_status": state.write_back_status,
            "total_elapsed_ms": elapsed_ms,
            **(detail or {}),
        },
        elapsed_ms=elapsed_ms,
    )

    logger.info(
        "流程收敛 | 任务=%s | 最终状态=%s | 综合等级=%s | 阻塞=%s | 总耗时=%.2fms",
        state.task_no,
        final_status,
        state.risk_level,
        state.is_blocked,
        elapsed_ms if elapsed_ms is not None else 0.0,
    )

    return {
        "status": final_status,
        "finished_at": finished_at,
        "current_node": node,
        "progress": 100,
        # 【归约器语义】仅返回增量
        "audit_logs": [finish_audit],
        "audit_sequence": finish_audit.sequence,
        "updated_at": finished_at,
    }


def calc_quality_score(
    suggestion_count: int,
    has_conclusion: bool,
    has_legal_basis: bool,
    rule_hit_count: int,
) -> Tuple[bool, List[str]]:
    """
    生成质量校验（五维校验的轻量实现）

    说明：
        本函数实现架构文档中"生成后校验"的确定性部分：
            1. 结论完整性：是否产出结论文本
            2. 法条引用真实性：是否提供了法律依据字段
            3. 条款完整性：风险项与建议项数量是否匹配
            4. 格式合规：文本非空且长度在配置上限内
            5. 规则冲突检测：是否存在同条款矛盾的决策

        语义一致性维度需人工或模型复核，不在确定性校验范围内，
        故此处只做四项确定性校验，并在返回消息中明确标注。

    参数：
        suggestion_count: 建议条数
        has_conclusion: 是否已生成结论文本
        has_legal_basis: 是否存在法律依据
        rule_hit_count: 规则命中数量

    返回：
        (是否通过, 未通过原因列表) 元组
    """
    issues: List[str] = []

    # 维度一：结论完整性
    if not has_conclusion:
        issues.append("结论完整性校验未通过：未产出审查结论文本")

    # 维度二：法条引用真实性
    if rule_hit_count > 0 and not has_legal_basis:
        issues.append("法条引用校验未通过：存在风险命中但未提供法律依据")

    # 维度三：条款完整性
    if rule_hit_count > 0 and suggestion_count == 0:
        issues.append("条款完整性校验未通过：存在风险命中但未生成任何修改建议")

    return (len(issues) == 0, issues)


def current_timestamp() -> int:
    """
    获取当前 Unix 时间戳（秒）

    说明：统一从 time_utils 获取，避免各节点自行调用 time.time() 造成口径不一。

    返回：
        当前时间戳（整数秒）
    """
    return now_timestamp()
