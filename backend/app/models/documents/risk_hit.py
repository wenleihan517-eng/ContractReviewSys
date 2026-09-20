# D:/ContractReviewSys-合同审批智能审查项目/backend/app/models/documents/risk_hit.py
"""
风险命中文档模型 — 对应 MongoDB 集合 cr_review_risk_hit

【架构红线（双来源隔离）】
    风险命中存在两个来源，必须严格区分存储：
        hit_source = "rule_engine"    → 规则引擎命中，可携带 risk_level（风险等级）
        hit_source = "llm_candidate"  → 大模型候选，禁止携带 risk_level
        hit_source = "vec_candidate"  → 向量召回候选，禁止携带 risk_level

    代码级强制：
        当 hit_source 非 rule_engine 时，若传入 risk_level 则直接抛错。
        这保证大模型产出物在数据结构层面就无法伪装成正式判定结果。

返回值说明：
    本模块只定义数据模型，无函数返回值

使用示例：
    from app.models.documents.risk_hit import RiskHit, HitSource
    hit = RiskHit(task_id="...", hit_source=HitSource.LLM_CANDIDATE.value, evidence_text="...")
"""

from datetime import datetime
from enum import Enum, unique
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from app.constants import RiskHitDecision, RiskLevel, RuleSeverity


@unique
class HitSource(str, Enum):
    """
    风险命中来源标识

    说明：
        该枚举是权责分离在数据层的落点。
        只有 RULE_ENGINE 来源的记录才有权携带风险等级。
    """

    RULE_ENGINE = "rule_engine"        # 规则引擎确定性命中（有权定级）
    LLM_CANDIDATE = "llm_candidate"    # 大模型疑似风险候选（无权定级）
    VEC_CANDIDATE = "vec_candidate"    # 向量语义召回候选（无权定级）


# 有权携带风险等级的来源集合
LEVEL_AUTHORIZED_SOURCES = frozenset({HitSource.RULE_ENGINE.value})


class RiskHit(BaseModel):
    """
    风险命中模型

    字段：
        id: 命中记录主键
        task_id: 审查任务 ID
        hit_source: 命中来源，取值见 HitSource 枚举
        rule_code: 规则编码（规则引擎命中时必填）
        rule_name: 规则名称
        clause_id: 命中条款 ID
        clause_type: 条款类型
        evidence_text: 证据原文片段
        char_start / char_end: 证据在原文字符位置（溯源用）
        page_no: 证据所在页码
        severity: 规则严重度（仅规则引擎命中时有值）
        risk_level: 风险等级（仅规则引擎命中时可写）
        decision: 处理决策（仅规则引擎命中时有值）
        llm_confidence: 大模型置信度（仅候选时有值）
        vec_similarity: 向量相似度（仅向量候选时有值）
        suggestion_text: 修改建议初稿（可为空）
        rule_snapshot: 规则快照（判定时的规则内容，保证可追溯）
        created_at: 创建时间
    """

    id: Optional[str] = Field(default=None, description="命中记录主键")
    task_id: str = Field(description="审查任务 ID")
    hit_source: str = Field(description="命中来源：rule_engine/llm_candidate/vec_candidate")

    # ---------------------- 规则标识（规则引擎命中时） ----------------------
    rule_code: Optional[str] = Field(
        default=None, description="规则编码，规则引擎命中时必填"
    )
    rule_name: Optional[str] = Field(default=None, description="规则名称")

    # ---------------------- 命中位置（溯源核心） ----------------------
    clause_id: Optional[str] = Field(default=None, description="命中条款 ID")
    clause_type: Optional[str] = Field(default=None, description="条款类型")
    evidence_text: str = Field(default="", description="证据原文片段")
    char_start: Optional[int] = Field(default=None, ge=0, description="证据起始字符位置")
    char_end: Optional[int] = Field(default=None, gt=0, description="证据结束字符位置")
    page_no: Optional[int] = Field(default=None, ge=1, description="证据所在页码")

    # ---------------------- 判定结果（仅规则引擎可写） ----------------------
    severity: Optional[str] = Field(
        default=None, description="规则严重度，仅规则引擎命中时有值"
    )
    risk_level: Optional[str] = Field(
        default=None,
        description="风险等级。仅当 hit_source=rule_engine 时可写，其余来源传入将报错",
    )
    decision: Optional[str] = Field(
        default=None, description="处理决策，仅规则引擎命中时有值"
    )

    # ---------------------- 候选置信度（仅候选用） ----------------------
    llm_confidence: Optional[float] = Field(
        default=None, ge=0.0, le=1.0, description="大模型置信度（0-1）"
    )
    vec_similarity: Optional[float] = Field(
        default=None, ge=-1.0, le=1.0, description="向量相似度（余弦）"
    )

    # ---------------------- 建议与快照 ----------------------
    suggestion_text: str = Field(default="", description="修改建议初稿，可为空")
    rule_snapshot: Dict[str, Any] = Field(
        default_factory=dict, description="规则快照，保证判定可追溯"
    )
    created_at: datetime = Field(default_factory=datetime.now, description="创建时间")

    @field_validator("hit_source")
    @classmethod
    def validate_hit_source(cls, value: str) -> str:
        """
        校验命中来源合法性

        参数：
            value: 来源字符串
        返回：
            校验通过的来源值
        异常：
            ValueError：取值不在 HitSource 枚举内时抛出
        """
        allowed = {item.value for item in HitSource}
        if value not in allowed:
            raise ValueError(f"命中来源非法：{value}，允许值：{sorted(allowed)}")
        return value

    @field_validator("risk_level")
    @classmethod
    def validate_risk_level(cls, value: Optional[str]) -> Optional[str]:
        """
        校验风险等级取值合法性

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
            raise ValueError(f"风险等级非法：{value}，允许值：{sorted(allowed)}")
        return value

    @field_validator("severity")
    @classmethod
    def validate_severity(cls, value: Optional[str]) -> Optional[str]:
        """
        校验规则严重度取值合法性

        参数：
            value: 严重度字符串或 None
        返回：
            校验通过的严重度值
        异常：
            ValueError：取值不在 RuleSeverity 枚举内时抛出
        """
        if value is None:
            return value
        allowed = {item.value for item in RuleSeverity}
        if value not in allowed:
            raise ValueError(f"规则严重度非法：{value}，允许值：{sorted(allowed)}")
        return value

    @field_validator("decision")
    @classmethod
    def validate_decision(cls, value: Optional[str]) -> Optional[str]:
        """
        校验处理决策取值合法性

        参数：
            value: 决策字符串或 None
        返回：
            校验通过的决策值
        异常：
            ValueError：取值不在 RiskHitDecision 枚举内时抛出
        """
        if value is None:
            return value
        allowed = {item.value for item in RiskHitDecision}
        if value not in allowed:
            raise ValueError(f"处理决策非法：{value}，允许值：{sorted(allowed)}")
        return value

    @model_validator(mode="after")
    def validate_source_permission(self) -> "RiskHit":
        """
        跨字段校验：非规则引擎来源禁止携带判定字段

        【红线强制】
            大模型候选与向量候选不得携带 risk_level / decision / severity，
            否则说明存在越权定级，直接拒绝构造该对象。

        返回：
            校验通过的本实例
        异常：
            ValueError：非规则引擎来源携带了判定字段时抛出
        """
        if self.hit_source not in LEVEL_AUTHORIZED_SOURCES:
            illegal_fields = []
            if self.risk_level is not None:
                illegal_fields.append("risk_level")
            if self.decision is not None:
                illegal_fields.append("decision")
            if self.severity is not None:
                illegal_fields.append("severity")

            if illegal_fields:
                raise ValueError(
                    f"权责违规：hit_source='{self.hit_source}' 无权写入判定字段 "
                    f"{illegal_fields}。风险等级与处理决策只能由规则引擎产出。"
                )

        # 规则引擎来源必须提供规则编码，否则无法追溯
        if self.hit_source == HitSource.RULE_ENGINE.value and not self.rule_code:
            raise ValueError("规则引擎命中必须提供 rule_code，否则无法追溯判定依据")

        return self

    @property
    def is_official_judgment(self) -> bool:
        """
        判断本条记录是否属于正式判定结果

        返回：
            True 表示规则引擎产出的正式判定；False 表示仅为候选
        """
        return self.hit_source == HitSource.RULE_ENGINE.value

    @property
    def evidence_range(self) -> Optional[tuple]:
        """
        获取证据字符区间

        返回：
            (char_start, char_end) 元组；缺失位置信息时返回 None
        """
        if self.char_start is None or self.char_end is None:
            return None
        return (self.char_start, self.char_end)

    @property
    def display_label(self) -> str:
        """
        获取前端展示标签

        说明：
            候选记录必须带"AI 候选"标识，避免被误认为正式判定。

        返回：
            展示用标签文本
        """
        if self.is_official_judgment:
            return f"[规则] {self.rule_name or self.rule_code}"
        if self.hit_source == HitSource.LLM_CANDIDATE.value:
            return "[AI 候选] 待人工确认"
        return "[语义候选] 待人工确认"
