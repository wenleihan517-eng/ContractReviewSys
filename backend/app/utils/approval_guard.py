# D:/ContractReviewSys-合同审批智能审查项目/backend/app/utils/approval_guard.py
"""
审批系统调用守卫 — 接口白名单强制校验

【绝对禁止项（代码级强制）】
    本模块是"禁止改变审批结论"这一红线的守卫。
    系统只允许调用审批系统的【评论区写入】接口，
    严禁调用任何同意 / 拒绝 / 转交 / 撤回等改变审批结论的接口。

    实现方式：
        1. 白名单机制：只有 ALLOWED_APPROVAL_APIS 中的接口可通过校验
        2. 黑名单拦截：命中 FORBIDDEN_APPROVAL_APIS 的路径立即抛异常
        3. 兜底策略：不在白名单内的所有路径一律拒绝（默认拒绝原则）

返回值说明：
    - ensure_comment_only(api_path)：校验通过返回 True，违规抛异常
    - build_comment_payload(...)：返回评论文本字典

使用示例：
    from app.utils.approval_guard import ensure_comment_only
    ensure_comment_only("/api/approval/comments")   # 通过
    ensure_comment_only("/api/approval/approve")    # 抛异常
"""

from typing import Any, Dict, List

from app.core.exceptions import BusinessException, ErrorCode
from app.core.logging_config import get_logger

logger = get_logger(__name__)


# ======================================================================
# 接口白名单与黑名单（唯一权威定义）
# ======================================================================
# 允许调用的审批系统接口（仅评论区写入）
ALLOWED_APPROVAL_APIS: List[str] = [
    "/comments",           # 写入评论
    "/comments/batch",     # 批量写入评论
]

# 严禁调用的接口（改变审批结论的操作）
FORBIDDEN_APPROVAL_APIS: List[str] = [
    "/approve",       # 同意
    "/reject",        # 拒绝
    "/transfer",      # 转交
    "/withdraw",      # 撤回
    "/reassign",      # 转办
    "/sign",          # 签署
    "/complete",      # 完结审批
    "/terminate",     # 终止审批
]


class ApprovalPermissionException(BusinessException):
    """
    审批接口调用越权异常

    说明：
        当系统试图调用白名单外的审批接口时抛出。
        该异常属于严重违规，必须记录 ERROR 级日志。
    """

    def __init__(self, api_path: str, reason: str) -> None:
        """
        初始化异常

        参数：
            api_path: 被拒绝的接口路径
            reason: 拒绝原因
        """
        super().__init__(
            code=ErrorCode.APPROVAL_PERMISSION_DENIED,
            message=f"审批接口调用被拒绝：{reason}",
            detail=f"尝试调用的接口路径 = {api_path}",
            http_status=403,
        )
        self.api_path = api_path
        self.reason = reason


def ensure_comment_only(api_path: str) -> bool:
    """
    校验接口路径只允许为评论区写入接口

    【强制规则】
        1. 命中黑名单（同意/拒绝/转交等）→ 立即抛异常并记录 ERROR 日志
        2. 不在白名单内 → 按"默认拒绝"原则抛异常
        3. 在白名单内 → 校验通过

    参数：
        api_path: 待校验的审批系统接口路径

    返回：
        True 表示校验通过（该接口允许调用）

    异常：
        ApprovalPermissionException：接口越权时抛出
    """
    # 规范化路径：去除首尾空格与末尾斜杠，统一小写比较
    normalized = api_path.strip().rstrip("/").lower()

    # 第一重：黑名单拦截（最高优先级，命中即拒绝并告警）
    for forbidden in FORBIDDEN_APPROVAL_APIS:
        if normalized.endswith(forbidden):
            logger.error(
                "【权限违规拦截】尝试调用禁止接口 | 路径=%s | 规则=%s | "
                "说明=系统仅允许写入审批评论区，严禁改变审批结论",
                api_path,
                forbidden,
            )
            raise ApprovalPermissionException(
                api_path=api_path,
                reason=f"禁止调用改变审批结论的接口（匹配规则 {forbidden}）",
            )

    # 第二重：白名单校验（默认拒绝原则）
    for allowed in ALLOWED_APPROVAL_APIS:
        if normalized.endswith(allowed):
            logger.debug("审批接口白名单校验通过 | 路径=%s", api_path)
            return True

    # 第三重：既非黑名单也不在白名单，一律拒绝
    logger.error(
        "【权限违规拦截】调用未授权接口 | 路径=%s | 白名单=%s",
        api_path,
        ALLOWED_APPROVAL_APIS,
    )
    raise ApprovalPermissionException(
        api_path=api_path,
        reason="该接口不在审批系统授权白名单内（系统仅允许写入评论区）",
    )


def build_comment_payload(
    task_no: str,
    final_risk_level: str,
    conclusion_text: str,
    risk_items: List[Dict[str, Any]],
    report_url: str = "",
    legal_opinion: str = "",
) -> Dict[str, Any]:
    """
    构造审批评论区写入内容

    说明：
        评论文本采用结构化模板，包含合同任务号、综合风险等级、审查结论、
        风险明细、报告链接与法务补充意见。文本内容全部来自规则引擎产出，
        不含大模型生成的结论性表述。

    参数：
        task_no: 审查任务编号
        final_risk_level: 最终风险等级（规则引擎产出）
        conclusion_text: 审查结论文本（规则引擎产出）
        risk_items: 风险明细列表，每项含 rule_code / rule_name /
                    evidence_text / suggested_text
        report_url: 审查报告访问链接
        legal_opinion: 法务补充意见（可为空）

    返回：
        形如 {"contract_review_comment": "评论文本", "risk_count": 3, ...}
        的字典，可直接序列化后提交给审批系统评论接口
    """
    # 风险等级中文映射（仅用于展示）
    level_label_map = {
        "low": "低风险",
        "medium": "中风险",
        "high": "高风险",
        "critical": "严重风险",
    }
    level_label = level_label_map.get(final_risk_level, final_risk_level)

    lines: List[str] = [
        "【合同智能审查意见】",
        f"任务编号：{task_no}",
        f"综合风险等级：{level_label}",
        f"审查结论：{conclusion_text}",
    ]

    # 风险明细：逐条列出编码、名称、证据与建议
    if risk_items:
        lines.append(f"风险明细（共 {len(risk_items)} 项）：")
        for index, item in enumerate(risk_items, start=1):
            lines.append(
                f"  {index}. [{item.get('rule_code', '-')}] "
                f"{item.get('rule_name', '未命名规则')}"
            )
            evidence = item.get("evidence_text", "")
            if evidence:
                lines.append(f"     证据：{evidence}")
            suggestion = item.get("suggested_text", "")
            if suggestion:
                lines.append(f"     建议：{suggestion}")
    else:
        lines.append("风险明细：未发现风险项")

    if report_url:
        lines.append(f"审查报告：{report_url}")

    if legal_opinion:
        lines.append(f"法务补充意见：{legal_opinion}")

    lines.append("（本意见由规则引擎判定产出，仅作审批参考，不改变审批结论）")

    comment_text = "\n".join(lines)

    payload: Dict[str, Any] = {
        "contract_review_comment": comment_text,
        "risk_count": len(risk_items),
        "final_risk_level": final_risk_level,
        "task_no": task_no,
    }

    logger.info(
        "审批评论内容已构造 | 任务=%s | 风险等级=%s | 风险数=%d | 文本长度=%d",
        task_no,
        final_risk_level,
        len(risk_items),
        len(comment_text),
    )

    return payload
