# D:/ContractReviewSys-合同审批智能审查项目/backend/tests/verify_review_flow.py
"""
风险审查流程端到端验证（向量检索 / 大模型审查 / 风险融合 / 条款推荐）

【验证目标】
    1. vector_engine：条款向量化 + Milvus 检索规则模板/标准条款 → VectorCandidate
    2. llm_engine：模糊风险识别 + 越权字段拦截 + 失败降级不中断主流程
    3. clause_recommend_engine：示范修改条款生成（llm 来源）+ 失败降级
    4. review_service.risk_merge_service：三路融合去重 + 规则>向量>大模型优先级
       + 以规则引擎为准取最高风险等级
    5. 权责红线：VectorCandidate / LLMCandidate 不含 risk_level；
       参考项 reference_only=True 不参与定级

【外部依赖隔离】
    Milvus / Embedding / LLM 均未就绪，通过注入 fake 对象隔离外部依赖。
    被验证的代码路径（检索结果解析、候选构造、越权拦截、降级、融合裁决）
    全部是真实执行的。

【运行方式】
    cd backend && python tests/verify_review_flow.py
"""

import asyncio
import os
import sys

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from app.constants import RISK_LEVEL_ORDER, RiskLevel
from app.engines.clause_recommend_engine import (
    ClauseRecommendEngine,
    ClauseRecommendation,
)
from app.engines.llm_client import LLMResponse
from app.engines.llm_engine import LLMEngine, LLMReviewResult
from app.engines.vector_engine import VectorEngine
from app.graph.state import ClauseItem, LLMCandidate, RiskItem, VectorCandidate
from app.services.review_service import (
    MergeResult,
    llm_review_service,
    risk_merge_service,
    vector_review_service,
)

PASSED = []
FAILED = []


def check(name: str, condition: bool, detail: str = "") -> None:
    """记录单项断言结果"""
    if condition:
        PASSED.append(name)
        print(f"  [PASS] {name}")
    else:
        FAILED.append((name, detail))
        print(f"  [FAIL] {name} | {detail}")


# ======================================================================
# 测试替身（fake 外部依赖）
# ======================================================================

class FakeEmbedder:
    """伪造向量化客户端：返回固定向量"""

    def __init__(self, dim: int = 1024):
        self.dim = dim
        self.calls = 0

    async def embed_in_batches(self, texts):
        self.calls += 1
        return [[0.1] * self.dim for _ in texts]


class FakeMilvusClient:
    """伪造 Milvus 客户端：返回固定的检索命中"""

    def __init__(self, hits=None):
        self._hits = hits or []
        self.search_calls = 0

    def search(self, collection_name, data, limit, output_fields, search_params):
        self.search_calls += 1
        return [self._hits]


class FakeLLM:
    """伪造大模型客户端：返回固定 JSON"""

    def __init__(self, parsed=None, content="", fail=False):
        self._parsed = parsed
        self._content = content
        self._fail = fail
        self.chat_json_calls = 0
        self.chat_calls = 0

    async def chat_json(self, system_prompt, user_prompt, **kwargs):
        self.chat_json_calls += 1
        if self._fail:
            from app.core.exceptions import LLMException

            raise LLMException(message="模拟大模型服务不可用")
        return self._parsed or {"overview": "", "candidates": []}

    async def chat(self, system_prompt, user_prompt, **kwargs):
        self.chat_calls += 1
        if self._fail:
            from app.core.exceptions import LLMException

            raise LLMException(message="模拟大模型服务不可用")
        return LLMResponse(content=self._content)


def make_clauses():
    """构造结构化条款列表（含 char_start/char_end 精确定位）"""
    specs = [
        ("C1", "subject_matter", "第一条 合同标的", "甲方向乙方采购工业机器人本体。"),
        ("C2", "liability", "第二条 违约责任", "任何一方违约概不负责，双方互不追究。"),
        ("C3", "dispute", "第三条 争议解决", "因本合同发生争议，双方应协商解决。"),
    ]
    clauses = []
    cursor = 0
    for index, (cid, ctype, title, content) in enumerate(specs, start=1):
        text = f"{title}\n{content}"
        clauses.append(
            ClauseItem(
                clause_id=cid, clause_index=index, clause_type=ctype,
                clause_title=title, content=text,
                char_start=cursor, char_end=cursor + len(text),
                page_start=1, page_end=1, paragraph_index=index - 1,
            )
        )
        cursor += len(text) + 1
    return clauses


# ======================================================================
# 验证一：向量检索引擎
# ======================================================================

async def test_vector_engine():
    print("\n[验证一] 向量检索引擎（向量化 + 检索 + 候选构造）")
    clauses = make_clauses()

    # 命中：规则模板集合返回 1 条高相似度 + 1 条低相似度
    fake_client = FakeMilvusClient(hits=[
        {"distance": 0.92, "id": "rule_vec_1", "entity": {
            "clause_id": "RISK-001", "content": "单方免责表述", "clause_type": "liability"}},
        {"distance": 0.50, "id": "rule_vec_2", "entity": {
            "clause_id": "RISK-002", "content": "低相似度规则", "clause_type": "liability"}},
    ])
    engine = VectorEngine(
        embedder=FakeEmbedder(),
        milvus_getter=lambda: fake_client,
    )

    candidates = await engine.review_clauses(
        clauses=clauses,
        search_rule_templates=True,
        search_standard=True,
    )

    check("向量化被调用", engine._embedder.calls >= 1)
    check("Milvus 检索被调用", fake_client.search_calls >= len(clauses))

    # 相似度阈值过滤：0.50 低于 RULE_SEMANTIC_RECALL_MIN_SIMILARITY(0.70)，应被过滤
    check("低相似度候选被过滤",
          all(c.similarity >= 0.70 for c in candidates),
          f"相似度={[c.similarity for c in candidates]}")

    # 候选为 VectorCandidate，且不含 risk_level 字段
    check("候选为 VectorCandidate 类型",
          all(isinstance(c, VectorCandidate) for c in candidates))
    check("VectorCandidate 数据结构不含 risk_level 字段（红线）",
          "risk_level" not in VectorCandidate.model_fields)
    check("VectorCandidate 数据结构不含 decision 字段（红线）",
          "decision" not in VectorCandidate.model_fields)
    check("VectorCandidate 数据结构不含 severity 字段（红线）",
          "severity" not in VectorCandidate.model_fields)

    # 候选携带相似度与证据
    if candidates:
        check("候选携带相似度", all(0.0 <= c.similarity <= 1.0 for c in candidates))
        check("候选携带证据文本", all(c.evidence_text for c in candidates))
        check("候选携带条款关联", all(c.clause_id for c in candidates))

    return candidates


# ======================================================================
# 验证二：大模型审查引擎
# ======================================================================

async def test_llm_engine():
    print("\n[验证二] 大模型审查引擎（模糊识别 + 越权拦截 + 降级）")
    clauses = make_clauses()

    # 模型返回含越权字段 risk_level / decision 的候选
    parsed = {
        "overview": "整体风险一般",
        "candidates": [
            {
                "clause_id": "C2",
                "risk_type": "付款条件模糊",
                "evidence_text": "任何一方违约概不负责",
                "confidence": 0.85,
                "suggestion_text": "建议明确违约责任",
                "reason": "免责表述过于绝对",
                # 越权字段：模型不应输出，但若输出应被丢弃
                "risk_level": "high",
                "decision": "block",
                "severity": "fatal",
            },
            {
                "clause_id": "NOT_EXIST",  # 非法条款 ID
                "risk_type": "测试",
                "evidence_text": "不存在条款",
                "confidence": 0.5,
            },
        ],
    }
    fake_llm = FakeLLM(parsed=parsed)
    engine = LLMEngine(llm=fake_llm)

    result = await engine.review_clauses(clauses=clauses, contract_context={})

    check("返回 LLMReviewResult", isinstance(result, LLMReviewResult))
    check("未降级失败", result.failed is False)
    # 非法 clause_id 的候选保留但 clause_id 置空（与既有语义一致，避免误丢）
    check("识别出候选（非法 clause_id 置空保留）", len(result.candidates) == 2,
          f"实际={len(result.candidates)}")
    check("非法 clause_id 被置空",
          any(c.clause_id is None for c in result.candidates),
          f"clause_ids={[c.clause_id for c in result.candidates]}")

    # 越权字段被丢弃
    check("越权字段 risk_level 被丢弃", "risk_level" in result.dropped_fields,
          f"丢弃字段={result.dropped_fields}")
    check("越权字段 decision 被丢弃", "decision" in result.dropped_fields)
    check("越权字段 severity 被丢弃", "severity" in result.dropped_fields)

    # 候选为 LLMCandidate，且不含 risk_level
    check("候选为 LLMCandidate 类型",
          all(isinstance(c, LLMCandidate) for c in result.candidates))
    check("LLMCandidate 数据结构不含 risk_level 字段（红线）",
          "risk_level" not in LLMCandidate.model_fields)

    # 置信度兜底
    if result.candidates:
        check("置信度范围兜底", 0.0 <= result.candidates[0].confidence <= 1.0)

    # -------------------- 失败降级：不中断主流程 --------------------
    fail_llm = FakeLLM(fail=True)
    fail_engine = LLMEngine(llm=fail_llm)
    fail_result = await fail_engine.review_clauses(clauses=clauses, contract_context={})

    check("大模型失败时降级（failed=True）", fail_result.failed is True)
    check("降级后返回空候选", len(fail_result.candidates) == 0)
    check("降级不抛异常（不中断主流程）", True, "已正常返回 LLMReviewResult")

    return result


# ======================================================================
# 验证三：条款推荐引擎
# ======================================================================

async def test_clause_recommend_engine():
    print("\n[验证三] 条款推荐引擎（示范条款生成 + 降级）")

    # 成功场景：llm 返回示范条款
    fake_llm = FakeLLM(content="双方应依法承担各自的违约责任，违约金不超过合同总额的30%。")
    engine = ClauseRecommendEngine(llm=fake_llm)

    rec = await engine.recommend_clause(
        clause_content="任何一方违约概不负责",
        risk_description="存在单方免责表述",
        standard_clause="",
        legal_basis="民法典第四百九十七条",
        clause_id="C2",
        risk_code="RISK-001",
    )

    check("返回 ClauseRecommendation", isinstance(rec, ClauseRecommendation))
    check("示范条款来源为 llm", rec.source == "llm", f"实际={rec.source}")
    check("示范条款文本非空", bool(rec.recommended_text))
    check("携带法律依据", bool(rec.legal_basis))
    check("携带原条款关联", rec.clause_id == "C2")

    # 失败降级场景：llm 不可用，降级为标准条款
    fail_llm = FakeLLM(fail=True)
    fail_engine = ClauseRecommendEngine(llm=fail_llm)
    rec2 = await fail_engine.recommend_clause(
        clause_content="任何一方违约概不负责",
        risk_description="存在单方免责表述",
        standard_clause="双方应承担对等的违约责任。",
        legal_basis="民法典",
    )
    check("大模型失败降级为标准条款", rec2.source == "standard_clause",
          f"实际={rec2.source}")
    check("降级后仍有示范文本", bool(rec2.recommended_text))

    # 双重降级：无标准条款时用模板
    rec3 = await fail_engine.recommend_clause(
        clause_content="任何一方违约概不负责",
        risk_description="存在单方免责表述",
        standard_clause="",
        legal_basis="",
    )
    check("无标准条款时降级为模板", rec3.source == "template", f"实际={rec3.source}")

    return rec


# ======================================================================
# 验证四：风险融合服务
# ======================================================================

def test_risk_merge():
    print("\n[验证四] 风险融合服务（三路去重 + 规则优先 + 最高等级）")

    # 规则引擎命中：C1(high)、C2(critical)
    rule_results = [
        RiskItem(risk_id="R1", rule_code="PUR-001", rule_name="缺质量保证",
                 clause_id="C1", risk_level=RiskLevel.HIGH.value),
        RiskItem(risk_id="R2", rule_code="RISK-001", rule_name="单方免责",
                 clause_id="C2", risk_level=RiskLevel.CRITICAL.value,
                 suggestion_text="建议修改", legal_basis="民法典497条"),
    ]

    # 向量候选：C3（未与规则命中去重）
    vector_results = [
        VectorCandidate(candidate_id="V1", clause_id="C3",
                        matched_clause_title="标准争议解决条款",
                        similarity=0.88, evidence_text="因本合同发生争议"),
    ]

    # 大模型候选：C2（与规则命中去重）+ C4（新增）
    llm_results = [
        LLMCandidate(candidate_id="L1", clause_id="C2", risk_type="免责表述",
                     evidence_text="任何一方违约概不负责", confidence=0.9),
        LLMCandidate(candidate_id="L2", clause_id="C4", risk_type="付款模糊",
                     evidence_text="付款条件待明确", confidence=0.7),
    ]

    result = risk_merge_service(
        rule_results=rule_results,
        vector_results=vector_results,
        llm_results=llm_results,
    )

    check("返回 MergeResult", isinstance(result, MergeResult))

    # 正式判定项数 = 2（规则命中）
    check("正式判定项数为 2", result.official_count == 2,
          f"实际={result.official_count}")

    # 参考项：C4(llm) + C3(vector) = 2；C2(llm) 与规则去重丢弃
    check("参考项数为 2", result.reference_count == 2,
          f"实际={result.reference_count}")
    check("去重 1 项（C2 llm 与规则命中去重）", result.dedup_count == 1,
          f"实际={result.dedup_count}")

    # 整体综合风险 = 规则命中的最高等级 critical（规则引擎为准）
    check("整体综合风险为 critical（规则引擎为准取最高）",
          result.overall_risk_level == RiskLevel.CRITICAL.value,
          f"实际={result.overall_risk_level}")

    # 参考项 reference_only=True 且 risk_level=low（不参与定级）
    ref_items = [i for i in result.final_items if i.reference_only]
    check("参考项均 reference_only=True", all(i.reference_only for i in ref_items))
    check("参考项统一最低等级 low（不参与定级）",
          all(i.risk_level == RiskLevel.LOW.value for i in ref_items))

    # 排序：规则命中项在前（等级降序），参考项在后
    official_positions = [i for i, item in enumerate(result.final_items) if not item.reference_only]
    ref_positions = [i for i, item in enumerate(result.final_items) if item.reference_only]
    check("规则命中项排在参考项之前（规则优先）",
          all(op < rp for op in official_positions for rp in ref_positions),
          f"正式位置={official_positions}, 参考位置={ref_positions}")

    # 正式判定项按等级降序
    official_levels = [result.final_items[i].risk_level for i in official_positions]
    check("正式判定项按风险等级降序",
          official_levels == sorted(official_levels, key=lambda l: RISK_LEVEL_ORDER[l], reverse=True),
          f"实际={official_levels}")

    # 风险分布：只统计正式判定项
    check("风险分布只统计正式判定项",
          result.risk_distribution.get("critical", 0) == 1
          and result.risk_distribution.get("high", 0) == 1,
          f"实际={result.risk_distribution}")

    return result


# ======================================================================
# 验证五：独立函数可单独调用（review_service 三个入口）
# ======================================================================

async def test_service_functions():
    print("\n[验证五] review_service 三个独立服务函数")

    clauses = make_clauses()

    # 1. vector_review_service 独立调用
    fake_client = FakeMilvusClient(hits=[
        {"distance": 0.9, "id": "r1", "entity": {
            "clause_id": "RISK-001", "content": "单方免责", "clause_type": "liability"}},
    ])
    vec_candidates = await vector_review_service(
        clauses=clauses,
        engine=VectorEngine(embedder=FakeEmbedder(), milvus_getter=lambda: fake_client),
    )
    check("vector_review_service 独立调用成功", len(vec_candidates) >= 0)

    # 2. llm_review_service 独立调用
    fake_llm = FakeLLM(parsed={"overview": "", "candidates": []})
    llm_result = await llm_review_service(
        clauses=clauses,
        contract_context={"contract_title": "测试合同", "contract_type": "purchase"},
        engine=LLMEngine(llm=fake_llm),
    )
    check("llm_review_service 独立调用成功", isinstance(llm_result, LLMReviewResult))

    # 3. risk_merge_service 独立调用（同步函数）
    merge = risk_merge_service(
        rule_results=[],
        vector_results=[],
        llm_results=[],
    )
    check("risk_merge_service 独立调用成功", isinstance(merge, MergeResult))
    check("空输入时整体风险为 low", merge.overall_risk_level == RiskLevel.LOW.value)


# ======================================================================
# 验证六：权责红线（数据结构级强制）
# ======================================================================

def test_redline():
    print("\n[验证六] 权责红线（数据结构级强制）")

    # 1. VectorCandidate / LLMCandidate 不含风险等级字段
    check("VectorCandidate 不含 risk_level", "risk_level" not in VectorCandidate.model_fields)
    check("LLMCandidate 不含 risk_level", "risk_level" not in LLMCandidate.model_fields)
    check("LLMCandidate 不含 decision", "decision" not in LLMCandidate.model_fields)
    check("LLMCandidate 不含 severity", "severity" not in LLMCandidate.model_fields)

    # 2. RiskItem 的 judge_source 强制 rule_engine
    check("RiskItem 含 judge_source 字段", "judge_source" in RiskItem.model_fields)

    # 3. VectorCandidate 传入 risk_level 会被忽略（Pydantic 默认 extra=ignore）
    candidate = VectorCandidate(candidate_id="VX", clause_id="C1", risk_level="high")
    check("VectorCandidate 忽略传入的 risk_level 字段",
          not hasattr(candidate, "risk_level"),
          "risk_level 字段未被忽略或未拒绝")


# ======================================================================
# 主流程
# ======================================================================

async def main() -> int:
    print("=" * 72)
    print("风险审查流程端到端验证")
    print("=" * 72)

    await test_vector_engine()
    await test_llm_engine()
    await test_clause_recommend_engine()
    test_risk_merge()
    await test_service_functions()
    test_redline()

    print("\n" + "=" * 72)
    print(f"结果：通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    if FAILED:
        print("-" * 72)
        for name, detail in FAILED:
            print(f"  [FAIL] {name} | {detail}")
    print("=" * 72)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
