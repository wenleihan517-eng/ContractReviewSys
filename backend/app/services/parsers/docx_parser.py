# D:/ContractReviewSys-合同审批智能审查项目/backend/app/services/parsers/docx_parser.py
"""
DOCX 解析器（python-docx）

【模块定位】
    使用 python-docx 抽取 DOCX 的段落与表格，产出 RawParseResult。
    重点在于保留 Word 文档的**样式层级**与**表格结构**，
    这两类信息是 PDF 抽取无法获得的，对条款切分有直接价值。

【为什么 DOCX 比 PDF 更有价值】
    DOCX 是结构化格式，携带 PDF 不具备的语义信息：
        1. 样式名（Heading 1/2/3、标题）→ 直接给出章条层级，无需猜测
        2. 段落编号（numPr）→ 识别自动编号的条款序号
        3. 表格单元格 → 合同中的报价明细、付款计划多为表格
    因此 DOCX 解析应尽量利用这些信息，而不是退化成"纯文本抽取"。

【关键实现决策】
    1. **按文档顺序遍历段落与表格**：
       python-docx 的 document.paragraphs 与 document.tables 是两个独立列表，
       直接分别遍历会丢失二者的相对顺序（表格在正文中的插入位置）。
       正确做法是遍历 body 的 XML 子元素，按文档流顺序处理。
    2. **表格转文本时保留结构**：
       表格转为文本块时以 " | " 分隔单元格、以换行分隔行，
       既保留可读性，又便于后续条款切分识别表格内容。
    3. **页码的说明**：
       DOCX 是流式格式，本身不含固定分页。python-docx 无法得知
       Word 渲染后的实际页码。本解析器统一将所有内容标记为第 1 页，
       并在 extras 中说明该限制 —— 不伪造页码，避免下游溯源到错误位置。
"""

import re
from typing import Any, Dict, List, Optional

from docx import Document
from docx.document import Document as DocxDocumentType
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

from app.core.logging_config import get_logger
from app.services.parsers.raw_types import RawBlock, RawPage, RawParseResult

logger = get_logger(__name__)


# ======================================================================
# 一、可调常量
# ======================================================================

# 判定为标题的样式名关键字（小写比对）
HEADING_STYLE_KEYWORDS = ("heading", "标题", "title")

# 表格单元格分隔符
CELL_SEPARATOR = " | "

# 有效文字层判定的最小字符数
MIN_CHARS_VALID = 10


# ======================================================================
# 二、辅助函数
# ======================================================================

def _iter_block_items(parent: DocxDocumentType) -> List[Any]:
    """
    按文档流顺序遍历段落与表格

    【为什么需要这个函数】
        python-docx 的 document.paragraphs 与 document.tables 分别返回
        全部段落与全部表格，但**丢失二者的相对顺序**。
        例如文档结构为"段落A → 表格1 → 段落B"，分别遍历会得到
        [段落A, 段落B] 与 [表格1]，无法还原"表格1 在 A 与 B 之间"。

        本函数直接遍历 body 的 XML 子元素，按 XML 中出现顺序产出
        Paragraph 与 Table 对象，从而还原真实文档流。

    参数：
        parent: python-docx 的 Document 对象

    返回：
        交替包含 Paragraph 与 Table 对象的有序列表

    异常：
        ValueError：parent 不是 Document 对象时抛出
    """
    if not isinstance(parent, DocxDocumentType):
        raise ValueError(
            f"_iter_block_items 只接受 Document 对象，实际：{type(parent).__name__}"
        )

    items: List[Any] = []

    # body 的每个子元素按文档流顺序排列
    body = parent.element.body

    for child in body.iterchildren():
        # w:p  → 段落
        if child.tag == qn("w:p"):
            items.append(Paragraph(child, parent))
        # w:tbl → 表格
        elif child.tag == qn("w:tbl"):
            items.append(Table(child, parent))

    return items


def _get_paragraph_style_name(paragraph: Paragraph) -> str:
    """
    安全获取段落样式名

    说明：
        python-docx 在段落无显式样式时可能返回 None 或抛出异常，
        此处统一兜底为空字符串。

    参数：
        paragraph: 段落对象

    返回：
        样式名（原样，未转小写）；无法获取时返回空字符串
    """
    try:
        style = paragraph.style
        if style is None:
            return ""
        name = style.name
        return str(name) if name else ""
    except Exception:  # noqa: BLE001 - 样式缺失属正常情况
        return ""


def _is_heading(style_name: str) -> bool:
    """
    判定样式名是否表示标题

    参数：
        style_name: 段落样式名

    返回：
        是标题返回 True
    """
    if not style_name:
        return False

    lowered = style_name.lower()
    return any(keyword in lowered for keyword in HEADING_STYLE_KEYWORDS)


def _extract_heading_level(style_name: str) -> Optional[int]:
    """
    从标题样式名中解析层级

    说明：
        Word 默认样式为 "Heading 1" / "Heading 2"；中文模板可能为
        "标题 1" / "标题 2"。也可能通过 "标题 1 Char" 之类的派生样式出现。

    参数：
        style_name: 段落样式名

    返回：
        层级数字（1-9）；无法解析时返回 None
    """
    if not style_name:
        return None

    match = re.search(r"(\d+)", style_name)
    if match:
        level = int(match.group(1))
        if 1 <= level <= 9:
            return level

    return None


def _get_numbering_info(paragraph: Paragraph) -> Dict[str, Any]:
    """
    获取段落的自动编号信息

    说明：
        Word 的自动编号（如"1.1"、"（一）"）不体现在段落文本中，
        而是通过 pPr/numPr 引用编号定义。合同条款大量使用自动编号，
        若忽略该信息，条款序号会完全丢失。

        本函数提取编号的 numId 与 ilvl（层级），具体的编号文本
        需结合 numbering.xml 解析，属于较重的处理，因此这里只记录
        结构信息供上层判断"该段落是一个编号项"。

    参数：
        paragraph: 段落对象

    返回：
        字典 {"has_numbering": bool, "num_id": Optional[str], "level": Optional[int]}
    """
    info: Dict[str, Any] = {
        "has_numbering": False,
        "num_id": None,
        "level": None,
    }

    try:
        p_pr = paragraph._p.pPr
        if p_pr is None:
            return info

        num_pr = p_pr.numPr
        if num_pr is None:
            return info

        info["has_numbering"] = True

        if num_pr.numId is not None:
            info["num_id"] = str(num_pr.numId.val)
        if num_pr.ilvl is not None:
            info["level"] = int(num_pr.ilvl.val)

    except Exception:  # noqa: BLE001 - 编号信息缺失属正常情况
        # 部分文档的 XML 结构不完整，此时按"无编号"处理
        return {"has_numbering": False, "num_id": None, "level": None}

    return info


def _table_to_text(table: Table) -> str:
    """
    将表格转换为文本表示

    说明：
        转换规则：
            · 行内单元格以 " | " 分隔
            · 行之间以换行分隔
            · 合并单元格（gridSpan）的重复内容会被 python-docx 展开，
              此处按原样保留，避免丢失列对齐关系
            · 内嵌换行替换为空格，防止破坏行结构

    参数：
        table: python-docx 的 Table 对象

    返回：
        表格的文本表示
    """
    lines: List[str] = []

    for row in table.rows:
        cells: List[str] = []
        for cell in row.cells:
            # 单元格内可能含多段落，合并为单行
            cell_text = " ".join(
                para.text.strip() for para in cell.paragraphs if para.text.strip()
            )
            # 内嵌换行与多余空白归一化
            cell_text = re.sub(r"\s+", " ", cell_text)
            cells.append(cell_text)

        # 整行全空时跳过，避免产生大量空行
        if any(cells):
            lines.append(CELL_SEPARATOR.join(cells))

    return "\n".join(lines)


def _count_merged_cells(table: Table) -> int:
    """
    统计表格中的合并单元格数量

    说明：
        用于 extras 中记录表格复杂度，供人工判断表格是否被正确解析。
        python-docx 无直接 API，通过比较 gridSpan 判断。

    参数：
        table: python-docx 的 Table 对象

    返回：
        含横向合并（gridSpan > 1）的单元格数量
    """
    merged_count = 0

    try:
        for row in table.rows:
            for cell in row.cells:
                tc_pr = cell._tc.tcPr
                if tc_pr is None:
                    continue
                grid_span = tc_pr.find(qn("w:gridSpan"))
                if grid_span is not None:
                    span_value = grid_span.get(qn("w:val"))
                    if span_value and int(span_value) > 1:
                        merged_count += 1
    except Exception:  # noqa: BLE001 - 统计失败不影响主流程
        return merged_count

    return merged_count


# ======================================================================
# 三、主解析入口
# ======================================================================

def parse_docx(
    file_bytes: bytes,
    file_name: str = "",
) -> RawParseResult:
    """
    解析 DOCX 并抽取段落、表格与样式层级

    【页码说明（重要）】
        DOCX 是流式格式，不存储固定分页。同一份文档在不同 Word 版本、
        不同纸张设置下渲染出的页码可能不同。python-docx 无法获知
        渲染后的实际页码，因此本解析器**统一将所有内容标记为第 1 页**，
        并在 extras["pagination_note"] 中显式说明该限制。

        这样处理的原因：伪造页码会让下游的"条款 → 原文位置"溯源
        指向错误位置，比"知道页码不可靠"更危险。

    【页码分块策略（可选启用）】
        为避免所有内容挤在一个"第 1 页"下导致版面分析失效，
        本解析器按累计字符数做逻辑分块（默认每 3000 字符一块），
        作为"逻辑页"标注。该逻辑页与真实页码无关，
        仅在 extras["logical_page_size"] 中说明分块依据。

    参数：
        file_bytes: DOCX 文件的字节内容
        file_name: 原始文件名，仅用于日志

    返回：
        RawParseResult，其中：
            pages           逻辑分页结果（每页含段落与表格块）
            page_count      逻辑页数
            engine          "docx_text"
            extras          含段落数、表格数、标题层级分布等

    异常：
        ValueError：文件不是合法 DOCX，或内容为空时抛出
    """
    if not file_bytes:
        raise ValueError(f"DOCX 内容为空，无法解析：file_name={file_name}")

    result = RawParseResult(engine="docx_text")

    # -------------------- 打开文档 --------------------
    try:
        import io

        document = Document(io.BytesIO(file_bytes))
    except Exception as exc:
        raise ValueError(
            f"无法打开 DOCX 文件，可能已损坏或格式不合法（"
            f"注意 .doc 旧格式需先转换）：file_name={file_name}, error={exc}"
        ) from exc

    # -------------------- 加密检测 --------------------
    # DOCX 加密后会变成 OLE 复合文档容器，python-docx 无法打开。
    # 能走到这里说明已是合法 DOCX；此处保留检测位以保持结构一致。
    result.is_encrypted = False

    # -------------------- 按文档流顺序遍历 --------------------
    try:
        items = _iter_block_items(document)
    except Exception as exc:
        raise ValueError(
            f"DOCX 文档结构异常，无法遍历内容：file_name={file_name}, error={exc}"
        ) from exc

    # 逐块收集（此时还未分页）
    collected_blocks: List[RawBlock] = []
    order = 0

    heading_levels: Dict[int, int] = {}   # 层级 → 数量
    paragraph_count = 0
    table_count = 0
    merged_cell_total = 0
    numbered_paragraph_count = 0

    for item in items:
        # -------------------- 段落 --------------------
        if isinstance(item, Paragraph):
            text = item.text
            if not text or not text.strip():
                continue

            paragraph_count += 1

            style_name = _get_paragraph_style_name(item)
            is_heading = _is_heading(style_name)
            level = _extract_heading_level(style_name) if is_heading else None

            if level is not None:
                heading_levels[level] = heading_levels.get(level, 0) + 1

            numbering = _get_numbering_info(item)
            if numbering["has_numbering"]:
                numbered_paragraph_count += 1

            collected_blocks.append(
                RawBlock(
                    page=1,                     # 占位，分页时重写
                    order_in_page=order,
                    text=text,
                    block_type="heading" if is_heading else "text",
                    bbox=None,
                    # 用 Word 层级映射为逻辑字号，便于下游统一按字号判层级
                    font_size=float(20 - level * 2) if level else None,
                    is_bold=is_heading,
                )
            )
            order += 1

        # -------------------- 表格 --------------------
        elif isinstance(item, Table):
            table_text = _table_to_text(item)
            table_count += 1
            merged_cell_total += _count_merged_cells(item)

            if not table_text.strip():
                # 空表格不计入正文，但仍统计数量
                continue

            collected_blocks.append(
                RawBlock(
                    page=1,
                    order_in_page=order,
                    text=table_text,
                    block_type="table",
                    bbox=None,
                    font_size=None,
                    is_bold=False,
                )
            )
            order += 1

    if not collected_blocks:
        result.page_count = 1
        result.has_text_layer = False
        result.warnings.append("DOCX 中未抽取到任何非空段落或表格内容")
        logger.warning("DOCX 无有效内容 | 文件=%s", file_name)
        return result

    # -------------------- 逻辑分页 --------------------
    # 按累计有效字符数切分逻辑页，避免全部内容挤在单页导致版面分析失效
    logical_page_size = 3000
    pages: List[RawPage] = []
    current_blocks: List[RawBlock] = []
    current_chars = 0
    logical_page_no = 1

    for block in collected_blocks:
        block_chars = sum(1 for ch in block.text if not ch.isspace())

        # 当前页已有内容且加入本块会超限时，先结算当前页
        if current_blocks and current_chars + block_chars > logical_page_size:
            page_text = "\n".join(item.text for item in current_blocks)
            pages.append(
                RawPage(
                    page=logical_page_no,
                    text=page_text,
                    blocks=current_blocks,
                    has_text_layer=current_chars >= MIN_CHARS_VALID,
                    width=0.0,      # DOCX 无固定尺寸，流通格式
                    height=0.0,
                )
            )
            logical_page_no += 1
            current_blocks = []
            current_chars = 0

        # 重写页码与页内序号
        block.page = logical_page_no
        block.order_in_page = len(current_blocks)
        current_blocks.append(block)
        current_chars += block_chars

    # 结算最后一页
    if current_blocks:
        page_text = "\n".join(item.text for item in current_blocks)
        pages.append(
            RawPage(
                page=logical_page_no,
                text=page_text,
                blocks=current_blocks,
                has_text_layer=current_chars >= MIN_CHARS_VALID,
                width=0.0,
                height=0.0,
            )
        )

    result.pages = pages
    result.page_count = len(pages)
    result.has_text_layer = any(page.has_text_layer for page in pages)

    # -------------------- 附加信息 --------------------
    result.extras.update(
        {
            "paragraph_count": paragraph_count,
            "table_count": table_count,
            "merged_cell_count": merged_cell_total,
            "heading_count": sum(heading_levels.values()),
            "heading_levels": {str(k): v for k, v in sorted(heading_levels.items())},
            "numbered_paragraph_count": numbered_paragraph_count,
            "block_count": len(collected_blocks),
            "total_chars": result.total_chars,
            "is_logical_pagination": True,
            "logical_page_size": logical_page_size,
            "pagination_note": (
                "DOCX 为流式格式，不存储固定分页。此处 page 字段为按累计字符数"
                f"（每 {logical_page_size} 字符）切分的逻辑页，与 Word 渲染页码无关，"
                "不可用于原文页码溯源"
            ),
        }
    )

    # DOCX 无渲染页码，显式提示上层
    result.warnings.append(
        "DOCX 无固定分页信息，页码为逻辑分页（按累计字符数切分），"
        "如需精确页码溯源请在合同中提供 PDF 版本"
    )

    if not result.has_text_layer:
        result.warnings.append("DOCX 抽取内容有效字符数不足，可能为空白模板")

    logger.info(
        "DOCX 解析完成 | 文件=%s | 逻辑页=%d | 段落=%d | 表格=%d | 标题=%d | 字符=%d",
        file_name,
        result.page_count,
        paragraph_count,
        table_count,
        sum(heading_levels.values()),
        result.total_chars,
    )

    return result
