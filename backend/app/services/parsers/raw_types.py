# D:/ContractReviewSys-合同审批智能审查项目/backend/app/services/parsers/raw_types.py
"""
解析器原始产物类型定义

【模块定位】
    定义 PDF / DOCX / OCR 三类解析器共同的输出结构。所有解析器都产出
    RawParseResult，由 parse_service 统一做标准化处理。

【为什么原始块要保留 bbox（边界框）】
    bbox 是"结构化结果 → 原文高亮"的正向溯源锚点。前端在展示风险条款时，
    需要在原始 PDF 页面上框出对应位置，仅靠字符偏移无法在扫描件上定位。
    因此即使当前业务还没有用到，也必须从解析阶段就记录下来 ——
    后期补录 bbox 需要重新解析全部历史文件，代价极高。

【坐标系约定】
    PDF 与图片的 bbox 统一为 (x0, y0, x1, y1)，原点在页面左上角，
    单位为 PDF 点（1 pt = 1/72 英寸）。PyMuPDF 原生使用该坐标系，
    图像 OCR 结果需按其 DPI 比例换算后再填入。
"""

from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field


class RawBlock(BaseModel):
    """
    原始文本块 — 解析器直接产出的最小文本单元

    说明：
        与最终对外的 TextBlock 区别在于：RawBlock 只记录"本块在页内"的信息
        （页内顺序、页内坐标），不计算全局字符偏移。全局偏移由
        parse_service 在拼接全文时统一计算，保证唯一计算来源。

    字段：
        page: 所在页码，从 1 开始
        order_in_page: 页内顺序号，从 0 开始
        text: 文本内容
        block_type: 块类型 text / table / image_caption / header / footer
        bbox: 边界框 (x0, y0, x1, y1)，单位为 PDF 点；无坐标为 None
        font_size: 主字号（PDF 有值），用于标题层级判定
        is_bold: 是否粗体（PDF 有值）
        confidence: OCR 置信度（0-1），仅 OCR 产出的块有值
    """

    page: int = Field(ge=1, description="页码，从 1 开始")
    order_in_page: int = Field(default=0, ge=0, description="页内顺序号")
    text: str = Field(default="", description="文本内容")
    block_type: str = Field(default="text", description="块类型")
    bbox: Optional[Tuple[float, float, float, float]] = Field(
        default=None, description="边界框 (x0, y0, x1, y1)，单位 PDF 点"
    )
    font_size: Optional[float] = Field(default=None, description="主字号")
    is_bold: bool = Field(default=False, description="是否粗体")
    confidence: Optional[float] = Field(
        default=None, ge=0.0, le=1.0, description="OCR 置信度"
    )


class RawPage(BaseModel):
    """
    原始页面 — 单页的文本与块集合

    字段：
        page: 页码，从 1 开始
        text: 本页纯文本（块按顺序拼接，块间以换行分隔）
        blocks: 本页的原始文本块列表
        has_text_layer: 本页是否含可用文字层
        avg_confidence: 本页平均 OCR 置信度，仅 OCR 有值
        width / height: 页面尺寸（PDF 点），用于坐标换算与版面分析
    """

    page: int = Field(ge=1, description="页码，从 1 开始")
    text: str = Field(default="", description="本页纯文本")
    blocks: List[RawBlock] = Field(default_factory=list, description="本页文本块")
    has_text_layer: bool = Field(default=True, description="是否有文字层")
    avg_confidence: Optional[float] = Field(
        default=None, ge=0.0, le=1.0, description="本页平均 OCR 置信度"
    )
    width: float = Field(default=0.0, ge=0.0, description="页面宽度（PDF 点）")
    height: float = Field(default=0.0, ge=0.0, description="页面高度（PDF 点）")

    @property
    def char_count(self) -> int:
        """
        本页有效字符数（剔除空白）

        返回：
            非空白字符数量
        """
        return sum(1 for ch in self.text if not ch.isspace())


class RawParseResult(BaseModel):
    """
    解析器原始产物 — 三类解析器的统一返回结构

    字段：
        pages: 逐页结果，索引 0 对应第 1 页
        page_count: 总页数
        engine: 实际使用的解析引擎标识
        avg_confidence: 全文档平均 OCR 置信度，仅 OCR 有值
        is_encrypted: 是否加密
        has_text_layer: 是否整体含文字层
        extras: 解析器特有附加信息（表单域、批注、表格数量等）
        warnings: 解析过程中的告警信息（非致命，供人工排查）
    """

    pages: List[RawPage] = Field(default_factory=list, description="逐页结果")
    page_count: int = Field(default=0, ge=0, description="总页数")
    engine: str = Field(default="", description="解析引擎标识")
    avg_confidence: Optional[float] = Field(
        default=None, ge=0.0, le=1.0, description="全文档平均 OCR 置信度"
    )
    is_encrypted: bool = Field(default=False, description="是否加密")
    has_text_layer: bool = Field(default=True, description="是否含文字层")
    extras: Dict[str, Any] = Field(default_factory=dict, description="附加信息")
    warnings: List[str] = Field(default_factory=list, description="非致命告警")

    # ---------------------- 派生属性 ----------------------

    @property
    def full_text(self) -> str:
        """
        拼接全文（页间以换行分隔）

        说明：
            该拼接方式是全局字符偏移计算的唯一依据，
            parse_service 的偏移计算必须与此保持一致。

        返回：
            完整文本
        """
        if not self.pages:
            return ""
        return "\n".join(page.text for page in self.pages)

    @property
    def all_blocks(self) -> List[RawBlock]:
        """
        展平全部页面中的原始块

        返回：
            原始块列表，按页码与页内顺序排列
        """
        result: List[RawBlock] = []
        for page in self.pages:
            result.extend(page.blocks)
        return result

    @property
    def total_chars(self) -> int:
        """
        全文档有效字符数

        返回：
            所有页面非空白字符之和
        """
        return sum(page.char_count for page in self.pages)

    @property
    def empty_page_count(self) -> int:
        """
        无有效文本的页面数量

        说明：
            空页比例过高是"扫描件未正确 OCR"或"PDF 为纯图片"的典型特征，
            是触发 OCR 降级的重要判据。

        返回：
            有效字符数为 0 的页面数量
        """
        return sum(1 for page in self.pages if page.char_count == 0)
