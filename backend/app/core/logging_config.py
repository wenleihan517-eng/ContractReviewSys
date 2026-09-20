# D:/ContractReviewSys-合同审批智能审查项目/backend/app/core/logging_config.py
"""
日志配置中心 — 统一日志初始化

本模块职责：
    1. 构建统一日志格式（含时间、级别、模块名、行号、消息）
    2. 同时输出到控制台与滚动文件，日志文件按天切割并限制保留天数
    3. 统一压制第三方库（如 urllib3、pymongo、milvus）的冗余日志
    4. 提供 get_logger() 供业务模块获取带命名空间的 logger

设计要点：
    - 使用标准库 logging，不引入额外依赖
    - 采用 TimedRotatingFileHandler 按天切割，通过 backupCount 控制保留天数
    - 幂等初始化：重复调用 init_logging 不会重复添加 handler

返回值说明：
    - init_logging()：无返回值，副作用为完成日志系统初始化
    - get_logger(name)：返回指定命名空间的 Logger 实例

使用示例：
    from app.core.logging_config import get_logger
    logger = get_logger(__name__)
    logger.info("服务启动完成")
"""

import logging
import logging.handlers
import os
import sys
from typing import Optional

from app.core.config import settings


# 日志输出格式：时间 | 级别 | 模块:行号 | 消息
LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s:%(lineno)d | %(message)s"
# 时间格式，精确到毫秒
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# 初始化标记，保证幂等，避免重复挂载 handler 导致日志重复输出
_LOGGING_INITIALIZED: bool = False

# 需要压制为 WARNING 级别的第三方库，避免其 DEBUG/INFO 日志淹没业务日志
_NOISY_LOGGERS = (
    "urllib3",
    "pymongo",
    "pymilvus",
    "minio",
    "httpx",
    "httpcore",
    "asyncio",
    "multipart",
    "watchfiles",
)


def _build_console_handler() -> logging.Handler:
    """
    构建控制台日志处理器

    返回：
        配置好的 StreamHandler，输出到标准输出
    """
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setLevel(getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO))
    handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))
    return handler


def _build_file_handler() -> logging.Handler:
    """
    构建滚动文件日志处理器

    说明：
        日志目录不存在时自动创建；文件按天切割，
        backupCount 由 LOG_RETENTION_DAYS 控制，实现超期自动清理。

    返回：
        配置好的 TimedRotatingFileHandler
    """
    # 计算日志目录绝对路径：项目根目录 / storage / logs
    # 说明：config.py 位于 backend/app/core/ 下，向上四级即项目根目录
    project_root = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "..")
    )
    log_dir = os.path.join(project_root, settings.LOG_DIR)
    os.makedirs(log_dir, exist_ok=True)

    log_path = os.path.join(log_dir, settings.LOG_FILE_NAME)

    handler = logging.handlers.TimedRotatingFileHandler(
        filename=log_path,
        when="midnight",                       # 每天零点切割
        interval=1,                            # 切割间隔为 1 天
        backupCount=settings.LOG_RETENTION_DAYS,  # 保留天数，超期自动删除
        encoding="utf-8",                      # 中文日志必须用 UTF-8
        delay=False,                           # 立即创建文件，便于排查启动期问题
    )
    # 切割后的历史文件后缀，如 contract_review.log.2026-09-18
    handler.suffix = "%Y-%m-%d"
    handler.setLevel(getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO))
    handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))
    return handler


def init_logging(force: bool = False) -> None:
    """
    初始化全局日志系统

    参数：
        force: 是否强制重新初始化。
               False（默认）时若已初始化则直接返回，保证幂等；
               True 时先移除已有 handler 再重建，用于测试或配置热更新场景

    返回：
        无返回值
    """
    global _LOGGING_INITIALIZED

    # 幂等保护：已初始化且非强制时直接返回
    if _LOGGING_INITIALIZED and not force:
        return

    root_logger = logging.getLogger()

    # 强制模式或首次初始化：清空已有 handler，防止日志重复打印
    for existing in list(root_logger.handlers):
        root_logger.removeHandler(existing)

    # 设置根日志级别
    root_logger.setLevel(getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO))

    # 挂载控制台处理器
    if settings.LOG_CONSOLE_OUTPUT:
        root_logger.addHandler(_build_console_handler())

    # 挂载文件处理器
    root_logger.addHandler(_build_file_handler())

    # 压制第三方库冗余日志
    for noisy in _NOISY_LOGGERS:
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _LOGGING_INITIALIZED = True

    # 输出初始化完成信息，便于确认日志路径与级别
    root_logger.info(
        "日志系统初始化完成 | 级别=%s | 目录=%s/%s | 保留天数=%d",
        settings.LOG_LEVEL,
        settings.LOG_DIR,
        settings.LOG_FILE_NAME,
        settings.LOG_RETENTION_DAYS,
    )


def get_logger(name: Optional[str] = None) -> logging.Logger:
    """
    获取带命名空间的 Logger

    参数：
        name: 日志命名空间，通常传入 __name__
              为空时返回根 logger

    返回：
        Logger 实例。若日志系统尚未初始化，会自动触发初始化
    """
    # 兜底初始化，防止业务代码未显式调用 init_logging 就直接取 logger
    if not _LOGGING_INITIALIZED:
        init_logging()

    return logging.getLogger(name)
