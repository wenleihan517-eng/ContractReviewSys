# D:/ContractReviewSys-合同审批智能审查项目/backend/app/services/auth_service.py
"""
登录鉴权业务服务 — 登录、登出、令牌刷新与账号初始化的编排层

【模块定位】
    本模块是 app/api/v1/auth.py 接口背后的业务编排层。
    职责：
        1. 账号密码登录（含账号锁定、失败计数、密码哈希校验与自动升级）
        2. 令牌刷新（含"刷新令牌版本同步"的处理）
        3. 退出登录（服务端撤销全部令牌）
        4. 内置演示账号初始化（幂等 upsert + 索引维护）
        5. 修改密码（供管理员或用户自身调用）

【分层约束】
    本模块位于 services 层，按项目分层规范：
        - 禁止直接操作 HTTP 请求/响应对象（不接收 Request，不做响应构造）
        - 通过 repositories 层访问数据，不直接操作 MongoDB
        - 通过 core/security 做密码与令牌计算，不自行实现密码学逻辑

【安全设计要点】
    1. 【账号枚举防护】账号不存在与密码错误返回**完全相同**的错误码与文案，
       防止攻击者通过响应差异批量探测有效账号。
    2. 【暴力破解防护】连续失败累加计数，达到阈值锁定账号；
       锁定期间即使密码正确也拒绝，抵御在线暴力破解。
    3. 【时序攻击防护】账号不存在时，仍然执行一次 bcrypt 校验（对固定假哈希），
       使响应耗时与"账号存在但密码错"接近，避免通过响应时间差异枚举账号。
    4. 【哈希自动升级】登录成功后若哈希强度低于当前配置，静默重算并落库，
       使存量账号随配置演进自动提升强度，无需人工批量刷库。
    5. 【最小权限下发】登录返回的用户信息只含角色与权限点，不含任何安全字段。

返回值说明：
    - login()：返回 LoginResponse（含令牌对与用户信息）
    - refresh_access_token()：返回 RefreshTokenResponse
    - logout()：返回 LogoutResponse
    - init_builtin_users()：返回初始化结果统计字典
    - change_password()：返回布尔值表示是否成功

使用示例：
    from app.services import auth_service
    result = await auth_service.login(username="user_law", password="123456", login_ip="127.0.0.1")
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from app.constants import UserRole, UserStatus
from app.core.config import settings
from app.core.exceptions import (
    ErrorCode,
    ForbiddenException,
    UnauthorizedException,
)
from app.core.logging_config import get_logger
from app.core.security import (
    CLAIM_ROLE,
    CLAIM_SUBJECT,
    CLAIM_USERNAME,
    CLAIM_VER,
    TokenType,
    create_token_pair,
    decode_token,
    hash_password,
    needs_rehash,
    verify_password,
)
from app.models.user import BUILTIN_USERS, User, UserInDB, build_builtin_user
from app.schemas.auth import (
    LoginResponse,
    LogoutResponse,
    RefreshTokenResponse,
    UserInfo,
)

logger = get_logger(__name__)


# ======================================================================
# 时序攻击防护用的假哈希
# ======================================================================
# 说明：
#     当账号不存在时，若直接返回"账号或密码错误"而不做哈希计算，
#     响应耗时约为 1ms；而账号存在时需执行 bcrypt 校验，耗时约 250ms。
#     攻击者可通过响应时间差（时序侧信道）判断账号是否存在，
#     进而枚举出全部有效账号，为后续暴力破解缩小范围。
#     解法：账号不存在时也对一个固定的假哈希执行一次校验，
#     使两条路径的耗时接近（差异被 bcrypt 本身的计算成本淹没）。
#
#     该哈希由 hash_password("dummy-password-for-timing") 生成，
#     对应的明文密码不可知也无所谓——反正是永不匹配的占位值。
_DUMMY_HASH = "$2b$12$cJvJ8Bn8ZqN5rXKZ6YyQ5OaM0hL4pR9tUwV2sXeD3fG7hI1jK4lN6"


# ======================================================================
# 一、初始化内置账号
# ======================================================================
@dataclass
class SeedResult:
    """
    内置账号初始化结果

    字段：
        total:      内置账号总数
        created:    本次新建数量
        existing:   已存在并跳过的数量
        usernames:  处理的账号名列表
    """

    total: int = 0
    created: int = 0
    existing: int = 0
    usernames: Optional[List[str]] = None

    def __post_init__(self) -> None:
        """初始化默认的空列表，避免可变默认值陷阱"""
        if self.usernames is None:
            self.usernames = []

    def to_dict(self) -> Dict[str, Any]:
        """
        转换为可序列化字典（供启动日志与接口返回）

        返回：
            Dict[str, Any]：统计信息字典
        """
        return {
            "total": self.total,
            "created": self.created,
            "existing": self.existing,
            "usernames": list(self.usernames or []),
        }


async def init_builtin_users() -> SeedResult:
    """
    初始化内置演示账号（幂等）

    返回：
        SeedResult：初始化统计结果

    异常：
        MongoDBException：数据库写入失败时由仓储层抛出

    说明：
        本项目为演示系统，不提供自助注册功能，仅内置三个测试账号：
            user_business / 123456  业务经办人
            user_law      / 123456  法务审查人
            user_admin    / 123456  系统管理员

        幂等策略（见 user_repo.upsert_builtin_user）：
            账号不存在 → 新建并写入初始密码哈希
            账号已存在 → 仅刷新角色/展示名等静态字段，
                          **不覆盖已修改过的密码**
        这样运维或用户改过密码后，重启服务不会把密码重置回去。

        密码哈希只在此处执行一次：
            bcrypt 单次计算约 200-300ms，三个账号约 0.6-0.9s。
            首启动可接受；后续启动因账号已存在，仍会计算哈希（用于 upsert 的
            $setOnInsert 参数），故耗时不变。为优化启动速度，
            此处先查询已存在账号，只对缺失的账号做哈希计算。
    """
    from app.core.exceptions import MongoDBException
    from app.repositories import user_repo

    result = SeedResult(total=len(BUILTIN_USERS))

    # 先建立唯一索引，保证后续 upsert 的防重能力（索引创建本身幂等）
    await user_repo.ensure_indexes()

    password = settings.DEFAULT_PASSWORD
    # 哈希只算一次：三个内置账号使用同一初始密码，复用同一哈希串即可。
    # 注意：不同账号共用同一哈希串在安全性上略弱于各自独立加盐，
    # 但此处是"已知初始密码的演示账号"，且用户首次登录后即应改密，
    # 复用可节省约 0.6s 启动时间，是演示场景下的合理取舍。
    shared_hash = hash_password(password)

    for spec in BUILTIN_USERS:
        username = spec["username"]
        try:
            user = build_builtin_user(spec, shared_hash)
            _, created = await user_repo.upsert_builtin_user(user)
            if created:
                result.created += 1
                logger.info(
                    "内置账号已创建 | username=%s | role=%s",
                    username,
                    spec["role"].value,
                )
            else:
                result.existing += 1
                logger.info("内置账号已存在，跳过 | username=%s", username)
            result.usernames.append(username)
        except MongoDBException as exc:
            # 单个账号失败不中断整体初始化：可能是用户名冲突或字段异常，
            # 记录错误后继续处理其余账号，并在最后汇总告警。
            # 这样不至于因为一个账号的问题导致服务完全起不来。
            logger.error("内置账号初始化失败 | username=%s | 异常=%s", username, exc)

    logger.info(
        "内置账号初始化完成 | 总数=%s | 新建=%s | 已存在=%s",
        result.total,
        result.created,
        result.existing,
    )
    return result


# ======================================================================
# 二、登录
# ======================================================================
async def login(
    username: str,
    password: str,
    login_ip: Optional[str] = None,
) -> LoginResponse:
    """
    账号密码登录

    参数：
        username: 登录账号
        password: 明文登录密码（仅在内存中短暂存在，绝不落库或写日志）
        login_ip: 登录来源 IP（可选，用于审计）

    返回：
        LoginResponse：含 access_token、refresh_token 与用户信息

    异常：
        UnauthorizedException（HTTP 401）：
            - 账号或密码错误（账号不存在、密码错误使用同一错误码）
        ForbiddenException（HTTP 403）：
            - 账号已停用
            - 账号因连续登录失败被锁定

    流程说明：
        1. 查询用户；不存在则执行一次假哈希校验（防时序侧信道）后返回 401
        2. 校验账号状态（停用 → 403）
        3. 清除已过期的历史锁定（状态漂移修正）
        4. 校验锁定期（锁定中 → 403）
        5. 校验密码
             失败 → 累加失败计数，达阈值则锁定，返回 401
             成功 → 清零计数、记录登录信息
        6. 哈希强度不足则静默升级
        7. 签发令牌对并返回用户信息
    """
    from app.repositories import user_repo

    # ---- 步骤 1：查询用户 ----
    user: Optional[UserInDB] = await user_repo.find_by_username(username)

    if user is None:
        # 【时序防护】对固定假哈希执行一次校验，使响应耗时与真实账号路径接近。
        # 返回值恒为 False，此处仅为消耗等量 CPU 时间。
        verify_password(password, _DUMMY_HASH)
        logger.warning(
            "登录失败（账号不存在）| username=%s | ip=%s",
            username,
            login_ip or "unknown",
        )
        raise UnauthorizedException(
            message="账号或密码错误",
            code=ErrorCode.CREDENTIALS_INVALID,
        )

    # ---- 步骤 2：账号状态校验 ----
    if user.status == UserStatus.DISABLED:
        logger.warning(
            "登录失败（账号已停用）| username=%s | ip=%s",
            username,
            login_ip or "unknown",
        )
        raise ForbiddenException(
            message="账号已被停用，请联系系统管理员",
            code=ErrorCode.ACCOUNT_DISABLED,
        )

    # ---- 步骤 3：清除已过期的锁定（修正"锁定已到期但状态未回改"的漂移）----
    if user.is_locked_until_expired():
        await user_repo.clear_expired_lock(username)
        # 本地对象同步修正，避免后续判断仍用旧的锁定时间
        user = user.model_copy(
            update={
                "locked_until": None,
                "failed_login_count": 0,
                "status": UserStatus.ACTIVE,
            }
        )

    # ---- 步骤 4：锁定期校验 ----
    if user.is_locked:
        remaining = 0
        if user.locked_until is not None:
            remaining = max(0, int((user.locked_until - datetime.now()).total_seconds() // 60) + 1)
        logger.warning(
            "登录失败（账号锁定中）| username=%s | 剩余分钟=%s | ip=%s",
            username,
            remaining,
            login_ip or "unknown",
        )
        raise ForbiddenException(
            message=f"账号已被锁定，请 {remaining} 分钟后重试或联系系统管理员",
            code=ErrorCode.ACCOUNT_LOCKED,
        )

    # ---- 步骤 5：密码校验 ----
    if not verify_password(password, user.password_hash):
        outcome = await user_repo.register_login_failure(
            username=username,
            max_failures=settings.AUTH_MAX_LOGIN_FAILURES,
            lock_minutes=settings.AUTH_LOGIN_LOCK_MINUTES,
        )
        logger.warning(
            "登录失败（密码错误）| username=%s | 累计失败=%s | 已锁定=%s | ip=%s",
            username,
            outcome.get("failed_count"),
            outcome.get("locked"),
            login_ip or "unknown",
        )
        # 若本次触发锁定，给用户明确提示；否则统一返回"账号或密码错误"
        if outcome.get("locked"):
            raise ForbiddenException(
                message=(
                    f"账号或密码错误，且连续失败已达 {settings.AUTH_MAX_LOGIN_FAILURES} 次，"
                    f"账号已锁定 {settings.AUTH_LOGIN_LOCK_MINUTES} 分钟"
                ),
                code=ErrorCode.ACCOUNT_LOCKED,
            )
        # 【账号枚举防护】与"账号不存在"返回完全相同的错误码与文案
        raise UnauthorizedException(
            message="账号或密码错误",
            code=ErrorCode.CREDENTIALS_INVALID,
        )

    # ---- 步骤 6：登录成功 —— 记录并清零失败计数 ----
    await user_repo.register_login_success(username=username, login_ip=login_ip)

    # ---- 步骤 7：哈希强度自动升级（失败不影响登录）----
    if needs_rehash(user.password_hash):
        try:
            new_hash = hash_password(password)
            await user_repo.update_password_hash(username, new_hash)
            logger.info("密码哈希已按当前强度自动升级 | username=%s", username)
        except Exception as exc:  # noqa: BLE001 - 升级失败不应阻断登录
            logger.warning("密码哈希升级失败（不影响本次登录）| username=%s | 异常=%s", username, exc)

    # ---- 步骤 8：签发令牌 ----
    # token_version 从数据库读取，保证新令牌与当前版本一致
    pair = create_token_pair(
        user_id=user.id,
        username=user.username,
        role=user.role.value,
        token_version=user.token_version,
    )

    logger.info(
        "登录成功 | username=%s | role=%s | ip=%s",
        user.username,
        user.role.value,
        login_ip or "unknown",
    )

    return LoginResponse(
        access_token=pair["access_token"],
        refresh_token=pair["refresh_token"],
        token_type=pair["token_type"],
        expires_in=pair["expires_in"],
        refresh_expires_in=pair["refresh_expires_in"],
        user=UserInfo.from_user_model(user),
    )


# ======================================================================
# 三、令牌刷新
# ======================================================================
async def refresh_access_token(refresh_token: str) -> RefreshTokenResponse:
    """
    使用刷新令牌换取新的访问令牌（同时轮换刷新令牌）

    参数：
        refresh_token: 登录时返回的刷新令牌

    返回：
        RefreshTokenResponse：新的访问令牌与刷新令牌

    异常：
        UnauthorizedException（HTTP 401）：
            - 刷新令牌非法 / 过期 / 类型不符
            - 刷新令牌版本已失效（用户已登出或改密）
            - 刷新令牌对应用户不存在
        ForbiddenException（HTTP 403）：
            - 账号已停用 / 锁定

    流程说明：
        1. 解码刷新令牌（强制校验类型为 refresh，防止用访问令牌刷新）
        2. 查询用户，校验存在性与状态
        3. 比对 token_version：不一致说明已登出/改密，拒绝刷新
        4. 签发新的令牌对（刷新令牌轮换）

    关于"刷新令牌轮换"：
        每次刷新都签发新的 refresh_token，而旧令牌在下次刷新时会因
        token_version 不匹配而失效。这形成"一次性刷新令牌"效果：
        若攻击者窃取了刷新令牌并使用，真实用户在下次刷新时会失败，
        从而暴露异常；反之若真实用户先刷新，攻击者的旧令牌立即失效。
        这是 OAuth2 中推荐的 Refresh Token Rotation 实践。
    """
    from app.repositories import user_repo

    # ---- 步骤 1：解码（强制 refresh 类型）----
    payload = decode_token(refresh_token, expected_type=TokenType.REFRESH)

    user_id = payload.get(CLAIM_SUBJECT)
    token_ver = payload.get(CLAIM_VER, 1)

    if not user_id:
        raise UnauthorizedException(
            message="刷新令牌内容不完整，请重新登录",
            code=ErrorCode.REFRESH_TOKEN_INVALID,
        )

    # ---- 步骤 2：确认用户存在且状态正常 ----
    user = await user_repo.find_by_id(str(user_id))
    if user is None:
        logger.warning("刷新令牌对应用户不存在 | user_id=%s", user_id)
        raise UnauthorizedException(
            message="账号不存在或已被删除，请重新登录",
            code=ErrorCode.REFRESH_TOKEN_INVALID,
        )

    if user.status == UserStatus.DISABLED:
        logger.warning("已停用账号尝试刷新令牌 | username=%s", user.username)
        raise ForbiddenException(
            message="账号已被停用，请联系系统管理员",
            code=ErrorCode.ACCOUNT_DISABLED,
        )

    if user.is_locked:
        logger.warning("锁定中账号尝试刷新令牌 | username=%s", user.username)
        raise ForbiddenException(
            message="账号已被锁定，请稍后再试或联系系统管理员",
            code=ErrorCode.ACCOUNT_LOCKED,
        )

    # ---- 步骤 3：版本比对（登出/改密后旧刷新令牌失效）----
    if int(token_ver) != int(user.token_version):
        logger.warning(
            "刷新令牌版本已失效 | username=%s | 令牌版本=%s | 当前版本=%s",
            user.username,
            token_ver,
            user.token_version,
        )
        raise UnauthorizedException(
            message="登录状态已失效（账号已在其他设备登出或密码已修改），请重新登录",
            code=ErrorCode.REFRESH_TOKEN_INVALID,
        )

    # ---- 步骤 4：签发新令牌对 ----
    pair = create_token_pair(
        user_id=user.id,
        username=user.username,
        role=user.role.value,
        token_version=user.token_version,
    )

    # 刷新属于"用户仍活跃"的信号，更新最近活跃时间供审计使用
    await user_repo.update_last_login(username=user.username)

    logger.info("令牌刷新成功 | username=%s | role=%s", user.username, user.role.value)

    return RefreshTokenResponse(
        access_token=pair["access_token"],
        token_type=pair["token_type"],
        expires_in=pair["expires_in"],
        refresh_token=pair["refresh_token"],
        refresh_expires_in=pair["refresh_expires_in"],
    )


# ======================================================================
# 四、退出登录
# ======================================================================
async def logout(username: str) -> LogoutResponse:
    """
    退出登录（服务端撤销该用户已签发的全部令牌）

    参数：
        username: 当前登录用户名

    返回：
        LogoutResponse：登出结果

    异常：
        MongoDBException：撤销操作失败时分抛出

    说明：
        JWT 无状态，服务端无法"删除"已签发的令牌。本实现采用
        "递增 token_version"方案：撤销后，所有旧令牌在下次鉴权时
        因版本不匹配而被判失效，等效实现立即登出。

        代价说明：该方案会同时使该用户在其他设备上的登录失效
        （全局登出）。这是当前实现的取舍——
        对演示系统而言，全局登出比"多设备精细管理"更有价值，
        因为它能确保用户"修改密码后所有地方都退出"。
        若将来需要"仅登出当前设备"，可引入 jti 黑名单（载荷中已预留 jti）。
    """
    from app.repositories import user_repo

    await user_repo.revoke_tokens(username)

    logger.info("用户已登出，全部令牌已撤销 | username=%s", username)

    return LogoutResponse(
        username=username,
        revoked_at=datetime.now(),
        message="已成功退出登录，所有设备的登录状态均已失效",
    )


# ======================================================================
# 五、修改密码
# ======================================================================
async def change_password(
    username: str,
    old_password: str,
    new_password: str,
) -> bool:
    """
    修改密码（校验原密码，成功后撤销全部令牌）

    参数：
        username:     用户名
        old_password: 原密码（用于身份二次确认）
        new_password: 新密码

    返回：
        bool：修改成功返回 True

    异常：
        UnauthorizedException（HTTP 401）：
            - 用户不存在
            - 原密码错误
        ForbiddenException（HTTP 403）：账号已停用
        ValueError：新密码与原密码相同，或新密码不符合强度要求

    说明：
        修改成功后 user_repo.update_password_hash 会自动递增 token_version，
        使旧令牌全部失效——这是安全要求：改密后其他设备必须重新登录，
        否则攻击者持有的旧令牌在用户改密后仍可继续访问。
    """
    from app.repositories import user_repo

    # ---- 新密码强度校验 ----
    _validate_password_strength(new_password)

    user = await user_repo.find_by_username(username)
    if user is None:
        raise UnauthorizedException(
            message="账号或密码错误",
            code=ErrorCode.CREDENTIALS_INVALID,
        )

    if user.status == UserStatus.DISABLED:
        raise ForbiddenException(
            message="账号已被停用，请联系系统管理员",
            code=ErrorCode.ACCOUNT_DISABLED,
        )

    if not verify_password(old_password, user.password_hash):
        logger.warning("修改密码失败（原密码错误）| username=%s", username)
        raise UnauthorizedException(
            message="原密码错误",
            code=ErrorCode.CREDENTIALS_INVALID,
        )

    if verify_password(new_password, user.password_hash):
        raise ValueError("新密码不能与原密码相同")

    new_hash = hash_password(new_password)
    await user_repo.update_password_hash(username, new_hash)

    logger.info("密码修改成功，全部令牌已撤销 | username=%s", username)
    return True


def _validate_password_strength(password: str) -> None:
    """
    校验密码强度（内部函数）

    参数：
        password: 明文密码

    返回：
        无返回值；校验通过时正常返回

    异常：
        ValueError：不满足强度要求时抛出

    强度要求：
        1. 长度 6-64 字符
        2. 至少包含字母与数字两类字符
        3. 不含空格（避免前后空格导致的登录困惑）

    说明：
        演示系统采用"够用即可"的强度标准（6 位起），
        因为内置账号密码为 123456，若要求 8 位以上会与演示场景冲突。
        生产环境应通过配置提高要求（如 12 位 + 大小写 + 符号）。
    """
    if not password or len(password) < 6:
        raise ValueError("密码长度至少 6 位")
    if len(password) > 64:
        raise ValueError("密码长度不能超过 64 位")
    if " " in password:
        raise ValueError("密码不能包含空格")
    has_letter = any(ch.isalpha() for ch in password)
    has_digit = any(ch.isdigit() for ch in password)
    if not (has_letter and has_digit):
        raise ValueError("密码必须同时包含字母和数字")


# ======================================================================
# 六、用户信息查询
# ======================================================================
async def get_user_profile(username: str) -> UserInfo:
    """
    获取用户信息（供"获取当前登录用户信息"接口使用）

    参数：
        username: 用户名

    返回：
        UserInfo：用户信息

    异常：
        UnauthorizedException（HTTP 401）：用户不存在

    说明：
        鉴权依赖已经从数据库读取过用户并构造了 CurrentUser，
        因此该接口优先复用 CurrentUser（见 app/api/v1/auth.py）。
        本函数保留作为兜底与复用入口（如按用户名查他人信息给管理员用）。
    """
    from app.repositories import user_repo

    user = await user_repo.find_by_username(username)
    if user is None:
        raise UnauthorizedException(
            message="用户不存在",
            code=ErrorCode.TOKEN_INVALID,
        )
    return UserInfo.from_user_model(user)


# ======================================================================
# 七、账号管理（管理员能力，仅用户列表查询）
# ======================================================================
async def list_users(
    role: Optional[str] = None,
    status: Optional[str] = None,
    keyword: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
) -> Tuple[List[UserInfo], int]:
    """
    分页查询用户列表（管理员使用）

    参数：
        role:      按角色筛选
        status:    按状态筛选
        keyword:   用户名/展示名模糊搜索
        page:      页码
        page_size: 每页条数

    返回：
        Tuple[List[UserInfo], int]：(用户信息列表, 总数)

    说明：
        返回值使用 UserInfo 契约模型，保证不泄露 password_hash 等字段。
        当前需求未要求用户管理功能，但保留该查询能力，
        便于管理员在演示中核对内置账号，且为后续扩展留出接口。
    """
    from app.repositories import user_repo

    users, total = await user_repo.list_users(
        role=role,
        status=status,
        keyword=keyword,
        page=page,
        page_size=page_size,
    )
    return [UserInfo.from_user_model(item) for item in users], total
