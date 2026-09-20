# D:/ContractReviewSys-合同审批智能审查项目/backend/app/schemas/review.py
"""
风险审查接口契约模型

【目录职责】
    定义"风险审查"接口的请求与响应模型。审查结果的 risk_level / decision
    只能来自规则引擎，接口层只做读取，不提供定级写入入口。
"""

from typing import Dict, List, Optional

from pydantic import BaseModel, Field


# ======================================================================
# 一、触发审查与结果查询
# ======================================================================
class ReviewTriggerRequest(BaseModel):
    """
    触发审查请求

    字段：
        task_id: 任务 ID
        object_key: 合同原件 MinIO 对象键（可选）。
                    若任务主档已保存合同键（先触发解析的场景），可省略；
                    若两者都缺失，接口返回"缺少合同文件对象键"。
        source_type: 合同来源类型 digital / scanned（可选，默认 digital）
        file_name: 原件文件名（可选，仅用于日志与标题兜底）
    """

    task_id: str = Field(description="任务 ID")
    object_key: str = Field(default="", description="合同原件 MinIO 对象键（可选）")
    source_type: str = Field(default="digital", description="来源类型 digital/scanned")
    file_name: str = Field(default="", description="原件文件名（可选）")


class ReviewTriggerResponse(BaseModel):
    """
    触发审查响应
    """

    task_id: str = Field(description="任务 ID")
    accepted: bool = Field(default=True, description="是否已受理")
    message: str = Field(default="", description="受理说明")


class RiskItemResponse(BaseModel):
    """
    风险项响应

    字段：
        risk_id / rule_code / rule_name：风险标识与规则
        clause_id / clause_type：关联条款
        evidence_text：证据原文
        char_start / char_end / page_no：原文定位
        risk_level / decision / severity：判定结果（规则引擎产出）
        suggestion_text / legal_basis：建议与依据
        reference_only：是否为参考项（AI 候选，不参与定级）
        matched_sources：命中来源
    """

    risk_id: str = Field(description="风险项 ID")
    rule_code: Optional[str] = Field(default=None, description="规则编码")
    rule_name: str = Field(default="", description="规则名称")
    clause_id: Optional[str] = Field(default=None, description="关联条款")
    clause_type: Optional[str] = Field(default=None, description="条款类型")
    evidence_text: str = Field(default="", description="证据原文")
    char_start: Optional[int] = Field(default=None, description="起始字符位置")
    char_end: Optional[int] = Field(default=None, description="结束字符位置")
    page_no: Optional[int] = Field(default=None, description="页码")
    risk_level: str = Field(default="low", description="风险等级")
    decision: str = Field(default="", description="处理决策")
    severity: Optional[str] = Field(default=None, description="规则严重度")
    suggestion_text: str = Field(default="", description="修改建议")
    legal_basis: str = Field(default="", description="法律依据")
    reference_only: bool = Field(default=False, description="是否参考项")
    matched_sources: List[str] = Field(default_factory=list, description="命中来源")


class ReviewResultResponse(BaseModel):
    """
    审查结果响应

    字段：
        task_id：任务 ID
        rule_evaluated：规则引擎是否已判定
        risk_level：综合风险等级（规则引擎产出）
        is_blocked：是否阻塞
        risk_distribution：风险等级分布
        conclusion_text：审查结论
        risk_items：最终风险项列表
        merge_summary：融合摘要
    """

    task_id: str = Field(description="任务 ID")
    rule_evaluated: bool = Field(default=False, description="规则引擎是否已判定")
    risk_level: Optional[str] = Field(default=None, description="综合风险等级")
    is_blocked: bool = Field(default=False, description="是否阻塞")
    risk_distribution: Dict[str, int] = Field(
        default_factory=dict, description="风险等级分布"
    )
    conclusion_text: str = Field(default="", description="审查结论")
    risk_items: List[RiskItemResponse] = Field(
        default_factory=list, description="最终风险项列表"
    )
    merge_summary: str = Field(default="", description="融合摘要")


# ======================================================================
# 二、人工编辑风险项与推荐条款
# ======================================================================
class RiskItemEditRequest(BaseModel):
    """
    人工编辑风险项请求

    【权责红线】
        人工编辑仅允许修改"建议类"字段（suggestion_text / legal_basis），
        不允许修改判定字段（risk_level / decision / severity / judge_source）。
        判定字段只能由规则引擎产出。

    字段：
        risk_id: 风险项 ID
        suggestion_text: 修改后的建议文本
        legal_basis: 修改后的法律依据
    """

    risk_id: str = Field(description="风险项 ID")
    suggestion_text: Optional[str] = Field(default=None, description="修改后的建议")
    legal_basis: Optional[str] = Field(default=None, description="修改后的法律依据")


class RiskItemEditResponse(BaseModel):
    """
    人工编辑风险项响应
    """

    risk_id: str = Field(description="风险项 ID")
    updated: bool = Field(default=True, description="是否已更新")
    message: str = Field(default="", description="结果说明")


class ClauseRecommendRequest(BaseModel):
    """
    人工修改推荐条款请求

    字段：
        risk_id: 关联风险项 ID
        recommended_text: 人工修订后的示范条款
    """

    risk_id: str = Field(description="关联风险项 ID")
    recommended_text: str = Field(description="人工修订后的示范条款")


class ClauseRecommendResponse(BaseModel):
    """
    人工修改推荐条款响应
    """

    risk_id: str = Field(description="关联风险项 ID")
    updated: bool = Field(default=True, description="是否已更新")
    message: str = Field(default="", description="结果说明")
