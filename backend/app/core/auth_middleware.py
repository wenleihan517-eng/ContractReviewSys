# D:/ContractReviewSys-合同审批智能审查项目/backend/app/core/auth_middleware.py
"""
全局鉴权中间件 — 接口层的默认拒绝兜底防线

【为什么需要中间件，而不是只靠依赖注入？】
    鉴权依赖（app.core.dependencies.get_current_user）是"显式声明"式防护：
    接口必须写 `Depends(get_current_user)` 才会校验。这带来一个真实风险——
    新增接口时一旦忘记加依赖，该接口就变成完全公开的，且不会有任何报错，
    只能靠代码评审发现。

    本中间件提供"默认拒绝"式兜底：除白名单外，所有请求一律要求携带
    合法访问令牌。两者的职责分工是：
        中间件   → 保证"不漏"（默认拒绝，防遗漏）
        依赖注入 → 保证"可判"（提供用户上下文，做细粒度权限与数据归属校验）

    因此本中间件只做"有没有合法令牌"这一层的粗粒度校验，
    不判断具体权限。真正的角色/权限判定仍由接口上的依赖完成，
    这样中间件无需感知业务权限矩阵，新增权限点不必改中间件。

【与依赖注入的关系：结果复用】
    中间件解析出的用户上下文写入 request.state.current_user，
    get_current_user 优先读取该缓存，避免同一请求内重复解码令牌与重复查库。

【401 与 403 的边界】
    401 Unauthorized：未认证 —— 未带令牌、令牌非法、令牌过期、令牌类型不符
    403 Forbidden   ：已认证但无权 —— 账号停用/锁定、角色权限不足
    本中间件只产生 401；403 由依赖注入在拿到用户身份后判断。

【安全约定】
    1. 白名单匹配采用"精确路径 + 前缀"双规则，且必须做前缀边界判断，
       严禁使用朴素 startswith 直接匹配，否则 "/api/v1/auth/login-anything"
       这类路径会被错误放行（详见 _is_whitelisted 的说明）。
    2. 非 HTTP 请求（如 lifespan 内部消息）直接放行，不做鉴权。
    3. OPTIONS 预检请求放行，否则浏览器跨域预检会因 401 失败，
       导致前端所有跨域请求报错且难以定位。
    4. 中间件内部异常一律不得吞掉：若鉴权逻辑自身出错，
       应返回 500 而不是放行，避免"鉴权故障 → 全部接口裸奔"。

返回值说明：
    - AuthMiddleware：中间件类，注册到 FastAPI 即可
    - build_auth_error_response()：构造统一格式的 401 响应

使用示例：
    from app.core.auth_middleware import AuthMiddleware
    app.add_middleware(AuthMiddleware)
"""

import uuid
from typing import Any, Callable, Dict, Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core.config import settings
from app.core.exceptions import ErrorCode, ForbiddenException, UnauthorizedException
from app.core.logging_config import get_logger
from app.core.security import TokenType, extract_bearer_token

logger = get_logger(__name__)


# 无需鉴权的 HTTP 方法（预检请求必须放行，否则浏览器跨域预检失败）
_ALWAYS_ALLOWED_METHODS = frozenset({"OPTIONS"})

# 文档与静态资源的固定放行后缀（避免每个部署都要手工配白名单）
_ALWAYS_ALLOWED_SUFFIXES = (
    "/docs",
    "/redoc",
    "/openapi.json",
    "/favicon.ico",
)


def _normalize_path(path: str) -> str:
    """
    规范化请求路径，供白名单比对使用

    参数：
        path: 原始请求路径

    返回：
        str：规范化后的路径

    规范化步骤（顺序不可调换）：
        1. 解码 URL 编码字符（如 %2e%2e → ..），防止编码绕过
        2. 折叠重复斜杠（// → /）
        3. 解析并消除 "." 与 ".." 路径段（防止目录穿越混淆）
        4. 去掉尾部斜杠（根路径保持为 "/"）

    【为什么必须处理 ".."】
        若不做路径段解析，以下请求会命中白名单而被错误放行：
            白名单配置 "/api/v1/auth/login"
            请求路径 "/api/v1/auth/login/../users"
            朴素的前缀匹配看到 "以 /api/v1/auth/login/ 开头" → 判定为白名单放行，
            而 ASGI 服务器会把该路径解析为 "/api/v1/users"，
            结果就是攻击者无需令牌即可访问用户管理接口。
        这是真实存在过的绕过手法，必须在此处彻底消除，
        不能依赖"框架会帮我解析"——不同服务器（uvicorn/hypercorn/nginx）
        的解析时机与结果并不一致。

    【注意】中间件早于路由匹配执行，此时拿到的 request.url.path 是未解码的原始
        路径。因此本函数承担了"白名单判定前的最后一次路径清洗"职责，
        判定与后续路由使用同一套解析结果，杜绝"判定的路径 ≠ 实际执行的路径"。

    使用示例：
        _normalize_path("/api/v1/auth/login/../users")  # "/api/v1/users"
        _normalize_path("/api/v1/health//")             # "/api/v1/health"
    """
    if not path:
        return "/"

    # --- 步骤 1：URL 解码（防御 %2e%2e 形式的编码穿越）---
    # 只解码一次，不循环解码；循环解码会让 %252e 被反复还原，
    # 反而制造出新的歧义（不同层的解码次数不一致）。
    try:
        from urllib.parse import unquote

        decoded = unquote(path)
    except Exception:  # noqa: BLE001 - 解码失败时退回原始路径，交由后续规则判定
        decoded = path

    # 统一反斜杠：部分服务器把 "\" 也当作路径分隔符
    decoded = decoded.replace("\\", "/")

    # --- 步骤 2 & 3：折叠斜杠 + 消除 "." 与 ".." ---
    # 使用栈式处理，语义与 POSIX 路径解析一致：
    #   "."  → 跳过（当前目录）
    #   ".." → 弹出栈顶（回退一级）；栈已空时忽略（已到根，不能越出根目录）
    segments = []
    for segment in decoded.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            if segments:
                segments.pop()
            # 栈为空说明试图越过根目录，直接忽略该 ".."，
            # 保证结果始终是绝对路径，不会产生 "../" 逃逸
            continue
        segments.append(segment)

    normalized = "/" + "/".join(segments)

    # --- 步骤 4：去掉尾部斜杠（根路径除外）---
    if len(normalized) > 1 and normalized.endswith("/"):
        normalized = normalized.rstrip("/")

    return normalized


def _is_whitelisted(path: str, whitelist: frozenset) -> bool:
    """
    判断请求路径是否命中鉴权白名单

    参数：
        path:      规范化后的请求路径
        whitelist: 白名单路径集合（来自 settings.auth_whitelist_paths）

    返回：
        bool：命中返回 True（放行），否则 False（需鉴权）

    匹配规则（按顺序）：
        1. 精确匹配：path == 白名单项
        2. 子路径匹配：path 以 "白名单项 + /" 开头
           —— 例如白名单含 "/docs"，则 "/docs/oauth2-redirect" 也放行，
              用于接口文档的静态资源；但 "/docs-malicious" 不匹配。

    【关键】为什么不用 path.startswith(item)？
        朴素前缀匹配存在安全漏洞：
            白名单配置 "/api/v1/auth/login"
            攻击者请求 "/api/v1/auth/login/../../admin/users"
            （或更直白的 "/api/v1/auth/loginBypass"）
            都会被 startswith 误判为白名单而放行。
        因此必须要求边界字符为 "/"，把匹配范围严格限制在"该路径的子树内"。

    使用示例：
        _is_whitelisted("/api/v1/auth/login", frozenset({"/api/v1/auth/login"}))  # True
        _is_whitelisted("/api/v1/auth/loginBypass", frozenset({"/api/v1/auth/login"}))  # False
    """
    if path in whitelist:
        return True

    for item in whitelist:
        if path.startswith(item + "/"):
            return True

    return False


def _is_always_allowed(path: str) -> bool:
    """
    判断路径是否属于"强制放行"范围（文档与静态资源）

    参数：
        path: 规范化后的请求路径

    返回：
        bool：属于放行范围返回 True

    说明：
        接口文档与 favicon 不含任何业务数据，且若需鉴权会导致
        浏览器访问 /docs 时无法加载，因此无论白名单怎么配都放行。
        注意这里同样使用 "路径 + 结尾" 的严格判断，不用朴素前缀匹配。
    """
    for suffix in _ALWAYS_ALLOWED_SUFFIXES:
        if path == suffix or path.endswith(suffix):
            return True
    return False


def build_auth_error_response(
    request: Request,
    exc: UnauthorizedException | ForbiddenException,
) -> JSONResponse:
    """
    构造鉴权失败的统一错误响应

    参数：
        request: 当前请求对象
        exc:     鉴权异常实例

    返回：
        JSONResponse：与全局异常处理器完全一致的响应结构
                      {code, message, detail, request_id}

    说明：
        中间件无法通过 raise 让全局异常处理器接管
        （BaseHTTPMiddleware 中的异常不会走 FastAPI 的异常处理器链），
        因此必须在中间件内手工构造响应。
        为保持前后端契约一致，这里刻意复刻 _build_error_response 的结构，
        字段名与生产环境隐藏 detail 的策略完全相同。
    """
    request_id = request.headers.get("X-Request-Id") or str(uuid.uuid4())
    return JSONResponse(
        status_code=exc.http_status,
        content={
            "code": exc.code,
            "message": exc.message,
            "detail": None if settings.is_production else exc.detail,
            "request_id": request_id,
        },
        headers={"X-Request-Id": request_id},
    )


class AuthMiddleware(BaseHTTPMiddleware):
    """
    全局鉴权中间件（默认拒绝）

    行为：
        1. 请求路径命中白名单 → 放行
        2. 请求方法为 OPTIONS（跨域预检）→ 放行
        3. 非 HTTP 请求 → 放行
        4. AUTH_ENABLED=False → 全部放行（仅本地调试，启动时打告警）
        5. 其余请求：
             a. 缺少 Authorization 头 → 401（TOKEN_MISSING）
             b. 请求头格式非法        → 401（TOKEN_INVALID）
             c. 令牌签名失败/被篡改   → 401（TOKEN_INVALID）
             d. 令牌已过期            → 401（TOKEN_EXPIRED）
             e. 令牌类型不符          → 401（TOKEN_TYPE_INVALID）
             f. 校验通过 → 解析用户上下文写入 request.state 后放行

    注册顺序要求：
        add_middleware 是"后注册者先执行"（Starlette 的中间件栈是洋葱模型）。
        本中间件依赖 request_id 用于日志串联，因此必须在 RequestIdMiddleware
        之前注册，即：
            app.add_middleware(AuthMiddleware)        # 先注册 → 后执行
            app.add_middleware(RequestIdMiddleware)   # 后注册 → 先执行
        若顺序颠倒，鉴权日志中将缺少 request_id，排查问题时无法串联链路。
    """

    async def dispatch(
        self, request: Request, call_next: Callable
    ) -> Response:
        """
        中间件主处理逻辑

        参数：
            request:  当前请求对象
            call_next: 下游处理调用链

        返回：
            Response：放行后的业务响应，或统一格式的鉴权失败响应

        异常：
            无 —— 所有鉴权异常均被转换为响应，不向上抛出。
            但内部非鉴权类异常（如配置错误）会向上抛出，
            由全局异常处理器兜底返回 500，避免"鉴权故障即放行"。
        """
        path = _normalize_path(request.url.path)

        # ---- 放行判断 1：跨域预检请求 ----
        # 浏览器在发起跨域非简单请求前会先发 OPTIONS 预检，
        # 该请求不携带业务令牌。若此处拦截，前端所有跨域请求都会失败，
        # 且浏览器报错信息为"CORS 错误"，极易误导排查方向。
        if request.method in _ALWAYS_ALLOWED_METHODS:
            return await call_next(request)

        # ---- 放行判断 2：非 HTTP 请求（lifespan 等内部消息）----
        if request.scope.get("type") != "http":
            return await call_next(request)

        # ---- 放行判断 3：接口文档与静态资源 ----
        if _is_always_allowed(path):
            return await call_next(request)

        # ---- 放行判断 4：白名单路径 ----
        whitelist = settings.auth_whitelist_paths
        if _is_whitelisted(path, whitelist):
            return await call_next(request)

        # ---- 放行判断 5：鉴权总开关关闭（仅本地调试）----
        if not settings.AUTH_ENABLED:
            # 该分支在生产环境不可达（AUTH_ENABLED 恒为 True），
            # 但一旦被误配，属于严重安全事件，故以 WARNING 记录并留痕
            logger.warning(
                "全局鉴权已关闭，所有接口对外开放 | 路径=%s | 客户端=%s",
                path,
                request.client.host if request.client else "unknown",
            )
            return await call_next(request)

        # ---- 核心：校验访问令牌 ----
        try:
            header_value = request.headers.get(settings.AUTH_TOKEN_HEADER_NAME)
            # 提取令牌（内部对缺失/格式非法分别抛 401）
            token = extract_bearer_token(header_value)
            # 解码校验（内部对签名失败/过期/类型不符分别抛 401）
            payload = self._decode_access_token(token)
        except UnauthorizedException as exc:
            logger.warning(
                "鉴权失败（未通过全局校验）| 路径=%s | 方法=%s | 错误码=%s | 提示=%s | 客户端=%s",
                path,
                request.method,
                exc.code,
                exc.message,
                request.client.host if request.client else "unknown",
            )
            return build_auth_error_response(request, exc)
        except ForbiddenException as exc:
            # ForbiddenException 由依赖注入抛出，中间件阶段理论上不会触发；
            # 保留该分支是为了将来把账号状态校验前移到中间件时无需改动结构
            logger.warning(
                "鉴权失败（账号状态异常）| 路径=%s | 错误码=%s | 提示=%s",
                path,
                exc.code,
                exc.message,
            )
            return build_auth_error_response(request, exc)

        # ---- 校验通过：把令牌载荷暂存，供下游依赖复用 ----
        # 注意：此处只暂存"解码后的载荷"，不查库、不构造 CurrentUser。
        # 原因：查库是有成本的，且并非所有接口都需要完整用户上下文
        # （例如仅需知道"已登录"的场景）。完整上下文的构造交给
        # get_current_user，它会在首次被依赖时执行一次并缓存结果。
        request.state.token_payload = payload

        return await call_next(request)

    @staticmethod
    def _decode_access_token(token: str) -> Dict[str, Any]:
        """
        解码并校验访问令牌（中间件内部使用的轻量校验）

        参数：
            token: JWT 字符串

        返回：
            Dict[str, Any]：解码后的载荷

        异常：
            UnauthorizedException：令牌非法 / 过期 / 类型不符

        说明：
            独立成静态方法便于单元测试直接调用，
            避免为了测一段逻辑而必须构造完整的 ASGI 请求对象。
        """
        from app.core.security import decode_token

        return decode_token(token, expected_type=TokenType.ACCESS)
