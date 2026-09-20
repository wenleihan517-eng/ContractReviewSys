# D:/ContractReviewSys-合同审批智能审查项目/backend/app/core/security.py
"""
安全工具模块 — bcrypt 密码哈希与 JWT 令牌签发/校验

【职责】
    1. 密码哈希：bcrypt 单向哈希，提供 hash_password / verify_password
    2. JWT 签发：签发 access_token（短效）与 refresh_token（长效）
    3. JWT 校验：解码并校验签名、算法、签发方、有效期、令牌类型
    4. 令牌载荷规范化：统一载荷字段名，避免各处拼写不一致

【设计要点】
    1. 【禁止明文密码】本模块是全局唯一的密码哈希入口。
       hash_password 输出的永远是 bcrypt 哈希串；
       verify_password 内部自动兼容历史哈希（如未来换成 argon2）也不会出错，
       因为 CryptContext 配置了 schemes 列表与 deprecated 策略。
    2. 【令牌类型隔离】
       access_token 与 refresh_token 在载荷中以 type 区分，解码函数强制校验。
       若不做该校验，攻击者可用长效 refresh_token 直接访问业务接口，
       "短效 access_token" 的安全设计将被完全绕过。
    3. 【无状态撤销补偿】
       JWT 本身无状态、签发后无法回收。为支持"改密/登出立即失效"，
       载荷中携带 token_version，鉴权时与数据库中的用户 token_version 比对，
       不一致即视为失效。这以一次轻量查询换取了可控的撤销能力。
    4. 【时间基准统一】
       全部时间以 UTC 计算并在载荷中写入标准 NumericDate（秒级整数），
       避免服务器本地时区（东八区）与 JWT 规范的 UTC 基准混淆。
    5. 【算法固定】
       解码时显式传入 algorithms=[settings.JWT_ALGORITHM]，
       绝不使用令牌头部自带的 alg 字段，防止 alg=none 降级攻击。

返回值说明：
    见各函数 docstring

使用示例：
    from app.core.security import hash_password, verify_password, create_token_pair, decode_token
    hashed = hash_password("123456")
    ok = verify_password("123456", hashed)          # True
    pair = create_token_pair(user_id="...", username="user_law", role="law_user")
    payload = decode_token(pair["access_token"], expected_type=TokenType.ACCESS)
"""

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import jwt
from passlib.context import CryptContext
from passlib.exc import UnknownHashError

from app.constants import TokenType
from app.core.config import settings
from app.core.exceptions import (
    ErrorCode,
    UnauthorizedException,
)
from app.core.logging_config import get_logger

logger = get_logger(__name__)


# ======================================================================
# 一、密码哈希（bcrypt）
# ======================================================================
# CryptContext 说明：
#   schemes=["bcrypt"]        —— 首选 bcrypt 算法
#   deprecated="auto"         —— 老算法哈希会在校验成功后被标记为待升级
#   bcrypt__rounds            —— 计算强度，来自配置（默认 12）
#   bcrypt__truncate_error=True —— 密码超过 72 字节时直接报错而非静默截断。
#       【重要】bcrypt 算法本身只取密码前 72 字节，超长部分被忽略，
#       若不报错，两个前 72 字节相同的不同密码会互相通过校验，
#       形成静默的安全漏洞。此处选择"显式报错"以避免该歧义。
#       密码长度上限同时由 schemas 层限制为 64 字符，双保险。
_CRYPT_CONTEXT = CryptContext(
    schemes=["bcrypt"],
    deprecated="auto",
    bcrypt__rounds=settings.BCRYPT_ROUNDS,
    bcrypt__truncate_error=True,
)

# bcrypt 算法对密码的最大处理长度（字节）
BCRYPT_MAX_PASSWORD_BYTES = 72


def _to_bcrypt_safe_bytes(password: str) -> bytes:
    """
    将密码字符串转换为 bcrypt 可安全处理的字节串

    参数：
        password: 明文密码

    返回：
        bytes：UTF-8 编码后的字节串

    异常：
        ValueError：密码为空或超过 72 字节时抛出

    说明：
        单独做长度前置校验，是为了在进入 passlib 之前就给出
        明确的中文错误信息，而不是依赖 passlib 的英文异常。
        同时防止"中文密码按字符数看着不长、按字节数已超限"的误判。
    """
    if not password:
        raise ValueError("密码不能为空")
    encoded = password.encode("utf-8")
    if len(encoded) > BCRYPT_MAX_PASSWORD_BYTES:
        raise ValueError(
            f"密码过长：bcrypt 最多处理 {BCRYPT_MAX_PASSWORD_BYTES} 字节，"
            f"当前为 {len(encoded)} 字节。请缩短密码（中文字符每个占 3 字节）"
        )
    return encoded


def hash_password(password: str) -> str:
    """
    对明文密码做 bcrypt 哈希

    参数：
        password: 明文密码

    返回：
        str：bcrypt 哈希串，形如 $2b$12$...</...>（60 字符）

    异常：
        ValueError：密码为空或超过 72 字节时抛出

    说明：
        bcrypt 每次哈希会自动生成随机盐，因此同一密码两次哈希结果不同，
        这是预期行为。校验必须通过 verify_password，不能比较哈希串是否相等。

    使用示例：
        hashed = hash_password("123456")
        # '$2b$12$iJEwP.Ogt/5O2VReeYGgO.4FaMxXR95wGTwSc3iT30vunz.OIkOg6'
    """
    _to_bcrypt_safe_bytes(password)
    hashed = _CRYPT_CONTEXT.hash(password)
    return hashed


def verify_password(plain_password: str, password_hash: str) -> bool:
    """
    校验明文密码与 bcrypt 哈希是否匹配

    参数：
        plain_password: 用户提交的明文密码
        password_hash:  数据库中存储的 bcrypt 哈希串

    返回：
        bool：匹配返回 True，不匹配或哈希串非法返回 False

    说明：
        1. 任何异常（哈希格式损坏、算法未注册等）都被吞掉并返回 False，
           因为对调用方而言"校验不出来"等价于"密码不对"，
           抛出异常会污染登录链路的错误语义（可能误报为 500）。
        2. 使用常量时间比较，不通过提前返回泄露匹配进度。

    使用示例：
        verify_password("123456", user.password_hash)   # True
        verify_password("wrong",  user.password_hash)   # False
    """
    if not plain_password or not password_hash:
        return False
    try:
        return _CRYPT_CONTEXT.verify(plain_password, password_hash)
    except (UnknownHashError, ValueError, TypeError) as exc:
        # 哈希串损坏或格式不被识别：记录告警但不抛出，
        # 让登录取证链路继续按"密码错误"处理，同时暴露数据异常线索。
        logger.warning("密码校验失败（哈希串异常）| 原因=%s", exc)
        return False


def needs_rehash(password_hash: str) -> bool:
    """
    判断已有哈希是否需要按当前配置重新计算

    参数：
        password_hash: 数据库中的哈希串

    返回：
        bool：需要升级返回 True

    说明：
        典型场景是调高了 BCRYPT_ROUNDS，或未来迁移到更强算法。
        登录成功后若本函数返回 True，应静默重算并落库，
        使存量账号的哈希强度随配置演进自动提升。
    """
    if not password_hash:
        return False
    try:
        return _CRYPT_CONTEXT.needs_update(password_hash)
    except Exception:  # noqa: BLE001 —— 哈希不可识别时同样视为需重算
        return True


# ======================================================================
# 二、JWT 载荷字段常量
# ======================================================================
# 统一字段名，避免各处手写字符串出现拼写不一致（如 user_id / userId）
CLAIM_SUBJECT = "sub"          # 主体：用户 id
CLAIM_USERNAME = "username"    # 用户名
CLAIM_ROLE = "role"            # 角色标识
CLAIM_TYPE = "type"            # 令牌类型：access / refresh
CLAIM_VER = "ver"              # 令牌版本号，用于撤销
CLAIM_JTI = "jti"              # 令牌唯一 id，便于日志追踪与黑名单扩展
CLAIM_ISSUER = "iss"           # 签发方
CLAIM_ISSUED_AT = "iat"        # 签发时间
CLAIM_EXPIRE_AT = "exp"        # 过期时间
CLAIM_NOT_BEFORE = "nbf"       # 生效时间


def _now_utc() -> datetime:
    """
    获取当前 UTC 时间

    返回：
        datetime：带时区信息的 UTC 当前时间

    说明：
        JWT 规范要求 exp/iat/nbf 以 UTC 秒数表示。统一从此函数取时间，
        杜绝使用 datetime.now()（本地时区）导致令牌提前或延后 8 小时失效。
    """
    return datetime.now(timezone.utc)


def create_access_token(
    user_id: str,
    username: str,
    role: str,
    token_version: int = 1,
    expires_minutes: Optional[int] = None,
) -> str:
    """
    签发访问令牌（access_token）

    参数：
        user_id:        用户 id
        username:       用户名
        role:           角色标识（business_user / law_user / admin）
        token_version:  用户当前令牌版本号，用于服务端撤销校验
        expires_minutes: 有效期（分钟），缺省取配置 JWT_EXPIRE_MINUTES

    返回：
        str：编码后的 JWT 字符串

    异常：
        ValueError：user_id 为空时抛出

    使用示例：
        token = create_access_token("65f...", "user_law", "law_user")
    """
    if not user_id:
        raise ValueError("签发令牌失败：user_id 不能为空")
    return _build_token(
        user_id=user_id,
        username=username,
        role=role,
        token_type=TokenType.ACCESS,
        token_version=token_version,
        expires_minutes=(
            expires_minutes if expires_minutes is not None else settings.JWT_EXPIRE_MINUTES
        ),
    )


def create_refresh_token(
    user_id: str,
    username: str,
    role: str,
    token_version: int = 1,
    expires_minutes: Optional[int] = None,
) -> str:
    """
    签发刷新令牌（refresh_token）

    参数：
        user_id:        用户 id
        username:       用户名
        role:           角色标识
        token_version:  用户当前令牌版本号
        expires_minutes: 有效期（分钟），缺省取配置 JWT_REFRESH_EXPIRE_MINUTES

    返回：
        str：编码后的 JWT 字符串

    说明：
        刷新令牌同样携带角色信息，换发新 access_token 时可直接复用，
        无需再次查询数据库（但 token_version 仍需比对，保证撤销能力）。
    """
    if not user_id:
        raise ValueError("签发令牌失败：user_id 不能为空")
    return _build_token(
        user_id=user_id,
        username=username,
        role=role,
        token_type=TokenType.REFRESH,
        token_version=token_version,
        expires_minutes=(
            expires_minutes
            if expires_minutes is not None
            else settings.JWT_REFRESH_EXPIRE_MINUTES
        ),
    )


def create_token_pair(
    user_id: str,
    username: str,
    role: str,
    token_version: int = 1,
) -> Dict[str, Any]:
    """
    一次性签发访问令牌与刷新令牌（登录接口主入口）

    参数：
        user_id:       用户 id
        username:      用户名
        role:          角色标识
        token_version: 用户当前令牌版本号

    返回：
        Dict[str, Any]：包含以下键
            access_token:  str  访问令牌
            refresh_token: str  刷新令牌
            token_type:    str  固定为 "Bearer"，供前端拼装请求头
            expires_in:    int  访问令牌有效期（秒），前端据此安排静默刷新
            refresh_expires_in: int 刷新令牌有效期（秒）

    使用示例：
        pair = create_token_pair("65f...", "user_law", "law_user")
        pair["access_token"], pair["expires_in"]
    """
    access_token = create_access_token(user_id, username, role, token_version)
    refresh_token = create_refresh_token(user_id, username, role, token_version)
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": settings.AUTH_TOKEN_PREFIX,
        "expires_in": settings.jwt_access_expire_seconds,
        "refresh_expires_in": settings.jwt_refresh_expire_seconds,
    }


def _build_token(
    user_id: str,
    username: str,
    role: str,
    token_type: TokenType,
    token_version: int,
    expires_minutes: int,
) -> str:
    """
    构造并签名 JWT（内部公共实现）

    参数：
        user_id:         用户 id
        username:        用户名
        role:            角色标识
        token_type:      令牌类型枚举
        token_version:   令牌版本号
        expires_minutes: 有效期（分钟）

    返回：
        str：签名后的 JWT 字符串

    异常：
        ValueError：expires_minutes 非正数时抛出

    说明：
        access 与 refresh 的唯一差异是 type 声明与有效期，
        因此合并为一个内部函数，避免两处逻辑漂移。
    """
    if expires_minutes <= 0:
        raise ValueError(f"令牌有效期必须为正数，当前为 {expires_minutes}")

    issued_at = _now_utc()
    expire_at = issued_at + timedelta(minutes=expires_minutes)

    payload: Dict[str, Any] = {
        CLAIM_SUBJECT: str(user_id),
        CLAIM_USERNAME: username,
        CLAIM_ROLE: role,
        CLAIM_TYPE: token_type.value,
        CLAIM_VER: token_version,
        # jti 使用 uuid4，保证同一用户同一秒签发的多个令牌互不相同，
        # 便于日志检索与后续扩展"精确吊销单个令牌"能力
        CLAIM_JTI: str(uuid.uuid4()),
        CLAIM_ISSUER: settings.JWT_ISSUER,
        CLAIM_ISSUED_AT: int(issued_at.timestamp()),
        CLAIM_NOT_BEFORE: int(issued_at.timestamp()),
        CLAIM_EXPIRE_AT: int(expire_at.timestamp()),
    }

    return jwt.encode(
        payload,
        settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )


def decode_token(
    token: str,
    expected_type: Optional[TokenType] = None,
    verify_expire: bool = True,
) -> Dict[str, Any]:
    """
    解码并校验 JWT

    参数：
        token:         待解码的 JWT 字符串
        expected_type: 期望的令牌类型；传入后会强制校验 type 声明，
                       防止用 refresh_token 越权访问业务接口
        verify_expire: 是否校验有效期。正常鉴权为 True；
                       仅在做诊断/调试时可为 False，业务代码不得传 False

    返回：
        Dict[str, Any]：解码后的载荷字典

    异常：
        UnauthorizedException：
            - 令牌为空或格式非法 → TOKEN_INVALID
            - 签名不匹配 / 被篡改 → TOKEN_INVALID
            - 已过期             → TOKEN_EXPIRED
            - 令牌类型不匹配     → TOKEN_TYPE_INVALID
            - 签发方不匹配       → TOKEN_INVALID

    使用示例：
        payload = decode_token(token, expected_type=TokenType.ACCESS)
        payload["sub"], payload["role"]
    """
    if not token or not isinstance(token, str):
        raise UnauthorizedException(
            message="未提供有效的访问凭证",
            code=ErrorCode.TOKEN_MISSING,
        )

    options = {
        # 显式关闭 exp 校验以便自行区分"过期"与"非法"两种错误，
        # 给出更精确的错误码供前端决定"静默刷新"还是"跳登录页"
        "verify_exp": False,
        "verify_iss": True,
        "require": [CLAIM_SUBJECT, CLAIM_EXPIRE_AT, CLAIM_TYPE],
    }

    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET_KEY,
            # 【关键】算法必须显式指定为白名单，禁止使用令牌头部的 alg 字段，
            # 否则攻击者可伪造 alg=none 的令牌绕过签名校验
            algorithms=[settings.JWT_ALGORITHM],
            issuer=settings.JWT_ISSUER,
            options=options,
        )
    except jwt.ExpiredSignatureError as exc:
        raise UnauthorizedException(
            message="登录状态已过期，请重新登录",
            code=ErrorCode.TOKEN_EXPIRED,
            detail=str(exc),
        ) from exc
    except jwt.InvalidIssuerError as exc:
        raise UnauthorizedException(
            message="访问凭证来源非法",
            code=ErrorCode.TOKEN_INVALID,
            detail=str(exc),
        ) from exc
    except jwt.InvalidTokenError as exc:
        # 覆盖签名不匹配、载荷缺失、格式错误等全部 InvalidTokenError 子类
        raise UnauthorizedException(
            message="访问凭证非法或已被篡改，请重新登录",
            code=ErrorCode.TOKEN_INVALID,
            detail=str(exc),
        ) from exc

    # ---- 手动校验有效期（区分过期与非法）----
    if verify_expire:
        expire_at = payload.get(CLAIM_EXPIRE_AT)
        if expire_at is None:
            raise UnauthorizedException(
                message="访问凭证缺少有效期声明",
                code=ErrorCode.TOKEN_INVALID,
            )
        if int(expire_at) <= int(_now_utc().timestamp()):
            raise UnauthorizedException(
                message="登录状态已过期，请重新登录",
                code=ErrorCode.TOKEN_EXPIRED,
            )

    # ---- 校验令牌类型 ----
    if expected_type is not None:
        actual_type = payload.get(CLAIM_TYPE)
        if actual_type != expected_type.value:
            logger.warning(
                "令牌类型不匹配 | 期望=%s | 实际=%s", expected_type.value, actual_type
            )
            raise UnauthorizedException(
                message="访问凭证类型不正确，请使用访问令牌访问业务接口",
                code=ErrorCode.TOKEN_TYPE_INVALID,
            )

    return payload


def extract_bearer_token(
    authorization_header: Optional[str],
    header_name: Optional[str] = None,
    prefix: Optional[str] = None,
) -> str:
    """
    从请求头中提取 Bearer 令牌

    参数：
        authorization_header: 请求头原始值，形如 "Bearer eyJhbGci..."
        header_name:          请求头名称，缺省取配置 AUTH_TOKEN_HEADER_NAME（仅用于错误提示）
        prefix:               令牌前缀，缺省取配置 AUTH_TOKEN_PREFIX

    返回：
        str：提取出的令牌字符串

    异常：
        UnauthorizedException：
            - 请求头为空            → TOKEN_MISSING
            - 请求头格式非法        → TOKEN_INVALID
            - 前缀不是 Bearer       → TOKEN_INVALID

    说明：
        前缀比较使用大小写不敏感方式（HTTP 头部的 scheme 是大小写不敏感的），
        但返回值中的令牌本身严格保持原样，不做任何大小写转换。
    """
    expected_prefix = prefix or settings.AUTH_TOKEN_PREFIX
    if not authorization_header or not authorization_header.strip():
        raise UnauthorizedException(
            message="未登录，请在请求头中携带访问凭证",
            code=ErrorCode.TOKEN_MISSING,
        )

    raw = authorization_header.strip()
    parts = raw.split()

    if len(parts) != 2:
        raise UnauthorizedException(
            message=f"访问凭证格式非法，正确格式为 '{expected_prefix} <token>'",
            code=ErrorCode.TOKEN_INVALID,
        )

    scheme, token = parts[0], parts[1]
    if scheme.lower() != expected_prefix.lower():
        raise UnauthorizedException(
            message=f"访问凭证前缀非法，应为 '{expected_prefix}'",
            code=ErrorCode.TOKEN_INVALID,
        )

    if not token:
        raise UnauthorizedException(
            message="访问凭证为空",
            code=ErrorCode.TOKEN_MISSING,
        )

    return token


# ======================================================================
# 三、辅助工具
# ======================================================================
def build_token_fingerprint(token: str) -> str:
    """
    计算令牌指纹（用于日志记录）

    参数：
        token: JWT 字符串

    返回：
        str：令牌的 SHA-256 前 16 位十六进制摘要

    说明：
        日志中绝不能记录完整令牌（等于泄露凭证），
        记录指纹既能用于关联同一令牌的多次请求，又不泄露可用信息。
        该指纹不可逆，且因令牌本身高熵，不存在彩虹表还原风险。
    """
    if not token:
        return "empty"
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return digest[:16]


def generate_random_secret(length: int = 48) -> str:
    """
    生成强随机密钥（用于替换默认 JWT_SECRET_KEY）

    参数：
        length: 密钥长度（字符数），默认 48

    返回：
        str：URL 安全的高熵随机字符串

    说明：
        部署时可用本函数生成密钥并写入 .env：
            python -c "from app.core.security import generate_random_secret; print(generate_random_secret())"
        使用 secrets 模块（密码学安全随机源）而非 random。
    """
    return secrets.token_urlsafe(length)


def get_token_expire_at(token: str) -> Optional[datetime]:
    """
    获取令牌的过期时间（不校验签名与有效期）

    参数：
        token: JWT 字符串

    返回：
        Optional[datetime]：过期时间（UTC）；无法解析时返回 None

    说明：
        仅用于展示与诊断（如"当前登录将于 x 时间过期"），
        因为不校验签名，其结果不可用于任何鉴权决策。
    """
    try:
        payload = jwt.decode(token, options={"verify_signature": False})
        expire_at = payload.get(CLAIM_EXPIRE_AT)
        if expire_at is None:
            return None
        return datetime.fromtimestamp(int(expire_at), tz=timezone.utc)
    except Exception:  # noqa: BLE001 —— 诊断用途，解析失败返回 None 即可
        return None
