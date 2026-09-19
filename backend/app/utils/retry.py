# D:/ContractReviewSys-合同审批智能审查项目/backend/app/utils/retry.py
"""
重试工具 — 指数退避自动重试装饰器

本模块职责：
    1. 提供异步函数指数退避重试装饰器，供节点执行与外部调用复用
    2. 支持指定可重试异常类型、最大重试次数、退避基数与上限
    3. 重试耗尽后抛出原始异常，由上层决定是否置任务为阻塞

设计要点：
    - 退避序列：base → base*2 → base*4 ... 由 max_delay 封顶
    - 仅对"可重试异常"生效，业务逻辑异常（如文档加密）不应重试
    - 每次重试输出 WARNING 日志，便于观测下游稳定性

返回值说明：
    - retry_async 装饰器：返回包装后的异步函数
    - RetryExhaustedError：重试耗尽异常（携带原始异常与重试次数）

使用示例：
    from app.utils.retry import retry_async

    @retry_async(max_retries=3, base_delay=1.0, retry_on=(httpx.TimeoutException,))
    async def call_external_service():
        ...
"""

import asyncio
import functools
import time
from typing import Any, Callable, Optional, Tuple, Type

from app.core.logging_config import get_logger

logger = get_logger(__name__)


class RetryExhaustedError(Exception):
    """
    重试耗尽异常

    参数：
        original: 最后一次失败的原始异常
        attempts: 实际尝试次数
        function_name: 被装饰函数名，便于定位

    属性：
        original / attempts / function_name：与参数一一对应
    """

    def __init__(self, original: Exception, attempts: int, function_name: str) -> None:
        self.original = original
        self.attempts = attempts
        self.function_name = function_name
        super().__init__(
            f"函数 {function_name} 重试 {attempts} 次后仍失败，"
            f"最后异常：{type(original).__name__}: {original}"
        )


def retry_async(
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 8.0,
    retry_on: Tuple[Type[Exception], ...] = (Exception,),
) -> Callable:
    """
    异步指数退避重试装饰器

    参数：
        max_retries: 最大重试次数（不含首次执行），总执行次数 = max_retries + 1
        base_delay: 退避基数（秒），第 n 次重试等待 base_delay * 2^(n-1)
        max_delay: 单次退避等待上限（秒），防止等待时间无限增长
        retry_on: 需要重试的异常类型元组。不在其中的异常直接向上抛出

    返回：
        装饰器函数

    使用示例：
        @retry_async(max_retries=3, retry_on=(ConnectionError, TimeoutError))
        async def fetch_data():
            ...
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            """
            包装后的执行体

            参数：
                *args: 原函数位置参数
                **kwargs: 原函数关键字参数

            返回：
                原函数返回值

            异常：
                RetryExhaustedError：重试耗尽时抛出，携带原始异常
            """
            last_exception: Optional[Exception] = None

            # 总执行次数 = 首次 + 重试次数
            for attempt in range(max_retries + 1):
                try:
                    return await func(*args, **kwargs)

                except retry_on as exc:
                    last_exception = exc

                    # 已达上限：不再重试，抛出重试耗尽异常
                    if attempt >= max_retries:
                        logger.error(
                            "重试耗尽 | 函数=%s | 尝试=%d次 | 最后异常=%s: %s",
                            func.__name__,
                            attempt + 1,
                            type(exc).__name__,
                            exc,
                        )
                        raise RetryExhaustedError(exc, attempt + 1, func.__name__) from exc

                    # 计算指数退避时长，由 max_delay 封顶
                    delay = min(base_delay * (2 ** attempt), max_delay)
                    logger.warning(
                        "执行失败，%.2fs 后重试 | 函数=%s | 第%d/%d次 | 异常=%s: %s",
                        delay,
                        func.__name__,
                        attempt + 1,
                        max_retries,
                        type(exc).__name__,
                        exc,
                    )
                    await asyncio.sleep(delay)

                # 非可重试异常：直接向上抛出，由上层业务决定处理方式
                except Exception:
                    raise

            # 理论不可达；保留兜底以避免静态检查告警
            if last_exception is not None:
                raise RetryExhaustedError(last_exception, max_retries + 1, func.__name__)
            raise RuntimeError(f"函数 {func.__name__} 重试流程异常终止")

        return wrapper

    return decorator


class RetryGuard:
    """
    重试状态守卫

    说明：
        用于需要跨调用维护重试计数的场景（如任务级重试）。
        节点执行时借助本类记录尝试次数，并计算下一次退避时长。

    属性：
        max_retry: 重试上限
        _count: 当前已重试次数
    """

    def __init__(self, max_retry: int = 3) -> None:
        """
        初始化重试守卫

        参数：
            max_retry: 重试上限，必须为正整数
        """
        if max_retry < 1:
            raise ValueError(f"max_retry 必须为正整数，当前为 {max_retry}")
        self.max_retry = max_retry
        self._count = 0

    @property
    def count(self) -> int:
        """
        当前已重试次数

        返回：
            已重试次数
        """
        return self._count

    @property
    def can_retry(self) -> bool:
        """
        是否仍可重试

        返回：
            True 表示未达上限
        """
        return self._count < self.max_retry

    def next_delay(self) -> float:
        """
        计算下一次退避时长

        返回：
            退避秒数（1s → 2s → 4s → 8s 封顶）
        """
        return min(2.0 ** self._count, 8.0)

    def record_attempt(self) -> None:
        """
        记录一次重试

        返回：
            无返回值
        """
        self._count += 1

    def reset(self) -> None:
        """
        重置计数（人工重跑时调用）

        返回：
            无返回值
        """
        self._count = 0


def measure_elapsed(func: Callable) -> Callable:
    """
    异步函数耗时测量装饰器

    说明：
        在日志中记录函数执行耗时，超阈值时以 WARNING 输出，
        便于定位性能瓶颈节点。

    参数：
        func: 被装饰的异步函数

    返回：
        包装后的异步函数
    """

    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        start = time.perf_counter()
        try:
            return await func(*args, **kwargs)
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            logger.debug(
                "函数耗时 | 函数=%s | 耗时=%.2fms", func.__name__, elapsed_ms
            )

    return wrapper
