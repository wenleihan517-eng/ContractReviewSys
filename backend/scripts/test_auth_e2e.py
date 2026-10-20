# D:/ContractReviewSys-合同审批智能审查项目/backend/scripts/test_auth_e2e.py
"""
登录鉴权模块端到端验证脚本

【脚本定位】
    本脚本对登录鉴权链路做完整的端到端验证，覆盖从"账号初始化"到
    "登录 → 鉴权 → 刷新 → 登出 → 权限校验"的全过程。

【为什么不用 HTTP 调用真实服务？】
    本机环境缺少 MongoDB（未安装 mongod），服务无法完成 lifespan 启动。
    因此本脚本采用"内存假集合 + 直调服务层/依赖层"的方式验证：

        1. 用一个内存字典实现 motor 集合所需的最小异步接口
           （find_one / insert_one / update_one / find_one_and_update /
             count_documents / find / create_index），
           通过替换 mongo_client.get_collection 注入到仓储层。
        2. 直接调用 auth_service 与 dependencies，验证业务逻辑正确性。
        3. 中间件与接口层通过 FastAPI TestClient 验证（使用注入后的集合）。

    这样验证的是"与生产完全相同的业务代码"，仅替换了数据库驱动这一层，
    等价于集成测试的效果。

【覆盖范围】
    A. 内置账号初始化（幂等、密码哈希落库、明文不落库）
    B. 登录成功（三种角色）、令牌内容正确
    C. 登录失败（密码错误、账号不存在、账号停用、账号锁定）
    D. 账号枚举防护（两种失败返回相同错误码）
    E. 鉴权依赖（正确令牌 / 伪造令牌 / 过期令牌 / 刷新令牌越权）
    F. 权限校验（三角色权限矩阵、水平越权防护）
    G. 令牌刷新与轮换
    H. 登出撤销（旧令牌立即失效）
    I. 鉴权中间件（白名单放行、未带令牌 401、路径穿越拒绝）
    J. 密码变更后旧令牌失效

返回值说明：
    运行后输出测试结果矩阵；全部通过时退出码为 0，否则为 1

使用示例：
    cd backend
    "C:/Users/Lenovo/.workbuddy/binaries/python/envs/default/Scripts/python.exe" scripts/test_auth_e2e.py
"""

import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

# 将 backend 目录加入模块搜索路径，保证可直接以脚本方式运行
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


# ======================================================================
# 测试结果统计
# ======================================================================
class Result:
    """测试结果收集器"""

    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0
        self.details: List[str] = []

    def check(self, label: str, condition: bool, extra: str = "") -> None:
        """
        记录一条断言结果

        参数：
            label:     断言描述
            condition: 断言条件
            extra:     附加说明
        """
        if condition:
            self.passed += 1
            print("  [PASS] %s %s" % (label, extra))
        else:
            self.failed += 1
            detail = "%s %s" % (label, extra)
            self.details.append(detail)
            print("  [FAIL] %s %s" % (label, extra))

    def summary(self) -> int:
        """
        输出汇总并返回退出码

        返回：
            0 表示全部通过，1 表示存在失败
        """
        total = self.passed + self.failed
        print()
        print("=" * 74)
        print("测试汇总：%d/%d 通过" % (self.passed, total))
        if self.failed:
            print("失败项：")
            for item in self.details:
                print("   -", item)
        print("=" * 74)
        return 0 if self.failed == 0 else 1


R = Result()


# ======================================================================
# 一、内存假集合（模拟 motor 异步集合接口）
# ======================================================================
class _FakeCursor:
    """模拟 motor 的游标对象（仅实现本模块用到的接口）"""

    def __init__(self, docs: List[Dict[str, Any]]) -> None:
        self._docs = docs

    def sort(self, field: str, direction: int = 1) -> "_FakeCursor":
        """按字段排序"""
        try:
            self._docs.sort(key=lambda d: d.get(field) or datetime.min, reverse=(direction == -1))
        except TypeError:
            pass
        return self

    def skip(self, n: int) -> "_FakeCursor":
        """跳过前 n 条"""
        self._docs = self._docs[max(0, n):]
        return self

    def limit(self, n: int) -> "_FakeCursor":
        """限制返回条数"""
        self._docs = self._docs[:n]
        return self

    async def to_list(self, length: Optional[int] = None) -> List[Dict[str, Any]]:
        """返回文档列表"""
        return list(self._docs)


class _FakeCollection:
    """
    内存假集合 —— 实现 motor 异步集合的最小接口子集

    说明：
        仅实现仓储层实际用到的方法，行为对齐 MongoDB 语义：
            insert_one / find_one / update_one / find_one_and_update /
            count_documents / find / create_index
        特别地，$set / $setOnInsert / $inc 三种更新操作符与
        upsert、return_document、DuplicateKeyError 语义均需正确。
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self.docs: List[Dict[str, Any]] = []
        self.unique_fields: List[str] = []
        self._auto_id = 0

    # ---------------- 内部工具 ----------------
    def _next_id(self) -> Any:
        """
        生成 _id

        返回：
            bson.ObjectId：与真实 MongoDB 一致的 ObjectId 实例

        说明：
            必须使用真实的 ObjectId 而非字符串。原因是仓储层的
            find_by_id 会执行 ObjectId(user_id) 转换后再查询，
            若假集合存的是字符串，查询条件 {"_id": ObjectId(...)}
            永远匹配不上字符串 _id，会导致"令牌有效却报用户不存在"。
            这里刻意使用真实类型，保证测试替身与生产行为一致。
        """
        from bson import ObjectId

        self._auto_id += 1
        # 用递增数构造稳定的 ObjectId（便于断言），格式仍合法
        return ObjectId("%024x" % self._auto_id)

    @staticmethod
    def _match(doc: Dict[str, Any], query: Dict[str, Any]) -> bool:
        """
        判断文档是否命中查询条件

        参数：
            doc:   文档
            query: 查询条件，支持普通等值、$or、$ne、$lte

        返回：
            bool：命中返回 True

        说明：
            仅实现仓储层用到的操作符。特别注意 $ne / $lte 的语义必须正确，
            否则"清除过期锁定"这类逻辑会静默失效。
        """
        for key, expected in query.items():
            if key == "$or":
                if not any(_FakeCollection._match(doc, cond) for cond in expected):
                    return False
                continue

            value = doc.get(key)

            if isinstance(expected, dict):
                for op, operand in expected.items():
                    if op == "$ne":
                        if value == operand:
                            return False
                    elif op == "$lte":
                        # 与 MongoDB 一致：null 不满足 $lte 比较
                        if value is None or not (value <= operand):
                            return False
                    elif op == "$exists":
                        # $exists: False 表示字段缺失；True 表示字段存在
                        # 注意：MongoDB 中字段值为 null 也算"存在"
                        exists = key in doc
                        if bool(operand) != exists:
                            return False
                    elif op == "$gte":
                        if value is None or not (value >= operand):
                            return False
                    elif op == "$regex":
                        import re

                        flags = re.IGNORECASE if expected.get("$options") == "i" else 0
                        if value is None or not re.search(operand, str(value), flags):
                            return False
                    else:
                        raise NotImplementedError("假集合未实现操作符：%s" % op)
                continue

            if value != expected:
                return False
        return True

    def _apply_update(self, doc: Dict[str, Any], update: Dict[str, Any]) -> None:
        """对文档应用更新操作符"""
        if "$set" in update:
            for k, v in update["$set"].items():
                doc[k] = v
        if "$setOnInsert" in update:
            # 仅在插入时生效，此处由调用方区分，这里不处理
            pass
        if "$inc" in update:
            for k, v in update["$inc"].items():
                doc[k] = (doc.get(k) or 0) + v

    def _check_unique(self, doc: Dict[str, Any], exclude_id: Any = None) -> None:
        """检查唯一约束，冲突时抛出 DuplicateKeyError（延迟导入真实异常类）"""
        from pymongo.errors import DuplicateKeyError

        for field in self.unique_fields:
            for other in self.docs:
                if other.get("_id") == exclude_id:
                    continue
                if other.get(field) == doc.get(field):
                    raise DuplicateKeyError("E11000 duplicate key error: %s" % field)

    # ---------------- 异步接口 ----------------
    async def insert_one(self, doc: Dict[str, Any]) -> Any:
        """插入文档，返回含 inserted_id 的结果对象"""
        new_doc = dict(doc)
        new_doc.setdefault("_id", self._next_id())
        self._check_unique(new_doc)
        self.docs.append(new_doc)

        class _R:
            inserted_id = new_doc["_id"]

        return _R()

    async def find_one(self, query: Dict[str, Any], *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        """查询单条文档"""
        for doc in self.docs:
            if self._match(doc, query):
                return dict(doc)
        return None

    async def update_one(self, query: Dict[str, Any], update: Dict[str, Any]) -> Any:
        """更新单条文档，返回含 modified_count 的结果对象"""
        matched = 0
        modified = 0
        for doc in self.docs:
            if self._match(doc, query):
                matched += 1
                before = dict(doc)
                self._apply_update(doc, update)
                if doc != before:
                    modified += 1
                break

        class _R:
            matched_count = matched
            modified_count = modified

        return _R()

    async def find_one_and_update(
        self,
        query: Dict[str, Any],
        update: Dict[str, Any],
        upsert: bool = False,
        return_document: Any = None,
        **kwargs: Any,
    ) -> Optional[Dict[str, Any]]:
        """
        查找并更新（支持 upsert 与返回更新后文档）

        参数：
            query:           查询条件
            update:          更新操作
            upsert:          未命中时是否插入
            return_document: pymongo.ReturnDocument.AFTER 时返回更新后的文档

        返回：
            Optional[Dict]：更新后的文档或 None

        说明：
            upsert 分支必须正确实现 $setOnInsert 语义（仅插入时写入），
            否则内置账号初始化会反复重置密码。
        """
        from pymongo import ReturnDocument

        for doc in self.docs:
            if self._match(doc, query):
                self._apply_update(doc, update)
                return dict(doc) if return_document == ReturnDocument.AFTER else dict(doc)

        if not upsert:
            return None

        # ---- upsert：新建文档 ----
        new_doc: Dict[str, Any] = {}
        # 查询条件中的等值字段作为新建文档的初始值
        for k, v in query.items():
            if not k.startswith("$") and not isinstance(v, dict):
                new_doc[k] = v
        new_doc.setdefault("_id", self._next_id())
        # $setOnInsert 仅在插入时生效
        if "$setOnInsert" in update:
            new_doc.update(update["$setOnInsert"])
        # $set 同时生效
        if "$set" in update:
            new_doc.update(update["$set"])
        if "$inc" in update:
            for k, v in update["$inc"].items():
                new_doc[k] = (new_doc.get(k) or 0) + v

        self._check_unique(new_doc)
        self.docs.append(new_doc)
        return dict(new_doc)

    async def count_documents(self, query: Dict[str, Any]) -> int:
        """统计命中文档数"""
        return sum(1 for doc in self.docs if self._match(doc, query))

    def find(self, query: Dict[str, Any]) -> _FakeCursor:
        """查询多条（返回游标）"""
        return _FakeCursor([dict(d) for d in self.docs if self._match(d, query)])

    async def create_index(self, keys: Any, unique: bool = False, name: str = "", **kwargs: Any) -> str:
        """创建索引（记录唯一字段以支持唯一性校验）"""
        if unique:
            for item in keys:
                field = item[0] if isinstance(item, (list, tuple)) else item
                if field not in self.unique_fields:
                    self.unique_fields.append(field)
        return name or "idx_%s" % len(self.unique_fields)


class _FakeCollectionProxy:
    """
    集合代理 —— 让仓储层的 mongo_client.get_collection(name) 返回假集合

    说明：
        用 __getattr__ 把“取集合”转为“取内存集合”，
        从而无需修改仓储层任何一行代码即可注入测试替身。
        这是"依赖通过单一入口获取"这一设计带来的可测性收益。
    """

    def __init__(self) -> None:
        self._collections: Dict[str, _FakeCollection] = {}

    def __call__(self, name: str) -> _FakeCollection:
        """以函数方式取集合（与 get_collection 签名一致）"""
        if name not in self._collections:
            self._collections[name] = _FakeCollection(name)
        return self._collections[name]

    def reset(self) -> None:
        """清空全部集合（供测试用例之间隔离）"""
        self._collections.clear()


FAKE_STORE = _FakeCollectionProxy()


def install_fake_mongo() -> None:
    """
    把假集合注入到 storage 层的 mongo_client

    返回：
        无返回值

    说明：
        直接替换 mongo_client 实例的 get_collection 方法，
        使所有仓储层调用落到内存假集合上。
        仓储层通过 `from app.storage.mongo_client import mongo_client;
        mongo_client.get_collection(...)` 取集合，因此替换该方法即可全覆盖。
    """
    from app.storage.mongo_client import mongo_client

    mongo_client.get_collection = FAKE_STORE  # type: ignore[assignment]
    # find_by_id 会用到 ObjectId 解析，假集合的 _id 已是 24 位十六进制，可正常解析


# ======================================================================
# 二、测试用例
# ======================================================================
async def test_seed_builtin_users() -> None:
    """A. 内置账号初始化（幂等 + 密码哈希落库）"""
    print()
    print("A. 内置账号初始化")
    from app.repositories import user_repo
    from app.services import auth_service

    FAKE_STORE.reset()

    r1 = await auth_service.init_builtin_users()
    R.check("首次初始化创建 3 个账号", r1.created == 3, "created=%d" % r1.created)
    R.check("账号名列表正确", r1.usernames == ["user_business", "user_law", "user_admin"])

    # 幂等性：再次初始化不应新建
    r2 = await auth_service.init_builtin_users()
    R.check("二次初始化幂等（不新建）", r2.created == 0 and r2.existing == 3,
            "created=%d existing=%d" % (r2.created, r2.existing))

    # 落库密码必须是 bcrypt 哈希，绝不能是明文
    raw = FAKE_STORE("cr_user").docs
    R.check("数据库中共 3 个账号", len(raw) == 3, "count=%d" % len(raw))
    plaintexts = [d for d in raw if d.get("password_hash") == "123456"]
    R.check("【安全】明文密码未落库", not plaintexts)
    from app.models.user import is_bcrypt_hash

    all_hashed = all(is_bcrypt_hash(d.get("password_hash", "")) for d in raw)
    R.check("【安全】密码均为 bcrypt 哈希", all_hashed)

    # 角色值正确
    roles = {d["username"]: d["role"] for d in raw}
    R.check("角色映射正确", roles == {
        "user_business": "business_user",
        "user_law": "law_user",
        "user_admin": "admin",
    }, str(roles))

    # 密码可校验
    u = await user_repo.find_by_username("user_law")
    from app.core.security import verify_password

    R.check("内置密码 123456 可校验通过", verify_password("123456", u.password_hash))
    R.check("错误密码校验失败", not verify_password("654321", u.password_hash))

    # 幂等不应重置已改密码
    from app.core.security import hash_password

    new_hash = hash_password("NewPass123")
    await user_repo.update_password_hash("user_law", new_hash)
    await auth_service.init_builtin_users()
    u2 = await user_repo.find_by_username("user_law")
    R.check("【关键】重复初始化不重置已改密码", verify_password("NewPass123", u2.password_hash))
    R.check("旧密码已失效", not verify_password("123456", u2.password_hash))


async def test_login_success() -> None:
    """B. 登录成功（三种角色）"""
    print()
    print("B. 登录成功（三种角色）")
    from app.services import auth_service

    FAKE_STORE.reset()
    await auth_service.init_builtin_users()

    cases = [
        ("user_business", "business_user", "业务经办人", 3),
        ("user_law", "law_user", "法务审查人", 6),
        ("user_admin", "admin", "系统管理员", 11),
    ]
    for username, role, label, perm_count in cases:
        resp = await auth_service.login(username, "123456", "127.0.0.1")
        R.check(
            "%s 登录成功" % username,
            resp.user.role == role and resp.user.role_label == label,
            "role=%s label=%s" % (resp.user.role, resp.user.role_label),
        )
        R.check(
            "%s 权限点数=%d" % (label, perm_count),
            len(resp.user.permissions) == perm_count,
            "got=%d" % len(resp.user.permissions),
        )

    # 校验令牌内容
    from app.core.security import decode_token
    from app.constants import TokenType

    resp = await auth_service.login("user_law", "123456", "10.0.0.5")
    p = decode_token(resp.access_token, expected_type=TokenType.ACCESS)
    R.check("令牌含用户id", bool(p.get("sub")), "sub=%s" % p.get("sub"))
    R.check("令牌含用户名", p.get("username") == "user_law")
    R.check("令牌含角色", p.get("role") == "law_user")
    R.check("令牌含版本号", p.get("ver") == 1)
    R.check("令牌含签发方", p.get("iss") == "contract-review-system")
    R.check("访问令牌有效期 7200s", p["exp"] - p["iat"] == 7200, "got=%d" % (p["exp"] - p["iat"]))
    R.check("返回 expires_in 正确", resp.expires_in == 7200 and resp.refresh_expires_in == 604800)

    rp = decode_token(resp.refresh_token, expected_type=TokenType.REFRESH)
    R.check("刷新令牌有效期 7 天", (rp["exp"] - rp["iat"]) // 86400 == 7)

    # 登录成功应清零失败计数并更新审计信息
    from app.repositories import user_repo

    u = await user_repo.find_by_username("user_law")
    R.check("登录成功后失败计数清零", u.failed_login_count == 0)
    R.check("记录了最近登录 IP", u.last_login_ip == "10.0.0.5", "ip=%s" % u.last_login_ip)
    R.check("记录了最近登录时间", u.last_login_at is not None)


async def test_login_failure() -> None:
    """C. 登录失败场景 + D. 账号枚举防护"""
    print()
    print("C. 登录失败场景")
    from app.services import auth_service
    from app.core.exceptions import ErrorCode, ForbiddenException, UnauthorizedException

    FAKE_STORE.reset()
    await auth_service.init_builtin_users()

    # C1. 密码错误
    try:
        await auth_service.login("user_law", "wrongpass", "127.0.0.1")
        R.check("密码错误应拒绝", False)
    except UnauthorizedException as e:
        R.check("密码错误返回 401", e.http_status == 401, "http=%d" % e.http_status)

    # C2. 账号不存在
    try:
        await auth_service.login("no_such_user", "123456", "127.0.0.1")
        R.check("账号不存在应拒绝", False)
    except UnauthorizedException as e:
        R.check("账号不存在返回 401", e.http_status == 401)
        code_not_exist = e.code

    # D. 账号枚举防护：两种失败的错误码必须完全一致
    try:
        await auth_service.login("user_law", "wrongpass", "127.0.0.1")
    except UnauthorizedException as e:
        code_wrong_pwd = e.code
    R.check(
        "【安全】账号不存在与密码错误返回相同错误码",
        code_not_exist == code_wrong_pwd == ErrorCode.CREDENTIALS_INVALID,
        "%s vs %s" % (code_not_exist, code_wrong_pwd),
    )

    # C3. 账号停用
    from app.repositories import user_repo

    await user_repo._collection().update_one(
        {"username": "user_law"}, {"$set": {"status": "disabled"}}
    )
    try:
        await auth_service.login("user_law", "123456", "127.0.0.1")
        R.check("停用账号应拒绝", False)
    except ForbiddenException as e:
        R.check("停用账号返回 403", e.http_status == 403 and e.code == ErrorCode.ACCOUNT_DISABLED)
    await user_repo._collection().update_one(
        {"username": "user_law"}, {"$set": {"status": "active"}}
    )

    # C4. 连续失败触发锁定
    FAKE_STORE.reset()
    await auth_service.init_builtin_users()
    from app.core.config import settings

    max_fail = settings.AUTH_MAX_LOGIN_FAILURES
    locked_at = None
    for i in range(max_fail):
        try:
            await auth_service.login("user_law", "wrongpass", "127.0.0.1")
        except (UnauthorizedException, ForbiddenException) as e:
            if isinstance(e, ForbiddenException):
                locked_at = i + 1
    u = await user_repo.find_by_username("user_law")
    R.check("【安全】连续失败 %d 次后账号锁定" % max_fail, locked_at == max_fail,
            "locked_at=%d" % (locked_at or -1))
    R.check("账号状态置为 locked", u.status.value == "locked", "status=%s" % u.status.value)
    R.check("写入了锁定截止时间", u.locked_until is not None)

    # C5. 锁定期间正确密码也被拒
    try:
        await auth_service.login("user_law", "123456", "127.0.0.1")
        R.check("锁定期间正确密码应被拒", False)
    except ForbiddenException as e:
        R.check("锁定期间返回 403", e.code == ErrorCode.ACCOUNT_LOCKED)

    # C6. 锁定到期后自动解除
    await user_repo._collection().update_one(
        {"username": "user_law"},
        {"$set": {"locked_until": datetime.now() - timedelta(minutes=1)}},
    )
    resp = await auth_service.login("user_law", "123456", "127.0.0.1")
    R.check("锁定到期后可正常登录", resp.user.username == "user_law")
    u = await user_repo.find_by_username("user_law")
    R.check("解锁后状态回置 active", u.status.value == "active", "status=%s" % u.status.value)
    R.check("解锁后失败计数清零", u.failed_login_count == 0)


async def test_auth_dependency() -> None:
    """E. 鉴权依赖：令牌解析与越权拦截"""
    print()
    print("E. 鉴权依赖（令牌校验）")
    from app.services import auth_service
    from app.core.dependencies import CurrentUser, get_current_user
    from app.core.exceptions import UnauthorizedException, ForbiddenException
    from app.core.security import create_access_token, create_refresh_token
    from app.constants import TokenType

    FAKE_STORE.reset()
    await auth_service.init_builtin_users()
    resp = await auth_service.login("user_law", "123456", "127.0.0.1")
    from app.repositories import user_repo

    user = await user_repo.find_by_username("user_law")

    class _Req:
        """极简请求替身：提供 headers / state / client / url"""

        def __init__(self, token: Optional[str]) -> None:
            self.headers = {"Authorization": "Bearer %s" % token} if token else {}
            self.state = type("S", (), {})()
            self.client = type("C", (), {"host": "127.0.0.1"})()
            self.url = type("U", (), {"path": "/api/v1/auth/me"})()

    # E1. 正确令牌 → 构造出 CurrentUser
    cu = await get_current_user(_Req(resp.access_token))  # type: ignore[arg-type]
    R.check("正确令牌解析出用户", isinstance(cu, CurrentUser) and cu.username == "user_law")
    R.check("上下文含角色中文名", cu.role_label == "法务审查人")
    R.check("上下文含权限点", "task:view_all" in cu.permissions)
    R.check("上下文不含密码字段", not hasattr(cu, "password_hash"))

    # E2. 未携带令牌 → 401
    try:
        await get_current_user(_Req(None))  # type: ignore[arg-type]
        R.check("未携带令牌应 401", False)
    except UnauthorizedException as e:
        R.check("未携带令牌返回 401", e.http_status == 401, "code=%s" % e.code)

    # E3. 伪造令牌（错误密钥）→ 401
    import jwt as _jwt
    from app.core.config import settings as _s

    forged = _jwt.encode(
        {"sub": user.id, "username": "user_law", "role": "admin", "type": "access",
         "ver": 1, "iss": _s.JWT_ISSUER, "exp": int((datetime.now()).timestamp()) + 9999},
        "attacker-secret", algorithm="HS256",
    )
    try:
        await get_current_user(_Req(forged))  # type: ignore[arg-type]
        R.check("【安全】伪造令牌应拒绝", False)
    except UnauthorizedException as e:
        R.check("【安全】伪造令牌返回 401", e.http_status == 401)

    # E4. 过期令牌 → 401 且错误码为 TOKEN_EXPIRED
    expired = _jwt.encode(
        {"sub": user.id, "username": "user_law", "role": "law_user", "type": "access",
         "ver": 1, "iss": _s.JWT_ISSUER, "exp": int(datetime.now().timestamp()) - 10},
        _s.JWT_SECRET_KEY, algorithm="HS256",
    )
    try:
        await get_current_user(_Req(expired))  # type: ignore[arg-type]
        R.check("过期令牌应拒绝", False)
    except UnauthorizedException as e:
        from app.core.exceptions import ErrorCode

        R.check("过期令牌错误码=TOKEN_EXPIRED", e.code == ErrorCode.TOKEN_EXPIRED, "code=%s" % e.code)

    # E5. 【红线】用 refresh_token 访问业务接口 → 401
    try:
        await get_current_user(_Req(resp.refresh_token))  # type: ignore[arg-type]
        R.check("【安全】refresh_token 越权应拒绝", False)
    except UnauthorizedException as e:
        from app.core.exceptions import ErrorCode

        R.check(
            "【安全】refresh_token 越权返回 TOKEN_TYPE_INVALID",
            e.code == ErrorCode.TOKEN_TYPE_INVALID,
            "code=%s" % e.code,
        )

    # E6. 令牌版本失效（模拟改密后旧令牌）
    old_token = create_access_token(user.id, "user_law", "law_user", token_version=user.token_version)
    await user_repo.revoke_tokens("user_law")
    try:
        await get_current_user(_Req(old_token))  # type: ignore[arg-type]
        R.check("登出后旧令牌应失效", False)
    except UnauthorizedException as e:
        R.check("登出后旧令牌返回 401", e.http_status == 401, "code=%s" % e.code)


async def test_permission_check() -> None:
    """F. 权限校验与水平越权防护"""
    print()
    print("F. 权限校验与水平越权防护")
    from app.core.dependencies import CurrentUser, assert_resource_owner, build_owner_filter
    from app.core.exceptions import ForbiddenException

    biz = CurrentUser(user_id="u-biz", username="user_business", display_name="张经办",
                      role="business_user", role_label="业务经办人", status="active",
                      permissions=("contract:upload", "task:view_own", "report:download"))
    law = CurrentUser(user_id="u-law", username="user_law", display_name="李法务",
                      role="law_user", role_label="法务审查人", status="active",
                      permissions=("task:view_own", "task:view_all", "report:download",
                                   "risk_item:edit", "comment:annotate", "approval:write_back"))
    adm = CurrentUser(user_id="u-adm", username="user_admin", display_name="王管理",
                      role="admin", role_label="系统管理员", status="active",
                      permissions=tuple(__import__("app.constants", fromlist=["ALL_PERMISSIONS"]).ALL_PERMISSIONS))

    # F1. 需求 5 的权责边界逐条验证
    R.check("业务经办人可上传合同", biz.has_permission("contract:upload"))
    R.check("业务经办人【不可】编辑规则", not biz.has_permission("rule_template:manage"))
    R.check("业务经办人【不可】编辑风险项", not biz.has_permission("risk_item:edit"))
    R.check("业务经办人【不可】触发回写", not biz.has_permission("approval:write_back"))
    R.check("业务经办人【不可】管理用户", not biz.has_permission("user:manage"))

    R.check("法务可查看全部任务", law.has_permission("task:view_all"))
    R.check("法务可编辑风险项", law.has_permission("risk_item:edit"))
    R.check("法务可人工批注", law.has_permission("comment:annotate"))
    R.check("法务可触发审批回写", law.has_permission("approval:write_back"))
    R.check("法务【不可】修改系统配置", not law.has_permission("rule_template:manage"))
    R.check("法务【不可】新增规则", not law.has_permission("rule_template:manage"))

    R.check("管理员拥有全部权限", len(adm.permissions) == 11 and adm.is_admin)
    R.check("管理员可管理用户", adm.has_permission("user:manage"))
    R.check("管理员可维护规则", adm.has_permission("rule_template:manage"))
    R.check("管理员可处理阻塞任务", adm.has_permission("blocked_task:handle"))
    R.check("管理员可系统监控", adm.has_permission("system:monitor"))

    # F2. 水平越权防护
    #
    # 【字段约定（2026-09-21 修正）】
    # 归属过滤与归属校验必须使用 **username**（账号名），因为任务主档的
    # created_by 实际存的就是账号名（如 "user_business"），不是 ObjectId。
    # 此前 build_owner_filter 返回 user_id，与真实存储不符 ——
    # 一旦被真实调用，业务经办人会**查不到自己的任务**。
    R.check("业务经办人查询过滤自己的任务（归属用账号名）",
            build_owner_filter(biz) == {"created_by": "user_business"},
            str(build_owner_filter(biz)))
    R.check("法务查询不过滤（可看全部）", build_owner_filter(law) == {}, str(build_owner_filter(law)))
    R.check("管理员查询不过滤", build_owner_filter(adm) == {})

    # 归属校验需同时兼容两种存储约定：username（任务主档）与 user_id（其他集合）
    try:
        assert_resource_owner(biz, "user_business")
        R.check("归属校验认 username（真实 created_by 约定）", True)
    except ForbiddenException:
        R.check("归属校验认 username（真实 created_by 约定）", False)

    try:
        assert_resource_owner(biz, "u-OTHER")
        R.check("【安全】业务经办人访问他人任务应拒绝", False)
    except ForbiddenException as e:
        R.check("【安全】水平越权返回 403", e.http_status == 403, "code=%s" % e.code)

    try:
        assert_resource_owner(biz, None)
        R.check("【安全】归属为空应拒绝（防历史脏数据泄露）", False)
    except ForbiddenException:
        R.check("【安全】归属为空返回 403", True)

    try:
        assert_resource_owner(biz, "u-biz")
        R.check("业务经办人访问自己的任务放行", True)
    except ForbiddenException:
        R.check("业务经办人访问自己的任务放行", False)

    try:
        assert_resource_owner(law, "u-OTHER")
        R.check("法务可跨用户查看（有 view_all）", True)
    except ForbiddenException:
        R.check("法务可跨用户查看（有 view_all）", False)


async def test_refresh_and_logout() -> None:
    """G. 令牌刷新与轮换；H. 登出撤销"""
    print()
    print("G. 令牌刷新与轮换")
    from app.services import auth_service
    from app.core.exceptions import UnauthorizedException, ErrorCode
    from app.core.security import decode_token
    from app.constants import TokenType

    FAKE_STORE.reset()
    await auth_service.init_builtin_users()
    resp = await auth_service.login("user_law", "123456", "127.0.0.1")

    refreshed = await auth_service.refresh_access_token(resp.refresh_token)
    R.check("刷新成功返回新访问令牌", bool(refreshed.access_token))
    R.check("刷新轮换：返回新刷新令牌", bool(refreshed.refresh_token))
    R.check("新旧访问令牌不同", refreshed.access_token != resp.access_token)

    p = decode_token(refreshed.access_token, expected_type=TokenType.ACCESS)
    R.check("新令牌角色正确", p.get("role") == "law_user")

    # 用访问令牌去刷新必须被拒（类型不符）
    try:
        await auth_service.refresh_access_token(resp.access_token)
        R.check("【安全】访问令牌不可用于刷新", False)
    except UnauthorizedException as e:
        R.check("【安全】访问令牌刷新被拒", e.code == ErrorCode.TOKEN_TYPE_INVALID)

    # G2. 非法刷新令牌
    try:
        await auth_service.refresh_access_token("not.a.valid.token")
        R.check("非法刷新令牌应拒绝", False)
    except UnauthorizedException:
        R.check("非法刷新令牌返回 401", True)

    print()
    print("H. 登出撤销")
    logout_resp = await auth_service.logout("user_law")
    R.check("登出返回成功提示", "退出登录" in logout_resp.message)

    from app.core.dependencies import get_current_user
    from app.repositories import user_repo

    u = await user_repo.find_by_username("user_law")
    R.check("登出后 token_version 递增", u.token_version == 2, "ver=%d" % u.token_version)

    class _Req:
        def __init__(self, token: str) -> None:
            self.headers = {"Authorization": "Bearer %s" % token}
            self.state = type("S", (), {})()
            self.client = type("C", (), {"host": "127.0.0.1"})()
            self.url = type("U", (), {"path": "/api/v1/auth/me"})()

    try:
        await get_current_user(_Req(refreshed.access_token))  # type: ignore[arg-type]
        R.check("【安全】登出后旧访问令牌失效", False)
    except UnauthorizedException:
        R.check("【安全】登出后旧访问令牌失效", True)

    try:
        await auth_service.refresh_access_token(refreshed.refresh_token)
        R.check("【安全】登出后旧刷新令牌失效", False)
    except UnauthorizedException:
        R.check("【安全】登出后旧刷新令牌失效", True)


async def test_change_password() -> None:
    """J. 改密后旧令牌失效"""
    print()
    print("J. 修改密码")
    from app.services import auth_service
    from app.core.exceptions import UnauthorizedException
    from app.core.dependencies import get_current_user
    from app.core.security import verify_password
    from app.repositories import user_repo

    FAKE_STORE.reset()
    await auth_service.init_builtin_users()
    resp = await auth_service.login("user_law", "123456", "127.0.0.1")

    # 原密码错误应拒绝
    try:
        await auth_service.change_password("user_law", "wrongold", "NewPass123")
        R.check("原密码错误应拒绝", False)
    except UnauthorizedException:
        R.check("原密码错误返回 401", True)

    # 新密码不合规应拒绝
    for bad, label in [("abc", "过短"), ("abcdefgh", "无数字"), ("12345678", "无字母")]:
        try:
            await auth_service.change_password("user_law", "123456", bad)
            R.check("弱密码(%s)应拒绝" % label, False)
        except ValueError:
            R.check("弱密码(%s)被拒绝" % label, True)

    # 正常改密
    ok = await auth_service.change_password("user_law", "123456", "NewPass123")
    R.check("改密成功", ok)
    u = await user_repo.find_by_username("user_law")
    R.check("新密码可校验", verify_password("NewPass123", u.password_hash))
    R.check("旧密码失效", not verify_password("123456", u.password_hash))
    R.check("改密后 token_version 递增", u.token_version == 2, "ver=%d" % u.token_version)

    # 改密后旧令牌失效
    class _Req:
        def __init__(self, token: str) -> None:
            self.headers = {"Authorization": "Bearer %s" % token}
            self.state = type("S", (), {})()
            self.client = type("C", (), {"host": "127.0.0.1"})()
            self.url = type("U", (), {"path": "/api/v1/auth/me"})()

    try:
        await get_current_user(_Req(resp.access_token))  # type: ignore[arg-type]
        R.check("【安全】改密后旧令牌失效", False)
    except UnauthorizedException:
        R.check("【安全】改密后旧令牌失效", True)


async def test_middleware_whitelist() -> None:
    """I. 鉴权中间件白名单与路径穿越防护"""
    print()
    print("I. 鉴权中间件")
    from app.core.auth_middleware import _is_always_allowed, _is_whitelisted, _normalize_path
    from app.core.config import settings

    wl = settings.auth_whitelist_paths

    # I1. 白名单放行
    for p in ["/api/v1/auth/login", "/api/v1/auth/refresh", "/api/v1/health"]:
        R.check("白名单放行 %s" % p, _is_whitelisted(_normalize_path(p), wl))

    # I2. 业务接口需鉴权
    for p in ["/api/v1/tasks", "/api/v1/rules", "/api/v1/auth/me", "/api/v1/auth/logout"]:
        n = _normalize_path(p)
        R.check("业务接口需鉴权 %s" % p, not (_is_whitelisted(n, wl) or _is_always_allowed(n)))

    # I3. 【安全】路径穿越攻击必须拒绝
    attacks = [
        "/api/v1/auth/login/../users",
        "/api/v1/auth/login/../../api/v1/rules",
        "/api/v1/auth/login/%2e%2e/users",
        "/api/v1/health/../tasks",
        "/docs/../api/v1/users",
    ]
    for a in attacks:
        n = _normalize_path(a)
        blocked = not (_is_whitelisted(n, wl) or _is_always_allowed(n))
        R.check("【安全】路径穿越被拒 %s" % a, blocked, "-> %s" % n)

    # I4. 前缀混淆
    R.check("【安全】/api/v1/auth/loginBypass 被拒",
            not _is_whitelisted(_normalize_path("/api/v1/auth/loginBypass"), wl))


async def test_http_layer() -> None:
    """K. HTTP 层端到端（TestClient）"""
    print()
    print("K. HTTP 层端到端")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.v1.router import api_router
    from app.core.auth_middleware import AuthMiddleware
    from app.core.config import settings
    from app.core.exceptions import register_exception_handlers
    from app.core.middleware import RequestIdMiddleware

    FAKE_STORE.reset()

    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    app.add_middleware(RequestIdMiddleware)
    register_exception_handlers(app)
    app.include_router(api_router, prefix=settings.API_V1_PREFIX)

    from app.services import auth_service

    await auth_service.init_builtin_users()

    client = TestClient(app)

    # K1. 登录接口（白名单，无需令牌）
    r = client.post("/api/v1/auth/login", json={"username": "user_law", "password": "123456"})
    R.check("登录接口返回 200", r.status_code == 200, "status=%d" % r.status_code)
    body = r.json()
    R.check("响应 code=0", body.get("code") == 0)
    R.check("响应含 access_token", bool(body["data"].get("access_token")))
    R.check("响应含 refresh_token", bool(body["data"].get("refresh_token")))
    R.check("响应含用户角色信息", body["data"]["user"]["role"] == "law_user")
    R.check("响应含 request_id", bool(body.get("request_id")))
    token = body["data"]["access_token"]

    # K2. 未带令牌访问受保护接口 → 401
    r = client.get("/api/v1/auth/me")
    R.check("未带令牌访问 /me 返回 401", r.status_code == 401, "status=%d" % r.status_code)
    R.check("401 响应结构统一", r.json().get("code") in (60002, 60003), "code=%s" % r.json().get("code"))

    # K3. 带令牌访问 → 200
    r = client.get("/api/v1/auth/me", headers={"Authorization": "Bearer %s" % token})
    R.check("带令牌访问 /me 返回 200", r.status_code == 200, "status=%d" % r.status_code)
    R.check("返回当前用户正确", r.json()["data"]["username"] == "user_law")

    # K4. 权限校验接口
    r = client.post("/api/v1/auth/check-permission",
                    json={"permission": "risk_item:edit"},
                    headers={"Authorization": "Bearer %s" % token})
    R.check("权限校验接口返回 200", r.status_code == 200)
    R.check("法务拥有 risk_item:edit", r.json()["data"]["passed"] is True)

    r = client.post("/api/v1/auth/check-permission",
                    json={"permission": "rule_template:manage"},
                    headers={"Authorization": "Bearer %s" % token})
    R.check("法务不拥有 rule_template:manage", r.json()["data"]["passed"] is False)
    R.check("校验失败仍返回 HTTP 200（语义为查询成功）", r.status_code == 200)

    r = client.get("/api/v1/auth/check-permission?role=admin",
                   headers={"Authorization": "Bearer %s" % token})
    R.check("GET 形式角色校验可用", r.status_code == 200 and r.json()["data"]["passed"] is False)

    # K5. 权限矩阵接口
    r = client.get("/api/v1/auth/permission-matrix",
                   headers={"Authorization": "Bearer %s" % token})
    matrix = r.json()["data"]
    R.check("权限矩阵含 3 个角色", len(matrix["roles"]) == 3)
    R.check("权限点总数 11", matrix["total_permissions"] == 11, "got=%s" % matrix["total_permissions"])

    # K6. 刷新令牌接口
    r = client.post("/api/v1/auth/refresh",
                    json={"refresh_token": body["data"]["refresh_token"]})
    R.check("刷新接口返回 200", r.status_code == 200, "status=%d" % r.status_code)
    new_token = r.json()["data"]["access_token"]
    R.check("返回新访问令牌", new_token != token)

    # K7. 登出后旧令牌失效
    r = client.post("/api/v1/auth/logout", headers={"Authorization": "Bearer %s" % new_token})
    R.check("登出接口返回 200", r.status_code == 200, "status=%d" % r.status_code)

    r = client.get("/api/v1/auth/me", headers={"Authorization": "Bearer %s" % new_token})
    R.check("【安全】登出后旧令牌访问返回 401", r.status_code == 401, "status=%d" % r.status_code)

    # K8. 伪造令牌 → 401
    r = client.get("/api/v1/auth/me", headers={"Authorization": "Bearer fake.token.here"})
    R.check("伪造令牌返回 401", r.status_code == 401, "status=%d" % r.status_code)

    # K9. 错误请求头格式 → 401
    r = client.get("/api/v1/auth/me", headers={"Authorization": "Basic abc123"})
    R.check("错误鉴权前缀返回 401", r.status_code == 401, "status=%d" % r.status_code)

    # K10. 路径穿越不能绕过鉴权
    r = client.get("/api/v1/auth/login/../rules")
    R.check("【安全】穿越路径未绕过鉴权", r.status_code in (401, 404), "status=%d" % r.status_code)

    # K11. 登录失败经 HTTP 层返回 401 且结构统一
    r = client.post("/api/v1/auth/login", json={"username": "user_law", "password": "bad"})
    R.check("HTTP 层密码错误返回 401", r.status_code == 401, "status=%d" % r.status_code)
    R.check("错误响应含 code/message",
            "code" in r.json() and "message" in r.json(), str(r.json())[:80])

    # K12. 参数校验 422
    r = client.post("/api/v1/auth/login", json={"username": "u", "password": ""})
    R.check("非法入参返回 422", r.status_code == 422, "status=%d" % r.status_code)

    # K13. 三种角色全链路登录
    for uname, role in [("user_business", "business_user"), ("user_admin", "admin")]:
        r = client.post("/api/v1/auth/login", json={"username": uname, "password": "123456"})
        R.check("HTTP 层 %s 登录成功" % uname,
                r.status_code == 200 and r.json()["data"]["user"]["role"] == role)


# ======================================================================
# 三、主流程
# ======================================================================
async def main() -> int:
    """
    执行全部测试用例

    返回：
        int：退出码，0 表示全部通过
    """
    print("=" * 74)
    print("登录鉴权模块端到端验证")
    print("=" * 74)
    print("说明：本机无 MongoDB，使用内存假集合替换存储驱动，")
    print("      业务代码（仓储层 / 服务层 / 依赖层 / 中间件 / 接口层）均为生产代码。")

    install_fake_mongo()

    await test_seed_builtin_users()
    await test_login_success()
    await test_login_failure()
    await test_auth_dependency()
    await test_permission_check()
    await test_refresh_and_logout()
    await test_change_password()
    await test_middleware_whitelist()
    await test_http_layer()

    return R.summary()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
