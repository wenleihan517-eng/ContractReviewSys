# D:/ContractReviewSys-合同审批智能审查项目/backend/app/engines/vector_engine.py
"""
向量检索引擎 — 合同条款语义召回（候选线索，无定级权）

【模块定位】
    本引擎封装"条款向量化 + Milvus 语义召回"的完整能力，
    是 vector_review 节点背后的专项引擎。

    职责：
        1. 将合同条款文本分块向量化（BGE-M3，委托 embedding_client）
        2. 在 Milvus 规则模板向量集合中检索语义相近的风险规则
        3. 在 Milvus 标准条款库中检索相似标准条款（条款推荐数据源）
        4. 产出 VectorCandidate 候选列表（仅相似度与疑似风险，无风险等级）

【架构红线（代码级强制）】
    本引擎的产物 VectorCandidate 在数据结构上**不含** risk_level /
    severity / decision 字段。相似度高只代表"与某风险规则语义接近"，
    不代表"构成风险"。是否构成风险、等级如何，必须由规则引擎依据
    规则模板判定。本引擎绝不输出任何正式判定结论。

    因此：
        - 本引擎输出的候选仅作"参考线索"与"标准条款对照"，
          在 risk_merge 阶段以 reference_only=True 保留，不参与定级。

返回值说明：
    - embed_texts()：批量向量化，返回 List[List[float]]
    - search_rule_templates()：检索规则模板向量，返回 List[VectorCandidate]
    - search_standard_clauses()：检索标准条款库，返回 List[VectorCandidate]
    - review_clauses()：一体化召回入口，返回 List[VectorCandidate]

使用示例：
    candidates = await vector_engine.review_clauses(clauses)
"""

import asyncio
from typing import Any, Dict, List, Optional

from app.core.config import settings
from app.core.exceptions import EmbeddingException, MilvusException
from app.core.logging_config import get_logger
from app.engines.embedding_client import EmbeddingClient, embedding_client

logger = get_logger(__name__)


class VectorEngine:
    """
    向量检索引擎

    属性：
        _embedder: 向量化客户端（可注入，便于测试隔离外部依赖）
        _milvus_getter: Milvus 客户端获取函数（可注入）
    """

    def __init__(
        self,
        embedder: Optional[EmbeddingClient] = None,
        milvus_getter: Optional[Any] = None,
    ) -> None:
        """
        初始化向量检索引擎

        参数：
            embedder: 向量化客户端，缺省使用全局 embedding_client 单例
            milvus_getter: 返回 MilvusClient 或 None 的获取函数。
                           缺省使用 app.storage.milvus_manager.get_client
        """
        self._embedder = embedder
        self._milvus_getter = milvus_getter

    # ------------------------------------------------------------------
    # 一、向量化
    # ------------------------------------------------------------------
    async def embed_texts(self, texts: List[str]) -> List[List[float]]:
        """
        批量文本向量化（委托 BGE-M3 客户端）

        说明：
            文本列表为空或全部为空白时返回空列表，不发起无意义请求。
            向量化失败时抛 EmbeddingException，由调用方决定重试或降级。

        参数：
            texts: 待向量化的文本列表

        返回：
            向量列表，顺序与入参严格一一对应

        异常：
            EmbeddingException：向量化服务调用失败
        """
        cleaned = [text.strip() for text in texts if text and text.strip()]
        if not cleaned:
            return []

        embedder = self._embedder or embedding_client
        vectors = await embedder.embed_in_batches(cleaned)

        if len(vectors) != len(cleaned):
            raise EmbeddingException(
                message="向量化返回条数与输入条数不一致",
                detail=f"期望={len(cleaned)}, 实际={len(vectors)}",
            )
        return vectors

    # ------------------------------------------------------------------
    # 二、Milvus 检索（规则模板向量 / 标准条款库）
    # ------------------------------------------------------------------
    async def review_clauses(
        self,
        clauses: List[Any],
        search_rule_templates: bool = True,
        search_standard: bool = True,
    ) -> List[Any]:
        """
        一体化向量召回入口

        说明：
            将条款向量化后，分别在规则模板向量集合与标准条款库中检索，
            汇总为 VectorCandidate 列表（候选线索，无风险等级）。

        参数：
            clauses: 结构化条款列表（ClauseItem）
            search_rule_templates: 是否检索规则模板向量集合
            search_standard: 是否检索标准条款库

        返回：
            VectorCandidate 列表（延迟导入，避免与 graph 包循环依赖）

        异常：
            EmbeddingException：向量化失败
            MilvusException / IOError：检索失败
        """
        # 过滤出有效条款
        valid_clauses = [clause for clause in clauses if (clause.content or "").strip()]
        if not valid_clauses:
            return []

        texts = [clause.content for clause in valid_clauses]
        vectors = await self.embed_texts(texts)

        if not vectors:
            return []

        candidates: List[Any] = []

        if search_rule_templates:
            rule_candidates = await self.search_rule_templates(valid_clauses, vectors)
            candidates.extend(rule_candidates)

        if search_standard:
            standard_candidates = await self.search_standard_clauses(valid_clauses, vectors)
            candidates.extend(standard_candidates)

        logger.info(
            "向量召回完成 | 条款数=%d | 规则模板候选=%d | 标准条款候选=%d | 合计=%d",
            len(valid_clauses),
            sum(1 for c in candidates if getattr(c, "matched_clause_id", None)),
            sum(1 for c in candidates if not getattr(c, "matched_clause_id", None)),
            len(candidates),
        )
        return candidates

    async def search_rule_templates(
        self,
        clauses: List[Any],
        vectors: List[List[float]],
    ) -> List[Any]:
        """
        在 Milvus 规则模板向量集合中检索语义相近的风险规则

        说明：
            匹配"风险规则向量"是 vector_review 的核心动作——找到与条款
            语义接近的风险规则描述，作为"疑似风险线索"输出。
            命中相似度高于阈值才保留，避免噪声污染参考项列表。

        参数：
            clauses: 有效条款列表（与 vectors 严格同序）
            vectors: 条款向量列表

        返回：
            VectorCandidate 列表（候选线索，无风险等级）
        """
        return await self._search_collection(
            clauses=clauses,
            vectors=vectors,
            collection=settings.MILVUS_COLLECTION_RULE_TEMPLATE,
            output_fields=["clause_id", "clause_type", "content"],
            candidate_kind="rule_template",
        )

    async def search_standard_clauses(
        self,
        clauses: List[Any],
        vectors: List[List[float]],
    ) -> List[Any]:
        """
        在 Milvus 标准条款库中检索语义相近的标准条款

        说明：
            标准条款库是条款推荐引擎的数据源。命中标准条款后，
            可据此生成示范修改条款，供人工对照核查。

        参数：
            clauses: 有效条款列表（与 vectors 严格同序）
            vectors: 条款向量列表

        返回：
            VectorCandidate 列表（候选线索，无风险等级）
        """
        return await self._search_collection(
            clauses=clauses,
            vectors=vectors,
            collection=settings.MILVUS_COLLECTION_STANDARD_CLAUSE,
            # 【字段对齐说明】标准条款库集合与条款/规则集合共用同一套 schema
            # （contract_id / clause_id / clause_type / content / embedding），
            # 此处必须用 content（条款正文），而非 clause_title/clause_text
            # —— 后两者不在 schema 中，检索会报 field not exist。
            output_fields=["clause_id", "clause_type", "content"],
            candidate_kind="standard_clause",
        )

    async def _search_collection(
        self,
        clauses: List[Any],
        vectors: List[List[float]],
        collection: str,
        output_fields: List[str],
        candidate_kind: str,
    ) -> List[Any]:
        """
        在指定 Milvus 集合中执行语义检索（内部通用实现）

        参数：
            clauses: 有效条款列表
            vectors: 条款向量列表
            collection: 集合名
            output_fields: 检索时回显的字段
            candidate_kind: 候选类型标识（rule_template / standard_clause）

        返回：
            VectorCandidate 列表

        异常：
            MilvusException：客户端未初始化
            IOError：检索失败（可重试）
        """
        client = self._get_milvus_client()
        if client is None:
            raise MilvusException(message="Milvus 客户端未初始化，向量检索不可用")

        from app.storage import milvus_manager

        search_params = milvus_manager.build_search_params()
        top_k = settings.MILVUS_SEARCH_TOP_K
        min_similarity = settings.RULE_SEMANTIC_RECALL_MIN_SIMILARITY

        candidates: List[Any] = []
        candidate_seq = 0  # 全局候选序号，保证 candidate_id 唯一

        for index, vector in enumerate(vectors):
            if index >= len(clauses):
                break
            clause = clauses[index]

            try:
                raw_results = await asyncio.to_thread(
                    client.search,
                    collection_name=collection,
                    data=[vector],
                    limit=top_k,
                    output_fields=output_fields,
                    search_params=search_params,
                )
            except Exception as exc:  # noqa: BLE001 - 检索失败转为可重试异常
                logger.error(
                    "Milvus 检索失败 | 集合=%s | 条款=%s | 异常=%s",
                    collection,
                    getattr(clause, "clause_id", ""),
                    exc,
                )
                raise IOError(f"Milvus 集合 {collection} 检索失败：{exc}") from exc

            if not raw_results:
                continue

            for hit in raw_results[0]:
                candidate = self._build_candidate(
                    clause=clause,
                    hit=hit,
                    candidate_kind=candidate_kind,
                    min_similarity=min_similarity,
                    seq=candidate_seq,
                )
                if candidate is not None:
                    candidates.append(candidate)
                    candidate_seq += 1

        logger.info(
            "集合检索完成 | 集合=%s | 候选类型=%s | 候选数=%d",
            collection,
            candidate_kind,
            len(candidates),
        )
        return candidates

    def _build_candidate(
        self,
        clause: Any,
        hit: Dict[str, Any],
        candidate_kind: str,
        min_similarity: float,
        seq: int,
    ) -> Optional[Any]:
        """
        将单条 Milvus 命中结果构造为 VectorCandidate

        说明：
            相似度低于阈值的候选直接丢弃，避免噪声。
            候选的 candidate_id 按种类使用不同前缀（R=规则模板，S=标准条款），
            便于前端区分来源；seq 为全局候选序号，保证唯一。

        参数：
            clause: 命中条款
            hit: Milvus 命中结果
            candidate_kind: 候选类型
            min_similarity: 最低相似度阈值
            seq: 全局候选序号

        返回：
            VectorCandidate；低于阈值或实体异常时返回 None
        """
        from app.graph.state import VectorCandidate

        similarity = float(hit.get("distance", 0.0))
        if similarity < min_similarity:
            return None

        entity = hit.get("entity", {}) or {}

        if candidate_kind == "rule_template":
            # 规则模板向量集合：content 存规则描述，clause_id 存规则编码
            rule_code = str(entity.get("clause_id", ""))
            rule_desc = str(entity.get("content", ""))
            prefix = "R"
            matched_title = rule_desc[:50] or rule_code
            suggestion = (
                f"该条款与风险规则《{rule_code or '未命名'}》语义相近（相似度="
                f"{similarity:.4f}），疑似存在相关风险，建议交规则引擎复核"
            )
        else:
            # 标准条款库：content 即标准条款正文（与集合 schema 对齐）
            prefix = "S"
            content_text = str(entity.get("content", ""))
            matched_title = content_text[:50] or "未命名标准条款"
            suggestion = (
                f"该条款与标准条款《{matched_title}》存在差异"
                f"（相似度={similarity:.4f}），建议对照标准文本核查；"
                f"标准条款参考：{content_text[:200]}"
            )

        return VectorCandidate(
            candidate_id=f"V{prefix}{seq:04d}",
            clause_id=getattr(clause, "clause_id", None),
            matched_clause_id=str(hit.get("id", "")),
            matched_clause_title=matched_title,
            similarity=similarity,
            evidence_text=(getattr(clause, "content", "") or "")[:300],
            char_start=getattr(clause, "char_start", None),
            char_end=getattr(clause, "char_end", None),
            page_no=getattr(clause, "page_start", None),
            suggestion_text=suggestion,
        )

    def _get_milvus_client(self) -> Any:
        """
        获取 Milvus 客户端

        说明：
            优先使用注入的获取函数（便于测试注入 fake client），
            否则使用全局 milvus_manager.get_client()。

        返回：
            MilvusClient 实例或 None
        """
        if self._milvus_getter is not None:
            return self._milvus_getter()

        from app.storage import milvus_manager

        return milvus_manager.get_client()


# 全局单例
vector_engine = VectorEngine()
