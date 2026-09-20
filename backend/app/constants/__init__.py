# D:/ContractReviewSys-合同审批智能审查项目/backend/app/constants/__init__.py
"""
全局常量定义 — 跨模块复用的枚举与固定值（唯一权威定义处）

本模块职责：
    1. 定义业务枚举（合同状态、审查任务状态、风险等级、条款类型等）
    2. 定义大模型与规则引擎的职责边界常量，防止越界
    3. 定义各存储层的固定集合名，避免魔法字符串散落各处

设计要点：
    - 全部使用 str 枚举（继承 str, Enum），便于直接序列化进 JSON / MongoDB
    - 风险等级枚举严格有序，供规则引擎定级比较使用
    - 明确标注哪些枚举"只能由规则引擎赋值"，作为架构红线的代码级体现

返回值说明：
    本模块只提供常量与枚举，无函数返回值

使用示例：
    from app.constants import RiskLevel, ReviewTaskStatus
    if task.status == ReviewTaskStatus.BLOCKED:
        ...
"""

from enum import Enum, unique
from typing import Final


# ======================================================================
# 一、通配常量
# ======================================================================
# 规则引擎名称标识，写入审查结论快照，表明判定来源
RULE_ENGINE_SOURCE: Final[str] = "rule_engine"
# 大模型来源标识，仅用于标记"辅助内容"的产出方，绝不用于结论标记
LLM_SOURCE: Final[str] = "llm_assistant"


# ======================================================================
# 二、合同状态
# ======================================================================
@unique
class ContractStatus(str, Enum):
    """
    合同主档状态

    流转路径：
        UPLOADED → PARSING → PARSED → REVIEWING → REVIEWED → APPROVED / REJECTED
    异常分支：
        解析失败 → PARSE_FAILED；审查被阻塞 → BLOCKED
    """

    UPLOADED = "uploaded"            # 已上传，尚未开始处理
    PARSING = "parsing"              # 解析中（文本抽取 / OCR）
    PARSED = "parsed"                # 解析完成，条款已结构化
    PARSE_FAILED = "parse_failed"    # 解析失败
    REVIEWING = "reviewing"          # 审查中
    REVIEWED = "reviewed"            # 审查完成，待审批决策
    BLOCKED = "blocked"              # 被高风险条款阻塞，禁止推进
    APPROVED = "approved"            # 审批通过
    REJECTED = "rejected"            # 审批驳回
    ARCHIVED = "archived"            # 已归档


@unique
class FileSourceType(str, Enum):
    """合同来源类型，决定是否走 OCR 链路"""

    DIGITAL = "digital"    # 电子合同（可直接抽取文本）
    SCANNED = "scanned"    # 扫描件（必须走 OCR）


# ======================================================================
# 三、解析与 OCR
# ======================================================================
@unique
class ParseEngine(str, Enum):
    """解析引擎类型"""

    PDF_TEXT = "pdf_text"        # PDF 原生文本抽取
    DOCX_TEXT = "docx_text"      # Word 文本抽取
    OCR = "ocr"                  # OCR 图像识别
    HYBRID = "hybrid"            # 混合：部分页有文字层、部分页需 OCR


@unique
class ClauseType(str, Enum):
    """
    条款类型

    说明：用于条款结构化分类，供规则模板按类型匹配。
    该分类结果可由大模型"建议"，但最终归类以规则模板映射为准。
    """

    PARTY_INFO = "party_info"                # 当事人信息
    SUBJECT_MATTER = "subject_matter"        # 标的与金额
    PAYMENT = "payment"                      # 付款条款
    DELIVERY = "delivery"                    # 交付与验收
    WARRANTY = "warranty"                    # 质量保证
    LIABILITY = "liability"                  # 违约责任
    CONFIDENTIALITY = "confidentiality"      # 保密条款
    INTELLECTUAL_PROPERTY = "ip"             # 知识产权
    TERMINATION = "termination"              # 合同解除与终止
    FORCE_MAJEURE = "force_majeure"          # 不可抗力
    DISPUTE_RESOLUTION = "dispute"           # 争议解决
    DATA_PROTECTION = "data_protection"      # 数据与个人信息保护
    COMPLIANCE = "compliance"                # 合规与反腐败
    OTHER = "other"                          # 其它/未分类


# ======================================================================
# 四、风险等级（架构红线：仅规则引擎可赋值）
# ======================================================================
@unique
class RiskLevel(str, Enum):
    """
    风险等级

    【架构红线】
        该枚举值只能由规则引擎写入数据库。
        大模型仅可输出疑似风险的"候选项"，不得直接落库为风险等级。
    数值越大风险越高，供规则引擎做定级比较与阻塞判定。
    """

    LOW = "low"              # 低风险：可提示，不影响推进
    MEDIUM = "medium"        # 中风险：需人工确认
    HIGH = "high"            # 高风险：需整改后推进
    CRITICAL = "critical"    # 严重风险：直接阻塞审批流


# 风险等级排序映射，供规则引擎比较大小使用
RISK_LEVEL_ORDER: Final[dict] = {
    RiskLevel.LOW.value: 1,
    RiskLevel.MEDIUM.value: 2,
    RiskLevel.HIGH.value: 3,
    RiskLevel.CRITICAL.value: 4,
}


# ======================================================================
# 五、审查任务状态（架构红线：阻塞判定仅规则引擎可下）
# ======================================================================
@unique
class ReviewTaskStatus(str, Enum):
    """
    审查任务状态

    【架构红线】
        BLOCKED 状态只能由规则引擎依据风险等级判定后写入，
        大模型输出不得直接触发任务阻塞。
    """

    PENDING = "pending"            # 待执行
    RUNNING = "running"            # 执行中
    LLM_DONE = "llm_done"          # 大模型辅助分析完成（尚未定级）
    RULE_DONE = "rule_done"        # 规则引擎定级完成
    BLOCKED = "blocked"            # 被规则引擎判定阻塞
    COMPLETED = "completed"        # 审查完成
    FAILED = "failed"              # 执行失败
    CANCELLED = "cancelled"        # 人工取消


@unique
class RiskHitDecision(str, Enum):
    """风险命中的处理决策，由规则引擎依据规则配置输出"""

    PASS = "pass"                  # 放行（仅提示）
    WARN = "warn"                  # 告警，记录但不阻断
    NEED_REVISION = "need_revision"  # 需修订后重新提交
    BLOCK = "block"                # 阻塞，禁止推进审批


# ======================================================================
# 六、审批与回写
# ======================================================================
@unique
class ApprovalAction(str, Enum):
    """审批动作"""

    SUBMIT = "submit"          # 提交审批
    APPROVE = "approve"        # 通过
    REJECT = "reject"          # 驳回
    WITHDRAW = "withdraw"      # 撤回
    REASSIGN = "reassign"      # 转办


@unique
class WriteBackStatus(str, Enum):
    """审批结果回写状态"""

    PENDING = "pending"        # 待回写
    SUCCESS = "success"        # 回写成功
    FAILED = "failed"          # 回写失败
    RETRYING = "retrying"      # 重试中


# ======================================================================
# 七、规则模板
# ======================================================================
@unique
class RuleTemplateStatus(str, Enum):
    """规则模板状态"""

    DRAFT = "draft"            # 草稿，未生效
    ENABLED = "enabled"        # 已启用
    DISABLED = "disabled"      # 已停用
    DEPRECATED = "deprecated"  # 已废弃


# ======================================================================
# 七之二、用户角色与账号状态（登录鉴权体系）
# ======================================================================
@unique
class UserRole(str, Enum):
    """
    系统用户角色

    说明：
        本枚举是全局唯一的角色权威定义处，JWT 载荷、MongoDB 用户文档、
        权限依赖、前端路由鉴权均以本枚举值为准，任何模块不得自行硬编码
        角色字符串，避免出现"配置里写 business_handler、代码里判 business_user"
        这类静默失效。

    三类角色的权责边界（与业务红线配套）：
        BUSINESS_USER（业务经办人）：
            可上传合同、查看/检索自己提交的任务、下载自己任务对应的报告；
            不可编辑风险规则、不可修改风险项、不可触发审批回写。
        LAW_USER（法务审查人）：
            可查看全部待审任务、编辑风险项、人工批注（B 类字段）、触发审批回写；
            不可修改系统配置、不可新增/编辑规则模板、不可管理用户。
        ADMIN（系统管理员）：
            全权限，含用户管理、合规规则维护、阻塞任务处理、系统监控。

    【架构红线约束】
        无论何种角色，均不具备"直接修改审查结论/风险等级"的能力：
        角色权限只决定"能看什么、能批注什么、能触发什么动作"，
        风险等级与最终结论始终由规则引擎产出，角色权限不得绕过该链路。
    """

    BUSINESS_USER = "business_user"    # 业务经办人：上传合同、查看自己的任务、下载报告
    LAW_USER = "law_user"              # 法务审查人：审查全部任务、编辑风险项、批注、触发回写
    ADMIN = "admin"                    # 系统管理员：全权限


# 角色中文名映射，用于界面展示与日志输出（避免各处重复维护中文文案）
USER_ROLE_LABELS: Final[dict] = {
    UserRole.BUSINESS_USER.value: "业务经办人",
    UserRole.LAW_USER.value: "法务审查人",
    UserRole.ADMIN.value: "系统管理员",
}

# 全部角色值集合，供账号初始化与参数校验使用
ALL_USER_ROLES: Final[tuple] = tuple(role.value for role in UserRole)


@unique
class UserStatus(str, Enum):
    """
    用户账号状态

    说明：
        登录链路只放行 ACTIVE 状态的账号。
        DISABLED 用于管理员主动停用；LOCKED 用于连续登录失败后的自动锁定。
    """

    ACTIVE = "active"        # 正常启用
    DISABLED = "disabled"    # 已停用（管理员操作）
    LOCKED = "locked"        # 已锁定（连续登录失败触发）


@unique
class TokenType(str, Enum):
    """
    JWT 令牌类型

    说明：
        access_token 用于业务接口鉴权，有效期较短；
        refresh_token 仅用于换取新的 access_token，有效期较长。
        解码时必须校验类型，防止用 refresh_token 直接访问业务接口
        （否则"短有效期 access_token"这一安全设计将被完全绕过）。
    """

    ACCESS = "access"      # 访问令牌
    REFRESH = "refresh"    # 刷新令牌


@unique
class RuleMatchType(str, Enum):
    """
    规则匹配方式

    说明：均为确定性匹配，保证判定可复现、可审计。
    """

    KEYWORD = "keyword"              # 关键词命中
    REGEX = "regex"                  # 正则表达式命中
    CLAUSE_TYPE = "clause_type"      # 条款类型命中
    SEMANTIC = "semantic"            # 语义相似度命中（阈值由规则配置决定）
    FIELD_COMPARE = "field_compare"  # 字段数值比较（如金额上限、期限天数）
    MISSING_CLAUSE = "missing_clause"        # 必备条款缺失检查（合同应含某类条款而缺失）
    NUMERIC_THRESHOLD = "numeric_threshold"  # 数值阈值校验（如违约金比例、付款周期上限）


@unique
class RuleSeverity(str, Enum):
    """
    规则自身定义的严重度

    说明：规则严重度经规则引擎映射表 → 最终风险等级。
    该映射过程完全确定，不引入模型判断。
    """

    INFO = "info"          # 提示级
    MINOR = "minor"        # 轻微
    MAJOR = "major"        # 重大
    FATAL = "fatal"        # 致命


# ======================================================================
# 八、报告导出
# ======================================================================
@unique
class ReportFormat(str, Enum):
    """报告导出格式"""

    DOCX = "docx"      # Word 报告
    PDF = "pdf"        # PDF 报告
    JSON = "json"      # 结构化 JSON（对接外部系统）


@unique
class ReportStatus(str, Enum):
    """报告生成状态"""

    PENDING = "pending"        # 排队中
    GENERATING = "generating"  # 生成中
    COMPLETED = "completed"    # 生成完成
    FAILED = "failed"          # 生成失败


# ======================================================================
# 九、LangGraph 工作流节点名
# 说明：节点名统一在此定义，避免编排代码中出现魔法字符串
# ======================================================================
@unique
class WorkflowNode(str, Enum):
    """
    审查工作流节点标识

    说明：
        本枚举与 LangGraph 流程图中的十个节点一一对应，
        节点名统一在此定义，避免编排代码中出现魔法字符串。
        命名与业务语义对齐（如 pull_approval_task、rule_review），
        便于日志检索与前端进度展示。
    """

    PULL_APPROVAL_TASK = "pull_approval_task"    # 拉取审批任务，注入合同基础信息
    PARSE_CONTRACT = "parse_document"            # 文档解析（文本抽取 / OCR）+ 条款结构化
    RULE_EVALUATE = "rule_review"                # 规则引擎判定（定级 + 阻塞，唯一判定方）
    VECTOR_REVIEW = "vector_review"              # 向量语义召回（候选语义，无定级权）
    LLM_ANALYZE = "llm_review"                   # 大模型辅助审查（候选语义，无定级权）
    RISK_MERGE = "risk_merge"                    # 多路召回融合与冲突裁决
    GENERATE_REPORT = "generate_report"          # 审查报告与修改建议生成
    GENERATE_COMMENT = "generate_comment"        # 审批评论区内容构造
    WRITE_BACK = "write_comment"                 # 审批评论区回写
    AUDIT = "audit"                              # 流程审计与收敛（图的终点节点）


# ======================================================================
# 十、MongoDB 集合名（统一在此登记，禁止散落硬编码）
# ======================================================================
class MongoCollection:
    """MongoDB 集合名常量表"""

    CONTRACT = "cr_contract"                     # 合同主档
    CONTRACT_CLAUSE = "cr_contract_clause"       # 合同条款（结构化结果）
    OCR_RESULT = "cr_ocr_result"                 # OCR 识别结果
    REVIEW_TASK = "cr_review_task"               # 审查任务
    REVIEW_RISK_HIT = "cr_review_risk_hit"       # 风险命中记录（含辅助候选）
    REVIEW_CONCLUSION = "cr_review_conclusion"   # 审查结论（规则引擎产出）
    RULE_TEMPLATE = "cr_rule_template"           # 规则模板
    RULE_HIT_LOG = "cr_rule_hit_log"             # 规则命中审计日志
    AUDIT_LOG = "cr_audit_log"                   # 任务全链路操作审计日志
    STANDARD_CLAUSE = "cr_standard_clause"       # 示范条款库（MongoDB 存储）
    SUGGESTION = "cr_suggestion"                 # 修改建议
    APPROVAL_RECORD = "cr_approval_record"       # 审批记录
    WRITE_BACK_LOG = "cr_write_back_log"         # 回写日志
    REPORT = "cr_report"                         # 报告记录
    FILE_OBJECT = "cr_file_object"               # 文件对象元数据
    SYS_CONFIG = "cr_sys_config"                 # 系统配置（运行期可改项）
    USER = "cr_user"                             # 系统用户（登录鉴权的账号主档）
    NOTIFICATION = "cr_notification"             # 站内通知（消息中心）
    TASK_OPERATION_LOG = "cr_task_operation_log"  # 任务处理操作记录（重试/重新上传留痕）


class FailureType(str, Enum):
    """
    任务执行失败类型（2026-09-20 交互优化新增）

    【为什么分类】
        不同失败类型的处置方式不同：
            - 瞬时异常（ocr_timeout / network_error）：可一键重试，
              且后台自动重试 1 次
            - 文件类异常（file_corrupt / file_format）：重试无意义，
              必须重新上传替换原文件
            - 引擎异常（rule_engine_error / other）：人工排查后重试
        分类随失败落库（ReviewTask.failure_type），供大盘悬浮提示、
        失败类型筛选与自动重试决策使用。
    """

    OCR_TIMEOUT = "ocr_timeout"              # OCR 识别接口调用超时
    FILE_FORMAT = "file_format"              # 文件格式不支持
    FILE_CORRUPT = "file_corrupt"            # 文件损坏 / 内容为空 / 加密
    RULE_ENGINE_ERROR = "rule_engine_error"  # 规则引擎执行异常
    NETWORK_ERROR = "network_error"          # 网络抖动 / 上游服务不可达
    PARSE_FAILED = "parse_failed"            # 其他解析失败
    OTHER = "other"                          # 未归类异常

    @classmethod
    def transient_types(cls) -> "set[str]":
        """瞬时异常集合：可自动重试 / 一键重试即可恢复"""
        return {cls.OCR_TIMEOUT.value, cls.NETWORK_ERROR.value}


class NotificationType(str, Enum):
    """站内/QQ 通知类型（消息分级推送）"""

    TASK_COMPLETED = "task_completed"  # 审查完成（含命中风险数）
    TASK_FAILED = "task_failed"        # 审查/解析执行失败
    SYSTEM_ALERT = "system_alert"      # 系统级异常（批量故障等）


class NotificationLevel(str, Enum):
    """通知级别：critical 标红高优先级，info 普通展示"""

    INFO = "info"
    CRITICAL = "critical"


# ======================================================================
# 十一、常用分页默认值
# ======================================================================
DEFAULT_PAGE_SIZE: Final[int] = 20
MAX_PAGE_SIZE: Final[int] = 200
DEFAULT_SORT_FIELD: Final[str] = "created_at"
DEFAULT_SORT_ORDER: Final[int] = -1  # -1 降序，1 升序（与 MongoDB 一致）


# ======================================================================
# 十二、权限点定义与角色权限矩阵（鉴权体系的判定依据）
# ======================================================================
# 权限点常量：全部以 "资源:动作" 形式命名，便于前端路由与后端接口共用同一套语义
# 说明：权限点只描述"能否发起该动作"，不描述"该动作如何判定"——
#       例如 PERM_RISK_ITEM_EDIT 只决定"法务能否修改风险项的批注/建议"，
#       不代表可以修改判定字段（等级/结论），后者由架构红线在模型层拦截。
PERM_CONTRACT_UPLOAD: Final[str] = "contract:upload"            # 上传合同
PERM_TASK_VIEW_OWN: Final[str] = "task:view_own"                # 查看自己提交的任务
PERM_TASK_VIEW_ALL: Final[str] = "task:view_all"                # 查看全部任务
PERM_REPORT_DOWNLOAD: Final[str] = "report:download"            # 下载审查报告
PERM_RISK_ITEM_EDIT: Final[str] = "risk_item:edit"              # 编辑风险项（批注/建议，B 类字段）
PERM_COMMENT_ANNOTATE: Final[str] = "comment:annotate"          # 人工批注
PERM_APPROVAL_WRITE_BACK: Final[str] = "approval:write_back"    # 触发审批回写（仅评论通道）
PERM_RULE_TEMPLATE_MANAGE: Final[str] = "rule_template:manage"  # 维护合规规则模板
PERM_USER_MANAGE: Final[str] = "user:manage"                    # 管理用户
PERM_BLOCKED_TASK_HANDLE: Final[str] = "blocked_task:handle"    # 处理阻塞任务（重跑/解阻塞）
PERM_SYSTEM_MONITOR: Final[str] = "system:monitor"              # 系统监控

# 全部权限点元组，供接口做参数合法性校验
ALL_PERMISSIONS: Final[tuple] = (
    PERM_CONTRACT_UPLOAD,
    PERM_TASK_VIEW_OWN,
    PERM_TASK_VIEW_ALL,
    PERM_REPORT_DOWNLOAD,
    PERM_RISK_ITEM_EDIT,
    PERM_COMMENT_ANNOTATE,
    PERM_APPROVAL_WRITE_BACK,
    PERM_RULE_TEMPLATE_MANAGE,
    PERM_USER_MANAGE,
    PERM_BLOCKED_TASK_HANDLE,
    PERM_SYSTEM_MONITOR,
)

# 角色 → 权限点映射（唯一权威定义处）
#
# 设计说明：
#     1. 采用"显式枚举"而非"角色继承"写法，使每个角色到底能做什么一目了然，
#        便于法务与安全审计人员直接核对，不必在脑内做继承推导。
#     2. 权限点严格对应需求中的权责边界，不额外放开任何一项。
#     3. 【红线】任何角色都不包含"修改风险等级/审查结论/阻塞判定"的权限点，
#        因为系统中根本不存在这样的权限点——判定权物理上只属于规则引擎。
ROLE_PERMISSIONS: Final[dict] = {
    # 业务经办人：上传 + 看自己的任务 + 下载报告，不具备任何审查类权限
    UserRole.BUSINESS_USER.value: (
        PERM_CONTRACT_UPLOAD,
        PERM_TASK_VIEW_OWN,
        PERM_REPORT_DOWNLOAD,
    ),
    # 法务审查人：看全部任务 + 编辑风险项 + 批注 + 触发回写，不具备规则/用户管理权限
    UserRole.LAW_USER.value: (
        PERM_TASK_VIEW_OWN,          # 法务同样可查看自己创建的任务
        PERM_TASK_VIEW_ALL,
        PERM_REPORT_DOWNLOAD,
        PERM_RISK_ITEM_EDIT,
        PERM_COMMENT_ANNOTATE,
        PERM_APPROVAL_WRITE_BACK,
    ),
    # 系统管理员：全权限（沿用 ALL_PERMISSIONS，保证新增权限点自动纳入）
    UserRole.ADMIN.value: ALL_PERMISSIONS,
}


def get_permissions_for_role(role: str) -> tuple:
    """
    获取指定角色拥有的权限点集合

    参数：
        role: 角色值字符串，取值见 UserRole（business_user / law_user / admin）

    返回：
        tuple[str, ...]：该角色的权限点元组；角色未知时返回空元组（默认拒绝原则）

    说明：
        未知角色一律返回空权限而非抛异常，是因为本函数会被鉴权中间件与
        前端路由守卫高频调用，抛异常会造成"未知角色 → 500"的误报；
        空权限会自然落到 403，语义更准确。
    """
    return ROLE_PERMISSIONS.get(role, ())


def role_has_permission(role: str, permission: str) -> bool:
    """
    判断指定角色是否拥有某个权限点

    参数：
        role:       角色值字符串
        permission: 权限点字符串

    返回：
        bool：拥有返回 True，否则 False

    使用示例：
        if role_has_permission(user.role, PERM_RULE_TEMPLATE_MANAGE):
            ...  # 放行规则维护接口
    """
    return permission in ROLE_PERMISSIONS.get(role, ())
