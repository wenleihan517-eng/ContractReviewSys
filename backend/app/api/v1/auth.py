# D:/ContractReviewSys-合同审批智能审查项目/backend/app/api/v1/auth.py
"""
登录鉴权接口 — 登录、登出、令牌刷新、用户信息与权限校验

【模块职责】
    提供登录鉴权模块对外暴露的全部 HTTP 接口：

        POST /api/v1/auth/login             账号密码登录，返回令牌对与用户信息
        POST /api/v1/auth/refresh           用刷新令牌换取新的访问令牌
        POST /api/v1/auth/logout            退出登录，撤销该用户全部令牌
        GET  /api/v1/auth/me                获取当前登录用户信息
        POST /api/v1/auth/check-permission  权限校验（前端路由鉴权使用）
        GET  /api/v1/auth/permission-matrix 角色权限矩阵（前端菜单渲染使用）
        POST /api/v1/auth/change-password   修改密码（可选能力）

【接口鉴权说明】
    login / refresh：位于鉴权白名单，无需令牌即可访问（见 .env 的 AUTH_WHITELIST_PATHS）
    logout / me / check-permission / permission-matrix / change-password：
        需要携带 Authorization: Bearer <access_token>

【响应约定】
    全部接口统一使用 ApiResponse 信封：
        { "code": 0, "message": "...", "data": {...}, "request_id": "..." }
    失败时由全局异常处理器返回：
        { "code": 6xxxx, "message": "...", "detail": "...", "request_id": "..." }
    其中 401 表示未认证（需跳登录页），403 表示无权限（提示无权访问）。

【分层约束】
    本模块属于 api 层，严格遵守项目分层规范：
        - 只做参数接收、调用 service、组装响应
        - 不直接访问数据库（走 services → repositories）
        - 不直接进行密码/令牌计算（走 services → core.security）
        - 不包含业务判定逻辑

返回值说明：
    全部接口返回 ApiResponse 或对应的 Pydantic 响应模型（均被 ApiResponse 包裹）

使用示例：
    # 前端登录
    POST /api/v1/auth/login
    {"username": "user_law", "password": "123456"}

    # 携带令牌访问
    GET /api/v1/auth/me
    Authorization: Bearer eyJhbGciOiJIUzI1NiIs...
"""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, Request, status

from app.constants import ALL_PERMISSIONS, USER_ROLE_LABELS, UserRole
from app.core.config import settings
from app.core.dependencies import (
    CurrentUser,
    _get_client_ip,
    get_current_request_id,
    get_current_user,
    get_permission_matrix,
)
from app.core.logging_config import get_logger
from app.core.response import ApiResponse, success_response
from app.schemas.auth import (
    LoginRequest,
    LoginResponse,
    LogoutRequest,
    LogoutResponse,
    PermissionCheckRequest,
    PermissionCheckResponse,
    PermissionMatrixResponse,
    RefreshTokenRequest,
    RefreshTokenResponse,
    UserInfo,
)
from app.services import auth_service

logger = get_logger(__name__)

# 路由对象，统一由 v1 路由聚合器注册
# prefix 前缀为 /auth，最终路径形如 /api/v1/auth/login
router = APIRouter(prefix="/auth", tags=["认证-登录鉴权"])


# ======================================================================
# 一、登录
# ======================================================================
@router.post(
    "/login",
    response_model=ApiResponse,
    summary="账号密码登录",
    description=(
        "使用账号密码登录系统。\n\n"
        "**请求示例**：`{\"username\": \"user_law\", \"password\": \"123456\"}`\n\n"
        "**成功返回**：access_token、refresh_token、用户信息（含角色与权限点清单）\n\n"
        "**内置演示账号**：\n"
        "- `user_business` / `123456` — 业务经办人\n"
        "- `user_law` / `123456` — 法务审查人\n"
        "- `user_admin` / `123456` — 系统管理员\n\n"
        "**失败返回**：\n"
        "- 401：账号或密码错误\n"
        "- 403：账号已停用 / 因连续登录失败被锁定\n\n"
        "**安全说明**：账号不存在与密码错误返回完全相同的错误码与提示，"
        "防止攻击者通过响应差异枚举有效账号。"
    ),
)
async def login(
    request: Request,
    payload: LoginRequest = Body(...),
) -> ApiResponse:
    """
    账号密码登录

    参数：
        request: FastAPI 请求对象（用于提取客户端 IP）
        payload: 登录请求体，含 username 与 password

    返回：
        ApiResponse：data 为 LoginResponse 结构，含令牌对与用户信息

    异常：
        UnauthorizedException（HTTP 401）：账号或密码错误
        ForbiddenException（HTTP 403）：账号停用 / 锁定

    说明：
        本接口位于鉴权白名单，无需携带令牌即可访问。
        客户端 IP 从请求头解析后传入服务层，用于登录审计与异常行为分析。
    """
    login_ip = _get_client_ip(request)

    result: LoginResponse = await auth_service.login(
        username=payload.username,
        password=payload.password,
        login_ip=login_ip,
    )

    return success_response(
        data=result.model_dump(),
        message=f"登录成功，欢迎 {result.user.display_name}",
        request_id=get_current_request_id(),
    )


# ======================================================================
# 二、刷新令牌
# ======================================================================
@router.post(
    "/refresh",
    response_model=ApiResponse,
    summary="刷新访问令牌",
    description=(
        "使用 refresh_token 换取新的 access_token。\n\n"
        "**请求示例**：`{\"refresh_token\": \"eyJhbGciOiJIUzI1NiIs...\"}`\n\n"
        "**成功返回**：新的访问令牌 + 新的刷新令牌（令牌轮换）\n\n"
        "**失败返回**：401（刷新令牌非法/过期/已失效）\n\n"
        "**说明**：本接口位于鉴权白名单，无需携带访问令牌。\n"
        "刷新令牌必须放在请求体中，不可放在 URL 查询参数"
        "（查询参数会进入访问日志与浏览器历史，导致长效令牌泄露）。"
    ),
)
async def refresh_token(
    payload: RefreshTokenRequest = Body(...),
) -> ApiResponse:
    """
    刷新访问令牌

    参数：
        payload: 刷新请求体，含 refresh_token

    返回：
        ApiResponse：data 为 RefreshTokenResponse 结构

    异常：
        UnauthorizedException（HTTP 401）：刷新令牌非法 / 过期 / 版本失效
        ForbiddenException（HTTP 403）：账号停用 / 锁定

    说明：
        采用刷新令牌轮换策略：每次刷新同时签发新的 refresh_token，
        旧的刷新令牌在下次使用时会因版本不匹配而失效，
        可缩小刷新令牌泄露后的可利用窗口。
    """
    result: RefreshTokenResponse = await auth_service.refresh_access_token(
        refresh_token=payload.refresh_token
    )

    return success_response(
        data=result.model_dump(),
        message="访问令牌已刷新",
        request_id=get_current_request_id(),
    )


# ======================================================================
# 三、退出登录
# ======================================================================
@router.post(
    "/logout",
    response_model=ApiResponse,
    summary="退出登录",
    description=(
        "退出登录并撤销当前用户的全部令牌（所有设备同时失效）。\n\n"
        "**需要鉴权**：请在请求头携带 `Authorization: Bearer <access_token>`\n\n"
        "**说明**：JWT 本身无状态，服务端无法删除已签发的令牌。"
        "本接口通过递增用户的令牌版本号（token_version）实现等效撤销——"
        "旧令牌在下次鉴权时因版本不匹配而被拒绝，从而实现立即登出。\n\n"
        "**代价**：该用户在其他设备上的登录状态也会一并失效（全局登出）。"
    ),
)
async def logout(
    payload: Optional[LogoutRequest] = Body(default=None),
    current_user: CurrentUser = Depends(get_current_user),
) -> ApiResponse:
    """
    退出登录

    参数：
        payload:      可选请求体（当前实现按用户整体撤销，传入刷新令牌不影响结果）
        current_user: 当前登录用户（由鉴权依赖注入）

    返回：
        ApiResponse：data 为 LogoutResponse 结构

    说明：
        必须依赖 get_current_user，原因有二：
            1. 保证只有已登录用户才能调用登出（否则任何人都能撤销他人会话）
            2. 直接从令牌中取用户名，无需客户端传用户名——
               避免攻击者传入他人用户名执行"强制下线"攻击
    """
    result: LogoutResponse = await auth_service.logout(current_user.username)

    return success_response(
        data=result.model_dump(mode="json"),
        message=result.message,
        request_id=get_current_request_id(),
    )


# ======================================================================
# 四、获取当前登录用户信息
# ======================================================================
@router.get(
    "/me",
    response_model=ApiResponse,
    summary="获取当前登录用户信息",
    description=(
        "返回当前登录用户的身份与权限信息，供前端初始化用户状态使用。\n\n"
        "**需要鉴权**：请在请求头携带 `Authorization: Bearer <access_token>`\n\n"
        "**返回字段**：user_id、username、display_name、role、role_label、"
        "permissions（权限点清单）、is_admin、can_view_all_tasks\n\n"
        "**前端用法**：应用启动时调用本接口恢复登录态；"
        "根据 `permissions` 控制菜单与按钮的可见性。"
    ),
)
async def get_me(
    current_user: CurrentUser = Depends(get_current_user),
) -> ApiResponse:
    """
    获取当前登录用户信息

    参数：
        current_user: 当前登录用户（由鉴权依赖注入）

    返回：
        ApiResponse：data 为 UserInfo 结构

    说明：
        直接复用鉴权依赖已构造的 CurrentUser，不再重复查库。
        这是"依赖注入 + 请求内缓存"设计的直接收益：
        同一请求内 get_current_user 被多个依赖引用时只执行一次。
    """
    return success_response(
        data=UserInfo.from_current_user(current_user).model_dump(),
        message="获取当前用户信息成功",
        request_id=get_current_request_id(),
    )


# ======================================================================
# 五、权限校验（前端路由鉴权）
# ======================================================================
@router.post(
    "/check-permission",
    response_model=ApiResponse,
    summary="权限校验（前端路由鉴权）",
    description=(
        "校验当前登录用户是否拥有指定权限点或角色，供前端路由守卫调用。\n\n"
        "**需要鉴权**：请在请求头携带 `Authorization: Bearer <access_token>`\n\n"
        "**请求示例**：\n"
        "- 校验权限点：`{\"permission\": \"rule_template:manage\"}`\n"
        "- 校验角色：`{\"role\": \"admin\"}`\n"
        "- 不传参数：返回当前用户的完整权限清单（用于前端初始化）\n\n"
        "**返回**：passed 表示是否通过；同时返回 permissions 完整清单\n\n"
        "**说明**：本接口始终返回 HTTP 200，通过 `data.passed` 表达校验结果。\n"
        "这与业务接口的 403 语义不同——业务接口无权限时应返回 403，"
        "而本接口的职责就是『查询有无权限』，成功查询到结果本身就是 200。"
    ),
)
async def check_permission(
    payload: PermissionCheckRequest = Body(default_factory=PermissionCheckRequest),
    current_user: CurrentUser = Depends(get_current_user),
) -> ApiResponse:
    """
    权限校验（供前端路由守卫使用）

    参数：
        payload:      校验请求，含 permission（权限点）或 role（角色），均可为空
        current_user: 当前登录用户

    返回：
        ApiResponse：data 为 PermissionCheckResponse 结构

    说明：
        校验优先级：
            1. 传了 permission → 按权限点校验
            2. 未传 permission 但传了 role → 按角色校验
            3. 两者都未传 → passed=True，仅返回权限清单

        之所以"两者都不传也返回 200 而非 422"，
        是为了让前端只用一个接口完成"初始化拉取权限"与"按需校验"两件事，
        减少一次往返请求。
    """
    permissions = list(current_user.permissions)

    # ---- 情况 1：按权限点校验 ----
    if payload.permission:
        passed = current_user.has_permission(payload.permission)
        response = PermissionCheckResponse(
            user_id=current_user.user_id,
            username=current_user.username,
            role=current_user.role,
            role_label=current_user.role_label,
            permissions=permissions,
            passed=passed,
            checked_type="permission",
            checked_value=payload.permission,
            message=(
                "权限校验通过"
                if passed
                else f"当前角色（{current_user.role_label}）不具备权限：{payload.permission}"
            ),
        )
        if not passed:
            logger.info(
                "前端权限校验未通过 | username=%s | 角色=%s | 权限点=%s",
                current_user.username,
                current_user.role,
                payload.permission,
            )
        return success_response(
            data=response.model_dump(),
            message=response.message,
            request_id=get_current_request_id(),
        )

    # ---- 情况 2：按角色校验 ----
    if payload.role:
        passed = current_user.role == payload.role
        role_label = USER_ROLE_LABELS.get(payload.role, payload.role)
        response = PermissionCheckResponse(
            user_id=current_user.user_id,
            username=current_user.username,
            role=current_user.role,
            role_label=current_user.role_label,
            permissions=permissions,
            passed=passed,
            checked_type="role",
            checked_value=payload.role,
            message=(
                "角色校验通过"
                if passed
                else f"当前角色为 {current_user.role_label}，不是 {role_label}"
            ),
        )
        return success_response(
            data=response.model_dump(),
            message=response.message,
            request_id=get_current_request_id(),
        )

    # ---- 情况 3：未指定校验目标，返回权限清单 ----
    response = PermissionCheckResponse(
        user_id=current_user.user_id,
        username=current_user.username,
        role=current_user.role,
        role_label=current_user.role_label,
        permissions=permissions,
        passed=True,
        checked_type="none",
        checked_value="",
        message="未指定校验目标，已返回当前用户的权限清单",
    )
    return success_response(
        data=response.model_dump(),
        message=response.message,
        request_id=get_current_request_id(),
    )


# 兼容前端可能使用的 GET 形式（把校验目标放在查询参数中）
@router.get(
    "/check-permission",
    response_model=ApiResponse,
    summary="权限校验（GET 形式）",
    description=(
        "与 POST /auth/check-permission 等价，支持通过查询参数传递校验目标。\n\n"
        "**请求示例**：`GET /api/v1/auth/check-permission?permission=rule_template:manage`\n\n"
        "**说明**：提供 GET 形式是为了适配前端路由守卫的同步判断场景"
        "（部分场景下用 GET 更便于缓存与调试）。\n\n"
        "注意：此处只接受权限点与角色这类非敏感值，"
        "绝不可用于传递令牌等凭证——URL 会进入访问日志。"
    ),
)
async def check_permission_get(
    permission: Optional[str] = None,
    role: Optional[str] = None,
    current_user: CurrentUser = Depends(get_current_user),
) -> ApiResponse:
    """
    权限校验（GET 形式）

    参数：
        permission:   待校验的权限点，可选
        role:         待校验的角色，可选
        current_user: 当前登录用户

    返回：
        ApiResponse：data 为 PermissionCheckResponse 结构

    异常：
        ValueError：role 取值非法时由 Pydantic 校验抛出 422

    说明：
        复用与 POST 版本完全相同的判定逻辑，
        通过构造 PermissionCheckRequest 后转调，避免两处逻辑漂移。
    """
    payload = PermissionCheckRequest(permission=permission, role=role)
    return await check_permission(payload=payload, current_user=current_user)


# ======================================================================
# 六、角色权限矩阵
# ======================================================================
@router.get(
    "/permission-matrix",
    response_model=ApiResponse,
    summary="获取角色权限矩阵",
    description=(
        "返回系统全部角色及其权限点清单，供前端渲染菜单与按钮可见性。\n\n"
        "**需要鉴权**：请在请求头携带 `Authorization: Bearer <access_token>`\n\n"
        "**说明**：由后端作为权限矩阵的唯一权威源下发，"
        "前端据此动态渲染菜单，避免前后端各自维护一份矩阵导致长期漂移。\n\n"
        "**权限矩阵**：\n"
        "- `business_user` 业务经办人：上传合同、查看自己的任务、下载报告\n"
        "- `law_user` 法务审查人：查看全部任务、编辑风险项、批注、触发回写\n"
        "- `admin` 系统管理员：全部权限"
    ),
)
async def get_permission_matrix_api(
    current_user: CurrentUser = Depends(get_current_user),
) -> ApiResponse:
    """
    获取角色权限矩阵

    参数：
        current_user: 当前登录用户（需登录才能查看）

    返回：
        ApiResponse：data 为 PermissionMatrixResponse 结构

    说明：
        返回全量矩阵而非仅当前角色，便于前端在演示中展示"三种角色分别能做什么"。
        该信息不含业务数据，不构成信息泄露风险。
    """
    raw_matrix = get_permission_matrix()
    roles: List[Dict[str, Any]] = [
        {
            "role": role_value,
            "role_label": detail["role_label"],
            "permissions": detail["permissions"],
        }
        for role_value, detail in raw_matrix.items()
    ]

    response = PermissionMatrixResponse(
        roles=roles,
        total_permissions=len(ALL_PERMISSIONS),
    )

    return success_response(
        data=response.model_dump(),
        message="获取权限矩阵成功",
        request_id=get_current_request_id(),
    )


# ======================================================================
# 七、修改密码
# ======================================================================
@router.post(
    "/change-password",
    response_model=ApiResponse,
    summary="修改密码",
    description=(
        "修改当前登录用户的密码，成功后该用户全部令牌立即失效，需重新登录。\n\n"
        "**需要鉴权**：请在请求头携带 `Authorization: Bearer <access_token>`\n\n"
        "**请求示例**："
        "`{\"old_password\": \"123456\", \"new_password\": \"Abc123456\"}`\n\n"
        "**密码要求**：长度 6-64 位，必须同时包含字母和数字，不含空格\n\n"
        "**说明**：修改密码后自动撤销全部令牌，"
        "确保攻击者持有的旧令牌在用户改密后立即失效。"
    ),
)
async def change_password(
    old_password: str = Body(..., min_length=1, max_length=64, description="原密码"),
    new_password: str = Body(..., min_length=6, max_length=64, description="新密码"),
    current_user: CurrentUser = Depends(get_current_user),
) -> ApiResponse:
    """
    修改密码

    参数：
        old_password: 原密码，用于身份二次确认
        new_password: 新密码
        current_user: 当前登录用户

    返回：
        ApiResponse：data 含提示信息

    异常：
        UnauthorizedException（HTTP 401）：原密码错误
        ForbiddenException（HTTP 403）：账号已停用
        RequestValidationError（HTTP 422）：新密码不符合强度要求

    说明：
        必须同时提供原密码，防止令牌被盗后攻击者直接改密锁定真实用户。
    """
    await auth_service.change_password(
        username=current_user.username,
        old_password=old_password,
        new_password=new_password,
    )

    return success_response(
        data={
            "username": current_user.username,
            "message": "密码修改成功，所有设备的登录状态已失效，请使用新密码重新登录",
        },
        message="密码修改成功",
        request_id=get_current_request_id(),
    )
