# D:/ContractReviewSys-合同审批智能审查项目/backend/tests/verify_parse_service.py
"""
parse_service 端到端验证脚本

【验证目标】
    1. 文本块全局字符偏移的精确性（full_text[start_idx:end_idx] == block.text）
    2. page_texts 为 List[str] 且索引 0 对应第 1 页
    3. 幂等校验：同一文件第二次调用命中缓存
    4. 三类法定阻断：DOC_ENCRYPTED / EMPTY_CONTENT / SCAN_TOO_BLURRY
    5. 元数据抽取（金额 / 主体 / 信用代码 / 履约期限）
    6. 节点适配入口的异常转换语义

【运行方式】
    cd backend && python tests/verify_parse_service.py

【说明】
    本脚本是"验证脚本"而非"测试框架用例"，用最直接的断言输出结果，
    便于人工逐项确认。断言失败会抛出 AssertionError 并中断。
"""

import asyncio
import os
import sys
import zlib

# 将 backend 加入模块搜索路径，使脚本可独立运行
BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from app.services import parse_service
from app.services.metadata_extractor import validate_credit_code

PASSED = []
FAILED = []


def check(name: str, condition: bool, detail: str = "") -> None:
    """记录单项断言结果"""
    if condition:
        PASSED.append(name)
        print(f"  [PASS] {name}")
    else:
        FAILED.append((name, detail))
        print(f"  [FAIL] {name} | {detail}")


# ======================================================================
# 测试样本构造工具
# ======================================================================

def make_credit_code(prefix: str = "91110108") -> str:
    """
    按 GB 32100-2015 生成合法统一社会信用代码

    参数：
        prefix: 8 位前缀（登记管理部门 + 机构类别 + 行政区划）
    返回：
        18 位合法信用代码
    """
    chars = "0123456789ABCDEFGHJKLMNPQRTUWXY"
    weights = (1, 3, 9, 27, 19, 26, 16, 17, 20, 29, 25, 13, 8, 24, 10, 30, 28)
    body = (prefix + "MA01ABCDE").upper()  # 共 17 位（8 位前缀 + 9 位主体标识）
    assert len(body) == 17, f"前缀长度错误: {len(body)}"
    total = sum(chars.index(c) * w for c, w in zip(body, weights))
    check_digit = (31 - total % 31) % 31
    return body + chars[check_digit]


def build_pdf(pages_text, header: str = "", footer: str = "") -> bytes:
    """
    构造含真实文字层与真实中文的 PDF（使用 PyMuPDF 生成）

    【为什么不手工拼装 PDF】
        手工拼装时只能使用内置字体（Helvetica 等），这类字体不含 CJK 字形，
        中文会被 PDF 工具替换为 "?"，导致 all-ASCII 文本层 —— 那会掩盖
        真实的解析行为，甚至误触发 OCR 降级判定。
        因此这里用 PyMuPDF 的 insert_text 写入真实中文（内置 CJK 字体）。

    参数：
        pages_text: [[第1页段落列表], [第2页段落列表], ...]
        header: 每页顶部文本（用于测试页眉过滤）
        footer: 每页底部文本（用于测试页脚过滤）
    返回：
        PDF 文件字节
    """
    try:
        import pymupdf as fitz
    except ImportError:
        import fitz

    doc = fitz.open()

    for page_lines in pages_text:
        page = doc.new_page(width=612, height=792)

        # 页眉：放在顶部 8% 区域内（y < 792*0.08 ≈ 63）
        if header:
            page.insert_text(
                fitz.Point(60, 40), header, fontname="china-s", fontsize=12
            )

        # 正文：从 y=100 开始逐行
        y = 100.0
        for line in page_lines:
            page.insert_text(
                fitz.Point(60, y), line, fontname="china-s", fontsize=12
            )
            y += 20.0

        # 页脚：放在底部 8% 区域内（y > 792*0.92 ≈ 729）
        if footer:
            page.insert_text(
                fitz.Point(60, 760), footer, fontname="china-s", fontsize=12
            )

    pdf_bytes = doc.tobytes()
    doc.close()
    return pdf_bytes


def build_empty_pdf() -> bytes:
    """
    构造一个真正空白的 PDF（无任何文本内容）

    【为什么不用"只有 1 个字符"的 PDF】
        单字符文档在业务上属于"内容极少"，此时走 OCR 尝试补全是合理行为，
        不能算 EMPTY_CONTENT 阻断。
        真正应当触发 EMPTY_CONTENT 的是"文档完全没有任何可抽取文本"
        —— 典型场景是扫描失败产生的全白页、或损坏的电子文档。
        因此这里生成结构完整但页面无文字的 PDF。

    返回：
        空白 PDF 文件字节
    """
    try:
        import pymupdf as fitz
    except ImportError:
        import fitz

    doc = fitz.open()
    doc.new_page(width=612, height=792)   # 新建页但不写入任何文本
    pdf_bytes = doc.tobytes()
    doc.close()
    return pdf_bytes


def build_encrypted_pdf() -> bytes:
    """
    构造真实的加密 PDF（使用 PyMuPDF 的保存时加密能力）

    【为什么不用文本替换方式伪造】
        只在 trailer 里手写 "/Encrypt 99 0 R" 会破坏 xref 结构，
        PyMuPDF 打开时会直接报文件损坏 —— 那验证的是"损坏文件"而非"加密文件"。
        必须生成结构合法且真正带加密字典的 PDF，
        才能真正验证 DOC_ENCRYPTED 判定链路。

    返回：
        加密 PDF 文件字节
    """
    try:
        import pymupdf as fitz
    except ImportError:
        import fitz

    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text(
        fitz.Point(60, 100),
        "本文件已加密，正文内容不应被抽取。",
        fontname="china-s",
        fontsize=12,
    )

    # 权限位：仅允许打印，禁止复制（触发 PDF_PERM_COPY 检查）
    permissions = fitz.PDF_PERM_PRINT
    pdf_bytes = doc.tobytes(
        encryption=fitz.PDF_ENCRYPT_AES_256,
        owner_pw="owner-secret",
        user_pw="",              # 空用户口令：无需口令即可打开，但复制受限
        permissions=permissions,
    )
    doc.close()
    return pdf_bytes


def build_docx(paragraphs, tables=None) -> bytes:
    """
    构造最小 DOCX（手工拼装 OOXML + zip，无需 python-docx 写接口）

    参数：
        paragraphs: [(文本, 样式名)] 列表，样式名可为 None
        tables: [[[单元格文本, ...], ...], ...] 表格列表
    返回：
        DOCX 文件字节
    """
    import io
    import zipfile

    body_parts = []
    for text, style in paragraphs:
        style_xml = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
        body_parts.append(
            f'<w:p>{style_xml}<w:r><w:t xml:space="preserve">{_xml_escape(text)}</w:t></w:r></w:p>'
        )

    for table in tables or []:
        rows_xml = []
        for row in table:
            cells_xml = "".join(
                f'<w:tc><w:p><w:r><w:t xml:space="preserve">{_xml_escape(cell)}</w:t></w:r></w:p></w:tc>'
                for cell in row
            )
            rows_xml.append(f"<w:tr>{cells_xml}</w:tr>")
        body_parts.append(f"<w:tbl>{''.join(rows_xml)}</w:tbl>")

    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{''.join(body_parts)}</w:body></w:document>"
    )

    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    )

    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
        "</Relationships>"
    )

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("word/document.xml", document_xml)
    return buffer.getvalue()


def _xml_escape(text: str) -> str:
    """转义 XML 特殊字符"""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# 真实合同正文样本（用于验证元数据抽取）
CONTRACT_LINES = [
    "设备采购合同",
    "",
    "合同编号：HT-2026-0918-001",
    "",
    "甲方（采购方）：北京华信智能科技有限公司",
    "统一社会信用代码：__CODE_A__",
    "乙方（供应方）：上海鼎盛机械制造有限公司",
    "统一社会信用代码：__CODE_B__",
    "",
    "第一条 合同标的",
    "甲方向乙方采购工业机器人本体及配套控制系统，具体型号详见附件一。",
    "",
    "第二条 合同金额",
    "本合同总金额为人民币（大写）叁佰万元整（￥3,000,000.00元）。",
    "上述价款已包含设备费、运输费、安装调试费及税费。",
    "",
    "第三条 履约期限",
    "本合同自2026年10月1日起至2027年9月30日止，履行期限为一年。",
    "",
    "第四条 付款方式",
    "合同签订后七个工作日内，甲方支付合同总额的百分之三十作为预付款。",
    "",
    "第五条 交付与验收",
    "乙方应于2026年12月31日前完成全部设备交付，并通过甲方验收。",
    "",
    "第六条 违约责任",
    "任何一方违约，应向守约方支付合同总额百分之十的违约金。",
    "",
    "签订日期：2026年9月18日",
]


def build_contract_pdf() -> bytes:
    """构造含完整合同要素的多页 PDF 样本"""
    code_a = make_credit_code("91110108")
    code_b = make_credit_code("91310115")

    page1 = [
        line.replace("__CODE_A__", code_a).replace("__CODE_B__", code_b)
        for line in CONTRACT_LINES[:20]
    ]
    page2 = [line for line in CONTRACT_LINES[20:] if line]

    return build_pdf(
        [page1, page2],
        header="内部资料 严禁外传",
        footer="第 1 页 共 2 页",
    )


# ======================================================================
# 验证一：PDF 解析与偏移精确性
# ======================================================================

async def test_pdf_offsets():
    print("\n[验证一] PDF 解析与全局字符偏移精确性")
    parse_service.clear_parse_cache()

    pdf_bytes = build_contract_pdf()
    tmp_path = os.path.join(BACKEND_DIR, "storage", "tmp", "verify_contract.pdf")
    os.makedirs(os.path.dirname(tmp_path), exist_ok=True)
    with open(tmp_path, "wb") as f:
        f.write(pdf_bytes)

    result = await parse_service.parse_local_file(tmp_path, source_type="digital")

    check("PDF 页数为 2", result.doc_metadata.page_count == 2,
          f"实际={result.doc_metadata.page_count}")
    check("page_texts 类型为 List[str]",
          isinstance(result.page_texts, list) and all(isinstance(p, str) for p in result.page_texts),
          f"类型={type(result.page_texts)}")
    check("page_texts 长度等于页数", len(result.page_texts) == 2,
          f"实际={len(result.page_texts)}")
    check("解析引擎为 pdf_text", result.doc_metadata.parse_engine == "pdf_text",
          f"实际={result.doc_metadata.parse_engine}")
    check("存在文字层", result.doc_metadata.has_text_layer is True)

    # 核心断言：所有块的偏移必须精确
    all_exact = True
    bad_block = None
    for block in result.text_blocks:
        if result.full_text[block.start_idx:block.end_idx] != block.text:
            all_exact = False
            bad_block = block
            break
    check("全部文本块偏移精确（全量校验）", all_exact,
          f"首个失败块={bad_block.block_index if bad_block else None}")

    # full_text 拼接方式必须与 page_texts 一致
    check('full_text == "\\n".join(page_texts)',
          result.full_text == "\n".join(result.page_texts))

    # 页码归属正确
    page1_blocks = [b for b in result.text_blocks if b.page == 1]
    page2_blocks = [b for b in result.text_blocks if b.page == 2]
    check("第 1 页有文本块", len(page1_blocks) > 0, f"实际={len(page1_blocks)}")
    check("第 2 页有文本块", len(page2_blocks) > 0, f"实际={len(page2_blocks)}")

    # 第 2 页块的偏移必须落在第 2 页起始位置之后
    page2_start = len(result.page_texts[0]) + 1
    check("第 2 页块偏移 >= 第 2 页起始偏移",
          all(b.start_idx >= page2_start for b in page2_blocks) if page2_blocks else False,
          f"页2起始={page2_start}")

    # 页眉页脚必须被过滤
    check("页眉已过滤", "内部资料 严禁外传" not in result.full_text)
    check("页脚已过滤", "第 1 页 共 2 页" not in result.full_text)

    # 正文关键词必须保留
    check("正文关键词已保留（采购）", "采购" in result.full_text)
    check("标题块存在", any("设备采购合同" in b.text for b in result.text_blocks))

    # block_index 与 paragraph_index 单调递增
    bi = [b.block_index for b in result.text_blocks]
    pi = [b.paragraph_index for b in result.text_blocks]
    check("block_index 单调递增", bi == sorted(bi) and len(set(bi)) == len(bi))
    check("paragraph_index 单调递增", pi == sorted(pi))

    return result, tmp_path


# ======================================================================
# 验证二：元数据抽取
# ======================================================================

async def test_metadata(result):
    print("\n[验证二] 合同关键元数据抽取")
    meta = result.metadata

    check("合同编号已抽取", meta.get("contract_no") == "HT-2026-0918-001",
          f"实际={meta.get('contract_no')}")
    check("金额已抽取", meta.get("amount") == 3000000.0,
          f"实际={meta.get('amount')}")
    check("金额来源为大写优先", meta.get("amount_source") == "chinese_uppercase",
          f"实际={meta.get('amount_source')}")
    check("甲方名称已抽取",
          meta.get("party_a_name") == "北京华信智能科技有限公司",
          f"实际={meta.get('party_a_name')}")
    check("乙方名称已抽取",
          meta.get("party_b_name") == "上海鼎盛机械制造有限公司",
          f"实际={meta.get('party_b_name')}")
    check("甲方信用代码已抽取且合法",
          bool(meta.get("party_a_credit_code"))
          and validate_credit_code(meta.get("party_a_credit_code", "")),
          f"实际={meta.get('party_a_credit_code')}")
    check("乙方信用代码已抽取且合法",
          bool(meta.get("party_b_credit_code"))
          and validate_credit_code(meta.get("party_b_credit_code", "")),
          f"实际={meta.get('party_b_credit_code')}")
    check("甲乙方信用代码不重复",
          meta.get("party_a_credit_code") != meta.get("party_b_credit_code"))

    period_start = meta.get("period_start")
    period_end = meta.get("period_end")
    check("履约起始日已抽取", str(period_start) == "2026-10-01", f"实际={period_start}")
    check("履约截止日已抽取", str(period_end) == "2027-09-30", f"实际={period_end}")
    check("履约天数计算正确（365 天）", meta.get("period_days") == 365,
          f"实际={meta.get('period_days')}")
    check("签订日期已抽取", str(meta.get("sign_date")) == "2026-09-18",
          f"实际={meta.get('sign_date')}")

    check("抽取成功率 > 0.8", (meta.get("extraction_success_rate") or 0) > 0.8,
          f"实际={meta.get('extraction_success_rate')}")

    return meta


# ======================================================================
# 验证三：DOCX 解析
# ======================================================================

async def test_docx():
    print("\n[验证三] DOCX 解析（段落 + 表格 + 文档流顺序）")
    parse_service.clear_parse_cache()

    # 样本设计说明：
    #   · 含两级标题（Heading 1 / Heading 2），验证标题层级识别
    #   · 含一个表格，且表格位于段落之间，验证"按文档流顺序"抽取
    #     （python-docx 的 paragraphs 与 tables 是分开的两个集合，
    #      若按集合分别遍历会丢失相对顺序，表格会被挤到末尾）
    docx_bytes = build_docx(
        paragraphs=[
            ("技术服务协议", "Heading 1"),
            ("第一条 服务内容", "Heading 2"),
            ("乙方为甲方提供技术服务。", None),
            ("第二条 服务费用", "Heading 2"),
            ("合同总金额为人民币壹拾万元整（￥100,000.00元）。", None),
        ],
        tables=[[["项目", "数量"], ["服务器", "2"]]],
    )

    tmp_path = os.path.join(BACKEND_DIR, "storage", "tmp", "verify_contract.docx")
    os.makedirs(os.path.dirname(tmp_path), exist_ok=True)
    with open(tmp_path, "wb") as f:
        f.write(docx_bytes)

    result = await parse_service.parse_local_file(tmp_path, source_type="digital")

    check("DOCX 解析引擎正确", result.doc_metadata.parse_engine == "docx_text",
          f"实际={result.doc_metadata.parse_engine}")
    check("全文非空", len(result.full_text) > 50, f"长度={len(result.full_text)}")
    check("标题已抽取", "技术服务协议" in result.full_text)
    check("表格内容进入正文（以 | 分隔）", "项目 | 数量" in result.full_text,
          f"片段={result.full_text[:200]!r}")
    check("金额已抽取", result.metadata.get("amount") == 100000.0,
          f"实际={result.metadata.get('amount')}")

    # 全部块偏移精确
    all_exact = all(
        result.full_text[b.start_idx:b.end_idx] == b.text for b in result.text_blocks
    )
    check("DOCX 全部文本块偏移精确", all_exact)

    # 逻辑分页说明必须存在
    has_note = bool(
        result.raw_result and result.raw_result.extras.get("pagination_note")
    )
    check("存在逻辑分页说明（不伪造页码）", has_note,
          f"extras={result.raw_result.extras if result.raw_result else None}")

    return result, tmp_path


# ======================================================================
# 验证四：幂等校验
# ======================================================================

async def test_idempotent(tmp_path):
    print("\n[验证四] 幂等校验（同一文件重复解析命中缓存）")
    parse_service.clear_parse_cache()

    first = await parse_service.parse_local_file(tmp_path, source_type="digital")
    check("首次解析未命中缓存", first.cache_hit is False)
    check("首次解析后缓存有 1 条", parse_service.get_parse_cache_size() == 1,
          f"实际={parse_service.get_parse_cache_size()}")

    second = await parse_service.parse_local_file(tmp_path, source_type="digital")
    check("二次解析命中缓存", second.cache_hit is True)
    check("命中结果全文一致", second.full_text == first.full_text)
    check("命中结果页数一致", second.doc_metadata.page_count == first.doc_metadata.page_count)
    check("命中结果块数一致", len(second.text_blocks) == len(first.text_blocks))
    check("命中结果指纹一致",
          second.doc_metadata.file_hash == first.doc_metadata.file_hash)
    check("命中耗时更短", second.elapsed_ms < first.elapsed_ms,
          f"首次={first.elapsed_ms:.1f}ms 二次={second.elapsed_ms:.1f}ms")
    check("命中结果偏移仍精确",
          all(second.full_text[b.start_idx:b.end_idx] == b.text
              for b in second.text_blocks))

    # 清空缓存后应重新解析
    cleared = parse_service.clear_parse_cache()
    check("清空缓存返回正确条目数", cleared == 1, f"实际={cleared}")
    third = await parse_service.parse_local_file(tmp_path, source_type="digital")
    check("清空后重新解析未命中", third.cache_hit is False)

    return first


# ======================================================================
# 验证五：三类法定阻断
# ======================================================================

async def test_blocked():
    print("\n[验证五] 三类法定阻断")
    tmp_dir = os.path.join(BACKEND_DIR, "storage", "tmp")
    os.makedirs(tmp_dir, exist_ok=True)

    # --- 5.1 加密文档 ---
    parse_service.clear_parse_cache()
    enc_path = os.path.join(tmp_dir, "verify_encrypted.pdf")
    with open(enc_path, "wb") as f:
        f.write(build_encrypted_pdf())

    blocked_reason = None
    blocked_detail = None
    try:
        await parse_service.parse_local_file(enc_path)
    except parse_service.ParseBlockedSignal as exc:
        blocked_reason = exc.reason
        blocked_detail = exc.detail

    check("加密 PDF 抛出 ParseBlockedSignal", blocked_reason is not None)
    check("阻断原因为 DOC_ENCRYPTED", blocked_reason == "DOC_ENCRYPTED",
          f"实际={blocked_reason}")
    check("阻断详情携带文件名", bool(blocked_detail and blocked_detail.get("file_name")))

    # --- 5.2 正文为空 ---
    parse_service.clear_parse_cache()
    empty_path = os.path.join(tmp_dir, "verify_empty.pdf")
    with open(empty_path, "wb") as f:
        f.write(build_empty_pdf())

    blocked_reason = None
    blocked_detail = None
    try:
        await parse_service.parse_local_file(empty_path)
    except parse_service.ParseBlockedSignal as exc:
        blocked_reason = exc.reason
        blocked_detail = exc.detail

    check("空正文 PDF 抛出 ParseBlockedSignal", blocked_reason is not None)
    check("阻断原因为 EMPTY_CONTENT", blocked_reason == "EMPTY_CONTENT",
          f"实际={blocked_reason}")
    check("阻断详情携带有效字符数",
          bool(blocked_detail and "effective_length" in blocked_detail))

    # --- 5.3 扫描件模糊（mock OCR 返回低置信度）---
    parse_service.clear_parse_cache()
    png_path = os.path.join(tmp_dir, "verify_blurry.png")
    with open(png_path, "wb") as f:
        f.write(_build_png())

    from app.core.config import get_settings
    settings = get_settings()
    original_engine = settings.OCR_ENGINE
    original_threshold = settings.OCR_MIN_CONFIDENCE_THRESHOLD

    try:
        # 切换到 mock 引擎，并抬高阈值使 mock 的 0.99 也被判为模糊
        settings.OCR_ENGINE = "mock"
        settings.OCR_MIN_CONFIDENCE_THRESHOLD = 0.995

        blocked_reason = None
        blocked_detail = None
        try:
            await parse_service.parse_local_file(png_path, source_type="scanned")
        except parse_service.ParseBlockedSignal as exc:
            blocked_reason = exc.reason
            blocked_detail = exc.detail

        check("低置信度扫描件抛出 ParseBlockedSignal", blocked_reason is not None,
              f"reason={blocked_reason}")
        check("阻断原因为 SCAN_TOO_BLURRY", blocked_reason == "SCAN_TOO_BLURRY",
              f"实际={blocked_reason}")
        check("阻断详情携带置信度与阈值",
              bool(blocked_detail and "avg_ocr_confidence" in blocked_detail
                   and "threshold" in blocked_detail))

        # --- 5.4 正常置信度且内容充足的扫描件应通过 ---
        settings.OCR_MIN_CONFIDENCE_THRESHOLD = 0.6
        # mock 引擎默认文本仅 13 字符，会被正文长度门禁判为 EMPTY_CONTENT；
        # 此处注入足够长的文本以模拟"扫描件识别成功"的正向场景
        os.environ["MOCK_OCR_TEXT"] = (
            "设备采购合同 合同编号：HT-2026-0918-001 "
            "甲方：北京华信智能科技有限公司 "
            "乙方：上海鼎盛机械制造有限公司 "
            "本合同总金额为人民币叁佰万元整。"
        )
        os.environ["MOCK_OCR_CONFIDENCE"] = "0.92"

        parse_service.clear_parse_cache()
        try:
            ok_result = await parse_service.parse_local_file(png_path, source_type="scanned")

            check("正常扫描件不阻断", ok_result is not None)
            check("扫描件来源类型为 scanned",
                  ok_result.doc_metadata.source_type == "scanned",
                  f"实际={ok_result.doc_metadata.source_type}")
            check("扫描件引擎标识为 ocr_mock",
                  ok_result.doc_metadata.parse_engine == "ocr_mock",
                  f"实际={ok_result.doc_metadata.parse_engine}")
            check("扫描件文本块带 OCR 置信度",
                  all(b.ocr_confidence is not None for b in ok_result.text_blocks),
                  f"块数={len(ok_result.text_blocks)}")
            check("扫描件 OCR 置信度已记录",
                  ok_result.doc_metadata.avg_ocr_confidence == 0.92,
                  f"实际={ok_result.doc_metadata.avg_ocr_confidence}")
            check("扫描件文本块偏移精确",
                  all(ok_result.full_text[b.start_idx:b.end_idx] == b.text
                      for b in ok_result.text_blocks))
            check("扫描件正文长度达标",
                  len(ok_result.full_text) >= 50,
                  f"长度={len(ok_result.full_text)}")

        finally:
            os.environ.pop("MOCK_OCR_TEXT", None)
            os.environ.pop("MOCK_OCR_CONFIDENCE", None)

    finally:
        settings.OCR_ENGINE = original_engine
        settings.OCR_MIN_CONFIDENCE_THRESHOLD = original_threshold


def _build_png(width: int = 400, height: int = 200) -> bytes:
    """
    生成一个最小合法 PNG（纯白底，用于测试图像解析链路）

    参数：
        width: 宽
        height: 高
    返回：
        PNG 文件字节
    """
    import struct

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    raw = b""
    for _ in range(height):
        raw += b"\x00" + b"\xff" * (width * 3)   # 每行前置 filter 字节 0

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    idat = zlib.compress(raw)

    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


# ======================================================================
# 验证六：节点适配入口的异常语义
# ======================================================================

async def test_node_adapter():
    print("\n[验证六] 节点适配入口 load_contract_text_for_node 异常语义")

    # --- 6.1 阻断信号必须原样上抛（不能被转成 IOError）---
    tmp_dir = os.path.join(BACKEND_DIR, "storage", "tmp")
    enc_path = os.path.join(tmp_dir, "verify_encrypted.pdf")
    with open(enc_path, "rb") as f:
        enc_bytes = f.read()

    parse_service.clear_parse_cache()

    # 直接调用底层入口（传入 file_bytes）验证异常类型
    raised_type = None
    try:
        await parse_service.parse_contract_document(
            object_key="local/enc.pdf",
            task_id="t1",
            file_name="enc.pdf",
            file_bytes=enc_bytes,
        )
    except parse_service.ParseBlockedSignal:
        raised_type = "ParseBlockedSignal"
    except IOError:
        raised_type = "IOError"
    except Exception as exc:  # noqa: BLE001
        raised_type = type(exc).__name__

    check("parse_contract_document 对加密文件抛 ParseBlockedSignal",
          raised_type == "ParseBlockedSignal", f"实际={raised_type}")

    # --- 6.2 格式校验失败应为 ValueError（节点侧转为 IOError 重试）---
    parse_service.clear_parse_cache()
    bad_path = os.path.join(tmp_dir, "verify_fake.docx")
    with open(bad_path, "wb") as f:
        f.write(b"%PDF-1.7\nfake pdf bytes for extension mismatch test")

    raised_type = None
    try:
        await parse_service.parse_local_file(bad_path)
    except parse_service.ParseBlockedSignal:
        raised_type = "ParseBlockedSignal"
    except ValueError:
        raised_type = "ValueError"
    except IOError:
        raised_type = "IOError"
    except Exception as exc:  # noqa: BLE001
        raised_type = type(exc).__name__

    check("扩展名与魔数不符时抛 ValueError", raised_type == "ValueError",
          f"实际={raised_type}")

    # --- 6.3 不支持格式应为 ValueError ---
    parse_service.clear_parse_cache()
    txt_path = os.path.join(tmp_dir, "verify_contract.txt")
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write("这是一份纯文本文件，不应被解析服务接受。" * 5)

    raised_type = None
    try:
        await parse_service.parse_local_file(txt_path)
    except ValueError:
        raised_type = "ValueError"
    except parse_service.ParseBlockedSignal:
        raised_type = "ParseBlockedSignal"
    except Exception as exc:  # noqa: BLE001
        raised_type = type(exc).__name__

    check("不支持的格式抛 ValueError", raised_type == "ValueError",
          f"实际={raised_type}")


# ======================================================================
# 主流程
# ======================================================================

async def main():
    print("=" * 72)
    print("parse_service 端到端验证")
    print("=" * 72)

    result, pdf_path = await test_pdf_offsets()
    await test_metadata(result)
    docx_result, docx_path = await test_docx()
    await test_idempotent(pdf_path)
    await test_blocked()
    await test_node_adapter()

    print("\n" + "=" * 72)
    print(f"结果：通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    if FAILED:
        print("-" * 72)
        for name, detail in FAILED:
            print(f"  [FAIL] {name} | {detail}")
    print("=" * 72)

    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
