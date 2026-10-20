# D:/ContractReviewSys-合同审批智能审查项目/backend/app/api/v1/endpoints/audit.py
"""
审计日志接口

【接口清单】
    GET /audit/tasks/{task_id}/logs   任务全链路操作记录查询

【设计要点】
    审计日志是只读数据，接口只提供查询，不提供写入。
    日志按 sequence 升序返回，保证时间顺序。

【鉴权约定（2026-09-21 补全）】
    审计日志记录了任务的全链路操作轨迹（含操作人、时间、动作），
    属任务附属敏感数据。此前无归属校验，业务经办人枚举 task_no
    即可查看他人任务的操作轨迹。现补任务归属校验：
      · 任务提交人本人，或 task:view_all（法务 / 管理员）
      · 任务不存在时保持原有"空结果"语义，不额外报 404
        （审计查询是排查用途，不存在不等于错误）
"""

from fastapi import APIRouter, Depends, Query

from app.core.dependencies import (
    CurrentUser,
    assert_task_access,
    get_current_request_id,
    get_current_user,
)
from app.core.logging_config import get_logger
from app.core.response import ApiResponse, page_response

logger = get_logger(__name__)

router = APIRouter(prefix="/audit", tags=["审计日志"])


async def _guard_task_read(task_id: str, user: CurrentUser, action: str) -> None:
    """
    审计日志读取前的任务归属校验

    参数：
        task_id: 任务标识（_id 或 task_no）
        user:    当前登录用户
        action:  操作标识，用于日志与错误文案

    返回：
        无

    异常：
        ForbiddenException：任务存在但不属于当前用户且无 task:view_all 时抛出

    说明：
        任务不存在时**不报错**——保持审计查询"空结果"的既有语义，
        避免把"查不到"与"没权限"混为一谈（前者是排查场景的常见情况）。
    """
    from app.services import task_service

    task = await task_service.get_task_by_id(task_id)
    if task is None:
        return
    assert_task_access(user, task, action=action)


@router.get("/tasks/{task_id}/logs", summary="任务操作记录查询",
            description="查询指定任务的全链路操作审计记录（按序号升序）。")
async def task_audit_logs(
    task_id: str,
    page: int = Query(default=1, ge=1, description="页码"),
    page_size: int = Query(default=50, ge=1, le=200, description="每页条数"),
    user: CurrentUser = Depends(get_current_user),
) -> ApiResponse:
    """
    任务操作记录查询

    入参：
        task_id: 任务 ID（路径参数）
        page / page_size: 分页参数
        user: 当前登录用户（用于任务归属校验）
    出参：
        data: 分页结果（items 为审计日志条目列表）

    权限：
        任务提交人本人，或拥有 task:view_all 的法务/管理员。
    """
    from app.repositories import audit_repo

    await _guard_task_read(task_id, user, action="view_audit_logs")

    items, total = await audit_repo.list_logs(
        task_id=task_id, skip=(page - 1) * page_size, limit=page_size
    )

    return page_response(
        items=items, total=total, page=page, page_size=page_size,
        message="审计日志查询成功", request_id=get_current_request_id(),
    )


@router.get("/tasks/{task_id}/logs/all", summary="任务全量操作记录",
            description="查询指定任务的完整操作记录（不分页，供排查用）。")
async def task_audit_logs_all(
    task_id: str,
    user: CurrentUser = Depends(get_current_user),
) -> ApiResponse:
    """
    任务全量操作记录（不分页）

    入参：
        task_id: 任务 ID（路径参数）
        user: 当前登录用户（用于任务归属校验）
    出参：
        data: 审计日志列表

    权限：
        任务提交人本人，或拥有 task:view_all 的法务/管理员。
    """
    from app.repositories import audit_repo

    await _guard_task_read(task_id, user, action="view_audit_logs_all")

    items, _ = await audit_repo.list_logs(task_id=task_id, skip=0, limit=500)
    return page_response(
        items=items, total=len(items), page=1, page_size=len(items),
        message="审计日志查询成功", request_id=get_current_request_id(),
    )
