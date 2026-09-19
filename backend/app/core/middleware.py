# D:/ContractReviewSys-合同审批智能审查项目/backend/app/core/middleware.py
"""
自定义中间件 — 请求链路追踪与统一耗时统计

本模块职责：
    1. 为每个请求生成或透传 X-Request-Id，写入响应头与日志上下文
    2. 统计每个请求的耗时，超过阈值时输出 WARNING 日志
    3. 记录请求入口与出口日志，形成完整调用链

设计要点：
    - 基于 Starlette BaseHTTPMiddleware 实现，轻量无额外依赖
    - 使用 contextvars 保存 request_id，保证异步并发下互不串号
    - 不吞异常，异常交由全局异常处理器统一处理

返回值说明：
    - RequestIdMiddleware：中间件类，注册到 FastAPI 即可
    - get_request_id()：从上下文读取当前请求的链路 ID

使用示例：
    app.add_middleware(RequestIdMiddleware)
"""

import time
import uuid
from contextvars import ContextVar
from typing import Callable, Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.logging_config import get_logger

logger = get_logger(__name__)

# 请求链路 ID 上下文变量：异步并发下每个请求独立持有，互不干扰
_request_id_ctx: ContextVar[Optional[str]] = ContextVar("request_id", default=None)

# 慢请求阈值（毫秒）：超过该耗时的请求将输出 WARNING 日志
SLOW_REQUEST_THRESHOLD_MS: float = 2000.0

# 无需记录访问日志的路径前缀，避免健康检查刷屏
_SKIP_LOG_PATHS = ("/health", "/favicon.ico", "/metrics")


def get_request_id() -> Optional[str]:
    """
    获取当前请求的链路 ID

    返回：
        当前上下文的 request_id；不在请求上下文中时返回 None
    """
    return _request_id_ctx.get()


class RequestIdMiddleware(BaseHTTPMiddleware):
    """
    请求链路追踪中间件

    行为：
        1. 优先透传客户端传入的 X-Request-Id（便于全链路串联）
        2. 客户端未传时自动生成 UUID4 作为链路 ID
        3. 将链路 ID 写入响应头 X-Request-Id，并存入 contextvars
        4. 记录方法、路径、状态码、耗时
        5. 耗时超过阈值时以 WARNING 级别单独告警
    """

    async def dispatch(
        self, request: Request, call_next: Callable
    ) -> Response:
        """
        中间件主处理逻辑

        参数：
            request: 当前请求对象
            call_next: 下游处理调用链

        返回：
            Response：正常响应或由异常处理器转换后的响应
        """
        # 1. 确定链路 ID：优先复用客户端透传值
        request_id = request.headers.get("X-Request-Id") or str(uuid.uuid4())

        # 2. 写入上下文变量，供本次请求内任意位置读取
        token = _request_id_ctx.set(request_id)

        # 3. 记录起始时间
        start_ts = time.perf_counter()

        try:
            # 4. 调用下游处理链
            response = await call_next(request)

            # 5. 回写链路 ID 到响应头
            response.headers["X-Request-Id"] = request_id

            # 6. 计算耗时
            elapsed_ms = (time.perf_counter() - start_ts) * 1000.0

            # 7. 输出访问日志（健康检查等路径跳过）
            if not request.url.path.startswith(_SKIP_LOG_PATHS):
                logger.info(
                    "访问日志 | 链路=%s | %s %s | 状态=%s | 耗时=%.2fms",
                    request_id,
                    request.method,
                    request.url.path,
                    response.status_code,
                    elapsed_ms,
                )

            # 8. 慢请求单独告警，便于性能排查
            if elapsed_ms > SLOW_REQUEST_THRESHOLD_MS:
                logger.warning(
                    "慢请求告警 | 链路=%s | %s %s | 耗时=%.2fms | 阈值=%.2fms",
                    request_id,
                    request.method,
                    request.url.path,
                    elapsed_ms,
                    SLOW_REQUEST_THRESHOLD_MS,
                )

            return response

        finally:
            # 9. 重置上下文变量，避免污染同线程后续任务
            _request_id_ctx.reset(token)
