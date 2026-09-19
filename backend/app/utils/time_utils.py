# D:/ContractReviewSys-合同审批智能审查项目/backend/app/utils/time_utils.py
"""
时间处理工具

本模块职责：
    1. 获取当前时间戳（秒级 / 毫秒级）
    2. 时间格式化与解析
    3. 时长人性化展示（用于前端展示节点耗时）
    4. 计算两个时间点之间的耗时

设计要点：
    - 全项目统一使用本地时间（datetime.now），不使用 UTC，避免前端展示错位
    - 时间戳统一为整数秒，便于存入 MongoDB 与 Milvus 的 INT64 字段

返回值说明：
    - now_timestamp()：返回当前 Unix 时间戳（秒）
    - format_datetime()：返回格式化时间字符串
    - parse_datetime()：返回 datetime 对象

使用示例：
    from app.utils.time_utils import now_timestamp, humanize_duration
    ts = now_timestamp()
    print(humanize_duration(125000))   # "2分5秒"
"""

from datetime import datetime
from typing import Optional

# 默认时间展示格式
DEFAULT_FORMAT = "%Y-%m-%d %H:%M:%S"


def now_timestamp() -> int:
    """
    获取当前 Unix 时间戳（秒级）

    返回：
        整数时间戳，如 1789000000
    """
    return int(datetime.now().timestamp())


def now_timestamp_ms() -> int:
    """
    获取当前 Unix 时间戳（毫秒级）

    返回：
        整数毫秒时间戳，用于精度要求较高的耗时统计
    """
    return int(datetime.now().timestamp() * 1000)


def format_datetime(dt: Optional[datetime], fmt: str = DEFAULT_FORMAT) -> str:
    """
    格式化时间对象为字符串

    参数：
        dt: 待格式化的时间对象。为 None 时返回空字符串
        fmt: 格式模板，默认 "YYYY-MM-DD HH:MM:SS"

    返回：
        格式化后的时间字符串；输入为 None 时返回 ""
    """
    if dt is None:
        return ""
    return dt.strftime(fmt)


def parse_datetime(text: str, fmt: str = DEFAULT_FORMAT) -> Optional[datetime]:
    """
    解析时间字符串为时间对象

    参数：
        text: 时间字符串
        fmt: 格式模板，需与字符串匹配

    返回：
        解析成功返回 datetime 对象；解析失败返回 None
    """
    try:
        return datetime.strptime(text, fmt)
    except (ValueError, TypeError):
        return None


def elapsed_ms(start: Optional[datetime], end: Optional[datetime]) -> Optional[float]:
    """
    计算两个时间点之间的耗时（毫秒）

    参数：
        start: 起始时间
        end: 结束时间

    返回：
        耗时毫秒数；任一为空时返回 None
    """
    if start is None or end is None:
        return None
    return (end - start).total_seconds() * 1000.0


def humanize_duration(milliseconds: float) -> str:
    """
    将毫秒数转换为人性化时长文本

    说明：
        用于前端展示节点耗时，避免直接展示大数字。

    参数：
        milliseconds: 毫秒数

    返回：
        人性化时长文本，如 "350毫秒" / "2.5秒" / "3分20秒" / "1时5分"
    """
    if milliseconds < 0:
        return "0毫秒"

    # 小于 1 秒：直接展示毫秒
    if milliseconds < 1000:
        return f"{int(milliseconds)}毫秒"

    seconds = milliseconds / 1000.0

    # 小于 60 秒：展示秒（保留 1 位小数）
    if seconds < 60:
        return f"{seconds:.1f}秒"

    minutes = int(seconds // 60)
    remain_seconds = int(seconds % 60)

    # 小于 60 分钟：展示分秒
    if minutes < 60:
        return f"{minutes}分{remain_seconds}秒"

    hours = minutes // 60
    remain_minutes = minutes % 60
    return f"{hours}时{remain_minutes}分"


def is_expired(expire_at: Optional[datetime]) -> bool:
    """
    判断是否已过期

    参数：
        expire_at: 过期时间点

    返回：
        True 表示已过期；expire_at 为空时返回 False（视为永不过期）
    """
    if expire_at is None:
        return False
    return datetime.now() > expire_at
