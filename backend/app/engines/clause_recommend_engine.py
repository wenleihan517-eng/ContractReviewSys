# D:/ContractReviewSys-合同审批智能审查项目/backend/app/engines/clause_recommend_engine.py
"""
条款推荐引擎 — 生成示范修改条款（辅助能力，标记来源）

【模块定位】
    本引擎负责"示范修改条款"的生成，服务于 llm_review 节点与条款推荐场景。
    当规则引擎或大模型识别出某条款存在风险后，本引擎生成一段"示范性的
    合规修改条款"，供法务与业务人员参考，加速整改。

    职责：
        1. 基于风险项（风险描述 + 原条款 + 参考标准条款）构造推荐提示词
        2. 调用本地大模型生成示范修改条款（标记来源 llm）
        3. 【失败降级】大模型不可用时，用标准条款/模板文本兜底，
           标记来源 standard_clause 或 template，不中断主流程

【架构红线（代码级强制）】
    本引擎只生成"示范修改条款文本"，是辅助性内容，**不是判定结果**。
    示范条款不携带任何风险等级、不改变规则引擎的定级结论。
    示范条款必须标注来源（llm / standard_clause / template），
    避免被误认为正式结论。

返回值说明：
    - recommend_clause()：返回 ClauseRecommendation（示范修改条款）
    - recommend_clauses()：批量推荐，返回 List[ClauseRecommendation]

使用示例：
    rec = await clause_recommend_engine.recommend_clause(
        clause_content="任何一方违约概不负责",
        risk_description="存在单方免责表述",
        standard_clause="双方应依法承担各自的违约责任",
        legal_basis="民法典第四百九十七条",
    )
"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from app.core.logging_config import get_logger
from app.engines.llm_client import LLMClient, llm_client

logger = get_logger(__name__)


# 条款推荐来源标识
RECOMMEND_SOURCE_LLM = "llm"
RECOMMEND_SOURCE_STANDARD = "standard_clause"
RECOMMEND_SOURCE_TEMPLATE = "template"


class ClauseRecommendation(BaseModel):
    """
    示范修改条款推荐结果

    字段：
        recommendation_id: 推荐标识
        clause_id: 关联的原条款 ID
        risk_code: 关联的风险规则编码（可为空）
        original_text: 原条款文本
        recommended_text: 示范修改后的条款文本
        source: 来源（llm / standard_clause / template）
        legal_basis: 法律依据
        confidence: 生成置信度（仅 llm 来源有值）
    """

    recommendation_id: str = Field(description="推荐标识")
    clause_id: Optional[str] = Field(default=None, description="关联原条款 ID")
    risk_code: Optional[str] = Field(default=None, description="关联风险规则编码")
    original_text: str = Field(default="", description="原条款文本")
    recommended_text: str = Field(default="", description="示范修改后的条款文本")
    source: str = Field(default=RECOMMEND_SOURCE_TEMPLATE, description="来源标识")
    legal_basis: str = Field(default="", description="法律依据")
    confidence: Optional[float] = Field(
        default=None, ge=0.0, le=1.0, description="生成置信度，仅 llm 来源有值"
    )


class ClauseRecommendEngine:
    """
    条款推荐引擎

    属性：
        _llm_client: 大模型客户端（可注入，便于测试隔离外部依赖）
    """

    def __init__(self, llm: Optional[LLMClient] = None) -> None:
        """
        初始化条款推荐引擎

        参数：
            llm: 大模型客户端，缺省使用全局 llm_client 单例
        """
        self._llm_client = llm

    # ------------------------------------------------------------------
    # 一、单条示范条款生成
    # ------------------------------------------------------------------
    async def recommend_clause(
        self,
        clause_content: str,
        risk_description: str,
        standard_clause: str = "",
        legal_basis: str = "",
        clause_id: Optional[str] = None,
        risk_code: Optional[str] = None,
    ) -> ClauseRecommendation:
        """
        生成单条示范修改条款

        【生成优先级】
            1. 优先调用大模型生成（source=llm，标记来源）
            2. 大模型失败时降级为"标准条款"文本（source=standard_clause）
            3. 无标准条款时降级为"模板化建议"文本（source=template）

        参数：
            clause_content: 原条款文本
            risk_description: 风险描述（识别出的风险类型/原因）
            standard_clause: 参考标准条款文本（可为空）
            legal_basis: 法律依据
            clause_id: 关联原条款 ID
            risk_code: 关联风险规则编码

        返回：
            ClauseRecommendation 实例
        """
        import uuid

        recommendation_id = f"rec_{uuid.uuid4().hex}"

        # -------------------- 第一步：尝试大模型生成 --------------------
        try:
            recommended_text, confidence = await self._generate_by_llm(
                clause_content=clause_content,
                risk_description=risk_description,
                standard_clause=standard_clause,
                legal_basis=legal_basis,
            )
            if recommended_text.strip():
                return ClauseRecommendation(
                    recommendation_id=recommendation_id,
                    clause_id=clause_id,
                    risk_code=risk_code,
                    original_text=clause_content,
                    recommended_text=recommended_text.strip(),
                    source=RECOMMEND_SOURCE_LLM,
                    legal_basis=legal_basis,
                    confidence=confidence,
                )

        except Exception as exc:  # noqa: BLE001 - 大模型失败降级，不中断主流程
            logger.warning(
                "示范条款生成调用大模型失败，降级为模板/标准条款 | 异常=%s", exc
            )

        # -------------------- 第二步：降级为标准条款 --------------------
        if standard_clause.strip():
            return ClauseRecommendation(
                recommendation_id=recommendation_id,
                clause_id=clause_id,
                risk_code=risk_code,
                original_text=clause_content,
                recommended_text=standard_clause.strip(),
                source=RECOMMEND_SOURCE_STANDARD,
                legal_basis=legal_basis,
                confidence=None,
            )

        # -------------------- 第三步：降级为模板化建议 --------------------
        return ClauseRecommendation(
            recommendation_id=recommendation_id,
            clause_id=clause_id,
            risk_code=risk_code,
            original_text=clause_content,
            recommended_text=(
                f"建议对原条款进行如下修改：针对「{risk_description or '待明确风险'}」"
                f"，调整相关表述，明确双方权利义务对等、责任边界清晰。"
            ),
            source=RECOMMEND_SOURCE_TEMPLATE,
            legal_basis=legal_basis,
            confidence=None,
        )

    # ------------------------------------------------------------------
    # 二、批量推荐
    # ------------------------------------------------------------------
    async def recommend_clauses(
        self,
        items: List[Dict[str, Any]],
    ) -> List[ClauseRecommendation]:
        """
        批量生成示范修改条款

        参数：
            items: 推荐请求列表，每项含：
                clause_content / risk_description / standard_clause /
                legal_basis / clause_id / risk_code

        返回：
            ClauseRecommendation 列表，顺序与入参一致
        """
        recommendations: List[ClauseRecommendation] = []

        for item in items:
            recommendation = await self.recommend_clause(
                clause_content=str(item.get("clause_content", "")),
                risk_description=str(item.get("risk_description", "")),
                standard_clause=str(item.get("standard_clause", "")),
                legal_basis=str(item.get("legal_basis", "")),
                clause_id=item.get("clause_id"),
                risk_code=item.get("risk_code"),
            )
            recommendations.append(recommendation)

        logger.info(
            "示范条款批量推荐完成 | 请求数=%d | llm 来源=%d | 降级来源=%d",
            len(items),
            sum(1 for r in recommendations if r.source == RECOMMEND_SOURCE_LLM),
            sum(1 for r in recommendations if r.source != RECOMMEND_SOURCE_LLM),
        )
        return recommendations

    # ------------------------------------------------------------------
    # 三、大模型生成实现
    # ------------------------------------------------------------------
    async def _generate_by_llm(
        self,
        clause_content: str,
        risk_description: str,
        standard_clause: str,
        legal_basis: str,
    ) -> tuple:
        """
        调用大模型生成示范修改条款

        参数：
            clause_content: 原条款文本
            risk_description: 风险描述
            standard_clause: 参考标准条款
            legal_basis: 法律依据

        返回：
            (示范条款文本, 置信度) 元组

        异常：
            LLMException 等：由调用方捕获后降级
        """
        system_prompt = (
            "你是企业合同条款起草助手。请针对给定的合同条款及其风险，"
            "起草一段示范性的修改条款。\n"
            "要求：\n"
            "1. 保持原条款的业务意图不变；\n"
            "2. 修正风险描述中指出的问题；\n"
            "3. 语言正式、简洁、可落地；\n"
            "4. 只输出修改后的条款文本本身，不要解释、不要编号、不要标题。"
        )

        parts = [
            f"【原条款】{clause_content}",
            f"【识别到的风险】{risk_description or '未提供'}",
        ]
        if standard_clause:
            parts.append(f"【可参考的标准条款】{standard_clause}")
        if legal_basis:
            parts.append(f"【法律依据】{legal_basis}")
        parts.append("请输出示范修改条款：")

        user_prompt = "\n\n".join(parts)

        llm = self._llm_client or llm_client
        response = await llm.chat(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=0.2,
            json_mode=False,
        )

        recommended_text = (response.content or "").strip()
        # 兜底置信度：成功生成即视为 0.9（无模型自评时）
        return (recommended_text, 0.9)

    # ------------------------------------------------------------------
    # 四、从风险项批量构造推荐请求
    # ------------------------------------------------------------------
    @staticmethod
    def build_requests_from_risk_items(
        risk_items: List[Any],
        standard_clause_map: Optional[Dict[str, str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        从风险项列表构造推荐请求（供 llm_review 节点调用）

        说明：
            将 RiskItem（或 LLMCandidate 的 reference_only 项）转换为
            推荐请求，携带原条款证据与风险描述。

        参数：
            risk_items: 风险项列表（RiskItem）
            standard_clause_map: clause_id → 标准条款文本 的映射（可选）

        返回：
            推荐请求字典列表
        """
        standard_clause_map = standard_clause_map or {}
        requests: List[Dict[str, Any]] = []

        for item in risk_items:
            clause_id = getattr(item, "clause_id", None)
            requests.append(
                {
                    "clause_id": clause_id,
                    "risk_code": getattr(item, "rule_code", None),
                    "clause_content": getattr(item, "evidence_text", ""),
                    "risk_description": getattr(item, "rule_name", "")
                    or getattr(item, "risk_type", ""),
                    "standard_clause": standard_clause_map.get(clause_id or "", ""),
                    "legal_basis": getattr(item, "legal_basis", ""),
                }
            )
        return requests


# 全局单例
clause_recommend_engine = ClauseRecommendEngine()
