# D:/ContractReviewSys-合同审批智能审查项目/backend/app/repositories/user_repo.py
"""
用户数据访问层 — 对应 MongoDB 集合 cr_user

【模块定位】
    本模块封装用户账号的持久化读写，为登录鉴权链路提供数据访问支撑。
    职责单一：只做 CRUD + 登录计数维护，不含业务编排
    （登录流程编排在 app/services/auth_service.py）。

【设计要点】
    - 使用 motor 异步驱动，所有方法为 async
    - 返回 Pydantic 模型（User / UserInDB）而非裸 dict，保证类型安全
    - 延迟获取集合：避免模块导入时触发存储初始化，便于单元测试替换
    - username 唯一性由唯一索引保证（storage 层建立），本层做重复前置提示
    - 登录失败计数、锁定、登录成功的状态更新都收口在此处，
      保证"账号安全状态"只有一条写入路径，避免散落导致计数错乱

返回值说明：
    - create_user()：返回插入后的 user_id
    - find_by_username() / find_by_id()：返回 UserInDB 或 None
    - list_users()：返回 (items, total)
    - 其余更新方法：返回 bool 表示是否命中并修改了文档
"""

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from pymongo import ASCENDING, ReturnDocument

from app.constants import MongoCollection, UserRole, UserStatus
from app.core.logging_config import get_logger
from app.models.user import User, UserInDB

logger = get_logger(__name__)


def _collection():
    """
    获取用户集合（延迟获取）

    返回：
        motor 异步集合对象

    说明：
        采用延迟 import + 延迟取集合，而不是模块顶层直接取，
        原因是模块导入时存储尚未初始化（init_all_storages 在 lifespan 中执行），
        顶层获取会得到 None 并导致后续所有调用失败。
    """
    from app.storage.mongo_client import mongo_client

    return mongo_client.get_collection(MongoCollection.USER)


def _to_model(raw: Optional[Dict[str, Any]]) -> Optional[UserInDB]:
    """
    将 MongoDB 原始文档转换为 UserInDB 模型

    参数：
        raw: MongoDB 查询返回的原始文档；为 None 时直接返回 None

    返回：
        Optional[UserInDB]：转换后的模型实例

    异常：
        无 —— 转换失败时记录错误日志并返回 None

    说明：
        1. 将 _id 映射为模型字段 id，并把 ObjectId 转为字符串，
           使模型与驱动解耦（模型层不感知 ObjectId 类型）。
        2. 转换失败（如历史脏数据字段类型不符）不抛出，而是返回 None 并告警。
           理由是鉴权链路中"用户不存在"与"用户数据损坏"对调用方的处置相同
           （一律拒绝登录），抛异常会污染为 500 错误。
    """
    if raw is None:
        return None
    data = dict(raw)
    if "_id" in data:
        data["id"] = str(data.pop("_id"))
    try:
        return UserInDB(**data)
    except Exception as exc:  # noqa: BLE001 - 脏数据容错
        logger.error(
            "用户文档转换失败 | username=%s | 异常=%s",
            data.get("username"),
            exc,
        )
        return None


def _to_model_list(raws: List[Dict[str, Any]]) -> List[UserInDB]:
    """
    批量转换用户文档

    参数：
        raws: MongoDB 查询结果列表

    返回：
        List[UserInDB]：转换成功的模型列表（跳过转换失败的脏数据）
    """
    result: List[UserInDB] = []
    for raw in raws:
        model = _to_model(raw)
        if model is not None:
            result.append(model)
    return result


# ======================================================================
# 一、写入
# ======================================================================
async def create_user(user: User) -> str:
    """
    创建用户

    参数：
        user: User 实例（password_hash 必须已是 bcrypt 哈希串）

    返回：
        str：插入后的文档 _id

    异常：
        MongoDBException：写入失败或用户名已存在

    说明：
        依赖 cr_user.username 上的唯一索引做最终防重。
        并发场景下先查后插存在竞态，唯一索引是唯可靠屏障，
        因此捕获 DuplicateKeyError 并转换为明确的业务提示。
    """
    from pymongo.errors import DuplicateKeyError

    from app.core.exceptions import MongoDBException

    collection = _collection()
    payload = user.to_mongo_document()

    try:
        result = await collection.insert_one(payload)
        user_id = str(result.inserted_id)
        logger.info(
            "用户已创建 | username=%s | role=%s | user_id=%s",
            user.username,
            user.role.value,
            user_id,
        )
        return user_id
    except DuplicateKeyError as exc:
        logger.warning("用户名已存在，拒绝创建 | username=%s", user.username)
        raise MongoDBException(
            message=f"用户名 {user.username} 已存在",
            detail=str(exc),
        ) from exc
    except Exception as exc:  # noqa: BLE001 - 统一转业务异常
        logger.error("用户创建失败 | username=%s | 异常=%s", user.username, exc)
        raise MongoDBException(message="用户创建失败", detail=str(exc)) from exc


async def upsert_builtin_user(user: User) -> Tuple[str, bool]:
    """
    幂等写入内置账号（存在则仅补齐缺失的静态字段，不覆盖密码）

    参数：
        user: 内置账号的 User 实例

    返回：
        Tuple[str, bool]：(user_id, created)
            created=True  表示本次新建了账号
            created=False 表示账号已存在，未新建

    异常：
        MongoDBException：写入失败

    说明：
        【关键设计】使用 $setOnInsert 而非 $set 更新密码字段。
        原因：运维或用户可能已经修改过内置账号的密码，
        若启动时用 $set 强行重置为初始密码，会造成"改密后重启即失效"
        的严重体验问题与安全隐患。此处只在新插入时写入初始密码，
        对已存在的账号仅刷新角色/展示名/状态等静态描述字段。
    """
    from app.core.exceptions import MongoDBException

    collection = _collection()
    now = datetime.now()

    # 仅在这些字段缺失时补写的基础字段
    #
    # 【关键】token_version / failed_login_count 必须包含在 $setOnInsert 中。
    # 原因：这两个字段靠 $inc 递增实现"令牌撤销"与"失败计数"。
    #   若文档中根本不存在该字段，MongoDB 的 $inc 会把缺失字段当作 0，
    #   结果是 `1`（可撤销成功一次），但若字段始终缺失，递增基准就会
    #   与模型中声明的初始值（token_version=1）不一致，
    #   出现"改密/登出后令牌版本仍是 1、旧令牌继续可用"的严重安全漏洞。
    #   显式在插入时写入初始值，保证 $inc 的基准与模型定义一致。
    set_on_insert: Dict[str, Any] = {
        "password_hash": user.password_hash,
        "token_version": user.token_version,
        "failed_login_count": 0,
        "locked_until": None,
        "last_login_at": None,
        "last_login_ip": None,
        "created_at": now,
        "created_by": "system",
    }
    # 每次启动都对齐的静态描述字段（不涉及凭证，安全）
    set_fields: Dict[str, Any] = {
        "display_name": user.display_name,
        "role": user.role.value,
        "status": user.status.value,
        "department": user.department,
        "email": user.email,
        "remark": user.remark,
        "is_builtin": True,
        "updated_at": now,
    }

    try:
        result = await collection.find_one_and_update(
            {"username": user.username},
            {
                "$set": set_fields,
                "$setOnInsert": set_on_insert,
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        user_id = str(result["_id"]) if result else ""
        # 通过 created_at 是否等于本次的 now 判断是否为新建
        created = bool(result) and result.get("created_at") == now
        logger.info(
            "内置账号已就绪 | username=%s | role=%s | 新建=%s",
            user.username,
            user.role.value,
            created,
        )
        return user_id, created
    except Exception as exc:  # noqa: BLE001 - 统一转业务异常
        logger.error("内置账号初始化失败 | username=%s | 异常=%s", user.username, exc)
        raise MongoDBException(message="内置账号初始化失败", detail=str(exc)) from exc


async def update_password_hash(username: str, password_hash: str) -> bool:
    """
    更新用户密码哈希（改密 / 哈希强度升级）

    参数：
        username:      用户名
        password_hash: 新的 bcrypt 哈希串

    返回：
        bool：命中并修改返回 True，用户不存在返回 False

    异常：
        MongoDBException：更新失败

    说明：
        同时递增 token_version，使旧令牌立即失效——
        否则攻击者即使拿到旧令牌，在用户改密后仍可继续访问。
    """
    from app.core.exceptions import MongoDBException

    # 先补齐可能缺失的 token_version，保证 $inc 的递增基准正确
    await ensure_token_version(username)

    collection = _collection()
    try:
        result = await collection.update_one(
            {"username": username},
            {
                "$set": {"password_hash": password_hash, "updated_at": datetime.now()},
                "$inc": {"token_version": 1},
            },
        )
        return result.modified_count > 0
    except Exception as exc:  # noqa: BLE001
        logger.error("密码更新失败 | username=%s | 异常=%s", username, exc)
        raise MongoDBException(message="密码更新失败", detail=str(exc)) from exc


async def ensure_token_version(username: str) -> None:
    """
    确保用户的 token_version 字段存在（历史数据修复）

    参数：
        username: 用户名

    返回：
        无返回值

    异常：
        无 —— 修复失败仅记录告警，不阻断业务流程

    说明：
        【为什么需要这个函数】
        历史数据或异常写入可能造成文档中缺失 token_version 字段。
        此时 $inc 会把缺失字段视为 0，递增结果为 1，
        而模型默认值恰好也是 1 —— 结果是"令牌版本看起来从没变过"，
        导致改密/登出后旧令牌仍然可用（撤销功能失效）。
        这是极难发现的静默安全缺陷，因此在每次撤销操作前主动补齐该字段。

        该函数幂等，对已有该字段的文档不做任何修改。
    """
    collection = _collection()
    try:
        await collection.update_one(
            {"username": username, "token_version": {"$exists": False}},
            {"$set": {"token_version": 1}},
        )
    except Exception as exc:  # noqa: BLE001 - 修复失败不应阻断登出/改密
        logger.warning("token_version 字段修复失败（不阻断操作）| username=%s | 异常=%s", username, exc)


# ======================================================================
# 二、查询
# ======================================================================
async def find_by_username(username: str) -> Optional[UserInDB]:
    """
    按用户名查询用户（登录链路主入口）

    参数：
        username: 用户名

    返回：
        Optional[UserInDB]：找到返回模型，未找到返回 None

    异常：
        MongoDBException：查询失败

    说明：
        用户名大小写敏感（与唯一索引口径一致），
        注册/初始化时已限制为字母数字下划线，避免大小写歧义。
    """
    from app.core.exceptions import MongoDBException

    collection = _collection()
    try:
        raw = await collection.find_one({"username": username})
        return _to_model(raw)
    except Exception as exc:  # noqa: BLE001
        logger.error("按用户名查询用户失败 | username=%s | 异常=%s", username, exc)
        raise MongoDBException(message="用户查询失败", detail=str(exc)) from exc


async def find_by_id(user_id: str) -> Optional[UserInDB]:
    """
    按用户 id 查询用户（鉴权链路主入口）

    参数：
        user_id: 用户 id 字符串（MongoDB _id 的字符串形式）

    返回：
        Optional[UserInDB]：找到返回模型，未找到或 id 非法返回 None

    异常：
        MongoDBException：查询失败

    说明：
        入参来自 JWT 载荷，可能被篡改或为历史脏数据，
        因此必须先尝试转 ObjectId；转换失败直接返回 None（视为用户不存在），
        不能让其抛异常冒泡为 500。
    """
    from bson import ObjectId
    from bson.errors import InvalidId

    from app.core.exceptions import MongoDBException

    if not user_id:
        return None

    try:
        object_id = ObjectId(user_id)
    except (InvalidId, TypeError):
        logger.warning("用户 id 格式非法，视为不存在 | user_id=%s", user_id)
        return None

    collection = _collection()
    try:
        raw = await collection.find_one({"_id": object_id})
        return _to_model(raw)
    except Exception as exc:  # noqa: BLE001
        logger.error("按 id 查询用户失败 | user_id=%s | 异常=%s", user_id, exc)
        raise MongoDBException(message="用户查询失败", detail=str(exc)) from exc


async def list_users(
    role: Optional[str] = None,
    status: Optional[str] = None,
    keyword: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
) -> Tuple[List[UserInDB], int]:
    """
    分页查询用户列表（管理员用户管理用）

    参数：
        role:      按角色筛选，缺省不筛选
        status:    按状态筛选，缺省不筛选
        keyword:   用户名/展示名模糊搜索（不区分大小写），缺省不筛选
        page:      页码，从 1 开始
        page_size: 每页条数

    返回：
        Tuple[List[UserInDB], int]：(当前页用户列表, 符合条件的总数)

    异常：
        MongoDBException：查询失败

    说明：
        keyword 使用正则做前缀无关的包含匹配，并对用户输入做转义，
        防止正则元字符（如 . * ( ) ）被当作模式解析导致查询异常或全表扫描。
    """
    import re

    from app.core.exceptions import MongoDBException

    query: Dict[str, Any] = {}
    if role:
        query["role"] = role
    if status:
        query["status"] = status
    if keyword:
        safe_keyword = re.escape(keyword.strip())
        query["$or"] = [
            {"username": {"$regex": safe_keyword, "$options": "i"}},
            {"display_name": {"$regex": safe_keyword, "$options": "i"}},
        ]

    skip = max(0, (page - 1) * page_size)
    collection = _collection()

    try:
        total = await collection.count_documents(query)
        cursor = (
            collection.find(query)
            .sort("created_at", ASCENDING)
            .skip(skip)
            .limit(page_size)
        )
        raws = await cursor.to_list(length=page_size)
        return _to_model_list(raws), total
    except Exception as exc:  # noqa: BLE001
        logger.error("用户列表查询失败 | 查询条件=%s | 异常=%s", query, exc)
        raise MongoDBException(message="用户列表查询失败", detail=str(exc)) from exc


async def count_by_role(role: str) -> int:
    """
    统计指定角色的用户数量

    参数：
        role: 角色标识

    返回：
        int：用户数量

    说明：
        用于「不能删除最后一个管理员」这类保护性业务判断，
        确保系统始终存在至少一个可登录的管理员。
    """
    collection = _collection()
    try:
        return await collection.count_documents({"role": role, "status": {"$ne": UserStatus.DISABLED.value}})
    except Exception as exc:  # noqa: BLE001
        logger.error("角色用户统计失败 | role=%s | 异常=%s", role, exc)
        return 0


# ======================================================================
# 三、登录安全状态维护
# ======================================================================
async def register_login_success(
    username: str,
    login_ip: Optional[str] = None,
) -> bool:
    """
    记录登录成功（清零失败计数、解除锁定、更新登录审计信息）

    参数：
        username: 用户名
        login_ip: 登录来源 IP

    返回：
        bool：更新成功返回 True

    异常：
        MongoDBException：更新失败

    说明：
        登录成功后必须清零 failed_login_count 并清除 locked_until，
        否则用户"失败 4 次 → 成功登录 → 再失败 1 次"就会被误锁，
        与"连续失败"的语义不符。
    """
    from app.core.exceptions import MongoDBException

    collection = _collection()
    now = datetime.now()
    set_fields: Dict[str, Any] = {
        "failed_login_count": 0,
        "locked_until": None,
        "last_login_at": now,
        "updated_at": now,
        # 登录成功说明账号可用，若此前被锁定则同步回置为 active
        "status": UserStatus.ACTIVE.value,
    }
    if login_ip:
        set_fields["last_login_ip"] = login_ip

    try:
        result = await collection.update_one(
            {"username": username},
            {"$set": set_fields},
        )
        logger.info("登录成功已记录 | username=%s | ip=%s", username, login_ip or "unknown")
        return result.modified_count > 0
    except Exception as exc:  # noqa: BLE001
        logger.error("登录成功记录失败 | username=%s | 异常=%s", username, exc)
        raise MongoDBException(message="登录状态更新失败", detail=str(exc)) from exc


async def register_login_failure(
    username: str,
    max_failures: int,
    lock_minutes: int,
) -> Dict[str, Any]:
    """
    记录登录失败（累加计数，达到阈值则锁定账号）

    参数：
        username:     用户名
        max_failures: 锁定阈值，来自配置 AUTH_MAX_LOGIN_FAILURES；为 0 表示不锁定
        lock_minutes: 锁定时长（分钟），来自配置 AUTH_LOGIN_LOCK_MINUTES

    返回：
        Dict[str, Any]：包含以下键
            failed_count: int  累计失败次数（本次之后）
            locked:       bool 本次是否触发了锁定

    异常：
        MongoDBException：更新失败

    说明：
        使用 $inc 原子自增而非"先读后写"，避免并发暴力破解时
        多个请求同时读到旧计数、各自加 1 后互相覆盖，
        导致实际失败次数远小于真实尝试次数（计数失真会让锁定形同虚设）。
        自增后用返回值读出真实计数，再决定是否写锁定时间。
    """
    from app.core.exceptions import MongoDBException

    collection = _collection()
    now = datetime.now()

    try:
        # 第一步：原子累加失败计数，同时读出累加后的值
        updated = await collection.find_one_and_update(
            {"username": username},
            {"$inc": {"failed_login_count": 1}, "$set": {"updated_at": now}},
            return_document=ReturnDocument.AFTER,
        )

        if updated is None:
            # 用户不存在：不泄露该信息，仅返回计数 1，由调用方统一按"账号或密码错误"处理
            logger.info("登录失败（用户不存在）| username=%s", username)
            return {"failed_count": 0, "locked": False}

        failed_count = int(updated.get("failed_login_count", 1))
        locked = False

        # 第二步：达到阈值则写入锁定时间
        if max_failures > 0 and failed_count >= max_failures:
            locked_until = now + timedelta(minutes=lock_minutes)
            await collection.update_one(
                {"username": username},
                {
                    "$set": {
                        "locked_until": locked_until,
                        "status": UserStatus.LOCKED.value,
                        "updated_at": now,
                    }
                },
            )
            locked = True
            logger.warning(
                "账号已锁定 | username=%s | 累计失败=%s | 解锁时间=%s",
                username,
                failed_count,
                locked_until.isoformat(),
            )
        else:
            logger.info(
                "登录失败已记录 | username=%s | 累计失败=%s", username, failed_count
            )

        return {"failed_count": failed_count, "locked": locked}
    except Exception as exc:  # noqa: BLE001
        logger.error("登录失败记录失败 | username=%s | 异常=%s", username, exc)
        raise MongoDBException(message="登录失败记录失败", detail=str(exc)) from exc


async def clear_expired_lock(username: str) -> bool:
    """
    清除已过期的账号锁定（用户下次登录尝试时调用）

    参数：
        username: 用户名

    返回：
        bool：实际清除了锁定时返回 True

    异常：
        MongoDBException：更新失败

    说明：
        锁定期满后不自动恢复 status 字段，会出现"锁已过期但状态仍为 locked"
        的状态漂移。本方法在登录尝试前调用，把状态显式回置为 active，
        保证数据库状态与可登录的实际行为一致，便于运维排查。
    """
    from app.core.exceptions import MongoDBException

    collection = _collection()
    now = datetime.now()
    try:
        result = await collection.update_one(
            {
                "username": username,
                "locked_until": {"$ne": None, "$lte": now},
            },
            {
                "$set": {
                    "locked_until": None,
                    "failed_login_count": 0,
                    "status": UserStatus.ACTIVE.value,
                    "updated_at": now,
                }
            },
        )
        if result.modified_count > 0:
            logger.info("账号锁定已到期并自动解除 | username=%s", username)
        return result.modified_count > 0
    except Exception as exc:  # noqa: BLE001
        logger.error("解除锁定失败 | username=%s | 异常=%s", username, exc)
        raise MongoDBException(message="解除账号锁定失败", detail=str(exc)) from exc


async def revoke_tokens(username: str) -> bool:
    """
    撤销该用户已签发的全部令牌（登出 / 强制下线）

    参数：
        username: 用户名

    返回：
        bool：撤销成功返回 True

    异常：
        MongoDBException：更新失败

    说明：
        JWT 无状态，服务端无法"删除"已签发的令牌。
        递增 token_version 后，鉴权时比对载荷中的 ver 与数据库值，
        不一致即判定失效，从而等效实现"立即登出"。
        这是无状态令牌与业务撤销需求之间成本最低的折中方案。
    """
    from app.core.exceptions import MongoDBException

    # 先补齐可能缺失的 token_version，保证 $inc 的递增基准正确
    # （否则递增结果可能仍为 1，导致旧令牌不被撤销）
    await ensure_token_version(username)

    collection = _collection()
    try:
        result = await collection.update_one(
            {"username": username},
            {
                "$inc": {"token_version": 1},
                "$set": {"updated_at": datetime.now()},
            },
        )
        logger.info("用户令牌已全部撤销 | username=%s", username)
        return result.modified_count > 0
    except Exception as exc:  # noqa: BLE001
        logger.error("令牌撤销失败 | username=%s | 异常=%s", username, exc)
        raise MongoDBException(message="登出失败", detail=str(exc)) from exc


async def update_last_login(
    username: str,
    login_ip: Optional[str] = None,
) -> bool:
    """
    仅更新最近登录信息（不动计数与锁定状态）

    参数：
        username: 用户名
        login_ip: 登录来源 IP

    返回：
        bool：更新成功返回 True

    说明：
        与 register_login_success 的区别：本方法用于"刷新令牌"场景，
        此时用户已经登录过，不应重复清零计数或改状态，
        只需留下最近活跃痕迹用于审计。
    """
    collection = _collection()
    now = datetime.now()
    set_fields: Dict[str, Any] = {"last_login_at": now, "updated_at": now}
    if login_ip:
        set_fields["last_login_ip"] = login_ip

    try:
        result = await collection.update_one({"username": username}, {"$set": set_fields})
        return result.modified_count > 0
    except Exception as exc:  # noqa: BLE001
        logger.error("最近登录信息更新失败 | username=%s | 异常=%s", username, exc)
        return False


# ======================================================================
# 四、索引初始化
# ======================================================================
async def ensure_indexes() -> None:
    """
    创建用户集合所需索引（幂等，可重复调用）

    返回：
        无返回值

    异常：
        无 —— 索引创建失败仅记录告警，不阻断应用启动

    说明：
        1. username 唯一索引：账号唯一性的最终保证，必须在应用启动时建立。
        2. role + status 复合索引：用户管理列表按角色/状态筛选的查询优化。
        3. 索引创建失败不抛异常，是为了"索引已存在但配置不同"等场景
           不至于让整个服务起不来；告警日志足以让运维发现并处理。
    """
    collection = _collection()
    try:
        await collection.create_index(
            [("username", ASCENDING)],
            unique=True,
            name="uk_username",
        )
        await collection.create_index(
            [("role", ASCENDING), ("status", ASCENDING)],
            name="idx_role_status",
        )
        logger.info("用户集合索引检查完成 | 集合=%s", MongoCollection.USER)
    except Exception as exc:  # noqa: BLE001
        logger.warning("用户集合索引创建失败（不阻断启动）| 异常=%s", exc)
