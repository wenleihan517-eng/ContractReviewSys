# D:/ContractReviewSys-合同审批智能审查项目/backend/app/repositories/task_repo.py
"""
审查任务数据访问层 — 对应 MongoDB 集合 cr_review_task

【模块定位】
    本模块封装审查任务的持久化读写，是"任务管理接口"与"任务状态机"的
    数据访问支撑。职责单一：只做 CRUD，不含业务逻辑（业务编排在
    app.services.task_service）。

【设计要点】
    - 使用 motor 异步驱动，所有方法为 async
    - 返回 Pydantic 模型（ReviewTask）而非裸 dict，保证类型安全
    - 唯一性由 task_no 保证（唯一索引在 storage 层建立）

返回值说明：
    - create_task()：返回插入后的 task_id
    - find_by_task_no()：返回 ReviewTask 或 None
    - list_tasks()：返回 (items, total)
"""

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from app.constants import MongoCollection
from app.core.logging_config import get_logger
from app.models.documents.review_task import ReviewTask

logger = get_logger(__name__)


def _collection():
    """获取任务集合（延迟获取，避免模块加载时触发存储初始化）"""
    from app.storage.mongo_client import mongo_client

    return mongo_client.get_collection(MongoCollection.REVIEW_TASK)


async def create_task(task: ReviewTask) -> str:
    """
    创建审查任务

    参数：
        task: ReviewTask 实例（含 task_no / contract_id / thread_id / work_dir 等）

    返回：
        插入后的文档 _id（字符串）

    异常：
        MongoDBException：写入失败
    """
    from app.core.exceptions import MongoDBException

    collection = _collection()
    payload = task.model_dump(mode="json")
    # 移除 id 字段，由 MongoDB 自动生成 _id
    payload.pop("id", None)

    try:
        result = await collection.insert_one(payload)
        task_id = str(result.inserted_id)
        logger.info("审查任务已创建 | task_no=%s | task_id=%s", task.task_no, task_id)
        return task_id
    except Exception as exc:  # noqa: BLE001 - 统一转业务异常
        logger.error("审查任务创建失败 | task_no=%s | 异常=%s", task.task_no, exc)
        raise MongoDBException(message="审查任务创建失败", detail=str(exc)) from exc


async def find_by_task_no(task_no: str) -> Optional[ReviewTask]:
    """
    按任务编号查询任务

    参数：
        task_no: 任务编号

    返回：
        ReviewTask 实例；不存在返回 None
    """
    collection = _collection()
    raw = await collection.find_one({"task_no": task_no})
    if raw is None:
        return None
    return _to_model(raw)


async def find_by_task_id(task_id: str) -> Optional[ReviewTask]:
    """
    按任务 ID 查询任务

    参数：
        task_id: 任务 ID（MongoDB _id）

    返回：
        ReviewTask 实例；不存在返回 None
    """
    from bson import ObjectId

    collection = _collection()
    try:
        raw = await collection.find_one({"_id": ObjectId(task_id)})
    except Exception:  # noqa: BLE001 - 非法 ObjectId 视为不存在
        return None
    if raw is None:
        return None
    return _to_model(raw)


async def list_tasks(
    filter_dict: Optional[Dict[str, Any]] = None,
    skip: int = 0,
    limit: int = 20,
    sort_field: str = "created_at",
    sort_order: int = -1,
) -> Tuple[List[ReviewTask], int]:
    """
    分页查询任务列表

    参数：
        filter_dict: 过滤条件（如 {"status": "blocked"}）
        skip: 跳过条数
        limit: 返回条数
        sort_field: 排序字段
        sort_order: 排序方向（-1 降序 / 1 升序）

    返回：
        (任务列表, 总条数) 元组
    """
    collection = _collection()
    filter_dict = filter_dict or {}

    total = await collection.count_documents(filter_dict)
    cursor = collection.find(filter_dict).sort(sort_field, sort_order).skip(skip).limit(limit)

    items: List[ReviewTask] = []
    async for raw in cursor:
        items.append(_to_model(raw))

    return (items, total)


async def update_task(task_id: str, updates: Dict[str, Any]) -> bool:
    """
    更新任务字段

    参数：
        task_id: 任务 ID
        updates: 待更新的字段字典（含 $set 或直接字段）

    返回：
        True 表示更新成功
    """
    from bson import ObjectId

    collection = _collection()
    updates["updated_at"] = datetime.now()

    try:
        result = await collection.update_one(
            {"_id": ObjectId(task_id)}, {"$set": updates}
        )
        return result.modified_count > 0
    except Exception as exc:  # noqa: BLE001
        logger.error("审查任务更新失败 | task_id=%s | 异常=%s", task_id, exc)
        return False


async def update_task_by_no(task_no: str, updates: Dict[str, Any]) -> bool:
    """
    按任务编号更新任务字段

    参数：
        task_no: 任务编号
        updates: 待更新的字段字典

    返回：
        True 表示更新成功
    """
    collection = _collection()
    updates["updated_at"] = datetime.now()

    try:
        result = await collection.update_one({"task_no": task_no}, {"$set": updates})
        return result.modified_count > 0
    except Exception as exc:  # noqa: BLE001
        logger.error("审查任务更新失败 | task_no=%s | 异常=%s", task_no, exc)
        return False


def _to_model(raw: Dict[str, Any]) -> ReviewTask:
    """
    将 MongoDB 文档转换为 ReviewTask 模型

    参数：
        raw: MongoDB 文档

    返回：
        ReviewTask 实例
    """
    data = dict(raw)
    if "_id" in data:
        data["id"] = str(data.pop("_id"))
    return ReviewTask(**data)
