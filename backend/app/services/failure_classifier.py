# D:/ContractReviewSys-合同审批智能审查项目/backend/app/services/failure_classifier.py
"""
失败分类器

【模块定位】
    把解析/审查过程中抛出的异常翻译为结构化的失败信息：
        (失败类型, 面向用户的原因摘要, 处置建议, 是否瞬时异常)
    分类结果随任务落库（ReviewTask.failure_type / failure_reason），
    供三类消费方使用：
        1. 大盘列表：失败行悬浮提示与「一键重试 / 重新上传」操作指引
        2. 异常任务筛选：按失败类型（OCR 超时 / 文件格式 / 文件损坏 / 引擎报错）查询
        3. 自动重试决策：仅瞬时异常后台自动重试 1 次，文件类异常直接人工通知
"""

import re
from typing import Optional, Tuple

from app.constants import FailureType
from app.core.logging_config import get_logger

logger = get_logger(__name__)

# 类型 → (面向用户的原因模板, 处置建议)
_REASON_ADVICE: dict = {
    FailureType.OCR_TIMEOUT.value: (
        "OCR 识别接口调用超时",
        "瞬时异常可直接一键重试；多次失败请检查网络后重新上传",
    ),
    FailureType.NETWORK_ERROR.value: (
        "网络异常或上游服务暂时不可达",
        "瞬时异常可直接一键重试",
    ),
    FailureType.FILE_FORMAT.value: (
        "文件格式不支持",
        "请点击【重新上传】替换为 PDF / Word / 图片扫描件后重新提交",
    ),
    FailureType.FILE_CORRUPT.value: (
        "文件损坏、内容为空或已加密",
        "请点击【重新上传】替换合同文件后重新提交",
    ),
    FailureType.RULE_ENGINE_ERROR.value: (
        "规则引擎执行异常",
        "请联系管理员排查规则配置；异常恢复后可一键重试",
    ),
    FailureType.PARSE_FAILED.value: (
        "文档解析失败",
        "请确认文件可正常打开；必要时点击【重新上传】替换文件",
    ),
    FailureType.OTHER.value: (
        "系统执行异常",
        "瞬时异常可直接一键重试；持续失败请联系管理员",
    ),
}

# 关键词 → 失败类型（按优先级顺序匹配，先命中先归类）
_KEYWORD_RULES: list = [
    # OCR 超时：在线 OCR 读超时 / OCR 字样的超时
    (FailureType.OCR_TIMEOUT.value, r"OCR.*(timed? ?out|超时|timeout)|读操作超时|read operation timed out", True),
    # 文件格式：扩展名被拒
    (FailureType.FILE_FORMAT.value, r"不支持的文件类型|不支持的文件格式|文件格式不支持|unsupported.*format", False),
    # 文件损坏：空内容 / 加密 / 无法打开
    (FailureType.FILE_CORRUPT.value, r"内容为空|文件损坏|已加密|无法打开|EMPTY_CONTENT|DOC_ENCRYPTED", False),
    # 规则引擎异常
    (FailureType.RULE_ENGINE_ERROR.value, r"规则引擎|rule_engine.*异常|RuleEngineException", False),
    # 网络抖动：连接拒绝 / 连接重置 / DNS（不含 OCR 字样，OCR 超时优先命中上一条）
    (FailureType.NETWORK_ERROR.value, r"连接被拒|connection refused|连接重置|connection reset|getaddrinfo|网络", True),
]


def classify_failure(
    exc: Optional[BaseException],
    message: str = "",
) -> Tuple[str, str, str, bool]:
    """
    将异常归类为结构化失败信息

    参数：
        exc: 捕获到的异常对象（可为 None，此时仅用 message 判断）
        message: 补充的原始错误文本（异常 str() 之外的上文信息）

    返回：
        (failure_type, user_reason, advice, is_transient) 四元组：
            failure_type: FailureType 取值
            user_reason: 面向用户的脱敏原因摘要（不含堆栈与内部地址）
            advice: 处置建议（悬浮提示与 QQ 通知的处理建议栏）
            is_transient: 是否瞬时异常（True 时后台自动重试 1 次）
    """
    raw_parts = [message or ""]
    if exc is not None:
        raw_parts.append(f"{type(exc).__name__}: {exc}")
    raw = "\n".join(part for part in raw_parts if part)

    failure_type: Optional[str] = None
    for type_value, pattern, transient in _KEYWORD_RULES:
        try:
            if re.search(pattern, raw, flags=re.IGNORECASE):
                failure_type = type_value
                break
        except re.error:  # pragma: no cover - 正则配置错误时跳过该条
            continue

    if failure_type is None:
        failure_type = FailureType.OTHER.value

    reason, advice = _REASON_ADVICE.get(
        failure_type, _REASON_ADVICE[FailureType.OTHER.value]
    )
    is_transient = failure_type in FailureType.transient_types()

    logger.info(
        "失败分类完成 | 类型=%s | 瞬时=%s | 原始错误=%s",
        failure_type, is_transient, raw[:200],
    )
    return failure_type, reason, advice, is_transient
