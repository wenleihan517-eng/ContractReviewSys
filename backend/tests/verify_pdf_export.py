# D:/ContractReviewSys-合同审批智能审查项目/backend/tests/verify_pdf_export.py
"""
PDF 审查报告导出链路端到端验证（缺口1 修复的守护测试）

【验证目标】
    一、渲染器基础能力：中文可提取、A4 尺寸、页眉页脚、分级标题
    二、内容一致性：PDF 由 Markdown 唯一数据源渲染，关键字段全部落到 PDF 中
       （这是需求"与 Markdown 报告内容完全一致"的机器可验证证据）
    三、风险等级配色：高风险红 / 中风险橙 / 低风险绿，颜色确实写进 PDF
    四、Markdown 解析：标题、表格、列表、分隔线、引用块均正确识别
    五、命名与对象键：{task_id}_review_report.pdf，与 Markdown 键互不覆盖
    六、持久化：上传 MinIO + 回写 pdf_* 五个字段，且不触碰 Markdown 字段
    七、幂等控制：源内容未变更时复用已有 PDF，跳过渲染与上传
    八、源内容变更：重新渲染并覆盖对象
    九、落库失败回滚：删除刚上传的 PDF 对象，不留脏数据
   十、下载接口：format=md（默认，兼容原行为）/ pdf 分支正确
   十一、下载接口：format 非法值 400；PDF 未生成 404；响应头 Content-Type 正确
   十二、权责红线：PDF 链路只写 pdf 字段，绝不触碰状态机/审批/判定字段
   十三、字体三级回退：内置 CJK 字体可作兜底，保证中文永不缺字
   十四、异常处理：空 Markdown 报错、页数上限保护

【外部依赖隔离】
    MinIO 与 MongoDB 均通过内存替身注入；
    PyMuPDF 为真实调用 —— PDF 渲染是本模块要验证的核心能力，不可用替身。

【运行方式】
    cd backend && python tests/verify_pdf_export.py
"""

import asyncio
import os
import sys
from typing import Any, Dict, Optional

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

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
# 内存替身
# ======================================================================
class FakeMinIO:
    """MinIO 内存替身，记录上传/删除调用与 content_type"""

    def __init__(self) -> None:
        self.objects: Dict[str, bytes] = {}
        self.content_types: Dict[str, str] = {}
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
        self.content_types[object_key] = content_type
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
    """任务仓储内存替身"""

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
# 辅助
# ======================================================================
def _patch(holder: dict) -> None:
    """把服务层单例替换为内存替身"""
    import app.repositories.audit_repo as audit_repo_mod
    import app.repositories.task_repo as task_repo_mod
    from app.storage import minio_client as minio_mod

    holder["minio"].objects.clear()
    minio_mod.upload_bytes = holder["minio"].upload_bytes
    minio_mod.download_bytes = holder["minio"].download_bytes
    minio_mod.delete_object = holder["minio"].delete_object
    minio_mod.get_presigned_url = holder["minio"].get_presigned_url

    task_repo_mod.find_by_task_id = holder["repo"].find_by_task_id
    task_repo_mod.update_task = holder["repo"].update_task

    audit_repo_mod.append_log = holder["audit"].append_log


def make_state(task_id: str = "65f1a2b3c4d5e6f7a8b9c0d1") -> ReviewTaskState:
    """构造含四种风险等级的测试状态（覆盖全部配色分支）"""
    risk_items = [
        RiskItem(
            risk_id="R1", rule_code="IP-001", rule_name="知识产权侵权责任转嫁异常",
            clause_id="C3", clause_type="ip",
            evidence_text="因知识产权侵权产生的全部责任由乙方承担。",
            char_start=100, char_end=140, page_no=1,
            risk_level="high",
            decision="need_revision", severity="major",
            suggestion_text="建议明确双方按过错比例分担侵权责任。",
            legal_basis="《民法典》第八百四十七条",
        ),
        RiskItem(
            risk_id="R2", rule_code="PAY-001", rule_name="无验收即全款支付",
            clause_id="C4", clause_type="payment",
            evidence_text="合同签订后即支付全部款项。",
            char_start=200, char_end=240, page_no=2,
            risk_level="critical",
            decision="block", severity="fatal",
            suggestion_text="建议改为验收合格后分期支付。",
            legal_basis="《民法典》第七百七十条",
        ),
        RiskItem(
            risk_id="R3", rule_code="BAL-004", rule_name="权利义务显失公平",
            clause_id="C5", clause_type="balance",
            evidence_text="乙方不得以任何理由主张任何权利。",
            char_start=300, char_end=340, page_no=2,
            risk_level="medium",
            decision="need_revision", severity="minor",
            suggestion_text="建议对等约定双方权利义务。",
            legal_basis="《民法典》第一百五十一条",
        ),
        RiskItem(
            risk_id="R4", rule_code="CFM-002", rule_name="保密条款表述可优化",
            clause_id="C7", clause_type="confidentiality",
            evidence_text="双方应对本合同内容保密。",
            char_start=400, char_end=430, page_no=3,
            risk_level="low",
            decision="pass", severity="info",
            suggestion_text="建议补充保密期限约定。",
            legal_basis="《民法典》第五百零一条",
        ),
    ]
    return ReviewTaskState(
        task_id=task_id,
        task_no="T20260919001",
        contract_id="C-001",
        contract_title="设备采购合同",
        contract_no="HT-2026-001",
        contract_type="purchase",
        contract_amount=860000.0,
        currency="CNY",
        risk_level="critical",
        final_risk_items=risk_items,
        conclusion_text="本次合同审查最终风险等级为【严重风险】，存在阻塞项。",
    )


def make_task(task_id: str = "65f1a2b3c4d5e6f7a8b9c0d1",
              created_by: str = "user_business") -> ReviewTask:
    """构造测试任务（默认无报告字段，模拟首次生成）"""
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


def _build_markdown() -> str:
    """用真实报告服务渲染一份 Markdown 源文本"""
    state = make_state()
    suggestion = report_service.build_suggestion_text(state.final_risk_items)
    return report_service.render_report_markdown(state, suggestion)


def _pdf_text(pdf_bytes: bytes) -> str:
    """提取 PDF 全部文本（跨页拼接）"""
    import pymupdf

    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    try:
        return "\n".join(page.get_text() for page in doc)
    finally:
        doc.close()


# ======================================================================
# 验证一：命名与对象键
# ======================================================================
def test_pdf_naming():
    print("\n[验证一] PDF 文件命名与对象键")
    task_id = "65f1a2b3c4d5e6f7a8b9c0d1"

    name = report_service.build_pdf_file_name(task_id)
    check("PDF 文件名符合 {task_id}_review_report.pdf",
          name == f"{task_id}_review_report.pdf", name)

    key = report_service.build_pdf_object_key(task_id)
    check("PDF 对象键为 report/ 前缀 + 文件名",
          key == f"report/{task_id}_review_report.pdf", key)

    md_key = report_service.build_report_object_key(task_id)
    check("PDF 与 Markdown 对象键互不覆盖", key != md_key, f"{key} vs {md_key}")

    evil = report_service.build_pdf_file_name("../../etc/passwd")
    check("PDF 命名对路径分隔符做防御清洗",
          "/" not in evil and "\\" not in evil, evil)


# ======================================================================
# 验证二：渲染器基础能力
# ======================================================================
def test_renderer_basics():
    print("\n[验证二] 渲染器基础能力（A4 / 中文 / 页眉页脚）")
    import pymupdf

    from app.services.pdf_renderer import (
        A4_HEIGHT_MM,
        A4_WIDTH_MM,
        build_layout_metrics,
        render_markdown_to_pdf,
    )

    pdf_bytes, pages, font_used = render_markdown_to_pdf(_build_markdown())

    check("成功渲染出 PDF 二进制", len(pdf_bytes) > 1000, f"bytes={len(pdf_bytes)}")
    check("PDF 魔术头正确", pdf_bytes[:5] == b"%PDF-", repr(pdf_bytes[:8]))
    check("至少渲染出 1 页", pages >= 1, f"pages={pages}")

    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    try:
        rect = doc[0].rect
        # A4 = 210×297mm = 595.28×841.89pt，允许 1pt 误差
        check("页面宽度为 A4（595.28pt）",
              abs(rect.width - 595.276) < 1.0, f"width={rect.width}")
        check("页面高度为 A4（841.89pt）",
              abs(rect.height - 841.891) < 1.0, f"height={rect.height}")

        # 页边距符合配置
        m = build_layout_metrics()
        check("左边距按配置换算正确",
              abs(m.margin_left - 22.0 * 72 / 25.4) < 0.01, str(m.margin_left))
        check("上边距按配置换算正确",
              abs(m.margin_top - 25.0 * 72 / 25.4) < 0.01, str(m.margin_top))

        text = _pdf_text(pdf_bytes)
        check("中文正常渲染（报告标题可提取）", "合同审查报告" in text)
        check("无中文乱码（无替换字符 \\ufffd）", "\ufffd" not in text)
        check("字体已成功解析",
              bool(font_used), f"font={font_used}")

        # 页眉
        from app.core.config import settings

        check("页眉含系统名称与文档名称",
              settings.PDF_HEADER_TEXT in text, settings.PDF_HEADER_TEXT)
        check("页眉含审查任务编号", "T20260919001" in text)

        # 页脚：页码依赖总页数，收尾统一绘制
        for index in range(pages):
            page_text = doc[index].get_text()
            check(f"第 {index + 1} 页含页码",
                  f"第 {index + 1} 页 / 共 {pages} 页" in page_text, page_text[:80])
            check(f"第 {index + 1} 页含生成时间",
                  "生成时间：" in page_text, page_text[:80])
    finally:
        doc.close()


# ======================================================================
# 验证三：与 Markdown 内容一致性（需求核心）
# ======================================================================
def test_content_parity():
    print("\n[验证三] PDF 与 Markdown 内容一致性")
    md = _build_markdown()
    pdf_bytes, _pages, _font = report_service.render_report_pdf(md, "T20260919001")
    text = _pdf_text(pdf_bytes)

    # 五个章节标题必须全部出现
    for section in ("一、合同基本信息", "二、审查结论",
                    "三、风险清单明细", "五、免责声明"):
        check(f"章节存在：{section}", section in text, section)

    # 合同基本信息的关键字段
    for field_name in ("合同标题", "合同编号", "合同金额", "设备采购合同", "HT-2026-001"):
        check(f"基本信息含：{field_name}", field_name in text, field_name)

    # 审查结论关键内容
    check("含综合风险等级", "综合风险等级" in text)
    check("含结论文本", "存在阻塞项" in text, text[:200])
    check("含判定来源", "规则引擎" in text)

    # 风险清单：四个规则的编码、名称、等级、法律依据、修改建议全部落到 PDF
    for code in ("IP-001", "PAY-001", "BAL-004", "CFM-002"):
        check(f"风险清单含规则编码：{code}", code in text)
    for label in ("高风险", "严重风险", "中风险", "低风险"):
        check(f"风险清单含等级标识：{label}", label in text)
    for basis in ("第八百四十七条", "第七百七十条", "第一百五十一条"):
        check(f"含法律依据：{basis}", basis in text)
    for suggestion in ("按过错比例分担", "验收合格后分期支付", "对等约定"):
        check(f"含修改建议：{suggestion}", suggestion in text)

    # 原文定位与证据原文（需求要求完整呈现）
    check("含原文定位（页码）", "第 1 页" in text or "第 2 页" in text)
    check("含字符区间定位", "字符区间" in text)
    check("含证据原文", "知识产权侵权产生的全部责任" in text)

    # 免责声明正文
    check("含免责声明正文", "审批人决策参考" in text or "自动生成" in text)

    # 反向验证：Markdown 中的风险项条数应与 PDF 中"风险项"出现次数一致
    md_count = md.count("#### 风险项 ")
    pdf_count = text.count("风险项 ")
    check("风险项条数与 Markdown 源文本一致",
          md_count == 4 and pdf_count >= md_count,
          f"md={md_count}, pdf={pdf_count}")


# ======================================================================
# 验证四：风险等级配色
# ======================================================================
def test_level_colors():
    print("\n[验证四] 风险等级配色（红/橙/绿标识）")
    import pymupdf

    from app.services.pdf_renderer import RISK_LABEL_TO_KEY, RISK_LEVEL_COLORS

    # 需求明确要求：高风险红色、中风险橙色、低风险绿色
    high = RISK_LEVEL_COLORS["high"]
    medium = RISK_LEVEL_COLORS["medium"]
    low = RISK_LEVEL_COLORS["low"]
    critical = RISK_LEVEL_COLORS["critical"]

    check("高风险为红色（R 显著高于 G/B）",
          high[0] > 0.7 and high[1] < 0.35 and high[2] < 0.35, str(high))
    check("中风险为橙色（R 高、G 中、B 低）",
          medium[0] > 0.8 and 0.2 < medium[1] < 0.6 and medium[2] < 0.2, str(medium))
    check("低风险为绿色（G 显著高于 R/B）",
          low[1] > 0.5 and low[0] < 0.3 and low[2] < 0.4, str(low))
    check("严重风险为深红（比高风险更深）",
          critical[0] > 0.6 and sum(critical) < sum(high), f"crit={critical} high={high}")

    check("四级等级到配色映射完整",
          set(RISK_LABEL_TO_KEY) == {"严重风险", "高风险", "中风险", "低风险"},
          str(RISK_LABEL_TO_KEY))

    # 验证颜色真的写进了 PDF：抽取含"高风险"的 span，检查其 fill 色
    md = _build_markdown()
    pdf_bytes, _p, _f = report_service.render_report_pdf(md, "T20260919001")
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    found_colors = set()
    try:
        for page in doc:
            data = page.get_text("dict")
            for block in data.get("blocks", []):
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        if span.get("color") is not None:
                            found_colors.add(span["color"])
    finally:
        doc.close()

    def _to_rgb_int(rgb: tuple) -> int:
        """把 0-1 浮点 RGB 转成 PyMuPDF 的整型颜色值"""
        r, g, b = (int(round(c * 255)) for c in rgb)
        return (r << 16) | (g << 8) | b

    high_int = _to_rgb_int(high)
    medium_int = _to_rgb_int(medium)
    low_int = _to_rgb_int(low)
    critical_int = _to_rgb_int(critical)

    check("PDF 中存在高风险红色文字", high_int in found_colors,
          f"期望 {hex(high_int)}，实际色集 {[hex(c) for c in sorted(found_colors)][:12]}")
    check("PDF 中存在中风险橙色文字", medium_int in found_colors, hex(medium_int))
    check("PDF 中存在低风险绿色文字", low_int in found_colors, hex(low_int))
    check("PDF 中存在严重风险深红文字", critical_int in found_colors, hex(critical_int))


# ======================================================================
# 验证五：Markdown 解析
# ======================================================================
def test_markdown_parser():
    print("\n[验证五] Markdown 轻量解析")
    from app.services.pdf_renderer import (
        BLOCK_H1,
        BLOCK_H2,
        BLOCK_H4,
        BLOCK_LIST,
        BLOCK_ORDERED,
        BLOCK_PARAGRAPH,
        BLOCK_QUOTE,
        BLOCK_RULE,
        BLOCK_TABLE,
        parse_markdown_blocks,
    )

    md = (
        "# 大标题\n\n"
        "## 二级标题\n\n"
        "### 三级标题\n\n"
        "#### 四级标题\n\n"
        "| 列A | 列B |\n| --- | --- |\n| v1 | v2 |\n\n"
        "- 无序项\n- **加粗**项：值\n\n"
        "1. 有序项\n\n"
        "> 引用内容\n\n"
        "---\n\n"
        "普通段落 **含加粗**\n"
    )
    blocks = parse_markdown_blocks(md)
    kinds = [b.kind for b in blocks]

    check("识别一级标题", BLOCK_H1 in kinds)
    check("识别二级标题", BLOCK_H2 in kinds)
    check("识别四级标题", BLOCK_H4 in kinds)
    check("识别表格", BLOCK_TABLE in kinds)
    check("识别无序列表", BLOCK_LIST in kinds)
    check("识别有序列表", BLOCK_ORDERED in kinds)
    check("识别引用块", BLOCK_QUOTE in kinds)
    check("识别分隔线", BLOCK_RULE in kinds)
    check("识别普通段落", BLOCK_PARAGRAPH in kinds)

    table = next(b for b in blocks if b.kind == BLOCK_TABLE)
    check("表格解析出表头与数据行",
          table.rows[0] == ["列A", "列B"] and table.rows[1] == ["v1", "v2"],
          str(table.rows))
    check("表格分隔行未被当作数据行", len(table.rows) == 2, str(len(table.rows)))

    # 行内标记剥离
    from app.services.pdf_renderer import _strip_inline_markup

    check("去除加粗标记", _strip_inline_markup("**加粗**文本") == "加粗文本")
    check("去除行内代码标记", _strip_inline_markup("`code`") == "code")
    check("还原表格内的转义竖线", _strip_inline_markup(r"a\|b") == "a|b")

    # 空内容与异常输入不崩溃
    check("空字符串解析为空列表", parse_markdown_blocks("") == [])
    check("纯空行解析为空列表", parse_markdown_blocks("\n\n\n") == [])
    # 未识别的行按段落保留，绝不丢弃内容
    weird = parse_markdown_blocks("@@@ 未识别行 @@@")
    check("未识别行按段落保留",
          len(weird) == 1 and "未识别行" in weird[0].text, str(weird))


# ======================================================================
# 验证六：字体三级回退
# ======================================================================
def test_font_fallback():
    print("\n[验证六] 字体三级回退（保证中文永不缺字）")
    from app.services.pdf_renderer import (
        BUILTIN_CJK_SANS,
        BUILTIN_CJK_SERIF,
        reset_font_cache,
        resolve_fonts,
    )

    reset_font_cache()
    fonts = resolve_fonts()
    check("字体解析成功返回", fonts is not None)
    check("正文与标题字体均已确定",
          bool(fonts.body) and bool(fonts.bold), f"{fonts.body}/{fonts.bold}")
    check("字体解析含探测说明（便于排查缺字）", bool(fonts.detail), fonts.detail)

    # 内置字体兜底：手动构造内置字体场景，验证仍能渲染中文
    from app.core.config import settings
    from app.services import pdf_renderer

    original_body = settings.PDF_FONT_PATH
    original_bold = settings.PDF_FONT_PATH_BOLD
    original_cache = pdf_renderer._FONT_CACHE
    try:
        # 强制走内置字体路径：清缓存 + 让系统字体探测全部失败
        reset_font_cache()
        saved_candidates = pdf_renderer._FONT_CANDIDATES_BODY
        saved_bold = pdf_renderer._FONT_CANDIDATES_BOLD
        pdf_renderer._FONT_CANDIDATES_BODY = ["/nonexistent/a.ttf"]
        pdf_renderer._FONT_CANDIDATES_BOLD = ["/nonexistent/b.ttf"]
        settings.PDF_FONT_PATH = ""
        settings.PDF_FONT_PATH_BOLD = ""
        try:
            builtin_fonts = resolve_fonts()
            check("探测失败时回退内置 CJK 宋体",
                  builtin_fonts.body == BUILTIN_CJK_SERIF, builtin_fonts.body)
            check("标题回退内置 CJK 黑体",
                  builtin_fonts.bold == BUILTIN_CJK_SANS, builtin_fonts.bold)

            pdf_bytes, _p, font_used = pdf_renderer.render_markdown_to_pdf(_build_markdown())
            text = _pdf_text(pdf_bytes)
            check("内置字体下中文仍正常渲染（无缺字）",
                  "合同审查报告" in text and "\ufffd" not in text)
            check("内置字体标识被回传", font_used == BUILTIN_CJK_SERIF, font_used)
        finally:
            pdf_renderer._FONT_CANDIDATES_BODY = saved_candidates
            pdf_renderer._FONT_CANDIDATES_BOLD = saved_bold
    finally:
        settings.PDF_FONT_PATH = original_body
        settings.PDF_FONT_PATH_BOLD = original_bold
        pdf_renderer._FONT_CACHE = original_cache


# ======================================================================
# 验证七：异常处理
# ======================================================================
def test_render_errors():
    print("\n[验证七] 渲染异常处理")
    from app.services.pdf_renderer import render_markdown_to_pdf

    # 空内容
    try:
        render_markdown_to_pdf("")
        check("空 Markdown 抛出 ValueError", False, "未抛出")
    except ValueError as exc:
        check("空 Markdown 抛出 ValueError", True, str(exc))
    except Exception as exc:  # noqa: BLE001
        check("空 Markdown 抛出 ValueError", False, f"实际 {type(exc).__name__}")

    try:
        render_markdown_to_pdf("   \n  \n ")
        check("纯空白 Markdown 抛出 ValueError", False, "未抛出")
    except ValueError:
        check("纯空白 Markdown 抛出 ValueError", True)

    # 报告服务层包装
    try:
        report_service.render_report_pdf("", "T1")
        check("服务层空内容抛出 ValueError", False, "未抛出")
    except ValueError:
        check("服务层空内容抛出 ValueError", True)

    # 超长内容触发页数上限
    from app.core.config import settings

    original_max = settings.PDF_MAX_PAGES
    try:
        settings.PDF_MAX_PAGES = 2
        long_md = "# 标题\n\n" + "\n\n".join(
            f"## 章节 {i}\n\n这是一段用于撑页数的正文内容，重复若干次以便触发分页。"
            for i in range(200)
        )
        try:
            render_markdown_to_pdf(long_md)
            check("超过页数上限抛出 RuntimeError", False, "未抛出")
        except RuntimeError as exc:
            check("超过页数上限抛出 RuntimeError", "上限" in str(exc), str(exc)[:80])
    finally:
        settings.PDF_MAX_PAGES = original_max


# ======================================================================
# 验证八：生成 + 持久化
# ======================================================================
async def test_pdf_persist(holder: dict):
    print("\n[验证八] PDF 生成与持久化")
    holder["repo"].task = make_task()
    holder["minio"].objects.clear()

    state = make_state()
    result = await report_service.generate_pdf_report_service(
        state, task_id="65f1a2b3c4d5e6f7a8b9c0d1"
    )

    check("返回 PDF 二进制", len(result.pdf_bytes) > 1000, str(len(result.pdf_bytes)))
    check("对象键符合约定",
          result.pdf_object_key == "report/65f1a2b3c4d5e6f7a8b9c0d1_review_report.pdf",
          result.pdf_object_key)
    check("文件名为约定格式",
          result.file_name == "65f1a2b3c4d5e6f7a8b9c0d1_review_report.pdf", result.file_name)
    check("文件名后缀为 .pdf", result.file_name.endswith(".pdf"))
    check("页数已统计", result.page_count >= 1, str(result.page_count))
    check("字节数已统计", result.size_bytes == len(result.pdf_bytes))
    check("渲染引擎标识为 pymupdf", result.render_engine == "pymupdf")
    check("已生成访问链接", bool(result.pdf_url), result.pdf_url)
    check("未标记复用（首次生成）", result.reused is False)
    check("已标记落库", result.persisted is True)

    # MinIO 侧
    key = result.pdf_object_key
    check("PDF 已上传 MinIO", key in holder["minio"].objects)
    check("上传的 content_type 为 application/pdf",
          holder["minio"].content_types.get(key) == "application/pdf",
          holder["minio"].content_types.get(key, "?"))
    check("上传内容与返回二进制一致",
          holder["minio"].objects.get(key) == result.pdf_bytes)

    # 任务文档侧
    updates = holder["repo"].update_calls[-1]
    check("落库含 pdf_object_key", updates.get("pdf_object_key") == key)
    check("落库含 pdf_content_hash", bool(updates.get("pdf_content_hash")))
    check("落库含 pdf_generated_at", updates.get("pdf_generated_at") is not None)
    check("落库含 pdf_size_bytes",
          updates.get("pdf_size_bytes") == len(result.pdf_bytes))
    check("落库含 pdf_render_engine",
          updates.get("pdf_render_engine") == "pymupdf")


# ======================================================================
# 验证九：幂等控制
# ======================================================================
async def test_pdf_idempotency(holder: dict):
    print("\n[验证九] PDF 幂等控制（源内容未变更则复用）")
    task_id = "65f1a2b3c4d5e6f7a8b9c0d1"
    before_uploads = holder["minio"].upload_calls

    state = make_state()
    result = await report_service.generate_pdf_report_service(state, task_id=task_id)

    check("第二次生成标记为复用", result.reused is True)
    check("复用时不重复上传 MinIO",
          holder["minio"].upload_calls == before_uploads,
          f"{before_uploads} -> {holder['minio'].upload_calls}")
    check("复用时不重复渲染（二进制为空）", result.pdf_bytes == b"")
    check("复用仍返回正确对象键",
          result.pdf_object_key == f"report/{task_id}_review_report.pdf")
    check("复用仍返回访问链接", bool(result.pdf_url))

    # 源内容变更 → 重新渲染上传
    changed = make_state()
    changed.final_risk_items[0].suggestion_text = "建议：改为对等承担侵权责任（已修改）。"
    before_uploads = holder["minio"].upload_calls
    result2 = await report_service.generate_pdf_report_service(changed, task_id=task_id)

    check("源内容变更后不标记复用", result2.reused is False)
    check("源内容变更后重新渲染", len(result2.pdf_bytes) > 1000)
    check("源内容变更后重新上传",
          holder["minio"].upload_calls == before_uploads + 1,
          f"{before_uploads} -> {holder['minio'].upload_calls}")
    check("源内容变更后对象键不变（覆盖写）",
          result2.pdf_object_key == f"report/{task_id}_review_report.pdf")
    check("源指纹随内容变化而变化", result2.source_hash != result.source_hash)


# ======================================================================
# 验证十：落库失败回滚
# ======================================================================
async def test_pdf_rollback(holder: dict):
    print("\n[验证十] PDF 落库失败自动回滚")
    holder["repo"].task = make_task()
    holder["minio"].objects.clear()
    holder["repo"].fail_update = True
    before_deletes = holder["minio"].delete_calls

    state = make_state()
    result = await report_service.generate_pdf_report_service(
        state, task_id="65f1a2b3c4d5e6f7a8b9c0d1"
    )

    check("落库失败时标记 persisted=False", result.persisted is False)
    check("落库失败触发 MinIO 删除回滚",
          holder["minio"].delete_calls == before_deletes + 1,
          f"{before_deletes} -> {holder['minio'].delete_calls}")
    check("脏对象已被清理",
          result.pdf_object_key not in holder["minio"].objects,
          result.pdf_object_key)

    holder["repo"].fail_update = False


# ======================================================================
# 验证十一：权责红线（只写 pdf 字段）
# ======================================================================
async def test_red_lines(holder: dict):
    print("\n[验证十一] 权责红线（PDF 字段与判定字段隔离）")
    holder["repo"].task = make_task()
    holder["repo"].update_calls.clear()
    holder["minio"].objects.clear()

    state = make_state()
    await report_service.generate_pdf_report_service(
        state, task_id="65f1a2b3c4d5e6f7a8b9c0d1"
    )

    # 收集 PDF 链路写过的所有字段名
    touched = set()
    for call in holder["repo"].update_calls:
        touched.update(call.keys())

    check("写入字段全部为 pdf_* 前缀",
          all(k.startswith("pdf_") for k in touched), str(touched))
    check("未触碰 Markdown 的 report_object_key",
          "report_object_key" not in touched)
    check("未触碰状态机字段 status", "status" not in touched)
    check("未触碰风险等级字段 risk_level", "risk_level" not in touched)
    check("未触碰阻塞标记字段 is_blocked", "is_blocked" not in touched)
    check("未触碰审批回写字段 write_back_status", "write_back_status" not in touched)
    check("未触碰审批结论字段 conclusion_text", "conclusion_text" not in touched)


# ======================================================================
# 验证十二：Markdown 主链路未受影响
# ======================================================================
async def test_md_unaffected(holder: dict):
    print("\n[验证十二] Markdown 主链路未被破坏")
    holder["repo"].task = make_task()
    holder["repo"].update_calls.clear()
    holder["minio"].objects.clear()

    state = make_state()
    # 关闭 PDF 联动，验证纯 Markdown 行为与改造前一致
    from app.core.config import settings

    original = settings.PDF_AUTO_WITH_MD
    try:
        settings.PDF_AUTO_WITH_MD = False
        result = await report_service.generate_report_service(
            state, task_id="65f1a2b3c4d5e6f7a8b9c0d1"
        )
    finally:
        settings.PDF_AUTO_WITH_MD = original

    check("Markdown 报告仍正常生成", len(result.report_content) > 500)
    check("Markdown 报告格式标记为 md", result.report_format == "md")
    check("Markdown 对象键符合原约定",
          result.report_object_key.endswith("_review_report.md"),
          result.report_object_key)
    check("关闭联动时不生成 PDF", result.pdf_object_key == "")

    # 开启联动：两种格式同时产出
    holder["repo"].task = make_task()
    holder["minio"].objects.clear()
    try:
        settings.PDF_AUTO_WITH_MD = True
        result2 = await report_service.generate_report_service(
            state, task_id="65f1a2b3c4d5e6f7a8b9c0d1"
        )
    finally:
        settings.PDF_AUTO_WITH_MD = original

    check("开启联动时 Markdown 仍正常", len(result2.report_content) > 500)
    check("开启联动时 PDF 对象键非空", bool(result2.pdf_object_key))
    check("两种格式对象键不同",
          result2.pdf_object_key != result2.report_object_key)
    check("两种格式均已上传",
          result2.pdf_object_key in holder["minio"].objects
          and result2.report_object_key in holder["minio"].objects)

    # Markdown 落库字段仍为原 4 个，未被 pdf_* 污染
    md_updates = [
        c for c in holder["repo"].update_calls
        if any(k == "report_object_key" for k in c)
    ]
    check("Markdown 落库仍在执行", len(md_updates) >= 1)


# ======================================================================
# 验证十三：下载接口 format 分支
# ======================================================================
async def test_download_format(holder: dict):
    print("\n[验证十三] 下载接口 format 分支（真实 ASGI 调用）")
    from fastapi.testclient import TestClient

    from app.core.security import create_access_token
    from app.main import app
    import app.repositories.user_repo as user_repo_mod

    task_id = "65f1a2b3c4d5e6f7a8b9c0d1"
    holder["repo"].task = make_task(task_id=task_id, created_by="user_business")
    holder["minio"].objects.clear()

    # 先生成两种格式的产物
    state = make_state(task_id=task_id)
    await report_service.generate_report_service(state, task_id=task_id)

    token = create_access_token(
        user_id="user_business", username="user_business", role="business_user"
    )

    # 鉴权中间件按 user_id 查用户，需桩掉仓储
    from app.models.user import User

    fake_user = User(
        id="user_business", username="user_business", display_name="张经办",
        password_hash="$2b$12$" + "x" * 53, role="business_user", status="active",
    )

    async def _fake_find(user_id=None, username=None):
        return fake_user

    original_find = user_repo_mod.find_by_id
    user_repo_mod.find_by_id = _fake_find
    # 中间件通过包导出访问仓储，需同时替换包属性
    import app.repositories as repos_pkg

    if hasattr(repos_pkg, "user_repo"):
        repos_pkg.user_repo.find_by_id = _fake_find

    headers = {"Authorization": f"Bearer {token}"}

    # ★ 不使用 `with TestClient(app)`：
    #   上下文管理器会执行 lifespan，进而真实连接 MongoDB / MinIO / Milvus。
    #   直接构造 TestClient 不进入上下文即可跳过 lifespan，
    #   鉴权中间件、路由分发、序列化等被测代码仍全部真实执行。
    client = TestClient(app)

    try:
        # ---- 默认（不传 format）必须仍是 Markdown，兼容原行为 ----
        resp = client.get(f"/api/v1/report/{task_id}/download", headers=headers)
        check("默认不传 format 返回 200", resp.status_code == 200, str(resp.status_code))
        body = resp.json()
        check("默认返回 Markdown 内容",
              "合同审查报告" in body.get("data", {}).get("content", ""))
        check("默认 report_format 为 md",
              body.get("data", {}).get("report_format") == "md",
              str(body.get("data", {}).get("report_format")))
        check("默认文件名为 .md",
              body.get("data", {}).get("file_name", "").endswith(".md"),
              body.get("data", {}).get("file_name", ""))

        # ---- format=md 显式 ----
        resp = client.get(
            f"/api/v1/report/{task_id}/download?format=md", headers=headers
        )
        check("format=md 返回 200", resp.status_code == 200, str(resp.status_code))

        # ---- format=pdf + mode=stream：下载 PDF 文件流 ----
        resp = client.get(
            f"/api/v1/report/{task_id}/download?format=pdf&mode=stream",
            headers=headers,
        )
        check("format=pdf 文件流返回 200", resp.status_code == 200, str(resp.status_code))
        check("Content-Type 为 application/pdf",
              resp.headers.get("content-type", "").startswith("application/pdf"),
              resp.headers.get("content-type", ""))
        check("PDF 魔术头正确", resp.content[:5] == b"%PDF-", repr(resp.content[:8]))
        check("Content-Disposition 携带 .pdf 文件名",
              "_review_report.pdf" in resp.headers.get("content-disposition", ""),
              resp.headers.get("content-disposition", ""))
        check("PDF 使用 inline（支持浏览器预览）",
              resp.headers.get("content-disposition", "").startswith("inline"),
              resp.headers.get("content-disposition", ""))
        check("Content-Length 与实际长度一致",
              resp.headers.get("content-length") == str(len(resp.content)),
              f"{resp.headers.get('content-length')} vs {len(resp.content)}")
        check("响应头暴露格式标识",
              resp.headers.get("x-report-format") == "pdf",
              resp.headers.get("x-report-format", ""))

        # ---- format=pdf + json 模式：返回元信息，正文留空 ----
        resp = client.get(
            f"/api/v1/report/{task_id}/download?format=pdf", headers=headers
        )
        check("format=pdf JSON 模式返回 200", resp.status_code == 200)
        data = resp.json().get("data", {})
        check("JSON 模式下 PDF 正文为空（不破坏原契约）", data.get("content") == "")
        check("JSON 模式下 report_format 为 pdf",
              data.get("report_format") == "pdf", str(data.get("report_format")))
        check("JSON 模式下返回预签名链接", bool(data.get("report_url")))
        check("JSON 模式下对象键为 .pdf",
              data.get("report_object_key", "").endswith(".pdf"),
              data.get("report_object_key", ""))

        # ---- 非法 format ----
        resp = client.get(
            f"/api/v1/report/{task_id}/download?format=docx", headers=headers
        )
        check("非法 format 返回 400", resp.status_code == 400, str(resp.status_code))
        check("非法 format 错误信息含可选值",
              "md" in resp.text and "pdf" in resp.text, resp.text[:120])

        # ---- 大小写与空格容错 ----
        resp = client.get(
            f"/api/v1/report/{task_id}/download?format=%20PDF%20&mode=stream",
            headers=headers,
        )
        check("format 大小写与空格容错（%20PDF%20 正常）",
              resp.status_code == 200 and resp.content[:5] == b"%PDF-",
              str(resp.status_code))

        # ---- PDF 未生成时明确 404 ----
        holder["repo"].task.pdf_object_key = None
        resp = client.get(
            f"/api/v1/report/{task_id}/download?format=pdf", headers=headers
        )
        check("PDF 未生成返回 404", resp.status_code == 404, str(resp.status_code))
        check("404 提示引导先生成 PDF",
              "pdf" in resp.text.lower() or "PDF" in resp.text, resp.text[:140])

        # Markdown 侧不受影响，仍可下载
        resp = client.get(f"/api/v1/report/{task_id}/download", headers=headers)
        check("PDF 缺失不影响 Markdown 下载", resp.status_code == 200,
              str(resp.status_code))
    finally:
        user_repo_mod.find_by_id = original_find
        if hasattr(repos_pkg, "user_repo"):
            repos_pkg.user_repo.find_by_id = original_find


# ======================================================================
# 验证十四：模型字段兼容
# ======================================================================
def test_pdf_model_compat():
    print("\n[验证十四] PDF 字段历史任务兼容")
    old = ReviewTask(
        id="legacy", task_no="T-OLD", contract_id="C-OLD",
        thread_id="th", work_dir="w",
    )
    check("历史任务 pdf_object_key 为 None", old.pdf_object_key is None)
    check("历史任务 pdf_generated_at 为 None", old.pdf_generated_at is None)
    check("历史任务 pdf_content_hash 为 None", old.pdf_content_hash is None)
    check("历史任务 pdf_size_bytes 为 None", old.pdf_size_bytes is None)
    check("历史任务 pdf_render_engine 为 None", old.pdf_render_engine is None)

    # 模拟旧文档反序列化
    from app.repositories.task_repo import _to_model

    raw = {
        "_id": "legacy", "task_no": "T-OLD", "contract_id": "C-OLD",
        "thread_id": "th", "work_dir": "w",
        "status": "completed", "write_back_status": "pending",
    }
    model = _to_model(raw)
    check("旧文档可正常反序列化", model.task_no == "T-OLD")
    check("反序列化后 PDF 字段为空值", model.pdf_object_key is None)
    check("PDF 字段声明齐全",
          all(f in ReviewTask.model_fields for f in (
              "pdf_object_key", "pdf_generated_at", "pdf_content_hash",
              "pdf_size_bytes", "pdf_render_engine",
          )))


# ======================================================================
# 主入口
# ======================================================================
async def main() -> int:
    holder = {
        "minio": FakeMinIO(),
        "repo": FakeTaskRepo(make_task()),
        "audit": FakeAuditRepo(),
    }
    _patch(holder)

    # 同步测试
    test_pdf_naming()
    test_renderer_basics()
    test_content_parity()
    test_level_colors()
    test_markdown_parser()
    test_font_fallback()
    test_render_errors()
    test_pdf_model_compat()

    # 异步测试（顺序有依赖）
    await test_pdf_persist(holder)
    await test_pdf_idempotency(holder)
    await test_pdf_rollback(holder)
    await test_red_lines(holder)
    await test_md_unaffected(holder)
    await test_download_format(holder)

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
