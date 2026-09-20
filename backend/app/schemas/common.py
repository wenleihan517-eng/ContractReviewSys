# D:/ContractReviewSys-合同审批智能审查项目/backend/app/schemas/common.py
"""
通用接口契约模型 — 分页参数与通用字段

【目录职责】
    本模块存放跨模块复用的接口契约模型，与各业务模块的请求/响应模型解耦。
    面向前端，只暴露必要字段，不泄露存储实现细节。

使用示例：
    from app.schemas.common import TaskNoPath, PaginationQuery
"""

from typing import Optional

from pydantic import BaseModel, Field

from app.constants import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE


class PaginationQuery(BaseModel):
    """
    分页查询参数

    字段：
        page: 页码，从 1 开始
        page_size: 每页条数，受上限约束
        keyword: 可选关键字，用于模糊搜索
    """

    page: int = Field(default=1, ge=1, description="页码，从 1 开始")
    page_size: int = Field(
        default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="每页条数"
    )
    keyword: Optional[str] = Field(default=None, description="关键字，可选")


class TaskNoPath(BaseModel):
    """
    路径参数：任务编号
    """

    task_no: str = Field(description="任务编号")


class TaskIdPath(BaseModel):
    """
    路径参数：任务 ID
    """

    task_id: str = Field(description="任务 ID")


class IdResponse(BaseModel):
    """
    通用 ID 响应（创建类接口返回）

    字段：
        id: 创建的资源 ID
    """

    id: str = Field(description="资源 ID")
