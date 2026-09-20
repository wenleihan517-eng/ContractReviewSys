# D:/ContractReviewSys-合同审批智能审查项目/backend/app/engines/llm_engine.py
"""
大模型审查引擎 — 模糊语义识别与建议初稿（辅助能力，无定级权）

【模块定位】
    本引擎封装"大模型辅助审查"的完整能力，是 llm_review 节点背后的专项引擎。

    职责：
        1. 构造审查提示词（系统提示词 + 用户提示词）
        2. 调用本地大模型（委托 llm_client，内置超时重试）
        3. 解析模型输出为 LLMCandidate 候选，拦截越权判定字段
        4. 【失败降级】模型调用失败时不抛异常、不中断主流程，
           降级为空候选 + 告警日志（规则引擎才是兜底判定方）

【架构红线（代码级强制）】
    1. 本引擎只做"模糊语义识别"与"修改建议初稿"，**不得输出风险等级**。
    2. 产出结构 LLMCandidate 在数据结构上不含 risk_level / severity /
       decision 字段；模型即使返回这些字段，也会在解析时被丢弃并告警。
    3. 任何产出都不参与定级计算，仅作 risk_merge 阶段的参考项。

【失败降级设计】
    与 vector_review（Milvus 失败可重试）不同，大模型是"辅助"能力：
        - 规则引擎是唯一判定方，即使大模型完全不可用，规则引擎仍能完成
          定级与结论，审查流程不应因大模型故障而整体失败。
        - 因此本引擎调用失败时返回 failed=True 与空候选，
          由 llm_review 节点记录告警后继续主流程，而不是抛异常触发重试。

返回值说明：
    - review_clauses()：返回 LLMReviewResult（含 candidates / summary /
      dropped_fields / failed / error）

使用示例：
    result = await llm_engine.review_clauses(clauses, contract_context)
    if result.failed:
        ...  # 记录告警，主流程继续
"""

from typing import Any, Dict, List, Optional, Set, Tuple

from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.exceptions import LLMException
from app.core.logging_config import get_logger
from app.engines.llm_client import LLMClient, llm_client

logger = get_logger(__name__)


class LLMReviewResult(BaseModel):
    """
    大模型审查结果封装

    字段：
        candidates: 疑似风险候选列表（LLMCandidate）
        summary: 输出摘要（总体印象等，仅供人工参考）
        dropped_fields: 被丢弃的越权判定字段名列表（红线防御记录）
        failed: 是否降级失败（模型调用失败时为 True）
        error: 失败原因摘要（failed=True 时有值）
    """

    candidates: List[Any] = Field(default_factory=list, description="疑似风险候选列表")
    summary: str = Field(default="", description="输出摘要，仅供人工参考")
    dropped_fields: List[str] = Field(
        default_factory=list, description="被丢弃的越权判定字段名"
    )
    failed: bool = Field(default=False, description="是否降级失败")
    error: str = Field(default="", description="失败原因摘要")


class LLMEngine:
    """
    大模型审查引擎

    属性：
        _llm_client: 大模型客户端（可注入，便于测试隔离外部依赖）
    """

    # 越权判定字段黑名单：模型绝无权限输出
    _FORBIDDEN_KEYS = frozenset(
        {
            "risk_level",
            "risk_score",
            "severity",
            "decision",
            "is_blocked",
            "blocked",
            "conclusion",
            "final_risk_level",
            "is_violated",
            "violation",
            "pass",
            "approve",
        }
    )

    # 白名单：仅保留这些字段进入 raw_output，彻底剥离模型可能返回的其它字段
    _RAW_OUTPUT_WHITELIST = frozenset(
        {
            "clause_id",
            "risk_type",
            "evidence_text",
            "confidence",
            "suggestion_text",
            "reason",
        }
    )

    def __init__(self, llm: Optional[LLMClient] = None) -> None:
        """
        初始化大模型审查引擎

        参数：
            llm: 大模型客户端，缺省使用全局 llm_client 单例
        """
        self._llm_client = llm

    # ------------------------------------------------------------------
    # 一、提示词构造
    # ------------------------------------------------------------------
    def build_system_prompt(self, is_deep: bool = False) -> str:
        """
        构造系统提示词

        说明：
            明确告知模型其角色是"辅助识别"，禁止输出风险等级与审批结论。
            深度复核通道追加强化审查清单。

        参数：
            is_deep: 是否为深度复核通道

        返回：
            系统提示词文本
        """
        base_prompt = (
            "你是企业合同审查的辅助分析助手。你的职责是：\n"
            "1. 解析合同条款的语义，理解其法律与商业含义；\n"
            "2. 识别条款中可能存在的疑似风险点（模糊表述、权利义务失衡、"
            "缺失关键要素等）；\n"
            "3. 为疑似风险点撰写修改建议初稿；\n"
            "4. 解读相关合规要求。\n\n"
            "【严格的输出边界】\n"
            "你【不得】输出以下内容，这些判定由企业规则引擎负责：\n"
            "  - 风险等级（如 high / medium / low / critical）\n"
            "  - 违规与否的判定结论\n"
            "  - 合同是否可以审批通过的结论\n"
            "  - 是否阻塞审批流的建议\n"
            "如果你输出了上述字段，它们将被系统直接丢弃。\n\n"
            "【输出格式要求】\n"
            "必须输出一个 JSON 对象，结构如下：\n"
            "{\n"
            '  "overview": "对整份合同条款的整体印象，100 字以内",\n'
            '  "candidates": [\n'
            "    {\n"
            '      "clause_id": "关联条款的 ID，必须来自输入数据",\n'
            '      "risk_type": "疑似风险的类型描述，如\'付款条件模糊\'",\n'
            '      "evidence_text": "支撑该判断的原文片段，必须逐字摘自条款正文",\n'
            '      "confidence": 0.0 到 1.0 之间的数字，表示你的确信程度,\n'
            '      "suggestion_text": "修改建议初稿",\n'
            '      "reason": "简要说明你的判断理由"\n'
            "    }\n"
            "  ]\n"
            "}\n"
            "如果未发现任何疑似风险，candidates 输出空数组。\n"
            "evidence_text 必须逐字摘自原文，禁止自行改写或概括。"
        )

        if not is_deep:
            return base_prompt

        return base_prompt + (
            "\n\n【深度复核强化要求】\n"
            "本次合同属于大额或高风险类型，需要逐项核查以下关键条款，"
            "即使未发现明显问题也要在 reason 中说明核查结论：\n"
            "  1. 担保与连带责任条款：是否存在无限连带责任、担保范围过宽\n"
            "  2. 违约责任条款：违约金比例、赔偿上限、责任是否对等\n"
            "  3. 合同解除与终止条款：解除条件是否单方有利、后果是否明确\n"
            "  4. 争议解决条款：管辖地是否不利、仲裁机构是否明确\n"
            "  5. 付款与结算条款：账期、验收标准、发票义务是否明确\n"
            "  6. 知识产权与保密条款：成果归属、保密期限是否合理\n"
            "  7. 不可抗力与情势变更：范围界定是否过宽\n"
            "对每一项均需给出明确核查意见，发现疑点的写入 candidates。"
        )

    def build_user_prompt(self, contract_context: Dict[str, Any], clauses: List[Any]) -> str:
        """
        构造用户提示词

        说明：
            为避免提示词过长导致截断，条款正文按总长度上限截取，
            并保留条款 ID 与类型信息，保证候选可回填到正确条款。

        参数：
            contract_context: 合同上下文（标题、编号、类型、金额等）
            clauses: 结构化条款列表

        返回：
            用户提示词文本
        """
        max_total_chars = 20000

        lines: List[str] = [
            "请审查以下合同条款。",
            f"合同标题：{contract_context.get('contract_title', '')}",
            f"合同编号：{contract_context.get('contract_no', '')}",
            f"合同类型：{contract_context.get('contract_type', '')}",
            f"业务类型：{contract_context.get('business_type', '')}",
            f"合同金额：{contract_context.get('contract_amount', 0):,.2f} "
            f"{contract_context.get('currency', 'CNY')}",
            f"送审部门：{contract_context.get('submit_dept', '')}",
            f"相对方：{contract_context.get('counterparty', '')}",
            f"履约周期：{contract_context.get('performance_period', '')}",
            "",
            "条款清单如下（JSON 数组）：",
        ]

        items: List[str] = []
        accumulated = 0

        for clause in clauses:
            content = clause.content or ""
            if len(content) > 2000:
                content = content[:2000] + "…（后续内容已截断）"

            if accumulated + len(content) > max_total_chars:
                lines.append(
                    f"（条款总数 {len(clauses)} 条，已达提交上限，后续条款省略）"
                )
                break

            items.append(
                "  {\n"
                f'    "clause_id": "{clause.clause_id}",\n'
                f'    "clause_index": {clause.clause_index},\n'
                f'    "clause_type": "{clause.clause_type}",\n'
                f'    "clause_title": {self._json_escape(clause.clause_title)},\n'
                f'    "content": {self._json_escape(content)}\n'
                "  }"
            )
            accumulated += len(content)

        lines.append("[\n" + ",\n".join(items) + "\n]")
        lines.append("")
        lines.append("请按系统提示词的 JSON 结构输出分析结果。")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 二、核心审查入口（含失败降级）
    # ------------------------------------------------------------------
    async def review_clauses(
        self,
        clauses: List[Any],
        contract_context: Optional[Dict[str, Any]] = None,
        is_deep: bool = False,
    ) -> LLMReviewResult:
        """
        执行大模型辅助审查（模糊风险识别 + 建议初稿）

        【失败降级】
            模型调用失败时**不抛异常**，返回 failed=True 与空候选。
            这是本引擎与 vector_review 的关键区别——大模型是辅助能力，
            故障不应中断以规则引擎为兜底的审查主流程。

        参数：
            clauses: 结构化条款列表
            contract_context: 合同上下文（标题/编号/类型/金额等）
            is_deep: 是否为深度复核通道

        返回：
            LLMReviewResult（failed=True 表示降级失败，candidates 为空）
        """
        if not clauses:
            return LLMReviewResult(
                candidates=[],
                summary="无结构化条款，大模型审查跳过",
                failed=False,
            )

        contract_context = contract_context or {}

        system_prompt = self.build_system_prompt(is_deep=is_deep)
        user_prompt = self.build_user_prompt(contract_context, clauses)

        llm = self._llm_client or llm_client

        try:
            parsed = await llm.chat_json(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )
        except LLMException as exc:
            # 【失败降级】模型不可用时不中断主流程，降级为空候选 + 告警
            logger.error(
                "大模型调用失败，降级为空候选（不中断主流程） | 异常=%s", exc
            )
            return LLMReviewResult(
                candidates=[],
                summary="大模型服务不可用，本次未执行语义辅助审查",
                failed=True,
                error=str(exc)[:500],
            )
        except Exception as exc:  # noqa: BLE001 - 任何异常都降级，不中断主流程
            logger.error(
                "大模型审查出现非预期异常，降级为空候选（不中断主流程） | 异常=%s",
                exc,
                exc_info=True,
            )
            return LLMReviewResult(
                candidates=[],
                summary="大模型审查异常，本次未执行语义辅助审查",
                failed=True,
                error=str(exc)[:500],
            )

        # 解析候选并拦截越权字段
        candidates, dropped_fields = self._parse_candidates(parsed, clauses)

        summary_parts = []
        overview = parsed.get("overview")
        if isinstance(overview, str) and overview.strip():
            summary_parts.append(f"总体印象：{overview.strip()[:300]}")
        summary_parts.append(f"识别疑似风险候选 {len(candidates)} 项")

        if dropped_fields:
            logger.warning(
                "【红线防御】大模型输出包含越权判定字段，已丢弃 | 丢弃字段=%s",
                dropped_fields,
            )

        return LLMReviewResult(
            candidates=candidates,
            summary="；".join(summary_parts),
            dropped_fields=dropped_fields,
            failed=False,
        )

    # ------------------------------------------------------------------
    # 三、候选解析与越权字段拦截
    # ------------------------------------------------------------------
    def _parse_candidates(
        self,
        parsed: Dict[str, Any],
        clauses: List[Any],
    ) -> Tuple[List[Any], List[str]]:
        """
        解析模型返回的候选列表，拦截越权判定字段

        说明：
            模型若在候选对象中返回 risk_level / decision / severity 等
            判定字段，本方法会收集字段名并忽略它们，构造 LLMCandidate
            时完全丢弃。LLMCandidate 模型定义本身也不含这些字段，
            双重保障确保模型输出无法伪装成正式判定。

        参数：
            parsed: 大模型返回的 JSON 字典
            clauses: 结构化条款列表（用于校验 clause_id 与回填位置）

        返回：
            (候选列表, 被丢弃的越权字段名列表)
        """
        from app.graph.state import LLMCandidate

        legal_clause_ids: Set[str] = {clause.clause_id for clause in clauses}

        raw_candidates = parsed.get("candidates", [])
        if not isinstance(raw_candidates, list):
            logger.warning(
                "大模型返回的 candidates 字段不是数组 | 类型=%s",
                type(raw_candidates).__name__,
            )
            return ([], [])

        candidates: List[Any] = []
        dropped_fields: List[str] = []

        for raw_item in raw_candidates:
            if not isinstance(raw_item, dict):
                continue

            # 拦截越权判定字段
            for key in raw_item.keys():
                if key in self._FORBIDDEN_KEYS and key not in dropped_fields:
                    dropped_fields.append(key)

            # 校验条款 ID 合法性
            clause_id = raw_item.get("clause_id")
            if clause_id and clause_id not in legal_clause_ids:
                logger.warning(
                    "大模型返回了不存在的条款 ID，已置空 | clause_id=%s", clause_id
                )
                clause_id = None

            # 回填条款位置信息
            position = self._locate_clause_position(clauses, clause_id)

            # 置信度范围兜底
            confidence = raw_item.get("confidence")
            if isinstance(confidence, (int, float)):
                confidence = max(0.0, min(1.0, float(confidence)))
            else:
                confidence = None

            candidates.append(
                LLMCandidate(
                    candidate_id=f"L{len(candidates) + 1:04d}",
                    clause_id=clause_id,
                    clause_type=position.get("clause_type"),
                    risk_type=str(raw_item.get("risk_type", ""))[:200],
                    evidence_text=str(raw_item.get("evidence_text", ""))[:1000],
                    char_start=position.get("char_start"),
                    char_end=position.get("char_end"),
                    page_no=position.get("page_no"),
                    confidence=confidence,
                    suggestion_text=str(raw_item.get("suggestion_text", ""))[
                        : settings.SUGGESTION_MAX_LENGTH
                    ],
                    reason=str(raw_item.get("reason", ""))[:1000],
                    # 仅保留白名单字段，彻底剥离模型可能返回的其它字段
                    raw_output={
                        key: value
                        for key, value in raw_item.items()
                        if key in self._RAW_OUTPUT_WHITELIST
                    },
                )
            )

        return (candidates, dropped_fields)

    @staticmethod
    def _locate_clause_position(
        clauses: List[Any],
        clause_id: Optional[str],
    ) -> Dict[str, Any]:
        """
        依据条款 ID 回填位置信息

        参数：
            clauses: 结构化条款列表
            clause_id: 条款 ID，可为 None

        返回：
            含 clause_type / char_start / char_end / page_no 的字典；未找到返回空字典
        """
        if not clause_id:
            return {}

        for clause in clauses:
            if clause.clause_id == clause_id:
                return {
                    "clause_type": clause.clause_type,
                    "char_start": clause.char_start,
                    "char_end": clause.char_end,
                    "page_no": clause.page_start,
                }

        return {}

    @staticmethod
    def _json_escape(text: str) -> str:
        """
        将文本转义为可安全嵌入 JSON 字符串的形式

        参数：
            text: 原始文本

        返回：
            带双引号的 JSON 字符串字面量
        """
        import json

        return json.dumps(text, ensure_ascii=False)


# 全局单例
llm_engine = LLMEngine()
