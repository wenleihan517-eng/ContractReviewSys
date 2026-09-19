# D:/ContractReviewSys-合同审批智能审查项目/backend/app/schemas/rule.py
"""
规则管理接口契约模型

【目录职责】
    定义"规则模板维护、示范条款库、启用禁用规则"接口的请求与响应模型。
    规则是规则引擎的配置来源，接口只做 CRUD，不改判定逻辑。
"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ======================================================================
# 一、规则模板
# ======================================================================
class RuleUpsertRequest(BaseModel):
    """
    创建/更新规则模板请求

    字段：
        rule_code: 规则编码（唯一）
        rule_name: 规则名称
        description: 规则说明
        match_type: 匹配方式
        match_config: 匹配配置
        contract_types: 适用合同类型
        severity: 规则严重度
        override_risk_level: 覆盖定级（可选）
        suggestion_template: 修改建议模板
        legal_basis: 法律依据
        priority: 优先级
        enabled: 是否启用
    """

    rule_code: str = Field(description="规则编码（唯一）")
    rule_name: str = Field(description="规则名称")
    description: str = Field(default="", description="规则说明")
    match_type: str = Field(default="keyword", description="匹配方式")
    match_config: Dict[str, Any] = Field(default_factory=dict, description="匹配配置")
    contract_types: List[str] = Field(
        default_factory=lambda: ["all"], description="适用合同类型"
    )
    severity: str = Field(default="info", description="规则严重度")
    override_risk_level: Optional[str] = Field(default=None, description="覆盖定级")
    suggestion_template: str = Field(default="", description="修改建议模板")
    legal_basis: str = Field(default="", description="法律依据")
    priority: int = Field(default=100, ge=0, description="优先级")
    enabled: bool = Field(default=True, description="是否启用")


class RuleItemResponse(BaseModel):
    """
    规则模板响应

    字段：规则的全部配置字段（面向前端展示）
    """

    rule_code: str = Field(description="规则编码")
    rule_name: str = Field(description="规则名称")
    description: str = Field(default="", description="规则说明")
    match_type: str = Field(description="匹配方式")
    match_config: Dict[str, Any] = Field(default_factory=dict, description="匹配配置")
    contract_types: List[str] = Field(default_factory=list, description="适用合同类型")
    severity: str = Field(description="规则严重度")
    override_risk_level: Optional[str] = Field(default=None, description="覆盖定级")
    suggestion_template: str = Field(default="", description="修改建议模板")
    legal_basis: str = Field(default="", description="法律依据")
    priority: int = Field(default=100, description="优先级")
    enabled: bool = Field(default=True, description="是否启用")
    version: int = Field(default=1, description="版本号")


class RuleEnableRequest(BaseModel):
    """
    启用/禁用规则请求

    字段：
        rule_code: 规则编码
        enabled: 目标启用状态
    """

    rule_code: str = Field(description="规则编码")
    enabled: bool = Field(description="目标启用状态")


class RuleEnableResponse(BaseModel):
    """
    启用/禁用规则响应
    """

    rule_code: str = Field(description="规则编码")
    enabled: bool = Field(description="更新后的启用状态")
    message: str = Field(default="", description="结果说明")


# ======================================================================
# 二、示范条款库
# ======================================================================
class StandardClauseUpsertRequest(BaseModel):
    """
    创建/更新示范条款请求

    字段：
        clause_id: 条款 ID（可选，更新时传入）
        clause_title: 条款标题
        clause_text: 条款正文
        clause_type: 条款类型
        contract_types: 适用合同类型
    """

    clause_id: Optional[str] = Field(default=None, description="条款 ID（更新时传入）")
    clause_title: str = Field(description="条款标题")
    clause_text: str = Field(description="条款正文")
    clause_type: str = Field(default="other", description="条款类型")
    contract_types: List[str] = Field(
        default_factory=lambda: ["all"], description="适用合同类型"
    )


class StandardClauseResponse(BaseModel):
    """
    示范条款响应
    """

    clause_id: str = Field(description="条款 ID")
    clause_title: str = Field(description="条款标题")
    clause_text: str = Field(description="条款正文")
    clause_type: str = Field(default="other", description="条款类型")
    contract_types: List[str] = Field(default_factory=list, description="适用合同类型")
