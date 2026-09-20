# D:/ContractReviewSys-合同审批智能审查项目/backend/app/models/documents/rule_template.py
"""
合规规则模板文档模型 — 对应 MongoDB 集合 cr_rule_template

【模型定位】
    本模型是规则引擎的配置来源。规则模板定义"怎么匹配"与"命中后怎么定级"，
    但最终定级仍由规则引擎的确定性映射逻辑执行，模板只提供配置参数。

    匹配类型与配置对应关系：
        keyword        → match_config.keywords     关键词列表
        regex          → match_config.pattern      正则表达式
        clause_type    → match_config.clause_types 条款类型列表
        semantic       → match_config.threshold    语义相似度阈值（配合 Milvus 召回）
        field_compare  → match_config.field/op/value  字段比较

返回值说明：
    本模块只定义数据模型，无函数返回值

使用示例：
    from app.models.documents.rule_template import RuleTemplate
    rule = RuleTemplate(rule_code="R001", rule_name="违约金比例过高", severity="fatal")
"""

from datetime import datetime
from enum import Enum, unique
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from app.constants import ClauseType, RiskLevel, RuleMatchType, RuleSeverity


@unique
class CompareOperator(str, Enum):
    """字段比较运算符（仅供 field_compare 类型规则使用）"""

    GT = "gt"      # 大于
    GTE = "gte"    # 大于等于
    LT = "lt"      # 小于
    LTE = "lte"    # 小于等于
    EQ = "eq"      # 等于
    NEQ = "neq"    # 不等于
    IN = "in"      # 属于集合
    NOT_IN = "not_in"  # 不属于集合


class RuleTemplate(BaseModel):
    """
    合规规则模板模型

    字段：
        id: 规则主键
        rule_code: 规则编码（唯一）
        rule_name: 规则名称
        description: 规则说明
        clause_type: 适用条款类型
        match_type: 匹配方式，取值见 RuleMatchType 枚举
        match_config: 匹配配置，结构随 match_type 变化
        severity: 规则严重度，取值见 RuleSeverity 枚举
        override_risk_level: 覆盖定级（可空）。非空时优先于严重度映射
        suggestion_template: 修改建议模板
        legal_basis: 法律依据
        status: 模板状态，取值见 RuleTemplateStatus 枚举
        version: 版本号，每次修改递增
        priority: 规则优先级，数值越大优先级越高（冲突时高优先级胜出）
        created_by / updated_by: 操作人
        created_at / updated_at: 时间戳
    """

    id: Optional[str] = Field(default=None, description="规则主键")
    rule_code: str = Field(description="规则编码（唯一）")
    rule_name: str = Field(description="规则名称")
    description: str = Field(default="", description="规则说明")

    # ---------------------- 适用范围 ----------------------
    clause_type: str = Field(
        default=ClauseType.OTHER.value,
        description="适用条款类型，取值见 ClauseType 枚举",
    )
    contract_types: List[str] = Field(
        default_factory=lambda: ["all"],
        description=(
            "适用合同类型列表。取值如 purchase(采购)/sales(销售)/service(服务)/"
            "labor(劳动)；特殊值 all 表示通用规则，适用于所有合同类型。"
            "空列表等价于 all。规则加载时按此字段做合同类型过滤"
        ),
    )

    # ---------------------- 匹配配置 ----------------------
    match_type: str = Field(
        default=RuleMatchType.KEYWORD.value,
        description=(
            "匹配方式：keyword/regex/clause_type/semantic/field_compare/"
            "missing_clause/numeric_threshold"
        ),
    )
    match_config: Dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "匹配配置，结构随 match_type 变化："
            "keyword→{keywords:[]}；regex→{pattern:str}；"
            "clause_type→{clause_types:[]}；semantic→{threshold:float}；"
            "field_compare→{field:str, op:str, value:any}；"
            "missing_clause→{require_clause_types:[]}；"
            "numeric_threshold→{field:str, op:str, value:number}"
        ),
    )
    priority: int = Field(
        default=100,
        ge=0,
        description="规则优先级，数值越大优先级越高，用于多路召回冲突处理",
    )

    # ---------------------- 定级配置（规则引擎消费） ----------------------
    severity: str = Field(
        default=RuleSeverity.INFO.value,
        description="规则严重度，取值见 RuleSeverity 枚举，由引擎映射为风险等级",
    )
    override_risk_level: Optional[str] = Field(
        default=None,
        description="覆盖定级。非空时直接采用该等级，优先于严重度映射",
    )

    # ---------------------- 建议与依据 ----------------------
    suggestion_template: str = Field(
        default="",
        description="修改建议模板。可含占位符如 {evidence}，最终文本由规则引擎填充",
    )
    legal_basis: str = Field(default="", description="法律依据")

    # ---------------------- 状态与版本 ----------------------
    status: str = Field(
        default="enabled", description="模板状态：draft/enabled/disabled/deprecated"
    )
    version: int = Field(default=1, ge=1, description="版本号，每次修改递增")

    # ---------------------- 人员与时间 ----------------------
    created_by: str = Field(default="", description="创建人")
    updated_by: str = Field(default="", description="最后修改人")
    created_at: datetime = Field(default_factory=datetime.now, description="创建时间")
    updated_at: datetime = Field(default_factory=datetime.now, description="更新时间")

    @field_validator("match_type")
    @classmethod
    def validate_match_type(cls, value: str) -> str:
        """
        校验匹配方式合法性

        参数：
            value: 匹配方式字符串
        返回：
            校验通过的匹配方式
        异常：
            ValueError：取值不在 RuleMatchType 枚举内时抛出
        """
        allowed = {item.value for item in RuleMatchType}
        if value not in allowed:
            raise ValueError(f"匹配方式非法：{value}，允许值：{sorted(allowed)}")
        return value

    @field_validator("severity")
    @classmethod
    def validate_severity(cls, value: str) -> str:
        """
        校验规则严重度合法性

        参数：
            value: 严重度字符串
        返回：
            校验通过的严重度
        异常：
            ValueError：取值不在 RuleSeverity 枚举内时抛出
        """
        allowed = {item.value for item in RuleSeverity}
        if value not in allowed:
            raise ValueError(f"规则严重度非法：{value}，允许值：{sorted(allowed)}")
        return value

    @field_validator("override_risk_level")
    @classmethod
    def validate_override_level(cls, value: Optional[str]) -> Optional[str]:
        """
        校验覆盖定级合法性

        参数：
            value: 风险等级字符串或 None
        返回：
            校验通过的等级值
        异常：
            ValueError：取值不在 RiskLevel 枚举内时抛出
        """
        if value is None:
            return value
        allowed = {item.value for item in RiskLevel}
        if value not in allowed:
            raise ValueError(f"覆盖定级非法：{value}，允许值：{sorted(allowed)}")
        return value

    @model_validator(mode="after")
    def validate_match_config(self) -> "RuleTemplate":
        """
        跨字段校验：match_config 必须与 match_type 匹配

        说明：
            规则模板的匹配配置缺失关键字段会导致规则静默失效，
            因此在此处做严格校验，配置不完整直接拒绝保存。

        返回：
            校验通过的本实例
        异常：
            ValueError：match_config 缺少该匹配类型必需的键时抛出
        """
        # 各匹配类型必需的配置键
        required_keys: Dict[str, tuple] = {
            RuleMatchType.KEYWORD.value: ("keywords",),
            RuleMatchType.REGEX.value: ("pattern",),
            RuleMatchType.CLAUSE_TYPE.value: ("clause_types",),
            RuleMatchType.SEMANTIC.value: ("threshold",),
            RuleMatchType.FIELD_COMPARE.value: ("field", "op", "value"),
            # 必备条款缺失：require_clause_types 指定必须存在的条款类型集合
            # （命中语义为"合同缺失了这些条款"，与 clause_type 的"条款类型匹配"相反）
            RuleMatchType.MISSING_CLAUSE.value: ("require_clause_types",),
            # 数值阈值：field 指定待比较字段，op 为比较运算符，value 为阈值
            RuleMatchType.NUMERIC_THRESHOLD.value: ("field", "op", "value"),
        }

        need = required_keys.get(self.match_type, ())
        missing = [key for key in need if key not in self.match_config]

        if missing:
            raise ValueError(
                f"match_type='{self.match_type}' 的 match_config 缺少必需配置项："
                f"{missing}。缺失配置会导致规则静默失效，禁止保存。"
            )

        # 语义匹配阈值必须落在 (0, 1] 区间
        if self.match_type == RuleMatchType.SEMANTIC.value:
            threshold = self.match_config.get("threshold")
            if not isinstance(threshold, (int, float)) or not (0.0 < float(threshold) <= 1.0):
                raise ValueError(
                    f"语义匹配阈值必须为 (0, 1] 之间的数值，当前为 {threshold}"
                )

        # 关键词列表必须非空
        if self.match_type == RuleMatchType.KEYWORD.value:
            keywords = self.match_config.get("keywords")
            if not isinstance(keywords, list) or not keywords:
                raise ValueError("关键词匹配的 keywords 必须为非空列表")

        # 必备条款缺失：require_clause_types 必须为非空列表
        if self.match_type == RuleMatchType.MISSING_CLAUSE.value:
            require_types = self.match_config.get("require_clause_types")
            if not isinstance(require_types, list) or not require_types:
                raise ValueError(
                    "必备条款缺失检查的 require_clause_types 必须为非空列表，"
                    "缺失会导致规则静默失效"
                )

        # 数值阈值：op 必须为合法比较运算符，value 必须为数值
        if self.match_type in (
            RuleMatchType.FIELD_COMPARE.value,
            RuleMatchType.NUMERIC_THRESHOLD.value,
        ):
            op = self.match_config.get("op")
            allowed_ops = {item.value for item in CompareOperator}
            if op not in allowed_ops:
                raise ValueError(
                    f"数值比较运算符非法：{op}，允许值：{sorted(allowed_ops)}"
                )
            value = self.match_config.get("value")
            if not isinstance(value, (int, float)):
                raise ValueError(
                    f"数值比较的 value 必须为数值类型，当前为 {value!r}"
                )

        return self

    @property
    def is_active(self) -> bool:
        """
        判断规则是否处于启用状态

        返回：
            True 表示规则已启用，可参与匹配
        """
        return self.status == "enabled"

    def matches_contract_type(self, contract_type: str) -> bool:
        """
        判断本规则是否适用于指定合同类型

        说明：
            规则配置的 contract_types 为空列表、含特殊值 "all"、
            或含目标合同类型时，均视为适用（空列表等价于通用规则）。
            这样"未配置适用范围"的规则不会因遗漏而被静默跳过，
            符合"宁可多审查、不可漏审查"的合规导向。

        参数：
            contract_type: 目标合同类型（如 purchase/sales/service/labor）

        返回：
            True 表示适用，False 表示不适用
        """
        # 未配置适用范围 / 含 all / 含目标类型 → 适用
        if not self.contract_types:
            return True
        if "all" in self.contract_types:
            return True
        return contract_type in self.contract_types

    @property
    def effective_risk_level(self) -> Optional[str]:
        """
        获取优先定级（如配置了覆盖定级）

        说明：
            本属性只反映模板配置，最终风险等级仍由规则引擎
            调用 map_severity_to_risk_level 计算得出。

        返回：
            覆盖定级值；未配置时返回 None
        """
        return self.override_risk_level

    def bump_version(self, operator: str) -> None:
        """
        递增版本号并记录修改人

        说明：
            规则模板任何变更都必须走本方法，保证变更留痕可追溯。

        参数：
            operator: 修改人标识
        返回：
            无返回值
        """
        self.version += 1
        self.updated_by = operator
        self.updated_at = datetime.now()
