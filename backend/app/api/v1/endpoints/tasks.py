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

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, File, Query, UploadFile

from app.core.config import settings
from app.core.dependencies import (
    CurrentUser,
    get_current_request_id,
    get_current_user,
    get_current_user_optional,
)
from app.core.exceptions import BusinessException, ErrorCode, FileUploadException
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


@router.get("", summary="任务列表", description="分页查询任务列表，支持按状态/失败类型过滤与关键字搜索。业务经办人仅可见本人提交的任务。")
async def list_tasks(
    status: Optional[str] = Query(default=None, description="任务状态过滤"),
    failure_type: Optional[str] = Query(default=None, description="失败类型过滤（异常任务维度：ocr_timeout/file_format/file_corrupt/rule_engine_error 等）"),
    page: int = Query(default=1, ge=1, description="页码"),
    page_size: int = Query(default=20, ge=1, le=200, description="每页条数"),
    keyword: Optional[str] = Query(default=None, description="关键字"),
    user: CurrentUser = Depends(get_current_user),
) -> ApiResponse:
    """
    任务列表

    入参：
        status / failure_type / page / page_size / keyword：查询参数
        user: 当前登录用户
    出参：
        data: 分页结果（total / page / page_size / items）

    权限说明（2026-09-20 修复"三角色看到相同列表"）：
        业务经办人（business_user）按 PERM_TASK_VIEW_OWN 仅返回本人创建的任务；
        法务审查人与系统管理员可查看全部任务。
    """
    from app.services import task_service

    # 业务经办人强制按提交人过滤（服务端权威过滤，前端无法绕过）
    owner_username = user.username if user.role == "business_user" else None

    items, total = await task_service.list_tasks(
        status=status, page=page, page_size=page_size, keyword=keyword,
        failure_type=failure_type, owner_username=owner_username,
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
            # 失败分类字段：前端失败行标红悬浮、失败原因展示使用
            failure_type=getattr(item, "failure_type", None),
            failure_reason=getattr(item, "failure_reason", None),
            # 提交经办人：前端"重新上传"权限（仅提交人/管理员）判定使用
            created_by=getattr(item, "created_by", "") or "",
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


@router.get("/{task_no}", summary="任务详情", description="查询单个任务的完整状态信息。业务经办人仅可查看本人提交的任务。")
async def get_task_detail(
    task_no: str,
    user: CurrentUser = Depends(get_current_user),
) -> ApiResponse:
    """
    任务详情

    入参：
        task_no: 任务编号（路径参数）
        user: 当前登录用户
    出参：
        data: TaskDetailResponse

    权限说明（2026-09-20 修复）：
        业务经办人（business_user）仅可查看本人提交的任务（PERM_TASK_VIEW_OWN），
        查看他人任务返回 403；法务审查人与系统管理员可查看全部。
    """
    from app.core.exceptions import BusinessException, ErrorCode
    from app.services import task_service

    task = await task_service.get_task_detail(task_no)
    if task is None:
        raise BusinessException(
            code=ErrorCode.NOT_FOUND, message=f"任务不存在：{task_no}", http_status=404
        )

    # 业务经办人越权防护：只能查看本人提交的任务
    if user.role == "business_user" and (task.created_by or "") != user.username:
        raise BusinessException(
            code=ErrorCode.APPROVAL_PERMISSION_DENIED,
            message="无权查看该任务：业务经办人仅可查看本人提交的合同",
            http_status=403,
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
        failure_type=getattr(task, "failure_type", None),
        failure_reason=getattr(task, "failure_reason", None),
        created_by=getattr(task, "created_by", "") or "",
    )
    return success_response(data=detail.model_dump(), message="任务详情查询成功",
                            request_id=get_current_request_id())


@router.post("/{task_no}/retry", summary="任务重试", description="重置失败/阻塞任务，回到可执行状态。")
async def retry_task(
    task_no: str,
    body: TaskRetryRequest,
    user: Optional[CurrentUser] = Depends(get_current_user_optional),
) -> ApiResponse:
    """
    任务重试

    入参：
        task_no: 任务编号（路径参数）
        body: TaskRetryRequest
        user: 当前登录用户（用于操作留痕）
    出参：
        data: TaskRetryResponse（resumed / message）
    """
    from app.services import task_service

    ok, message = await task_service.retry_task(task_no)
    # 操作留痕：记录操作人 / 结果（2026-09-20 数据与流程闭环）
    if user is not None:
        await task_service.record_task_operation(
            task_no=task_no,
            operator=user.username,
            action="retry",
            result="accepted" if ok else "failed",
            detail=message,
        )
    return success_response(
        data=TaskRetryResponse(task_no=task_no, resumed=ok, message=message).model_dump(),
        message="任务重试受理" if ok else "任务重试失败",
        request_id=get_current_request_id(),
    )


@router.post("/{task_no}/replace-file", summary="重新上传替换合同文件",
             description="替换原合同文件后重新提交审查（仅提交经办人/管理员可用，审查员不可替换原文件）。")
async def replace_file(
    task_no: str,
    file: UploadFile = File(..., description="新的合同文件"),
    user: CurrentUser = Depends(get_current_user),
) -> ApiResponse:
    """
    重新上传替换合同文件（权责分离）

    权限控制：
        - 仅【合同提交经办人】或【管理员】可替换原文件；
        - 审查员（law_user）不可替换，防止私自篡改原合同材料。

    流程：
        1. 权限校验（管理员 或 任务提交人）
        2. 上传新文件到 MinIO（复用 parse 上传逻辑）
        3. 更新任务 object_key / file_name / source_type，清失败标记
        4. 操作留痕
        5. 重新触发解析（解析成功即自动续跑审查）
    """
    from app.services import task_service
    from app.utils.file_hash import is_scanned_image_ext, normalize_ext
    from app.utils.id_generator import generate_object_key
    from app.storage import minio_client
    from app.core.exceptions import FileUploadException, ErrorCode

    task = await task_service.get_task_by_id(task_no)
    if task is None:
        raise BusinessException(
            code=ErrorCode.NOT_FOUND, message=f"任务不存在：{task_no}", http_status=404
        )

    # ---------------- 权限校验：提交经办人 或 管理员 ----------------
    is_owner = task.created_by and task.created_by == user.username
    if user.role != "admin" and not is_owner:
        raise BusinessException(
            code=ErrorCode.APPROVAL_PERMISSION_DENIED,
            message="无权替换该合同文件：仅合同提交经办人或管理员可重新上传，审查员不可替换原文件",
            http_status=403,
        )

    # ---------------- 文件校验与上传 ----------------
    content = await file.read()
    if not content:
        raise FileUploadException(message="上传文件内容为空")
    file_name = file.filename or "contract"
    ext = normalize_ext(file_name)
    allowed = {".pdf", ".docx", ".doc", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}
    if ext not in allowed:
        raise FileUploadException(
            code=ErrorCode.FILE_TYPE_NOT_ALLOWED,
            message=f"不支持的文件类型：{ext}",
        )

    object_key = generate_object_key(prefix="contract", file_name=file_name)
    await minio_client.upload_bytes(
        bucket=settings.MINIO_BUCKET_CONTRACT,
        object_key=object_key,
        data=content,
        content_type=file.content_type or "application/octet-stream",
    )
    source_type = "scanned" if is_scanned_image_ext(ext) else "digital"

    # ---------------- 更新任务主档（替换文件引用 + 清失败标记） ----------------
    from app.repositories import task_repo

    await task_repo.update_task(
        str(task.id),
        {
            "object_key": object_key,
            "file_name": file_name,
            "source_type": source_type,
            "failure_type": None,
            "failure_reason": None,
            "parse_status": "not_started",
            "updated_at": datetime.now(),
        },
    )

    # ---------------- 操作留痕 ----------------
    await task_service.record_task_operation(
        task_no=task_no,
        operator=user.username,
        action="replace_file",
        result="accepted",
        detail=f"替换为 {file_name}",
    )

    # ---------------- 重新触发解析（解析成功自动续跑审查） ----------------
    from app.services import orchestration_service

    outcome = await orchestration_service.trigger_parse(
        task_id=str(task.id),
        object_key=object_key,
        source_type=source_type,
        file_name=file_name,
    )

    return success_response(
        data={
            "task_no": task_no,
            "object_key": object_key,
            "accepted": outcome.accepted,
            "message": outcome.message,
        },
        message="合同文件已替换并重新提交审查" if outcome.accepted else "文件替换成功，但重新解析未受理",
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
