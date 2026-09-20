# D:/ContractReviewSys-合同审批智能审查项目/backend/app/engines/__init__.py
"""
能力引擎层统一出口 — 主从式架构中的"专项模块"

【架构定位】
    本层承载系统全部专项能力，由 L3 编排层（LangGraph）作为主调度器调用。
    各引擎职责单一、互不耦合，可独立测试与独立重试。

引擎清单与权责：
    ┌────────────────────┬──────────────────────────────┬──────────────┐
    │ 引擎                │ 职责                          │ 有无判定权    │
    ├────────────────────┼──────────────────────────────┼──────────────┤
    │ rule_engine        │ 定级 / 阻塞判定 / 最终结论      │ ✅ 唯一判定方 │
    │ llm_client         │ 语义解析 / 风险候选 / 建议初稿  │ ❌ 仅辅助     │
    │ embedding_client   │ BGE-M3 向量化                  │ ❌ 仅特征提取 │
    └────────────────────┴──────────────────────────────┴──────────────┘

【红线声明（代码级强制）】
    1. 风险等级定级、违规判定、任务阻塞判定、最终审查结论
       —— 只能由 rule_engine 产出，llm_client 无任何判定接口。
    2. llm_client 的返回结构统一为"候选"语义，不提供等级字段写入能力。
    3. 结论写入前必须校验 decision_source == "rule_engine"。

返回值说明：
    本模块仅做聚合导出，无函数返回值

使用示例：
    from app.engines import rule_engine, llm_client, embedding_client
"""

from app.engines.clause_recommend_engine import (
    ClauseRecommendEngine,
    ClauseRecommendation,
    clause_recommend_engine,
)
from app.engines.embedding_client import EmbeddingClient, embedding_client
from app.engines.llm_client import LLMClient, LLMResponse, llm_client
from app.engines.llm_engine import LLMEngine, LLMReviewResult, llm_engine
from app.engines.rule_engine import (
    MatchResult,
    ReviewOutcome,
    RuleDecisionResult,
    RuleEngine,
    RuleHitInput,
    RuleMatcher,
    RuleRegistry,
    rule_engine,
    rule_registry,
)
from app.engines.vector_engine import VectorEngine, vector_engine

__all__ = [
    # 规则引擎（唯一判定方）
    "rule_engine",
    "RuleEngine",
    "RuleDecisionResult",
    "RuleHitInput",
    "MatchResult",
    "ReviewOutcome",
    "RuleMatcher",
    "rule_registry",
    "RuleRegistry",
    # 大模型客户端（辅助能力）
    "llm_client",
    "LLMClient",
    "LLMResponse",
    # 大模型审查引擎（辅助能力，模糊识别 + 建议初稿）
    "llm_engine",
    "LLMEngine",
    "LLMReviewResult",
    # 向量化客户端（特征提取）
    "embedding_client",
    "EmbeddingClient",
    # 向量检索引擎（候选线索，无定级权）
    "vector_engine",
    "VectorEngine",
    # 条款推荐引擎（示范修改条款，辅助能力）
    "clause_recommend_engine",
    "ClauseRecommendEngine",
    "ClauseRecommendation",
]
