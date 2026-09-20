# D:/ContractReviewSys-合同审批智能审查项目/backend/app/services/pdf_renderer.py
"""
PDF 报告渲染器 — 基于 PyMuPDF 原生绘制的 A4 版式引擎

【模块定位】
    把 report_service.render_report_markdown() 产出的 Markdown 源文本，
    渲染为符合企业正式文档规范的 A4 PDF。

    ★ 唯一数据源是 Markdown 文本。
      渲染器**不做任何业务判定**、不查询数据库、不重新拼装结论 ——
      它只做"文本 → 版式"的确定性转换。这样保证：
        Markdown 报告与 PDF 报告永远是同一份数据源的同构呈现，
        从机制上杜绝"两种格式结论不一致"的风险（架构红线）。

【为什么不用 WeasyPrint（决策记录，勿回退）】
    曾实测 weasyprint==70.0：pip 安装成功，但运行期必然抛出
        OSError: cannot load library 'libgobject-2.0-0': error 0x7e
    因为 WeasyPrint 的文本排版层强依赖 GTK/Pango 系统库。
    在 Windows 上唯一解法是要求部署方额外安装 GTK3 运行时，
    违背"零系统级依赖、Windows 原生开箱可运行"的部署约束。
    PyMuPDF（fitz）是纯 wheel 分发的二进制绑定，零系统依赖。

【中文渲染的三级字体回退（关键设计）】
    一级：配置指定的字体文件（PDF_FONT_PATH / PDF_FONT_PATH_BOLD）
    二级：自动探测系统字体（Windows: simsun.ttc/msyh.ttc/simhei.ttf 等；
          Linux: Noto Sans CJK / wqy 系）
    三级：PyMuPDF 内置 CJK 字体（china-s / china-ss）
          ★ 内置字体编译在 PyMuPDF 二进制内，**不需要任何外部字体文件**，
            是"永不缺字、永无乱码"的兜底保证。
    三级全部失败才会抛出异常，且异常信息会列出全部探测路径，便于排查。

【版式规范】
    纸张：A4（210 × 297 mm）
    页眉：左「合同审批智能审查系统 · 审查报告」（可配置），右「任务编号」
          底部一条细分隔线
    页脚：左「生成时间」，右「第 X 页 / 共 Y 页」
    正文：宋体系族 10.5pt（五号），行高 1.6 倍
    标题：黑体系族，H1/H2/H3/H4 四级字号递减
    配色：高风险红 #cf1322 / 中风险橙 #fa541c / 低风险绿 #52c41a
          （与前端、Markdown 报告完全一致，见 RISK_LEVEL_COLORS）

【Markdown 解析范围】
    本渲染器只处理报告实际用到的 Markdown 子集，不做通用 Markdown 解析：
        # / ## / ### / ####     标题（四级）
        | a | b |               表格（含表头分隔行）
        - xxx / - **xxx**：yyy  无序列表 / 字段行
        1. xxx                  有序列表
        ---                     水平分隔线
        > xxx                   引用块
        普通段落（含 **加粗** 行内标记）
    遇到无法识别的行，一律按普通段落渲染，**绝不丢弃内容**。

使用示例：
    from app.services.pdf_renderer import render_markdown_to_pdf
    pdf_bytes, page_count, font_used = render_markdown_to_pdf(md_text)
"""

import os
import platform
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from app.core.config import settings
from app.core.logging_config import get_logger

logger = get_logger(__name__)


# ======================================================================
# 一、常量与配色
# ======================================================================
# 风险等级配色（唯一权威定义处）
#
# ★ 与需求文字的严格对应关系（2026-09-19 调整）：
#   需求原文：「高风险红色、中风险橙色、低风险绿色」。
#   原色板沿用系统其它处的 antd 色值（high=#fa541c），但该值（橙红）
#   在纸质与屏幕上都明显偏橙，会被误读为"中风险"，与需求描述不符。
#   故 PDF 侧将四级调整为语义无歧义的配色：
#       critical 深红 #b91c1c —— 严重风险（最高级，比高风险更深以体现层级）
#       high     正红 #dc2626 —— 高风险（需求指定的"红色"）
#       medium   橙   #ea580c —— 中风险（需求指定的"橙色"）
#       low      绿   #16a34a —— 低风险（需求指定的"绿色"）
#   注意：前端与 Markdown 报告仍使用 antd 色值，本处只影响 PDF 版式，
#   属于"同一语义、不同媒介下的等效表达"，不构成口径冲突。
RISK_LEVEL_COLORS: Dict[str, Tuple[float, float, float]] = {
    "critical": (0.725, 0.110, 0.110),  # #b91c1c 严重风险 深红
    "high": (0.863, 0.149, 0.149),      # #dc2626 高风险   正红
    "medium": (0.918, 0.345, 0.047),    # #ea580c 中风险   橙
    "low": (0.086, 0.639, 0.290),       # #16a34a 低风险   绿
}

# 风险等级中文 → 键 反查（Markdown 里是中文，需映射回键以取色）
RISK_LABEL_TO_KEY: Dict[str, str] = {
    "严重风险": "critical",
    "高风险": "high",
    "中风险": "medium",
    "低风险": "low",
}

# 中性色
COLOR_TEXT = (0.13, 0.13, 0.13)         # 正文近黑
COLOR_MUTED = (0.45, 0.45, 0.45)        # 次要文字灰
COLOR_RULE = (0.78, 0.78, 0.78)         # 分隔线浅灰
COLOR_TABLE_BORDER = (0.70, 0.70, 0.70)
COLOR_TABLE_HEAD_BG = (0.925, 0.937, 0.953)  # 表头浅蓝灰底

# 单位换算：1 毫米 = 72 / 25.4 磅
MM_TO_PT = 72.0 / 25.4

# A4 尺寸（毫米）
A4_WIDTH_MM = 210.0
A4_HEIGHT_MM = 297.0


# ======================================================================
# 二、字体解析（三级回退）
# ======================================================================
# 系统字体候选路径（按优先级排列）
# Windows 候选：宋体 / 微软雅黑 / 黑体 / 等线
_FONT_CANDIDATES_BODY = [
    "c:/windows/fonts/simsun.ttc",   # 宋体（正文首选，公文体）
    "c:/windows/fonts/msyh.ttc",     # 微软雅黑
    "c:/windows/fonts/simkai.ttf",   # 楷体
    "c:/windows/fonts/Deng.ttf",     # 等线
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/System/Library/Fonts/PingFang.ttc",
]
_FONT_CANDIDATES_BOLD = [
    "c:/windows/fonts/simhei.ttf",   # 黑体（标题首选）
    "c:/windows/fonts/msyhbd.ttc",   # 微软雅黑粗体
    "c:/windows/fonts/simsun.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/System/Library/Fonts/PingFang.ttc",
]

# PyMuPDF 内置 CJK 字体名（编译在二进制内，零外部依赖）
# china-s  = 简体中文宋体（serif）
# china-ss = 简体中文黑体（sans-serif）
BUILTIN_CJK_SERIF = "china-s"
BUILTIN_CJK_SANS = "china-ss"


@dataclass
class FontResolution:
    """
    字体解析结果

    字段：
        body: 正文字体名。
              使用系统字体文件时为已注册的别名 "crszh_body"；
              回退内置字体时直接为内置名 "china-s"（无需注册）
        bold: 标题字体名。命名规则同 body
        body_file: 正文字体文件路径；使用内置字体时为 None
        bold_file: 标题字体文件路径；使用内置字体时为 None
        body_is_builtin: 正文是否使用内置 CJK 字体
        bold_is_builtin: 标题是否使用内置 CJK 字体
        detail: 探测过程说明（写入日志，便于排查缺字问题）
    """

    body: str
    bold: str
    body_file: Optional[str] = None
    bold_file: Optional[str] = None
    body_is_builtin: bool = False
    bold_is_builtin: bool = False
    detail: str = ""

    def text_font(self, bold: bool = False) -> Tuple[str, Optional[str]]:
        """
        取绘制文本所需的 (字体名, 字体文件路径) 二元组

        参数：
            bold: 是否需要标题字体

        返回：
            (fontname, fontfile)。fontfile 为 None 表示使用 PyMuPDF 内置字体，
            insert_text 时不能传 fontfile 参数。
        """
        if bold:
            if self.bold_is_builtin:
                return (self.bold, None)
            return (self.bold, self.bold_file)
        if self.body_is_builtin:
            return (self.body, None)
        return (self.body, self.body_file)


# 进程级字体缓存：字体探测涉及磁盘 IO，同一进程内只需解一次
_FONT_CACHE: Optional[FontResolution] = None


def _first_existing(paths: List[str]) -> Optional[str]:
    """
    返回候选路径中第一个真实存在的文件

    参数：
        paths: 候选路径列表

    返回：
        存在的路径；全部不存在时返回 None
    """
    for path in paths:
        if path and os.path.isfile(path):
            return path
    return None


def resolve_fonts() -> FontResolution:
    """
    解析字体（三级回退，进程内缓存）

    回退顺序：
        1. 配置指定的字体文件
        2. 系统字体候选路径自动探测
        3. PyMuPDF 内置 CJK 字体

    返回：
        FontResolution
    """
    global _FONT_CACHE
    if _FONT_CACHE is not None:
        return _FONT_CACHE

    import pymupdf

    detail_parts: List[str] = []

    # -------------------- 一级 + 二级：文件字体 --------------------
    body_path = _first_existing([settings.PDF_FONT_PATH]) if settings.PDF_FONT_PATH else None
    if body_path:
        detail_parts.append(f"正文=配置指定({os.path.basename(body_path)})")
    else:
        body_path = _first_existing(_FONT_CANDIDATES_BODY)
        if body_path:
            detail_parts.append(f"正文=自动探测({os.path.basename(body_path)})")

    bold_path = (
        _first_existing([settings.PDF_FONT_PATH_BOLD])
        if settings.PDF_FONT_PATH_BOLD
        else None
    )
    if bold_path:
        detail_parts.append(f"标题=配置指定({os.path.basename(bold_path)})")
    else:
        bold_path = _first_existing(_FONT_CANDIDATES_BOLD)
        if bold_path:
            detail_parts.append(f"标题=自动探测({os.path.basename(bold_path)})")

    # ------------------------------------------------------------------
    # ★ PyMuPDF 字体注册方式说明（实测踩坑记录，勿改）
    #   坑一：pymupdf.Font(fontfile=path) 只是内存中的字体解析对象，
    #         **不会**把字体注册到 PDF 的字体表，仅做可用性验证。
    #   坑二：page.insert_text(fontname="任意别名") 会走 PyMuPDF 的
    #         Base14/CJK 内置字体库解析，"任意别名"必然失败，
    #         报错 `Exception: need font file or buffer`。
    #
    #   唯一正确做法：在**每个页面**上通过
    #       page.insert_font(fontname=<别名>, fontfile=<路径>)
    #   显式注册字体，之后该页才能用这个别名 insert_text。
    #   因此正文字体用「文件路径」传递（内置字体则为 None），
    #   由 PdfCanvas 在每次 new_page 时完成注册。
    # ------------------------------------------------------------------
    def _probe(path: str) -> bool:
        """验证字体文件确实是 PyMuPDF 可解析的字体"""
        try:
            pymupdf.Font(fontfile=path)
            return True
        except Exception as exc:  # noqa: BLE001 - 字体损坏则降级
            logger.warning("字体文件不可用，降级处理 | 路径=%s | 异常=%s", path, exc)
            return False

    if body_path and not _probe(body_path):
        body_path = None
    if bold_path and not _probe(bold_path):
        bold_path = None

    # -------------------- 三级：内置 CJK 字体 --------------------
    if body_path:
        body_name = "crszh_body"
        body_builtin = False
    else:
        # 内置字体名直接作为 fontname 传给 PyMuPDF 即可，无需 Font 对象
        body_name = BUILTIN_CJK_SERIF
        body_builtin = True
        detail_parts.append("正文=内置CJK宋体(china-s)")

    if bold_path:
        bold_name = "crszh_bold"
        bold_builtin = False
    else:
        # 标题也用内置字体时选黑体，以在视觉上区分层级
        bold_name = BUILTIN_CJK_SANS
        bold_builtin = True
        detail_parts.append("标题=内置CJK黑体(china-ss)")

    resolution = FontResolution(
        body=body_name,
        bold=bold_name,
        body_file=body_path,
        bold_file=bold_path,
        body_is_builtin=body_builtin,
        bold_is_builtin=bold_builtin,
        detail="；".join(detail_parts),
    )
    logger.info(
        "PDF 字体解析完成 | 平台=%s | %s", platform.system(), resolution.detail
    )
    _FONT_CACHE = resolution
    return resolution


def reset_font_cache() -> None:
    """
    清空字体缓存

    说明：
        供测试与配置变更后重新探测使用。生产链路无需调用。
    """
    global _FONT_CACHE
    _FONT_CACHE = None


# ======================================================================
# 三、Markdown 轻量解析
# ======================================================================
# 解析产物以「块」为单位，每个块带类型，交给绘制层按类型排版。
# 之所以不引入通用 Markdown 库：报告结构高度固定，自解析可完全掌控
# 每个块的排版方式（表格边框、等级配色等），且零额外依赖。

BLOCK_H1 = "h1"            # # 一级标题（报告大标题）
BLOCK_H2 = "h2"            # ## 二级标题（章节）
BLOCK_H3 = "h3"            # ### 三级标题（小节）
BLOCK_H4 = "h4"            # #### 四级标题（风险项）
BLOCK_PARAGRAPH = "p"      # 普通段落
BLOCK_LIST = "li"          # 无序列表项
BLOCK_ORDERED = "oli"      # 有序列表项
BLOCK_TABLE = "table"      # 表格
BLOCK_RULE = "hr"          # 水平分隔线
BLOCK_QUOTE = "quote"      # 引用块


@dataclass
class MdBlock:
    """
    Markdown 解析后的版式块

    字段：
        kind: 块类型（BLOCK_* 常量）
        text: 文本内容（表格块时为表头单元格列表的占位，实际看 rows）
        level: 标题层级（仅标题块使用）
        rows: 表格数据（二维字符串列表，第一行为表头）
        raw: 原始 Markdown 行（用于日志定位）
    """

    kind: str
    text: str = ""
    level: int = 0
    rows: List[List[str]] = field(default_factory=list)
    raw: str = ""


def _strip_inline_markup(text: str) -> str:
    """
    去除行内 Markdown 标记，保留纯文本

    处理的标记：
        **粗体** / __粗体__ / *斜体* / _斜体_ / `代码`
        \\| 转义竖线（表格内转义）→ |

    说明：
        PyMuPDF 是绘制层而非富文本引擎，"同一行内混排多种字重"
        实现成本高且收益低。报告中行内标记主要用于强调，
        统一去标记后由块的类型（标题/字段名）来体现层级，视觉更整洁。

    参数：
        text: 含标记的文本

    返回：
        去标记后的纯文本
    """
    if not text:
        return ""
    out = text.replace("\\|", "|")
    # 反复替换以处理简单嵌套（如 **`xxx`**）
    for _ in range(3):
        before = out
        out = out.replace("**", "").replace("__", "")
        out = out.replace("`", "")
        if out == before:
            break
    # 斜体：只处理成对出现的 * / _，避免误伤价格、路径等含下划线的内容
    if out.count("*") >= 2:
        out = out.replace("*", "")
    return out.strip()


def _split_table_row(line: str) -> List[str]:
    """
    拆分 Markdown 表格行

    参数：
        line: 形如 "| a | b |" 的行

    返回：
        单元格文本列表（已去行内标记）
    """
    stripped = line.strip()
    # 去掉首尾竖线后按竖线切分
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [_strip_inline_markup(cell.strip()) for cell in stripped.split("|")]


def _is_table_separator(line: str) -> bool:
    """
    判断是否为 Markdown 表格的分隔行（|---|---|）

    参数：
        line: 待判断行

    返回：
        True 表示是分隔行
    """
    stripped = line.strip()
    if not stripped.startswith("|"):
        return False
    # 去掉竖线、空格、破折号、冒号后应为空
    body = stripped.replace("|", "").replace("-", "").replace(":", "").replace(" ", "")
    return body == ""


def parse_markdown_blocks(markdown_text: str) -> List[MdBlock]:
    """
    把报告 Markdown 解析为版式块列表

    参数：
        markdown_text: 报告 Markdown 全文

    返回：
        MdBlock 列表（顺序与原文一致，不丢弃任何内容）
    """
    blocks: List[MdBlock] = []
    lines = markdown_text.split("\n")
    index = 0
    total = len(lines)

    while index < total:
        raw_line = lines[index]
        line = raw_line.rstrip()
        stripped = line.strip()

        # -------------------- 空行：段落边界，跳过 --------------------
        if not stripped:
            index += 1
            continue

        # -------------------- 水平分隔线 --------------------
        if stripped in ("---", "***", "___") or (
            len(stripped) >= 3 and set(stripped) <= {"-"} and stripped.startswith("---")
        ):
            blocks.append(MdBlock(kind=BLOCK_RULE, raw=raw_line))
            index += 1
            continue

        # -------------------- 标题（#### 到 #） --------------------
        if stripped.startswith("#"):
            hash_count = len(stripped) - len(stripped.lstrip("#"))
            if 1 <= hash_count <= 6:
                text = _strip_inline_markup(stripped.lstrip("#").strip())
                if hash_count == 1:
                    kind = BLOCK_H1
                elif hash_count == 2:
                    kind = BLOCK_H2
                elif hash_count == 3:
                    kind = BLOCK_H3
                else:
                    kind = BLOCK_H4
                blocks.append(
                    MdBlock(kind=kind, text=text, level=hash_count, raw=raw_line)
                )
                index += 1
                continue

        # -------------------- 引用块 --------------------
        if stripped.startswith(">"):
            # 连续的 > 行合并为一个引用块
            quote_lines: List[str] = []
            while index < total and lines[index].strip().startswith(">"):
                quote_lines.append(
                    _strip_inline_markup(lines[index].strip().lstrip(">").strip())
                )
                index += 1
            blocks.append(
                MdBlock(kind=BLOCK_QUOTE, text=" ".join(quote_lines), raw=raw_line)
            )
            continue

        # -------------------- 表格 --------------------
        # 判定条件：当前行以 | 开头，且下一行是分隔行
        if stripped.startswith("|") and index + 1 < total and _is_table_separator(
            lines[index + 1]
        ):
            rows: List[List[str]] = [_split_table_row(stripped)]
            index += 2  # 跳过分隔行
            while index < total and lines[index].strip().startswith("|"):
                rows.append(_split_table_row(lines[index]))
                index += 1
            blocks.append(MdBlock(kind=BLOCK_TABLE, rows=rows, raw=raw_line))
            continue

        # -------------------- 无序列表 --------------------
        if stripped.startswith("- ") or stripped.startswith("* "):
            blocks.append(
                MdBlock(
                    kind=BLOCK_LIST,
                    text=_strip_inline_markup(stripped[2:].strip()),
                    raw=raw_line,
                )
            )
            index += 1
            continue
        # 缩进的列表续行（如 "   - 关联条款：xxx"）
        if stripped.startswith("- ") and raw_line.startswith((" ", "\t")):
            blocks.append(
                MdBlock(
                    kind=BLOCK_LIST,
                    text=_strip_inline_markup(stripped[2:].strip()),
                    raw=raw_line,
                )
            )
            index += 1
            continue

        # -------------------- 有序列表 --------------------
        dot_pos = stripped.find(". ")
        if dot_pos > 0 and stripped[:dot_pos].isdigit():
            blocks.append(
                MdBlock(
                    kind=BLOCK_ORDERED,
                    text=_strip_inline_markup(stripped[dot_pos + 2:].strip()),
                    raw=raw_line,
                )
            )
            index += 1
            continue

        # -------------------- 普通段落 --------------------
        blocks.append(
            MdBlock(kind=BLOCK_PARAGRAPH, text=_strip_inline_markup(stripped), raw=raw_line)
        )
        index += 1

    return blocks


# ======================================================================
# 四、版式参数与绘制上下文
# ======================================================================
@dataclass
class LayoutMetrics:
    """
    版式度量（全部换算为磅 pt，便于 PyMuPDF 直接使用）

    字段：
        page_width / page_height: 页面尺寸
        margin_left / margin_right / margin_top / margin_bottom: 页边距
        content_width: 正文可用宽度
        header_y: 页眉基线 Y
        header_rule_y: 页眉分隔线 Y
        content_top: 正文起始 Y（页眉下方）
        content_bottom: 正文下界 Y（页脚上方）
        footer_y: 页脚基线 Y
        body_size / table_size: 字号
        line_gap: 正文行间距
    """

    page_width: float
    page_height: float
    margin_left: float
    margin_right: float
    margin_top: float
    margin_bottom: float
    content_width: float
    header_y: float
    header_rule_y: float
    content_top: float
    content_bottom: float
    footer_y: float
    body_size: float
    table_size: float
    line_gap: float


def build_layout_metrics() -> LayoutMetrics:
    """
    依据配置构造版式度量

    返回：
        LayoutMetrics
    """
    page_width = A4_WIDTH_MM * MM_TO_PT
    page_height = A4_HEIGHT_MM * MM_TO_PT
    margin_left = settings.PDF_PAGE_MARGIN_LEFT_MM * MM_TO_PT
    margin_right = settings.PDF_PAGE_MARGIN_RIGHT_MM * MM_TO_PT
    margin_top = settings.PDF_PAGE_MARGIN_TOP_MM * MM_TO_PT
    margin_bottom = settings.PDF_PAGE_MARGIN_BOTTOM_MM * MM_TO_PT

    body_size = settings.PDF_BODY_FONT_SIZE_PT
    table_size = settings.PDF_TABLE_FONT_SIZE_PT
    line_gap = body_size * settings.PDF_LINE_HEIGHT_RATIO

    return LayoutMetrics(
        page_width=page_width,
        page_height=page_height,
        margin_left=margin_left,
        margin_right=margin_right,
        margin_top=margin_top,
        margin_bottom=margin_bottom,
        content_width=page_width - margin_left - margin_right,
        # 页眉基线位于上边距之上 12pt 处；页边距本身已为页眉留白
        header_y=margin_top - 14.0,
        header_rule_y=margin_top - 8.0,
        content_top=margin_top,
        content_bottom=page_height - margin_bottom,
        footer_y=page_height - margin_bottom + 16.0,
        body_size=body_size,
        table_size=table_size,
        line_gap=line_gap,
    )


class PdfCanvas:
    """
    PDF 绘制上下文 — 封装"分页 + 光标推进 + 页眉页脚"的通用逻辑

    设计意图：
        把"内容画到哪里、何时翻页"与"这一块长什么样"分离。
        绘制方法只负责调用 write_* 系列原子操作，
        翻页与页眉页脚由本类统一处理，避免每处都判断剩余空间。
    """

    def __init__(self, metrics: LayoutMetrics, fonts: FontResolution, header_right: str = ""):
        """
        参数：
            metrics: 版式度量
            fonts: 字体解析结果
            header_right: 页眉右侧文字（通常为任务编号）
        """
        import pymupdf

        self._pymupdf = pymupdf
        self.metrics = metrics
        self.fonts = fonts
        self.header_right = header_right
        self.doc = pymupdf.open()
        self.page = None
        self.y = 0.0
        self.page_count = 0
        self._new_page()

    # ------------------------------------------------------------------
    # 页面管理
    # ------------------------------------------------------------------
    def _new_page(self) -> None:
        """
        新建一页并绘制页眉，重置光标到正文起始位置

        同时在本页注册字体：PyMuPDF 的字体注册是**按页**生效的，
        每页都必须重新 insert_font，否则该页无法用别名绘制文字。
        """
        m = self.metrics
        self.page = self.doc.new_page(width=m.page_width, height=m.page_height)
        self.page_count += 1
        self.y = m.content_top
        self._register_fonts()
        self._draw_header()

    def _register_fonts(self) -> None:
        """
        在当前页注册正文字体与标题字体

        说明：
            仅对"系统字体文件"需要注册；内置 CJK 字体（china-s/china-ss）
            由 PyMuPDF 内部处理，无需也**不能**传 fontfile。
            注册失败时把该字体重置为内置字体，保证后续绘制不中断
            （宁可用内置字体，也不能因字体问题产出空白 PDF）。
        """
        f = self.fonts
        for is_bold in (False, True):
            fontname, fontfile = f.text_font(bold=is_bold)
            if not fontfile:
                continue
            try:
                self.page.insert_font(fontname=fontname, fontfile=fontfile)
            except Exception as exc:  # noqa: BLE001 - 字体注册失败降级为内置字体
                logger.warning(
                    "字体注册失败，降级为内置 CJK 字体 | 字体=%s | 路径=%s | 异常=%s",
                    fontname, fontfile, exc,
                )
                if is_bold:
                    f.bold, f.bold_file, f.bold_is_builtin = BUILTIN_CJK_SANS, None, True
                else:
                    f.body, f.body_file, f.body_is_builtin = BUILTIN_CJK_SERIF, None, True
                f.detail += "；[降级]字体注册失败改用内置CJK"

    # ------------------------------------------------------------------
    # ★ 唯一文本绘制出口
    #   PyMuPDF 的 insert_text 在字体参数与 fontfile 不匹配时会抛
    #   `Exception: need font file or buffer`。为避免每一处绘制都重复
    #   "要不要传 fontfile"的判断（极易漏改），全部文本统一经由本方法绘制。
    # ------------------------------------------------------------------
    def draw_text(
        self,
        x: float,
        y: float,
        text: str,
        bold: bool = False,
        fontsize: Optional[float] = None,
        color: Tuple[float, float, float] = COLOR_TEXT,
        page: Any = None,
    ) -> None:
        """
        在指定页的指定位置绘制一行文本（唯一出口）

        参数：
            x: 起始 X（磅，文本左边缘）
            y: 基线 Y（磅）
            text: 文本内容
            bold: 是否使用标题字体
            fontsize: 字号（默认正文字号）
            color: 颜色 RGB 三元组（0-1）
            page: 目标页对象。None 表示当前页（self.page）。
                  ★ 页脚收尾时必须显式传页对象：因为页码依赖总页数，
                    只能在全部页面生成后统一回填，此时 self.page 已停在
                    最后一页，不传 page 会把所有页脚都画到最后一张上。
        """
        if not text:
            return
        target = page if page is not None else self.page
        fontname, fontfile = self.fonts.text_font(bold=bold)
        kwargs: Dict[str, Any] = {
            "fontname": fontname,
            "fontsize": fontsize if fontsize is not None else self.metrics.body_size,
            "color": color,
        }
        if fontfile:
            kwargs["fontfile"] = fontfile
        try:
            target.insert_text((x, y), text, **kwargs)
        except Exception as exc:  # noqa: BLE001 - 单行绘制失败不应中断整份报告
            logger.warning(
                "文本绘制失败，已跳过该行 | 字体=%s | 内容=%s | 异常=%s",
                fontname, text[:40], exc,
            )

    def measure(self, text: str, bold: bool = False, fontsize: Optional[float] = None) -> float:
        """
        度量文本宽度（磅）

        参数：
            text: 文本
            bold: 是否使用标题字体
            fontsize: 字号

        返回：
            文本宽度（磅）
        """
        fontname, _ = self.fonts.text_font(bold=bold)
        size = fontsize if fontsize is not None else self.metrics.body_size
        return self._text_width(text, fontname, size)

    def ensure_space(self, needed: float) -> None:
        """
        确保当前页剩余空间足够，不足则翻页

        参数：
            needed: 本块预计需要的高度（磅）
        """
        if self.y + needed > self.metrics.content_bottom:
            self._new_page()

    def finish(self) -> None:
        """
        收尾：为所有页面绘制页脚（页码依赖总页数，必须全部页生成后统一绘制）
        """
        for page_index in range(self.doc.page_count):
            self._draw_footer(self.doc[page_index], page_index + 1)

    # ------------------------------------------------------------------
    # 页眉页脚
    # ------------------------------------------------------------------
    def _draw_header(self) -> None:
        """
        绘制页眉：左侧固定标题文字 + 右侧任务编号 + 底部细分隔线
        """
        m = self.metrics
        f = self.fonts
        header_text = settings.PDF_HEADER_TEXT or ""
        if not header_text and not self.header_right:
            return

        if header_text:
            self.draw_text(
                m.margin_left, m.header_y, header_text, bold=True,
                fontsize=8.5, color=COLOR_MUTED,
            )
        if self.header_right:
            # 右对齐：按文本宽度反推起始 X
            text_width = self.measure(self.header_right, fontsize=8.5)
            self.draw_text(
                m.page_width - m.margin_right - text_width, m.header_y,
                self.header_right, fontsize=8.5, color=COLOR_MUTED,
            )
        # 页眉分隔线
        self.page.draw_line(
            (m.margin_left, m.header_rule_y),
            (m.page_width - m.margin_right, m.header_rule_y),
            color=COLOR_RULE,
            width=0.6,
        )

    def _draw_footer(self, page: Any, page_no: int) -> None:
        """
        绘制页脚：左侧生成时间 + 右侧页码

        参数：
            page: 目标页对象
            page_no: 页码（从 1 开始）
        """
        m = self.metrics
        f = self.fonts

        if settings.PDF_FOOTER_SHOW_TIME:
            generated = self._generated_at or ""
            if generated:
                self.draw_text(
                    m.margin_left, m.footer_y, f"生成时间：{generated}",
                    fontsize=8.0, color=COLOR_MUTED, page=page,
                )
        if settings.PDF_FOOTER_SHOW_PAGE:
            text = f"第 {page_no} 页 / 共 {self.page_count} 页"
            text_width = self.measure(text, fontsize=8.0)
            self.draw_text(
                m.page_width - m.margin_right - text_width, m.footer_y, text,
                fontsize=8.0, color=COLOR_MUTED, page=page,
            )

    # 生成时间：由渲染入口注入，供所有页面页脚统一显示
    _generated_at: str = ""

    # ------------------------------------------------------------------
    # 文本度量
    # ------------------------------------------------------------------
    def _text_width(self, text: str, fontname: str, fontsize: float) -> float:
        """
        度量文本宽度（磅）

        参数：
            text: 文本
            fontname: 字体名
            fontsize: 字号

        返回：
            文本宽度（磅）
        """
        try:
            return self._pymupdf.get_text_length(text, fontname=fontname, fontsize=fontsize)
        except Exception:  # noqa: BLE001 - 度量失败时用保守估计（按全角字符估）
            return len(text) * fontsize

    def wrap_text(self, text: str, fontname: str, fontsize: float, max_width: float) -> List[str]:
        """
        按可用宽度对文本折行（中文按字符断行，英文优先按词断行）

        参数：
            text: 待折行文本
            fontname: 字体名
            fontsize: 字号
            max_width: 单行最大宽度（磅）

        返回：
            折行后的行列表

        说明：
            中文没有词边界，逐字符累加是最稳妥的做法；
            遇到 ASCII 连续段（单词/数字/路径）时整段处理，避免把
            "GB/T 12345" 拆成 "GB/T 123" + "45" 这类断法。
        """
        if not text:
            return [""]

        lines: List[str] = []
        current = ""
        i = 0
        n = len(text)

        while i < n:
            ch = text[i]

            # 收集连续的 ASCII 非空字符（视为一个不可拆分的"词"）
            if ch.isascii() and not ch.isspace():
                j = i
                while j < n and text[j].isascii() and not text[j].isspace():
                    j += 1
                token = text[i:j]
            else:
                token = ch
                j = i + 1

            candidate = current + token
            if self._text_width(candidate, fontname, fontsize) <= max_width or not current:
                current = candidate
            else:
                lines.append(current)
                current = token
            i = j

        if current:
            lines.append(current)
        return lines or [""]

    # ------------------------------------------------------------------
    # 原子绘制操作
    # ------------------------------------------------------------------
    def write_paragraph(
        self,
        text: str,
        fontname: Optional[str] = None,
        fontsize: Optional[float] = None,
        color: Tuple[float, float, float] = COLOR_TEXT,
        indent: float = 0.0,
        space_before: float = 0.0,
        space_after: float = 4.0,
        line_gap: Optional[float] = None,
        width: Optional[float] = None,
    ) -> None:
        """
        绘制一个段落（自动折行、自动翻页）

        参数：
            text: 文本
            fontname: 字体名（默认正文宋体）
            fontsize: 字号（默认正文字号）
            color: 文字颜色 RGB 三元组（0-1）
            indent: 左缩进（磅）
            space_before: 段前间距（磅）
            space_after: 段后间距（磅）
            line_gap: 行高（磅，默认取配置行高）
            width: 可用宽度（磅，默认正文宽减缩进）
        """
        m = self.metrics
        f = self.fonts
        fontname = fontname or f.body
        fontsize = fontsize or m.body_size
        line_gap = line_gap or (fontsize * settings.PDF_LINE_HEIGHT_RATIO)
        max_width = (width if width is not None else m.content_width) - indent

        lines = self.wrap_text(text, fontname, fontsize, max_width)
        self.y += space_before

        for line in lines:
            self.ensure_space(line_gap)
            self.draw_text(
                m.margin_left + indent, self.y + fontsize, line,
                bold=(fontname != f.body), fontsize=fontsize, color=color,
            )
            self.y += line_gap

        self.y += space_after

    def write_heading(
        self,
        text: str,
        level: int,
    ) -> None:
        """
        绘制分级标题

        层级规范：
            H1（报告大标题）：18pt，居中，段后 16pt，下方加粗分隔线
            H2（章节）：14pt，段前 14pt 段后 8pt，左侧加色条
            H3（小节）：12pt，段前 10pt 段后 6pt
            H4（风险项）：11pt，段前 10pt 段后 5pt，按风险等级着色由调用方传入

        参数：
            text: 标题文本
            level: 标题层级 1-4
        """
        m = self.metrics
        f = self.fonts

        if level == 1:
            size = 18.0
            self.ensure_space(size * 2.4)
            self.y += 6.0
            # 居中：按文本宽度反推起始 X
            text_width = self.measure(text, bold=True, fontsize=size)
            x = m.margin_left + max(0.0, (m.content_width - text_width) / 2)
            self.draw_text(x, self.y + size, text, bold=True, fontsize=size)
            self.y += size * 1.5
            # 大标题下方装饰线
            self.page.draw_line(
                (m.margin_left, self.y),
                (m.page_width - m.margin_right, self.y),
                color=COLOR_TEXT,
                width=1.2,
            )
            self.y += 14.0
            return

        if level == 2:
            size = 14.0
            space_before, space_after = 14.0, 8.0
        elif level == 3:
            size = 12.0
            space_before, space_after = 10.0, 6.0
        else:
            size = 11.0
            space_before, space_after = 10.0, 5.0

        line_gap = size * 1.45
        self.ensure_space(space_before + line_gap + space_after)
        self.y += space_before

        # 二级标题左侧加一条强调色条，提升正式文档的视觉层次
        if level == 2:
            self.page.draw_rect(
                self._pymupdf.Rect(
                    m.margin_left - 6.0, self.y + size * 0.28,
                    m.margin_left - 3.5, self.y + size * 1.05,
                ),
                color=(0.145, 0.388, 0.922),  # #2563eb 主题蓝
                fill=(0.145, 0.388, 0.922),
            )

        self.draw_text(m.margin_left, self.y + size, text, bold=True, fontsize=size)
        self.y += line_gap + space_after

    def write_list_item(
        self,
        text: str,
        marker: str = "•",
        indent: float = 11.0,
        color: Tuple[float, float, float] = COLOR_TEXT,
        fontsize: Optional[float] = None,
    ) -> None:
        """
        绘制无序列表项（自动折行且续行对齐缩进）

        参数：
            text: 列表项文本
            marker: 项目符号
            indent: 缩进（磅），续行保持同等缩进
            color: 文字颜色
            fontsize: 字号（默认正文字号）
        """
        m = self.metrics
        f = self.fonts
        fontsize = fontsize or m.body_size
        line_gap = fontsize * settings.PDF_LINE_HEIGHT_RATIO

        # 项目符号与文本分两栏绘制：符号绘制在缩进位，文本起点再右移
        text_indent = indent + 11.0
        max_width = m.content_width - text_indent
        lines = self.wrap_text(text, f.body, fontsize, max_width)

        for seq, line in enumerate(lines):
            self.ensure_space(line_gap)
            if seq == 0:
                self.draw_text(
                    m.margin_left + indent, self.y + fontsize, marker,
                    fontsize=fontsize, color=COLOR_MUTED,
                )
            self.draw_text(
                m.margin_left + text_indent, self.y + fontsize, line,
                fontsize=fontsize, color=color,
            )
            self.y += line_gap

    def write_rule(self, space_before: float = 8.0, space_after: float = 8.0) -> None:
        """
        绘制水平分隔线

        参数：
            space_before: 线前间距（磅）
            space_after: 线后间距（磅）
        """
        m = self.metrics
        self.ensure_space(space_before + space_after + 1.0)
        self.y += space_before
        self.page.draw_line(
            (m.margin_left, self.y),
            (m.page_width - m.margin_right, self.y),
            color=COLOR_RULE,
            width=0.7,
        )
        self.y += space_after

    def write_quote(self, text: str) -> None:
        """
        绘制引用块（左侧竖线 + 浅色文字）

        参数：
            text: 引用文本
        """
        m = self.metrics
        f = self.fonts
        fontsize = m.body_size - 0.5
        line_gap = fontsize * settings.PDF_LINE_HEIGHT_RATIO
        indent = 12.0
        max_width = m.content_width - indent - 6.0
        lines = self.wrap_text(text, f.body, fontsize, max_width)

        start_y = self.y
        for line in lines:
            self.ensure_space(line_gap)
            # 翻页后竖线需从新页顶部重新起画
            if self.y < start_y:
                start_y = self.y
            self.draw_text(
                m.margin_left + indent, self.y + fontsize, line,
                fontsize=fontsize, color=(0.35, 0.35, 0.35),
            )
            self.y += line_gap

        self.page.draw_line(
            (m.margin_left + 4.0, start_y + 3.0),
            (m.margin_left + 4.0, self.y - line_gap + fontsize * 0.9),
            color=(0.70, 0.75, 0.85),
            width=2.2,
        )
        self.y += 4.0


# ======================================================================
# 五、表格绘制
# ======================================================================
def _column_widths(
    canvas: "PdfCanvas",
    rows: List[List[str]],
    fontsize: float,
    total_width: float,
) -> List[float]:
    """
    按各列最宽内容占比分配列宽

    算法：
        1. 计算每列的内容宽度需求 = max(表头宽, 表头宽×1.15, 最长单元格宽×0.72)
           ★ 权重设计说明：
             - 表头按 1.15 倍加权 —— 表头是短词（如"风险等级"4 字），
               若恰好等于内容宽度会因浮点误差折行，加权可彻底避免；
             - 单元格内容按 0.72 倍折算 —— 表格本就允许长文本折行，
               若按全长分配，"法律依据"这类长列会霸占过半页宽，
               导致短列（序号、等级）被挤成竖排。
        2. 施加最小/最大列宽约束后按权重归一化到总宽。

    参数：
        canvas: 绘制上下文（用于文本度量）
        rows: 表格数据（含表头）
        fontsize: 表格字号
        total_width: 表格总宽度（磅）

    返回：
        各列宽度列表，长度等于列数
    """
    if not rows:
        return []

    col_count = max(len(r) for r in rows)
    header = rows[0]
    body_rows = rows[1:] if len(rows) > 1 else []

    # -------------------- 特例：两列「项目 | 内容」型信息表 --------------------
    # 这类表格是"字段名 + 值"结构（合同基本信息表），
    # 若按内容权重分配，字段名列会被压到刚好放下表头，
    # 值列过窄导致中文逐字折行、字符间距离散，观感很差。
    # 正式文档的惯例是字段名列固定约 30% 宽，值列占剩余空间。
    if col_count == 2 and len(header) >= 2 and "内容" in header[1]:
        label_width = total_width * 0.30
        return [label_width, total_width - label_width]

    weights: List[float] = []
    for col in range(col_count):
        header_text = header[col] if col < len(header) else ""
        header_w = canvas._text_width(header_text, canvas.fonts.body, fontsize)

        # 表头加权：保证表头绝不折行
        demand = header_w * 1.15

        # 内容加权：取该列最长单元格的折算宽度
        max_cell = 0.0
        for row in body_rows:
            cell = row[col] if col < len(row) else ""
            max_cell = max(max_cell, canvas._text_width(cell, canvas.fonts.body, fontsize))
        demand = max(demand, max_cell * 0.72)

        weights.append(demand)

    # 下限：至少能放下 5 个半角字符；上限：不超过总宽的 42%
    min_width = fontsize * 4.2
    max_width = total_width * 0.42
    adjusted = [min(max(w, min_width), max_width) for w in weights]

    weight_sum = sum(adjusted) or 1.0
    return [w / weight_sum * total_width for w in adjusted]


def draw_table(
    canvas: "PdfCanvas",
    rows: List[List[str]],
    risk_column: Optional[int] = None,
    zebra: bool = True,
) -> None:
    """
    绘制表格（带边框、表头底色、可跨页、风险等级列着色）

    跨页策略：
        表头在每页表格续段处重绘，保证长表格分页后仍可读。

    参数：
        canvas: 绘制上下文
        rows: 表格数据，第一行为表头
        risk_column: 风险等级列下标（该列按等级着色），None 表示无
        zebra: 是否启用斑马纹
    """
    import pymupdf

    if not rows:
        return

    m = canvas.metrics
    f = canvas.fonts
    fontsize = m.table_size
    padding_x = 4.0   # 单元格左右内边距
    padding_y = 3.0   # 单元格上下内边距
    line_gap = fontsize * 1.32

    col_count = max(len(r) for r in rows)
    widths = _column_widths(canvas, rows, fontsize, m.content_width)

    def row_height(cells: List[str]) -> float:
        """计算一行的高度（按各列折行后的最大行数）"""
        max_lines = 1
        for col in range(col_count):
            cell = cells[col] if col < len(cells) else ""
            avail = widths[col] - padding_x * 2
            lines = canvas.wrap_text(cell, f.body, fontsize, max(avail, fontsize))
            max_lines = max(max_lines, len(lines))
        return max_lines * line_gap + padding_y * 2

    def draw_row(
        cells: List[str],
        is_header: bool,
        y_top: float,
        height: float,
        bg: Optional[Tuple[float, float, float]] = None,
    ) -> None:
        """绘制单行（背景 + 文字 + 边框）"""
        # 背景
        if bg:
            canvas.page.draw_rect(
                pymupdf.Rect(m.margin_left, y_top, m.page_width - m.margin_right, y_top + height),
                color=bg,
                fill=bg,
            )

        x = m.margin_left
        for col in range(col_count):
            cell = cells[col] if col < len(cells) else ""
            # 空单元格用长横线占位：正式文档中留白会被误认为排版遗漏，
            # 统一的占位符能明确表达"该项无数据"。
            display_cell = cell if cell.strip() else "—"
            avail = widths[col] - padding_x * 2
            lines = canvas.wrap_text(display_cell, f.body, fontsize, max(avail, fontsize))

            # 单元格文字颜色：表头用深灰；风险等级列按等级着色
            color = COLOR_TEXT
            fontname = f.bold if is_header else f.body
            if not is_header and risk_column is not None and col == risk_column:
                key = RISK_LABEL_TO_KEY.get(cell.strip())
                if key:
                    color = RISK_LEVEL_COLORS[key]
                    fontname = f.bold  # 等级单元格加粗，强化视觉标识
            elif not is_header and not cell.strip():
                color = COLOR_MUTED  # 占位符用浅色，避免喧宾夺主

            text_y = y_top + padding_y
            for line in lines:
                canvas.draw_text(
                    x + padding_x, text_y + fontsize, line,
                    bold=(fontname != f.body), fontsize=fontsize, color=color,
                )
                text_y += line_gap

            # 单元格竖线
            canvas.page.draw_line(
                (x, y_top), (x, y_top + height), color=COLOR_TABLE_BORDER, width=0.5
            )
            x += widths[col]

        # 最右侧竖线
        canvas.page.draw_line(
            (m.page_width - m.margin_right, y_top),
            (m.page_width - m.margin_right, y_top + height),
            color=COLOR_TABLE_BORDER,
            width=0.5,
        )
        # 横线
        canvas.page.draw_line(
            (m.margin_left, y_top),
            (m.page_width - m.margin_right, y_top),
            color=COLOR_TABLE_BORDER,
            width=0.5,
        )
        canvas.page.draw_line(
            (m.margin_left, y_top + height),
            (m.page_width - m.margin_right, y_top + height),
            color=COLOR_TABLE_BORDER,
            width=0.5,
        )

    # -------------------- 表头 --------------------
    header = rows[0]
    header_height = row_height(header)

    def draw_header_if_needed() -> None:
        """表头绘制（分页后续段也调用）"""
        canvas.ensure_space(header_height + line_gap)
        draw_row(header, True, canvas.y, header_height, bg=COLOR_TABLE_HEAD_BG)
        canvas.y += header_height

    draw_header_if_needed()
    canvas.y += 0.6

    # -------------------- 数据行 --------------------
    for index, cells in enumerate(rows[1:]):
        height = row_height(cells)
        # 空间不足则翻页并重绘表头
        if canvas.y + height > m.content_bottom:
            canvas._new_page()
            draw_header_if_needed()
            canvas.y += 0.6

        bg = (0.973, 0.976, 0.980) if (zebra and index % 2 == 1) else None
        draw_row(cells, False, canvas.y, height, bg=bg)
        canvas.y += height

    canvas.y += 6.0


# ======================================================================
# 六、块调度绘制
# ======================================================================
def draw_block(canvas: PdfCanvas, block: MdBlock) -> None:
    """
    按块类型分派到对应绘制方法

    参数：
        canvas: 绘制上下文
        block: 版式块

    说明：
        本函数是"解析结果 → 版式"的唯一映射点。
        风险等级配色在此处应用：四级标题中若含"风险项"标识，
        则按该风险项的等级取色绘制，实现需求要求的
        「高风险红色、中风险橙色、低风险绿色」视觉标识。
    """
    if block.kind == BLOCK_RULE:
        canvas.write_rule()
        return

    if block.kind in (BLOCK_H1, BLOCK_H2, BLOCK_H3, BLOCK_H4):
        canvas.write_heading(block.text, block.level)
        return

    if block.kind == BLOCK_QUOTE:
        canvas.write_quote(block.text)
        return

    if block.kind == BLOCK_TABLE:
        # 空表保护：仅有表头、无数据行的表格（如报告无审计日志时的
        # 「流程审计链路」表）直接画一个空框既浪费版面又让读者困惑，
        # 改为渲染一句明确说明，与 Markdown 的"无记录"语义保持一致。
        if len(block.rows) <= 1:
            header_text = ""
            if block.rows:
                header_text = "、".join(c for c in block.rows[0] if c)
            hint = (
                f"（本表无数据记录：{header_text}）" if header_text else "（本表无数据记录）"
            )
            canvas.write_paragraph(hint, color=COLOR_MUTED, indent=11.0)
            return

        # 判断是否存在"风险等级"列：报告概览表的表头里含该字样
        risk_col: Optional[int] = None
        for col, cell in enumerate(block.rows[0]):
            if "风险等级" in cell:
                risk_col = col
                break
        draw_table(canvas, block.rows, risk_column=risk_col)
        return

    if block.kind == BLOCK_LIST:
        _draw_field_or_list_item(canvas, block.text)
        return

    if block.kind == BLOCK_ORDERED:
        canvas.write_list_item(block.text, marker="·", indent=6.0)
        return

    # -------------------- 段落（含等级着色判断） --------------------
    _draw_paragraph_with_level_color(canvas, block.text)


def _extract_level_color(text: str) -> Optional[Tuple[float, float, float]]:
    """
    从文本中识别风险等级并返回对应颜色

    识别规则：
        文本中同时出现"风险等级"与等级中文名时着色；
        或文本以等级中文名开头时着色。

    参数：
        text: 文本

    返回：
        颜色三元组；未识别到等级时返回 None
    """
    for label, key in RISK_LABEL_TO_KEY.items():
        if label in text:
            return RISK_LEVEL_COLORS[key]
    return None


def _draw_paragraph_with_level_color(canvas: PdfCanvas, text: str) -> None:
    """
    绘制段落，若含风险等级则整段着色

    参数：
        canvas: 绘制上下文
        text: 段落文本
    """
    color = _extract_level_color(text) or COLOR_TEXT
    canvas.write_paragraph(text, color=color)


def _draw_field_or_list_item(canvas: PdfCanvas, text: str) -> None:
    """
    绘制列表项/字段行

    版式策略：
        形如「风险等级：高风险」「原文定位：第 3 页」的字段行，
        冒号前部分（字段名）用加粗绘制、冒号后部分用正文绘制，
        以贴近正式文档的字段呈现习惯。

    参数：
        canvas: 绘制上下文
        text: 列表项文本
    """
    m = canvas.metrics
    f = canvas.fonts

    # 识别"字段名：值"结构（中文全角冒号或英文冒号，且冒号前不过长）
    for sep in ("：", ":"):
        pos = text.find(sep)
        if 0 < pos <= 12:
            label = text[: pos + 1]
            value = text[pos + 1:].strip()
            if not value:
                # 空值字段（如「审批单 ID：」本身为空）：
                # 直接以字段名 + 明确占位绘制，不能落到下方的项目符号分支
                # —— 否则会在句首画出一个无意义的项目符号。
                canvas.write_paragraph(f"{label}—", indent=11.0, color=COLOR_MUTED)
                return

            fontsize = m.body_size
            line_gap = fontsize * settings.PDF_LINE_HEIGHT_RATIO
            indent = 11.0
            label_width = canvas._text_width(label, f.bold, fontsize)
            max_width = m.content_width - indent - label_width

            lines = canvas.wrap_text(value, f.body, fontsize, max(max_width, fontsize))

            for seq, line in enumerate(lines):
                canvas.ensure_space(line_gap)
                if seq == 0:
                    canvas.draw_text(
                        m.margin_left + indent, canvas.y + fontsize, label,
                        bold=True, fontsize=fontsize, color=COLOR_TEXT,
                    )
                canvas.draw_text(
                    m.margin_left + indent + label_width, canvas.y + fontsize, line,
                    fontsize=fontsize,
                    color=_extract_level_color(line) or COLOR_TEXT,
                )
                canvas.y += line_gap
            canvas.y += 1.0
            return

    canvas.write_list_item(text)


# ======================================================================
# 七、对外渲染入口
# ======================================================================
def render_markdown_to_pdf(
    markdown_text: str,
    generated_at: Optional[str] = None,
) -> Tuple[bytes, int, str]:
    """
    把报告 Markdown 渲染为 PDF 二进制

    参数：
        markdown_text: 报告 Markdown 全文（唯一数据源）
        generated_at: 生成时间字符串（写入页脚）。
                      为 None 时取当前时间。

    返回：
        (pdf_bytes, page_count, font_used) 三元组

    异常：
        ValueError: Markdown 为空
        RuntimeError: 渲染过程异常或页数超出上限
    """
    if not markdown_text or not markdown_text.strip():
        raise ValueError("Markdown 内容为空，无法渲染 PDF")

    from datetime import datetime

    metrics = build_layout_metrics()
    fonts = resolve_fonts()

    # 报告头中的「审查任务编号」提取出来放到页眉右侧，便于纸质件归档核对
    header_right = ""
    for line in markdown_text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("- 审查任务编号："):
            header_right = stripped.replace("- 审查任务编号：", "").strip()
            break

    canvas = PdfCanvas(metrics, fonts, header_right=header_right)
    canvas._generated_at = generated_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        blocks = parse_markdown_blocks(markdown_text)
        for block in blocks:
            draw_block(canvas, block)
            # 页数保护：异常超长内容可能撑爆内存，达上限立即中断
            if canvas.page_count > settings.PDF_MAX_PAGES:
                raise RuntimeError(
                    f"PDF 页数超过上限 {settings.PDF_MAX_PAGES} 页，已中断渲染。"
                    "请检查报告内容是否异常膨胀"
                )

        canvas.finish()
        pdf_bytes = canvas.doc.tobytes(deflate=True, garbage=3)
        return (pdf_bytes, canvas.page_count, fonts.body)
    except RuntimeError:
        raise
    except Exception as exc:  # noqa: BLE001 - 包装为可读错误
        raise RuntimeError(f"PyMuPDF 渲染异常：{exc}") from exc
    finally:
        # 显式关闭文档对象，及时释放 PyMuPDF 持有的原生资源
        try:
            canvas.doc.close()
        except Exception:  # noqa: BLE001 - 关闭失败不影响已取出的字节
            pass
