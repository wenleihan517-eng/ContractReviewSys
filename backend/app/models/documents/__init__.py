# D:/ContractReviewSys-合同审批智能审查项目/backend/app/models/documents/__init__.py
"""
数据模型统一出口 — MongoDB 集合文档模型

本包职责：
    聚合全部 MongoDB 文档模型，业务代码统一从此处导入。
    模型与集合一一对应，字段设计遵循架构文档 §2.1。

模型清单：
    - Contract           → cr_contract           合同主档
    - ContractClause     → cr_contract_clause    条款结构化结果（含溯源位置）
    - ReviewTask         → cr_review_task        审查任务（状态机 + 会话隔离）
    - RiskHit            → cr_review_risk_hit    风险命中（双来源隔离）
    - ReviewConclusion   → cr_review_conclusion  审查结论（规则引擎独占）
    - RuleTemplate       → cr_rule_template      合规规则模板

设计要点：
    - 全部基于 Pydantic v2 BaseModel，自带类型校验
    - 枚举字段统一从 app.constants 引用，禁止硬编码字符串
    - 权责红线通过 model_validator 在数据层强制，越权构造直接抛错

返回值说明：
    本模块仅做聚合导出，无函数返回值

使用示例：
    from app.models.documents import Contract, ReviewTask, RiskHit
"""

from app.models.documents.clause import BoundingBox, ContractClause
from app.models.documents.contract import Contract, PartyInfo
from app.models.documents.notification import Notification
from app.models.documents.review_conclusion import ReviewConclusion
from app.models.documents.review_task import (
    BlockedReason,
    NodeExecution,
    ReviewTask,
    RouteType,
)
from app.models.documents.risk_hit import HitSource, RiskHit
from app.models.documents.rule_template import RuleTemplate

__all__ = [
    # 合同
    "Contract",
    "PartyInfo",
    # 条款
    "ContractClause",
    "BoundingBox",
    # 通知
    "Notification",
    # 任务
    "ReviewTask",
    "NodeExecution",
    "BlockedReason",
    "RouteType",
    # 风险命中
    "RiskHit",
    "HitSource",
    # 结论
    "ReviewConclusion",
    # 规则模板
    "RuleTemplate",
]
