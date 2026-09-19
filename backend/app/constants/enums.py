# D:/ContractReviewSys-合同审批智能审查项目/backend/app/constants/enums.py
"""
枚举统一出口（模块化组织）

本模块职责：
    将高频使用的枚举集中转发，便于业务代码按需 import。
    实际定义位于 app/constants/__init__.py（唯一权威定义处），
    本文件仅做集中导出，不重复定义，避免双份定义导致不一致。

返回值说明：
    本模块仅做导出，无函数返回值

使用示例：
    from app.constants.enums import RiskLevel, ReviewTaskStatus, ClauseType
"""

from app.constants import (  # noqa: F401
    ALL_PERMISSIONS,
    ALL_USER_ROLES,
    PERM_APPROVAL_WRITE_BACK,
    PERM_BLOCKED_TASK_HANDLE,
    PERM_COMMENT_ANNOTATE,
    PERM_CONTRACT_UPLOAD,
    PERM_REPORT_DOWNLOAD,
    PERM_RISK_ITEM_EDIT,
    PERM_RULE_TEMPLATE_MANAGE,
    PERM_SYSTEM_MONITOR,
    PERM_TASK_VIEW_ALL,
    PERM_TASK_VIEW_OWN,
    PERM_USER_MANAGE,
    ROLE_PERMISSIONS,
    USER_ROLE_LABELS,
    ApprovalAction,
    ClauseType,
    ContractStatus,
    FileSourceType,
    ParseEngine,
    ReportFormat,
    ReportStatus,
    ReviewTaskStatus,
    RiskHitDecision,
    RiskLevel,
    RuleMatchType,
    RuleSeverity,
    RuleTemplateStatus,
    TokenType,
    UserRole,
    UserStatus,
    WorkflowNode,
    WriteBackStatus,
    get_permissions_for_role,
    role_has_permission,
)

__all__ = [
    "ContractStatus",
    "FileSourceType",
    "ParseEngine",
    "ClauseType",
    "RiskLevel",
    "ReviewTaskStatus",
    "RiskHitDecision",
    "ApprovalAction",
    "WriteBackStatus",
    "RuleTemplateStatus",
    "RuleMatchType",
    "RuleSeverity",
    "ReportFormat",
    "ReportStatus",
    "WorkflowNode",
    # 以下为登录鉴权体系新增
    "UserRole",
    "UserStatus",
    "TokenType",
    "USER_ROLE_LABELS",
    "ALL_USER_ROLES",
    "ALL_PERMISSIONS",
    "ROLE_PERMISSIONS",
    "get_permissions_for_role",
    "role_has_permission",
    "PERM_CONTRACT_UPLOAD",
    "PERM_TASK_VIEW_OWN",
    "PERM_TASK_VIEW_ALL",
    "PERM_REPORT_DOWNLOAD",
    "PERM_RISK_ITEM_EDIT",
    "PERM_COMMENT_ANNOTATE",
    "PERM_APPROVAL_WRITE_BACK",
    "PERM_RULE_TEMPLATE_MANAGE",
    "PERM_USER_MANAGE",
    "PERM_BLOCKED_TASK_HANDLE",
    "PERM_SYSTEM_MONITOR",
]
