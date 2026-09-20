# D:/ContractReviewSys-合同审批智能审查项目/backend/app/models/documents/review_task.py
"""
审查任务文档模型 — 对应 MongoDB 集合 cr_review_task

【模型定位】
    本模型是"任务状态机"的载体，同时承载异步会话隔离信息。
    状态流转规则：
        pending → parsing → reviewing → completed
                       ↘         ↘
                        blocked ←──┘（阻塞后可人工重跑，回到原阶段）
    回写状态独立于审查状态，单独维护：
        not_written → writing → success / failed

返回值说明：
    本模块只定义数据模型，无函数返回值

使用示例：
    from app.models.documents.review_task import ReviewTask, BlockedReason
    task = ReviewTask(contract_id="...", task_no="T20260918001")
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator

from app.constants import ReviewTaskStatus, RiskLevel, WriteBackStatus
from enum import Enum, unique


@unique
class BlockedReason(str, Enum):
    """
    任务阻塞原因码

    说明：
        法定阻塞（文档加密 / 扫描件模糊 / 正文为空）由解析阶段直接判定；
        技术阻塞（重试耗尽 / 解析失败）由异常重试机制判定。
        两者都必须给出明确原因码，禁止出现"未知原因阻塞"。
    """

    DOC_ENCRYPTED = "DOC_ENCRYPTED"          # 文档加密，无法解析
    SCAN_TOO_BLURRY = "SCAN_TOO_BLURRY"      # 扫描件模糊，OCR 置信度过低
    EMPTY_CONTENT = "EMPTY_CONTENT"          # 正文为空，文本长度不足
    PARSE_FAILED = "PARSE_FAILED"            # 解析失败
    RETRY_EXHAUSTED = "RETRY_EXHAUSTED"      # 重试次数耗尽
    MANUAL = "MANUAL"                        # 管理员人工置阻塞


@unique
class RouteType(str, Enum):
    """
    差异化路由类型

    说明：由接入流转模块的路由决策器判定，决定走哪条审查链路。
    """

    FAST = "fast"          # 快速通道：模板合同，仅跑结构化规则
    STANDARD = "standard"  # 标准审查：完整多路召回 + 大模型辅助
    DEEP = "deep"          # 深度复核：大额高风险，含人工复核网关


class NodeExecution(BaseModel):
    """
    节点执行记录

    字段：
        node: 节点名，取自 WorkflowNode 枚举
        status: 节点状态（pending/running/success/failed/skipped）
        retry: 该节点已重试次数
        started_at: 节点开始时间
        finished_at: 节点结束时间
        error: 失败时的错误摘要
    """

    node: str = Field(description="节点名")
    status: str = Field(default="pending", description="节点状态")
    retry: int = Field(default=0, ge=0, description="已重试次数")
    started_at: Optional[datetime] = Field(default=None, description="开始时间")
    finished_at: Optional[datetime] = Field(default=None, description="结束时间")
    error: Optional[str] = Field(default=None, description="失败错误摘要")

    @property
    def elapsed_ms(self) -> Optional[float]:
        """
        节点耗时（毫秒）

        返回：
            已结束时返回耗时毫秒数；未结束时返回 None
        """
        if self.started_at is None or self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds() * 1000.0


class ReviewTask(BaseModel):
    """
    审查任务模型

    字段：
        id: 任务主键
        task_no: 任务编号（唯一）
        contract_id: 所属合同 ID
        status: 任务状态，取值见 ReviewTaskStatus 枚举
        route_type: 路由类型，取值见 RouteType 枚举
        current_node: 当前执行节点名
        node_history: 节点执行历史（支持单节点重试追溯）
        blocked_reason: 阻塞原因码，仅 blocked 状态有值
        blocked_detail: 阻塞详情，便于人工处理
        retry_count: 当前节点重试次数
        max_retry: 重试上限
        thread_id: LangGraph 会话 ID（异步会话隔离）
        work_dir: 任务独立工作目录（文件目录隔离）
        progress: 进度百分比 0-100
        write_back_status: 回写状态，取值见 WriteBackStatus 枚举
        write_back_retry: 回写重试次数
        report_object_key: 审查报告在 MinIO 中的对象键，未生成时为空
        report_format: 报告导出格式（md/pdf/docx/json）
        report_generated_at: 报告生成时间，未生成时为 None
        report_content_hash: 报告内容的 SHA-256 指纹，用于幂等复用判定
        pdf_object_key: PDF 报告在 MinIO 中的对象键，与 report_object_key 并存
        pdf_generated_at: PDF 报告生成时间，未生成时为 None
        pdf_content_hash: PDF 所依据源文本的 SHA-256 指纹，用于幂等复用判定
        pdf_size_bytes: PDF 文件字节数
        pdf_render_engine: PDF 渲染引擎标识（固定 pymupdf）
        created_by: 提交人
        started_at: 开始时间
        finished_at: 结束时间
        created_at: 创建时间
        updated_at: 更新时间

【报告持久化字段的兼容性说明】
    报告类字段均为可选且带默认值。历史任务（在这些字段引入之前创建）
    从 MongoDB 读出的文档不含这些键，Pydantic 会用默认值填充，
    因此旧数据不会引发校验错误，列表/详情接口也无需改动。

【两种格式并存的设计意图】
    Markdown（report_object_key）是**权威源文本**，承载报告全部结构化内容；
    PDF（pdf_object_key）是**面向交付的版式化产物**，由同一份源文本渲染而来。
    二者内容必须 100% 一致 —— PDF 渲染器只读取 Markdown 源文本，
    不单独查询风险数据，从机制上杜绝"两种格式结论不一致"的风险。
"""

    id: Optional[str] = Field(default=None, description="任务主键")
    task_no: str = Field(description="任务编号（唯一）")
    contract_id: str = Field(description="所属合同 ID")

    # ---------------------- 状态机 ----------------------
    status: str = Field(
        default=ReviewTaskStatus.PENDING.value,
        description="任务状态：pending/parsing/reviewing/blocked/completed",
    )
    route_type: str = Field(
        default=RouteType.STANDARD.value,
        description="路由类型：fast/standard/deep",
    )
    current_node: Optional[str] = Field(default=None, description="当前执行节点名")
    node_history: List[NodeExecution] = Field(
        default_factory=list,
        description="节点执行历史，支持单节点重试追溯",
    )

    # ---------------------- 阻塞与重试 ----------------------
    blocked_reason: Optional[str] = Field(
        default=None, description="阻塞原因码，仅 blocked 状态有值"
    )
    blocked_detail: Optional[str] = Field(
        default=None, description="阻塞详情，便于人工定位处理"
    )
    retry_count: int = Field(default=0, ge=0, description="当前节点重试次数")
    max_retry: int = Field(default=3, ge=1, description="重试上限")

    # ---------------------- 会话与目录隔离 ----------------------
    thread_id: str = Field(description="LangGraph 会话 ID，保证并发任务上下文隔离")
    work_dir: str = Field(description="任务独立工作目录，避免并发文件串扰")

    # ---------------------- 进度 ----------------------
    progress: int = Field(default=0, ge=0, le=100, description="进度百分比")

    # ---------------------- 回写状态机 ----------------------
    write_back_status: str = Field(
        default=WriteBackStatus.PENDING.value,
        description="回写状态：not_written/writing/success/failed",
    )
    write_back_retry: int = Field(default=0, ge=0, description="回写重试次数")

    # ---------------------- 报告持久化（缺口2 修复新增） ----------------------
    # 说明：报告生成后需持久化对象键，否则下载接口无从取回文件。
    #       三个字段都可选，保证历史任务读出时不触发校验错误。
    report_object_key: Optional[str] = Field(
        default=None,
        description="审查报告在 MinIO 中的对象键，未生成时为 None",
    )
    report_format: Optional[str] = Field(
        default=None,
        description="报告导出格式：md/pdf/docx/json，未生成时为 None",
    )
    report_generated_at: Optional[datetime] = Field(
        default=None,
        description="报告生成时间，未生成时为 None",
    )
    report_content_hash: Optional[str] = Field(
        default=None,
        description="报告内容 SHA-256 指纹，用于幂等复用判定（内容未变则不重复上传）",
    )

    # ---------------------- PDF 报告持久化（缺口1 修复新增） ----------------------
    # 设计说明：
    #   PDF 与 Markdown 是**并列的两种交付格式**，因此对象键必须分开存放，
    #   不能复用 report_object_key —— 否则两种格式会互相覆盖，
    #   下载接口也无法判断某一格式是否已生成。
    #   pdf_content_hash 记录「渲染 PDF 所依据的 Markdown 源文本」的指纹，
    #   与 report_content_hash 对齐：只要源文本未变，PDF 即可直接复用。
    #   四个字段全部可选，保证历史任务读出时不触发校验错误。
    pdf_object_key: Optional[str] = Field(
        default=None,
        description="PDF 报告在 MinIO 中的对象键，未生成时为 None",
    )
    pdf_generated_at: Optional[datetime] = Field(
        default=None,
        description="PDF 报告生成时间，未生成时为 None",
    )
    pdf_content_hash: Optional[str] = Field(
        default=None,
        description=(
            "PDF 所依据源文本的 SHA-256 指纹，用于幂等复用判定。"
            "与 report_content_hash 同源，源文本未变则 PDF 直接复用"
        ),
    )
    pdf_size_bytes: Optional[int] = Field(
        default=None,
        ge=0,
        description="PDF 文件字节数，用于审计与下载响应头 Content-Length",
    )
    pdf_render_engine: Optional[str] = Field(
        default=None,
        description="PDF 渲染引擎标识（固定 pymupdf），便于日后审计渲染来源",
    )

    # ---------------------- 合同原件引用 ----------------------
    # 说明：审查流程需要读取合同原文（解析节点），而合同业务字段（标题/金额等）
    #       传统上由审批系统提供。自"上传免填表单"改造后（2026-09-19），
    #       手动上传任务的业务字段由 metadata_extractor 从合同原文自动识别
    #       并回填（contract_title），审批单上下文缺失时以识别值兜底。
    contract_title: Optional[str] = Field(
        default=None,
        description="合同标题（审批单提供或解析后由系统自动识别回填；空表示尚未识别）",
    )
    object_key: Optional[str] = Field(
        default=None, description="合同原件在 MinIO 中的对象键（合同桶）"
    )
    source_type: str = Field(
        default="digital", description="合同来源类型 digital / scanned"
    )
    file_name: str = Field(default="", description="合同原件原始文件名")
    # ---------------------- 失败分类（2026-09-20 交互优化新增） ----------------------
    # 说明：解析/审查失败时按异常分类落库（FailureType），供大盘悬浮提示、
    #       失败类型筛选与"瞬时异常自动重试一次"决策使用。
    failure_type: Optional[str] = Field(
        default=None,
        description="失败类型（FailureType 取值）；仅 status=failed 时有值",
    )
    failure_reason: Optional[str] = Field(
        default=None,
        description="面向用户的失败原因摘要（脱敏后）；仅 status=failed 时有值",
    )
    parse_status: str = Field(
        default="not_started", description="解析状态 not_started/parsing/parsed/failed"
    )
    # 解析产物快照：供解析结果接口回读（全文 + 文本块）。
    # 说明：解析产物本身也缓存在 parse_service 的进程内缓存中，
    #       但进程重启即失效；此处持久化一份，保证接口随时可查、可追溯。
    parse_snapshot: Optional[Dict[str, Any]] = Field(
        default=None, description="解析产物快照（full_text / text_blocks / page_count）"
    )

    # ---------------------- 时间与人员 ----------------------
    created_by: str = Field(default="", description="提交人")
    started_at: Optional[datetime] = Field(default=None, description="开始时间")
    finished_at: Optional[datetime] = Field(default=None, description="结束时间")
    created_at: datetime = Field(default_factory=datetime.now, description="创建时间")
    updated_at: datetime = Field(default_factory=datetime.now, description="更新时间")

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        """
        校验任务状态取值合法性

        参数：
            value: 状态字符串
        返回：
            校验通过的状态值
        异常：
            ValueError：取值不在 ReviewTaskStatus 枚举内时抛出
        """
        allowed = {item.value for item in ReviewTaskStatus}
        if value not in allowed:
            raise ValueError(f"任务状态非法：{value}，允许值：{sorted(allowed)}")
        return value

    @field_validator("write_back_status")
    @classmethod
    def validate_write_back_status(cls, value: str) -> str:
        """
        校验回写状态取值合法性

        参数：
            value: 回写状态字符串
        返回：
            校验通过的回写状态值
        异常：
            ValueError：取值不在 WriteBackStatus 枚举内时抛出
        """
        allowed = {item.value for item in WriteBackStatus}
        if value not in allowed:
            raise ValueError(f"回写状态非法：{value}，允许值：{sorted(allowed)}")
        return value

    @property
    def is_blocked(self) -> bool:
        """
        判断任务是否处于阻塞状态

        返回：
            True 表示已阻塞
        """
        return self.status == ReviewTaskStatus.BLOCKED.value

    @property
    def can_retry(self) -> bool:
        """
        判断当前节点是否还可继续自动重试

        返回：
            True 表示未达重试上限，可继续重试
        """
        return self.retry_count < self.max_retry

    def next_retry_delay(self) -> float:
        """
        计算下一次重试的退避等待时间（秒）

        说明：
            指数退避策略：1s → 2s → 4s → 8s（上限 8 秒），
            避免下游服务被瞬时重试压垮。

        返回：
            等待秒数
        """
        return min(2.0 ** self.retry_count, 8.0)

    @property
    def is_llm_judged(self) -> bool:
        """
        标记位：本任务是否已由规则引擎完成判定

        说明：
            该属性始终返回 False，用于代码层面提醒：
            任务状态本身不代表判定完成，判定结果必须查
            cr_review_conclusion 集合的 decision_source 字段。
            任何依据任务状态直接推断风险结论的实现都是违规的。

        返回：
            False（恒为 False，语义为"不可由任务状态推断判定结果"）
        """
        return False
