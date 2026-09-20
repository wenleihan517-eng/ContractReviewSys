# D:/ContractReviewSys-合同审批智能审查项目/backend/app/schemas/parse.py
"""
文档解析接口契约模型

【目录职责】
    定义"文档解析"接口的请求与响应模型。解析是异步任务的第一阶段，
    接口只暴露触发与结果查询，不暴露解析器内部细节。
"""

from typing import Any, Dict, List

from pydantic import BaseModel, Field


# ======================================================================
# 一、上传与触发解析
# ======================================================================
class ParseTriggerRequest(BaseModel):
    """
    触发解析请求

    字段：
        task_id: 任务 ID（关联已创建的任务）
        object_key: MinIO 对象键（已上传文件的引用）
        source_type: 来源类型（digital/scanned）
        file_name: 原始文件名（可选）
    """

    task_id: str = Field(description="任务 ID")
    object_key: str = Field(description="MinIO 对象键")
    source_type: str = Field(default="digital", description="来源类型 digital/scanned")
    file_name: str = Field(default="", description="原始文件名")


class ParseTriggerResponse(BaseModel):
    """
    触发解析响应
    """

    task_id: str = Field(description="任务 ID")
    accepted: bool = Field(default=True, description="是否已受理")
    message: str = Field(default="", description="受理说明")


# ======================================================================
# 二、解析结果查询
# ======================================================================
class TextBlockItem(BaseModel):
    """
    文本块（前端展示用）

    字段：
        block_index / page：块序号与页码
        start_idx / end_idx：全文字符区间
        paragraph_index：段落索引
        text：块文本
    """

    block_index: int = Field(description="块序号")
    page: int = Field(description="页码")
    start_idx: int = Field(description="起始字符位置")
    end_idx: int = Field(description="结束字符位置")
    paragraph_index: int = Field(default=0, description="段落索引")
    text: str = Field(default="", description="块文本")


class ClauseItem(BaseModel):
    """
    条款项（前端展示用）
    """

    clause_id: str = Field(description="条款 ID")
    clause_index: int = Field(description="条款序号")
    clause_type: str = Field(default="other", description="条款类型")
    clause_title: str = Field(default="", description="条款标题")
    content: str = Field(default="", description="条款正文")


class ParseResultResponse(BaseModel):
    """
    解析结果响应

    字段：
        task_id：任务 ID
        parse_status：解析状态
        full_text：抽取的合同全文
        page_count：页数
        total_chars：有效字符数
        parse_engine：解析引擎
        source_type：来源类型
        text_blocks：文本块列表
        clauses：条款列表
        metadata：合同元数据（金额/主体/期限等）
        parse_error：解析错误（如有）
    """

    task_id: str = Field(description="任务 ID")
    parse_status: str = Field(description="解析状态")
    full_text: str = Field(default="", description="合同全文")
    page_count: int = Field(default=0, description="页数")
    total_chars: int = Field(default=0, description="有效字符数")
    parse_engine: str = Field(default="", description="解析引擎")
    source_type: str = Field(default="", description="来源类型")
    text_blocks: List[TextBlockItem] = Field(default_factory=list, description="文本块")
    clauses: List[ClauseItem] = Field(default_factory=list, description="条款列表")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="合同元数据")
    parse_error: str = Field(default="", description="解析错误")
