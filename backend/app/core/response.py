# D:/ContractReviewSys-合同审批智能审查项目/backend/app/core/response.py
"""
统一响应封装 — 所有接口返回结构标准化

本模块职责：
    1. 定义前端约定的统一响应体结构
    2. 提供成功 / 失败响应的快捷构造函数
    3. 提供分页响应专用结构，供列表类接口复用

统一响应结构：
    {
        "code": 0,                # 0 表示成功，非 0 为错误码
        "message": "操作成功",     # 面向用户的提示
        "data": {...},            # 业务数据，失败时为 null
        "request_id": "xxx"       # 请求链路 ID
    }

分页数据结构（置于 data 内）：
    {
        "total": 100,             # 总条数
        "page": 1,                # 当前页码（从 1 开始）
        "page_size": 20,          # 每页条数
        "items": [...]            # 当前页数据列表
    }

返回值说明：
    - success_response()：返回 ApiResponse 实例
    - error_response()：返回 ApiResponse 实例
    - PageResult：泛型分页容器，可被 success_response 包装

使用示例：
    from app.core.response import success_response, PageResult
    return success_response(data={"id": "xxx"}, message="创建成功")
"""

from typing import Any, Generic, List, Optional, TypeVar

from pydantic import BaseModel, Field

from app.core.exceptions import ErrorCode

# 泛型变量，用于分页容器承载任意数据类型
T = TypeVar("T")


class ApiResponse(BaseModel):
    """
    统一响应体

    字段：
        code: 业务错误码，0 表示成功
        message: 提示信息
        data: 业务数据，失败时为 None
        request_id: 请求链路 ID，便于问题定位
    """

    code: int = Field(default=ErrorCode.SUCCESS, description="业务错误码，0 表示成功")
    message: str = Field(default="操作成功", description="提示信息")
    data: Optional[Any] = Field(default=None, description="业务数据，失败时为 null")
    request_id: Optional[str] = Field(default=None, description="请求链路 ID")


class PageResult(BaseModel, Generic[T]):
    """
    分页结果容器

    字段：
        total: 满足条件的总记录数
        page: 当前页码，从 1 开始
        page_size: 每页条数
        items: 当前页数据列表
        total_pages: 总页数，由 total 与 page_size 计算得出
    """

    total: int = Field(default=0, description="总记录数")
    page: int = Field(default=1, description="当前页码，从 1 开始")
    page_size: int = Field(default=20, description="每页条数")
    items: List[T] = Field(default_factory=list, description="当前页数据列表")

    @property
    def total_pages(self) -> int:
        """
        计算总页数

        返回：
            总页数。当 page_size 非法（<=0）或 total 为 0 时返回 0
        """
        if self.page_size <= 0 or self.total <= 0:
            return 0
        # 向上取整：(total + page_size - 1) // page_size
        return (self.total + self.page_size - 1) // self.page_size


def success_response(
    data: Optional[Any] = None,
    message: str = "操作成功",
    request_id: Optional[str] = None,
) -> ApiResponse:
    """
    构造成功响应

    参数：
        data: 业务数据，可为 None（如删除操作）
        message: 成功提示语
        request_id: 请求链路 ID

    返回：
        ApiResponse，code 固定为 0
    """
    return ApiResponse(
        code=ErrorCode.SUCCESS,
        message=message,
        data=data,
        request_id=request_id,
    )


def error_response(
    code: int = ErrorCode.UNKNOWN_ERROR,
    message: str = "操作失败",
    data: Optional[Any] = None,
    request_id: Optional[str] = None,
) -> ApiResponse:
    """
    构造失败响应

    参数：
        code: 业务错误码，必须非 0
        message: 失败提示语
        data: 附加数据（如字段级错误明细），通常为 None
        request_id: 请求链路 ID

    返回：
        ApiResponse，code 为非 0 错误码
    """
    return ApiResponse(
        code=code,
        message=message,
        data=data,
        request_id=request_id,
    )


def page_response(
    items: List[Any],
    total: int,
    page: int,
    page_size: int,
    message: str = "查询成功",
    request_id: Optional[str] = None,
) -> ApiResponse:
    """
    构造分页成功响应

    参数：
        items: 当前页数据列表
        total: 总记录数
        page: 当前页码
        page_size: 每页条数
        message: 提示语
        request_id: 请求链路 ID

    返回：
        ApiResponse，data 为 PageResult 的字典形式
    """
    page_result = PageResult[Any](
        total=total,
        page=page,
        page_size=page_size,
        items=items,
    )
    payload = page_result.model_dump()
    # 手动补充计算属性，便于前端直接使用
    payload["total_pages"] = page_result.total_pages

    return ApiResponse(
        code=ErrorCode.SUCCESS,
        message=message,
        data=payload,
        request_id=request_id,
    )
