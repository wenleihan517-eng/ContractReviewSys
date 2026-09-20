# D:/ContractReviewSys-合同审批智能审查项目/backend/app/utils/__init__.py
"""
工具层统一出口

本包职责：
    提供与业务无关的通用工具，供各层复用：
        - id_generator    编号 / 会话 ID / 对象键生成
        - retry           指数退避重试装饰器与重试守卫
        - approval_guard  审批接口白名单守卫（红线强制）
        - time_utils      时间格式化与区间计算

设计要点：
    - 工具函数保持纯粹，不依赖具体业务模型
    - 涉及红线约束的工具（如 approval_guard）在工具层即做强制校验

返回值说明：
    本模块仅做聚合导出，无函数返回值

使用示例：
    from app.utils import generate_task_no, retry_async, ensure_comment_only
"""

from app.utils.approval_guard import (
    ALLOWED_APPROVAL_APIS,
    FORBIDDEN_APPROVAL_APIS,
    ApprovalPermissionException,
    build_comment_payload,
    ensure_comment_only,
)
from app.utils.id_generator import (
    build_work_dir,
    generate_contract_no,
    generate_object_key,
    generate_task_no,
    generate_thread_id,
)
from app.utils.retry import RetryExhaustedError, RetryGuard, measure_elapsed, retry_async
from app.utils.time_utils import (
    format_datetime,
    humanize_duration,
    now_timestamp,
    parse_datetime,
)

__all__ = [
    # ID 生成
    "generate_task_no",
    "generate_contract_no",
    "generate_thread_id",
    "generate_object_key",
    "build_work_dir",
    # 重试
    "retry_async",
    "RetryGuard",
    "RetryExhaustedError",
    "measure_elapsed",
    # 审批守卫
    "ensure_comment_only",
    "build_comment_payload",
    "ApprovalPermissionException",
    "ALLOWED_APPROVAL_APIS",
    "FORBIDDEN_APPROVAL_APIS",
    # 时间
    "now_timestamp",
    "format_datetime",
    "parse_datetime",
    "humanize_duration",
]
