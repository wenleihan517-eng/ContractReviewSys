# D:/ContractReviewSys-合同审批智能审查项目/backend/app/schemas/auth.py
"""
登录鉴权接口契约模型

【目录职责】
    定义"登录鉴权"接口的请求与响应模型。
    与 app/models/user.py 的存储模型严格区分：
        存储模型（User）    —— 含 password_hash / token_version / failed_login_count
                              等内部字段，绝不出现在接口出入参中
        契约模型（本模块）  —— 只暴露前端需要的字段，不泄露任何凭证与安全计数

【安全约定】
    1. 本模块中的任何响应模型都不包含 password_hash 字段。
       这不是靠"记得不要加"来保证，而是通过 UserInfo 字段白名单式定义，
       即使调用方传入完整 User 模型也不会带出哈希值。
    2. LoginRequest.password 参与日志时需做掩码（见 docstring 说明），
       严禁将密码明文写入日志或响应。
    3. RefreshTokenRequest 位于请求体而非查询参数——
       查询参数会进入访问日志、浏览器历史、Referer 头，
       把长效刷新令牌放在 URL 中是典型的信息泄露途径。

返回值说明：
    本模块只定义数据模型，无函数返回值

使用示例：
    from app.schemas.auth import LoginRequest, LoginResponse, UserInfo
    req = LoginRequest(username="user_law", password="123456")
"""

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.constants import UserRole, UserStatus


# ======================================================================
# 一、请求模型
# ======================================================================
class LoginRequest(BaseModel):
    """
    账号密码登录请求

    字段：
        username: 登录账号（必填）
        password: 登录密码（必填）

    安全说明：
        密码字段在日志输出时会被掩码处理（见 mask_password）。
        Pydantic 的 repr 默认会展示字段值，因此本模型重写了
        __repr__，保证异常堆栈或调试打印中不会泄露明文密码。
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    username: str = Field(
        ...,
        min_length=3,
        max_length=50,
        description="登录账号",
        examples=["user_law"],
    )
    # 长度上限 64 字符（远小于 bcrypt 的 72 字节限制，
    # 中文密码 64 字符 = 192 字节，故此处按字符数限制并在 security 层再做字节校验）
    password: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="登录密码",
    )

    @field_validator("username")
    @classmethod
    def validate_username(cls, value: str) -> str:
        """
        校验账号格式（与用户模型保持同一口径）

        参数：
            value: 待校验账号

        返回：
            str：校验通过的账号

        异常：
            ValueError：包含非法字符时抛出

        说明：
            登录阶段做格式前置校验，可以在不查库的情况下快速拒绝
            明显非法的输入（如含空格、中文、SQL/NoSQL 注入字符的账号），
            减少无效的数据库查询与日志噪音。
        """
        import re

        if not re.match(r"^[A-Za-z][A-Za-z0-9_]*$", value):
            raise ValueError("账号只能由字母、数字、下划线组成且以字母开头")
        return value

    def __repr__(self) -> str:
        """
        返回脱敏后的调试字符串（密码替换为掩码）

        返回：
            str：形如 LoginRequest(username='user_law', password='***')
        """
        return f"LoginRequest(username={self.username!r}, password='***')"

    __str__ = __repr__


class RefreshTokenRequest(BaseModel):
    """
    刷新访问令牌请求

    字段：
        refresh_token: 刷新令牌字符串

    说明：
        刷新令牌放在请求体而非查询参数，避免令牌进入
        访问日志 / 浏览器历史 / Referer 头等持久化位置。
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    refresh_token: str = Field(
        ...,
        min_length=10,
        description="刷新令牌（登录接口返回的 refresh_token）",
    )

    def __repr__(self) -> str:
        """
        返回脱敏后的调试字符串

        返回：
            str：形如 RefreshTokenRequest(refresh_token='***')
        """
        return "RefreshTokenRequest(refresh_token='***')"

    __str__ = __repr__


class LogoutRequest(BaseModel):
    """
    退出登录请求

    字段：
        refresh_token: 可选的刷新令牌

    说明：
        本项目采用"服务端递增 token_version"方式撤销令牌，
        因此登出无需提交令牌即可生效（撤销该用户全部令牌）。
        保留该字段是为了支持"仅登出当前设备"的未来扩展；
        当前实现下传入与否不影响结果。
    """

    refresh_token: Optional[str] = Field(
        default=None,
        description="刷新令牌（可选，当前实现按用户整体撤销，传入不影响结果）",
    )


class PermissionCheckRequest(BaseModel):
    """
    权限校验请求（供前端路由守卫使用）

    字段：
        permission:      可选，待校验的权限点（如 contract:upload）
        role:            可选，待校验的角色（如 law_user）
        require_all:     是否要求全部权限点满足（仅在没有 permission 时无效）

    说明：
        两种校验方式二选一，至少填一个：
            - 传 permission：校验"当前登录用户是否拥有该权限点"
            - 传 role：校验"当前登录用户是否属于该角色"
        若不传任何字段，接口会返回当前用户的完整权限清单（用于前端初始化），
        而非报错——这样前端只需一个接口即可完成初始化与按需校验两件事。
    """

    permission: Optional[str] = Field(
        default=None,
        max_length=64,
        description="待校验的权限点，如 contract:upload",
    )
    role: Optional[str] = Field(
        default=None,
        max_length=32,
        description="待校验的角色，如 law_user",
    )

    @field_validator("role")
    @classmethod
    def validate_role(cls, value: Optional[str]) -> Optional[str]:
        """
        校验角色值合法性

        参数：
            value: 待校验角色

        返回：
            Optional[str]：校验通过的角色值；为 None 时直接返回 None

        异常：
            ValueError：角色不在 UserRole 枚举内时抛出

        说明：
            角色值非法时直接返回 422 参数错误，而不是静默返回"无权限"。
            原因是"角色名拼错"属于前端代码缺陷，应被立即发现；
            若静默返回 403，开发者会误以为是自己权限不足，
            从而朝错误方向排查。
        """
        if value is None:
            return None
        valid = [item.value for item in UserRole]
        if value not in valid:
            raise ValueError(f"角色非法，可选值为 {valid}")
        return value


# ======================================================================
# 二、响应模型
# ======================================================================
class UserInfo(BaseModel):
    """
    用户信息响应模型（对外暴露的用户身份）

    字段：
        user_id:            用户 id
        username:           登录账号
        display_name:       展示名称
        role:               角色标识
        role_label:         角色中文名
        status:             账号状态
        department:         所属部门
        email:              邮箱
        permissions:        该角色拥有的权限点清单
        is_admin:           是否管理员
        can_view_all_tasks: 是否可查看全部任务

    安全说明：
        本模型是"字段白名单"式定义，不含 password_hash、token_version、
        failed_login_count、locked_until 等任何安全敏感字段。
        即使外部传入完整的 User 存储模型，也只会取到下列字段
        （由 from_user_model 方法显式挑选字段实现）。
    """

    user_id: str = Field(description="用户 ID")
    username: str = Field(description="登录账号")
    display_name: str = Field(description="展示名称")
    role: str = Field(description="角色标识：business_user / law_user / admin")
    role_label: str = Field(description="角色中文名")
    status: str = Field(description="账号状态：active / disabled / locked")
    department: Optional[str] = Field(default=None, description="所属部门")
    email: Optional[str] = Field(default=None, description="邮箱")
    permissions: List[str] = Field(
        default_factory=list,
        description="当前角色拥有的权限点清单，供前端路由鉴权使用",
    )
    is_admin: bool = Field(default=False, description="是否为系统管理员")
    can_view_all_tasks: bool = Field(
        default=False,
        description="是否可查看全部审查任务（业务经办人为 False，前端据此隐藏全量入口）",
    )

    @classmethod
    def from_current_user(cls, user) -> "UserInfo":
        """
        从 CurrentUser 上下文对象构造用户信息

        参数：
            user: app.core.dependencies.CurrentUser 实例

        返回：
            UserInfo：对外用户信息模型

        说明：
            显式逐字段挑选而非 model_validate(user)，
            保证将来 CurrentUser 新增敏感字段时不会意外泄露到接口响应中。
            这是"显式优于隐式"在安全场景下的具体应用。
        """
        return cls(
            user_id=user.user_id,
            username=user.username,
            display_name=user.display_name,
            role=user.role,
            role_label=user.role_label,
            status=user.status,
            department=user.department,
            email=user.email,
            permissions=list(user.permissions),
            is_admin=user.is_admin,
            can_view_all_tasks=user.can_view_all_tasks,
        )

    @classmethod
    def from_user_model(cls, user) -> "UserInfo":
        """
        从数据库用户模型构造用户信息

        参数：
            user: app.models.user.UserInDB 实例

        返回：
            UserInfo：对外用户信息模型

        说明：
            用于登录接口——此时尚未构造 CurrentUser，直接由存储模型转换。
            权限点通过角色的权限矩阵实时计算，与 CurrentUser 保持一致口径。
        """
        from app.constants import get_permissions_for_role
        from app.core.dependencies import build_owner_filter  # noqa: F401 - 仅保持导入一致性

        permissions = get_permissions_for_role(user.role.value)
        return cls(
            user_id=user.id,
            username=user.username,
            display_name=user.display_name,
            role=user.role.value,
            role_label=user.role_label,
            status=user.status.value,
            department=user.department,
            email=user.email,
            permissions=list(permissions),
            is_admin=user.is_admin,
            can_view_all_tasks="task:view_all" in permissions,
        )


class TokenInfo(BaseModel):
    """
    令牌信息模型（登录与刷新接口共用的返回体）

    字段：
        access_token:       访问令牌
        refresh_token:      刷新令牌
        token_type:         固定为 "Bearer"
        expires_in:         访问令牌有效期（秒）
        refresh_expires_in: 刷新令牌有效期（秒）
    """

    access_token: str = Field(description="访问令牌，用于业务接口鉴权")
    refresh_token: str = Field(description="刷新令牌，用于换取新的访问令牌")
    token_type: str = Field(default="Bearer", description="令牌类型，固定为 Bearer")
    expires_in: int = Field(description="访问令牌有效期（秒）")
    refresh_expires_in: int = Field(description="刷新令牌有效期（秒）")

    def __repr__(self) -> str:
        """
        返回脱敏后的调试字符串（令牌替换为掩码）

        返回：
            str：形如 TokenInfo(token_type='Bearer', expires_in=7200, ...)
        """
        return (
            f"TokenInfo(access_token='***', refresh_token='***', "
            f"token_type={self.token_type!r}, expires_in={self.expires_in}, "
            f"refresh_expires_in={self.refresh_expires_in})"
        )

    __str__ = __repr__


class LoginResponse(BaseModel):
    """
    登录响应模型

    字段：
        access_token:       访问令牌
        refresh_token:      刷新令牌
        token_type:         令牌类型
        expires_in:         访问令牌有效期（秒）
        refresh_expires_in: 刷新令牌有效期（秒）
        user:               登录用户信息（含角色与权限点）

    使用示例（前端）：
        const { access_token, user } = resp.data;
        localStorage.setItem('access_token', access_token);
        // user.role 决定可访问的路由，user.permissions 决定按钮级权限
    """

    access_token: str = Field(description="访问令牌")
    refresh_token: str = Field(description="刷新令牌")
    token_type: str = Field(default="Bearer", description="令牌类型")
    expires_in: int = Field(description="访问令牌有效期（秒）")
    refresh_expires_in: int = Field(description="刷新令牌有效期（秒）")
    user: UserInfo = Field(description="登录用户信息")

    def __repr__(self) -> str:
        """
        返回脱敏后的调试字符串

        返回：
            str：不含令牌原文
        """
        return (
            f"LoginResponse(access_token='***', refresh_token='***', "
            f"user={self.user.username!r}, role={self.user.role!r})"
        )

    __str__ = __repr__


class RefreshTokenResponse(BaseModel):
    """
    刷新令牌响应模型

    字段：
        access_token:       新的访问令牌
        token_type:         令牌类型
        expires_in:         新访问令牌有效期（秒）
        refresh_token:      新的刷新令牌（令牌轮换）
        refresh_expires_in: 新刷新令牌有效期（秒）

    说明：
        采用"刷新令牌轮换"策略：每次刷新同时签发新的 refresh_token，
        使旧刷新令牌在下次刷新时因版本不匹配而失效。
        这可缩小刷新令牌泄露后的可利用窗口——
        若客户端收到过新令牌，说明旧令牌已被使用过（可能是攻击者），
        此时旧令牌失效可阻断攻击者的持续访问。
    """

    access_token: str = Field(description="新的访问令牌")
    token_type: str = Field(default="Bearer", description="令牌类型")
    expires_in: int = Field(description="访问令牌有效期（秒）")
    refresh_token: str = Field(description="新的刷新令牌")
    refresh_expires_in: int = Field(description="刷新令牌有效期（秒）")

    def __repr__(self) -> str:
        """
        返回脱敏后的调试字符串

        返回：
            str：不含令牌原文
        """
        return (
            f"RefreshTokenResponse(access_token='***', refresh_token='***', "
            f"expires_in={self.expires_in})"
        )

    __str__ = __repr__


class LogoutResponse(BaseModel):
    """
    退出登录响应模型

    字段：
        username:    登出账号
        revoked_at:  撤销时间
        message:     提示信息
    """

    username: str = Field(description="已登出的账号")
    revoked_at: datetime = Field(description="令牌撤销时间")
    message: str = Field(default="已成功退出登录", description="提示信息")


class PermissionCheckResponse(BaseModel):
    """
    权限校验响应模型（前端路由守卫使用）

    字段：
        user_id:       当前用户 id
        username:      当前用户账号
        role:          当前用户角色
        role_label:    角色中文名
        permissions:   当前用户拥有的全部权限点
        passed:        本次校验是否通过（未指定校验目标时恒为 True）
        checked_type:  本次校验的类型：role / permission / none
        checked_value: 本次校验的目标值（未指定时为空字符串）
        message:       校验结果说明

    使用示例（前端路由守卫）：
        // 进入"合规规则与模板"页面前校验
        const { passed } = await checkPermission({ permission: 'rule_template:manage' });
        if (!passed) return <Navigate to="/dashboard" />;
    """

    user_id: str = Field(description="当前用户 ID")
    username: str = Field(description="当前用户账号")
    role: str = Field(description="当前用户角色")
    role_label: str = Field(description="角色中文名")
    permissions: List[str] = Field(
        default_factory=list, description="当前用户拥有的全部权限点"
    )
    passed: bool = Field(description="本次校验是否通过")
    checked_type: str = Field(description="校验类型：role / permission / none")
    checked_value: str = Field(default="", description="被校验的角色或权限点值")
    message: str = Field(default="", description="校验结果说明")


class PermissionMatrixResponse(BaseModel):
    """
    角色权限矩阵响应模型

    字段：
        roles: 角色列表，每项含角色值、中文名与该角色的权限点清单

    说明：
        由后端作为权限矩阵的唯一权威源下发，前端据此渲染菜单与按钮的
        可见性，避免前后端各自维护一份矩阵导致长期漂移。
    """

    roles: List[dict] = Field(
        default_factory=list,
        description="角色权限矩阵：[{role, role_label, permissions}, ...]",
    )
    total_permissions: int = Field(
        default=0, description="系统定义的权限点总数"
    )
