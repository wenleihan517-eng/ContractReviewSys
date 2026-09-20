# D:/ContractReviewSys-合同审批智能审查项目/backend/app/repositories/rule_template_repo.py
"""
规则模板数据访问层 — 对应 MongoDB 集合 cr_rule_template

【模块定位】
    本模块封装规则模板的持久化读写，是"规则管理接口"与规则引擎
    `_load_active_rule_templates` 契约点的数据访问支撑。

【关键契约】
    list_active_templates(contract_type) —— 规则引擎节点 `_load_active_rule_templates`
    依赖此函数签名：入参 contract_type，出参 List[RuleTemplate]（模型实例）。
    本模块补齐了之前规则引擎模块的"待交付依赖"。

返回值说明：
    - list_active_templates()：返回启用规则列表（模型实例）
    - upsert_rule()：创建或更新规则，返回是否成功
    - set_rule_enabled()：启用/禁用规则
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from app.constants import MongoCollection
from app.core.logging_config import get_logger
from app.models.documents.rule_template import RuleTemplate

logger = get_logger(__name__)


def _collection():
    """获取规则模板集合（延迟获取）"""
    from app.storage.mongo_client import mongo_client

    return mongo_client.get_collection(MongoCollection.RULE_TEMPLATE)


async def list_active_templates(contract_type: str) -> List[RuleTemplate]:
    """
    查询适用于指定合同类型的启用规则模板（按优先级降序）

    【契约（规则引擎节点依赖）】
        入参：contract_type（合同类型）
        出参：RuleTemplate 模型实例列表（启用 + 合同类型匹配 + 优先级降序）

    参数：
        contract_type: 合同类型（purchase/sales/service/labor 等）

    返回：
        RuleTemplate 列表
    """
    collection = _collection()
    cursor = collection.find({"status": "enabled"}).sort("priority", -1)

    templates: List[RuleTemplate] = []
    async for raw in cursor:
        template = _to_model(raw)
        if template.matches_contract_type(contract_type):
            templates.append(template)

    logger.info(
        "启用规则模板查询完成 | 合同类型=%s | 模板数=%d",
        contract_type, len(templates),
    )
    return templates


async def list_rules(
    contract_type: Optional[str] = None,
    skip: int = 0,
    limit: int = 20,
) -> tuple:
    """
    分页查询规则模板（含停用规则）

    参数：
        contract_type: 可选合同类型过滤
        skip: 跳过条数
        limit: 返回条数

    返回：
        (规则列表, 总条数)
    """
    collection = _collection()
    filter_dict: Dict[str, Any] = {}
    if contract_type:
        filter_dict["contract_types"] = {"$in": [contract_type, "all"]}

    total = await collection.count_documents(filter_dict)
    cursor = collection.find(filter_dict).sort("priority", -1).skip(skip).limit(limit)

    items: List[RuleTemplate] = []
    async for raw in cursor:
        items.append(_to_model(raw))

    return (items, total)


async def find_rule(rule_code: str) -> Optional[RuleTemplate]:
    """
    按规则编码查询规则

    参数：
        rule_code: 规则编码

    返回：
        RuleTemplate 实例；不存在返回 None
    """
    collection = _collection()
    raw = await collection.find_one({"rule_code": rule_code})
    if raw is None:
        return None
    return _to_model(raw)


async def upsert_rule(rule: RuleTemplate) -> bool:
    """
    创建或更新规则模板（按 rule_code 幂等）

    参数：
        rule: RuleTemplate 实例

    返回：
        True 表示成功
    """
    from app.core.exceptions import MongoDBException

    collection = _collection()
    payload = rule.model_dump(mode="json")
    payload.pop("id", None)

    try:
        result = await collection.update_one(
            {"rule_code": rule.rule_code},
            {"$set": payload},
            upsert=True,
        )
        logger.info(
            "规则模板已保存 | rule_code=%s | upserted=%s | modified=%s",
            rule.rule_code, result.upserted_id is not None, result.modified_count > 0,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("规则模板保存失败 | rule_code=%s | 异常=%s", rule.rule_code, exc)
        raise MongoDBException(message="规则模板保存失败", detail=str(exc)) from exc


async def set_rule_enabled(rule_code: str, enabled: bool) -> bool:
    """
    启用或禁用规则模板

    参数：
        rule_code: 规则编码
        enabled: 目标状态（True 启用 / False 禁用）

    返回：
        True 表示成功；规则不存在返回 False
    """
    collection = _collection()
    status = "enabled" if enabled else "disabled"
    result = await collection.update_one(
        {"rule_code": rule_code},
        {"$set": {"status": status, "updated_at": datetime.now()}},
    )
    return result.matched_count > 0


async def delete_rule(rule_code: str) -> bool:
    """
    删除规则模板

    参数：
        rule_code: 规则编码

    返回：
        True 表示删除成功；不存在返回 False
    """
    collection = _collection()
    result = await collection.delete_one({"rule_code": rule_code})
    return result.deleted_count > 0


def _to_model(raw: Dict[str, Any]) -> RuleTemplate:
    """
    将 MongoDB 文档转换为 RuleTemplate 模型

    参数：
        raw: MongoDB 文档

    返回：
        RuleTemplate 实例
    """
    data = dict(raw)
    if "_id" in data:
        data["id"] = str(data.pop("_id"))
    return RuleTemplate(**data)
