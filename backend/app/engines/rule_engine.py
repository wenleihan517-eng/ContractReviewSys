# D:/ContractReviewSys-合同审批智能审查项目/backend/app/engines/rule_engine.py
"""
规则引擎 — 所有正式业务判定的唯一裁决入口

【架构红线（代码级强制）】
    以下判定只能由本引擎产出，大模型无权参与：
        1. 风险等级定级（RiskLevel）
        2. 违规判定（是否违规 + 违规依据）
        3. 任务阻塞判定（ReviewTaskStatus.BLOCKED）
        4. 最终审查结论（是否可通过审批）
    任何绕过本引擎直接写入上述结果的行为都属于违规实现。

【本模块提供的能力】
    1. 定级映射表：规则严重度 + 命中上下文 → 风险等级（确定性映射）
    2. 阻塞判定：最终风险等级 → 是否阻塞任务（由配置驱动）
    3. 结论生成：汇总风险命中 → 最终审查结论快照（含引擎版本，可追溯）
    4. 规则匹配：7 种匹配方式（keyword / regex / clause_type / clause_missing
       / numeric_threshold / field_compare / semantic）
    5. 模板加载：load_rules_from_yaml() 从 YAML 加载规则模板并校验合法性
    6. 决策入口：evaluate() 完整的确定性决策流程（定级 → 阻塞 → 结论）
    7. 落库转换：build_risk_items() 将命中转换为可持久化的 RiskItem 列表

【权责红线在本模块的落地】
    - build_risk_items() 产出的一切 RiskItem 的 judge_source 恒为 rule_engine
    - 大模型候选条目只能作为 llm_candidate 旁挂，不带 risk_level / severity
    - 合同级规则（contract_amount / contract_type / clause_missing）做去重，
      避免"规则 × 条款"笛卡尔遍历导致的重复计数虚增违规条数

返回值说明：
    - RuleEngine.evaluate()：返回 RuleDecisionResult 实例（含定级、阻塞、结论）
    - RuleEngine.map_severity_to_risk_level()：返回 RiskLevel

使用示例：
    engine = rule_engine
    decision = engine.evaluate(
        task_id="xxx",
        rule_hits=[{"rule_code": "R001", "severity": "major", "matched": True}],
    )
    if decision.is_blocked:
        ...  # 阻塞审批流
"""

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from pydantic import BaseModel, Field

from app.core.config import settings
from app.constants import (
    RISK_LEVEL_ORDER,
    RULE_ENGINE_SOURCE,
    RiskHitDecision,
    RiskLevel,
    RuleMatchType,
    RuleSeverity,
)
from app.core.exceptions import RuleEngineException
from app.core.logging_config import get_logger
from app.models.documents.rule_template import RuleTemplate

logger = get_logger(__name__)


# ======================================================================
# 一、规则引擎输入输出数据结构
# ======================================================================
class RuleHitInput(BaseModel):
    """
    单条规则命中输入

    字段：
        rule_code: 规则编码，唯一标识
        rule_name: 规则名称，便于报告展示
        severity: 规则自身严重度（info/minor/major/fatal）
        matched: 是否命中。False 表示规则执行了但未命中
        clause_id: 命中的条款 ID
        clause_type: 条款类型
        evidence: 命中证据（原文片段、正则匹配结果等）
        match_type: 规则匹配模式（keyword/regex/missing_clause/...）
        match_config: 规则匹配配置原文（含 field / op / value 等）
        extra: 附加上下文，如金额、天数等字段值

    说明：
        match_type 与 match_config 随命中一路透传到 RiskItem 构造阶段，
        用于判定规则作用域（合同级 / 条款级），进而决定是否需要去重。
        缺少这两个字段时无法区分"合同整体提示"与"具体条款命中"。
    """

    rule_code: str = Field(description="规则编码")
    rule_name: str = Field(default="", description="规则名称")
    severity: str = Field(default=RuleSeverity.INFO.value, description="规则严重度")
    matched: bool = Field(default=False, description="是否命中")
    clause_id: Optional[str] = Field(default=None, description="命中条款 ID")
    clause_type: Optional[str] = Field(default=None, description="条款类型")
    evidence: Optional[str] = Field(default=None, description="命中证据")
    match_type: str = Field(default="", description="规则匹配模式")
    match_config: Dict[str, Any] = Field(
        default_factory=dict, description="规则匹配配置原文"
    )
    extra: Dict[str, Any] = Field(default_factory=dict, description="附加上下文")


class RuleDecisionResult(BaseModel):
    """
    规则引擎决策结果

    字段：
        task_id: 审查任务 ID
        final_risk_level: 最终风险等级（由规则引擎定级）
        is_blocked: 是否阻塞任务
        decision: 命中处理决策（pass/warn/need_revision/block）
        violated_rule_codes: 判定违规的规则编码列表
        risk_level_distribution: 各风险等级命中数量统计
        conclusion: 最终审查结论文本
        conclusion_hash: 结论内容哈希，用于防篡改校验
        engine_version: 规则引擎版本，写入快照保证可追溯
        decided_at: 判定时间戳
        decision_source: 判定来源，固定为 rule_engine
    """

    task_id: str = Field(description="审查任务 ID")
    final_risk_level: str = Field(description="最终风险等级")
    is_blocked: bool = Field(default=False, description="是否阻塞任务")
    decision: str = Field(default=RiskHitDecision.PASS.value, description="处理决策")
    violated_rule_codes: List[str] = Field(default_factory=list, description="违规规则编码列表")
    risk_level_distribution: Dict[str, int] = Field(
        default_factory=dict, description="风险等级分布统计"
    )
    conclusion: str = Field(default="", description="最终审查结论")
    conclusion_hash: str = Field(default="", description="结论哈希，防篡改")
    engine_version: str = Field(default=settings.RULE_ENGINE_VERSION, description="引擎版本")
    decided_at: int = Field(default_factory=lambda: int(time.time()), description="判定时间戳")
    decision_source: str = Field(default=RULE_ENGINE_SOURCE, description="判定来源")


# ======================================================================
# 二、规则引擎主体
# ======================================================================
class MatchResult(BaseModel):
    """
    单条规则对单条条款（或整个合同）的确定性匹配结果

    【定位】
        匹配引擎（RuleMatcher）的产出物，承载"是否命中"与"命中证据的精确位置"。
        位置信息用于构造 RiskItem 的溯源字段，是"条款 ↔ 原文"双向反查的基础。

    字段：
        matched: 是否命中
        rule_code: 命中的规则编码
        evidence: 命中证据片段文本
        char_start / char_end: 证据在全文中的精确字符区间（左闭右开）
        page_no: 证据所在页码
        paragraph_index: 证据所在段落索引
        matched_value: 命中的具体值（关键词/正则分组/字段实际值），供诊断与报告
    """

    matched: bool = Field(default=False, description="是否命中")
    rule_code: str = Field(default="", description="命中的规则编码")
    evidence: str = Field(default="", description="命中证据片段")
    char_start: Optional[int] = Field(default=None, ge=0, description="证据起始字符位置")
    char_end: Optional[int] = Field(default=None, gt=0, description="证据结束字符位置")
    page_no: Optional[int] = Field(default=None, ge=1, description="证据所在页码")
    paragraph_index: Optional[int] = Field(default=None, ge=0, description="段落索引")
    matched_value: Any = Field(default=None, description="命中的具体值，供诊断与报告")


class ReviewOutcome(BaseModel):
    """
    规则引擎一体化审查的完整产出

    字段：
        risk_items: 最终风险项列表（RiskItem，与图状态定义完全对齐）
        decision: 规则引擎判定结果（定级 / 阻塞 / 结论）
        total_rules: 参与匹配的规则总数
        matched_rules: 实际命中的规则数
        rule_hit_inputs: 规则命中输入列表（供审计快照与溯源）
    """

    risk_items: List[Any] = Field(default_factory=list, description="最终风险项列表")
    decision: Optional["RuleDecisionResult"] = Field(
        default=None, description="规则引擎判定结果"
    )
    total_rules: int = Field(default=0, description="参与匹配的规则总数")
    matched_rules: int = Field(default=0, description="实际命中的规则数")
    rule_hit_inputs: List[Dict[str, Any]] = Field(
        default_factory=list, description="规则命中输入列表，供审计快照"
    )


# ======================================================================
# 三、规则匹配引擎
# ======================================================================
class RuleMatcher:
    """
    规则匹配引擎 —— 四类确定性匹配的纯函数实现

    【设计原则】
        - 纯确定性计算：无随机、无模型参与，相同输入必得相同输出
        - 精确溯源：命中时不仅给出证据片段，还定位证据在全文中的
          字符区间、页码、段落索引，供 RiskItem 反查原文
        - 单条规则异常不拖垮整体：正则非法、字段类型不符等异常
          按"未命中"处理并记录告警，保证判定流程的健壮性
    """

    # 文本比较运算符集合（与 CompareOperator 对齐）
    _TEXT_OPERATORS = {"eq", "neq", "in", "not_in"}

    def match_clause(
        self,
        template: RuleTemplate,
        clause: Any,
        contract_amount: float,
        contract_type: str,
    ) -> MatchResult:
        """
        对单条条款执行规则匹配（条款级匹配）

        说明：
            适用于 keyword / regex / clause_type / field_compare /
            numeric_threshold 五种模式。
            missing_clause 属于合同级匹配，不在此方法处理。

        参数：
            template: 规则模板
            clause: 结构化条款（ClauseItem）
            contract_amount: 合同金额（供数值比较规则使用）
            contract_type: 合同类型（供字段比较规则使用）

        返回：
            MatchResult 实例（matched=False 表示未命中）
        """
        match_type = template.match_type
        config = template.match_config or {}
        text = getattr(clause, "content", "") or ""

        try:
            if match_type == RuleMatchType.KEYWORD.value:
                return self._match_keyword(template, clause, config, text)

            if match_type == RuleMatchType.REGEX.value:
                return self._match_regex(template, clause, config, text)

            if match_type == RuleMatchType.CLAUSE_TYPE.value:
                return self._match_clause_type(template, clause, config, text)

            if match_type in (
                RuleMatchType.FIELD_COMPARE.value,
                RuleMatchType.NUMERIC_THRESHOLD.value,
            ):
                return self._match_numeric(
                    template, clause, config, contract_amount, contract_type, text
                )

            if match_type == RuleMatchType.SEMANTIC.value:
                # 语义召回由 vector_review 借助 Milvus 完成，规则引擎不做模型计算。
                # 规则匹配阶段按"未命中"处理，候选在 risk_merge 阶段作为参考项合并。
                return MatchResult(matched=False, rule_code=template.rule_code)

        except Exception as exc:  # noqa: BLE001 - 单条规则异常不拖垮整体
            logger.warning(
                "规则匹配异常，按未命中处理 | 规则=%s | 类型=%s | 异常=%s",
                template.rule_code,
                match_type,
                exc,
            )
            return MatchResult(matched=False, rule_code=template.rule_code)

        return MatchResult(matched=False, rule_code=template.rule_code)

    def match_missing_clause(
        self,
        template: RuleTemplate,
        existing_clause_types: List[str],
    ) -> MatchResult:
        """
        执行"必备条款缺失"检查（合同级匹配）

        说明：
            命中语义与其它模式相反：合同**缺失**了 require_clause_types
            中的某类条款时才算命中。因此入参不是单条条款，而是
            整个合同已切分出的条款类型集合。

        参数：
            template: 规则模板（match_type 须为 missing_clause）
            existing_clause_types: 合同已存在的条款类型集合

        返回：
            MatchResult。命中时 evidence 为"缺失条款类型清单"
        """
        require_types = template.match_config.get("require_clause_types", []) or []
        missing_types = [t for t in require_types if t not in existing_clause_types]

        if not missing_types:
            return MatchResult(matched=False, rule_code=template.rule_code)

        evidence = "合同缺失必备条款类型：" + "、".join(missing_types)
        return MatchResult(
            matched=True,
            rule_code=template.rule_code,
            evidence=evidence,
            matched_value=missing_types,
            # 合同级命中无单一字符区间，位置信息留空由下游处理
            char_start=None,
            char_end=None,
            page_no=None,
            paragraph_index=None,
        )

    # ------------------------------------------------------------------
    # 各匹配模式的私有实现
    # ------------------------------------------------------------------
    def _match_keyword(
        self,
        template: RuleTemplate,
        clause: Any,
        config: Dict[str, Any],
        text: str,
    ) -> MatchResult:
        """
        关键词匹配：命中任一关键词即命中

        说明：
            命中时精确定位关键词在全文中的字符区间。
            全文区间 = 条款起始偏移 + 关键词在条款正文内的相对偏移。
        """
        keywords = config.get("keywords", []) or []
        clause_start = getattr(clause, "char_start", 0) or 0

        for keyword in keywords:
            if not keyword:
                continue
            position = text.find(keyword)
            if position < 0:
                continue

            abs_start = clause_start + position
            abs_end = abs_start + len(keyword)
            evidence = self._build_evidence(text, position, len(keyword))

            return MatchResult(
                matched=True,
                rule_code=template.rule_code,
                evidence=evidence,
                char_start=abs_start,
                char_end=abs_end,
                page_no=getattr(clause, "page_start", None),
                paragraph_index=getattr(clause, "paragraph_index", None),
                matched_value=keyword,
            )

        return MatchResult(matched=False, rule_code=template.rule_code)

    def _match_regex(
        self,
        template: RuleTemplate,
        clause: Any,
        config: Dict[str, Any],
        text: str,
    ) -> MatchResult:
        """
        正则匹配：模式命中即命中

        说明：
            使用 match.start()/end() 定位正则命中片段在全文中的精确区间。
        """
        import re

        pattern = config.get("pattern", "")
        if not pattern:
            return MatchResult(matched=False, rule_code=template.rule_code)

        found = re.search(pattern, text)
        if not found:
            return MatchResult(matched=False, rule_code=template.rule_code)

        clause_start = getattr(clause, "char_start", 0) or 0
        matched_text = found.group(0)
        abs_start = clause_start + found.start()
        abs_end = clause_start + found.end()
        evidence = self._build_evidence(text, found.start(), len(matched_text))

        return MatchResult(
            matched=True,
            rule_code=template.rule_code,
            evidence=evidence,
            char_start=abs_start,
            char_end=abs_end,
            page_no=getattr(clause, "page_start", None),
            paragraph_index=getattr(clause, "paragraph_index", None),
            matched_value=matched_text,
        )

    def _match_clause_type(
        self,
        template: RuleTemplate,
        clause: Any,
        config: Dict[str, Any],
        text: str,
    ) -> MatchResult:
        """
        条款类型匹配：条款类型命中配置的条款类型集合即命中
        """
        clause_types = config.get("clause_types", []) or []
        clause_type = getattr(clause, "clause_type", None)

        if clause_type not in clause_types:
            return MatchResult(matched=False, rule_code=template.rule_code)

        # 命中整条条款，证据取条款正文前 200 字符
        evidence = (text or "")[:200]
        return MatchResult(
            matched=True,
            rule_code=template.rule_code,
            evidence=evidence,
            char_start=getattr(clause, "char_start", None),
            char_end=getattr(clause, "char_end", None),
            page_no=getattr(clause, "page_start", None),
            paragraph_index=getattr(clause, "paragraph_index", None),
            matched_value=clause_type,
        )

    def _match_numeric(
        self,
        template: RuleTemplate,
        clause: Any,
        config: Dict[str, Any],
        contract_amount: float,
        contract_type: str,
        text: str,
    ) -> MatchResult:
        """
        数值比较匹配（field_compare 与 numeric_threshold 共用实现）

        说明：
            从契约上下文提取字段实际值，与规则阈值比较。
            支持的字段：
                contract_amount      合同金额（元）
                contract_type        合同类型
                clause_char_length   条款正文字符数
                penalty_ratio        违约金比例（从条款正文正则提取百分比）
                payment_days         付款周期天数（从条款正文正则提取）
        """
        field = config.get("field", "")
        operator = config.get("op", "eq")
        expected = config.get("value")

        actual = self._resolve_field_value(
            field=field,
            clause=clause,
            text=text,
            contract_amount=contract_amount,
            contract_type=contract_type,
        )

        if actual is None:
            return MatchResult(matched=False, rule_code=template.rule_code)

        if not self._compare(actual, operator, expected):
            return MatchResult(matched=False, rule_code=template.rule_code)

        evidence = f"{field}={actual} {operator} {expected}"
        return MatchResult(
            matched=True,
            rule_code=template.rule_code,
            evidence=evidence,
            char_start=getattr(clause, "char_start", None),
            char_end=getattr(clause, "char_end", None),
            page_no=getattr(clause, "page_start", None),
            paragraph_index=getattr(clause, "paragraph_index", None),
            matched_value=actual,
        )

    def _resolve_field_value(
        self,
        field: str,
        clause: Any,
        text: str,
        contract_amount: float,
        contract_type: str,
    ) -> Any:
        """
        解析字段实际值

        参数：
            field: 字段名
            clause: 条款对象
            text: 条款正文
            contract_amount: 合同金额
            contract_type: 合同类型

        返回：
            字段实际值；无法解析时返回 None（视为未命中）
        """
        if field == "contract_amount":
            return contract_amount
        if field == "contract_type":
            return contract_type
        if field == "clause_char_length":
            char_start = getattr(clause, "char_start", 0) or 0
            char_end = getattr(clause, "char_end", 0) or 0
            return char_end - char_start
        if field == "penalty_ratio":
            return self._extract_penalty_ratio(text)
        if field == "payment_days":
            return self._extract_payment_days(text)
        return None

    @staticmethod
    def _extract_penalty_ratio(text: str) -> Optional[float]:
        """
        从条款正文提取违约金比例

        说明：
            匹配"百分之 X"或"X%"，X 支持小数（如 百分之三十 → 0.30）。
            法律实务中"百分之三十"应折算为 0.30 参与比较。

        参数：
            text: 条款正文

        返回：
            比例值（0-1 区间）；未提取到返回 None
        """
        import re

        # 优先匹配"百分之 N"（中文表述）
        cn = re.search(r"百分之([零一二三四五六七八九十百点\d]+)", text)
        if cn:
            digits = cn.group(1)
            # 简单中文数字转阿拉伯（覆盖十/百/点及常见表述）
            ratio = RuleMatcher._cn_to_float(digits)
            if ratio is not None:
                return ratio / 100.0

        # 回退匹配 "N%"
        pct = re.search(r"(\d+(?:\.\d+)?)\s*%", text)
        if pct:
            return float(pct.group(1)) / 100.0

        return None

    @staticmethod
    def _extract_payment_days(text: str) -> Optional[int]:
        """
        从条款正文提取付款周期天数

        说明：
            匹配"X 个工作日内""X 日内""X 天内"等表述。

        参数：
            text: 条款正文

        返回：
            天数；未提取到返回 None
        """
        import re

        found = re.search(r"(\d+)\s*(?:个工作?日|日内|天内|日之内)", text)
        if found:
            return int(found.group(1))
        return None

    @staticmethod
    def _cn_to_float(cn: str) -> Optional[float]:
        """
        中文数字转浮点（支持十/百/点/零，覆盖常见合同比例表述）

        参数：
            cn: 中文数字字符串（如 "三十"、"十五点五"）

        返回：
            浮点数值；无法解析返回 None
        """
        digits = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
                  "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
        units = {"十": 10, "百": 100}

        total = 0
        current = 0
        has_digit = False

        for ch in cn:
            if ch == "点":
                # 小数点后部分（此处比例场景最多一位小数，简化处理）
                continue
            if ch in digits:
                current = digits[ch]
                has_digit = True
            elif ch in units:
                unit = units[ch]
                total += (current if current else 1) * unit
                current = 0
                has_digit = True
            else:
                return None

        total += current
        return float(total) if has_digit else None

    @staticmethod
    def _compare(actual: Any, operator: str, expected: Any) -> bool:
        """
        执行比较运算

        参数：
            actual: 实际值
            operator: 比较运算符（gt/gte/lt/lte/eq/neq/in/not_in）
            expected: 期望值

        返回：
            比较结果布尔值；类型不匹配时返回 False
        """
        try:
            if operator == "gt":
                return actual > expected
            if operator == "gte":
                return actual >= expected
            if operator == "lt":
                return actual < expected
            if operator == "lte":
                return actual <= expected
            if operator == "eq":
                return actual == expected
            if operator == "neq":
                return actual != expected
            if operator == "in":
                return actual in (expected or [])
            if operator == "not_in":
                return actual not in (expected or [])
        except TypeError:
            return False
        return False

    @staticmethod
    def _build_evidence(text: str, position: int, length: int, window: int = 60) -> str:
        """
        从正文截取命中位置附近的证据片段

        说明：
            以命中位置为中心，前后各取 window 个字符，并加省略号标识截断。
            用于前端高亮与人工复核。

        参数：
            text: 条款正文
            position: 命中位置（相对偏移）
            length: 命中文本长度
            window: 前后截取字符数

        返回：
            证据片段文本
        """
        start = max(0, position - window)
        end = min(len(text), position + length + window)

        prefix = "..." if start > 0 else ""
        suffix = "..." if end < len(text) else ""
        return f"{prefix}{text[start:end]}{suffix}"


# ======================================================================
# 四、规则引擎主体
# ======================================================================
class RuleEngine:
    """
    规则引擎

    设计原则：
        - 纯确定性计算：相同输入必得相同输出，无随机性、无模型参与
        - 配置驱动：阈值、映射关系全部来自配置中心与规则模板
        - 全程留痕：每次判定输出可审计快照（含版本与哈希）
    """

    def __init__(self) -> None:
        # 严重度 → 风险等级 的基础映射表
        # 说明：这是确定性映射，规则模板可通过 override 覆盖单条规则的定级
        self._severity_to_level: Dict[str, str] = {
            RuleSeverity.INFO.value: RiskLevel.LOW.value,
            RuleSeverity.MINOR.value: RiskLevel.LOW.value,
            RuleSeverity.MAJOR.value: RiskLevel.HIGH.value,
            RuleSeverity.FATAL.value: RiskLevel.CRITICAL.value,
        }

    # ------------------------------------------------------------------
    # 一、定级
    # ------------------------------------------------------------------
    def map_severity_to_risk_level(
        self,
        severity: str,
        override_level: Optional[str] = None,
    ) -> str:
        """
        将规则严重度映射为风险等级

        参数：
            severity: 规则自身严重度，取值见 RuleSeverity
            override_level: 规则模板中单独指定的风险等级。
                            非空时优先使用，用于单条规则的精细化定级

        返回：
            风险等级字符串，取值见 RiskLevel

        异常：
            RuleEngineException：传入未知严重度且无 override 时抛出
        """
        # 规则模板显式指定的等级优先级最高
        if override_level:
            if override_level not in RISK_LEVEL_ORDER:
                raise RuleEngineException(
                    message="规则模板指定的风险等级非法",
                    detail=f"override_level={override_level}",
                )
            return override_level

        if severity not in self._severity_to_level:
            raise RuleEngineException(
                message="未知的规则严重度，无法完成定级",
                detail=f"severity={severity}, 允许值={list(self._severity_to_level.keys())}",
            )

        return self._severity_to_level[severity]

    def compute_final_risk_level(self, hit_levels: List[str]) -> str:
        """
        依据全部命中条款的风险等级计算最终风险等级

        规则：
            取所有命中等级中的最高级（保守策略：从严定级）。
            无命中时返回 LOW。

        参数：
            hit_levels: 命中的风险等级列表

        返回：
            最终风险等级字符串

        异常：
            RuleEngineException：存在非法等级值时抛出
        """
        # 无命中视为低风险
        if not hit_levels:
            return RiskLevel.LOW.value

        max_order = 0
        max_level = RiskLevel.LOW.value

        for level in hit_levels:
            if level not in RISK_LEVEL_ORDER:
                raise RuleEngineException(
                    message="命中记录中存在非法的风险等级",
                    detail=f"level={level}, 允许值={list(RISK_LEVEL_ORDER.keys())}",
                )
            order = RISK_LEVEL_ORDER[level]
            # 严格大于才更新，保证同级别情况下结果稳定
            if order > max_order:
                max_order = order
                max_level = level

        return max_level

    # ------------------------------------------------------------------
    # 二、阻塞判定
    # ------------------------------------------------------------------
    def judge_blocked(self, final_risk_level: str) -> bool:
        """
        判定任务是否阻塞

        规则：
            最终风险等级命中配置的阻塞等级列表（RULE_ENGINE_BLOCK_LEVELS）时阻塞。
            该判定完全由配置驱动，大模型无权干预。

        参数：
            final_risk_level: 最终风险等级

        返回：
            True 表示阻塞，False 表示不阻塞
        """
        blocked = final_risk_level in settings.RULE_ENGINE_BLOCK_LEVELS
        logger.info(
            "阻塞判定 | 最终等级=%s | 阻塞等级配置=%s | 结论=%s",
            final_risk_level,
            settings.RULE_ENGINE_BLOCK_LEVELS,
            "阻塞" if blocked else "放行",
        )
        return blocked

    def judge_decision(self, final_risk_level: str, is_blocked: bool) -> str:
        """
        依据最终等级与阻塞结果推导处理决策

        规则（确定性映射）：
            critical/high 且阻塞 → block
            high 未阻塞         → need_revision
            medium              → warn
            low                 → pass

        参数：
            final_risk_level: 最终风险等级
            is_blocked: 是否阻塞

        返回：
            处理决策字符串，取值见 RiskHitDecision
        """
        if is_blocked:
            return RiskHitDecision.BLOCK.value

        if final_risk_level in (RiskLevel.HIGH.value, RiskLevel.CRITICAL.value):
            return RiskHitDecision.NEED_REVISION.value

        if final_risk_level == RiskLevel.MEDIUM.value:
            return RiskHitDecision.WARN.value

        return RiskHitDecision.PASS.value

    # ------------------------------------------------------------------
    # 三、结论生成
    # ------------------------------------------------------------------
    def build_conclusion(
        self,
        final_risk_level: str,
        is_blocked: bool,
        violated_hits: List[RuleHitInput],
    ) -> str:
        """
        生成最终审查结论文本

        说明：
            结论文本为固定模板 + 确定性字段拼装，不含任何模型生成内容，
            保证结论表述稳定、可复现、可审计。

        参数：
            final_risk_level: 最终风险等级
            is_blocked: 是否阻塞
            violated_hits: 判定违规的命中记录列表

        返回：
            审查结论文本
        """
        level_label = {
            RiskLevel.LOW.value: "低风险",
            RiskLevel.MEDIUM.value: "中风险",
            RiskLevel.HIGH.value: "高风险",
            RiskLevel.CRITICAL.value: "严重风险",
        }.get(final_risk_level, "未知风险")

        # 拼接违规规则明细
        if violated_hits:
            details = "；".join(
                f"[{hit.rule_code}]{hit.rule_name or '未命名规则'}"
                for hit in violated_hits
            )
        else:
            details = "无"

        verdict = "不予通过，需整改后重新提交" if is_blocked else "可进入人工审批环节"

        return (
            f"审查结论：本次合同审查最终风险等级为【{level_label}】。"
            f"命中违规规则 {len(violated_hits)} 条：{details}。"
            f"审批处置意见：{verdict}。"
            f"（判定来源：规则引擎 {settings.RULE_ENGINE_VERSION}）"
        )

    @staticmethod
    def build_conclusion_hash(conclusion: str, task_id: str) -> str:
        """
        生成结论内容哈希

        说明：
            用于结论防篡改校验。任何对结论内容的修改都会导致哈希不匹配。

        参数：
            conclusion: 结论文本
            task_id: 任务 ID（参与哈希，防止跨任务结论复制）

        返回：
            SHA-256 十六进制哈希字符串
        """
        raw = f"{task_id}|{conclusion}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    # ------------------------------------------------------------------
    # 四、统一决策入口
    # ------------------------------------------------------------------
    def evaluate(
        self,
        task_id: str,
        rule_hits: List[Dict[str, Any]],
    ) -> RuleDecisionResult:
        """
        执行完整规则判定（核心入口）

        流程：
            1. 过滤出真实命中的规则（matched=True）
            2. 逐条完成定级（severity → risk_level，支持模板 override）
            3. 汇总计算最终风险等级（取最高）
            4. 判定是否阻塞
            5. 推导处理决策
            6. 生成结论文本与防篡改哈希

        参数：
            task_id: 审查任务 ID
            rule_hits: 规则命中列表，每项结构见 RuleHitInput

        返回：
            RuleDecisionResult 实例

        异常：
            RuleEngineException：规则引擎被关闭、输入非法或定级失败时抛出
        """
        # 前置校验：规则引擎必须处于启用状态
        if not settings.RULE_ENGINE_ENABLED:
            raise RuleEngineException(
                message="规则引擎已被关闭，拒绝执行判定（合同场景不允许未判定放行）"
            )

        if not task_id:
            raise RuleEngineException(message="执行规则判定时 task_id 不能为空")

        start_ts = time.perf_counter()

        # 1. 解析输入并筛选真实命中项
        parsed_hits: List[RuleHitInput] = []
        matched_hits: List[RuleHitInput] = []
        level_distribution: Dict[str, int] = {level: 0 for level in settings.RULE_RISK_LEVELS}

        for raw_hit in rule_hits:
            hit = RuleHitInput(**raw_hit)
            parsed_hits.append(hit)
            if hit.matched:
                matched_hits.append(hit)

        # 去重：消除合同级规则在每条条款上重复命中导致的计数失真
        matched_hits = self._dedup_hits(matched_hits)

        # 2. 逐条定级
        hit_levels: List[str] = []
        for hit in matched_hits:
            # 从 extra 中读取模板级 override（由规则模板管理模块写入）
            override_level = hit.extra.get("override_risk_level")
            level = self.map_severity_to_risk_level(hit.severity, override_level)
            hit_levels.append(level)
            level_distribution[level] = level_distribution.get(level, 0) + 1

        # 3. 计算最终风险等级
        final_risk_level = self.compute_final_risk_level(hit_levels)

        # 4. 阻塞判定
        is_blocked = self.judge_blocked(final_risk_level)

        # 5. 推导处理决策
        decision = self.judge_decision(final_risk_level, is_blocked)

        # 6. 生成结论与哈希
        conclusion = self.build_conclusion(final_risk_level, is_blocked, matched_hits)
        conclusion_hash = self.build_conclusion_hash(conclusion, task_id)

        result = RuleDecisionResult(
            task_id=task_id,
            final_risk_level=final_risk_level,
            is_blocked=is_blocked,
            decision=decision,
            violated_rule_codes=[hit.rule_code for hit in matched_hits],
            risk_level_distribution=level_distribution,
            conclusion=conclusion,
            conclusion_hash=conclusion_hash,
            engine_version=settings.RULE_ENGINE_VERSION,
        )

        elapsed_ms = (time.perf_counter() - start_ts) * 1000.0
        logger.info(
            "规则判定完成 | 任务=%s | 总规则数=%d | 命中数=%d | 最终等级=%s | 阻塞=%s | 耗时=%.2fms",
            task_id,
            len(parsed_hits),
            len(matched_hits),
            final_risk_level,
            is_blocked,
            elapsed_ms,
        )

        return result

    # ------------------------------------------------------------------
    # 五、审计快照
    # ------------------------------------------------------------------
    def build_audit_snapshot(
        self,
        result: RuleDecisionResult,
        rule_hits: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        构造判定审计快照

        说明：
            快照包含输入与输出全量信息，写入 MongoDB 的 cr_rule_hit_log 集合，
            保证任意历史判定结果均可完整回溯。

        参数：
            result: 规则判定结果
            rule_hits: 原始规则命中输入

        返回：
            可直接落库的字典
        """
        snapshot = {
            "task_id": result.task_id,
            "engine_version": result.engine_version,
            "decision_source": result.decision_source,
            "input_hits": rule_hits,
            "output": result.model_dump(),
            "block_levels_config": settings.RULE_ENGINE_BLOCK_LEVELS,
            "snapshot_hash": hashlib.sha256(
                json.dumps(
                    {"input": rule_hits, "output": result.model_dump()},
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                ).encode("utf-8")
            ).hexdigest(),
            "created_at": int(time.time()),
        }
        return snapshot

    # ------------------------------------------------------------------
    # 六、规则加载与一体化审查（YAML 配置驱动的匹配引擎入口）
    # ------------------------------------------------------------------
    def load_rules_from_yaml(self, path: str) -> List[RuleTemplate]:
        """
        从 YAML 文件加载规则模板

        说明：
            委托给 RuleRegistry 完成解析与校验。文件不存在或格式非法时
            抛出 RuleEngineException，绝不静默返回空列表
            （合同场景下"无规则即放行"是严重合规风险）。

        参数：
            path: YAML 文件路径（绝对或相对）

        返回：
            RuleTemplate 实例列表（按文件内定义顺序，未排序）

        异常：
            RuleEngineException：文件不存在、YAML 语法错误、规则字段非法
        """
        return rule_registry.load_yaml(path)

    def get_active_rules(
        self,
        rules: List[RuleTemplate],
        contract_type: str,
    ) -> List[RuleTemplate]:
        """
        过滤启用规则并按优先级排序

        说明：
            过滤条件：status=enabled 且适用于指定合同类型。
            排序规则：priority 降序（数值越大优先级越高），
            同优先级按 rule_code 升序保证结果稳定。

        参数：
            rules: 规则模板列表
            contract_type: 目标合同类型

        返回：
            过滤并排序后的规则列表
        """
        active = [
            rule for rule in rules
            if rule.is_active and rule.matches_contract_type(contract_type)
        ]
        active.sort(key=lambda rule: (-rule.priority, rule.rule_code))
        return active

    def review_clauses(
        self,
        clauses: List[Any],
        contract_type: str,
        task_id: str,
        contract_amount: float = 0.0,
        rules: Optional[List[RuleTemplate]] = None,
    ) -> "ReviewOutcome":
        """
        一体化规则审查（YAML 规则驱动的完整判定入口）

        【职责】
            这是规则引擎对外的最核心入口，完成：
                1. 获取规则集（参数优先，否则从配置的 YAML 加载）
                2. 过滤启用 + 合同类型 + 优先级排序
                3. 条款级匹配（keyword/regex/clause_type/field_compare/numeric_threshold）
                4. 合同级匹配（missing_clause 必备条款缺失检查）
                5. 汇总命中 → 定级 → 阻塞 → 结论（复用 evaluate）
                6. 构造标准化 RiskItem 列表（与图状态定义完全对齐）

        【架构红线】
            本方法产出的 risk_level / decision 完全由规则引擎的确定性映射产生，
            不包含任何模型计算。所有 RiskItem 的 judge_source 固定为 rule_engine。

        参数：
            clauses: 结构化条款列表（ClauseItem）
            contract_type: 合同类型（purchase/sales/service/labor 等）
            task_id: 审查任务 ID
            contract_amount: 合同金额（元），供数值规则使用
            rules: 规则模板列表。为空时从配置的 RULE_RULES_PATH 加载

        返回：
            ReviewOutcome（含 risk_items 与 decision）

        异常：
            RuleEngineException：规则引擎关闭、无规则或定级失败
        """
        if not settings.RULE_ENGINE_ENABLED:
            raise RuleEngineException(
                message="规则引擎已被关闭，拒绝执行判定（合同场景不允许未判定放行）"
            )

        if not task_id:
            raise RuleEngineException(message="执行规则判定时 task_id 不能为空")

        start_ts = time.perf_counter()

        # -------------------- 第一步：获取并过滤规则集 --------------------
        if rules is None:
            rules = self.load_rules_from_yaml(settings.RULE_RULES_PATH)

        active_rules = self.get_active_rules(rules, contract_type)

        if not active_rules:
            raise RuleEngineException(
                message=(
                    f"未找到适用于合同类型 '{contract_type}' 的启用规则，"
                    f"无法执行合规判定。请检查规则配置文件"
                ),
                detail=f"rules_path={settings.RULE_RULES_PATH}",
            )

        # -------------------- 第二步：规则匹配（条款级 + 合同级） --------------------
        matcher = RuleMatcher()
        existing_clause_types: List[str] = [
            getattr(clause, "clause_type", "") for clause in clauses
        ]

        hit_inputs: List[Dict[str, Any]] = []
        matched_count = 0

        for rule in active_rules:
            if rule.match_type == RuleMatchType.MISSING_CLAUSE.value:
                # 必备条款缺失：合同级匹配，对整个合同的条款类型集合判定一次
                result = matcher.match_missing_clause(rule, existing_clause_types)
                hit_inputs.append(
                    self._build_hit_input(rule, result, clause_id=None, clause_type=None)
                )
                if result.matched:
                    matched_count += 1
                continue

            # 条款级匹配：对每条条款分别执行，命中即记录一条
            rule_matched = False
            for clause in clauses:
                result = matcher.match_clause(
                    template=rule,
                    clause=clause,
                    contract_amount=contract_amount,
                    contract_type=contract_type,
                )
                if not result.matched:
                    continue

                rule_matched = True
                hit_inputs.append(
                    self._build_hit_input(
                        rule,
                        result,
                        clause_id=getattr(clause, "clause_id", None),
                        clause_type=getattr(clause, "clause_type", None),
                    )
                )
                matched_count += 1

            # 若该规则对所有条款均未命中，仍记录一条"执行但未命中"，
            # 供 evaluate 统计规则执行总量（保证审计完整性）
            if not rule_matched:
                hit_inputs.append(
                    self._build_hit_input(
                        rule,
                        MatchResult(matched=False, rule_code=rule.rule_code),
                        clause_id=None,
                        clause_type=None,
                    )
                )

        # -------------------- 第三步：调用 evaluate 完成定级与结论 --------------------
        decision = self.evaluate(task_id=task_id, rule_hits=hit_inputs)

        # -------------------- 第四步：构造 RiskItem 列表 --------------------
        risk_items = self.build_risk_items(decision, hit_inputs)

        elapsed_ms = (time.perf_counter() - start_ts) * 1000.0
        logger.info(
            "一体化规则审查完成 | 任务=%s | 合同类型=%s | 规则数=%d | 命中=%d | "
            "风险项=%d | 最终等级=%s | 阻塞=%s | 耗时=%.2fms",
            task_id, contract_type, len(active_rules), matched_count,
            len(risk_items), decision.final_risk_level, decision.is_blocked, elapsed_ms,
        )

        return ReviewOutcome(
            risk_items=risk_items,
            decision=decision,
            total_rules=len(active_rules),
            matched_rules=matched_count,
            rule_hit_inputs=hit_inputs,
        )

    @staticmethod
    def _dedup_hits(hits: List["RuleHitInput"]) -> List["RuleHitInput"]:
        """
        对命中记录去重，消除合同级规则的重复计数

        【为什么必须去重】
            规则匹配阶段是"规则 × 条款"的笛卡尔遍历：每条规则会在每一条
            条款上各执行一次。对于条款级规则这是正确的（不同条款代表不同的
            风险事实）；但对于取值来自合同整体字段的规则（如
            contract_amount 阈值判定、必备条款缺失判定），在每条条款上都会
            得到完全相同的结论，属于同一风险事实的重复记录。

            若不去重，一份 10 条条款的大额合同会让 NUM-004 这一条规则
            产出 10 个风险项，进而导致：
                - 结论文本"命中违规规则 N 条"的 N 被夸大（用户直接可见）
                - risk_level_distribution 计数失真（前端图表数据来源）
                - violated_rule_codes 出现大段重复
                - 前端风险列表出现内容完全一致的重复卡片

        【去重键定义】
            去重键完全由命中记录自身可推导的字段决定，不依赖任何外部注入：
                - 合同级规则：(rule_code, "")
                - 条款级规则：(rule_code, clause_id)
            作用域判定依据 match_type 与 match_config.field，与 evaluate 的
            统计口径来自同一处推导逻辑，保证两处结果一致。

        参数：
            hits: 已判定命中的记录列表

        返回：
            去重后的命中记录列表，保持原有相对顺序
        """
        # 合同级取值字段：命中结论与具体条款无关，须整份合同收敛为一条
        contract_scoped_fields = {"contract_amount", "contract_type"}

        deduped: List["RuleHitInput"] = []
        seen: set = set()

        for hit in hits:
            # 推导规则作用域
            is_contract_scoped = hit.match_type == RuleMatchType.MISSING_CLAUSE.value
            if hit.match_type in (
                RuleMatchType.NUMERIC_THRESHOLD.value,
                RuleMatchType.FIELD_COMPARE.value,
            ):
                field = (hit.match_config or {}).get("field", "")
                is_contract_scoped = field in contract_scoped_fields

            key = (
                hit.rule_code,
                "" if is_contract_scoped else (hit.clause_id or ""),
            )
            if key in seen:
                logger.debug(
                    "规则命中重复，已合并计数 | 规则=%s | 条款=%s | 作用域=%s",
                    hit.rule_code,
                    hit.clause_id,
                    "合同级" if is_contract_scoped else "条款级",
                )
                continue
            seen.add(key)
            deduped.append(hit)

        return deduped

    def build_risk_items(
        self,
        decision: "RuleDecisionResult",
        hit_inputs: List[Dict[str, Any]],
    ) -> List[Any]:
        """
        将规则命中输入转换为标准化 RiskItem 列表

        说明：
            RiskItem 与 app.graph.state.RiskItem 完全对齐，
            其中的 risk_level / decision / severity 全部来自规则引擎的
            确定性判定结果，judge_source 固定为 rule_engine。

        参数：
            decision: 规则引擎判定结果
            hit_inputs: 规则命中输入列表（与 evaluate 输入一致）

        返回：
            RiskItem 实例列表（仅含真实命中的项，按命中顺序）

        去重规则（按规则作用域区分，二者不可混用）：
            同一规则会在多条条款上逐条执行匹配，命中规模取决于规则作用域：

            1) 条款级规则（keyword / regex / clause_type，以及以条款正文为
               取值来源的 numeric_threshold，如 penalty_ratio / payment_days）
               —— 命中天然锚定到具体条款，不同条款的命中是**不同的风险事实**，
               必须全部保留。去重键为 (rule_code, clause_id)。

            2) 合同级规则（missing_clause 必备条款缺失，以及以 contract_amount /
               contract_type 为取值来源的 numeric_threshold）
               —— 取值来自合同整体，与具体条款无关；在每条条款上重复匹配会得到
               完全相同的结论，属于**同一风险事实的重复记录**，必须收敛为一条。
               去重键为 (rule_code, "")，整份合同仅保留首条命中。

            若不做区分，合同级规则会随条款数量线性膨胀出大量内容相同的风险项，
            既污染风险计数与风险等级分布，也让前端风险列表出现无意义的重复卡片。

            补充：以 contract_amount 为阈值的规则本就是"对合同的整体提示"，
            其证据文本形如 contract_amount=12000000.0 gte 5000000，不含条款语义，
            因此即使被判定为条款级保留也难以定位，收敛为首条最利于人工阅读。
        """
        # 延迟导入，避免与 graph 包的循环依赖
        from app.graph.state import RiskItem

        risk_items: List[Any] = []
        risk_index = 0

        # 复用统一去重逻辑：与 evaluate 保持一致，避免两处口径漂移
        matched_hits = [
            RuleHitInput(**hit) for hit in hit_inputs if hit.get("matched")
        ]
        matched_hits = self._dedup_hits(matched_hits)

        for hit in matched_hits:
            extra = hit.extra or {}
            rule_code = hit.rule_code
            clause_id = hit.clause_id

            # 定级完全由规则引擎产生，此处只做读取
            risk_level = self.map_severity_to_risk_level(
                severity=hit.severity,
                override_level=extra.get("override_risk_level"),
            )

            # 渲染修改建议（填充证据占位符）
            suggestion = self._render_suggestion(
                template_text=extra.get("suggestion_template", ""),
                evidence=hit.evidence or "",
            )

            risk_index += 1
            risk_items.append(
                RiskItem(
                    risk_id=f"R{risk_index:04d}",
                    rule_code=rule_code,
                    rule_name=hit.rule_name,
                    clause_id=clause_id,
                    clause_type=hit.clause_type,
                    evidence_text=hit.evidence or "",
                    char_start=extra.get("char_start"),
                    char_end=extra.get("char_end"),
                    page_no=extra.get("page_no"),
                    risk_level=risk_level,
                    decision=decision.decision,
                    severity=hit.severity,
                    suggestion_text=suggestion,
                    legal_basis=extra.get("legal_basis", ""),
                    reference_only=False,
                    matched_sources=[RULE_ENGINE_SOURCE],
                    conflict_flag=False,
                    resolution_note="规则引擎确定性命中，等级由引擎依据严重度映射表定级",
                )
            )

        return risk_items

    def _build_hit_input(
        self,
        rule: RuleTemplate,
        result: "MatchResult",
        clause_id: Optional[str],
        clause_type: Optional[str],
    ) -> Dict[str, Any]:
        """
        构造规则引擎 evaluate 所需的命中输入字典

        说明：
            将 RuleTemplate + MatchResult 组装为 RuleHitInput 结构，
            位置信息与建议/依据挂在 extra 上，供构造 RiskItem 使用。

        【位置字段必须显式保留 None 键】
            extra 中的 char_start / char_end / page_no / paragraph_index /
            matched_value 即便取值为 None 也必须显式写入。
            原因：下游 build_risk_items 按 extra["char_start"] is not None
            判定"该命中是否锚定到具体条款"；若此处省略 None 键，
            读取侧只能拿到 dict.get() 的 None，判定逻辑将退化为恒假，
            导致合同级规则（missing_clause / numeric_threshold）在每条条款上
            各产出一条内容完全相同的风险项。详见本方法调用方的去重逻辑。

        参数：
            rule: 规则模板
            result: 匹配结果
            clause_id: 命中条款 ID（合同级命中时为 None）
            clause_type: 命中条款类型

        返回：
            可直接传入 evaluate 的命中输入字典
        """
        return {
            "rule_code": rule.rule_code,
            "rule_name": rule.rule_name,
            "severity": rule.severity,
            "matched": result.matched,
            "clause_id": clause_id,
            "clause_type": clause_type,
            "evidence": result.evidence,
            # 透传匹配模式与配置：供 build_risk_items 判定规则作用域（去重依据）
            "match_type": rule.match_type,
            "match_config": dict(rule.match_config or {}),
            "extra": {
                "override_risk_level": rule.override_risk_level,
                "suggestion_template": rule.suggestion_template,
                "legal_basis": rule.legal_basis,
                "template_version": rule.version,
                "template_priority": rule.priority,
                # 精确位置信息，供 RiskItem 溯源（None 也必须保留键）
                "char_start": result.char_start,
                "char_end": result.char_end,
                "page_no": result.page_no,
                "paragraph_index": result.paragraph_index,
                "matched_value": result.matched_value,
            },
        }

    @staticmethod
    def _render_suggestion(template_text: str, evidence: str) -> str:
        """
        渲染修改建议模板

        说明：
            suggestion_template 可含 {evidence} 占位符，替换为实际证据片段。
            渲染长度受配置 SUGGESTION_MAX_LENGTH 限制。

        参数：
            template_text: 建议模板文本
            evidence: 证据片段

        返回：
            渲染后的建议文本
        """
        if not template_text:
            return ""

        try:
            rendered = template_text.replace("{evidence}", evidence or "（证据片段）")
        except (AttributeError, TypeError):
            rendered = template_text

        max_length = settings.SUGGESTION_MAX_LENGTH
        if len(rendered) > max_length:
            rendered = rendered[:max_length]

        return rendered


# ======================================================================
# 五、规则注册表（YAML 配置加载）
# ======================================================================
class RuleRegistry:
    """
    规则注册表 —— 从 YAML 配置文件加载风险规则

    【职责】
        1. 解析 YAML 配置文件为 RuleTemplate 列表
        2. 严格校验每条规则的字段合法性（复用 RuleTemplate 的校验器）
        3. 支持按合同类型分组查询与优先级排序

    【YAML 文件结构约定】
        rules:
          - rule_code: "PUR-001"
            rule_name: "缺少质量保证条款"
            description: "..."
            severity: "major"              # info/minor/major/fatal
            match_type: "missing_clause"   # keyword/regex/clause_type/semantic/
                                           # field_compare/missing_clause/numeric_threshold
            match_config:
              require_clause_types: ["warranty"]
            contract_types: ["purchase"]   # 缺省/含 all 表示通用
            priority: 100
            enabled: true                  # false 表示停用
            suggestion_template: "..."
            legal_basis: "..."
            override_risk_level: null      # 可选，直接指定风险等级

    参数说明：
        - enabled: 布尔值，false 时加载为 status=disabled（不参与匹配）
        - override_risk_level: 可选，非空时优先于 severity 映射
    """

    def __init__(self, path: Optional[str] = None) -> None:
        """
        初始化注册表

        参数：
            path: 可选，初始化时立即加载的 YAML 路径
        """
        self._rules: List[RuleTemplate] = []
        if path:
            self._rules = self.load_yaml(path)

    def load_yaml(self, path: str) -> List[RuleTemplate]:
        """
        加载 YAML 规则配置文件

        参数：
            path: YAML 文件路径

        返回：
            RuleTemplate 实例列表

        异常：
            RuleEngineException：文件不存在、语法错误或规则字段非法
        """
        file_path = Path(path)
        if not file_path.is_file():
            raise RuleEngineException(
                message="规则配置文件不存在",
                detail=f"path={path}",
            )

        try:
            with file_path.open("r", encoding="utf-8") as stream:
                data = yaml.safe_load(stream)
        except yaml.YAMLError as exc:
            raise RuleEngineException(
                message="规则配置文件 YAML 语法错误",
                detail=f"path={path}, error={exc}",
            ) from exc

        if not isinstance(data, dict) or "rules" not in data:
            raise RuleEngineException(
                message="规则配置文件缺少顶层 'rules' 列表",
                detail=f"path={path}",
            )

        raw_rules = data.get("rules") or []
        if not isinstance(raw_rules, list) or not raw_rules:
            raise RuleEngineException(
                message="规则配置文件 'rules' 为空",
                detail=f"path={path}",
            )

        templates: List[RuleTemplate] = []
        for index, raw in enumerate(raw_rules):
            if not isinstance(raw, dict):
                raise RuleEngineException(
                    message=f"第 {index + 1} 条规则不是合法的字典结构",
                    detail=f"path={path}",
                )

            templates.append(self._parse_rule(raw, index, path))

        # 规则编码唯一性校验（重复编码会导致判定不可追溯）
        codes = [rule.rule_code for rule in templates]
        duplicates = {code for code in codes if codes.count(code) > 1}
        if duplicates:
            raise RuleEngineException(
                message=f"规则编码重复：{sorted(duplicates)}",
                detail=f"path={path}",
            )

        self._rules = templates
        logger.info(
            "规则配置加载完成 | 路径=%s | 规则数=%d", path, len(templates)
        )
        return templates

    def _parse_rule(
        self,
        raw: Dict[str, Any],
        index: int,
        path: str,
    ) -> RuleTemplate:
        """
        解析单条 YAML 规则为 RuleTemplate

        说明：
            YAML 中的 enabled 字段映射为 RuleTemplate.status：
                enabled=true  → status="enabled"
                enabled=false → status="disabled"
            其余字段直接透传，由 RuleTemplate 的校验器做合法性把关。

        参数：
            raw: 单条规则的字典
            index: 规则在列表中的序号（用于错误定位）
            path: 源文件路径

        返回：
            RuleTemplate 实例

        异常：
            RuleEngineException：字段缺失或非法
        """
        required = ("rule_code", "rule_name", "match_type")
        missing = [key for key in required if not raw.get(key)]
        if missing:
            raise RuleEngineException(
                message=f"第 {index + 1} 条规则缺少必需字段：{missing}",
                detail=f"path={path}",
            )

        # 构造 RuleTemplate 参数（enabled 转为 status）
        payload: Dict[str, Any] = {
            "rule_code": raw["rule_code"],
            "rule_name": raw["rule_name"],
            "description": raw.get("description", ""),
            "clause_type": raw.get("clause_type", "other"),
            "match_type": raw["match_type"],
            "match_config": raw.get("match_config") or {},
            "contract_types": raw.get("contract_types", ["all"]),
            "priority": int(raw.get("priority", 100)),
            "severity": raw.get("severity", RuleSeverity.INFO.value),
            "override_risk_level": raw.get("override_risk_level"),
            "suggestion_template": raw.get("suggestion_template", ""),
            "legal_basis": raw.get("legal_basis", ""),
            "status": "enabled" if raw.get("enabled", True) else "disabled",
            "version": int(raw.get("version", 1)),
        }

        try:
            return RuleTemplate(**payload)
        except (ValueError, TypeError) as exc:
            raise RuleEngineException(
                message=f"第 {index + 1} 条规则字段非法：{exc}",
                detail=f"path={path}, rule_code={raw.get('rule_code')}",
            ) from exc

    def filter_active(self, contract_type: str) -> List[RuleTemplate]:
        """
        按合同类型过滤启用规则并按优先级排序

        参数：
            contract_type: 目标合同类型

        返回：
            过滤并排序后的规则列表
        """
        active = [
            rule for rule in self._rules
            if rule.is_active and rule.matches_contract_type(contract_type)
        ]
        active.sort(key=lambda rule: (-rule.priority, rule.rule_code))
        return active

    @property
    def rules_by_contract_type(self) -> Dict[str, List[RuleTemplate]]:
        """
        按合同类型分组管理规则

        说明：
            返回 {合同类型: 规则列表} 的映射，便于按类型批量查询与展示。
            通用规则（含 all）会归入 "all" 分组。

        返回：
            合同类型 → 规则列表 的字典
        """
        grouped: Dict[str, List[RuleTemplate]] = {}
        for rule in self._rules:
            types = rule.contract_types or ["all"]
            for contract_type in types:
                grouped.setdefault(contract_type, []).append(rule)
        return grouped

    @property
    def size(self) -> int:
        """已加载规则总数"""
        return len(self._rules)


# 全局单例：所有判定必须通过该实例执行
rule_engine = RuleEngine()

# 全局规则注册表单例：YAML 配置加载的唯一入口
rule_registry = RuleRegistry()
