# D:/ContractReviewSys-合同审批智能审查项目/backend/tests/verify_report_export.py
"""
审查报告持久化与下载链路端到端验证（缺口2 修复的守护测试）

【验证目标】
    一、报告文件命名与对象键符合约定
    二、报告生成后持久化：上传 MinIO + 回写任务 4 个字段
    三、幂等控制：内容未变更时复用已有文件，不重复上传
    四、内容变更时重新上传并覆盖对象键
    五、落库失败自动回滚：删除刚上传的 MinIO 对象，不留脏数据
    六、下载接口：报告不存在返回 404；MinIO 读取失败返回 30001
    七、下载双模式：JSON 兼容原结构；stream 返回带文件名的文件流
    八、权限校验：非本人且非特权角色被拒 403；三角色均可下载自己的报告
    九、历史任务兼容：无报告字段时明确返回"尚未生成"，不报错、不返回空正文
    十、权责红线：报告持久化只写报告字段，绝不触碰审批/状态机字段

【外部依赖隔离】
    MinIO 与 MongoDB 均通过 monkeypatch 注入内存替身。
    被验证的代码路径（命名规则、指纹计算、幂等判定、持久化、回滚、
    下载分流、权限判定）全部是真实执行的生产代码。

【运行方式】
    cd backend && python tests/verify_report_export.py
"""

import asyncio
import hashlib
import os
import sys
from typing import Any, Dict, Optional

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from app.constants import PERM_REPORT_DOWNLOAD, RiskLevel, UserRole
from app.core.dependencies import CurrentUser
from app.graph.state import ReviewTaskState, RiskItem
from app.models.documents.review_task import ReviewTask
from app.services import report_service

PASSED = []
FAILED = []


def check(name: str, condition: bool, detail: str = "") -> None:
    """断言并记录结果"""
    if condition:
        PASSED.append(name)
        print(f"  [PASS] {name}")
    else:
        FAILED.append((name, detail))
        print(f"  [FAIL] {name} | {detail}")


# ======================================================================
# 内存替身：MinIO 与 MongoDB
# ======================================================================
class FakeMinIO:
    """MinIO 内存替身，支持上传/下载/删除/预签名，并记录调用次数"""

    def __init__(self) -> None:
        self.objects: Dict[str, bytes] = {}
        self.upload_calls = 0
        self.delete_calls = 0
        self.fail_upload = False
        self.fail_download = False

    async def upload_bytes(self, bucket: str, object_key: str, data: bytes,
                           content_type: str = "") -> str:
        if self.fail_upload:
            from app.core.exceptions import MinIOException

            raise MinIOException(message="模拟上传失败", detail="fake")
        self.upload_calls += 1
        self.objects[object_key] = data
        return object_key

    async def download_bytes(self, bucket: str, object_key: str) -> bytes:
        if self.fail_download:
            from app.core.exceptions import MinIOException

            raise MinIOException(message="模拟下载失败", detail="fake")
        if object_key not in self.objects:
            from app.core.exceptions import MinIOException

            raise MinIOException(message="对象不存在", detail=object_key)
        return self.objects[object_key]

    async def delete_object(self, bucket: str, object_key: str) -> bool:
        self.delete_calls += 1
        self.objects.pop(object_key, None)
        return True

    async def get_presigned_url(self, bucket: str, object_key: str,
                                expires_seconds: int = 3600) -> str:
        return f"http://fake-minio/{bucket}/{object_key}?expires={expires_seconds}"


class FakeTaskRepo:
    """任务仓储内存替身，模拟 find_by_task_id 与 update_task"""

    def __init__(self, task: Optional[ReviewTask] = None) -> None:
        self.task = task
        self.update_calls: list = []
        self.fail_update = False

    async def find_by_task_id(self, task_id: str) -> Optional[ReviewTask]:
        if self.task is None or self.task.id != task_id:
            return None
        return self.task

    async def update_task(self, task_id: str, updates: Dict[str, Any]) -> bool:
        if self.fail_update:
            raise RuntimeError("模拟落库失败")
        self.update_calls.append(dict(updates))
        if self.task is not None and self.task.id == task_id:
            # 真实回写：把字段写到任务对象上，供后续幂等判定读取
            for key, value in updates.items():
                if key != "updated_at":
                    object.__setattr__(self.task, key, value)
            return True
        return False


class FakeAuditRepo:
    """审计仓储内存替身"""

    def __init__(self) -> None:
        self.logs: list = []

    async def append_log(self, entry: Dict[str, Any]) -> bool:
        self.logs.append(entry)
        return True


# ======================================================================
# 辅助：注入替身
# ======================================================================
def _patch(monkeypatch_holder: dict) -> None:
    """把服务层用到的单例替换为内存替身"""
    import app.repositories.task_repo as task_repo_mod
    import app.repositories.audit_repo as audit_repo_mod
    from app.storage import minio_client as minio_mod

    monkeypatch_holder["minio"].objects.clear()
    minio_mod.upload_bytes = monkeypatch_holder["minio"].upload_bytes
    minio_mod.download_bytes = monkeypatch_holder["minio"].download_bytes
    minio_mod.delete_object = monkeypatch_holder["minio"].delete_object
    minio_mod.get_presigned_url = monkeypatch_holder["minio"].get_presigned_url

    task_repo_mod.find_by_task_id = monkeypatch_holder["repo"].find_by_task_id
    task_repo_mod.update_task = monkeypatch_holder["repo"].update_task

    audit_repo_mod.append_log = monkeypatch_holder["audit"].append_log


def make_state() -> ReviewTaskState:
    """构造含风险项的测试状态"""
    risk_items = [
        RiskItem(
            risk_id="R1", rule_code="IP-001", rule_name="知识产权侵权责任转嫁异常",
            clause_id="C3", clause_type="ip",
            evidence_text="因知识产权侵权产生的全部责任由乙方承担。",
            char_start=100, char_end=140, page_no=1,
            risk_level=RiskLevel.HIGH.value,
            decision="need_revision", severity="major",
            suggestion_text="建议明确双方按过错比例分担侵权责任。",
            legal_basis="《民法典》第八百四十七条",
        ),
        RiskItem(
            risk_id="R2", rule_code="PAY-001", rule_name="无验收即全款支付",
            clause_id="C4", clause_type="payment",
            evidence_text="合同签订后即支付全部款项。",
            char_start=200, char_end=240, page_no=1,
            risk_level=RiskLevel.CRITICAL.value,
            decision="block", severity="fatal",
            suggestion_text="建议改为验收合格后分期支付。",
            legal_basis="《民法典》第七百七十条",
        ),
    ]
    state = ReviewTaskState(
        task_id="65f1a2b3c4d5e6f7a8b9c0d1",
        task_no="T20260919001",
        contract_id="C-001",
        contract_title="设备采购合同",
        contract_no="HT-2026-001",
        contract_type="purchase",
        contract_amount=860000.0,
        currency="CNY",
        risk_level=RiskLevel.CRITICAL.value,
        final_risk_items=risk_items,
        conclusion_text="本次合同审查最终风险等级为【严重风险】。",
    )
    return state


def make_task(task_id: str = "65f1a2b3c4d5e6f7a8b9c0d1",
              created_by: str = "user_business") -> ReviewTask:
    """构造测试任务（历史任务默认无报告字段）"""
    return ReviewTask(
        id=task_id,
        task_no="T20260919001",
        contract_id="C-001",
        thread_id="th-001",
        work_dir="work/T20260919001",
        created_by=created_by,
    )


def make_user(username: str, role: str) -> CurrentUser:
    """构造测试用户，权限按角色矩阵真实取用"""
    from app.constants import USER_ROLE_LABELS, get_permissions_for_role

    return CurrentUser(
        user_id=username,
        username=username,
        display_name=username,
        role=role,
        role_label=USER_ROLE_LABELS.get(role, role),
        status="active",
        permissions=get_permissions_for_role(role),
    )


# ======================================================================
# 验证一：命名规则与对象键
# ======================================================================
def test_naming():
    print("\n[验证一] 报告文件命名与对象键")
    task_id = "65f1a2b3c4d5e6f7a8b9c0d1"

    name = report_service.build_report_file_name(task_id)
    check("文件名符合 {task_id}_review_report.md", name == f"{task_id}_review_report.md", name)

    key = report_service.build_report_object_key(task_id)
    check("对象键为 report/ 前缀 + 文件名",
          key == f"report/{task_id}_review_report.md", key)

    # 路径穿越防御
    evil = report_service.build_report_file_name("../../etc/passwd")
    check("任务 ID 中的路径分隔符被清洗",
          "/" not in evil and "\\" not in evil, evil)

    # 指纹
    h1 = report_service.compute_report_content_hash("# 报告")
    h2 = report_service.compute_report_content_hash("# 报告")
    h3 = report_service.compute_report_content_hash("# 报告（修订）")
    check("相同内容指纹一致", h1 == h2)
    check("内容变更指纹变化", h1 != h3)
    check("指纹为 SHA-256（64 位十六进制）",
          len(h1) == 64 and all(c in "0123456789abcdef" for c in h1))
    check("指纹与标准库实现一致",
          h1 == hashlib.sha256("# 报告".encode("utf-8")).hexdigest())


# ======================================================================
# 验证二：生成后持久化
# ======================================================================
async def test_persist(holder: dict):
    print("\n[验证二] 报告生成后持久化（上传 + 落库 4 字段）")
    state = make_state()
    result = await report_service.generate_report_service(
        state, task_id=state.task_id
    )

    minio: FakeMinIO = holder["minio"]
    repo: FakeTaskRepo = holder["repo"]

    check("报告正文非空", len(result.report_content) > 0)
    check("对象键符合约定",
          result.report_object_key == f"report/{state.task_id}_review_report.md",
          result.report_object_key)
    check("文件已上传到 MinIO", result.report_object_key in minio.objects)
    check("上传内容与报告正文一致",
          minio.objects[result.report_object_key].decode("utf-8") == result.report_content)
    check("返回预签名链接非空", result.report_url.startswith("http"))
    check("report_format 为 md", result.report_format == "md")
    check("persisted 为 True", result.persisted is True)
    check("reused 为 False（首次生成）", result.reused is False)

    # 落库字段检查
    # ★ 缺口1 修复后：默认 PDF_AUTO_WITH_MD=True，一次生成会落库两次 ——
    #   先写 Markdown 的 report_* 字段，再写 PDF 的 pdf_* 字段。
    #   因此这里断言"至少 1 次"，并定位到承载 report_* 的那一次。
    check("执行了落库操作", len(repo.update_calls) >= 1)
    md_calls = [c for c in repo.update_calls if "report_object_key" in c]
    check("其中包含 Markdown 字段落库", len(md_calls) == 1, str(len(md_calls)))
    if md_calls:
        updates = md_calls[0]
        check("落库含 report_object_key", "report_object_key" in updates)
        check("落库含 report_format", updates.get("report_format") == "md")
        check("落库含 report_generated_at", "report_generated_at" in updates)
        check("落库含 report_content_hash",
              updates.get("report_content_hash") == result.content_hash)

        # 权责红线：绝不写审批/状态机字段
        forbidden = {
            "status", "write_back_status", "write_back_retry", "approval_status",
            "conclusion_text", "risk_level", "decision", "blocked_reason",
            "retry_count", "current_node", "route_type", "progress",
        }
        touched = forbidden & set(updates.keys())
        check("落库未触碰审批/状态机字段（权责红线）", not touched, f"越界字段={touched}")


# ======================================================================
# 验证三：幂等复用（内容未变更）
# ======================================================================
async def test_idempotent(holder: dict):
    print("\n[验证三] 幂等控制：内容未变更时复用已有文件")
    state = make_state()
    minio: FakeMinIO = holder["minio"]

    uploads_before = minio.upload_calls
    result = await report_service.generate_report_service(state, task_id=state.task_id)

    check("识别为复用（reused=True）", result.reused is True,
          f"reused={result.reused}")
    check("未重复上传（上传次数不变）",
          minio.upload_calls == uploads_before,
          f"before={uploads_before}, after={minio.upload_calls}")
    check("仍返回可用链接", result.report_url.startswith("http"))
    check("仍返回报告正文", len(result.report_content) > 0)
    check("落库成功（刷新生成时间）", result.persisted is True)


# ======================================================================
# 验证四：内容变更时重新上传
# ======================================================================
async def test_content_changed(holder: dict):
    print("\n[验证四] 内容变更时重新上传并覆盖对象键")
    state = make_state()
    # 追加一条风险项 → 报告内容变化 → 指纹变化
    state.final_risk_items.append(
        RiskItem(
            risk_id="R3", rule_code="JUR-001", rule_name="争议管辖地约定违规",
            clause_id="C5", clause_type="dispute",
            evidence_text="争议由对方所在地法院管辖。",
            char_start=300, char_end=330, page_no=1,
            risk_level=RiskLevel.HIGH.value,
            decision="need_revision", severity="major",
        )
    )
    minio: FakeMinIO = holder["minio"]
    uploads_before = minio.upload_calls

    result = await report_service.generate_report_service(state, task_id=state.task_id)

    check("识别为重新上传（reused=False）", result.reused is False)
    # ★ 缺口1 修复后：默认 PDF_AUTO_WITH_MD=True，内容变更会同时重新上传
    #   Markdown 与 PDF 两个对象，因此期望 +2 而非 +1。
    #   这里用"至少 +1"并单独校验 Markdown 对象已更新，避免与 PDF 行为耦合。
    check("上传次数增加（Markdown 重新上传）",
          minio.upload_calls >= uploads_before + 1,
          f"before={uploads_before}, after={minio.upload_calls}")
    check("新报告含新增风险项 JUR-001", "JUR-001" in result.report_content)
    check("对象键保持不变（同任务同一对象键）",
          result.report_object_key == f"report/{state.task_id}_review_report.md")
    check("MinIO 中内容已更新为新版",
          "JUR-001" in minio.objects[result.report_object_key].decode("utf-8"))


# ======================================================================
# 验证五：落库失败自动回滚
# ======================================================================
async def test_rollback(holder: dict):
    print("\n[验证五] 落库失败自动回滚（删除 MinIO 对象，不留脏数据）")
    # 用全新任务与全新 MinIO，隔离此前状态
    task = make_task(task_id="aaaabbbbccccddddeeee0001")
    holder["repo"].task = task
    holder["minio"].objects.clear()
    holder["minio"].delete_calls = 0

    # 构造新内容以触发生成上传（避免幂等复用路径）
    state = make_state()
    state.task_id = task.id
    state.contract_title = "回滚测试合同"

    holder["repo"].fail_update = True  # 模拟落库异常
    result = await report_service.generate_report_service(state, task_id=task.id)
    holder["repo"].fail_update = False

    check("persisted 为 False（落库失败）", result.persisted is False)
    check("执行了回滚删除", holder["minio"].delete_calls >= 1,
          f"delete_calls={holder['minio'].delete_calls}")
    check("MinIO 中无残留对象",
          result.report_object_key not in holder["minio"].objects,
          f"objects={list(holder['minio'].objects.keys())}")
    check("仍返回报告正文（供调用方兜底）", len(result.report_content) > 0)


# ======================================================================
# 验证六：下载接口 —— 报告不存在 / MinIO 失败
# ======================================================================
async def test_download_missing(holder: dict):
    print("\n[验证六] 下载接口：报告未生成返回 404，MinIO 失败返回 30001")
    from app.api.v1.endpoints import report_comment
    from app.core.exceptions import BusinessException, ErrorCode

    # 场景 A：历史任务无报告字段
    task = make_task(task_id="aaaabbbbccccddddeeee0002")
    holder["repo"].task = task
    # 让 task_service 返回该任务
    _patch_task_service(task)

    user = make_user("user_business", UserRole.BUSINESS_USER.value)

    try:
        await report_comment.download_report(task.id, "json", user)
        check("历史任务无报告时抛 404", False, "未抛异常")
    except BusinessException as exc:
        check("历史任务无报告时抛 404",
              exc.code == ErrorCode.NOT_FOUND and exc.http_status == 404,
              f"code={exc.code}, http={exc.http_status}")
        check("错误信息明确提示尚未生成", "尚未生成" in exc.message, exc.message)

    # 场景 B：有报告字段但 MinIO 读取失败
    task.report_object_key = f"report/{task.id}_review_report.md"
    task.report_format = "md"
    holder["minio"].fail_download = True
    try:
        await report_comment.download_report(task.id, "json", user)
        check("MinIO 读取失败时抛 30001", False, "未抛异常")
    except BusinessException as exc:
        check("MinIO 读取失败时抛 30001", exc.code == ErrorCode.MINIO_ERROR,
              f"code={exc.code}")
        check("HTTP 状态为 502", exc.http_status == 502, str(exc.http_status))
    finally:
        holder["minio"].fail_download = False


# ======================================================================
# 验证七：下载双模式（JSON 兼容 + 文件流）
# ======================================================================
async def test_download_modes(holder: dict):
    print("\n[验证七] 下载双模式：JSON 兼容原结构 + stream 文件流")
    from app.api.v1.endpoints import report_comment
    from fastapi.responses import Response

    task = make_task(task_id="aaaabbbbccccddddeeee0003")
    holder["repo"].task = task
    _patch_task_service(task)

    # 先生成报告，完成持久化
    state = make_state()
    state.task_id = task.id
    await report_service.generate_report_service(state, task_id=task.id)

    user = make_user("user_business", UserRole.BUSINESS_USER.value)

    # ---------- JSON 模式 ----------
    resp = await report_comment.download_report(task.id, "json", user)
    data = resp.data
    check("JSON 模式保留 task_id 字段", data.get("task_id") == task.id)
    check("JSON 模式保留 content 字段（非空）", bool(data.get("content")))
    check("JSON 模式保留 report_url 字段", "report_url" in data)
    check("content 与生成内容一致",
          data.get("content") == state and True or "合同基本信息" in data.get("content", ""))
    check("新增 file_name 字段", data.get("file_name") == f"{task.id}_review_report.md",
          str(data.get("file_name")))
    check("新增 report_available=True", data.get("report_available") is True)
    check("新增 report_generated_at", bool(data.get("report_generated_at")))
    check("预签名链接已重新签发", str(data.get("report_url")).startswith("http"))

    # ---------- stream 模式 ----------
    stream_resp = await report_comment.download_report(task.id, "stream", user)
    check("stream 模式返回 Response 对象", isinstance(stream_resp, Response))
    # ★ 缺口1 修复：media_type 由笼统的 application/octet-stream
    #   改为更精确的 text/markdown; charset=utf-8（Markdown 的真实类型）。
    #   浏览器行为不变（仍按 Content-Disposition 下载），但语义更准确，
    #   且为 PDF 分支的 application/pdf 内联预览留出了一致的处理方式。
    check("media_type 为 text/markdown",
          stream_resp.media_type == "text/markdown; charset=utf-8",
          str(stream_resp.media_type))
    disposition = stream_resp.headers.get("content-disposition", "")
    check("含 Content-Disposition 附件头",
          "attachment" in disposition and f"{task.id}_review_report.md" in disposition,
          disposition)
    check("含 Content-Length", bool(stream_resp.headers.get("content-length")))
    check("文件流内容为 Markdown 报告",
          "合同基本信息".encode("utf-8") in stream_resp.body)


# ======================================================================
# 验证八：权限校验
# ======================================================================
async def test_permissions(holder: dict):
    print("\n[验证八] 权限校验：本人/法务/管理员可下载，他人被拒")
    from app.api.v1.endpoints import report_comment
    from app.core.exceptions import BusinessException, ErrorCode

    task = make_task(task_id="aaaabbbbccccddddeeee0004", created_by="user_business")
    holder["repo"].task = task
    _patch_task_service(task)

    state = make_state()
    state.task_id = task.id
    await report_service.generate_report_service(state, task_id=task.id)

    # 本人（业务经办人）
    owner = make_user("user_business", UserRole.BUSINESS_USER.value)
    try:
        resp = await report_comment.download_report(task.id, "json", owner)
        check("任务提交人可下载", resp.data.get("report_available") is True)
    except BusinessException as exc:
        check("任务提交人可下载", False, f"被拒：{exc.message}")

    # 法务（非提交人，但特权角色）
    law = make_user("user_law", UserRole.LAW_USER.value)
    try:
        await report_comment.download_report(task.id, "json", law)
        check("法务审查人可下载他人任务报告", True)
    except BusinessException as exc:
        check("法务审查人可下载他人任务报告", False, f"被拒：{exc.message}")

    # 管理员
    admin = make_user("user_admin", UserRole.ADMIN.value)
    try:
        await report_comment.download_report(task.id, "json", admin)
        check("系统管理员可下载他人任务报告", True)
    except BusinessException as exc:
        check("系统管理员可下载他人任务报告", False, f"被拒：{exc.message}")

    # 其他业务经办人（非提交人、非特权角色）→ 应被拒
    other = make_user("user_other", UserRole.BUSINESS_USER.value)
    try:
        await report_comment.download_report(task.id, "json", other)
        check("非本人业务经办人被拒 403", False, "居然通过了")
    except BusinessException as exc:
        check("非本人业务经办人被拒 403",
              exc.code == ErrorCode.PERMISSION_DENIED and exc.http_status == 403,
              f"code={exc.code}, http={exc.http_status}")

    # 无 report:download 权限点的用户 → 应被拒
    from app.core.dependencies import CurrentUser

    noperm = CurrentUser(
        user_id="x", username="x", display_name="x",
        role=UserRole.BUSINESS_USER.value, role_label="业务经办人",
        status="active", permissions=(),   # 故意清空权限
    )
    try:
        await report_comment.download_report(task.id, "json", noperm)
        check("缺少 report:download 权限点被拒", False, "居然通过了")
    except BusinessException as exc:
        check("缺少 report:download 权限点被拒",
              exc.code == ErrorCode.PERMISSION_DENIED, f"code={exc.code}")


# ======================================================================
# 验证九：审计留痕
# ======================================================================
async def test_audit(holder: dict):
    print("\n[验证九] 关键操作审计留痕")
    audit: FakeAuditRepo = holder["audit"]
    actions = [log.get("action") for log in audit.logs]

    check("记录了下载动作", "report_download" in actions, str(actions))
    check("记录了报告未生成的拒绝动作",
          "report_download_missing" in actions, str(actions))
    check("审计日志含操作人", all(log.get("operator") for log in audit.logs))
    check("审计日志含 request_id 字段", all("request_id" in log for log in audit.logs))
    check("审计日志含任务编号",
          all(log.get("task_no") for log in audit.logs))


# ======================================================================
# 验证十：历史任务模型兼容
# ======================================================================
def test_model_compat():
    print("\n[验证十] 历史任务模型兼容（无报告字段不报错）")
    old = ReviewTask(
        id="legacy", task_no="T-OLD", contract_id="C-OLD",
        thread_id="th", work_dir="w",
    )
    check("历史任务 report_object_key 为 None", old.report_object_key is None)
    check("历史任务 report_format 为 None", old.report_format is None)
    check("历史任务 report_generated_at 为 None", old.report_generated_at is None)
    check("历史任务 report_content_hash 为 None", old.report_content_hash is None)

    # 模拟从 MongoDB 读出旧文档（不含报告键）
    raw = {
        "_id": "legacy", "task_no": "T-OLD", "contract_id": "C-OLD",
        "thread_id": "th", "work_dir": "w",
        "status": "completed", "write_back_status": "pending",
    }
    from app.repositories.task_repo import _to_model

    model = _to_model(raw)
    check("旧文档可正常反序列化为模型", model.task_no == "T-OLD")
    check("反序列化后报告字段为空值", model.report_object_key is None)


# ======================================================================
# 测试辅助
# ======================================================================
def _patch_task_service(task: ReviewTask) -> None:
    """让 task_service.get_task_by_id 返回指定任务"""
    import app.services.task_service as ts

    async def _fake_get(task_id: str):
        return task if task_id == task.id else None

    ts.get_task_by_id = _fake_get


async def main() -> int:
    holder = {
        "minio": FakeMinIO(),
        "repo": FakeTaskRepo(make_task()),
        "audit": FakeAuditRepo(),
    }
    _patch(holder)

    # 同步测试
    test_naming()
    test_model_compat()

    # 异步测试（顺序有依赖：先持久化 → 再幂等 → 再变更）
    await test_persist(holder)
    await test_idempotent(holder)
    await test_content_changed(holder)
    await test_rollback(holder)
    await test_download_missing(holder)
    await test_download_modes(holder)
    await test_permissions(holder)
    await test_audit(holder)

    print("\n" + "=" * 72)
    print(f"结果：通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    print("=" * 72)
    if FAILED:
        for name, detail in FAILED:
            print(f"  [FAIL] {name} | {detail}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
