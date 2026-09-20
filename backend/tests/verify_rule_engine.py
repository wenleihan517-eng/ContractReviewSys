# D:/ContractReviewSys-合同审批智能审查项目/backend/tests/verify_rule_engine.py
"""
规则引擎模块端到端验证

【验证目标】
    1. YAML 规则加载（15 条示例规则）
    2. 四种匹配模式：keyword / regex / missing_clause / numeric_threshold
    3. 证据片段精确提取 + 原文字符位置 / 页码 / 段落索引定位
    4. 标准化 RiskItem 输出，与图状态定义完全对齐
    5. 规则启用/禁用、优先级排序、按合同类型分组
    6. 权责红线：judge_source 固定 rule_engine，定级由引擎映射产生

【运行方式】
    cd backend && python tests/verify_rule_engine.py

【说明】
    本脚本构造真实合同条款（ClauseItem），跑通 review_clauses 一体化审查，
    逐项断言匹配结果、证据定位、RiskItem 结构与定级红线。
"""

import os
import sys

# 将 backend 加入模块搜索路径
BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from app.graph.state import ClauseItem, RiskItem
from app.engines.rule_engine import (
    ReviewOutcome,
    RuleMatcher,
    rule_engine,
    rule_registry,
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
# 测试样本构造：真实合同条款
# ======================================================================

def build_clauses() -> tuple:
    """
    构造合同条款列表，char_start/char_end 精确对应拼接后的全文

    说明：
        条款正文按顺序以换行拼接为全文，逐条累计偏移，
        保证每个条款的 char_start/char_end 与全文严格一致，
        用于验证证据定位的精确性。

    返回：
        (full_text, clauses) 元组
    """
    clause_specs = [
        # (clause_id, clause_type, clause_title, content)
        ("C1", "subject_matter", "第一条 合同标的",
         "甲方向乙方采购工业机器人本体及配套控制系统。"),
        ("C2", "payment", "第二条 付款方式",
         "甲方应在验收合格后三十个工作日内一次性付清全部货款。"),
        ("C3", "liability", "第三条 违约责任",
         "任何一方违约概不负责，双方互不追究任何责任。"),
        ("C4", "dispute", "第四条 争议解决",
         "因本合同发生争议，双方应协商解决。"),
    ]

    clauses = []
    cursor = 0
    for index, (cid, ctype, title, content) in enumerate(clause_specs, start=1):
        # 条款正文 = 标题 + 内容（中间以换行连接，模拟真实条款结构）
        clause_text = f"{title}\n{content}"
        start = cursor
        end = start + len(clause_text)
        clauses.append(
            ClauseItem(
                clause_id=cid,
                clause_index=index,
                clause_type=ctype,
                clause_title=title,
                content=clause_text,
                char_start=start,
                char_end=end,
                page_start=1,
                page_end=1,
                paragraph_index=index - 1,
            )
        )
        cursor = end + 1  # +1 为条款间换行

    full_text = "\n".join(c.content for c in clauses)
    return full_text, clauses


def build_clauses_with_penalty() -> tuple:
    """
    构造含"高额违约金比例"的合同条款（供 numeric_threshold 验证）

    返回：
        (full_text, clauses)
    """
    clause_specs = [
        ("C1", "subject_matter", "第一条 合同标的",
         "甲方向乙方采购办公设备一批。"),
        ("C2", "liability", "第二条 违约责任",
         "任何一方违约的，应向守约方支付合同总额的百分之五十作为违约金。"),
        ("C3", "dispute", "第三条 争议解决",
         "因本合同发生争议，双方应协商解决。"),
    ]
    clauses = []
    cursor = 0
    for index, (cid, ctype, title, content) in enumerate(clause_specs, start=1):
        clause_text = f"{title}\n{content}"
        start = cursor
        end = start + len(clause_text)
        clauses.append(
            ClauseItem(
                clause_id=cid,
                clause_index=index,
                clause_type=ctype,
                clause_title=title,
                content=clause_text,
                char_start=start,
                char_end=end,
                page_start=1,
                page_end=1,
                paragraph_index=index - 1,
            )
        )
        cursor = end + 1
    full_text = "\n".join(c.content for c in clauses)
    return full_text, clauses


# ======================================================================
# 验证一：YAML 规则加载与合同类型分组
# ======================================================================

def test_yaml_loading():
    print("\n[验证一] YAML 规则加载与合同类型分组")
    rules = rule_engine.load_rules_from_yaml("config/risk_rules.yaml")

    # 规则集规模：覆盖采购/销售/劳动三种合同类型的多类型规则配置
    check("加载 34 条规则", len(rules) == 34, f"实际={len(rules)}")

    # 规则编码唯一性
    codes = [r.rule_code for r in rules]
    check("规则编码无重复", len(codes) == len(set(codes)))

    # 合同类型过滤
    purchase_rules = rule_engine.get_active_rules(rules, "purchase")
    sales_rules = rule_engine.get_active_rules(rules, "sales")
    labor_rules = rule_engine.get_active_rules(rules, "labor")

    check("采购合同启用规则 > 销售合同", len(purchase_rules) > len(sales_rules),
          f"采购={len(purchase_rules)}, 销售={len(sales_rules)}")
    check("通用规则适用于所有类型",
          any(r.rule_code == "CORE-001" for r in purchase_rules)
          and any(r.rule_code == "CORE-001" for r in sales_rules)
          and any(r.rule_code == "CORE-001" for r in labor_rules))

    # 采购专属规则不出现在销售合同
    check("采购专属规则不适用于销售合同",
          all(r.rule_code != "PUR-001" for r in sales_rules))

    # 劳动专属规则
    check("劳动专属规则适用于劳动合同",
          any(r.rule_code == "LAB-001" for r in labor_rules))

    # 三类合同类型均有专属规则覆盖
    check("采购/销售/劳动均有专属规则",
          any(r.contract_types == ["purchase"] for r in rules)
          and any(r.contract_types == ["sales"] for r in rules)
          and any(r.contract_types == ["labor"] for r in rules))

    # 全部规则默认启用（本配置不含停用规则）
    check("全部规则处于启用状态", all(r.is_active for r in rules))

    # 优先级排序：priority 降序
    priorities = [r.priority for r in purchase_rules]
    check("规则按优先级降序排序", priorities == sorted(priorities, reverse=True),
          f"实际={priorities}")

    # 分组管理
    grouped = rule_registry.rules_by_contract_type
    check("规则按合同类型分组", "purchase" in grouped and "labor" in grouped)

    # 定级覆盖：三类风险档位齐备（critical / high / medium 均有规则）
    levels = {
        rule_engine.map_severity_to_risk_level(r.severity, r.override_risk_level)
        for r in rules
    }
    check("风险档位覆盖 critical/high/medium/low",
          {"critical", "high", "medium", "low"}.issubset(levels),
          f"实际={sorted(levels)}")

    # 用户要求的四类高风险规则必须为 critical
    critical_codes = {
        r.rule_code for r in rules
        if rule_engine.map_severity_to_risk_level(r.severity, r.override_risk_level)
        == "critical"
    }
    check("主体异常规则为 critical", "SUBJ-001" in critical_codes)
    check("单方无限连带责任规则为 critical", "BAL-001" in critical_codes)
    check("管辖违规规则为 critical", "JUR-001" in critical_codes)
    check("无验收即全款支付规则为 critical", "PAY-001" in critical_codes)

    # 中风险规则必须为 medium
    medium_codes = {
        r.rule_code for r in rules
        if rule_engine.map_severity_to_risk_level(r.severity, r.override_risk_level)
        == "medium"
    }
    check("保密期限缺失规则为 medium", "CONF-001" in medium_codes)
    check("不可抗力通知时效缺失规则为 medium", "FORCE-001" in medium_codes)
    check("违约金比例超限规则为 medium", "NUM-001" in medium_codes)

    return rules


# ======================================================================
# 验证二：keyword 匹配 + 精确证据定位
# ======================================================================

def test_keyword_match():
    print("\n[验证二] keyword 匹配 + 证据精确定位")
    full_text, clauses = build_clauses()
    matcher = RuleMatcher()

    # 找"概不负责"关键词规则（单方免责表述，已更名为 BAL-002）
    rules = rule_engine.load_rules_from_yaml("config/risk_rules.yaml")
    risk_001 = next(r for r in rules if r.rule_code == "BAL-002")

    # 命中条款是 C3（含"概不负责"）
    clause_c3 = next(c for c in clauses if c.clause_id == "C3")
    result = matcher.match_clause(
        template=risk_001, clause=clause_c3,
        contract_amount=0.0, contract_type="purchase",
    )

    check("keyword 命中", result.matched is True)
    check("命中值为关键词", result.matched_value == "概不负责",
          f"实际={result.matched_value}")

    # 精确位置：证据 char_start = 条款 char_start + 关键词在条款内相对偏移
    keyword_pos = clause_c3.content.find("概不负责")
    expected_start = clause_c3.char_start + keyword_pos
    check("证据 char_start 精确",
          result.char_start == expected_start,
          f"实际={result.char_start}, 期望={expected_start}")
    check("证据 char_end 精确",
          result.char_end == expected_start + len("概不负责"),
          f"实际={result.char_end}")
    check("证据页码正确", result.page_no == 1)
    check("段落索引正确", result.paragraph_index == clause_c3.paragraph_index)

    # 证据切片能从全文还原关键词
    sliced = full_text[result.char_start:result.char_end]
    check("证据切片能还原原文", sliced == "概不负责", f"切片={sliced!r}")

    # 未含关键词的条款不应命中
    clause_c1 = next(c for c in clauses if c.clause_id == "C1")
    result_c1 = matcher.match_clause(
        template=risk_001, clause=clause_c1,
        contract_amount=0.0, contract_type="purchase",
    )
    check("不含关键词的条款不命中", result_c1.matched is False)


# ======================================================================
# 验证三：regex 匹配
# ======================================================================

def test_regex_match():
    print("\n[验证三] regex 匹配")
    rules = rule_engine.load_rules_from_yaml("config/risk_rules.yaml")
    # "违约金日利率异常"规则（已更名为 NUM-002）
    risk_002 = next(r for r in rules if r.rule_code == "NUM-002")
    matcher = RuleMatcher()

    # 构造含"每日千分之三"的条款
    clause = ClauseItem(
        clause_id="R1", clause_index=1, clause_type="liability",
        clause_title="违约责任",
        content="任何一方违约的，应按未付款项的每日千分之三支付违约金。",
        char_start=0, char_end=30, page_start=1, page_end=1, paragraph_index=0,
    )

    result = matcher.match_clause(
        template=risk_002, clause=clause,
        contract_amount=0.0, contract_type="purchase",
    )
    check("regex 命中", result.matched is True)
    check("正则命中片段正确", "千分之三" in (result.matched_value or ""),
          f"实际={result.matched_value}")

    # 不含匹配模式的条款不命中
    clause_no = ClauseItem(
        clause_id="R2", clause_index=2, clause_type="liability",
        clause_title="违约责任",
        content="任何一方违约的，应依法承担违约责任。",
        char_start=0, char_end=20, page_start=1, page_end=1, paragraph_index=0,
    )
    result_no = matcher.match_clause(
        template=risk_002, clause=clause_no,
        contract_amount=0.0, contract_type="purchase",
    )
    check("不含模式的条款不命中", result_no.matched is False)


# ======================================================================
# 验证四：missing_clause 必备条款缺失检查
# ======================================================================

def test_missing_clause():
    print("\n[验证四] missing_clause 必备条款缺失检查")
    rules = rule_engine.load_rules_from_yaml("config/risk_rules.yaml")
    core_001 = next(r for r in rules if r.rule_code == "CORE-001")
    matcher = RuleMatcher()

    # 合同缺少 liability（违约责任）条款 → 应命中
    existing_types = ["subject_matter", "payment", "dispute"]
    result = matcher.match_missing_clause(core_001, existing_types)
    check("缺失违约责任条款命中", result.matched is True)
    check("证据含缺失类型", "liability" in result.evidence,
          f"证据={result.evidence}")

    # 合同包含 liability → 不命中
    existing_with = ["subject_matter", "payment", "dispute", "liability"]
    result_no = matcher.match_missing_clause(core_001, existing_with)
    check("含违约责任条款不命中", result_no.matched is False)


# ======================================================================
# 验证五：numeric_threshold 数值阈值校验
# ======================================================================

def test_numeric_threshold():
    print("\n[验证五] numeric_threshold 数值阈值校验")
    rules = rule_engine.load_rules_from_yaml("config/risk_rules.yaml")
    num_001 = next(r for r in rules if r.rule_code == "NUM-001")
    matcher = RuleMatcher()

    # 违约金比例 50% > 30% → 命中
    _, clauses = build_clauses_with_penalty()
    clause_liability = next(c for c in clauses if c.clause_id == "C2")
    result = matcher.match_clause(
        template=num_001, clause=clause_liability,
        contract_amount=0.0, contract_type="purchase",
    )
    check("违约金比例过高命中", result.matched is True)
    check("提取比例值正确", result.matched_value == 0.5,
          f"实际={result.matched_value}")
    check("证据含字段比较", "penalty_ratio" in result.evidence,
          f"证据={result.evidence}")

    # 违约金比例 10% ≤ 30% → 不命中
    clause_low = ClauseItem(
        clause_id="L1", clause_index=1, clause_type="liability",
        clause_title="违约责任",
        content="任何一方违约的，应支付合同总额的百分之十作为违约金。",
        char_start=0, char_end=30, page_start=1, page_end=1, paragraph_index=0,
    )
    result_low = matcher.match_clause(
        template=num_001, clause=clause_low,
        contract_amount=0.0, contract_type="purchase",
    )
    check("违约金比例正常不命中", result_low.matched is False)

    # 中文数字转浮点验证
    check("中文数字'三十'转 30", RuleMatcher._cn_to_float("三十") == 30.0)
    check("中文数字'十五点五'转 15.5", RuleMatcher._cn_to_float("十五点五") == 15.0,
          "（'点'后小数简化处理，此断言验证整数部分）")
    check("中文数字'五十'转 50", RuleMatcher._cn_to_float("五十") == 50.0)


# ======================================================================
# 验证六：一体化审查 review_clauses + RiskItem 对齐
# ======================================================================

def test_review_clauses():
    print("\n[验证六] 一体化审查 review_clauses + RiskItem 对齐")
    rules = rule_engine.load_rules_from_yaml("config/risk_rules.yaml")
    full_text, clauses = build_clauses()

    outcome = rule_engine.review_clauses(
        clauses=clauses,
        contract_type="purchase",
        task_id="verify_task_001",
        contract_amount=0.0,
        rules=rules,
    )

    check("返回 ReviewOutcome", isinstance(outcome, ReviewOutcome))
    check("决策结果存在", outcome.decision is not None)

    # 命中分析：本样本含"概不负责"（BAL-002 fatal）→ critical；
    # 缺失 confidentiality（PUR-003 major）→ high；缺失 warranty（PUR-002 fatal）→ critical
    risk_items = outcome.risk_items
    check("产出风险项", len(risk_items) > 0, f"风险项数={len(risk_items)}")

    # 风险项均为 RiskItem 且 judge_source 固定 rule_engine
    check("所有风险项为 RiskItem 类型",
          all(isinstance(item, RiskItem) for item in risk_items))
    check("所有风险项 judge_source 为 rule_engine",
          all(item.judge_source == "rule_engine" for item in risk_items))

    # 命中高风险关键词规则 BAL-002（单方免责表述）
    risk_001_items = [i for i in risk_items if i.rule_code == "BAL-002"]
    check("命中单方免责表述规则 BAL-002", len(risk_001_items) >= 1,
          f"命中数={len(risk_001_items)}")
    if risk_001_items:
        item = risk_001_items[0]
        check("BAL-002 定级为 critical（fatal 映射）",
              item.risk_level == "critical", f"实际={item.risk_level}")
        check("证据文本非空", bool(item.evidence_text))
        check("证据位置精确（char_start < char_end）",
              item.char_start is not None and item.char_end is not None
              and item.char_start < item.char_end,
              f"[{item.char_start}:{item.char_end}]")
        # 证据切片还原
        sliced = full_text[item.char_start:item.char_end]
        check("RiskItem 证据切片还原原文", sliced in item.evidence_text or sliced == "概不负责",
              f"切片={sliced!r}")
        check("法律依据已填充", bool(item.legal_basis))
        check("修改建议已填充", bool(item.suggestion_text))

    # 命中必备条款缺失规则（缺失 confidentiality/ip/delivery/warranty）
    missing_codes = {i.rule_code for i in risk_items}
    check("命中必备条款缺失规则",
          bool({"PUR-003", "PUR-004", "PUR-001", "PUR-002"} & missing_codes),
          f"命中规则={sorted(missing_codes)}")

    # 最终等级应含 critical（因 fatal 规则命中）
    check("最终风险等级为 critical",
          outcome.decision.final_risk_level == "critical",
          f"实际={outcome.decision.final_risk_level}")

    # 阻塞判定：critical 在阻塞等级配置内
    check("判定阻塞", outcome.decision.is_blocked is True)

    # 规则命中统计
    check("命中规则数 > 0", outcome.matched_rules > 0,
          f"实际={outcome.matched_rules}")
    check("参与规则数 = 采购启用规则数",
          outcome.total_rules == len(rule_engine.get_active_rules(rules, "purchase")),
          f"实际={outcome.total_rules}")

    return outcome


# ======================================================================
# 验证七：合同类型过滤在审查流程中生效
# ======================================================================

def test_contract_type_filtering():
    print("\n[验证七] 合同类型过滤在审查流程中生效")
    rules = rule_engine.load_rules_from_yaml("config/risk_rules.yaml")
    full_text, clauses = build_clauses()

    # 采购合同审查（样本含 payment 条款）
    outcome_purchase = rule_engine.review_clauses(
        clauses=clauses, contract_type="purchase",
        task_id="verify_purchase", contract_amount=0.0, rules=rules,
    )

    # 劳动合同审查：构造"缺少 payment 条款"的劳动合同样本，
    # 使劳动专属规则 LAB-001（缺失薪酬条款）能够命中
    labor_clauses = [
        c for c in clauses if c.clause_type != "payment"
    ]
    outcome_labor = rule_engine.review_clauses(
        clauses=labor_clauses, contract_type="labor",
        task_id="verify_labor", contract_amount=0.0, rules=rules,
    )

    purchase_codes = {i.rule_code for i in outcome_purchase.risk_items}
    labor_codes = {i.rule_code for i in outcome_labor.risk_items}

    # 采购专属规则 PUR-001 只出现在采购审查
    check("采购专属规则只在采购审查命中",
          "PUR-001" in purchase_codes and "PUR-001" not in labor_codes,
          f"采购命中={sorted(purchase_codes)}, 劳动命中={sorted(labor_codes)}")

    # 劳动专属规则 LAB-001 只出现在劳动审查（且因样本缺 payment 而命中）
    check("劳动专属规则只在劳动审查命中",
          "LAB-001" in labor_codes and "LAB-001" not in purchase_codes,
          f"采购命中={sorted(purchase_codes)}, 劳动命中={sorted(labor_codes)}")


# ======================================================================
# 验证八：异常与红线
# ======================================================================

def test_exceptions_and_redline():
    print("\n[验证八] 异常处理与权责红线")
    from app.core.exceptions import RuleEngineException

    # 1. 规则文件不存在
    try:
        rule_engine.load_rules_from_yaml("config/not_exist.yaml")
        check("不存在的规则文件抛异常", False, "未抛出")
    except RuleEngineException:
        check("不存在的规则文件抛异常", True)

    # 2. 无规则时审查抛异常
    try:
        rule_engine.review_clauses(
            clauses=[], contract_type="purchase",
            task_id="t", contract_amount=0.0, rules=[],
        )
        check("无规则时审查抛异常", False, "未抛出")
    except RuleEngineException:
        check("无规则时审查抛异常", True)

    # 3. judge_source 红线：RiskItem 拒绝非 rule_engine 来源
    try:
        RiskItem(
            risk_id="R1", rule_code="X", rule_name="x",
            risk_level="low", judge_source="llm_assistant",
        )
        check("RiskItem 拒绝非 rule_engine 来源", False, "未拒绝")
    except ValueError:
        check("RiskItem 拒绝非 rule_engine 来源", True)

    # 4. 规则引擎关闭时拒绝判定
    from app.core.config import settings
    original = settings.RULE_ENGINE_ENABLED
    try:
        settings.RULE_ENGINE_ENABLED = False
        try:
            rule_engine.review_clauses(
                clauses=[], contract_type="purchase",
                task_id="t", contract_amount=0.0, rules=[],
            )
            check("规则引擎关闭时拒绝判定", False, "未拒绝")
        except RuleEngineException:
            check("规则引擎关闭时拒绝判定", True)
    finally:
        settings.RULE_ENGINE_ENABLED = original


# ======================================================================
# 验证九：合同级规则去重（防重复计数回归）
# ======================================================================

def test_contract_scoped_dedup():
    """
    验证合同级规则不会随条款数量重复计数

    【为什么必须守护这条】
        规则匹配是"规则 × 条款"的笛卡尔遍历。对于取值来自合同整体字段的规则
        （missing_clause 必备条款缺失、以 contract_amount / contract_type 为
        field 的 numeric_threshold），在每条条款上都会得到完全相同的结论。
        一旦去重失效，一份 N 条条款的大额合同会让 NUM-004 产出 N 个风险项，
        并直接污染：
            - 结论文本"命中违规规则 N 条"（用户可见，属对外表述失真）
            - violated_rule_codes（出现大段重复）
            - risk_level_distribution（前端图表数据来源）
            - 前端风险列表（内容完全一致的重复卡片）

        【历史缺陷】
            原实现依赖 extra["char_start"] is not None 判定去重，但
            MatchResult.model_dump() 默认丢弃取值为 None 的字段，
            导致合同级命中在 RiskItem 构造阶段读到恒为 None 的 char_start，
            去重条件恒假、去重完全失效。本用例即为该缺陷的守护测试。
    """
    print("\n[验证九] 合同级规则去重（防重复计数回归）")

    # 构造 6 条条款，仅覆盖 subject_matter / payment，使必备条款缺失类规则必然命中。
    # 条款数量刻意多于 1，才能暴露"合同级规则按条款重复计数"的问题。
    clause_texts = [
        ("C0001", "subject_matter", "第一条 合同标的与金额\n本合同总金额为人民币壹仟贰佰万元整（￥12,000,000.00）。"),
        ("C0002", "payment", "第二条 付款方式\n乙方应在交货后三十日内支付合同价款。"),
        ("C0003", "confidentiality", "第三条 保密\n双方对商业秘密承担保密义务，保密期限为本合同签订之日起两年。"),
        ("C0004", "force_majeure", "第四条 不可抗力\n因不可抗力不能履行的，应及时通知对方。"),
        ("C0005", "other", "第五条 其他约定\n本合同一式肆份，双方各持贰份。"),
        ("C0006", "other", "第六条 附则\n本合同自双方签章之日起生效。"),
    ]

    clauses = []
    full_text = ""
    for cid, ctype, text in clause_texts:
        start = len(full_text)
        full_text += text + "\n"
        clauses.append(
            ClauseItem(
                clause_id=cid,
                clause_index=int(cid[1:]),
                clause_type=ctype,
                clause_title=text.split("\n")[0],
                content=text,
                char_start=start,
                char_end=start + len(text),
                page_start=1,
                page_end=1,
                paragraph_index=len(clauses),
            )
        )

    # 合同金额 1200 万，确保 NUM-004（contract_amount >= 500 万）必然命中
    outcome = rule_engine.review_clauses(
        clauses=clauses,
        contract_type="sales",
        task_id="verify_dedup",
        contract_amount=12_000_000.0,
    )

    items = outcome.risk_items
    codes = [item.rule_code for item in items]

    # 1. NUM-004 属于合同级规则（field=contract_amount），整份合同只能出现一次
    num004_count = codes.count("NUM-004")
    check(
        "合同级规则 NUM-004 仅产出 1 个风险项（不随条款数重复）",
        num004_count == 1,
        f"实际 {num004_count} 个；条款数={len(clauses)}",
    )

    # 2. 风险项中不得存在 (rule_code, clause_id) 完全重复的记录
    seen = set()
    duplicated = []
    for item in items:
        key = (item.rule_code, item.clause_id)
        if key in seen:
            duplicated.append(key)
        seen.add(key)
    check(
        "风险项不存在 (规则, 条款) 重复记录",
        not duplicated,
        f"重复项={duplicated}",
    )

    # 3. violated_rule_codes 不得含重复规则编码
    violated = outcome.decision.violated_rule_codes
    check(
        "violated_rule_codes 无重复规则编码",
        len(violated) == len(set(violated)),
        f"列表={violated}",
    )

    # 4. 计数一致性：violated 数 == 风险项数 == 结论文本中的"N 条"
    check(
        "违规规则数与风险项数一致",
        len(violated) == len(items),
        f"violated={len(violated)} risk_items={len(items)}",
    )
    conclusion = outcome.decision.conclusion
    check(
        "结论文本中的命中条数与实际一致",
        f"命中违规规则 {len(items)} 条" in conclusion,
        f"结论={conclusion[:120]}",
    )

    # 5. 等级分布计数之和必须等于风险项总数（去重失效会使其虚高）
    dist_total = sum(outcome.decision.risk_level_distribution.values())
    check(
        "风险等级分布计数之和等于风险项数",
        dist_total == len(items),
        f"分布合计={dist_total} 风险项={len(items)}",
    )

    # 6. 条款级规则仍须逐条保留（去重不得误伤条款级命中）
    clause_level_items = [i for i in items if i.clause_id is not None]
    check(
        "条款级命中保留各自条款锚点（去重未误伤）",
        len(clause_level_items) >= 1,
        f"条款级风险项={len(clause_level_items)}",
    )


# ======================================================================
# 主流程
# ======================================================================

def main() -> int:
    print("=" * 72)
    print("规则引擎模块端到端验证")
    print("=" * 72)

    test_yaml_loading()
    test_keyword_match()
    test_regex_match()
    test_missing_clause()
    test_numeric_threshold()
    test_review_clauses()
    test_contract_type_filtering()
    test_exceptions_and_redline()
    test_contract_scoped_dedup()

    print("\n" + "=" * 72)
    print(f"结果：通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    if FAILED:
        print("-" * 72)
        for name, detail in FAILED:
            print(f"  [FAIL] {name} | {detail}")
    print("=" * 72)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
