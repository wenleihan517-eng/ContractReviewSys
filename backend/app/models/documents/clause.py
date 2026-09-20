# D:/ContractReviewSys-合同审批智能审查项目/backend/app/models/documents/clause.py
"""
合同条款文档模型 — 对应 MongoDB 集合 cr_contract_clause

【模型定位】
    本模型是"双向溯源"能力的载体。条款切分结果必须完整记录原文位置，
    未记录位置信息的条款视为无效数据，禁止落库。

【双向溯源设计】
    正向：条款 → 原文（风险点击 → PDF 翻页 + 高亮）
        依赖 bbox_list（PDF 坐标框）与 page_no（页码）
    反向：原文 → 条款（用户圈选 → 定位条款）
        依赖 char_start / char_end（原文字符区间）

返回值说明：
    本模块只定义数据模型，无函数返回值

使用示例：
    from app.models.documents.clause import ContractClause
    clause = ContractClause(contract_id="...", clause_index=1, char_start=1024, char_end=1089)
"""

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator

from app.constants import ClauseType, ParseEngine


class BoundingBox(BaseModel):
    """
    页面坐标框 — PDF 高亮定位的最小单元

    字段：
        page: 页码，从 1 开始
        x: 左上角横坐标（PDF 坐标系，单位 pt）
        y: 左上角纵坐标
        width: 宽度
        height: 高度
    """

    page: int = Field(ge=1, description="页码，从 1 开始")
    x: float = Field(description="左上角横坐标")
    y: float = Field(description="左上角纵坐标")
    width: float = Field(gt=0, description="宽度，必须为正数")
    height: float = Field(gt=0, description="高度，必须为正数")


class ContractClause(BaseModel):
    """
    合同条款结构化模型

    字段：
        id: 条款主键（MongoDB _id 的字符串形式）
        contract_id: 所属合同 ID
        clause_index: 条款在合同中的序号，从 1 开始
        clause_type: 条款类型，取值见 ClauseType 枚举
        clause_title: 条款标题，如"第三条 违约责任"
        content: 条款正文全文
        char_start: 原文起始字符位置（含），反向溯源锚点
        char_end: 原文结束字符位置（不含），反向溯源锚点
        page_start: 起始页码
        page_end: 结束页码
        paragraph_index: 原文段落索引
        bbox_list: 坐标框列表，正向溯源锚点（PDF 高亮用）
        source_engine: 解析引擎类型，取值见 ParseEngine 枚举
        ocr_confidence: OCR 置信度（0-1），仅扫描件有值
        created_at: 创建时间
    """

    id: Optional[str] = Field(default=None, description="条款主键")
    contract_id: str = Field(description="所属合同 ID")
    clause_index: int = Field(ge=1, description="条款序号，从 1 开始")
    clause_type: str = Field(
        default=ClauseType.OTHER.value,
        description="条款类型，取值见 ClauseType 枚举",
    )
    clause_title: str = Field(default="", description="条款标题")
    content: str = Field(description="条款正文全文")

    # ---------------------- 位置信息（溯源核心，必填） ----------------------
    char_start: int = Field(ge=0, description="原文起始字符位置（含）")
    char_end: int = Field(gt=0, description="原文结束字符位置（不含）")
    page_start: int = Field(ge=1, description="起始页码")
    page_end: int = Field(ge=1, description="结束页码")
    paragraph_index: int = Field(ge=0, description="原文段落索引")

    # ---------------------- PDF 高亮坐标 ----------------------
    bbox_list: List[BoundingBox] = Field(
        default_factory=list,
        description="坐标框列表，供前端 PDF 高亮层绘制",
    )

    # ---------------------- 解析元信息 ----------------------
    source_engine: str = Field(
        default=ParseEngine.PDF_TEXT.value,
        description="解析引擎类型，取值见 ParseEngine 枚举",
    )
    ocr_confidence: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="OCR 置信度（0-1），仅扫描件有值",
    )
    created_at: datetime = Field(default_factory=datetime.now, description="创建时间")

    @field_validator("char_end")
    @classmethod
    def validate_char_range(cls, value: int, info) -> int:
        """
        校验字符区间合法性

        参数：
            value: char_end 的值
            info: Pydantic 校验上下文，可读取 char_start
        返回：
            校验通过的 char_end
        异常：
            ValueError：char_end 小于等于 char_start 时抛出
        """
        char_start = info.data.get("char_start")
        if char_start is not None and value <= char_start:
            raise ValueError(
                f"char_end({value}) 必须大于 char_start({char_start})，否则区间非法"
            )
        return value

    @field_validator("page_end")
    @classmethod
    def validate_page_range(cls, value: int, info) -> int:
        """
        校验页码区间合法性

        参数：
            value: page_end 的值
            info: Pydantic 校验上下文，可读取 page_start
        返回：
            校验通过的 page_end
        异常：
            ValueError：page_end 小于 page_start 时抛出
        """
        page_start = info.data.get("page_start")
        if page_start is not None and value < page_start:
            raise ValueError(
                f"page_end({value}) 不能小于 page_start({page_start})"
            )
        return value

    @property
    def char_length(self) -> int:
        """
        条款正文字符数

        返回：
            char_end - char_start 的差值
        """
        return self.char_end - self.char_start

    @property
    def page_span(self) -> List[int]:
        """
        条款跨越的页码列表

        返回：
            从 page_start 到 page_end 的连续页码列表
        """
        return list(range(self.page_start, self.page_end + 1))

    def contains_char(self, char_pos: int) -> bool:
        """
        判断给定字符位置是否落在本条款区间内

        说明：
            用于"原文 → 条款"的反向溯源：用户在 PDF 上圈选后，
            以选区字符偏移量反查覆盖该位置的条款。

        参数：
            char_pos: 待判断的原文字符位置
        返回：
            True 表示该位置属于本条款
        """
        return self.char_start <= char_pos < self.char_end
