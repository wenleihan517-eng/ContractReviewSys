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

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

import httpx

from app.constants import MongoCollection, ReviewTaskStatus, RiskLevel
from app.core.config import settings
from app.core.logging_config import get_logger
from app.core.exceptions import BusinessException, ErrorCode

logger = get_logger(__name__)

# ----------------------------------------------------------------------
# 自动审查调度（上传即审链路）
# ----------------------------------------------------------------------
# 说明：前端"上传合同送审"只触发到解析；若无人再调审查触发接口，
#   任务会永久停留在 RUNNING（前端表现为"审查中 0%"长期不动）。
#   因此解析成功后由此处自动调度后台审查，形成"上传→解析→审查"一条龙。
#   用进程内集合做在飞去重，防止解析重试与手动触发并发导致同一任务重复跑图。
_auto_review_inflight: Set[str] = set()


def _schedule_auto_review(
    task_id: str,
    task_no: str,
    object_key: str,
    source_type: str,
    file_name: str,
    operator_username: str = "",
) -> None:
    """
    调度后台自动审查（fire-and-forget）

    说明：
        审查走完整 LangGraph 图，LLM 辅助节点失败时会降级（不中断主流程），
        但整图耗时可能达数十秒；为避免占用解析接口的响应时间，
        采用后台任务执行，任务状态经 MongoDB 轮询对前端可见。

    参数：
        task_id: 任务 ID
        task_no: 任务编号（日志用）
        object_key: 合同对象键
        source_type: digital / scanned
        file_name: 原始文件名（标题兜底用）
        operator_username: 提交经办人（完成/失败通知的精确接收人）
    """
    if task_id in _auto_review_inflight:
        logger.info("自动审查已在执行，跳过重复调度 | 任务=%s", task_no)
        return
    _auto_review_inflight.add(task_id)

    async def _runner() -> None:
        # 【2026-09-20 交互优化】审查失败分类 + 瞬时异常自动重试 1 次 + 通知
        try:
            await _auto_review_attempt()
        finally:
            _auto_review_inflight.discard(task_id)

    async def _auto_review_attempt() -> None:
        from app.services.failure_classifier import classify_failure

        max_attempts = 2  # 原始 1 次 + 瞬时异常自动重试 1 次
        attempt = 0
        while attempt < max_attempts:
            attempt += 1
            failure_type = ""
            reason = ""
            advice = ""
            is_transient = False
            try:
                outcome = await trigger_review(
                    task_id,
                    object_key=object_key,
                    source_type=source_type,
                    file_name=file_name,
                )
                logger.info(
                    "自动审查完成 | 任务=%s | accepted=%s | 状态=%s | message=%s",
                    task_no, outcome.accepted, outcome.status, outcome.message,
                )
                if outcome.accepted:
                    # 流程正常走完（含规则引擎终裁 blocked，属正确判定非异常）。
                    # 仅"审查完成"向经办员发通知；blocked/failed 不发完成通知。
                    if outcome.status in ("completed", "reviewed"):
                        from app.services import notification_service

                        await notification_service.notify_task_completed(
                            task_no=task_no,
                            risk_level=outcome.risk_level or "未知",
                            risk_count=outcome.risk_item_count or 0,
                            operator_username=operator_username,
                        )
                    return
                # accepted=False：图执行异常，任务已被置为 failed
                failure_type, reason, advice, is_transient = classify_failure(
                    None, outcome.message
                )
            except Exception as exc:  # noqa: BLE001 - 后台任务必须自吞异常
                logger.error(
                    "自动审查执行异常 | 任务=%s | 异常=%s", task_no, exc, exc_info=True
                )
                failure_type, reason, advice, is_transient = classify_failure(exc)

            # 本次尝试失败：落失败分类
            from app.repositories import task_repo

            await task_repo.update_task(
                task_id,
                {
                    "status": ReviewTaskStatus.FAILED.value,
                    "failure_type": failure_type,
                    "failure_reason": reason,
                    "updated_at": datetime.now(),
                },
            )

            # 瞬时异常自动重试 1 次；重试额度用尽后推送人工通知
            if is_transient and attempt < max_attempts:
                logger.warning(
                    "审查瞬时异常，自动重试（1/1）| 任务=%s | 类型=%s",
                    task_no, failure_type,
                )
                await task_repo.update_task(
                    task_id,
                    {
                        "status": ReviewTaskStatus.RUNNING.value,
                        "failure_type": None,
                        "failure_reason": None,
                        "updated_at": datetime.now(),
                    },
                )
                await asyncio.sleep(5)
                continue

            from app.services import notification_service

            await notification_service.notify_task_failed(
                task_no=task_no,
                failure_type=failure_type,
                reason=reason,
                advice=advice,
                operator_username=operator_username,
            )
            return

    task = asyncio.create_task(_runner(), name=f"auto-review-{task_no}")
    # 持有强引用，防止任务被垃圾回收中途取消（asyncio 官方建议做法）
    _auto_review_tasks.add(task)


# 后台任务强引用集合（仅防 GC，不参与业务判断）
_auto_review_tasks: set = set()

# 解析瞬时异常自动重试的防重入标记：
# 首次失败调度重试时加入，自动重试完成（无论成败）后移除；
# 重试期间再次失败则不再调度（保证"瞬时异常后台自动重试 1 次"语义）。
_parse_retry_scheduled: Set[str] = set()


def _schedule_parse_auto_retry(
    task_id: str,
    task_no: str,
    object_key: str,
    source_type: str,
    file_name: str,
) -> bool:
    """
    调度解析瞬时异常的后台自动重试（仅 1 次）

    说明：
        需求「失败自动重试机制」：仅接口超时、网络抖动这类瞬时异常，
        后台自动重试 1 次；文件损坏、格式错误类异常不自动重试，
        直接标记失败并推送人工通知，避免无效重复调用接口浪费资源。

    返回：
        True 表示已调度自动重试（此时不发人工通知）；
        False 表示不再调度（已重试过或非调度场景，调用方应发人工通知）
    """
    if task_id in _parse_retry_scheduled:
        return False
    _parse_retry_scheduled.add(task_id)

    async def _retry_runner() -> None:
        try:
            await asyncio.sleep(5)
            logger.warning("解析瞬时异常，自动重试（1/1）| 任务=%s", task_no)
            from app.repositories import task_repo
            await task_repo.update_task(
                task_id,
                {
                    "status": ReviewTaskStatus.RUNNING.value,
                    "parse_status": "parsing",
                    "failure_type": None,
                    "failure_reason": None,
                    "updated_at": datetime.now(),
                },
            )
            await trigger_parse(
                task_id,
                object_key=object_key,
                source_type=source_type,
                file_name=file_name,
            )
        except Exception as exc:  # noqa: BLE001 - 后台任务自吞异常
            logger.error("解析自动重试仍失败 | 任务=%s | 异常=%s", task_no, exc)
        finally:
            _parse_retry_scheduled.discard(task_id)

    task = asyncio.create_task(_retry_runner(), name=f"parse-retry-{task_no}")
    _auto_review_tasks.add(task)
    return True


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
            timeout=httpx.Timeout(timeout, connect=connect_timeout),
            # 内网审批系统不走系统代理（避免本机代理劫持 127.0.0.1 请求）
            trust_env=False,
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

    # -------------------- 字段取值：审批单优先，自动识别兜底 --------------------
    # 【兜底链（2026-09-19 上传免填表单改造）】
    #   审批单上下文 > 解析产物自动识别（parse_snapshot.auto_metadata）> 文件名/占位
    #   手动上传任务在审批系统中没有审批单，上下文为空字典；
    #   此时以 metadata_extractor 从合同原文自动识别的字段兜底，
    #   仍缺失的金额按 0 处理并在 notes 中备注（由后续识别推送补充），
    #   不再因基础信息缺失而阻塞审查 —— 否则"上传即审"链路必然卡死。
    snapshot = getattr(task, "parse_snapshot", None) or {}
    auto_meta: Dict[str, Any] = snapshot.get("auto_metadata") or {}

    contract_title = (
        approval_ctx.get("contract_title")
        or approval_ctx.get("title")
        or ""
    ).strip()
    if not contract_title:
        # 自动识别标题（解析后由系统回填，保证可追溯）
        contract_title = (auto_meta.get("contract_title") or "").strip()
    if not contract_title and file_name:
        contract_title = file_name.rsplit(".", 1)[0]

    contract_no = (
        approval_ctx.get("contract_no")
        or (auto_meta.get("contract_no") or "").strip()
        or task.task_no
        or ""
    ).strip()

    counterparty = (approval_ctx.get("counterparty") or "").strip()
    if not counterparty:
        # 自动识别乙方（相对方）；识别失败给明确占位，保证流程可继续
        counterparty = (
            auto_meta.get("party_b_name")
            or approval_ctx.get("applicant")
            or "待补充相对方"
        ).strip()

    # 金额取值链：审批单 > 自动识别（含中文大写解析）> 0（缺失备注，不阻塞）
    raw_amount = approval_ctx.get("contract_amount")
    if not raw_amount:
        raw_amount = auto_meta.get("amount")
    contract_amount = float(raw_amount or 0.0)

    # 合同类型取值链：审批单 > 自动识别（关键词轻量识别）> other
    # 【2026-09-21 修复｜规则静默漏检】
    #   此处原先只读审批单上下文，手动上传任务（无审批单，即"上传免填表单"
    #   改造后的唯一入口）恒得 "other"。而规则集是按 contract_type 过滤加载的
    #   （见 _load_active_rule_templates），口径错成 other 会直接导致类型专属
    #   规则整体不加载：
    #       purchase 26 条 → other 16 条
    #       PAY-001 / IP-001（付款与知识产权，规格 2.4.9 场景一必须命中）
    #       PUR-001 / PUR-002（缺失交付验收 / 质量保证）全部缺席
    #   结果是"规则引擎正常执行、无任何报错，但该命中的风险没命中"——
    #   属最危险的静默失效。解析阶段已把识别结果写入
    #   parse_snapshot.auto_metadata.contract_type，故必须与 contract_title /
    #   amount / counterparty 一样纳入同一兜底链，保持口径一致。
    contract_type = (
        approval_ctx.get("contract_type")
        or auto_meta.get("contract_type")
        or "other"
    ).strip()

    # 自动识别的备注信息（用于任务追溯：哪些字段是系统识别的）
    auto_notes: List[str] = list(snapshot.get("auto_notes") or [])

    missing: List[str] = []
    if not contract_title:
        missing.append("contract_title（合同标题）")
    if not contract_no:
        missing.append("contract_no（合同编号）")
    if missing:
        # 标题（文件名兜底）与编号（task_no 兜底）在正常链路下不可能同时缺失，
        # 保留该防御分支以拦截结构性异常（如任务主档损坏）
        raise ValueError(
            "合同基础信息不完整，无法启动审查流程，缺失：" + "、".join(missing)
        )

    if contract_amount <= 0:
        # 金额缺失不阻塞：路由按 standard 处理（≥500 万深度复核不适用），
        # 备注留痕，后续识别/审批单补充后可重跑
        auto_notes.append("合同金额缺失，路由按 standard 处理；待审批单或自动识别补充")

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
        currency=(approval_ctx.get("currency") or auto_meta.get("currency") or "CNY").strip(),
        performance_period=(approval_ctx.get("performance_period") or "").strip(),
        business_type=(approval_ctx.get("business_type") or "").strip(),
        contract_type=contract_type,
        template_flag=bool(approval_ctx.get("template_flag") or False),
        source_type=source_type,
        file_object_key=object_key,
        # 流程控制
        status=ReviewTaskStatus.RUNNING.value,
        route_type=task.route_type,
    )
    # 自动识别备注：仅日志留痕（图状态为契约不随意加字段；
    # 字段溯源已持久化在 parse_snapshot.auto_metadata 的 evidence 中）
    if auto_notes:
        logger.info(
            "自动识别备注 | 任务=%s | %s", task.task_no, "；".join(auto_notes)
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
        # 【2026-09-20 交互优化】失败分类落库：类型 + 面向用户原因（脱敏），
        # 供大盘悬浮提示、失败类型筛选、自动重试决策与人工通知使用。
        from app.services.failure_classifier import classify_failure

        failure_type, reason, advice, is_transient = classify_failure(exc)
        logger.error(
            "文档解析失败 | 任务=%s | 类型=%s | 异常=%s",
            task_id, failure_type, exc, exc_info=True,
        )
        await task_repo.update_task(
            task_id,
            {
                "status": ReviewTaskStatus.FAILED.value,
                "parse_status": "failed",
                "failure_type": failure_type,
                "failure_reason": reason,
                "updated_at": datetime.now(),
            },
        )

        # 瞬时异常（OCR 超时 / 网络抖动）：后台自动重试 1 次，暂不发人工通知；
        # 自动重试仍失败（不再调度）时才推送人工通知 —— 避免无效告警轰炸。
        scheduled = False
        if is_transient:
            scheduled = _schedule_parse_auto_retry(
                task_id=task_id,
                task_no=task.task_no,
                object_key=object_key,
                source_type=source_type,
                file_name=file_name,
            )
        if not scheduled:
            from app.services import notification_service

            await notification_service.notify_task_failed(
                task_no=task.task_no,
                failure_type=failure_type,
                reason=reason,
                advice=advice,
                operator_username=task.created_by or "",
            )

        return ParseTriggerOutcome(
            task_id=task_id,
            task_no=task.task_no,
            accepted=False,
            status=ReviewTaskStatus.FAILED.value,
            message=f"解析失败：{reason}",
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

    # -------------------- 自动识别合同元数据（上传免填表单） --------------------
    # 说明：metadata_extractor 全部字段"抽取失败即 None"，绝不猜测；
    #   识别结果写入 parse_snapshot.auto_metadata（含 evidence 证据可追溯），
    #   并把识别出的标题回填任务主档（前端列表即时可见），
    #   合同类型同步给出基于关键词的轻量识别，供规则集路由参考。
    from app.services.metadata_extractor import extract_all_metadata

    auto_meta = extract_all_metadata(full_text)
    auto_notes: List[str] = []
    if auto_meta.get("contract_title"):
        auto_notes.append(
            f"合同标题由系统自动识别：{auto_meta['contract_title']}"
        )
    if auto_meta.get("amount") is not None:
        auto_notes.append(
            f"合同金额由系统自动识别：{auto_meta['amount']} 元"
            f"（来源：{auto_meta.get('amount_source', 'unknown')}）"
        )
    else:
        auto_notes.append("合同金额未能自动识别，路由按 standard 处理")
    if auto_meta.get("party_b_name"):
        auto_notes.append(f"相对方由系统自动识别：{auto_meta['party_b_name']}")
    missing_fields = auto_meta.get("extraction_missing_fields") or []
    if missing_fields:
        auto_notes.append(f"以下字段待审批单或人工补充：{', '.join(missing_fields)}")

    # 合同类型轻量识别：基于标题与首段关键词（仅参考，不影响规则引擎判定权）
    identified_type = _identify_contract_type(full_text)
    if identified_type and identified_type != "other":
        auto_meta["contract_type"] = identified_type
        auto_notes.append(f"合同类型由系统自动识别：{identified_type}")

    await task_repo.update_task(
        task_id,
        {
            "parse_status": "parsed",
            "object_key": object_key,
            "source_type": source_type,
            "file_name": file_name,
            # 自动识别的标题回填主档：审批单缺失时前端列表可直接显示
            "contract_title": auto_meta.get("contract_title")
            or (file_name.rsplit(".", 1)[0] if file_name else None),
            "parse_snapshot": {
                "full_text": full_text,
                "text_blocks": snapshot_blocks,
                "page_count": len(page_texts),
                "auto_metadata": auto_meta,
                "auto_notes": auto_notes,
            },
            "updated_at": datetime.now(),
        },
    )

    logger.info(
        "文档解析完成 | 任务=%s | 全文长度=%d | 文本块=%d | 页数=%d",
        task_id, len(full_text), len(text_blocks), len(page_texts),
    )
    if auto_notes:
        logger.info("自动识别结果 | 任务=%s | %s", task.task_no, "；".join(auto_notes))

    # -------------------- 自动调度审查（上传即审一条龙） --------------------
    # 说明：此处不再要求前端/调用方额外触发审查接口；
    #   审查在后台执行，任务状态经 MongoDB 轮询对前端可见。
    _schedule_auto_review(
        task_id=task_id,
        task_no=task.task_no,
        object_key=object_key,
        source_type=source_type,
        file_name=file_name,
        operator_username=task.created_by or "",
    )

    return ParseTriggerOutcome(
        task_id=task_id,
        task_no=task.task_no,
        accepted=True,
        status=ReviewTaskStatus.RUNNING.value,
        full_text_length=len(full_text),
        text_block_count=len(text_blocks),
        clause_count=0,
        message="解析完成，审查已自动启动",
    )


def _identify_contract_type(text: str) -> str:
    """
    基于关键词的合同类型轻量识别

    说明：
        仅用于解析后给任务打"参考类型"标注（前端展示与规则集预选），
        【不参与任何风险定级】—— 定级权归规则引擎（架构红线）。
        规则审查按"通用规则 + 标题关键词匹配"的既有逻辑工作，
        不依赖本识别结果的正确性。

    参数：
        text: 合同全文

    返回：
        识别出的合同类型值（purchase/sales/service/labor/other）
    """
    if not text:
        return "other"
    head = text[:600]  # 标题与首部信息集中在文本开头
    rules: List[tuple] = [
        ("purchase", ("采购", "购销（采购方）", "设备采购", "供货")),
        ("sales", ("销售合同", "销售协议", "经销")),
        ("service", ("服务合同", "技术服务", "咨询服务", "运维服务", "委托服务")),
        ("labor", ("劳动合同", "聘用合同", "劳务合同", "用工")),
    ]
    for type_value, keywords in rules:
        for kw in keywords:
            if kw in head:
                return type_value
    return "other"


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
