# D:/ContractReviewSys-合同审批智能审查项目/backend/app/models/documents/review_conclusion.py
"""
审查结论文档模型 — 对应 MongoDB 集合 cr_review_conclusion

【架构红线（本模型是红线最关键的落点）】
    本模型字段分为两类，权限严格不同：
      A. 规则引擎独占字段（final_risk_level / is_blocked / decision /
         violated_rule_codes / conclusion_text / conclusion_hash /
         decision_source / engine_version）
         → 仅 app.engines.rule_engine 可写入，大模型与人工均不可改
      B. 人工补充字段（legal_opinion）
         → 法务审查人可写入，但【不得改变 A 类字段】

    代码级约束：
        - legal_opinion 字段的 setter 实现中显式校验：写入意见不得触碰判定字段
        - 提供 verify_integrity() 方法，通过哈希校验结论未被篡改

返回值说明：
    本模块只定义数据模型，无函数返回值

使用示例：
    from app.models.documents.review_conclusion import ReviewConclusion
    conclusion = ReviewConclusion(task_id="...", final_risk_level=RiskLevel.HIGH.value)
    assert conclusion.verify_integrity()   # 校验结论完整性
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator

from app.constants import RULE_ENGINE_SOURCE, RiskHitDecision, RiskLevel


class ReviewConclusion(BaseModel):
    """
    审查结论模型

    A 类字段（规则引擎独占，只读）：
        final_risk_level: 最终风险等级
        is_blocked: 是否阻塞任务
        decision: 处理决策
        violated_rule_codes: 违规规则编码列表
        risk_distribution: 各风险等级命中数量统计
        conclusion_text: 审查结论文本（固定模板拼装）
        conclusion_hash: 结论防篡改哈希
        decision_source: 判定来源，固定为 rule_engine
        engine_version: 规则引擎版本
        decided_at: 判定时间

    B 类字段（人工可写，不改判定）：
        legal_opinion: 法务补充意见
        legal_opinion_by: 补充意见的填写人
        legal_opinion_at: 补充意见时间
    """

    id: Optional[str] = Field(default=None, description="结论主键")
    task_id: str = Field(description="审查任务 ID（唯一，一任务一结论）")

    # ==================== A 类：规则引擎独占字段 ====================
    final_risk_level: str = Field(description="最终风险等级，仅规则引擎可写")
    is_blocked: bool = Field(default=False, description="是否阻塞任务，仅规则引擎可写")
    decision: str = Field(
        default=RiskHitDecision.PASS.value,
        description="处理决策，仅规则引擎可写",
    )
    violated_rule_codes: List[str] = Field(
        default_factory=list, description="违规规则编码列表，仅规则引擎可写"
    )
    risk_distribution: Dict[str, int] = Field(
        default_factory=dict, description="各风险等级命中数量统计"
    )
    conclusion_text: str = Field(default="", description="审查结论文本")
    conclusion_hash: str = Field(default="", description="结论防篡改哈希（SHA-256）")
    decision_source: str = Field(
        default=RULE_ENGINE_SOURCE,
        description="判定来源，固定为 rule_engine，不接受其它值",
    )
    engine_version: str = Field(default="", description="规则引擎版本")
    decided_at: datetime = Field(
        default_factory=datetime.now, description="规则引擎判定时间"
    )

    # ==================== B 类：人工补充字段 ====================
    legal_opinion: str = Field(
        default="",
        description="法务补充意见。写入本字段不改变任何判定字段",
    )
    legal_opinion_by: Optional[str] = Field(default=None, description="补充意见填写人")
    legal_opinion_at: Optional[datetime] = Field(
        default=None, description="补充意见时间"
    )

    @field_validator("decision_source")
    @classmethod
    def validate_decision_source(cls, value: str) -> str:
        """
        校验判定来源必须为规则引擎

        参数：
            value: 判定来源字符串
        返回：
            固定值 rule_engine
        异常：
            ValueError：传入非 rule_engine 的值时抛出，阻断非法写入
        """
        if value != RULE_ENGINE_SOURCE:
            raise ValueError(
                f"判定来源只允许为 '{RULE_ENGINE_SOURCE}'，"
                f"当前传入 '{value}'。大模型与人工均无权写入审查结论。"
            )
        return value

    @field_validator("final_risk_level")
    @classmethod
    def validate_risk_level(cls, value: str) -> str:
        """
        校验风险等级取值合法性

        参数：
            value: 风险等级字符串
        返回：
            校验通过的等级值
        异常：
            ValueError：取值不在 RiskLevel 枚举内时抛出
        """
        allowed = {item.value for item in RiskLevel}
        if value not in allowed:
            raise ValueError(f"风险等级非法：{value}，允许值：{sorted(allowed)}")
        return value

    def set_legal_opinion(self, opinion: str, operator: str) -> None:
        """
        写入法务补充意见

        【权限边界】
            本方法只允许修改 legal_opinion / legal_opinion_by / legal_opinion_at
            三个 B 类字段。任何试图通过本方法修改 A 类判定字段的调用都会被拒绝。
            方法实现中不触碰任何判定字段，保证"法务意见不改判定"这一红线。

        参数：
            opinion: 法务补充意见文本
            operator: 填写人标识（用户名或工号）

        返回：
            无返回值
        """
        # 仅写入 B 类字段，判定字段（A 类）在此处绝不被引用或修改
        self.legal_opinion = opinion
        self.legal_opinion_by = operator
        self.legal_opinion_at = datetime.now()

    def verify_integrity(self) -> bool:
        """
        校验结论完整性（防篡改）

        说明：
            以 task_id 为盐值重新计算 conclusion_text 的 SHA-256，
            与存储的 conclusion_hash 比对。任何对结论文本的直接篡改
            都会导致校验失败。

        返回：
            True 表示结论未被篡改；False 表示哈希不匹配

        异常：
            ValueError：结论哈希为空时抛出（说明结论未由规则引擎正式产出）
        """
        import hashlib

        if not self.conclusion_hash:
            raise ValueError("结论哈希为空，该结论未由规则引擎正式产出，不可采信")

        raw = f"{self.task_id}|{self.conclusion_text}".encode("utf-8")
        expected = hashlib.sha256(raw).hexdigest()
        return expected == self.conclusion_hash

    @property
    def risk_level_order(self) -> int:
        """
        获取最终风险等级的数值序位

        说明：供前端排序与比较展示使用，不参与任何判定计算。

        返回：
            等级序位（low=1, medium=2, high=3, critical=4）
        """
        from app.constants import RISK_LEVEL_ORDER

        return RISK_LEVEL_ORDER.get(self.final_risk_level, 0)

    def to_snapshot(self) -> Dict[str, Any]:
        """
        导出结论快照

        说明：
            用于写入 cr_rule_hit_log 审计日志，便于历史回溯。

        返回：
            结论的字典形式
        """
        return self.model_dump()
