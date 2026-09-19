# D:/ContractReviewSys-合同审批智能审查项目/backend/app/core/dependencies.py
"""
依赖注入中心 — FastAPI Depends 统一的依赖提供者

本模块职责：
    1. 提供分页参数的统一解析依赖
    2. 提供请求链路 ID 的依赖
    3. 提供各存储客户端（MongoDB / MinIO / Milvus）的依赖函数
    4. 提供 LLM / Embedding 客户端的依赖函数
    5. 提供登录鉴权依赖（当前用户解析）与角色/权限校验依赖

设计要点：
    - 所有依赖均为"获取已初始化单例"，不在请求期重复建立连接
    - 连接初始化在应用 lifespan 阶段完成，此处只做取用与健康校验
    - 依赖函数保持同步/异步语义清晰，避免阻塞事件循环
    - 【鉴权设计】鉴权依赖采用"依赖注入"而非"中间件"作为主判定手段：
        中间件只能决定放行/拦截，无法把用户身份传递给接口函数；
        依赖注入既能在解析失败时抛出 401/403，又能把 CurrentUser 注入接口。
        中间件作为兜底防线存在（见 app/core/auth_middleware.py）。

返回值说明：
    - 各依赖函数返回对应客户端实例或参数对象
    - 鉴权依赖返回 CurrentUser 实例

使用示例：
    from app.core.dependencies import get_mongo_db, PaginationParams, CurrentUser, require_roles
    @router.get("/list")
    async def list_items(
        page: PaginationParams = Depends(),
        db = Depends(get_mongo_db),
        user: CurrentUser = Depends(get_current_user),
    ):
        ...
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, List, Optional, Tuple

from fastapi import Depends, Query, Request

from app.constants import (
    ALL_PERMISSIONS,
    PERM_TASK_VIEW_ALL,
    ROLE_PERMISSIONS,
    UserRole,
    UserStatus,
    get_permissions_for_role,
    role_has_permission,
)
from app.core.config import settings
from app.constants import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from app.core.exceptions import (
    ConfigException,
    ErrorCode,
    ForbiddenException,
    MongoDBException,
    UnauthorizedException,
)
from app.core.logging_config import get_logger
from app.core.middleware import get_request_id
from app.core.security import (
    CLAIM_ROLE,
    CLAIM_SUBJECT,
    CLAIM_USERNAME,
    CLAIM_VER,
    TokenType,
    build_token_fingerprint,
    decode_token,
    extract_bearer_token,
)

logger = get_logger(__name__)


# ======================================================================
# 一、请求上下文类依赖
# ======================================================================
class PaginationParams:
    """
    分页参数依赖对象

    参数：
        page: 当前页码，从 1 开始
        page_size: 每页条数，受 MAX_PAGE_SIZE 限制
        sort_field: 排序字段名
        sort_order: 排序方向，-1 降序 / 1 升序

    使用方式：
        通过 Depends() 自动从查询参数解析，无需手动构造
    """

    def __init__(
        self,
        page: int = Query(default=1, ge=1, description="页码，从 1 开始"),
        page_size: int = Query(
            default=DEFAULT_PAGE_SIZE,
            ge=1,
            le=MAX_PAGE_SIZE,
            description=f"每页条数，最大 {MAX_PAGE_SIZE}",
        ),
        sort_field: str = Query(default="created_at", description="排序字段"),
        sort_order: int = Query(default=-1, ge=-1, le=1, description="排序方向：-1 降序，1 升序"),
    ) -> None:
        self.page = page
        self.page_size = page_size
        self.sort_field = sort_field
        self.sort_order = sort_order

    @property
    def skip(self) -> int:
        """
        计算 MongoDB 查询跳过条数

        返回：
            (page - 1) * page_size，用于 skip()
        """
        return (self.page - 1) * self.page_size

    @property
    def sort_tuples(self) -> list:
        """
        构造 MongoDB 排序参数

        返回：
            形如 [("created_at", -1)] 的排序列表，可直接传给 find().sort()
        """
        return [(self.sort_field, self.sort_order)]


def get_current_request_id() -> Optional[str]:
    """
    获取当前请求链路 ID 的依赖函数

    返回：
        当前请求的 request_id，不在请求上下文时返回 None
    """
    return get_request_id()


# ======================================================================
# 二、存储客户端依赖
# ======================================================================
async def get_mongo_db() -> Any:
    """
    提供 MongoDB 数据库对象的依赖

    说明：
        从应用状态中取出 lifespan 阶段初始化好的 AsyncIOMotorDatabase，
        不在请求期新建连接，避免连接风暴。

    返回：
        AsyncIOMotorDatabase 实例

    异常：
        ConfigException：应用尚未完成初始化时抛出
    """
    # 延迟导入，避免模块循环依赖
    from app.storage.mongo_client import mongo_client

    client = mongo_client.get_client()
    if client is None:
        raise ConfigException(message="MongoDB 尚未初始化，请检查应用启动流程")
    return client[settings.MONGO_DB_NAME]


async def get_minio_client() -> Any:
    """
    提供 MinIO 客户端对象的依赖

    返回：
        Minio 客户端实例

    异常：
        ConfigException：应用尚未完成初始化时抛出
    """
    from app.storage.minio_client import minio_client

    client = minio_client.get_client()
    if client is None:
        raise ConfigException(message="MinIO 尚未初始化，请检查应用启动流程")
    return client


async def get_milvus_client() -> Any:
    """
    提供 Milvus 客户端对象的依赖

    返回：
        MilvusClient 实例（已连接至本项目独立 database）

    异常：
        ConfigException：应用尚未完成初始化时抛出
    """
    from app.storage.milvus_client import milvus_manager

    client = milvus_manager.get_client()
    if client is None:
        raise ConfigException(message="Milvus 尚未初始化，请检查应用启动流程")
    return client


# ======================================================================
# 三、模型服务依赖
# ======================================================================
def get_llm_client() -> Any:
    """
    提供本地大模型客户端（OpenAI 兼容协议）的依赖

    返回：
        LLMClient 实例，供条款解析、风险候选识别、建议初稿等辅助能力调用
    """
    from app.engines.llm_client import llm_client

    return llm_client


def get_embedding_client() -> Any:
    """
    提供 BGE-M3 向量化客户端的依赖

    返回：
        EmbeddingClient 实例
    """
    from app.engines.embedding_client import embedding_client

    return embedding_client


# ======================================================================
# 四、规则引擎依赖（架构红线：最终判定唯一入口）
# ======================================================================
def get_rule_engine() -> Any:
    """
    提供规则引擎实例的依赖

    说明：
        规则引擎是所有正式业务判定的唯一裁决入口，
        路由与工作流中凡涉及定级 / 阻塞 / 结论，必须通过本依赖获取实例，
        禁止绕过规则引擎直接写入判定结果。

    返回：
        RuleEngine 实例

    异常：
        ConfigException：规则引擎开关被关闭时抛出，防止出现未判定的放行
    """
    from app.engines.rule_engine import rule_engine

    if not settings.RULE_ENGINE_ENABLED:
        raise ConfigException(
            message="规则引擎已被关闭，禁止执行审查流程。请检查 RULE_ENGINE_ENABLED 配置"
        )
    return rule_engine


# ======================================================================
# 五、登录鉴权依赖
#
# 本节是接口层鉴权的唯一入口。设计取舍说明：
#
#   1. 为什么用依赖注入而不是纯中间件？
#      中间件在路由匹配之前执行，能拦截请求，但无法把"当前用户"这一
#      上下文传递给接口函数。若只靠中间件，每个接口都要重新解析一次令牌，
#      既重复解析（性能损耗）又拿不到统一的用户对象。
#      依赖注入方案：首次解析 → 缓存进 request.state → 同请求内后续接口/依赖
#      直接复用，既保证 401/403 语义准确，又能把 CurrentUser 注入业务代码。
#
#   2. 为什么保留中间件？
#      依赖注入是"显式声明"的，一旦新增接口忘了加 Depends(get_current_user)，
#      该接口就变成公开接口（漏保护）。中间件是"默认拒绝"的兜底网，
#      两者互补：中间件保证不漏，依赖注入提供上下文与细粒度权限。
#      中间件实现见 app/core/auth_middleware.py。
#
#   3. 令牌撤销如何实现？
#      JWT 无状态，签发即不可回收。本方案在载荷中携带 token_version，
#      鉴权时与数据库中的用户 token_version 比对，不一致即视为失效。
#      代价是每次鉴权一次轻量主键查询（有 _id 索引，成本极低），
#      换来"登出/改密立即生效"的能力。这是安全性与性能的合理折中。
# ======================================================================


@dataclass
class CurrentUser:
    """
    当前登录用户上下文对象

    说明：
        这是鉴权链路对外暴露的唯一用户载体，由 get_current_user 构造。
        它只包含接口层需要的身份与权限信息，不含密码哈希等敏感字段，
        因此可以安全地被 service 层使用，但不应直接作为响应体返回
        （响应体请使用 app/schemas/auth.py 的契约模型）。

    字段：
        user_id:      用户 id
        username:     用户名
        display_name: 展示名称
        role:         角色标识（business_user / law_user / admin）
        role_label:   角色中文名
        status:       账号状态
        department:   所属部门
        email:        邮箱
        permissions:  该角色拥有的权限点集合
        token_type:   本次鉴权使用的令牌类型
        token_ver:    令牌版本号
        token_jti:    令牌唯一 id，便于日志追踪
        token_exp:    令牌过期时间（UTC 时间戳，秒）
        login_ip:     请求来源 IP
    """

    user_id: str
    username: str
    display_name: str
    role: str
    role_label: str
    status: str
    department: Optional[str] = None
    email: Optional[str] = None
    permissions: Tuple[str, ...] = field(default_factory=tuple)
    token_type: str = TokenType.ACCESS.value
    token_ver: int = 1
    token_jti: Optional[str] = None
    token_exp: Optional[int] = None
    login_ip: Optional[str] = None

    # ---------------- 权限判定方法 ----------------
    def has_permission(self, permission: str) -> bool:
        """
        判断当前用户是否拥有指定权限点

        参数：
            permission: 权限点字符串，取自 app.constants 的 PERM_* 常量

        返回：
            bool：拥有返回 True
        """
        return permission in self.permissions

    def has_any_permission(self, permissions: List[str]) -> bool:
        """
        判断当前用户是否拥有给定权限点中的任意一个（或关系）

        参数：
            permissions: 权限点列表

        返回：
            bool：任一命中即返回 True

        使用示例：
            # 能看全部任务 或 能看自己任务，二者其一即可访问列表接口
            if user.has_any_permission([PERM_TASK_VIEW_ALL, PERM_TASK_VIEW_OWN]):
                ...
        """
        return any(item in self.permissions for item in permissions)

    def has_all_permissions(self, permissions: List[str]) -> bool:
        """
        判断当前用户是否同时拥有全部给定权限点（与关系）

        参数：
            permissions: 权限点列表

        返回：
            bool：全部命中返回 True

        使用示例：
            # 必须同时能编辑风险项、能批注，才允许提交审查意见
            if user.has_all_permissions([PERM_RISK_ITEM_EDIT, PERM_COMMENT_ANNOTATE]):
                ...
        """
        return all(item in self.permissions for item in permissions)

    def is_role(self, *roles: str) -> bool:
        """
        判断当前用户角色是否属于给定角色之一

        参数：
            roles: 角色标识可变参数

        返回：
            bool：属于其中之一返回 True

        使用示例：
            if user.is_role(UserRole.ADMIN.value, UserRole.LAW_USER.value):
                ...
        """
        return self.role in roles

    @property
    def is_admin(self) -> bool:
        """
        判断是否为系统管理员

        返回：
            bool：角色为 admin 时返回 True
        """
        return self.role == UserRole.ADMIN.value

    @property
    def can_view_all_tasks(self) -> bool:
        """
        判断是否可查看全部审查任务

        返回：
            bool：拥有 task:view_all 权限返回 True

        说明：
            业务经办人只能看自己的任务，此处返回 False；
            接口层据此在查询条件中追加 created_by 过滤（见 task_service）。
        """
        return self.has_permission(PERM_TASK_VIEW_ALL)

    def to_public_dict(self) -> dict:
        """
        转换为可安全返回给前端的用户信息字典

        返回：
            dict：不含任何令牌与凭证字段的用户信息

        说明：
            字段命名与 app/schemas/auth.py 的 UserInfo 保持一致，
            前端登录后保存该结构用于界面展示与路由鉴权。
        """
        return {
            "user_id": self.user_id,
            "username": self.username,
            "display_name": self.display_name,
            "role": self.role,
            "role_label": self.role_label,
            "status": self.status,
            "department": self.department,
            "email": self.email,
            "permissions": list(self.permissions),
            "is_admin": self.is_admin,
            "can_view_all_tasks": self.can_view_all_tasks,
        }


def _get_client_ip(request: Request) -> Optional[str]:
    """
    从请求中解析客户端真实 IP

    参数：
        request: FastAPI 请求对象

    返回：
        Optional[str]：客户端 IP；无法解析时返回 None

    说明：
        优先读取 X-Forwarded-For 的首个地址（经反向代理时的真实客户端），
        回退到 X-Real-IP，最后使用直连地址。
        注意：X-Forwarded-For 可被客户端伪造，因此其结果只用于审计展示，
        绝不可作为任何安全判定的依据。
    """
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        # X-Forwarded-For 格式：client, proxy1, proxy2 —— 取第一个为原始客户端
        first = forwarded.split(",")[0].strip()
        if first:
            return first[:64]
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip.strip()[:64]
    if request.client and request.client.host:
        return request.client.host[:64]
    return None


async def get_current_user(request: Request) -> CurrentUser:
    """
    【核心鉴权依赖】解析请求中的访问令牌并返回当前登录用户

    参数：
        request: FastAPI 请求对象（由框架自动注入）

    返回：
        CurrentUser：当前登录用户上下文

    异常：
        UnauthorizedException（HTTP 401）：
            - 未携带令牌 / 令牌格式非法 / 签名失败 / 已过期 / 类型不符
            - 令牌对应用户不存在（账号已被删除）
        ForbiddenException（HTTP 403）：
            - 账号已停用（status=disabled）
            - 账号处于锁定期内
        ConfigException（HTTP 500）：
            - 鉴权已启用但未配置密钥等内部配置问题

    使用示例：
        @router.get("/me")
        async def me(user: CurrentUser = Depends(get_current_user)):
            return success_response(user.to_public_dict())

    流程说明：
        1. 读请求头 → 提取 Bearer 令牌
        2. 解码校验（签名 / 算法 / 签发方 / 有效期 / 令牌类型）
        3. 校验必需声明齐全
        4. 查询数据库确认用户仍存在且状态正常
        5. 比对 token_version，实现"登出/改密后旧令牌立即失效"
        6. 组装 CurrentUser 并缓存到 request.state，供同一请求内复用
    """
    # 鉴权开关：关闭时返回一个"系统内置超级用户"上下文，
    # 便于本地联调时不带令牌直接调试业务接口。
    # 【安全提示】该分支仅在 AUTH_ENABLED=False 时生效，生产环境必须为 True。
    if not settings.AUTH_ENABLED:
        logger.warning(
            "鉴权已关闭，接口以匿名超级用户身份执行 | 路径=%s", request.url.path
        )
        return _build_anonymous_admin(request)

    # ---- 同一请求内复用已解析结果，避免重复解码与重复查库 ----
    cached = getattr(request.state, "current_user", None)
    if isinstance(cached, CurrentUser):
        return cached

    # ---- 步骤 1：提取令牌 ----
    header_value = request.headers.get(settings.AUTH_TOKEN_HEADER_NAME)
    token = extract_bearer_token(header_value)
    fingerprint = build_token_fingerprint(token)

    # ---- 步骤 2：解码与校验（内部抛 401）----
    payload = decode_token(token, expected_type=TokenType.ACCESS)

    # ---- 步骤 3：校验必需声明 ----
    user_id = payload.get(CLAIM_SUBJECT)
    username = payload.get(CLAIM_USERNAME)
    role = payload.get(CLAIM_ROLE)
    token_ver = payload.get(CLAIM_VER, 1)

    missing = [
        name
        for name, value in (
            (CLAIM_SUBJECT, user_id),
            (CLAIM_USERNAME, username),
            (CLAIM_ROLE, role),
        )
        if not value
    ]
    if missing:
        logger.warning(
            "令牌声明缺失 | 缺失项=%s | 指纹=%s | 路径=%s",
            missing,
            fingerprint,
            request.url.path,
        )
        raise UnauthorizedException(
            message="访问凭证内容不完整，请重新登录",
            code=ErrorCode.TOKEN_INVALID,
            detail=f"缺少声明：{missing}",
        )

    # ---- 步骤 4：确认用户存在且状态正常 ----
    from app.repositories import user_repo

    user = await user_repo.find_by_id(str(user_id))
    if user is None:
        logger.warning(
            "令牌对应用户不存在 | user_id=%s | 指纹=%s | 路径=%s",
            user_id,
            fingerprint,
            request.url.path,
        )
        # 注意：此处返回 401 而非 404，避免通过状态码差异探测用户是否存在
        raise UnauthorizedException(
            message="账号不存在或已被删除，请重新登录",
            code=ErrorCode.TOKEN_INVALID,
        )

    if user.status == UserStatus.DISABLED:
        logger.warning("已停用账号尝试访问 | username=%s | 路径=%s", user.username, request.url.path)
        raise ForbiddenException(
            message="账号已被停用，请联系系统管理员",
            code=ErrorCode.ACCOUNT_DISABLED,
        )

    if user.is_locked:
        logger.warning(
            "锁定中账号尝试访问 | username=%s | 解锁时间=%s",
            user.username,
            user.locked_until.isoformat() if user.locked_until else None,
        )
        raise ForbiddenException(
            message="账号已被锁定，请稍后再试或联系系统管理员",
            code=ErrorCode.ACCOUNT_LOCKED,
        )

    # ---- 步骤 5：令牌版本比对（实现无状态令牌的撤销能力）----
    if int(token_ver) != int(user.token_version):
        logger.warning(
            "令牌版本已失效 | username=%s | 令牌版本=%s | 当前版本=%s | 路径=%s",
            user.username,
            token_ver,
            user.token_version,
            request.url.path,
        )
        raise UnauthorizedException(
            message="登录状态已失效（可能在其他设备登出或已修改密码），请重新登录",
            code=ErrorCode.TOKEN_EXPIRED,
        )

    # ---- 步骤 6：组装上下文并缓存 ----
    current_user = CurrentUser(
        user_id=user.id,
        username=user.username,
        display_name=user.display_name,
        role=user.role.value,
        role_label=user.role_label,
        status=user.status.value,
        department=user.department,
        email=user.email,
        # 权限点实时依据角色计算，保证权限矩阵调整后立即生效，
        # 无需等令牌过期——这也是不把权限点写进 JWT 的原因
        permissions=get_permissions_for_role(user.role.value),
        token_type=TokenType.ACCESS.value,
        token_ver=int(user.token_version),
        token_jti=payload.get("jti"),
        token_exp=payload.get("exp"),
        login_ip=_get_client_ip(request),
    )
    request.state.current_user = current_user

    logger.debug(
        "鉴权通过 | username=%s | role=%s | 路径=%s | 指纹=%s",
        current_user.username,
        current_user.role,
        request.url.path,
        fingerprint,
    )
    return current_user


def _build_anonymous_admin(request: Request) -> CurrentUser:
    """
    构造"匿名超级用户"上下文（仅在 AUTH_ENABLED=False 时使用）

    参数：
        request: FastAPI 请求对象

    返回：
        CurrentUser：拥有全部权限的虚拟用户

    说明：
        该方法存在的唯一目的是支持本地关闭鉴权调试。
        为保证关闭鉴权时业务接口的行为与管理员一致（不因缺字段报错），
        这里给出全部权限。生产环境 AUTH_ENABLED 恒为 True，此分支不可达。
    """
    return CurrentUser(
        user_id="anonymous",
        username="anonymous",
        display_name="匿名调试用户（鉴权已关闭）",
        role=UserRole.ADMIN.value,
        role_label="系统管理员",
        status=UserStatus.ACTIVE.value,
        department=None,
        email=None,
        permissions=ALL_PERMISSIONS,
        token_type=TokenType.ACCESS.value,
        token_ver=1,
        login_ip=_get_client_ip(request),
    )


async def get_current_user_optional(request: Request) -> Optional[CurrentUser]:
    """
    可选鉴权依赖 — 有令牌则解析，无令牌返回 None

    参数：
        request: FastAPI 请求对象

    返回：
        Optional[CurrentUser]：解析成功返回用户，未携带令牌返回 None；
                              令牌非法/过期仍然抛 401（不静默放过）

    异常：
        UnauthorizedException：携带了令牌但令牌非法 / 过期时抛出
        ForbiddenException：账号停用 / 锁定

    说明：
        适用场景：接口对"未登录"与"已登录"返回不同内容，但不强制登录。
        注意：区分两种"没有用户"的情形——
            未携带令牌 → 返回 None（匿名访问）
            携带了非法令牌 → 抛 401（凭证有问题必须告知，不能假装匿名）
        若把非法令牌也当匿名处理，用户会看到"登录了但内容不对"的诡异现象。
    """
    header_value = request.headers.get(settings.AUTH_TOKEN_HEADER_NAME)
    if not header_value or not header_value.strip():
        return None
    return await get_current_user(request)


# ----------------------------------------------------------------------
# 角色与权限校验依赖工厂
# ----------------------------------------------------------------------
def require_roles(*roles: str) -> Callable[..., Awaitable[CurrentUser]]:
    """
    【角色校验依赖工厂】要求当前用户角色属于给定集合之一

    参数：
        roles: 允许访问的角色标识（可变参数）

    返回：
        Callable：可直接用于 Depends 的异步依赖函数

    使用示例：
        # 仅管理员可访问
        @router.get("/admin-only", dependencies=[Depends(require_roles(UserRole.ADMIN.value))])
        async def admin_only(): ...

        # 管理员与法务可访问，且需要拿到用户对象
        @router.post("/review")
        async def review(user: CurrentUser = Depends(require_roles(UserRole.ADMIN.value, UserRole.LAW_USER.value))):
            ...

    说明：
        判定逻辑走 require_roles 而非直接比较角色名，
        好处是权限矩阵调整时只需改一处配置，不必全项目搜索角色字面量。
        角色合法性与集合非空在工厂调用时（模块加载期）即校验，
        配置写错会在启动时报错而不是运行时静默放行/拦截。
    """
    allowed = tuple(roles)
    if not allowed:
        raise ConfigException(
            message="require_roles 至少需要指定一个角色，禁止使用空角色集合（会导致全部拒绝）"
        )

    valid_roles = {item.value for item in UserRole}
    unknown = [item for item in allowed if item not in valid_roles]
    if unknown:
        raise ConfigException(
            message=f"require_roles 收到未知角色 {unknown}，合法值为 {sorted(valid_roles)}"
        )

    async def _checker(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        """
        实际执行角色校验的内部依赖

        参数：
            user: 当前登录用户（经 get_current_user 解析）

        返回：
            CurrentUser：校验通过的用户对象

        异常：
            ForbiddenException（HTTP 403）：角色不在允许集合内
        """
        if not user.is_role(*allowed):
            logger.warning(
                "角色校验未通过 | username=%s | 当前角色=%s | 允许角色=%s",
                user.username,
                user.role,
                list(allowed),
            )
            raise ForbiddenException(
                message=f"当前角色（{user.role_label}）无权访问该接口",
                code=ErrorCode.PERMISSION_DENIED,
                detail=f"需要角色之一：{list(allowed)}，当前角色：{user.role}",
            )
        return user

    return _checker


def require_permissions(*permissions: str, require_all: bool = False) -> Callable[..., Awaitable[CurrentUser]]:
    """
    【权限校验依赖工厂】要求当前用户拥有给定权限点

    参数：
        permissions: 权限点（可变参数），取自 app.constants 的 PERM_* 常量
        require_all: False（默认）表示"任一命中即放行"（或关系）；
                     True 表示"必须全部拥有"（与关系）

    返回：
        Callable：可直接用于 Depends 的异步依赖函数

    使用示例：
        # 上传合同（业务经办人）
        @router.post("/upload", dependencies=[Depends(require_permissions(PERM_CONTRACT_UPLOAD))])
        async def upload(): ...

        # 编辑风险项（法务）
        @router.put("/risk-items/{id}", dependencies=[Depends(require_permissions(PERM_RISK_ITEM_EDIT))])
        async def edit_risk_item(): ...

    说明：
        与 require_roles 的关系：require_roles 是粗粒度（按角色名），
        require_permissions 是细粒度（按能力）。业务接口优先使用后者，
        因为它描述的是"这个接口需要什么能力"而非"什么角色能用"，
        新增角色时无需改动任何接口代码。
    """
    if not permissions:
        raise ConfigException(
            message="require_permissions 至少需要指定一个权限点，禁止使用空权限集合"
        )

    unknown = [item for item in permissions if item not in ALL_PERMISSIONS]
    if unknown:
        raise ConfigException(
            message=(
                f"require_permissions 收到未定义的权限点 {unknown}。"
                "请在 app/constants 的 PERM_* 常量中登记后再使用，"
                "禁止使用未定义权限点（会导致接口永久拒绝访问）"
            )
        )

    required = tuple(permissions)
    use_all = require_all

    async def _checker(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        """
        实际执行权限校验的内部依赖

        参数：
            user: 当前登录用户

        返回：
            CurrentUser：校验通过的用户对象

        异常：
            ForbiddenException（HTTP 403）：权限不足
        """
        passed = (
            user.has_all_permissions(list(required))
            if use_all
            else user.has_any_permission(list(required))
        )
        if not passed:
            logger.warning(
                "权限校验未通过 | username=%s | 角色=%s | 需要=%s | 关系=%s",
                user.username,
                user.role,
                list(required),
                "全部" if use_all else "任一",
            )
            raise ForbiddenException(
                message=f"当前角色（{user.role_label}）无权执行该操作",
                code=ErrorCode.PERMISSION_DENIED,
                detail=(
                    f"需要权限{'全部' if use_all else '之一'}：{list(required)}；"
                    f"当前角色 {user.role} 拥有：{list(user.permissions)}"
                ),
            )
        return user

    return _checker


def require_admin() -> Callable[..., Awaitable[CurrentUser]]:
    """
    【管理员专用依赖】要求当前用户为系统管理员

    返回：
        Callable：可直接用于 Depends 的异步依赖函数

    使用示例：
        @router.get("/users", dependencies=[Depends(require_admin())])
        async def list_users(): ...

    说明：
        这是 require_roles(admin) 的语义化快捷方式。
        单列出它是因为"管理员专属"是本系统最高频也最关键的权限判断
        （用户管理、规则维护、阻塞处理、系统监控），
        用 require_admin() 表达比 require_roles(UserRole.ADMIN.value) 更不易写错。
    """
    return require_roles(UserRole.ADMIN.value)


def assert_resource_owner(
    user: CurrentUser,
    resource_owner_id: Optional[str],
) -> None:
    """
    校验资源归属 — 数据级权限过滤（非管理员只能操作自己的资源）

    参数：
        user:               当前登录用户
        resource_owner_id:  资源所属用户 id

    返回：
        无返回值；校验通过时正常返回

    异常：
        ForbiddenException（HTTP 403）：非管理员且资源不属于自己时抛出

    使用示例：
        task = await task_service.get_task(task_no)
        assert_resource_owner(user, task.created_by)
        # 通过后才会继续执行查看/下载逻辑

    说明：
        【重要】这是"水平越权"的防护点。
        角色权限（require_permissions）只回答"能不能用这个功能"，
        不回答"能不能碰这条数据"。业务经办人拥有"查看自己的任务"权限，
        但这不代表他可以查看别人的任务——
        因此凡涉及具体资源 id 的接口，都必须调用本函数做归属校验。
        管理员天然豁免（拥有 task:view_all 权限者视为可跨用户访问）。
    """
    if user.can_view_all_tasks:
        return

    if resource_owner_id is None or str(resource_owner_id) != str(user.user_id):
        logger.warning(
            "资源归属校验未通过（疑似水平越权）| username=%s | 用户id=%s | 资源归属=%s",
            user.username,
            user.user_id,
            resource_owner_id,
        )
        raise ForbiddenException(
            message="无权访问他人提交的资源",
            code=ErrorCode.PERMISSION_DENIED,
        )


def build_owner_filter(user: CurrentUser, field_name: str = "created_by") -> dict:
    """
    构造数据级权限过滤条件（供仓储层查询使用）

    参数：
        user:       当前登录用户
        field_name: 归属字段名，默认 created_by

    返回：
        dict：可直接合并进 MongoDB 查询条件的字典；
              拥有全局查看权限时返回空字典（不过滤）

    使用示例：
        query = {"status": "pending"}
        query.update(build_owner_filter(user))
        tasks = await task_repo.list_tasks(query)

    说明：
        把"是否过滤"的决策收口到本函数，避免每个查询点各写一遍
        if user.role == "admin" 之类的判断——那种写法一旦漏写，
        业务经办人就能看到全部任务，且不易被测试发现。
    """
    if user.can_view_all_tasks:
        return {}
    return {field_name: user.user_id}


def get_permission_matrix() -> dict:
    """
    获取完整的角色权限矩阵（供权限校验接口返回给前端）

    返回：
        dict：结构为 {角色值: {"role_label": 中文名, "permissions": [权限点...]}}

    说明：
        前端路由鉴权需要知道"每种角色能做什么"。
        与其在前端硬编码一份矩阵（必然与后端漂移），
        不如由后端作为唯一权威源下发，前端只做渲染与判断。
    """
    from app.constants import USER_ROLE_LABELS

    matrix: dict = {}
    for role_enum in UserRole:
        role_value = role_enum.value
        matrix[role_value] = {
            "role_label": USER_ROLE_LABELS.get(role_value, role_value),
            "permissions": list(ROLE_PERMISSIONS.get(role_value, ())),
        }
    return matrix
