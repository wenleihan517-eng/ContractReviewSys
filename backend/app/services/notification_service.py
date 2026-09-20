# D:/ContractReviewSys-合同审批智能审查项目/backend/app/services/notification_service.py
"""
消息通知服务（分级推送）

【模块定位】
    审查成功 / 审查失败 / 系统级异常事件的统一通知出口：
        1. 站内通知：按角色分级规则写入 cr_notification，前端消息中心轮询展示
        2. 邮件通道：按通知模板组装文本，通过 SMTP 发送到 QQ 邮箱（未配置则跳过并留痕）

【分级推送规则（与需求一一对应，勿改语义）】
    接收人员（QQ 邮箱）：经办员 2241071024@qq.com ｜ 管理员 2584076700@qq.com
    ｜ 审查员 1623084893@qq.com
    - 经办员：接收本人提交合同的【审查成功、审查失败】全部通知
    - 审查员：仅接收异常任务通知
    - 管理员：仅接收执行失败、系统级异常通知（普通合同审查完成不推送）
"""

import asyncio
from datetime import datetime
from typing import Optional

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
            email_push_status="pending",
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
# 三、邮件通道（QQ 邮箱）
# ======================================================================

async def _send_email(payload_text: str, to_emails: list) -> str:
    """
    通过 SMTP 发送通知邮件到 QQ 邮箱

    说明：
        使用 QQ 邮箱 SMTP（smtp.qq.com:465 SSL）。需在 .env 配置
        SMTP_USER（QQ 邮箱号）与 SMTP_PASSWORD（SMTP 授权码，非登录密码）。
        未配置时跳过（返回 skipped，不影响任何主流程）。

    参数：
        payload_text: 邮件正文（纯文本，通知模板拼装结果）
        to_emails: 收件邮箱列表

    返回：
        sent / skipped / failed
    """
    import smtplib
    from email.header import Header
    from email.mime.text import MIMEText
    from email.utils import formataddr

    smtp_user = (getattr(settings, "SMTP_USER", "") or "").strip()
    smtp_password = (getattr(settings, "SMTP_PASSWORD", "") or "").strip()
    if not smtp_user or not smtp_password:
        return "skipped"

    to_emails = [e for e in to_emails if e and "@" in e]
    if not to_emails:
        return "skipped"

    # 主题：从正文首行提取（【明契智审 - 异常提醒】等）
    first_line = payload_text.splitlines()[0].strip() if payload_text else ""
    subject = first_line.replace("【明契智审 - ", "").replace("】", "") or "明契智审通知"

    try:
        msg = MIMEText(payload_text, "plain", "utf-8")
        msg["Subject"] = Header(subject, "utf-8")
        msg["From"] = formataddr((str(Header("明契智审", "utf-8")), smtp_user))
        msg["To"] = ", ".join(to_emails)

        def _send() -> None:
            if getattr(settings, "SMTP_USE_SSL", True):
                server = smtplib.SMTP_SSL(settings.SMTP_HOST, settings.SMTP_PORT, timeout=15)
            else:
                server = smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=15)
            try:
                server.login(smtp_user, smtp_password)
                server.sendmail(smtp_user, to_emails, msg.as_string())
            finally:
                server.quit()

        # 邮件发送为阻塞 IO，移出事件循环
        await asyncio.to_thread(_send)
        return "sent"
    except Exception as exc:  # noqa: BLE001 - 发送失败只记录，不影响主流程
        logger.error("邮件推送失败 | 收件人=%s | 异常=%s", to_emails, exc)
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
    status = await _send_email(
        text,
        [settings.NOTIFY_ADMIN_EMAIL, settings.NOTIFY_REVIEWER_EMAIL, settings.NOTIFY_OPERATOR_EMAIL],
    )
    logger.info("任务失败通知完成 | 任务=%s | 邮件=%s", task_no, status)


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
        - 管理员 / 审查员：普通完成【不推送】（需求明确）
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

    # 邮件：仅经办员（管理员/审查员按需求不推送普通完成）
    status = await _send_email(text, [settings.NOTIFY_OPERATOR_EMAIL])
    logger.info("任务完成通知完成 | 任务=%s | 邮件=%s", task_no, status)


async def notify_system_alert(message: str, *, critical: bool = True) -> None:
    """
    系统级异常通知（如批量 OCR 失败、服务故障）

    分级规则落地点：管理员
    """
    text = f"【明契智审 - 系统告警】\n{message}\n请管理员尽快登录系统排查。"
    await _insert_notification(
        type_value=NotificationType.SYSTEM_ALERT.value,
        level=NotificationLevel.CRITICAL.value if critical else NotificationLevel.INFO.value,
        title="系统告警", content=text, task_no="",
        recipient_role="admin",
    )
    targets = [settings.NOTIFY_ADMIN_EMAIL]
    status = await _send_email(text, targets)
    logger.info("系统告警通知完成 | 邮件=%s | 内容=%s", status, message[:100])
