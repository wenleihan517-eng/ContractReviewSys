# D:/ContractReviewSys-合同审批智能审查项目/backend/app/services/comment_service.py
"""
审批评论生成与回写服务 — 审查意见的拼装、校验与评论区写入

【模块定位】
    本模块是 generate_comment 与 write_comment 两个节点背后的服务。

    职责：
        1. 按模板拼装审批评论（风险摘要 + 分级明细 + 修改建议汇总 + 人工提示）
        2. 生成后格式校验，输出标准中文专业表述
        3. 回写前幂等校验（已回写则跳过，避免重复评论）
        4. 预览模式（仅记录不写入）/ 正式模式（调用审批系统评论区接口）
        5. 回写失败记录错误信息，支持重试

【权责红线（代码级强制）】
    本模块仅生成审查意见并写入审批系统评论区：
        - 不得修改审批状态
        - 不得触发同意 / 拒绝 / 转交 / 撤回等改变审批结论的操作
        - 最终决策权保留给审批人
    强制手段：
        1. 接口路径白名单校验（approval_guard.ensure_comment_only）
        2. 请求体字段清洗（剔除 action/approve/reject 等审批动作字段）
        3. 评论末尾固定附加"不改变审批结论"声明

返回值说明：
    - generate_comment_service()：返回 CommentResult
    - write_comment_service()：返回 WriteBackInfo（回写结果载体）

使用示例：
    comment = await generate_comment_service(state)
    write_back = await write_comment_service(state, preview=True)
"""

import asyncio
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from app.constants import RISK_LEVEL_ORDER, RiskLevel
from app.core.config import settings
from app.core.logging_config import get_logger
from app.services.report_service import RISK_LEVEL_LABELS
from app.utils.approval_guard import build_comment_payload, ensure_comment_only

logger = get_logger(__name__)


class CommentResult(BaseModel):
    """
    评论生成结果

    字段：
        comment_text: 评论文本
        payload: 提交给审批系统的评论内容字典
        api_path: 评论接口路径
        risk_item_count: 写入评论的正式风险项数
        validation_passed: 格式校验是否通过
        validation_issues: 格式校验问题列表
    """

    comment_text: str = Field(default="", description="评论文本")
    payload: Dict[str, Any] = Field(default_factory=dict, description="评论内容字典")
    api_path: str = Field(default="", description="评论接口路径")
    risk_item_count: int = Field(default=0, description="正式风险项数")
    validation_passed: bool = Field(default=False, description="格式校验是否通过")
    validation_issues: List[str] = Field(
        default_factory=list, description="格式校验问题列表"
    )


# ======================================================================
# 一、评论生成
# ======================================================================
async def generate_comment_service(
    state: Any,
) -> CommentResult:
    """
    生成审批评论内容（主入口）

    流程：
        1. 校验回写前置条件（规则引擎已判定、任务未阻塞）
        2. 校验接口路径白名单
        3. 构造风险明细（仅正式判定项，参考项不写入）
        4. 按模板拼装评论（风险摘要 + 分级明细 + 修改建议汇总 + 人工提示）
        5. 格式校验

    参数：
        state: 图状态（ReviewTaskState）

    返回：
        CommentResult

    异常：
        ApprovalPermissionException：接口越权（不可重试，直接阻塞）
    """
    # -------------------- 步骤一：接口路径白名单校验（红线强制） --------------------
    api_path = settings.APPROVAL_COMMENT_API_PATH
    ensure_comment_only(api_path)

    # -------------------- 步骤二：构造正式风险项明细（参考项不写入） --------------------
    official_items = [item for item in state.final_risk_items if not item.reference_only]
    risk_items: List[Dict[str, Any]] = [
        {
            "rule_code": item.rule_code or "",
            "rule_name": item.rule_name,
            "evidence_text": item.evidence_text,
            "suggested_text": item.suggestion_text,
            "risk_level": item.risk_level,
            "legal_basis": item.legal_basis,
        }
        for item in official_items
    ]

    # -------------------- 步骤三：按模板拼装评论 --------------------
    comment_text = _build_comment_text(
        task_no=state.task_no,
        final_risk_level=state.risk_level or RiskLevel.LOW.value,
        conclusion_text=state.conclusion_text,
        risk_items=risk_items,
        report_url=state.report_url,
        legal_opinion="",
    )

    # 构造 payload（沿用 approval_guard.build_comment_payload 的字段约定）
    payload = build_comment_payload(
        task_no=state.task_no,
        final_risk_level=state.risk_level or RiskLevel.LOW.value,
        conclusion_text=state.conclusion_text,
        risk_items=[
            {
                "rule_code": item["rule_code"],
                "rule_name": item["rule_name"],
                "evidence_text": item["evidence_text"],
                "suggested_text": item["suggested_text"],
            }
            for item in risk_items
        ],
        report_url=state.report_url,
        legal_opinion="",
    )
    # 用自定义的结构化评论文本覆盖 payload 的默认评论文本
    payload["contract_review_comment"] = comment_text
    payload["risk_count"] = len(risk_items)

    # -------------------- 步骤四：格式校验 --------------------
    validation_passed, validation_issues = _validate_comment(comment_text, risk_items)

    return CommentResult(
        comment_text=comment_text,
        payload=payload,
        api_path=api_path,
        risk_item_count=len(risk_items),
        validation_passed=validation_passed,
        validation_issues=validation_issues,
    )


def _build_comment_text(
    task_no: str,
    final_risk_level: str,
    conclusion_text: str,
    risk_items: List[Dict[str, Any]],
    report_url: str = "",
    legal_opinion: str = "",
) -> str:
    """
    按模板拼装审批评论文本

    结构：风险摘要 → 分级风险明细 → 修改建议汇总 → 人工提示

    参数：
        task_no: 审查任务编号
        final_risk_level: 最终风险等级
        conclusion_text: 审查结论
        risk_items: 风险明细列表
        report_url: 报告链接
        legal_opinion: 法务补充意见

    返回：
        结构化评论文本
    """
    level_label = RISK_LEVEL_LABELS.get(final_risk_level, final_risk_level)

    lines: List[str] = [
        "【合同智能审查意见】",
        f"任务编号：{task_no}",
        f"综合风险等级：{level_label}",
        "",
        "一、风险摘要",
    ]

    # -------------------- 风险摘要：按等级统计 --------------------
    if risk_items:
        level_counter: Dict[str, int] = {}
        for item in risk_items:
            level = item.get("risk_level", "")
            level_counter[level] = level_counter.get(level, 0) + 1

        summary_parts = []
        # 按等级从高到低排列统计
        for level in sorted(level_counter.keys(), key=lambda l: RISK_LEVEL_ORDER.get(l, 0), reverse=True):
            summary_parts.append(
                f"{RISK_LEVEL_LABELS.get(level, level)} {level_counter[level]} 项"
            )
        lines.append(
            f"本次审查共识别 {len(risk_items)} 项规则引擎判定风险"
            f"（{'、'.join(summary_parts)}）。"
        )
    else:
        lines.append("本次审查未发现规则引擎判定的风险项。")

    # -------------------- 分级风险明细 --------------------
    lines.append("")
    lines.append("二、分级风险明细")

    if risk_items:
        # 按等级分组（高 → 低）
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for item in risk_items:
            level = item.get("risk_level", "")
            grouped.setdefault(level, []).append(item)

        for level in sorted(grouped.keys(), key=lambda l: RISK_LEVEL_ORDER.get(l, 0), reverse=True):
            lines.append("")
            lines.append(f"【{RISK_LEVEL_LABELS.get(level, level)}】")
            for index, item in enumerate(grouped[level], start=1):
                lines.append(
                    f"{index}. [{item.get('rule_code', '-')}] {item.get('rule_name', '未命名规则')}"
                )
                evidence = item.get("evidence_text", "")
                if evidence:
                    lines.append(f"   证据：{evidence}")
                suggestion = item.get("suggested_text", "")
                if suggestion:
                    lines.append(f"   建议：{suggestion}")
                legal = item.get("legal_basis", "")
                if legal:
                    lines.append(f"   法律依据：{legal}")
    else:
        lines.append("无。")

    # -------------------- 修改建议汇总 --------------------
    lines.append("")
    lines.append("三、修改建议汇总")
    if risk_items:
        suggestions = [item.get("suggested_text", "") for item in risk_items if item.get("suggested_text")]
        if suggestions:
            for index, suggestion in enumerate(suggestions, start=1):
                lines.append(f"{index}. {suggestion}")
        else:
            lines.append("详见审查报告中的修改建议清单。")
    else:
        lines.append("无。")

    # -------------------- 人工提示 --------------------
    lines.append("")
    lines.append("四、人工提示")
    lines.append(f"审查结论：{conclusion_text}")
    if report_url:
        lines.append(f"审查报告：{report_url}")
    if legal_opinion:
        lines.append(f"法务补充意见：{legal_opinion}")
    lines.append(
        "本意见由规则引擎判定产出，仅供审批参考，不改变审批结论，最终决策权保留给审批人。"
    )

    return "\n".join(lines)


def _validate_comment(
    comment_text: str,
    risk_items: List[Dict[str, Any]],
) -> tuple:
    """
    评论格式校验

    校验维度：
        1. 评论非空
        2. 含四个必需段落（风险摘要 / 分级明细 / 修改建议汇总 / 人工提示）
        3. 含"不改变审批结论"声明（红线）
        4. 长度不超限

    参数：
        comment_text: 评论文本
        risk_items: 风险明细列表

    返回：
        (是否通过, 问题列表)
    """
    issues: List[str] = []

    if not comment_text.strip():
        issues.append("评论文本为空")
        return (False, issues)

    # 必需段落检查
    required_sections = {
        "一、风险摘要": "风险摘要",
        "二、分级风险明细": "分级风险明细",
        "三、修改建议汇总": "修改建议汇总",
        "四、人工提示": "人工提示",
    }
    for marker, label in required_sections.items():
        if marker not in comment_text:
            issues.append(f"缺少必需段落：{label}")

    # 红线声明检查
    if "不改变审批结论" not in comment_text:
        issues.append("缺少'不改变审批结论'红线声明")

    # 有风险时必须含分级明细
    if risk_items and "【" not in comment_text:
        issues.append("存在风险项但缺少分级明细标记")

    # 长度检查
    max_length = settings.SUGGESTION_MAX_LENGTH * 4  # 评论可长于单条建议
    if len(comment_text) > max_length:
        issues.append(f"评论文本长度 {len(comment_text)} 超过上限 {max_length}")

    if issues:
        logger.warning("评论格式校验未通过 | 问题=%s", issues)

    return (not issues, issues)


# ======================================================================
# 二、评论回写
# ======================================================================
async def write_comment_service(
    state: Any,
    preview: bool = False,
) -> Any:
    """
    审批评论区回写（主入口）

    流程：
        1. 幂等校验：已回写（success/not_written）则跳过
        2. 接口路径二次白名单校验（防御性）
        3. 请求体字段清洗（剔除审批动作字段）
        4. 预览模式：仅记录内容，不实际写入
        5. 正式模式：调用审批系统评论区接口，支持重试
        6. 记录返回结果，更新回写状态

    参数：
        state: 图状态（ReviewTaskState）
        preview: 是否预览模式（True 仅记录不写入）

    返回：
        WriteBackInfo（回写结果载体）

    说明：
        回写失败不抛异常（不阻塞报告产出），由系统管理员通过重推接口处理。
    """
    # 延迟导入，避免与 graph 包的循环依赖
    from app.graph.state import WriteBackInfo, WriteBackState

    # -------------------- 幂等校验：已回写则跳过 --------------------
    if state.write_back_status in (
        WriteBackState.SUCCESS.value,
        WriteBackState.NOT_WRITTEN.value,
    ):
        logger.info(
            "回写幂等跳过 | 任务=%s | 状态=%s（已回写或未构造，避免重复评论）",
            state.task_no, state.write_back_status,
        )
        return state.write_back_content

    # -------------------- 接口路径二次白名单校验（防御性） --------------------
    api_path = state.write_back_content.api_path
    if not api_path:
        failed_info = state.write_back_content.model_copy(
            update={
                "status": WriteBackState.FAILED.value,
                "error": "回写接口路径为空，评论内容未构造",
            }
        )
        return failed_info

    ensure_comment_only(api_path)

    # -------------------- 请求体字段清洗（剔除审批动作字段） --------------------
    forbidden_body_keys = {
        "action", "approve", "reject", "transfer", "withdraw",
        "reassign", "sign", "complete", "terminate", "result", "decision",
    }
    raw_body = dict(state.write_back_content.comment_payload)
    stripped_keys = [key for key in raw_body.keys() if key.lower() in forbidden_body_keys]
    if stripped_keys:
        logger.error(
            "【红线拦截】回写请求体含审批动作字段，已剔除 | 任务=%s | 剔除字段=%s",
            state.task_no, stripped_keys,
        )
        for key in stripped_keys:
            raw_body.pop(key, None)

    request_body: Dict[str, Any] = {
        "contract_review_comment": raw_body.get("contract_review_comment", ""),
        "task_no": raw_body.get("task_no", state.task_no),
        "final_risk_level": raw_body.get("final_risk_level", state.risk_level or ""),
        "risk_count": raw_body.get("risk_count", 0),
    }

    # -------------------- 预览模式：仅记录不写入 --------------------
    if preview:
        preview_info = WriteBackInfo(
            status=WriteBackState.WRITING.value,
            comment_payload=state.write_back_content.comment_payload,
            comment_text=state.write_back_content.comment_text,
            api_path=api_path,
            response_body={"preview": True},
            error="预览模式：内容已生成，未实际写入审批系统",
            retry_count=state.write_back_retry_count,
        )
        logger.info(
            "回写预览模式（未实际写入） | 任务=%s | 接口=%s", state.task_no, api_path
        )
        return preview_info

    # -------------------- 正式模式：调用审批系统接口（含重试） --------------------
    return await _submit_comment(state, api_path, request_body, WriteBackInfo, WriteBackState)


async def _submit_comment(
    state: Any,
    api_path: str,
    request_body: Dict[str, Any],
    write_back_info_cls,
    write_back_state_cls,
) -> Any:
    """
    正式提交评论到审批系统（含重试）

    参数：
        state: 图状态
        api_path: 接口路径
        request_body: 请求体
        write_back_info_cls: WriteBackInfo 类
        write_back_state_cls: WriteBackState 类

    返回：
        WriteBackInfo
    """
    import httpx

    url = f"{settings.APPROVAL_BASE_URL.rstrip('/')}/{api_path.lstrip('/')}"
    headers = {
        "Authorization": f"Bearer {settings.APPROVAL_API_KEY}",
        "Content-Type": "application/json",
    }
    timeout = httpx.Timeout(
        timeout=settings.APPROVAL_TIMEOUT_SECONDS,
        connect=settings.APPROVAL_CONNECT_TIMEOUT_SECONDS,
    )

    last_error = ""
    max_retries = max(0, settings.APPROVAL_MAX_RETRIES)

    for attempt in range(1, max_retries + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(url, json=request_body, headers=headers)

            response_code: Optional[int] = None
            response_body: Dict[str, Any] = {}
            try:
                parsed_body = response.json()
                if isinstance(parsed_body, dict):
                    response_body = parsed_body
                raw_code = parsed_body.get("code")
                if isinstance(raw_code, int):
                    response_code = raw_code
            except ValueError:
                response_body = {"raw_text": response.text[:1000]}

            info = write_back_info_cls(
                status=write_back_state_cls.SUCCESS.value,
                comment_payload=state.write_back_content.comment_payload,
                comment_text=state.write_back_content.comment_text,
                api_path=api_path,
                response_body=response_body,
                response_code=response_code,
                http_status=response.status_code,
                error="",
                written_at=datetime.now(),
                retry_count=state.write_back_retry_count + attempt - 1,
            )

            if response.status_code >= 400:
                info.status = write_back_state_cls.FAILED.value
                info.error = f"审批系统返回 HTTP {response.status_code}：{response.text[:300]}"
                logger.error(
                    "回写失败 | 任务=%s | 状态码=%s | 响应=%s",
                    state.task_no, response.status_code, response.text[:300],
                )
                last_error = info.error
                if attempt < max_retries:
                    await asyncio.sleep(min(2.0 ** (attempt - 1), 8.0))
                    continue
                return info

            logger.info(
                "回写成功 | 任务=%s | 接口=%s | HTTP=%s | 业务码=%s",
                state.task_no, api_path, response.status_code, response_code,
            )
            return info

        except httpx.TimeoutException as exc:
            last_error = f"请求超时：{exc}"
            logger.error("回写超时 | 任务=%s | 接口=%s | 异常=%s", state.task_no, api_path, exc)
        except httpx.RequestError as exc:
            last_error = f"网络请求异常：{exc}"
            logger.error("回写网络异常 | 任务=%s | 接口=%s | 异常=%s", state.task_no, api_path, exc)

        if attempt < max_retries:
            backoff = min(2.0 ** (attempt - 1), 8.0)
            logger.warning("回写重试 | 任务=%s | 第 %d/%d 次 | %.1fs 后重试",
                           state.task_no, attempt, max_retries, backoff)
            await asyncio.sleep(backoff)

    # 重试耗尽
    failed_info = write_back_info_cls(
        status=write_back_state_cls.FAILED.value,
        comment_payload=state.write_back_content.comment_payload,
        comment_text=state.write_back_content.comment_text,
        api_path=api_path,
        error=last_error or "未知错误",
        retry_count=state.write_back_retry_count + max_retries,
    )
    logger.error("回写重试耗尽 | 任务=%s | 接口=%s | 最后异常=%s",
                 state.task_no, api_path, last_error)
    return failed_info
