# D:/ContractReviewSys-合同审批智能审查项目/backend/app/repositories/audit_repo.py
"""
审计日志数据访问层 — 对应 MongoDB 集合 cr_audit_log

【模块定位】
    本模块封装任务全链路操作审计日志的持久化与查询，是"审计日志接口"
    的数据访问支撑。审计日志是只读数据，写入由流程节点触发。

返回值说明：
    - append_log()：追加一条审计日志
    - list_logs()：分页查询任务审计日志
"""

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from app.constants import MongoCollection
from app.core.logging_config import get_logger

logger = get_logger(__name__)


def _collection():
    """获取审计日志集合（延迟获取）"""
    from app.storage.mongo_client import mongo_client

    return mongo_client.get_collection(MongoCollection.AUDIT_LOG)


async def append_log(entry: Dict[str, Any]) -> bool:
    """
    追加一条审计日志

    参数：
        entry: 审计日志字典（含 task_id/task_no/node/action/message 等）

    返回：
        True 表示写入成功
    """
    collection = _collection()
    entry.setdefault("created_at", datetime.now())

    try:
        await collection.insert_one(entry)
        return True
    except Exception as exc:  # noqa: BLE001 - 审计写入失败仅告警，不阻断主流程
        logger.error("审计日志写入失败 | 异常=%s", exc)
        return False


async def list_logs(
    task_id: Optional[str] = None,
    task_no: Optional[str] = None,
    skip: int = 0,
    limit: int = 50,
) -> Tuple[List[Dict[str, Any]], int]:
    """
    分页查询任务审计日志

    参数：
        task_id: 可选，按任务 ID 过滤
        task_no: 可选，按任务编号过滤
        skip: 跳过条数
        limit: 返回条数

    返回：
        (日志列表, 总条数) 元组
    """
    collection = _collection()
    filter_dict: Dict[str, Any] = {}
    if task_id:
        filter_dict["task_id"] = task_id
    if task_no:
        filter_dict["task_no"] = task_no

    total = await collection.count_documents(filter_dict)
    cursor = (
        collection.find(filter_dict)
        .sort("sequence", 1)
        .skip(skip)
        .limit(limit)
    )

    items: List[Dict[str, Any]] = []
    async for raw in cursor:
        data = dict(raw)
        if "_id" in data:
            data["id"] = str(data.pop("_id"))
        items.append(data)

    return (items, total)
