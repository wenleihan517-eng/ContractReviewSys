# D:/ContractReviewSys-合同审批智能审查项目/backend/app/utils/id_generator.py
"""
ID 与编号生成工具

本模块职责：
    1. 生成任务编号（task_no），规则：T + 年月日 + 当日序号
    2. 生成合同编号（contract_no），规则：HT + 年月日 + 当日序号
    3. 生成 LangGraph 会话 ID（thread_id），保证全局唯一
    4. 生成任务独立工作目录名，保证文件隔离

设计要点：
    - 序号使用当日时间戳后 4 位 + 随机 2 位组合，避免依赖数据库自增
    - thread_id 使用 UUID4，全局唯一，杜绝并发会话串扰
    - work_dir 命名包含 task_id，保证目录级隔离

返回值说明：
    - generate_task_no()：返回任务编号字符串
    - generate_contract_no()：返回合同编号字符串
    - generate_thread_id()：返回会话 ID 字符串
    - build_work_dir(task_id)：返回工作目录相对路径

使用示例：
    from app.utils.id_generator import generate_task_no, generate_thread_id
    task_no = generate_task_no()
    thread_id = generate_thread_id()
"""

import random
import uuid
from datetime import datetime

# 任务编号前缀
TASK_NO_PREFIX = "T"
# 合同编号前缀
CONTRACT_NO_PREFIX = "HT"
# 工作目录根路径（相对于任务存储根）
WORK_DIR_ROOT = "tasks"


def _daily_serial() -> str:
    """
    生成当日序列片段

    说明：
        由"时分秒微秒 + 2 位随机数"组成，保证同日内高并发下不重复。
        不依赖数据库自增，避免引入额外计数表。

    返回：
        6 位数字字符串，如 "143052"
    """
    now = datetime.now()
    # 时分秒（6 位）作为主体，保证时间上单调倾向
    base = now.strftime("%H%M%S")
    # 追加 2 位随机数，降低同一秒内并发碰撞概率
    suffix = random.randint(10, 99)
    return f"{base}{suffix}"


def generate_task_no() -> str:
    """
    生成审查任务编号

    格式：T + yyyyMMdd + 时分秒 + 2位随机数
    示例：T2026091814305273

    返回：
        任务编号字符串
    """
    today = datetime.now().strftime("%Y%m%d")
    return f"{TASK_NO_PREFIX}{today}{_daily_serial()}"


def generate_contract_no() -> str:
    """
    生成合同编号

    格式：HT + yyyyMMdd + 时分秒 + 2位随机数
    示例：HT2026091814305273

    返回：
        合同编号字符串
    """
    today = datetime.now().strftime("%Y%m%d")
    return f"{CONTRACT_NO_PREFIX}{today}{_daily_serial()}"


def generate_thread_id() -> str:
    """
    生成 LangGraph 会话 ID

    说明：
        使用 UUID4 保证全局唯一。每个审查任务独立持有一个 thread_id，
        实现异步会话隔离，避免并发任务上下文串扰。

    返回：
        32 位十六进制字符串（无连字符）
    """
    return uuid.uuid4().hex


def generate_object_key(prefix: str, file_name: str) -> str:
    """
    生成 MinIO 对象键

    格式：{prefix}/{yyyy}/{MM}/{dd}/{uuid32}.{ext}
    示例：contract/2026/09/18/3f2a1b...c9d8.pdf

    参数：
        prefix: 业务前缀，如 contract / ocr / report / tmp
        file_name: 原始文件名，用于提取扩展名

    返回：
        规范化对象键字符串
    """
    # 提取扩展名（小写，含点）；无扩展名时留空
    ext = ""
    if "." in file_name:
        ext = "." + file_name.rsplit(".", 1)[-1].lower()

    today = datetime.now()
    return f"{prefix}/{today:%Y/%m/%d}/{uuid.uuid4().hex}{ext}"


def build_work_dir(task_id: str) -> str:
    """
    构造任务独立工作目录路径

    说明：
        每个任务拥有独立目录，用于存放解析中间产物、OCR 图片、临时文件，
        实现任务级文件隔离，避免并发任务互相覆盖。

    参数：
        task_id: 任务 ID

    返回：
        形如 tasks/{task_id} 的相对路径
    """
    return f"{WORK_DIR_ROOT}/{task_id}"
