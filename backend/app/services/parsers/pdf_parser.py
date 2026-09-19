# D:/ContractReviewSys-合同审批智能审查项目/backend/app/services/parsers/pdf_parser.py
"""
PDF 原生文本解析器（PyMuPDF / fitz）

【模块定位】
    使用 PyMuPDF 从 PDF 中抽取带完整版面信息的文本，产出 RawParseResult。
    本解析器负责"把 PDF 读准"，不做全局字符偏移计算（由 parse_service 统一处理）。

【为什么选 PyMuPDF】
    1. 抽取粒度细：可获取每个文本块的 bbox、字号、字体，支撑标题层级判定
    2. 坐标精确：直接给出 PDF 点坐标，是原文高亮的锚点来源
    3. 容错性好：对结构异常的 PDF 容忍度高，但**加密文件必须显式识别并阻断**
    4. 纯本地运行：符合"数据不出内网"要求，无外部服务依赖

【关键实现决策】
    1. **文本按块（block）抽取，而非按行**：
       PDF 中一个 block 通常对应一个段落或表格单元格，比行更接近语义单元。
       块内可能含多行，需按行拼接并保留换行，否则段落结构会丢失。
    2. **过滤页眉页脚**：合同 PDF 常有页眉页脚（页码、公司名、密级标识）。
       这些内容重复出现在每页，若进入正文会污染条款切分，并可能造成
       "同一页码被反复引用"的溯源混乱。通过块位置与跨页重复特征识别。
    3. **保留原始顺序**：严格按 PyMuPDF 给出的阅读顺序输出，
       不做自定义排序 —— 排序错误会导致条款顺序错乱。
    4. **加密检测前置**：解析前先检查 doc.needs_pass / is_encrypted，
       而不是等抽取失败 —— 部分加密 PDF 用空密码能打开，但内容不可读。
"""

import re
from typing import Any, Dict, List, Tuple

# 【导入说明】PyMuPDF 1.24 起 `fitz` 作为顶层模块名已废弃，
# 官方推荐 `import pymupdf`。此处优先使用新名称，
# 并对尚未升级的旧版本回退到 fitz，保证兼容性。
try:
    import pymupdf as fitz
except ImportError:  # pragma: no cover - 仅在极旧版本上触发
    import fitz  # type: ignore[no-redef]

from app.core.logging_config import get_logger
from app.services.parsers.raw_types import RawBlock, RawPage, RawParseResult

logger = get_logger(__name__)


# ======================================================================
# 一、可调常量
# ======================================================================

# 判定页眉页脚的位置阈值（占页面高度的比例）
# 说明：位于页面顶部 8% 或底部 8% 区域的文本块，视为页眉页脚候选
HEADER_ZONE_RATIO = 0.08
FOOTER_ZONE_RATIO = 0.08

# 跨页重复判定的最小页数比例：某文本在超过该比例页面重复出现时判为页眉页脚
REPEAT_PAGE_RATIO = 0.5

# 有效文字层判定的最小字符数：单页少于该数量视为"无有效文字层"
MIN_CHARS_PER_PAGE = 10

# 页眉页脚文本的最大长度：过长的文本更可能是正文而非页眉
MAX_HEADER_FOOTER_LENGTH = 80


# ======================================================================
# 二、辅助函数
# ======================================================================

def is_pdf_encrypted(doc: "fitz.Document") -> bool:
    """
    判定 PDF 文档是否加密

    说明：
        PyMuPDF 的判定要点：
            needs_pass     需要密码才能访问
            is_encrypted   文档结构上标记为加密
            permissions    权限位；部分文档虽可打开但禁止复制文本
        只要满足任一条件即视为加密 —— 因为"禁止复制"会导致
        文本抽取产出空内容，若不提前识别，会被误判为"正文为空"，
        给出误导性的阻断原因。

    参数：
        doc: 已打开的 PyMuPDF 文档对象

    返回：
        加密或受限返回 True
    """
    try:
        if doc.needs_pass:
            return True
        if doc.is_encrypted:
            return True

        # 权限位检查：0 表示无任何权限；特定位为 0 表示对应操作被禁止
        # PyMuPDF 常量：PDF_PERM_COPY = 16
        permissions = doc.permissions
        if isinstance(permissions, int) and permissions == 0:
            return True
        if isinstance(permissions, int) and not (permissions & fitz.PDF_PERM_COPY):
            return True

    except Exception as exc:  # noqa: BLE001 - 属性访问异常时保守判为受限
        logger.warning("PDF 加密状态判定异常，保守判为受限 | 异常=%s", exc)
        return True

    return False


def _extract_block_text(block: Dict[str, Any]) -> Tuple[str, float, bool]:
    """
    从 PyMuPDF 的块字典中提取文本、主字号与粗体标记

    说明：
        PyMuPDF 的块结构分为两类：
            type=0  文本块，含 lines → spans → text/font/size
            type=1  图像块，无文本
        本函数只处理文本块。

        字号取块内出现次数最多的 span 字号（按字符数加权），
        而非简单取第一个 —— 段落首字常因排版需要放大，
        取第一个会导致整段字号被误判。

    参数：
        block: PyMuPDF 的块字典

    返回：
        (块文本, 主字号, 是否粗体)
    """
    if block.get("type") != 0:
        return "", 0.0, False

    lines_text: List[str] = []
    size_weight: Dict[float, int] = {}
    bold_chars = 0
    total_chars = 0

    for line in block.get("lines", []):
        span_texts: List[str] = []
        for span in line.get("spans", []):
            text = span.get("text", "")
            if not text:
                continue

            span_texts.append(text)

            # 按字符数加权统计字号
            size = float(span.get("size", 0.0))
            char_count = len(text.strip())
            if char_count:
                size_weight[size] = size_weight.get(size, 0) + char_count
                total_chars += char_count

                # 粗体判定：字体名含 Bold/Heavy/Black 等标识
                font_name = str(span.get("font", "")).lower()
                if any(flag in font_name for flag in ("bold", "heavy", "black", "semibold")):
                    bold_chars += char_count

        if span_texts:
            lines_text.append("".join(span_texts))

    text = "\n".join(lines_text)

    # 主字号 = 加权字符数最多的字号
    main_size = 0.0
    if size_weight:
        main_size = max(size_weight.items(), key=lambda item: item[1])[0]

    is_bold = total_chars > 0 and (bold_chars / total_chars) > 0.5

    return text, main_size, is_bold


def _detect_header_footer_texts(
    pages_blocks: List[List[Dict[str, Any]]],
    page_heights: List[float],
) -> set:
    """
    识别跨页重复出现的页眉页脚文本

    【识别策略（双条件与判定）】
        条件一：位置在页面顶部或底部区域（按比例阈值）
        条件二：在超过半数页面上重复出现（文本归一化后比对）

        两个条件同时满足才判为页眉页脚。只用位置判定会误删位于页面
        顶部/底部的正文（如合同首页标题、落款）；只用重复判定会误删
        正文中的重复条款（如"本合同一式两份"）。

    参数：
        pages_blocks: 每页的块字典列表
        page_heights: 每页高度（PDF 点）

    返回：
        归一化后的页眉页脚文本集合
    """
    if not pages_blocks:
        return set()

    total_pages = len(pages_blocks)
    # 出现次数统计：归一化文本 → 出现页数
    text_page_count: Dict[str, int] = {}

    for page_index, blocks in enumerate(pages_blocks):
        page_height = page_heights[page_index] if page_index < len(page_heights) else 0.0
        if page_height <= 0:
            continue

        seen_in_page: set = set()

        for block in blocks:
            if block.get("type") != 0:
                continue

            bbox = block.get("bbox")
            if not bbox:
                continue

            text, _, _ = _extract_block_text(block)
            normalized = _normalize_for_repeat(text)
            if not normalized or len(normalized) > MAX_HEADER_FOOTER_LENGTH:
                continue

            y0, y1 = float(bbox[1]), float(bbox[3])

            # 条件一：位于顶部或底部区域
            in_header = y1 <= page_height * HEADER_ZONE_RATIO
            in_footer = y0 >= page_height * (1.0 - FOOTER_ZONE_RATIO)
            if not (in_header or in_footer):
                continue

            # 同页内同文本只计一次，避免同页多个相同块抬高计数
            if normalized not in seen_in_page:
                seen_in_page.add(normalized)
                text_page_count[normalized] = text_page_count.get(normalized, 0) + 1

    # 条件二：出现页数超过阈值
    threshold = max(2, int(total_pages * REPEAT_PAGE_RATIO))
    header_footer = {
        text for text, count in text_page_count.items() if count >= threshold
    }

    if header_footer:
        logger.info(
            "识别到跨页重复页眉页脚 | 数量=%d | 样例=%s",
            len(header_footer),
            list(header_footer)[:3],
        )

    return header_footer


def _normalize_for_repeat(text: str) -> str:
    """
    归一化文本用于跨页重复比对

    处理：
        1. 剔除全部空白
        2. 将连续数字统一替换为占位符（页码每页不同，需归一化）

    参数：
        text: 原始文本

    返回：
        归一化文本
    """
    if not text:
        return ""

    normalized = re.sub(r"\s+", "", text)
    normalized = re.sub(r"\d+", "#", normalized)
    return normalized


# ======================================================================
# 三、主解析入口
# ======================================================================

def parse_pdf(
    file_bytes: bytes,
    file_name: str = "",
    enable_header_footer_filter: bool = True,
) -> RawParseResult:
    """
    解析 PDF 并抽取带版面信息的文本

    参数：
        file_bytes: PDF 文件的字节内容
        file_name: 原始文件名，仅用于日志
        enable_header_footer_filter: 是否启用页眉页脚过滤。
            过滤会改变正文内容，因此在"内容疑似被过度删减"时
            可通过关闭该开关做诊断对比。

    返回：
        RawParseResult，其中：
            pages           逐页结果（含块、坐标、字号）
            page_count      总页数
            engine          "pdf_text"
            is_encrypted    是否加密（加密时 pages 为空）
            has_text_layer  是否含有效文字层

    异常：
        ValueError：文件不是合法 PDF，或文档完全无法打开时抛出
        RuntimeError：PyMuPDF 内部错误（如文件损坏）时抛出

    说明：
        本函数**不抛加密异常**，而是通过 is_encrypted 字段返回。
        是否因此阻断任务由上层（parse_service）依据业务规则决定，
        保持解析器只做"事实陈述"，不做"业务裁决"。
    """
    if not file_bytes:
        raise ValueError(f"PDF 内容为空，无法解析：file_name={file_name}")

    result = RawParseResult(engine="pdf_text")

    try:
        doc = fitz.open(stream=file_bytes, filetype="pdf")
    except Exception as exc:
        raise ValueError(
            f"无法打开 PDF 文件，可能已损坏或格式不合法：file_name={file_name}, error={exc}"
        ) from exc

    try:
        # -------------------- 加密检测前置 --------------------
        if is_pdf_encrypted(doc):
            logger.warning(
                "PDF 已加密或文本复制被禁止 | 文件=%s | needs_pass=%s | is_encrypted=%s",
                file_name,
                doc.needs_pass,
                doc.is_encrypted,
            )
            result.is_encrypted = True
            result.page_count = doc.page_count
            result.has_text_layer = False
            return result

        result.page_count = doc.page_count

        if doc.page_count == 0:
            raise ValueError(f"PDF 不含任何页面：file_name={file_name}")

        # -------------------- 第一遍：抽取全部页面的块 --------------------
        # 先全量抽取，再统一做页眉页脚识别（需要跨页统计）
        raw_pages_blocks: List[List[Dict[str, Any]]] = []
        page_heights: List[float] = []
        page_sizes: List[Tuple[float, float]] = []

        for page_index in range(doc.page_count):
            page = doc.load_page(page_index)

            # get_text("dict") 返回含 blocks → lines → spans 的嵌套结构
            page_dict = page.get_text("dict")
            blocks = page_dict.get("blocks", [])

            raw_pages_blocks.append(blocks)
            page_sizes.append((float(page.rect.width), float(page.rect.height)))
            page_heights.append(float(page.rect.height))

        # -------------------- 页眉页脚识别 --------------------
        header_footer_texts: set = set()
        if enable_header_footer_filter and doc.page_count >= 2:
            # 单页文档不存在"跨页重复"，无需过滤
            header_footer_texts = _detect_header_footer_texts(raw_pages_blocks, page_heights)

        # -------------------- 第二遍：构建结构化页面 --------------------
        for page_index, blocks in enumerate(raw_pages_blocks):
            page_no = page_index + 1
            width, height = page_sizes[page_index]

            page_blocks: List[RawBlock] = []
            order_in_page = 0

            for block in blocks:
                if block.get("type") != 0:
                    # 图像块：记录为 image 类型占位，供"该页需 OCR"诊断使用
                    bbox_raw = block.get("bbox")
                    page_blocks.append(
                        RawBlock(
                            page=page_no,
                            order_in_page=order_in_page,
                            text="",
                            block_type="image",
                            bbox=(
                                (float(bbox_raw[0]), float(bbox_raw[1]),
                                 float(bbox_raw[2]), float(bbox_raw[3]))
                                if bbox_raw else None
                            ),
                        )
                    )
                    order_in_page += 1
                    continue

                text, font_size, is_bold = _extract_block_text(block)

                if not text.strip():
                    continue

                # 页眉页脚过滤
                if header_footer_texts:
                    normalized = _normalize_for_repeat(text)
                    if normalized in header_footer_texts:
                        continue

                bbox_raw = block.get("bbox")
                page_blocks.append(
                    RawBlock(
                        page=page_no,
                        order_in_page=order_in_page,
                        text=text,
                        block_type="text",
                        bbox=(
                            (float(bbox_raw[0]), float(bbox_raw[1]),
                             float(bbox_raw[2]), float(bbox_raw[3]))
                            if bbox_raw else None
                        ),
                        font_size=font_size if font_size > 0 else None,
                        is_bold=is_bold,
                    )
                )
                order_in_page += 1

            # 页面纯文本 = 块文本按顺序以换行拼接
            page_text = "\n".join(
                block.text for block in page_blocks if block.block_type == "text" and block.text
            )

            has_text_layer = sum(1 for ch in page_text if not ch.isspace()) >= MIN_CHARS_PER_PAGE

            result.pages.append(
                RawPage(
                    page=page_no,
                    text=page_text,
                    blocks=page_blocks,
                    has_text_layer=has_text_layer,
                    width=width,
                    height=height,
                )
            )

        # -------------------- 整体文字层判定 --------------------
        # 判定标准：有文字层的页面占比超过半数
        if result.pages:
            text_layer_pages = sum(1 for page in result.pages if page.has_text_layer)
            result.has_text_layer = text_layer_pages > (len(result.pages) / 2)
        else:
            result.has_text_layer = False

        # -------------------- 附加信息 --------------------
        result.extras.update(
            {
                "block_count": len(result.all_blocks),
                "empty_page_count": result.empty_page_count,
                "total_chars": result.total_chars,
                "header_footer_filtered": sorted(header_footer_texts)[:20],
                "header_footer_filtered_count": len(header_footer_texts),
                "has_outline": bool(doc.get_toc()),
                "metadata": {
                    key: value
                    for key, value in (doc.metadata or {}).items()
                    if value
                },
            }
        )

        # 无有效文字层时给出告警，提示上层可能需要 OCR
        if not result.has_text_layer:
            result.warnings.append(
                f"PDF 未检测到有效文字层（有文字层页面 {sum(1 for p in result.pages if p.has_text_layer)}"
                f"/{len(result.pages)} 页），可能需要 OCR 处理"
            )

        logger.info(
            "PDF 解析完成 | 文件=%s | 页数=%d | 字符数=%d | 块数=%d | 文字层=%s",
            file_name,
            result.page_count,
            result.total_chars,
            len(result.all_blocks),
            result.has_text_layer,
        )

        return result

    finally:
        # 确保文档句柄释放，避免文件描述符泄漏
        doc.close()


def render_page_to_png(
    file_bytes: bytes,
    page_no: int,
    dpi: int = 200,
) -> bytes:
    """
    将指定页面渲染为 PNG 图像

    说明：
        供 OCR 降级链路使用。当 PDF 某页无文字层时，需要把该页渲染成位图
        再交给 OCR 引擎识别。

        DPI 选择说明：
            200 DPI 是 OCR 准确率与处理速度的平衡点。
            低于 150 DPI 时中文小字识别率明显下降；
            高于 300 DPI 时单页处理时间成倍增长而准确率提升有限。

    参数：
        file_bytes: PDF 文件字节内容
        page_no: 页码，从 1 开始
        dpi: 渲染分辨率，默认 200

    返回：
        PNG 图像字节流

    异常：
        ValueError：页码越界或文件无法打开时抛出
    """
    if not file_bytes:
        raise ValueError("PDF 内容为空，无法渲染页面")

    try:
        doc = fitz.open(stream=file_bytes, filetype="pdf")
    except Exception as exc:
        raise ValueError(f"无法打开 PDF 用于页面渲染：error={exc}") from exc

    try:
        if page_no < 1 or page_no > doc.page_count:
            raise ValueError(
                f"页码越界：page_no={page_no}，文档共 {doc.page_count} 页"
            )

        page = doc.load_page(page_no - 1)

        # 按 DPI 计算缩放矩阵：PDF 默认 72 DPI
        zoom = dpi / 72.0
        matrix = fitz.Matrix(zoom, zoom)

        pixmap = page.get_pixmap(matrix=matrix, alpha=False)

        return pixmap.tobytes("png")

    finally:
        doc.close()
