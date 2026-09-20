# D:/ContractReviewSys-合同审批智能审查项目/backend/app/graph/routes.py
"""
差异化流程路由 — 三条分支的判定逻辑

【文件定位】
    本模块实现架构文档规定的三条差异化审查链路的分支判定：
        ┌──────────────┬───────────────────────────────────────────────┐
        │ 分支          │ 触发条件与走向                                 │
        ├──────────────┼───────────────────────────────────────────────┤
        │ 阻断分支      │ 解析失败 / 文档加密 / 扫描件模糊 → 直接 audit   │
        │              │ 并标记 blocked，不进入任何审查环节              │
        │ 快速通道      │ 模板合同 → 仅规则审查 → 风险融合               │
        │              │ 跳过向量召回与大模型审查，降低耗时              │
        │ 深度复核      │ 大额（≥500万）或高风险合同类型 → 追加大模型审查 │
        │              │ 标准通道 + 强制法务人工复核网关                 │
        └──────────────┴───────────────────────────────────────────────┘

【路由决策的确定性原则】
    路由判定完全由配置与客观字段驱动（模板标记、金额、合同类型、解析状态），
    不引入任何模型判断。相同输入必得相同路由结果，保证流程可复现、可审计。

返回值说明：
    - route_after_pull(state)：返回 pull_approval_task 之后的目标节点名
    - route_after_parse(state)：返回 parse_document 之后的目标节点名
    - route_after_rule(state)：返回 rule_review 之后的目标节点名
    - route_after_risk_merge(state)：返回 risk_merge 之后的目标节点名

使用示例：
    graph.add_conditional_edges(
        WorkflowNode.PARSE_CONTRACT.value,
        route_after_parse,
        {"rule_review": "rule_review", "audit": "audit"},
    )
"""

from typing import List

from app.constants import WorkflowNode
from app.core.config import settings
from app.core.logging_config import get_logger
from app.graph.state import ReviewTaskState, ParseStatus
from app.models.documents.review_task import RouteType

logger = get_logger(__name__)


# ======================================================================
# 一、路由决策说明常量（供文档与审计使用）
# ======================================================================
ROUTE_REASON_TEMPLATE_NOT_SET = "非模板合同，走标准审查链路"
ROUTE_REASON_TEMPLATE_FAST = "模板合同命中快速通道，仅执行结构化规则审查"


def decide_route_type(state: ReviewTaskState) -> str:
    """
    依据合同客观字段判定差异化路由类型

    决策优先级（从高到低）：
        1. 大额或高风险类型 → deep（深度复核）
        2. 模板合同且快速通道已启用 → fast（快速通道）
        3. 其余 → standard（标准审查）

    说明：
        优先级 1 高于 2 是刻意的：一份大额的模板合同依然必须走深度复核。
        "模板"只代表条款形态规范，不代表金额风险可控，二者不可相互抵消。

    参数：
        state: 当前图状态，读取 contract_amount / contract_type / template_flag

    返回：
        路由类型字符串，取值见 RouteType 枚举
    """
    # -------------------- 优先判定深度复核 --------------------
    deep_reasons: List[str] = []

    amount_threshold = settings.ROUTE_DEEP_AMOUNT_THRESHOLD
    if state.contract_amount >= amount_threshold:
        deep_reasons.append(
            f"合同金额 {state.contract_amount:,.2f} {state.currency} "
            f"达到大额阈值 {amount_threshold:,.2f}"
        )

    force_types = settings.deep_force_type_list
    if state.contract_type in force_types:
        deep_reasons.append(f"合同类型 {state.contract_type} 属于强制深度复核类型")

    if deep_reasons:
        logger.info(
            "路由决策-深度复核 | 任务=%s | 依据=%s",
            state.task_no,
            "；".join(deep_reasons),
        )
        return RouteType.DEEP.value

    # -------------------- 其次判定快速通道 --------------------
    if state.template_flag and settings.ROUTE_FAST_ENABLED:
        logger.info(
            "路由决策-快速通道 | 任务=%s | 依据=%s",
            state.task_no,
            ROUTE_REASON_TEMPLATE_FAST,
        )
        return RouteType.FAST.value

    # -------------------- 默认标准审查 --------------------
    logger.info(
        "路由决策-标准审查 | 任务=%s | 金额=%.2f | 类型=%s | 模板=%s",
        state.task_no,
        state.contract_amount,
        state.contract_type,
        state.template_flag,
    )
    return RouteType.STANDARD.value


# ======================================================================
# 二、分支一：任务拉取后的路由
# ======================================================================
def route_after_pull(state: ReviewTaskState) -> str:
    """
    分支路由 — pull_approval_task 之后

    说明：
        拉取任务成功后统一进入文档解析。此处的条件路由保留扩展位：
        若后续需要"任务已阻塞则不解析"的早退逻辑（例如审批单已被撤回），
        可在本函数内扩展。

    参数：
        state: 当前图状态

    返回：
        下一节点名
    """
    # 拉取阶段若已被判定阻塞（如审批单状态非法），直接进入审计收敛
    if state.is_blocked:
        logger.warning(
            "路由决策-任务拉取后即阻塞 | 任务=%s | 原因=%s",
            state.task_no,
            state.blocked_info.reason if state.blocked_info else "未知",
        )
        return "audit"

    return WorkflowNode.PARSE_CONTRACT.value


# ======================================================================
# 三、分支二：文档解析后的路由（阻断分支入口）
# ======================================================================
def route_after_parse(state: ReviewTaskState) -> str:
    """
    分支路由 — parse_document 之后

    【分支一：阻断分支】
        触发条件（任一命中即阻断）：
            a. 文档加密，无法抽取文本
            b. 扫描件 OCR 平均置信度低于配置阈值（判定为影像模糊）
            c. 抽取正文长度低于配置下限（判定为正文为空）
            d. 解析过程抛出不可恢复异常（解析失败）

        阻断后的走向：
            直接进入 audit 节点收敛流程，任务标记 blocked，
            不执行规则审查、向量召回、大模型审查，也不触发回写。

    参数：
        state: 当前图状态

    返回：
        下一节点名：rule_review（正常）或 audit（阻断）

    异常：
        无。本函数只做分支判定，不抛异常
    """
    # -------------------- 情形 a：解析被阻断 --------------------
    if state.parse_status == ParseStatus.BLOCKED.value:
        reason = (
            state.blocked_info.reason if state.blocked_info else "解析被阻断"
        )
        logger.warning(
            "路由决策-阻断分支 | 任务=%s | 解析状态=blocked | 原因=%s | 走向=audit",
            state.task_no,
            reason,
        )
        return "audit"

    # -------------------- 情形 b：解析失败（可重试但本轮未成功） --------------------
    if state.parse_status == ParseStatus.FAILED.value:
        logger.error(
            "路由决策-阻断分支 | 任务=%s | 解析状态=failed | 错误=%s | 走向=audit",
            state.task_no,
            state.parse_error[:200],
        )
        return "audit"

    # -------------------- 情形 c：解析未完成（异常态，防御性判定） --------------------
    if state.parse_status != ParseStatus.SUCCESS.value:
        logger.warning(
            "路由决策-解析未完成 | 任务=%s | 解析状态=%s | 走向=audit",
            state.task_no,
            state.parse_status,
        )
        return "audit"

    # -------------------- 情形 d：解析成功但无有效条款 --------------------
    if not state.clauses:
        logger.warning(
            "路由决策-无有效条款 | 任务=%s | 走向=audit（标记阻塞）",
            state.task_no,
        )
        return "audit"

    # -------------------- 正常路径：进入规则审查 --------------------
    logger.info(
        "路由决策-解析通过 | 任务=%s | 条款数=%d | 文本块=%d | 走向=rule_review",
        state.task_no,
        len(state.clauses),
        len(state.text_blocks),
    )
    return WorkflowNode.RULE_EVALUATE.value


# ======================================================================
# 四、分支三：规则审查后的路由（快速通道 / 深度复核分叉）
# ======================================================================
def route_after_rule(state: ReviewTaskState) -> str:
    """
    分支路由 — rule_review 之后

    【分支二：快速通道】
        触发条件：route_type == fast（模板合同）
        走向：跳过 vector_review 与 llm_review，直接进入 risk_merge。
        设计依据：模板合同条款与标准库高度一致，语义召回无增量价值；
                  大模型辅助审查在此场景下会显著拉长链路却不改变判定结果
                  （判定权始终在规则引擎），故予跳过。

    【分支三：深度复核】
        触发条件：route_type == deep（大额或高风险类型）
        走向：先后执行 vector_review 与 llm_review，再进入 risk_merge。
        设计依据：大额与高风险合同需要多路证据交叉验证，
                  大模型承担"查漏补缺"的辅助角色，其候选交由规则引擎复核。

    【标准通道】
        走向：同样执行 vector_review 与 llm_review。

    参数：
        state: 当前图状态

    返回：
        下一节点名：risk_merge（快速通道）或 vector_review（标准/深度）
    """
    # -------------------- 规则引擎未完成判定，不允许继续（保守从严） --------------------
    if not state.rule_evaluated:
        logger.error(
            "路由决策-规则判定缺失 | 任务=%s | 规则引擎未完成判定，"
            "为避免未判定放行，直接进入 audit 收敛",
            state.task_no,
        )
        return "audit"

    # -------------------- 分支二：快速通道 --------------------
    if state.is_route_fast:
        logger.info(
            "路由决策-快速通道 | 任务=%s | 跳过向量召回与大模型审查 | 走向=risk_merge",
            state.task_no,
        )
        return "risk_merge"

    # -------------------- 分支三：深度复核 --------------------
    if state.is_route_deep:
        logger.info(
            "路由决策-深度复核 | 任务=%s | 金额=%.2f | 追加大模型审查 | 走向=vector_review",
            state.task_no,
            state.contract_amount,
        )
        return "vector_review"

    # -------------------- 标准通道 --------------------
    logger.info(
        "路由决策-标准审查 | 任务=%s | 走向=vector_review",
        state.task_no,
    )
    return "vector_review"


# ======================================================================
# 五、分支四：风险融合后的路由
# ======================================================================
def route_after_risk_merge(state: ReviewTaskState) -> str:
    """
    分支路由 — risk_merge 之后

    说明：
        融合完成后必须经规则引擎复核定级（rule_review 已在融合前执行过定级，
        融合阶段只做证据归并与冲突裁决，不再改变等级）。
        因此此处按以下规则走向：
            - 任务已被规则引擎判定阻塞 → 直接生成报告后收敛（不回写审批流）
            - 否则 → 生成报告与建议，再执行回写

    参数：
        state: 当前图状态

    返回：
        下一节点名：generate_report（统一走向）
    """
    if state.is_blocked:
        logger.warning(
            "路由决策-融合后阻塞 | 任务=%s | 综合等级=%s | 原因=%s | 阻塞任务不触发审批回写",
            state.task_no,
            state.risk_level,
            state.blocked_info.reason if state.blocked_info else "未知",
        )
    else:
        logger.info(
            "路由决策-融合完成 | 任务=%s | 综合等级=%s | 有效风险项=%d | 走向=generate_report",
            state.task_no,
            state.risk_level,
            state.official_risk_count,
        )

    return WorkflowNode.GENERATE_REPORT.value


# ======================================================================
# 六、分支五：回写完成后的路由
# ======================================================================
def route_after_write_back(state: ReviewTaskState) -> str:
    """
    分支路由 — write_comment 之后

    说明：
        回写成功与否不阻塞流程收敛，均进入 audit 做终态记录。
        回写失败会保留完整错误信息与重试次数，供系统管理员人工重推。

    参数：
        state: 当前图状态

    返回：
        下一节点名：audit
    """
    logger.info(
        "路由决策-回写完成 | 任务=%s | 回写状态=%s | 走向=audit",
        state.task_no,
        state.write_back_status,
    )
    return "audit"


# ======================================================================
# 七、路由决策快照（供审计与前端展示）
# ======================================================================
def build_route_snapshot(state: ReviewTaskState) -> dict:
    """
    构造路由决策快照

    说明：
        记录路由判定所依据的全部客观字段与阈值，保证"为什么走这条链路"
        可以被完整回溯，满足合同场景的审计要求。

    参数：
        state: 当前图状态

    返回：
        路由决策快照字典
    """
    return {
        "task_no": state.task_no,
        "route_type": state.route_type,
        "contract_amount": state.contract_amount,
        "currency": state.currency,
        "amount_threshold": settings.ROUTE_DEEP_AMOUNT_THRESHOLD,
        "contract_type": state.contract_type,
        "force_deep_types": settings.deep_force_type_list,
        "template_flag": state.template_flag,
        "fast_enabled": settings.ROUTE_FAST_ENABLED,
        "source_type": state.source_type,
        "parse_status": state.parse_status,
        "clause_count": len(state.clauses),
    }
