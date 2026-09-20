# D:/ContractReviewSys-合同审批智能审查项目/backend/app/api/v1/endpoints/audit.py
"""
审计日志接口

【接口清单】
    GET /audit/tasks/{task_id}/logs   任务全链路操作记录查询

【设计要点】
    审计日志是只读数据，接口只提供查询，不提供写入。
    日志按 sequence 升序返回，保证时间顺序。
"""

from fastapi import APIRouter, Query

from app.core.dependencies import get_current_request_id
from app.core.logging_config import get_logger
from app.core.response import ApiResponse, page_response

logger = get_logger(__name__)

router = APIRouter(prefix="/audit", tags=["审计日志"])


@router.get("/tasks/{task_id}/logs", summary="任务操作记录查询",
            description="查询指定任务的全链路操作审计记录（按序号升序）。")
async def task_audit_logs(
    task_id: str,
    page: int = Query(default=1, ge=1, description="页码"),
    page_size: int = Query(default=50, ge=1, le=200, description="每页条数"),
) -> ApiResponse:
    """
    任务操作记录查询

    入参：
        task_id: 任务 ID（路径参数）
        page / page_size: 分页参数
    出参：
        data: 分页结果（items 为审计日志条目列表）
    """
    from app.repositories import audit_repo

    items, total = await audit_repo.list_logs(
        task_id=task_id, skip=(page - 1) * page_size, limit=page_size
    )

    return page_response(
        items=items, total=total, page=page, page_size=page_size,
        message="审计日志查询成功", request_id=get_current_request_id(),
    )


@router.get("/tasks/{task_id}/logs/all", summary="任务全量操作记录",
            description="查询指定任务的完整操作记录（不分页，供排查用）。")
async def task_audit_logs_all(task_id: str) -> ApiResponse:
    """
    任务全量操作记录（不分页）

    入参：
        task_id: 任务 ID（路径参数）
    出参：
        data: 审计日志列表
    """
    from app.repositories import audit_repo

    items, _ = await audit_repo.list_logs(task_id=task_id, skip=0, limit=500)
    return page_response(
        items=items, total=len(items), page=1, page_size=len(items),
        message="审计日志查询成功", request_id=get_current_request_id(),
    )
