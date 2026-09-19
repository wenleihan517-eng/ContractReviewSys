# D:/ContractReviewSys-合同审批智能审查项目/backend/app/services/review_service.py
"""
风险审查服务 — 向量检索 / 大模型审查 / 风险融合 的统一编排层

【模块定位】
    本模块是"风险审查"三个节点背后的服务编排层，把向量检索、大模型
    审查、风险融合的能力编排为三个**独立可测试**的服务函数：

        vector_review_service  —— 向量语义召回（候选线索，无定级权）
        llm_review_service      —— 大模型模糊识别 + 示范条款推荐（辅助，无定级权）
        risk_merge_service      —— 三路融合（规则引擎为准兜底判定）

    每个函数都是独立函数，可单独 import 调用与单元测试，
    不依赖 LangGraph 的节点包装器。

【权责分离（架构强制）】
    1. 向量检索仅输出相似度得分与疑似风险（VectorCandidate），不做最终判定
    2. 大模型仅做模糊语义识别、生成修改建议初稿（LLMCandidate），
       不得输出最终风险等级
    3. 最终风险等级与综合风险判定由 risk_merge_service 以规则引擎为准兜底：
       综合风险 = 规则命中项的最高风险等级，向量/大模型候选不参与定级

【与图节点的契约】
    三个节点（vector_review / llm_review / risk_merge）分别委托调用
    本模块的对应服务函数，节点只做状态回填与审计埋点，
    具体能力全部收敛在本模块与 engines 层。

使用示例：
    candidates = await vector_review_service(clauses)
    llm_result = await llm_review_service(clauses, contract_context)
    merged = risk_merge_service(rule_results, vector_results, llm_results)
"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from app.constants import RISK_LEVEL_ORDER, RiskHitDecision, RiskLevel
from app.core.config import settings
from app.core.logging_config import get_logger
from app.engines.clause_recommend_engine import (
    ClauseRecommendEngine,
    ClauseRecommendation,
    clause_recommend_engine,
)
from app.engines.llm_engine import LLMEngine, LLMReviewResult, llm_engine
from app.engines.vector_engine import VectorEngine, vector_engine

logger = get_logger(__name__)


class MergeResult(BaseModel):
    """
    风险融合结果

    字段：
        final_items: 融合后的最终风险项列表（RiskItem）
        risk_distribution: 各风险等级命中数量统计（仅统计正式判定项）
        overall_risk_level: 整体综合风险等级（规则引擎为准取最高）
        official_count: 正式判定项数（非 reference_only）
        reference_count: 参考项数（reference_only）
        dedup_count: 去重丢弃的候选数
        conflict_count: 冲突裁决处数
        merge_summary: 融合摘要文本
    """

    final_items: List[Any] = Field(default_factory=list, description="最终风险项列表")
    risk_distribution: Dict[str, int] = Field(
        default_factory=dict, description="风险等级分布"
    )
    overall_risk_level: str = Field(default=RiskLevel.LOW.value, description="整体综合风险等级")
    official_count: int = Field(default=0, description="正式判定项数")
    reference_count: int = Field(default=0, description="参考项数")
    dedup_count: int = Field(default=0, description="去重丢弃数")
    conflict_count: int = Field(default=0, description="冲突裁决处数")
    merge_summary: str = Field(default="", description="融合摘要")


# ======================================================================
# 一、向量检索服务（候选线索，无定级权）
# ======================================================================
async def vector_review_service(
    clauses: List[Any],
    search_rule_templates: bool = True,
    search_standard: bool = True,
    engine: Optional[VectorEngine] = None,
) -> List[Any]:
    """
    向量语义召回服务

    说明：
        将条款向量化后，在 Milvus 规则模板向量集合与标准条款库中检索，
        产出 VectorCandidate 候选列表（仅相似度与疑似风险，无风险等级）。

    参数：
        clauses: 结构化条款列表（ClauseItem）
        search_rule_templates: 是否检索规则模板向量集合
        search_standard: 是否检索标准条款库
        engine: 向量检索引擎（可注入，便于测试）

    返回：
        VectorCandidate 列表

    异常：
        EmbeddingException / MilvusException：向量化或检索失败（可重试）
    """
    engine = engine or vector_engine
    return await engine.review_clauses(
        clauses=clauses,
        search_rule_templates=search_rule_templates,
        search_standard=search_standard,
    )


# ======================================================================
# 二、大模型审查服务（模糊识别 + 示范条款，辅助无定级权）
# ======================================================================
async def llm_review_service(
    clauses: List[Any],
    contract_context: Optional[Dict[str, Any]] = None,
    is_deep: bool = False,
    enable_clause_recommend: bool = True,
    engine: Optional[LLMEngine] = None,
    recommend_engine: Optional[ClauseRecommendEngine] = None,
) -> LLMReviewResult:
    """
    大模型辅助审查服务

    说明：
        调用大模型做模糊风险识别（LLMCandidate），并可选调用条款推荐引擎
        生成示范修改条款。失败降级为空候选，不中断主流程。

    参数：
        clauses: 结构化条款列表
        contract_context: 合同上下文（标题/编号/类型/金额等）
        is_deep: 是否深度复核通道
        enable_clause_recommend: 是否启用示范条款推荐
        engine: 大模型审查引擎（可注入）
        recommend_engine: 条款推荐引擎（可注入）

    返回：
        LLMReviewResult（failed=True 表示降级失败，candidates 为空）
    """
    engine = engine or llm_engine
    result = await engine.review_clauses(
        clauses=clauses,
        contract_context=contract_context,
        is_deep=is_deep,
    )

    # 示范条款推荐：基于识别出的疑似风险候选生成示范修改条款
    if enable_clause_recommend and result.candidates and not result.failed:
        recommend_engine = recommend_engine or clause_recommend_engine
        requests = recommend_engine.build_requests_from_risk_items(result.candidates)
        if requests:
            try:
                recommendations = await recommend_engine.recommend_clauses(requests)
                # 将示范条款文本回填到对应候选的 suggestion_text 上
                result = _attach_recommendations(result, recommendations)
            except Exception as exc:  # noqa: BLE001 - 推荐失败不影响主流程
                logger.warning("示范条款推荐失败，跳过 | 异常=%s", exc)

    return result


def _attach_recommendations(
    result: LLMReviewResult,
    recommendations: List[ClauseRecommendation],
) -> LLMReviewResult:
    """
    将示范条款推荐结果回填到候选的修改建议上

    说明：
        推荐引擎产出的示范条款（source=llm 或降级来源）优先作为候选的
        修改建议初稿；若候选已有建议文本，则以示范条款增强之。

    参数：
        result: 大模型审查结果
        recommendations: 示范条款推荐列表

    返回：
        回填后的 LLMReviewResult
    """
    # 建立 clause_id → 推荐文本 映射
    rec_map: Dict[str, str] = {}
    for rec in recommendations:
        if rec.clause_id and rec.recommended_text:
            rec_map[rec.clause_id] = rec.recommended_text

    if not rec_map:
        return result

    for candidate in result.candidates:
        clause_id = candidate.clause_id
        if clause_id and clause_id in rec_map:
            recommended = rec_map[clause_id]
            # 示范条款优先作为修改建议初稿
            candidate.suggestion_text = recommended[: settings.SUGGESTION_MAX_LENGTH]

    return result


# ======================================================================
# 三、风险融合服务（规则引擎为准兜底判定）
# ======================================================================
def risk_merge_service(
    rule_results: List[Any],
    vector_results: List[Any],
    llm_results: List[Any],
) -> MergeResult:
    """
    三路结果融合服务（规则引擎为准兜底判定）

    【融合规则】
        1. 以规则引擎命中（rule_results）为骨架，这是唯一有权定级的来源
        2. 向量/大模型候选若与规则命中去重（同 clause_id），则丢弃
        3. 未被规则覆盖的向量/大模型候选转为"参考项"（reference_only=True，
           统一最低等级 low，仅作人工复核参考，不参与定级）
        4. 排序：规则命中项（等级降序）> 向量参考项 > 大模型参考项
        5. 整体综合风险 = 规则命中项的最高风险等级（保守从严，规则引擎为准）

    【架构红线】
        本函数绝不生成新的风险等级——所有风险等级均复制自 rule_results。
        向量/大模型候选的"等级"字段（如有）一律丢弃。

    参数：
        rule_results: 规则引擎命中项（RiskItem，含 risk_level）
        vector_results: 向量召回候选（VectorCandidate）
        llm_results: 大模型候选（LLMCandidate）

    返回：
        MergeResult
    """
    # 延迟导入，避免与 graph 包的循环依赖
    from app.graph.state import RiskItem

    # -------------------- 步骤一：以规则命中为骨架 --------------------
    final_items: List[Any] = list(rule_results)

    covered_clause_ids: set = {
        item.clause_id for item in final_items if item.clause_id
    }

    conflict_count = 0
    reference_count = 0
    dedup_count = 0

    # -------------------- 步骤二：合并大模型候选为参考项 --------------------
    for candidate in llm_results:
        if candidate.clause_id and candidate.clause_id in covered_clause_ids:
            dedup_count += 1
            continue

        final_items.append(
            RiskItem(
                risk_id=f"M{len(final_items) + 1:04d}",
                rule_code=None,
                rule_name=f"AI 疑似风险：{candidate.risk_type or '未分类'}",
                clause_id=candidate.clause_id,
                clause_type=candidate.clause_type,
                evidence_text=candidate.evidence_text,
                char_start=candidate.char_start,
                char_end=candidate.char_end,
                page_no=candidate.page_no,
                risk_level=RiskLevel.LOW.value,
                decision=RiskHitDecision.PASS.value,
                severity=None,
                suggestion_text=candidate.suggestion_text,
                legal_basis="",
                reference_only=True,
                matched_sources=["llm_candidate"],
                conflict_flag=False,
                resolution_note=(
                    f"大模型疑似风险候选，置信度="
                    f"{candidate.confidence if candidate.confidence is not None else '未提供'}。"
                    f"未匹配到任何规则模板，仅作人工复核参考，不参与定级。"
                ),
            )
        )
        reference_count += 1

    # -------------------- 步骤三：合并向量召回候选为参考项 --------------------
    for candidate in vector_results:
        if candidate.clause_id and candidate.clause_id in covered_clause_ids:
            dedup_count += 1
            continue

        final_items.append(
            RiskItem(
                risk_id=f"V{len(final_items) + 1:04d}",
                rule_code=None,
                rule_name=f"标准条款差异：{candidate.matched_clause_title or '标准条款'}",
                clause_id=candidate.clause_id,
                clause_type=None,
                evidence_text=candidate.evidence_text,
                char_start=candidate.char_start,
                char_end=candidate.char_end,
                page_no=candidate.page_no,
                risk_level=RiskLevel.LOW.value,
                decision=RiskHitDecision.PASS.value,
                severity=None,
                suggestion_text=candidate.suggestion_text,
                legal_basis="",
                reference_only=True,
                matched_sources=["vec_candidate"],
                conflict_flag=False,
                resolution_note=(
                    f"与标准条款《{candidate.matched_clause_title or '未命名'}》"
                    f"的余弦相似度={candidate.similarity:.4f}，低于标准文本相似度。"
                    f"仅作对照参考，不参与定级。"
                ),
            )
        )
        reference_count += 1

    # -------------------- 步骤四：冲突裁决（同条款多规则命中取最高） --------------------
    clause_level_map: Dict[str, List[Any]] = {}
    for item in final_items:
        if not item.clause_id or item.reference_only:
            continue
        clause_level_map.setdefault(item.clause_id, []).append(item)

    for clause_id, items in clause_level_map.items():
        if len(items) <= 1:
            continue

        levels = [item.risk_level for item in items]
        max_level = max(levels, key=lambda level: RISK_LEVEL_ORDER.get(level, 0))

        if len(set(levels)) > 1:
            conflict_count += 1
            for item in items:
                item.conflict_flag = True
                item.resolution_note = (
                    f"同一条款被多条规则命中的等级存在分歧 "
                    f"{sorted(set(levels))}，已按保守从严原则取最高等级 "
                    f"【{max_level}】。等级由规则引擎定级映射表产出，未引入模型判断。"
                )

        # 参考项与规则命中并存：标注规则优先
        reference_items = [
            item
            for item in final_items
            if item.clause_id == clause_id and item.reference_only
        ]
        for item in reference_items:
            item.conflict_flag = True
            item.resolution_note += (
                " 该条款已有规则引擎正式判定，依据'规则优先'原则，"
                "参考项不影响最终等级。"
            )

    # -------------------- 步骤五：整体综合风险等级（规则引擎为准取最高） --------------------
    official_items = [item for item in final_items if not item.reference_only]
    if official_items:
        overall_risk_level = max(
            (item.risk_level for item in official_items),
            key=lambda level: RISK_LEVEL_ORDER.get(level, 0),
        )
    else:
        overall_risk_level = RiskLevel.LOW.value

    # -------------------- 步骤六：重算风险等级分布 --------------------
    distribution: Dict[str, int] = {level: 0 for level in settings.RULE_RISK_LEVELS}
    for item in official_items:
        distribution[item.risk_level] = distribution.get(item.risk_level, 0) + 1

    # -------------------- 步骤七：排序（规则 > 向量 > 大模型） --------------------
    final_items.sort(key=_risk_sort_key)

    # -------------------- 步骤八：融合摘要 --------------------
    official_count = len(official_items)
    merge_summary = (
        f"多路召回融合完成：规则命中 {official_count} 项（参与定级），"
        f"参考项 {reference_count} 项（不参与定级），"
        f"去重 {dedup_count} 项，冲突裁决 {conflict_count} 处。"
        f"整体综合风险等级【{overall_risk_level}】以规则引擎为准，"
        f"向量/大模型候选未参与定级。"
    )

    return MergeResult(
        final_items=final_items,
        risk_distribution=distribution,
        overall_risk_level=overall_risk_level,
        official_count=official_count,
        reference_count=reference_count,
        dedup_count=dedup_count,
        conflict_count=conflict_count,
        merge_summary=merge_summary,
    )


def _risk_sort_key(item: Any) -> tuple:
    """
    风险项排序键：规则命中项在前（等级降序），参考项在后

    说明：
        排序优先级：规则 > 向量 > 大模型。
        具体规则：
            - 非参考项（规则命中）排在参考项之前
            - 非参考项内部按风险等级降序（critical > high > medium > low）
            - 参考项内部按来源排序（vec_candidate > llm_candidate），
              再按相似度/置信度降序

    参数：
        item: RiskItem

    返回：
        排序键元组
    """
    if not item.reference_only:
        # 正式判定项：等级降序（数值越大越靠前，取负号实现降序）
        level_order = RISK_LEVEL_ORDER.get(item.risk_level, 0)
        return (0, -level_order, item.rule_code or "")

    # 参考项：向量(1) 优先于 大模型(2)
    source = item.matched_sources[0] if item.matched_sources else ""
    source_rank = 1 if source == "vec_candidate" else 2
    return (1, source_rank, item.rule_name or "")
