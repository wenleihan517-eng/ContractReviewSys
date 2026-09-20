# D:/ContractReviewSys-合同审批智能审查项目/backend/app/schemas/audit.py
"""
审计日志接口契约模型

【目录职责】
    定义"任务全链路操作记录查询"接口的响应模型。
    审计日志是只读数据，接口只提供查询，不提供写入。
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class AuditLogItem(BaseModel):
    """
    审计日志条目

    字段：
        sequence: 全局单调递增序号
        task_id / task_no：关联任务
        node: 节点名
        action: 动作类型（node_enter/node_exit/node_retry 等）
        message: 说明
        detail: 详情
        elapsed_ms: 耗时（毫秒）
        created_at: 发生时间
    """

    sequence: int = Field(description="全局序号")
    task_id: str = Field(default="", description="任务 ID")
    task_no: str = Field(default="", description="任务编号")
    node: str = Field(default="", description="节点名")
    action: str = Field(default="", description="动作类型")
    message: str = Field(default="", description="说明")
    detail: Optional[dict] = Field(default=None, description="详情")
    elapsed_ms: Optional[float] = Field(default=None, description="耗时毫秒")
    created_at: Optional[datetime] = Field(default=None, description="发生时间")


class AuditQueryResponse(BaseModel):
    """
    审计日志查询响应（分页由通用 page_response 包装）
    """
