# D:/ContractReviewSys-合同审批智能审查项目/backend/app/schemas/report.py
"""
报告与评论接口契约模型

【目录职责】
    定义"报告生成、评论预览、评论回写、报告下载"接口的请求与响应模型。
    评论回写仅写入评论区，不改变审批结论（权责红线）。
"""

from typing import List, Optional

from pydantic import BaseModel, Field


# ======================================================================
# 一、报告生成与下载
# ======================================================================
class ReportGenerateRequest(BaseModel):
    """
    生成审查报告请求

    字段：
        task_id: 任务 ID
        format: 导出格式
                md  —— 生成 Markdown 源文本（默认，兼容原有调用）
                pdf —— 生成 PDF 版式报告（PyMuPDF 原生绘制）
                说明：无论传何种格式，Markdown 源文本始终会先生成并落库，
                      因为 PDF 由该源文本渲染而来，二者必须内容一致。
    """

    task_id: str = Field(description="任务 ID")
    format: str = Field(default="md", description="导出格式：md / pdf")


class ReportGenerateResponse(BaseModel):
    """
    生成报告响应

    字段：
        task_id: 任务 ID
        report_url: 报告预签名访问链接
        report_object_key: 报告在 MinIO 中的对象键（已持久化到任务文档）
        quality_passed: 质量校验是否通过
        risk_item_count: 风险项总数
        report_format: 报告导出格式
        reused: 是否复用了已有报告文件（内容未变更时为 True）
        persisted: 报告字段是否已成功落库
        pdf_object_key: PDF 对象键（缺口1 新增，未生成时为空字符串）
        pdf_url: PDF 预签名访问链接（缺口1 新增）
        pdf_size_bytes: PDF 字节数（缺口1 新增）
        pdf_page_count: PDF 页数（缺口1 新增）
        pdf_render_engine: PDF 渲染引擎标识，固定 pymupdf（缺口1 新增）
    """

    task_id: str = Field(description="任务 ID")
    report_url: str = Field(default="", description="报告访问链接")
    report_object_key: str = Field(default="", description="报告对象键")
    quality_passed: bool = Field(default=False, description="质量校验是否通过")
    risk_item_count: int = Field(default=0, description="风险项总数")
    report_format: str = Field(default="md", description="报告导出格式")
    reused: bool = Field(default=False, description="是否复用已有报告文件")
    persisted: bool = Field(default=False, description="报告字段是否已落库")
    pdf_object_key: str = Field(default="", description="PDF 报告对象键")
    pdf_url: str = Field(default="", description="PDF 报告访问链接")
    pdf_size_bytes: int = Field(default=0, description="PDF 文件字节数")
    pdf_page_count: int = Field(default=0, description="PDF 页数")
    pdf_render_engine: str = Field(default="", description="PDF 渲染引擎标识")
    pdf_reused: bool = Field(default=False, description="PDF 是否复用了已有文件")


class ReportDownloadResponse(BaseModel):
    """
    下载报告响应（JSON 模式）

    说明：
        前三个字段沿用原有结构，保证前端无需改动；
        其余为新增的元信息字段（可选，老前端会忽略）。

    ★ PDF 格式下的特殊约定：
        PDF 是二进制内容，塞入 JSON 的 content 字段会破坏原有契约
        （老前端把 content 当 Markdown 文本渲染），因此：
        当 format=pdf 时 content 固定为空字符串，
        正文请改用 mode=stream 获取文件流，或直接使用 report_url。

    字段：
        task_id: 任务 ID
        content: 报告 Markdown 正文（format=md 时有效；pdf 时为空）
        report_url: 报告预签名链接（每次下载重新签发，pdf 亦可直接访问）
        report_object_key: MinIO 对象键
        file_name: 建议的下载文件名，形如 {task_id}_review_report.md / .pdf
        report_format: 报告格式（md / pdf）
        report_generated_at: 报告生成时间（ISO 8601）
        report_available: 报告是否可用（True 表示已读取到文件）
    """

    task_id: str = Field(description="任务 ID")
    content: str = Field(default="", description="报告 Markdown 内容（pdf 格式时为空）")
    report_url: str = Field(default="", description="报告预签名链接")
    report_object_key: str = Field(default="", description="报告对象键")
    file_name: str = Field(default="", description="下载文件名")
    report_format: str = Field(default="md", description="报告格式：md / pdf")
    report_generated_at: Optional[str] = Field(
        default=None, description="报告生成时间（ISO 8601）"
    )
    report_available: bool = Field(default=False, description="报告是否可用")


# ======================================================================
# 二、评论预览与回写
# ======================================================================
class CommentPreviewRequest(BaseModel):
    """
    预览审批评论请求

    字段：
        task_id: 任务 ID
    """

    task_id: str = Field(description="任务 ID")


class CommentPreviewResponse(BaseModel):
    """
    预览评论响应

    字段：
        task_id：任务 ID
        comment_text：评论文本
        api_path：评论接口路径
        risk_item_count：风险项数
        validation_passed：格式校验是否通过
        validation_issues：校验问题列表
    """

    task_id: str = Field(description="任务 ID")
    comment_text: str = Field(default="", description="评论文本")
    api_path: str = Field(default="", description="评论接口路径")
    risk_item_count: int = Field(default=0, description="风险项数")
    validation_passed: bool = Field(default=False, description="格式校验是否通过")
    validation_issues: List[str] = Field(
        default_factory=list, description="校验问题列表"
    )


class CommentWriteBackRequest(BaseModel):
    """
    正式回写审批评论请求

    字段：
        task_id: 任务 ID
        preview: 是否预览模式（True 仅记录不写入）
    """

    task_id: str = Field(description="任务 ID")
    preview: bool = Field(default=False, description="是否预览模式")


class CommentWriteBackResponse(BaseModel):
    """
    回写评论响应

    字段：
        task_id：任务 ID
        write_back_status：回写状态（not_written/writing/success/failed）
        preview：是否预览模式
        http_status：审批系统 HTTP 状态码（正式模式）
        response_code：审批系统业务码（正式模式）
        error：失败错误信息
    """

    task_id: str = Field(description="任务 ID")
    write_back_status: str = Field(description="回写状态")
    preview: bool = Field(default=False, description="是否预览模式")
    http_status: Optional[int] = Field(default=None, description="HTTP 状态码")
    response_code: Optional[int] = Field(default=None, description="业务码")
    error: str = Field(default="", description="失败错误信息")
