# D:/ContractReviewSys-合同审批智能审查项目/backend/app/models/__init__.py
"""
数据模型层统一出口

【目录职责】
    models/ 保持纯粹，只放数据模型定义，不掺入任何 IO 代码：
        models/documents/  Mongo 集合文档模型（Pydantic v2）
    MongoDB / MinIO / Milvus 的客户端单例位于 app/storage/，与本目录职责分离。

设计要点：
    - 模型与集合一一对应，字段命名与架构文档保持一致
    - 权责红线（大模型无定级权）在模型层通过校验器强制
    - 全部模型基于 Pydantic v2，提供类型校验与序列化能力

返回值说明：
    本模块仅做聚合导出，无函数返回值

使用示例：
    from app.models import Contract, ReviewTask, RiskHit, ReviewConclusion
"""

from app.models.documents import (
    BlockedReason,
    BoundingBox,
    Contract,
    ContractClause,
    HitSource,
    NodeExecution,
    PartyInfo,
    ReviewConclusion,
    ReviewTask,
    RiskHit,
    RouteType,
    RuleTemplate,
)

__all__ = [
    "Contract",
    "PartyInfo",
    "ContractClause",
    "BoundingBox",
    "ReviewTask",
    "NodeExecution",
    "BlockedReason",
    "RouteType",
    "RiskHit",
    "HitSource",
    "ReviewConclusion",
    "RuleTemplate",
]
