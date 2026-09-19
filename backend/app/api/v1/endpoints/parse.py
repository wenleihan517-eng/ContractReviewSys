# D:/ContractReviewSys-合同审批智能审查项目/backend/app/api/v1/endpoints/parse.py
"""
文档解析接口

【接口清单】
    POST /parse/upload                 本地上传合同文件（保存至 MinIO）
    POST /parse/{task_id}/trigger      触发文档解析
    GET  /parse/{task_id}/result       解析结果查询

【设计要点】
    - 文件上传用 multipart/form-data，保存至 MinIO 合同桶
    - 解析是异步任务的第一阶段，触发接口返回受理，结果异步查询
"""

from fastapi import APIRouter, File, UploadFile

from app.core.config import settings
from app.core.dependencies import get_current_request_id
from app.core.exceptions import BusinessException, ErrorCode, FileUploadException
from app.core.logging_config import get_logger
from app.core.response import ApiResponse, success_response
from app.schemas.parse import (
    ClauseItem,
    ParseResultResponse,
    ParseTriggerRequest,
    ParseTriggerResponse,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/parse", tags=["文档解析"])


@router.post("/upload", summary="本地上传文件", description="上传合同文件（PDF/DOCX/图片），保存至 MinIO 并返回对象键。")
async def upload_file(file: UploadFile = File(..., description="合同文件")) -> ApiResponse:
    """
    本地上传合同文件

    入参：
        file: 上传文件（PDF/DOCX/PNG/JPG 等）
    出参：
        data: {"object_key": 对象键, "file_name": 文件名, "file_size": 字节数}
    """
    from app.utils.file_hash import is_scanned_image_ext, normalize_ext

    # 读取文件字节
    content = await file.read()
    if not content:
        raise FileUploadException(message="上传文件内容为空")

    file_name = file.filename or "contract"
    ext = normalize_ext(file_name)

    # 白名单校验：仅允许 PDF / DOCX / 图像
    allowed_exts = {".pdf", ".docx", ".doc", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}
    if ext not in allowed_exts:
        raise FileUploadException(
            code=ErrorCode.FILE_TYPE_NOT_ALLOWED,
            message=f"不支持的文件类型：{ext}",
            detail=f"允许类型：{sorted(allowed_exts)}",
        )

    # 保存至 MinIO 合同桶
    from app.storage import minio_client
    from app.utils.id_generator import generate_object_key

    object_key = generate_object_key(prefix="contract", file_name=file_name)
    await minio_client.upload_bytes(
        bucket=settings.MINIO_BUCKET_CONTRACT,
        object_key=object_key,
        data=content,
        content_type=file.content_type or "application/octet-stream",
    )

    return success_response(
        data={
            "object_key": object_key,
            "file_name": file_name,
            "file_size": len(content),
            "file_ext": ext,
            "source_type": "scanned" if is_scanned_image_ext(ext) else "digital",
        },
        message="文件上传成功",
        request_id=get_current_request_id(),
    )


@router.post("/{task_id}/trigger", summary="触发解析", description="对指定任务触发文档解析（同步执行，返回解析统计）。")
async def trigger_parse(task_id: str, body: ParseTriggerRequest) -> ApiResponse:
    """
    触发文档解析

    入参：
        task_id: 任务 ID（路径参数）
        body: ParseTriggerRequest（object_key / source_type / file_name）
    出参：
        data: ParseTriggerResponse（accepted / message / 解析统计）
    """
    from app.services import orchestration_service

    # 兼容调用方把 object_key 传空的情况：编排层会回退读取任务主档中的缓存值
    outcome = await orchestration_service.trigger_parse(
        task_id=task_id,
        object_key=body.object_key,
        source_type=body.source_type or "digital",
        file_name=body.file_name or "",
    )

    return success_response(
        data=ParseTriggerResponse(
            task_id=task_id,
            accepted=outcome.accepted,
            message=outcome.message,
        ).model_dump(),
        message="解析完成" if outcome.accepted else "解析失败",
        request_id=get_current_request_id(),
    )


@router.get("/{task_id}/result", summary="解析结果查询", description="查询指定任务的解析结果（全文/文本块/条款/元数据）。")
async def parse_result(task_id: str) -> ApiResponse:
    """
    解析结果查询

    入参：
        task_id: 任务 ID（路径参数）
    出参：
        data: ParseResultResponse（全文/页数/文本块/条款/元数据）
    """
    from app.services import task_service, orchestration_service

    task = await task_service.get_task_by_id(task_id)
    if task is None:
        raise BusinessException(
            code=ErrorCode.NOT_FOUND, message=f"任务不存在：{task_id}", http_status=404
        )

    # 解析产物读取来源说明：
    #   1) 任务主档的 parse_snapshot（由触发解析时写入，持久化，随时可查）
    #   2) 若存在已结构化的条款，补充到 clauses 字段
    from app.constants import MongoCollection
    from app.storage.mongo_client import mongo_client

    snapshot = await orchestration_service.get_parse_result(task_id) or {}

    clauses_raw = []
    collection = mongo_client.get_collection(MongoCollection.CONTRACT_CLAUSE)
    cursor = collection.find({"contract_id": task.contract_id})
    async for raw in cursor:
        clauses_raw.append({
            "clause_id": str(raw.get("_id", "")),
            "clause_index": raw.get("clause_index", 0),
            "clause_type": raw.get("clause_type", "other"),
            "clause_title": raw.get("clause_title", ""),
            "content": raw.get("content", ""),
        })

    full_text = snapshot.get("full_text", "") or ""
    result = ParseResultResponse(
        task_id=task_id,
        parse_status=getattr(task, "parse_status", "") or task.status,
        full_text=full_text,
        page_count=snapshot.get("page_count", 0) or 0,
        total_chars=len(full_text),
        parse_engine="pymupdf" if (getattr(task, "source_type", "digital") == "digital") else "paddleocr",
        source_type=snapshot.get("source_type", "") or getattr(task, "source_type", "") or "",
        text_blocks=snapshot.get("text_blocks", []) or [],
        clauses=[ClauseItem(**c) for c in clauses_raw],
        metadata={},
    )
    return success_response(data=result.model_dump(), message="解析结果查询成功",
                            request_id=get_current_request_id())
