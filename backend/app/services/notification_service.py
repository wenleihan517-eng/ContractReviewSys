# D:/ContractReviewSys-合同审批智能审查项目/backend/app/services/notification_service.py
"""
消息通知服务（分级推送）

【模块定位】
    审查成功 / 审查失败 / 系统级异常事件的统一通知出口：
        1. 站内通知：按角色分级规则写入 cr_notification，前端消息中心轮询展示
        2. QQ 通道：按通知模板组装文本，POST 到配置的 Webhook（未配置则跳过并留痕）

【分级推送规则（与需求一一对应，勿改语义）】
    接收人员（QQ）：经办员 2241071024 ｜ 管理员 2584076700 ｜ 审查员 1623084893 ｜ 工作群 1070152604
    - 经办员：接收本人提交合同的【审查成功、审查失败】全部通知
    - 审查员：仅接收异常任务通知
    - 管理员：仅接收执行失败、系统级异常通知（普通合同审查完成不推送）
    - 工作群：仅推送大批量 OCR 失败、系统故障等高优先级异常
"""

import asyncio
from datetime import datetime
from typing import Optional

import httpx

from app.constants import MongoCollection, NotificationLevel, NotificationType
from app.core.config import settings
from app.core.logging_config import get_logger

logger = get_logger(__name__)


# ======================================================================
# 一、QQ 推送模板
# ======================================================================

def _build_failed_text(task_no: str, reason: str, advice: str) -> str:
    """失败通知模板（与需求给出的模板逐行对齐）"""
    return (
        "【明契智审 - 异常提醒】\n"
        f"合同编号：{task_no}\n"
        "异常状态：执行失败\n"
        f"失败原因：{reason}\n"
        f"处理建议：{advice}\n"
        "系统处理入口：明契智审-合同审查大盘\n"
        "请及时登录系统处理该任务。"
    )


def _build_completed_text(task_no: str, risk_level: str, risk_count: int) -> str:
    """成功通知模板"""
    return (
        "【明契智审 - 审查完成提醒】\n"
        f"合同编号：{task_no}\n"
        "审查状态：已完成\n"
        f"综合风险等级：{risk_level}，共命中{risk_count}项风险点\n"
        "系统处理入口：明契智审-智能审查工作台\n"
        "请登录系统查看AI审查报告。"
    )


# ======================================================================
# 二、站内通知写入
# ======================================================================

async def _insert_notification(
    *,
    type_value: str,
    level: str,
    title: str,
    content: str,
    task_no: str,
    recipient_role: str,
    recipient_username: str = "",
) -> None:
    """写一条站内通知（异常吞掉，通知失败不影响主流程）"""
    from app.models.documents import Notification
    from app.storage.mongo_client import mongo_client

    try:
        doc = Notification(
            type=type_value,
            level=level,
            title=title,
            content=content,
            task_no=task_no,
            recipient_role=recipient_role,
            recipient_username=recipient_username,
            qq_push_status="pending",
        )
        collection = mongo_client.get_collection(MongoCollection.NOTIFICATION)
        await collection.insert_one(doc.model_dump())
        logger.info(
            "站内通知已写入 | 类型=%s | 级别=%s | 接收角色=%s | 接收人=%s | 任务=%s",
            type_value, level, recipient_role, recipient_username or "(全员)", task_no,
        )
    except Exception as exc:  # noqa: BLE001 - 通知是旁路能力，绝不阻断审查主流程
        logger.error("站内通知写入失败 | 任务=%s | 异常=%s", task_no, exc)


# ======================================================================
# 三、QQ 通道
# ======================================================================

async def _push_qq(payload_text: str, qq_targets: list) -> str:
    """
    推送 QQ 通知

    说明：
        走配置的 Webhook（POST JSON：{"qq": 目标, "text": 文本}）。
        未配置 QQ_PUSH_WEBHOOK_URL 时跳过（返回 skipped，不影响任何主流程）。

    返回：
        sent / skipped / failed
    """
    webhook = (getattr(settings, "QQ_PUSH_WEBHOOK_URL", "") or "").strip()
    if not webhook:
        return "skipped"

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0),
                                     trust_env=False) as client:
            for qq in qq_targets:
                await client.post(
                    webhook,
                    json={"qq": qq, "text": payload_text},
                )
        return "sent"
    except Exception as exc:  # noqa: BLE001 - 推送失败只记录，不影响主流程
        logger.error("QQ 推送失败 | 目标=%s | 异常=%s", qq_targets, exc)
        return "failed"


# ======================================================================
# 四、对外入口：任务完成 / 任务失败
# ======================================================================

async def notify_task_failed(
    task_no: str,
    failure_type: str,
    reason: str,
    advice: str,
    operator_username: str = "",
) -> None:
    """
    任务执行失败通知（站内 + QQ）

    分级规则落地点：
        - 经办员（本人）：失败通知全收
        - 审查员：异常通知收
        - 管理员：失败通知收
        - 工作群：失败属于异常事件，推送（大批量判定交由系统告警，见 notify_system_alert）
    """
    text = _build_failed_text(task_no, reason, advice)
    title = "审查失败提醒"

    # 站内：按角色各写一条（经办员精确到本人）
    await _insert_notification(
        type_value=NotificationType.TASK_FAILED.value,
        level=NotificationLevel.CRITICAL.value,
        title=title, content=text, task_no=task_no,
        recipient_role="business_user", recipient_username=operator_username,
    )
    await _insert_notification(
        type_value=NotificationType.TASK_FAILED.value,
        level=NotificationLevel.CRITICAL.value,
        title=title, content=text, task_no=task_no,
        recipient_role="law_user",
    )
    await _insert_notification(
        type_value=NotificationType.TASK_FAILED.value,
        level=NotificationLevel.CRITICAL.value,
        title=title, content=text, task_no=task_no,
        recipient_role="admin",
    )

    # QQ：管理员 + 审查员 + 经办员
    status = await _push_qq(
        text,
        [settings.QQ_NOTIFY_ADMIN, settings.QQ_NOTIFY_REVIEWER, settings.QQ_NOTIFY_OPERATOR],
    )
    logger.info("任务失败通知完成 | 任务=%s | QQ=%s", task_no, status)


async def notify_task_completed(
    task_no: str,
    risk_level: str,
    risk_count: int,
    operator_username: str = "",
) -> None:
    """
    任务审查完成通知

    分级规则落地点：
        - 经办员（本人）：完成通知收
        - 管理员 / 审查员 / 工作群：普通完成【不推送】（需求明确）
    """
    text = _build_completed_text(task_no, risk_level, risk_count)
    title = "审查完成提醒"

    # 站内：仅经办员本人
    await _insert_notification(
        type_value=NotificationType.TASK_COMPLETED.value,
        level=NotificationLevel.INFO.value,
        title=title, content=text, task_no=task_no,
        recipient_role="business_user", recipient_username=operator_username,
    )

    # QQ：仅经办员（管理员/审查员/工作群按需求不推送普通完成）
    status = await _push_qq(text, [settings.QQ_NOTIFY_OPERATOR])
    logger.info("任务完成通知完成 | 任务=%s | QQ=%s", task_no, status)


async def notify_system_alert(message: str, *, critical: bool = True) -> None:
    """
    系统级异常通知（如批量 OCR 失败、服务故障）

    分级规则落地点：管理员 + 工作群（高优先级异常才推群）
    """
    text = f"【明契智审 - 系统告警】\n{message}\n请管理员尽快登录系统排查。"
    await _insert_notification(
        type_value=NotificationType.SYSTEM_ALERT.value,
        level=NotificationLevel.CRITICAL.value if critical else NotificationLevel.INFO.value,
        title="系统告警", content=text, task_no="",
        recipient_role="admin",
    )
    targets = [settings.QQ_NOTIFY_ADMIN]
    if critical:
        targets.append(settings.QQ_NOTIFY_GROUP)
    status = await _push_qq(text, targets)
    logger.info("系统告警通知完成 | QQ=%s | 内容=%s", status, message[:100])
