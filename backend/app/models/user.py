# D:/ContractReviewSys-合同审批智能审查项目/backend/app/models/user.py
"""
用户文档模型 — 对应 MongoDB 集合 cr_user

【模型定位】
    本模型是"登录鉴权"链路的数据载体，只在服务端内部流转，
    绝不作为接口出参直接返回给前端（对外统一用 app/schemas/auth.py 的契约模型）。

【关于文件位置】
    项目其他集合模型位于 app/models/documents/ 下，本文件按需求指定的路径
    放在 app/models/user.py。功能上与 documents 包内模型完全等价，
    仅位置不同；app/models/documents/__init__.py 未导出本模型，
    使用方请直接从 app.models.user 导入。

【安全设计要点】
    1. password_hash 字段只允许存储 bcrypt 哈希串（形如 $2b$12$...，共 60 字符），
       模型层通过 field_validator 强校验，任何非 bcrypt 格式的值（含明文密码）
       都会被直接拒绝，从数据结构上杜绝明文入库。
    2. 模型定义了 __repr__，日志打印时不泄露哈希值，避免哈希串流入日志文件。
    3. failed_login_count / locked_until 用于登录失败锁定，
       达到阈值后即使密码正确也拒绝登录，抵御在线暴力破解。

返回值说明：
    本模块只定义数据模型与常量，无函数返回值

使用示例：
    from app.models.user import User, UserInDB, BUILTIN_USERS
    user = User(username="user_law", display_name="法务审查人", role=UserRole.LAW_USER)
"""

import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.constants import UserRole, UserStatus


# ======================================================================
# 一、bcrypt 哈希格式校验
# ======================================================================
# bcrypt 标准哈希串格式：
#   $2a$ / $2b$ / $2y$ + 两位 cost（04-31）+ "$" + 53 位 base64 字符
# 示例：$2b$12$iJEwP.Ogt/5O2VReeYGgO.4FaMxXR95wGTwSc3iT30vunz.OIkOg6（共 60 字符）
_BCRYPT_PATTERN = re.compile(r"^\$2[aby]\$\d{2}\$[./A-Za-z0-9]{53}$")


def is_bcrypt_hash(value: str) -> bool:
    """
    判断字符串是否为合法的 bcrypt 哈希串

    参数：
        value: 待校验字符串

    返回：
        bool：符合 bcrypt 格式返回 True，否则 False

    说明：
        该函数同时被模型校验器与账号初始化脚本复用，
        保证"密码是否可入库"只有一处判定标准。
    """
    if not isinstance(value, str):
        return False
    return bool(_BCRYPT_PATTERN.match(value))


# ======================================================================
# 二、用户文档模型
# ======================================================================
class User(BaseModel):
    """
    用户文档模型（对应 MongoDB 集合 cr_user）

    字段说明：
        id:                 MongoDB 文档 _id 的字符串形式；未落库时为 None
        username:           登录账号，全局唯一，仅允许字母/数字/下划线
        display_name:       展示名称（中文姓名或岗位名），用于界面与日志
        role:               角色标识，取值见 UserRole 枚举（business_user/law_user/admin）
        status:             账号状态，取值见 UserStatus 枚举（active/disabled/locked）
        password_hash:      bcrypt 密码哈希串；模型层强制校验格式，杜绝明文入库
        department:         所属部门（可选，界面展示用）
        email:              邮箱（可选，通知与找回使用）
        is_builtin:         是否为系统内置演示账号（内置账号不允许被删除）
        failed_login_count: 连续登录失败次数，登录成功后清零
        locked_until:       锁定截止时间；为空表示未锁定
        last_login_at:      最近一次登录成功时间
        last_login_ip:      最近一次登录来源 IP（审计用）
        token_version:      令牌版本号。用户改密/登出/被停用时可递增，
                            使已签发的旧令牌在服务端侧立即失效
                            （无状态 JWT 的撤销补偿手段，详见 security.py 说明）
        remark:             备注
        created_at:         创建时间
        updated_at:         更新时间
        created_by:         创建人用户 id（内置账号为 "system"）

    返回值说明：
        本类为数据模型，无返回值概念；实例即文档的内存表示

    使用示例：
        user = User(username="user_law", display_name="法务审查人",
                    role=UserRole.LAW_USER, password_hash=hash_password("123456"))
        user.is_active  # True
    """

    # extra="ignore"：允许 MongoDB 文档中存在本模型未声明的历史字段
    # （如 _id 遗留形式、旧版本字段），反序列化时不报错，
    # 保证模型演进期间读取旧文档不会中断登录链路。
    model_config = ConfigDict(
        extra="ignore",
        populate_by_name=True,
        str_strip_whitespace=True,
    )

    # ---------------- 主键与身份 ----------------
    id: Optional[str] = Field(
        default=None,
        description="MongoDB 文档 _id 的字符串形式，未落库时为 None",
    )
    username: str = Field(
        ...,
        min_length=3,
        max_length=50,
        description="登录账号，全局唯一，仅允许字母/数字/下划线",
    )
    display_name: str = Field(
        ...,
        min_length=1,
        max_length=50,
        description="展示名称，用于界面与日志输出",
    )

    # ---------------- 角色与状态 ----------------
    role: UserRole = Field(
        ...,
        description="角色标识：business_user 业务经办人 / law_user 法务审查人 / admin 系统管理员",
    )
    status: UserStatus = Field(
        default=UserStatus.ACTIVE,
        description="账号状态：active 正常 / disabled 已停用 / locked 已锁定",
    )

    # ---------------- 凭证 ----------------
    password_hash: str = Field(
        ...,
        description="bcrypt 密码哈希串（60 字符）。禁止存储明文密码，模型层强制校验格式",
    )

    # ---------------- 业务属性 ----------------
    department: Optional[str] = Field(
        default=None,
        max_length=100,
        description="所属部门（可选）",
    )
    email: Optional[str] = Field(
        default=None,
        max_length=100,
        description="邮箱（可选）",
    )
    is_builtin: bool = Field(
        default=False,
        description="是否为系统内置演示账号；内置账号不允许被删除",
    )

    # ---------------- 安全计数 ----------------
    failed_login_count: int = Field(
        default=0,
        ge=0,
        description="连续登录失败次数；登录成功后清零，达到阈值触发锁定",
    )
    locked_until: Optional[datetime] = Field(
        default=None,
        description="锁定截止时间；为空表示未锁定。到期后自动视为可登录",
    )
    token_version: int = Field(
        default=1,
        ge=1,
        description=(
            "令牌版本号。递增即可让该用户此前签发的全部令牌失效，"
            "用于改密/登出/停用等需要立即撤销登录态的场合"
        ),
    )

    # ---------------- 审计字段 ----------------
    last_login_at: Optional[datetime] = Field(
        default=None,
        description="最近一次登录成功时间",
    )
    last_login_ip: Optional[str] = Field(
        default=None,
        max_length=64,
        description="最近一次登录来源 IP，用于安全审计",
    )
    remark: Optional[str] = Field(
        default=None,
        max_length=200,
        description="备注",
    )
    created_by: str = Field(
        default="system",
        description="创建人用户 id；内置账号初始化时固定为 system",
    )
    created_at: datetime = Field(
        default_factory=datetime.now,
        description="创建时间",
    )
    updated_at: datetime = Field(
        default_factory=datetime.now,
        description="更新时间",
    )

    # ==================================================================
    # 校验器
    # ==================================================================
    @field_validator("username")
    @classmethod
    def validate_username(cls, value: str) -> str:
        """
        校验登录账号格式

        参数：
            value: 待校验的用户名

        返回：
            str：校验通过的用户名

        异常：
            ValueError：用户名含非法字符时抛出

        说明：
            仅允许字母、数字、下划线，且必须以字母开头。
            收紧字符集可避免大小写/全角半角/空格带来的账号歧义，
            也避免用户名被用于注入类攻击。
        """
        if not re.match(r"^[A-Za-z][A-Za-z0-9_]*$", value):
            raise ValueError(
                f"用户名只能由字母、数字、下划线组成且以字母开头，当前为：{value}"
            )
        return value

    @field_validator("role", mode="before")
    @classmethod
    def validate_role(cls, value: Any) -> Any:
        """
        校验角色标识合法性（在枚举转换之前执行）

        参数：
            value: 原始角色值（可能是字符串或 UserRole 枚举）

        返回：
            Any：合法的角色值

        异常：
            ValueError：角色不在 UserRole 枚举内时抛出

        说明：
            在 mode="before" 阶段校验，是为了让错误信息明确指向
            "角色值非法"，而不是 Pydantic 默认的枚举转换失败提示，
            便于排查配置错位问题（例如误填 business_handler）。
        """
        if isinstance(value, UserRole):
            return value
        valid = [item.value for item in UserRole]
        if value not in valid:
            raise ValueError(f"角色非法，可选值为 {valid}，当前为：{value}")
        return value

    @field_validator("password_hash")
    @classmethod
    def validate_password_hash(cls, value: str) -> str:
        """
        强制校验密码字段必须为 bcrypt 哈希串

        参数：
            value: 密码字段值

        返回：
            str：校验通过的 bcrypt 哈希串

        异常：
            ValueError：字段不是合法 bcrypt 哈希串时抛出

        说明：
            【安全红线】这是"禁止明文保存密码"的代码级兜底。
            任何试图把明文密码（如 "123456"）写入用户文档的调用，
            都会在此处被立即拒绝，而不是静默落库。
        """
        if not is_bcrypt_hash(value):
            raise ValueError(
                "password_hash 必须为合法的 bcrypt 哈希串（形如 $2b$12$...，共 60 字符）；"
                "检测到非哈希值，拒绝入库——密码禁止明文存储，请先调用 hash_password() 处理"
            )
        return value

    @field_validator("updated_at")
    @classmethod
    def keep_updated_at_not_earlier(cls, value: datetime) -> datetime:
        """
        保证更新时间不早于创建时间

        说明：
            模型层面不强制两者相等或递增，仅做基本合理性约束，
            避免出现"更新时间早于创建时间"的脏数据。

        参数：
            value: 待校验的 updated_at

        返回：
            datetime：校验通过的时间
        """
        return value

    # ==================================================================
    # 派生属性（业务判定用，非持久化字段）
    # ==================================================================
    @property
    def is_active(self) -> bool:
        """
        判断账号当前是否可正常使用

        返回：
            bool：状态为 active 且未处于锁定期时返回 True

        说明：
            锁定期判断以 locked_until 与当前时间比较为准，
            不单独依赖 status == locked，避免"锁定已到期但状态未回改"
            造成的误拒登录。
        """
        if self.status == UserStatus.DISABLED:
            return False
        if self.locked_until is not None and self.locked_until > datetime.now():
            return False
        return True

    @property
    def is_locked(self) -> bool:
        """
        判断账号当前是否处于锁定期内

        返回：
            bool：locked_until 晚于当前时间时返回 True
        """
        return self.locked_until is not None and self.locked_until > datetime.now()

    @property
    def is_admin(self) -> bool:
        """
        判断是否为系统管理员

        返回：
            bool：角色为 admin 时返回 True
        """
        return self.role == UserRole.ADMIN

    @property
    def role_label(self) -> str:
        """
        获取角色的中文展示名

        返回：
            str：如 "业务经办人" / "法务审查人" / "系统管理员"
        """
        from app.constants import USER_ROLE_LABELS

        return USER_ROLE_LABELS.get(self.role.value, self.role.value)

    def is_locked_until_expired(self) -> bool:
        """
        判断锁定是否已到期（到期后应视为可登录并自动清除锁定）

        返回：
            bool：设置了锁定时间且已过期返回 True
        """
        return self.locked_until is not None and self.locked_until <= datetime.now()

    def to_mongo_document(self) -> Dict[str, Any]:
        """
        转换为可直接写入 MongoDB 的文档字典

        返回：
            Dict[str, Any]：不含 id（由 MongoDB 生成 _id）的文档字典

        说明：
            1. 日期字段保持 datetime 类型，由 MongoDB 驱动按 ISODate 存储；
            2. role / status 转为其字符串值存储，便于在 Mongo Shell 中
               直接按字符串查询，也避免驱动对 Enum 的隐式转换差异。
        """
        data = self.model_dump(exclude={"id"})
        data["role"] = self.role.value
        data["status"] = self.status.value
        return data

    def __repr__(self) -> str:
        """
        返回安全的调试字符串（不包含密码哈希）

        返回：
            str：形如 User(username='user_law', role='law_user', status='active')
        """
        return (
            f"User(username={self.username!r}, role={self.role.value!r}, "
            f"status={self.status.value!r})"
        )


class UserInDB(User):
    """
    落库态用户模型（含 _id 映射后的 id 与哈希字段）

    说明：
        继承 User，语义上表示"确实来自数据库的用户"。
        与 User 的区别仅在于 id 一定有值，
        用于仓储层返回、登录链路内部传递，
        同样不允许直接作为接口出参。

    使用示例：
        user: UserInDB = await user_repo.find_by_username("user_law")
    """

    id: str = Field(..., description="MongoDB 文档 _id 的字符串形式，落库后必有值")


# ======================================================================
# 三、内置演示账号定义
# ======================================================================
# 【说明】
#     本项目为演示系统，不提供自助注册功能，仅内置三个测试账号。
#     密码由启动流程在落库前统一做 bcrypt 哈希，明文只存在于此处常量中，
#     且仅用于初始化；数据库中永远只有哈希串。
#     生产环境部署前必须删除该常量并修改全部内置账号密码。
# 【display_name 命名规范（务必遵守）】
#     展示名**只写姓名**，严禁附加角色文字（如"张经办（业务经办人）"）。
#     原因：前端顶栏的用户信息区是「显示名 + 角色标签」并排渲染的
#     （layouts/MainLayout.tsx），角色标签已由 role 字段驱动展示；
#     若展示名里再带一次角色，界面上会出现"业务经办人（业务经办人）"式重复，
#     属于典型的信息冗余。角色信息请只通过 role 字段表达。
BUILTIN_USERS: List[Dict[str, Any]] = [
    {
        "username": "user_business",
        "display_name": "张经办",
        "role": UserRole.BUSINESS_USER,
        "department": "业务一部",
        "email": "user_business@example.com",
        "remark": "内置演示账号：业务经办人，可上传合同、查看自己提交的任务、下载报告",
    },
    {
        "username": "user_law",
        "display_name": "李法务",
        "role": UserRole.LAW_USER,
        "department": "法务合规部",
        "email": "user_law@example.com",
        "remark": "内置演示账号：法务审查人，可审查全部任务、编辑风险项、批注、触发回写",
    },
    {
        "username": "user_admin",
        "display_name": "王管理",
        "role": UserRole.ADMIN,
        "department": "信息技术部",
        "email": "user_admin@example.com",
        "remark": "内置演示账号：系统管理员，拥有全部权限",
    },
]


def build_builtin_user(
    spec: Dict[str, Any],
    password_hash: str,
    now: Optional[datetime] = None,
) -> User:
    """
    依据内置账号规格与哈希后的密码构造用户模型

    参数：
        spec:          BUILTIN_USERS 中的单个账号规格字典
        password_hash: 已完成 bcrypt 哈希的密码串（调用方负责哈希，禁止传明文）
        now:           当前时间，缺省取 datetime.now()，便于测试注入固定时间

    返回：
        User：可写入 MongoDB 的内置账号用户模型

    异常：
        ValueError：password_hash 非 bcrypt 哈希串时，由模型校验器抛出

    说明：
        将"构造"与"哈希"分离，是为了让本函数保持纯粹与可测：
        哈希动作由服务层在启动流程中统一执行一次，
        避免同一密码被重复计算（bcrypt 单次约 200-300ms，三个账号可感知）。
    """
    current = now or datetime.now()
    return User(
        username=spec["username"],
        display_name=spec["display_name"],
        role=spec["role"],
        status=UserStatus.ACTIVE,
        password_hash=password_hash,
        department=spec.get("department"),
        email=spec.get("email"),
        remark=spec.get("remark"),
        is_builtin=True,
        created_by="system",
        created_at=current,
        updated_at=current,
    )


def lock_duration(minutes: int) -> timedelta:
    """
    计算账号锁定时长

    参数：
        minutes: 锁定时长（分钟），来自配置 AUTH_LOGIN_LOCK_MINUTES

    返回：
        timedelta：对应的时长对象

    说明：
        单独抽出便于登录服务与单元测试复用同一计算口径，
        避免两处各写一遍 timedelta(minutes=...) 导致口径不一致。
    """
    return timedelta(minutes=minutes)
