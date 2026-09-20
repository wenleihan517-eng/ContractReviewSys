# D:/ContractReviewSys-合同审批智能审查项目/backend/app/api/v1/endpoints/notifications.py
"""
站内通知接口（消息中心）

【接口清单】
    GET  /notifications              分页查询当前用户可见通知
    GET  /notifications/unread-count 未读数量（消息中心小红点）
    POST /notifications/{notify_id}/read  标记单条已读
    POST /notifications/read-all     全部标记已读

【可见性规则】
    通知按 recipient_role + recipient_username 双条件过滤：
        - recipient_username 非空：仅该用户名可见（精确推送，如经办员本人）
        - recipient_username 为空：该角色全员可见（按角色广播）
    当前用户只能看到与自己角色匹配、且（若精确推送）用户名匹配的通知。
"""

from typing import Optional

from fastapi import APIRouter, Depends, Query

from app.constants import MongoCollection
from app.core.dependencies import CurrentUser, get_current_request_id, get_current_user
from app.core.exceptions import BusinessException, ErrorCode
from app.core.logging_config import get_logger
from app.core.response import ApiResponse, page_response, success_response
from app.storage.mongo_client import mongo_client

logger = get_logger(__name__)

router = APIRouter(prefix="/notifications", tags=["站内通知"])


def _visibility_filter(user: CurrentUser) -> dict:
    """
    构造当前用户的通知可见性过滤条件

    参数：
        user: 当前登录用户

    返回：
        MongoDB 过滤字典：角色匹配 + 精确接收人匹配
    """
    return {
        "recipient_role": user.role,
        "$or": [
            {"recipient_username": ""},
            {"recipient_username": user.username},
        ],
    }


@router.get("", summary="通知列表", description="分页查询当前用户可见的站内通知。")
async def list_notifications(
    user: CurrentUser = Depends(get_current_user),
    page: int = Query(default=1, ge=1, description="页码"),
    page_size: int = Query(default=20, ge=1, le=100, description="每页条数"),
    unread_only: bool = Query(default=False, description="仅未读"),
) -> ApiResponse:
    """
    通知列表
    """
    collection = mongo_client.get_collection(MongoCollection.NOTIFICATION)
    base_filter = _visibility_filter(user)
    if unread_only:
        base_filter["read"] = False

    total = await collection.count_documents(base_filter)
    cursor = (
        collection.find(base_filter)
        .sort("created_at", -1)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    items = []
    async for raw in cursor:
        items.append(
            {
                "notify_id": str(raw.get("_id", "")),
                "type": raw.get("type", ""),
                "level": raw.get("level", "info"),
                "title": raw.get("title", ""),
                "content": raw.get("content", ""),
                "task_no": raw.get("task_no", ""),
                "read": raw.get("read", False),
                "created_at": raw.get("created_at"),
            }
        )

    return page_response(
        items=items, total=total, page=page, page_size=page_size,
        message="通知列表查询成功", request_id=get_current_request_id(),
    )


@router.get("/unread-count", summary="未读数量", description="当前用户未读通知数量（消息中心小红点）。")
async def unread_count(user: CurrentUser = Depends(get_current_user)) -> ApiResponse:
    """未读数量"""
    collection = mongo_client.get_collection(MongoCollection.NOTIFICATION)
    base_filter = _visibility_filter(user)
    base_filter["read"] = False
    count = await collection.count_documents(base_filter)
    return success_response(
        data={"unread_count": count}, message="未读数量查询成功",
        request_id=get_current_request_id(),
    )


@router.post("/{notify_id}/read", summary="标记单条已读",
             description="点击通知跳转工作台后自动标记已读。")
async def mark_read(notify_id: str, user: CurrentUser = Depends(get_current_user)) -> ApiResponse:
    """标记单条已读"""
    from bson import ObjectId
    from datetime import datetime

    collection = mongo_client.get_collection(MongoCollection.NOTIFICATION)
    try:
        oid = ObjectId(notify_id)
    except Exception:  # noqa: BLE001 - 非法 id 视为不存在
        raise BusinessException(
            code=ErrorCode.NOT_FOUND, message=f"通知不存在：{notify_id}", http_status=404
        )

    result = await collection.update_one(
        {"_id": oid, **_visibility_filter(user)},
        {"$set": {"read": True, "read_at": datetime.now()}},
    )
    if result.matched_count == 0:
        raise BusinessException(
            code=ErrorCode.NOT_FOUND, message=f"通知不存在或无权访问：{notify_id}",
            http_status=404,
        )
    return success_response(
        data={"notify_id": notify_id}, message="已标记为已读",
        request_id=get_current_request_id(),
    )


@router.post("/read-all", summary="全部标记已读", description="一键清空未读红点。")
async def mark_all_read(user: CurrentUser = Depends(get_current_user)) -> ApiResponse:
    """全部标记已读"""
    from datetime import datetime

    collection = mongo_client.get_collection(MongoCollection.NOTIFICATION)
    base_filter = _visibility_filter(user)
    base_filter["read"] = False
    result = await collection.update_many(
        base_filter, {"$set": {"read": True, "read_at": datetime.now()}}
    )
    return success_response(
        data={"updated": result.modified_count}, message="全部标记已读",
        request_id=get_current_request_id(),
    )
