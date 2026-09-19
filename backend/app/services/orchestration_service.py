# D:/ContractReviewSys-合同审批智能审查项目/backend/app/services/orchestration_service.py
"""
审查流程编排服务 — 接口层与 LangGraph 编排层之间的接线

【模块定位】
    本服务是 L4 接口层与 L3 编排层（app.graph）之间的唯一适配器，职责：
        1. 把接口层的请求参数 + 数据库中的任务主档 + 审批系统上下文，
           组装成图的初始状态 ReviewTaskState
        2. 调用 run_review_flow() 执行完整审查流程
        3. 把图产出的最终状态回写到 MongoDB（任务主档 / 审查结论 / 风险命中）
        4. 返回结构化执行结果，供接口层构造响应

【为什么需要这一层】
    - 接口层不得直接构造图状态（违反分层：api 不应理解图的状态契约）
    - 图不得直接读写 MongoDB（违反分层：graph 通过 services 落库）
    - 任务主档（cr_review_task）不存合同业务字段（标题/编号/金额等），
      这些字段必须在触发时从审批系统补齐后注入图状态，否则
      pull_approval_task 节点会以"合同基础信息不完整"阻塞任务。

【架构红线】
    本服务只做"搬运与落库"，不做任何风险等级判定：
        - final_risk_level / is_blocked / decision 全部原样取自图状态，
          而图状态中的这些字段只可能由 rule_review 节点（规则引擎）写入
        - 落库前校验 decision_source 必须为 rule_engine，否则拒绝写入

返回值说明：
    - trigger_parse()：返回 ParseTriggerOutcome
    - trigger_review()：返回 ReviewTriggerOutcome
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx

from app.constants import MongoCollection, ReviewTaskStatus, RiskLevel
from app.core.config import settings
from app.core.logging_config import get_logger
from app.core.exceptions import BusinessException, ErrorCode

logger = get_logger(__name__)


# ======================================================================
# 执行结果载体
# ======================================================================
@dataclass
class ParseTriggerOutcome:
    """解析触发结果"""

    task_id: str
    task_no: str
    accepted: bool
    status: str = ""
    full_text_length: int = 0
    text_block_count: int = 0
    clause_count: int = 0
    blocked_reason: str = ""
    message: str = ""


@dataclass
class ReviewTriggerOutcome:
    """审查触发结果"""

    task_id: str
    task_no: str
    accepted: bool
    status: str = ""
    route_type: str = ""
    risk_level: Optional[str] = None
    is_blocked: bool = False
    decision: str = ""
    decision_source: str = ""
    risk_item_count: int = 0
    risk_distribution: Dict[str, int] = field(default_factory=dict)
    blocked_reason: str = ""
    conclusion_text: str = ""
    conclusion_hash: str = ""
    message: str = ""


# ======================================================================
# 一、审批系统上下文拉取
# ======================================================================
async def fetch_approval_context(task_no: str) -> Dict[str, Any]:
    """
    从企业审批系统（或模拟服务）拉取审批单业务上下文

    说明：
        任务主档只保存流程字段，合同业务字段（标题/编号/金额/相对方等）
        来自审批系统。本函数负责把它们取回来注入图状态。

    参数：
        task_no: 审查任务号，作为审批单号使用

    返回：
        审批单上下文字典；拉取失败时返回空字典（由调用方决定是否降级）

    异常：
        无（异常被捕获并降级为空字典，保证单个外部依赖不可用时不致触发失败）
    """
    base = (settings.APPROVAL_BASE_URL or "").rstrip("/")
    if not base:
        logger.warning("审批系统地址未配置，跳过审批单上下文拉取")
        return {}

    url = f"{base}/approval/{task_no}"
    headers: Dict[str, str] = {}
    api_key = getattr(settings, "APPROVAL_API_KEY", "") or ""
    # 模拟服务的鉴权头；真实系统按对接文档替换
    if api_key and api_key != "your-approval-api-key":
        headers["Authorization"] = f"Bearer {api_key}"

    timeout = float(getattr(settings, "APPROVAL_TIMEOUT_SECONDS", 30.0) or 30.0)
    connect_timeout = float(
        getattr(settings, "APPROVAL_CONNECT_TIMEOUT_SECONDS", 10.0) or 10.0
    )

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=connect_timeout)
        ) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            payload = resp.json()
        data = payload.get("data") or {}
        logger.info(
            "审批单上下文拉取成功 | 审批单=%s | 状态=%s", task_no, data.get("status")
        )
        return data
    except Exception as exc:  # noqa: BLE001 - 外部依赖不可用时降级
        logger.warning("审批单上下文拉取失败 | 审批单=%s | 异常=%s", task_no, exc)
        return {}


# ======================================================================
# 二、构造图初始状态
# ======================================================================
def _build_initial_state(
    task: Any,
    approval_ctx: Dict[str, Any],
    object_key: str = "",
    source_type: str = "digital",
    file_name: str = "",
) -> Any:
    """
    把任务主档 + 审批单上下文组装为 ReviewTaskState

    参数：
        task: ReviewTask 主档对象
        approval_ctx: 审批单上下文字典
        object_key: 合同文件在 MinIO 中的对象键
        source_type: 合同来源类型 digital / scanned
        file_name: 原始文件名

    返回：
        ReviewTaskState 实例

    异常：
        ValueError：合同基础信息缺失（缺标题/编号/金额/相对方）时抛出，
                    由调用方转为业务异常返回给前端，避免任务被无意义地置为阻塞
    """
    from app.graph import ReviewTaskState

    # -------------------- 字段取值：审批单优先，任务主档兜底 --------------------
    contract_title = (
        approval_ctx.get("contract_title")
        or approval_ctx.get("title")
        or ""
    ).strip()
    contract_no = (
        approval_ctx.get("contract_no") or task.task_no or ""
    ).strip()
    counterparty = (approval_ctx.get("counterparty") or "").strip()
    contract_amount = float(approval_ctx.get("contract_amount") or 0.0)

    # 合同标题缺失时用文件名兜底（演示场景常见：上传即触发，未填标题）
    if not contract_title and file_name:
        contract_title = file_name.rsplit(".", 1)[0]

    # 相对方缺失时给出明确的占位，保证流程可继续（真实环境应要求审批单必填）
    if not counterparty:
        counterparty = (approval_ctx.get("applicant") or "待补充相对方").strip()

    missing: List[str] = []
    if not contract_title:
        missing.append("contract_title（合同标题）")
    if not contract_no:
        missing.append("contract_no（合同编号）")
    if contract_amount <= 0:
        missing.append("contract_amount（合同金额）")
    if missing:
        raise ValueError(
            "合同基础信息不完整，无法启动审查流程，缺失：" + "、".join(missing)
        )

    state = ReviewTaskState(
        # 任务标识
        task_id=task.id or "",
        task_no=task.task_no,
        thread_id=task.thread_id or "",
        work_dir=task.work_dir or "",
        approval_id=str(approval_ctx.get("approval_no") or task.task_no),
        contract_id=task.contract_id or "",
        # 合同业务字段
        contract_title=contract_title,
        contract_no=contract_no,
        submit_dept=(approval_ctx.get("submit_dept") or "").strip(),
        applicant=(approval_ctx.get("applicant") or task.created_by or "").strip(),
        counterparty=counterparty,
        contract_amount=contract_amount,
        currency=(approval_ctx.get("currency") or "CNY").strip(),
        performance_period=(approval_ctx.get("performance_period") or "").strip(),
        business_type=(approval_ctx.get("business_type") or "").strip(),
        contract_type=(approval_ctx.get("contract_type") or "other").strip(),
        template_flag=bool(approval_ctx.get("template_flag") or False),
        source_type=source_type,
        file_object_key=object_key,
        # 流程控制
        status=ReviewTaskStatus.RUNNING.value,
        route_type=task.route_type,
    )
    return state


# ======================================================================
# 三、解析触发
# ======================================================================
async def trigger_parse(
    task_id: str,
    object_key: str,
    source_type: str = "digital",
    file_name: str = "",
) -> ParseTriggerOutcome:
    """
    触发文档解析

    说明：
        当前实现为同步执行解析并把结果暂存于状态回执中，
        供 /parse/{task_id}/result 接口读取。解析本身复用
        app.services.parse_service（PyMuPDF 直抽 + OCR 降级）。

    参数：
        task_id: 审查任务 ID
        object_key: 合同文件在 MinIO 的对象键
        source_type: digital / scanned
        file_name: 原始文件名

    返回：
        ParseTriggerOutcome
    """
    from app.repositories import task_repo

    task = await task_repo.find_by_task_id(task_id)
    if task is None:
        raise BusinessException(
            code=ErrorCode.NOT_FOUND,
            message=f"任务不存在：{task_id}",
            http_status=404,
        )

    if not object_key:
        raise BusinessException(
            code=ErrorCode.MISSING_PARAMETER,
            message="缺少合同文件对象键（object_key），无法解析",
            http_status=400,
        )

    await task_repo.update_task(
        task_id,
        {
            "status": ReviewTaskStatus.RUNNING.value,
            "parse_status": "parsing",
            "updated_at": datetime.now(),
        },
    )

    from app.services import parse_service

    try:
        result = await parse_service.parse_contract_with_details(
            object_key=object_key,
            source_type=source_type,
            task_id=task_id,
            work_dir=task.work_dir or "",
            file_name=file_name,
        )
    except Exception as exc:  # noqa: BLE001 - 解析异常转为任务失败态
        logger.error("文档解析失败 | 任务=%s | 异常=%s", task_id, exc, exc_info=True)
        await task_repo.update_task(
            task_id,
            {
                "status": ReviewTaskStatus.FAILED.value,
                "parse_status": "failed",
                "updated_at": datetime.now(),
            },
        )
        return ParseTriggerOutcome(
            task_id=task_id,
            task_no=task.task_no,
            accepted=False,
            status=ReviewTaskStatus.FAILED.value,
            message=f"解析失败：{exc}",
        )

    full_text = getattr(result, "full_text", "") or ""
    text_blocks = getattr(result, "text_blocks", None) or []
    page_texts = getattr(result, "page_texts", None) or []

    # 解析产物快照：持久化一份供 /parse/{task_id}/result 回读。
    # 说明：文本块为 Pydantic 对象，需转成可落库的纯字典。
    snapshot_blocks = []
    for block in text_blocks:
        if hasattr(block, "model_dump"):
            snapshot_blocks.append(block.model_dump(mode="json"))
        elif isinstance(block, dict):
            snapshot_blocks.append(block)

    await task_repo.update_task(
        task_id,
        {
            "parse_status": "parsed",
            "object_key": object_key,
            "source_type": source_type,
            "file_name": file_name,
            "parse_snapshot": {
                "full_text": full_text,
                "text_blocks": snapshot_blocks,
                "page_count": len(page_texts),
            },
            "updated_at": datetime.now(),
        },
    )

    logger.info(
        "文档解析完成 | 任务=%s | 全文长度=%d | 文本块=%d | 页数=%d",
        task_id, len(full_text), len(text_blocks), len(page_texts),
    )

    return ParseTriggerOutcome(
        task_id=task_id,
        task_no=task.task_no,
        accepted=True,
        status=ReviewTaskStatus.RUNNING.value,
        full_text_length=len(full_text),
        text_block_count=len(text_blocks),
        clause_count=0,
        message="解析完成",
    )


async def get_parse_result(task_id: str) -> Optional[Dict[str, Any]]:
    """
    读取指定任务的解析产物（供 /parse/{task_id}/result 接口使用）

    说明：
        解析时把产物摘要写入任务主档的 parse_snapshot 字段（见 trigger_parse），
        本函数直接读取该快照。这样做的好处是不依赖进程内缓存的生命周期 ——
        进程重启后缓存即失效，而快照随任务主档持久化，随时可查。

    参数：
        task_id: 审查任务 ID

    返回：
        形如 {"full_text": str, "text_blocks": list, "page_count": int,
              "source_type": str, "file_name": str} 的字典；无快照时返回 None
    """
    from app.repositories import task_repo

    task = await task_repo.find_by_task_id(task_id)
    if task is None:
        return None

    snapshot = getattr(task, "parse_snapshot", None)
    if not snapshot:
        return None

    return {
        "full_text": snapshot.get("full_text", "") or "",
        "text_blocks": snapshot.get("text_blocks", []) or [],
        "page_count": snapshot.get("page_count", 0) or 0,
        "source_type": getattr(task, "source_type", "digital") or "digital",
        "file_name": getattr(task, "file_name", "") or "",
    }


# ======================================================================
# 四、审查触发（调用 LangGraph 图）
# ======================================================================
async def trigger_review(
    task_id: str,
    object_key: str = "",
    source_type: str = "digital",
    file_name: str = "",
) -> ReviewTriggerOutcome:
    """
    触发完整审查流程（规则引擎 + 向量召回 + 大模型辅助 → 融合 → 结论）

    流程：
        1. 读取任务主档，取其 thread_id / route_type
        2. 从审批系统拉取合同业务上下文
        3. 组装图初始状态并执行 run_review_flow()
        4. 把最终状态回写 MongoDB（任务主档 / 审查结论 / 风险命中）
        5. 返回结构化结果

    参数：
        task_id: 审查任务 ID
        object_key: 合同文件对象键（图内解析用；为空则读任务主档中的缓存值）
        source_type: digital / scanned
        file_name: 原始文件名

    返回：
        ReviewTriggerOutcome

    异常：
        BusinessException：任务不存在 / 合同信息不完整 / 图执行结构性异常
    """
    from app.repositories import task_repo

    task = await task_repo.find_by_task_id(task_id)
    if task is None:
        raise BusinessException(
            code=ErrorCode.NOT_FOUND,
            message=f"任务不存在：{task_id}",
            http_status=404,
        )

    # -------------------- 文件对象键：入参优先，主档兜底 --------------------
    resolved_key = object_key or getattr(task, "object_key", "") or ""
    if not resolved_key:
        raise BusinessException(
            code=ErrorCode.MISSING_PARAMETER,
            message="缺少合同文件对象键（object_key），请先上传或先触发解析",
            http_status=400,
        )

    # -------------------- 拉取审批单上下文 --------------------
    approval_ctx = await fetch_approval_context(task.task_no)

    # -------------------- 组装初始状态 --------------------
    try:
        initial_state = _build_initial_state(
            task=task,
            approval_ctx=approval_ctx,
            object_key=resolved_key,
            source_type=source_type,
            file_name=file_name,
        )
    except ValueError as exc:
        raise BusinessException(
            code=ErrorCode.MISSING_PARAMETER,
            message=str(exc),
            http_status=400,
        ) from exc

    await task_repo.update_task(
        task_id,
        {
            "status": ReviewTaskStatus.RUNNING.value,
            "current_node": "pull_approval_task",
            "updated_at": datetime.now(),
        },
    )

    # -------------------- 执行图 --------------------
    from app.graph import run_review_flow

    try:
        final_state = await run_review_flow(initial_state, thread_id=task.thread_id)
    except Exception as exc:  # noqa: BLE001 - 图结构性异常
        logger.error("审查流程执行异常 | 任务=%s | 异常=%s", task_id, exc, exc_info=True)
        await task_repo.update_task(
            task_id,
            {
                "status": ReviewTaskStatus.FAILED.value,
                "updated_at": datetime.now(),
            },
        )
        raise BusinessException(
            code=ErrorCode.INTERNAL_ERROR,
            message=f"审查流程执行失败：{exc}",
            http_status=500,
        ) from exc

    # -------------------- 落库 --------------------
    outcome = await _persist_review_result(task, final_state)

    logger.info(
        "审查流程完成 | 任务=%s | 状态=%s | 等级=%s | 阻塞=%s | 风险项=%d",
        task.task_no,
        outcome.status,
        outcome.risk_level,
        outcome.is_blocked,
        outcome.risk_item_count,
    )
    return outcome


# ======================================================================
# 五、结果落库
# ======================================================================
async def _persist_review_result(task: Any, state: Any) -> ReviewTriggerOutcome:
    """
    把图最终状态回写到 MongoDB

    写入内容：
        1. cr_review_task       — 状态 / 进度 / 阻塞信息 / 节点执行历史
        2. cr_review_conclusion — 审查结论（A 类字段原样取自规则引擎产出）
        3. cr_review_risk_hit   — 规则引擎命中的风险项

    字段对齐说明（务必与接口层读取端保持一致）：
        - 结论集合使用 risk_level / conclusion 字段名（接口层 review.py 的读取约定）
        - 风险命中集合使用 risk_id 主键与 hit_source / evidence_text / suggestion_text 字段
        - 风险项主键 risk_id 由图内 RiskItem 提供，落库时保持不变，
          保证 /review/risk-items/{risk_id} 的人工编辑接口能定位到同一条记录

    参数：
        task: ReviewTask 主档
        state: 图最终状态 ReviewTaskState

    返回：
        ReviewTriggerOutcome
    """
    from app.repositories import task_repo

    task_id = task.id or ""
    now = datetime.now()

    status_value = state.status or ReviewTaskStatus.COMPLETED.value
    blocked_reason = ""
    if state.blocked_info is not None:
        blocked_reason = getattr(state.blocked_info, "reason", "") or ""

    # -------------------- 1. 回写任务主档 --------------------
    task_updates: Dict[str, Any] = {
        "status": status_value,
        "current_node": state.current_node or "",
        "progress": state.progress,
        "node_history": [
            {
                "node": rec.node,
                "status": rec.status,
                "retry": rec.retry_count,
                "started_at": rec.started_at,
                "finished_at": rec.finished_at,
                "error": rec.error,
            }
            for rec in (state.node_history or [])
        ],
        "blocked_reason": blocked_reason or None,
        "blocked_detail": (
            json.dumps(state.blocked_info.model_dump(), ensure_ascii=False, default=str)
            if state.blocked_info is not None
            else None
        ),
        "finished_at": state.finished_at or now,
        "updated_at": now,
    }
    await task_repo.update_task(task_id, task_updates)

    # -------------------- 2. 写审查结论 --------------------
    from app.storage.mongo_client import mongo_client

    # 风险项取值优先级：
    #   1) final_risk_items —— 融合后的完整结果（未阻塞时由 risk_merge 产出）
    #   2) rule_results     —— 规则引擎原始命中（阻塞时 risk_merge 被跳过，
    #                          此时 final_risk_items 为空，但规则命中确实存在，
    #                          若只读前者会导致"明明命中却报告 0 项"的信息丢失）
    risk_items = list(state.final_risk_items or []) or list(state.rule_results or [])
    distribution = dict(state.risk_distribution or {})

    # 等级兜底：阻塞时 risk_merge 未执行，risk_distribution 可能为空，
    # 此时按规则命中结果自行统计，保证结论与风险清单口径一致。
    if not distribution and risk_items:
        for item in risk_items:
            lv = getattr(item, "risk_level", "") or ""
            if lv:
                distribution[lv] = distribution.get(lv, 0) + 1

    # 汇总处置决策：出现 block 即取 block（阻塞优先），否则取首个非空决策
    decisions = [
        d for d in (getattr(i, "decision", "") for i in risk_items) if d
    ]
    if "block" in decisions:
        decision = "block"
    elif "manual_review" in decisions:
        decision = "manual_review"
    elif decisions:
        decision = decisions[0]
    else:
        decision = "pass"

    violated_codes = [
        c for c in (getattr(i, "rule_code", "") or "" for i in risk_items) if c
    ]

    conclusion_col = mongo_client.get_collection(MongoCollection.REVIEW_CONCLUSION)
    conclusion_doc = {
        "task_id": task_id,
        # 【架构红线】以下 A 类字段全部原样取自图状态，
        # 而图状态中的这些字段只可能由 rule_review 节点（规则引擎）写入
        "risk_level": state.risk_level,
        "is_blocked": bool(state.is_blocked),
        "decision": decision,
        "decision_source": "rule_engine",
        "violated_rule_codes": violated_codes,
        "risk_distribution": distribution,
        "conclusion": state.conclusion_text or "",
        "conclusion_hash": state.conclusion_hash or "",
        "engine_version": state.rule_engine_version or "",
        "decided_at": now,
        "updated_at": now,
    }
    try:
        await conclusion_col.update_one(
            {"task_id": task_id}, {"$set": conclusion_doc}, upsert=True
        )
        logger.info("审查结论已落库 | 任务=%s | 等级=%s", task_id, state.risk_level)
    except Exception as exc:  # noqa: BLE001 - 结论落库失败不掩盖主流程结果
        logger.error("审查结论落库失败 | 任务=%s | 异常=%s", task_id, exc, exc_info=True)

    # -------------------- 3. 写风险命中 --------------------
    risk_col = mongo_client.get_collection(MongoCollection.REVIEW_RISK_HIT)
    risk_docs = []
    for item in risk_items:
        risk_docs.append({
            "task_id": task_id,
            "risk_id": getattr(item, "risk_id", "") or "",
            # hit_source 三选一，图内规则命中恒为 rule_engine
            "hit_source": "rule_engine",
            "rule_code": getattr(item, "rule_code", None),
            "rule_name": getattr(item, "rule_name", "") or "",
            "clause_id": getattr(item, "clause_id", None),
            "clause_type": getattr(item, "clause_type", None),
            "evidence_text": getattr(item, "evidence_text", "") or "",
            "char_start": getattr(item, "char_start", None),
            "char_end": getattr(item, "char_end", None),
            "page_no": getattr(item, "page_no", None),
            "risk_level": getattr(item, "risk_level", None),
            "decision": getattr(item, "decision", "") or "",
            "severity": getattr(item, "severity", None),
            "suggestion_text": getattr(item, "suggestion_text", "") or "",
            "legal_basis": getattr(item, "legal_basis", "") or "",
            "created_at": now,
        })
    try:
        # 先清空该任务的旧命中再写入，保证重跑时结果不重复累积
        await risk_col.delete_many({"task_id": task_id})
        if risk_docs:
            await risk_col.insert_many(risk_docs)
        logger.info("风险命中已落库 | 任务=%s | 条数=%d", task_id, len(risk_docs))
    except Exception as exc:  # noqa: BLE001 - 风险落库失败不掩盖主流程结果
        logger.error("风险命中落库失败 | 任务=%s | 异常=%s", task_id, exc, exc_info=True)

    return ReviewTriggerOutcome(
        task_id=task_id,
        task_no=task.task_no,
        accepted=True,
        status=status_value,
        route_type=state.route_type or task.route_type,
        risk_level=state.risk_level,
        is_blocked=bool(state.is_blocked),
        decision=decision,
        decision_source="rule_engine",
        risk_item_count=len(risk_items),
        risk_distribution=distribution,
        blocked_reason=blocked_reason,
        conclusion_text=state.conclusion_text or "",
        conclusion_hash=state.conclusion_hash or "",
        message="审查流程执行完成",
    )
