# D:/ContractReviewSys-合同审批智能审查项目/backend/app/core/exceptions.py
"""
全局异常定义与统一异常处理器

本模块职责：
    1. 定义业务异常基类与各业务域专用异常，统一携带错误码与提示信息
    2. 定义全局错误码常量，前后端约定一致
    3. 注册 FastAPI 全局异常处理器，保证任何异常都以统一 JSON 结构返回

统一响应结构（所有异常均遵循）：
    {
        "code": 错误码（int，非 0 表示失败）,
        "message": 面向用户的错误提示,
        "detail": 详细的错误上下文（仅非生产环境返回，便于排查）,
        "request_id": 请求链路 ID，用于日志定位
    }

设计要点：
    - 业务异常继承 BusinessException，由统一处理器捕获，避免到处 try/except
    - 生产环境隐藏内部异常细节，防止敏感信息（连接串、路径）泄漏
    - 所有异常必须记录日志，保证可追溯

返回值说明：
    - 各异常类：抛出即中断当前流程，由注册的处理器转换为 HTTP 响应
    - register_exception_handlers(app)：无返回值，副作用为向 app 注册处理器
"""

import traceback
import uuid
from typing import Any, Dict, Optional

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.config import settings
from app.core.logging_config import get_logger

logger = get_logger(__name__)


# ======================================================================
# 一、全局错误码定义
# 约定：0 表示成功；1xxxx 通用错误；2xxxx 参数校验；3xxxx 存储层；
#       4xxxx 大模型/向量服务；5xxxx 业务层；9xxxx 系统级
# ======================================================================
class ErrorCode:
    """全局错误码常量表，前后端必须保持一致，禁止随意变更既有码值"""

    # 通用
    SUCCESS = 0                       # 成功
    UNKNOWN_ERROR = 10000             # 未知错误
    INTERNAL_ERROR = 10001            # 服务器内部错误
    SERVICE_UNAVAILABLE = 10002       # 服务不可用
    NOT_FOUND = 10003                 # 资源不存在
    METHOD_NOT_ALLOWED = 10004        # 请求方法不允许

    # 参数与请求
    VALIDATION_ERROR = 20000          # 请求参数校验失败
    MISSING_PARAMETER = 20001         # 缺少必填参数

    # 存储层
    MONGO_ERROR = 30000               # MongoDB 操作异常
    MINIO_ERROR = 30001               # MinIO 操作异常
    MILVUS_ERROR = 30002              # Milvus 操作异常

    # 大模型 / 向量服务
    LLM_ERROR = 40000                 # 大模型调用异常
    LLM_TIMEOUT = 40001               # 大模型调用超时
    EMBEDDING_ERROR = 40002           # 向量化服务异常

    # 业务层
    FILE_UPLOAD_ERROR = 50000         # 文件上传失败
    FILE_TYPE_NOT_ALLOWED = 50001     # 文件类型不允许
    FILE_SIZE_EXCEEDED = 50002        # 文件大小超限
    CONTRACT_PARSE_ERROR = 50003      # 合同解析失败
    OCR_ERROR = 50004                 # OCR 识别失败
    RULE_ENGINE_ERROR = 50005         # 规则引擎执行异常
    REVIEW_TASK_ERROR = 50006         # 审查任务异常
    TASK_BLOCKED = 50007              # 任务处于阻塞状态，需人工处理
    APPROVAL_PERMISSION_DENIED = 50008  # 审批接口调用越权（严禁改变审批结论）
    WRITE_BACK_ERROR = 50009          # 审批回写失败

    # 鉴权与授权（6xxxx 段）
    # 说明：本段错误码与 HTTP 状态码严格对应，前端据 http 状态码做统一拦截，
    #       据业务码做差异化文案提示，两者不可混用。
    AUTH_ERROR = 60000                # 鉴权通用失败（兜底）
    CREDENTIALS_INVALID = 60001       # 账号或密码错误（HTTP 401）
    TOKEN_MISSING = 60002             # 未携带凭证（HTTP 401）
    TOKEN_INVALID = 60003             # 凭证非法/被篡改（HTTP 401）
    TOKEN_EXPIRED = 60004             # 凭证已过期，需刷新或重新登录（HTTP 401）
    TOKEN_TYPE_INVALID = 60005        # 令牌类型不匹配（如用 refresh_token 访问业务接口，HTTP 401）
    REFRESH_TOKEN_INVALID = 60006     # 刷新令牌失效，需重新登录（HTTP 401）
    ACCOUNT_DISABLED = 60007          # 账号已被停用（HTTP 403）
    ACCOUNT_LOCKED = 60008            # 账号已被锁定（HTTP 403）
    PERMISSION_DENIED = 60009         # 已登录但无权限访问该资源（HTTP 403）

    # 系统级
    CONFIG_ERROR = 90000              # 配置错误


# ======================================================================
# 二、业务异常基类
# ======================================================================
class BusinessException(Exception):
    """
    业务异常基类

    参数：
        code: 业务错误码，取自 ErrorCode
        message: 面向用户的错误提示（中文，可直接展示）
        detail: 详细错误上下文，仅开发/测试环境返回前端
        http_status: 对应的 HTTP 状态码，默认 400

    属性：
        code / message / detail / http_status：与参数一一对应
    """

    def __init__(
        self,
        code: int = ErrorCode.UNKNOWN_ERROR,
        message: str = "业务处理失败",
        detail: Optional[Any] = None,
        http_status: int = status.HTTP_400_BAD_REQUEST,
    ) -> None:
        self.code = code
        self.message = message
        self.detail = detail
        self.http_status = http_status
        # 调用父类构造，保证异常链信息完整
        super().__init__(message)


# ----------------------------------------------------------------------
# 存储层异常
# ----------------------------------------------------------------------
class MongoDBException(BusinessException):
    """MongoDB 操作异常"""

    def __init__(self, message: str = "MongoDB 操作失败", detail: Any = None) -> None:
        super().__init__(
            code=ErrorCode.MONGO_ERROR,
            message=message,
            detail=detail,
            http_status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


class MinIOException(BusinessException):
    """MinIO 对象存储操作异常"""

    def __init__(self, message: str = "对象存储操作失败", detail: Any = None) -> None:
        super().__init__(
            code=ErrorCode.MINIO_ERROR,
            message=message,
            detail=detail,
            http_status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


class MilvusException(BusinessException):
    """Milvus 向量库操作异常"""

    def __init__(self, message: str = "向量库操作失败", detail: Any = None) -> None:
        super().__init__(
            code=ErrorCode.MILVUS_ERROR,
            message=message,
            detail=detail,
            http_status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


# ----------------------------------------------------------------------
# 大模型 / 向量服务异常
# ----------------------------------------------------------------------
class LLMException(BusinessException):
    """本地大模型调用异常"""

    def __init__(self, message: str = "大模型调用失败", detail: Any = None) -> None:
        super().__init__(
            code=ErrorCode.LLM_ERROR,
            message=message,
            detail=detail,
            http_status=status.HTTP_502_BAD_GATEWAY,
        )


class EmbeddingException(BusinessException):
    """向量化服务调用异常"""

    def __init__(self, message: str = "向量化服务调用失败", detail: Any = None) -> None:
        super().__init__(
            code=ErrorCode.EMBEDDING_ERROR,
            message=message,
            detail=detail,
            http_status=status.HTTP_502_BAD_GATEWAY,
        )


# ----------------------------------------------------------------------
# 业务域异常
# ----------------------------------------------------------------------
class FileUploadException(BusinessException):
    """文件上传相关异常（类型、大小、写入失败）"""

    def __init__(
        self,
        message: str = "文件上传失败",
        code: int = ErrorCode.FILE_UPLOAD_ERROR,
        detail: Any = None,
    ) -> None:
        super().__init__(
            code=code,
            message=message,
            detail=detail,
            http_status=status.HTTP_400_BAD_REQUEST,
        )


class ContractParseException(BusinessException):
    """合同解析异常"""

    def __init__(self, message: str = "合同解析失败", detail: Any = None) -> None:
        super().__init__(
            code=ErrorCode.CONTRACT_PARSE_ERROR,
            message=message,
            detail=detail,
            http_status=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )


class OCRException(BusinessException):
    """OCR 识别异常"""

    def __init__(self, message: str = "OCR 识别失败", detail: Any = None) -> None:
        super().__init__(
            code=ErrorCode.OCR_ERROR,
            message=message,
            detail=detail,
            http_status=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )


class RuleEngineException(BusinessException):
    """
    规则引擎执行异常

    说明：规则引擎是最终判定方，其异常必须显式暴露，
    不允许静默降级，避免出现"未判定却放行"的合规风险。
    """

    def __init__(self, message: str = "规则引擎执行失败", detail: Any = None) -> None:
        super().__init__(
            code=ErrorCode.RULE_ENGINE_ERROR,
            message=message,
            detail=detail,
            http_status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


class ConfigException(BusinessException):
    """配置相关异常"""

    def __init__(self, message: str = "配置错误", detail: Any = None) -> None:
        super().__init__(
            code=ErrorCode.CONFIG_ERROR,
            message=message,
            detail=detail,
            http_status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


# ----------------------------------------------------------------------
# 鉴权与授权异常（HTTP 401 / 403）
#
# 设计说明：
#     401（未认证）与 403（已认证但无权限）语义必须严格区分，否则前端无法
#     判断"该跳登录页"还是"该提示无权限"。因此：
#         UnauthorizedException 及其子类 → 一律 401
#         ForbiddenException 及其子类    → 一律 403
#     安全提示：401 场景下对外文案保持模糊（如"账号或密码错误"不区分是账号不存在
#     还是密码错误），防止攻击者通过响应差异枚举有效账号。
# ----------------------------------------------------------------------
class AuthException(BusinessException):
    """
    鉴权异常基类

    参数：
        code: 鉴权错误码，取自 ErrorCode 的 6xxxx 段
        message: 面向用户的提示
        detail: 内部详情（服务端日志用，生产环境不外发）
        http_status: HTTP 状态码，默认 401

    说明：
        统一以 401 作为默认状态码；需要返回 403 的场景请使用 ForbiddenException。
    """

    def __init__(
        self,
        message: str = "鉴权失败",
        code: int = ErrorCode.AUTH_ERROR,
        detail: Any = None,
        http_status: int = status.HTTP_401_UNAUTHORIZED,
    ) -> None:
        super().__init__(
            code=code,
            message=message,
            detail=detail,
            http_status=http_status,
        )


class UnauthorizedException(AuthException):
    """
    未认证异常（HTTP 401）

    触发场景：
        1. 未携带 Authorization 请求头
        2. Authorization 头格式非法（非 "Bearer <token>"）
        3. token 签名校验失败 / 被篡改
        4. token 已过期
        5. 用 refresh_token 访问业务接口（令牌类型不匹配）
        6. 刷新令牌失效
    """

    def __init__(
        self,
        message: str = "未登录或登录状态已失效，请重新登录",
        code: int = ErrorCode.TOKEN_MISSING,
        detail: Any = None,
    ) -> None:
        super().__init__(
            message=message,
            code=code,
            detail=detail,
            http_status=status.HTTP_401_UNAUTHORIZED,
        )


class ForbiddenException(AuthException):
    """
    无权限异常（HTTP 403）

    触发场景：
        1. 已登录，但当前角色不具备该接口所需权限点
        2. 已登录，但访问的是他人资源（如业务经办人查看别人提交的任务）
        3. 账号状态异常（已停用 / 已锁定）
    """

    def __init__(
        self,
        message: str = "当前账号无权访问该资源",
        code: int = ErrorCode.PERMISSION_DENIED,
        detail: Any = None,
    ) -> None:
        super().__init__(
            message=message,
            code=code,
            detail=detail,
            http_status=status.HTTP_403_FORBIDDEN,
        )


# ======================================================================
# 三、统一响应构造
# ======================================================================
def _build_error_response(
    request: Request,
    code: int,
    message: str,
    detail: Any = None,
    http_status: int = status.HTTP_400_BAD_REQUEST,
) -> JSONResponse:
    """
    构造统一格式的错误响应

    参数：
        request: 当前请求对象，用于提取 request_id
        code: 业务错误码
        message: 错误提示
        detail: 错误详情，生产环境将被置空
        http_status: HTTP 状态码

    返回：
        JSONResponse，结构统一为 {code, message, detail, request_id}
    """
    # 从请求头读取链路 ID，缺省时生成一个，便于跨日志检索
    request_id = request.headers.get("X-Request-Id") or str(uuid.uuid4())

    payload: Dict[str, Any] = {
        "code": code,
        "message": message,
        "detail": None if settings.is_production else detail,
        "request_id": request_id,
    }

    return JSONResponse(status_code=http_status, content=payload)


def register_exception_handlers(app: FastAPI) -> None:
    """
    向 FastAPI 应用注册全局异常处理器

    参数：
        app: FastAPI 应用实例

    返回：
        无返回值

    覆盖范围：
        1. BusinessException  — 业务自定义异常
        2. RequestValidationError — 请求参数校验失败
        3. StarletteHTTPException — HTTP 层异常（404/405 等）
        4. Exception — 兜底未知异常
    """

    @app.exception_handler(BusinessException)
    async def handle_business_exception(
        request: Request, exc: BusinessException
    ) -> JSONResponse:
        """处理业务自定义异常：记录 WARNING 日志并返回统一结构"""
        logger.warning(
            "业务异常 | 路径=%s | 错误码=%s | 提示=%s | 详情=%s",
            request.url.path,
            exc.code,
            exc.message,
            exc.detail,
        )
        return _build_error_response(
            request=request,
            code=exc.code,
            message=exc.message,
            detail=exc.detail,
            http_status=exc.http_status,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_exception(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """处理请求参数校验失败：透出具体字段错误，便于前端定位"""
        logger.warning(
            "参数校验失败 | 路径=%s | 错误=%s", request.url.path, exc.errors()
        )
        return _build_error_response(
            request=request,
            code=ErrorCode.VALIDATION_ERROR,
            message="请求参数校验失败",
            detail=exc.errors(),
            http_status=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        """处理 HTTP 层异常：将状态码映射为业务错误码"""
        code_map = {
            status.HTTP_404_NOT_FOUND: ErrorCode.NOT_FOUND,
            status.HTTP_405_METHOD_NOT_ALLOWED: ErrorCode.METHOD_NOT_ALLOWED,
        }
        code = code_map.get(exc.status_code, ErrorCode.UNKNOWN_ERROR)

        logger.warning(
            "HTTP 异常 | 路径=%s | 状态码=%s | 详情=%s",
            request.url.path,
            exc.status_code,
            exc.detail,
        )
        return _build_error_response(
            request=request,
            code=code,
            message=str(exc.detail),
            detail=None,
            http_status=exc.status_code,
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_exception(
        request: Request, exc: Exception
    ) -> JSONResponse:
        """兜底处理未预期异常：记录完整堆栈，对外隐藏内部细节"""
        logger.error(
            "未预期异常 | 路径=%s | 异常类型=%s | 堆栈=%s",
            request.url.path,
            type(exc).__name__,
            traceback.format_exc(),
        )
        return _build_error_response(
            request=request,
            code=ErrorCode.INTERNAL_ERROR,
            message="服务器内部错误，请联系管理员",
            detail=f"{type(exc).__name__}: {exc}",
            http_status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
