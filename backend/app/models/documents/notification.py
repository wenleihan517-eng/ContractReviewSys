# D:/ContractReviewSys-合同审批智能审查项目/backend/app/models/documents/notification.py
"""
站内通知模型

【模块定位】
    消息中心的持久化载体。审查成功 / 审查失败 / 系统级异常事件
    由 notification_service 按角色分级规则写入本集合，
    前端消息中心轮询未读数并拉取列表。

【分级推送规则（与需求一致）】
    - 经办员（business_user）：接收本人提交合同的【审查成功、审查失败】全部通知
    - 审查员（law_user）：仅接收异常任务通知
    - 管理员（admin）：仅接收执行失败、系统级异常通知（普通完成不推送）
    - 工作群（QQ 通道）：仅推送大批量 OCR 失败、系统故障等高优先级异常
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class Notification(BaseModel):
    """站内通知主档（与 MongoDB cr_notification 集合一一对应）"""

    # ---------------------- 通知内容 ----------------------
    type: str = Field(
        description="通知类型（NotificationType：task_completed/task_failed/system_alert）"
    )
    level: str = Field(
        default="info",
        description="通知级别（NotificationLevel：info/critical），critical 前端标红",
    )
    title: str = Field(description="通知标题（如：审查完成提醒 / 异常提醒）")
    content: str = Field(default="", description="通知正文（含失败原因与处理建议）")

    # ---------------------- 关联任务 ----------------------
    task_no: str = Field(default="", description="关联任务编号（点击跳转工作台）")

    # ---------------------- 接收人 ----------------------
    recipient_role: str = Field(
        description="接收角色（business_user/law_user/admin）"
    )
    recipient_username: str = Field(
        default="",
        description="精确接收人用户名；为空表示该角色全员可见（按角色广播）",
    )

    # ---------------------- 已读状态 ----------------------
    read: bool = Field(default=False, description="是否已读（跳转工作台后自动置真）")
    read_at: Optional[datetime] = Field(default=None, description="已读时间")

    # ---------------------- QQ 通道状态 ----------------------
    qq_push_status: str = Field(
        default="pending",
        description="QQ 推送状态 pending/sent/skipped；未配置 webhook 时为 skipped",
    )

    created_at: datetime = Field(default_factory=datetime.now, description="创建时间")
