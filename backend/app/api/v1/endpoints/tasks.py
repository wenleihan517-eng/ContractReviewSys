# D:/ContractReviewSys-合同审批智能审查项目/backend/app/api/v1/endpoints/tasks.py
"""
任务管理接口

【接口清单】
    POST /tasks                      创建审查任务
    GET  /tasks                      任务列表（支持状态过滤 + 分页）
    GET  /tasks/approval-pending     拉取审批待办
    GET  /tasks/{task_no}            任务详情
    POST /tasks/{task_no}/retry      任务重试
    POST /tasks/{task_no}/resolve    处理阻塞任务（重跑 / 驳回）

【设计要点】
    - RESTful 语义：创建用 POST，查询用 GET，状态变更用 POST 子资源
    - 统一响应体 ApiResponse；分页用 page_response
"""

from typing import Optional

from fastapi import APIRouter, Depends, Query

from app.core.dependencies import (
    CurrentUser,
    get_current_request_id,
    get_current_user,
    get_current_user_optional,
)
from app.core.logging_config import get_logger
from app.core.response import ApiResponse, page_response, success_response
from app.schemas.task import (
    ApprovalPendingItem,
    BlockedTaskResolveRequest,
    BlockedTaskResolveResponse,
    TaskCreateRequest,
    TaskCreateResponse,
    TaskDetailResponse,
    TaskListItem,
    TaskRetryRequest,
    TaskRetryResponse,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/tasks", tags=["审查任务"])


@router.post("", summary="创建审查任务", description="提交合同创建审查任务，自动执行差异化路由决策。")
async def create_task(
    body: TaskCreateRequest,
    user: Optional[CurrentUser] = Depends(get_current_user_optional),
) -> ApiResponse:
    """
    创建审查任务

    入参：
        body: TaskCreateRequest（合同标题/编号/金额/类型/来源等）
        user: 当前登录用户（由认证依赖注入，用于写入任务归属人）
    出参：
        data: TaskCreateResponse（task_id / task_no）

    说明：
        必须把登录用户名写入 created_by，否则下游报告生成/下载、
        评论回写等接口的所有者校验会因比对不上而误判为越权（403）。
    """
    from app.services import task_service

    owner = user.username if user is not None else None
    task_id, task_no = await task_service.create_task(body, owner=owner)
    return success_response(
        data=TaskCreateResponse(task_id=task_id, task_no=task_no).model_dump(),
        message="审查任务创建成功",
        request_id=get_current_request_id(),
    )


@router.get("", summary="任务列表", description="分页查询任务列表，支持按状态过滤与关键字搜索。")
async def list_tasks(
    status: Optional[str] = Query(default=None, description="任务状态过滤"),
    page: int = Query(default=1, ge=1, description="页码"),
    page_size: int = Query(default=20, ge=1, le=200, description="每页条数"),
    keyword: Optional[str] = Query(default=None, description="关键字"),
) -> ApiResponse:
    """
    任务列表

    入参：
        status / page / page_size / keyword：查询参数
    出参：
        data: 分页结果（total / page / page_size / items）
    """
    from app.services import task_service

    items, total = await task_service.list_tasks(
        status=status, page=page, page_size=page_size, keyword=keyword
    )

    item_list = [
        TaskListItem(
            task_id=item.id or "",
            task_no=item.task_no,
            contract_title=getattr(item, "contract_title", "") or "",
            status=item.status,
            route_type=item.route_type,
            progress=item.progress,
            blocked_reason=item.blocked_reason,
            created_at=item.created_at,
        ).model_dump()
        for item in items
    ]

    return page_response(
        items=item_list, total=total, page=page, page_size=page_size,
        message="任务列表查询成功", request_id=get_current_request_id(),
    )


@router.get("/approval-pending", summary="拉取审批待办", description="查询已完成审查、待进入人工审批环节的任务。")
async def approval_pending(
    page: int = Query(default=1, ge=1, description="页码"),
    page_size: int = Query(default=20, ge=1, le=200, description="每页条数"),
) -> ApiResponse:
    """
    拉取审批待办

    出参：
        data: 分页结果（items 为 ApprovalPendingItem 列表）
    """
    from app.services import task_service

    items, total = await task_service.list_approval_pending(page=page, page_size=page_size)

    item_list = [
        ApprovalPendingItem(
            task_no=item.task_no,
            contract_title=getattr(item, "contract_title", "") or "",
            risk_level=None,
            conclusion_text="",
            write_back_status=item.write_back_status,
        ).model_dump()
        for item in items
    ]

    return page_response(
        items=item_list, total=total, page=page, page_size=page_size,
        message="审批待办查询成功", request_id=get_current_request_id(),
    )


@router.get("/{task_no}", summary="任务详情", description="查询单个任务的完整状态信息。")
async def get_task_detail(task_no: str) -> ApiResponse:
    """
    任务详情

    入参：
        task_no: 任务编号（路径参数）
    出参：
        data: TaskDetailResponse
    """
    from app.core.exceptions import BusinessException, ErrorCode
    from app.services import task_service

    task = await task_service.get_task_detail(task_no)
    if task is None:
        raise BusinessException(
            code=ErrorCode.NOT_FOUND, message=f"任务不存在：{task_no}", http_status=404
        )

    detail = TaskDetailResponse(
        task_id=task.id or "",
        task_no=task.task_no,
        contract_id=task.contract_id,
        contract_title=getattr(task, "contract_title", "") or "",
        contract_no=getattr(task, "contract_no", "") or "",
        status=task.status,
        route_type=task.route_type,
        progress=task.progress,
        current_node=task.current_node,
        blocked_reason=task.blocked_reason,
        blocked_detail=task.blocked_detail,
        retry_count=task.retry_count,
        max_retry=task.max_retry,
        write_back_status=task.write_back_status,
        is_blocked=task.is_blocked,
        created_at=task.created_at,
        updated_at=task.updated_at,
    )
    return success_response(data=detail.model_dump(), message="任务详情查询成功",
                            request_id=get_current_request_id())


@router.post("/{task_no}/retry", summary="任务重试", description="重置失败/阻塞任务，回到可执行状态。")
async def retry_task(task_no: str, body: TaskRetryRequest) -> ApiResponse:
    """
    任务重试

    入参：
        task_no: 任务编号（路径参数）
        body: TaskRetryRequest
    出参：
        data: TaskRetryResponse（resumed / message）
    """
    from app.services import task_service

    ok, message = await task_service.retry_task(task_no)
    return success_response(
        data=TaskRetryResponse(task_no=task_no, resumed=ok, message=message).model_dump(),
        message="任务重试受理" if ok else "任务重试失败",
        request_id=get_current_request_id(),
    )


@router.post("/{task_no}/resolve", summary="处理阻塞任务", description="对 blocked 任务执行重跑或驳回处理。")
async def resolve_blocked(task_no: str, body: BlockedTaskResolveRequest) -> ApiResponse:
    """
    处理阻塞任务

    入参：
        task_no: 任务编号（路径参数）
        body: BlockedTaskResolveRequest（action=retry/dismiss）
    出参：
        data: BlockedTaskResolveResponse
    """
    from app.services import task_service

    ok, message = await task_service.resolve_blocked_task(
        task_no=task_no, action=body.action, reason=body.reason
    )
    return success_response(
        data=BlockedTaskResolveResponse(
            task_no=task_no, action=body.action, message=message
        ).model_dump(),
        message="阻塞任务处理完成" if ok else "阻塞任务处理失败",
        request_id=get_current_request_id(),
    )
