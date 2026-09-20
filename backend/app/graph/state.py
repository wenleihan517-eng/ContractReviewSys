# D:/ContractReviewSys-合同审批智能审查项目/backend/app/graph/state.py
"""
LangGraph 审查流程状态定义 — ReviewTaskState

【文件定位】
    本模块是整个 LangGraph 编排层的"状态契约"，定义流程在节点之间流转时
    携带的全部数据。所有节点函数的入参均为本状态，出参均为本状态的增量字典。

【状态分组（对应架构文档"核心业务全流程"）】
    ┌──────────────┬────────────────────────────────────────────┐
    │ 分组          │ 内容                                        │
    ├──────────────┼────────────────────────────────────────────┤
    │ 任务基础信息  │ task_id / approval_id / 合同元数据           │
    │ 文档解析状态  │ 解析状态 / 文本块 / 段落树 / 元数据 / 错误信息 │
    │ 风险审查状态  │ 规则结果 / 向量结果 / 大模型结果 / 融合结果    │
    │ 回写状态      │ 回写状态 / 回写内容 / 接口返回信息            │
    │ 审计日志      │ 累加型，每个节点操作自动追加                  │
    └──────────────┴────────────────────────────────────────────┘

【架构红线（本模块的代码级强制点）】
    1. 综合风险等级字段名为 risk_level，其注释中明确标注"仅规则引擎可写"。
    2. 大模型结果以大模型候选语义承载，数据结构上不含风险等级字段，
       从数据层杜绝"模型输出伪装成判定结果"。
    3. 审计日志与节点历史为累加型（operator.add 归约），
       保证多节点并行执行时日志不丢失、不互相覆盖。

返回值说明：
    本模块只定义数据模型与归约器，无函数返回值

使用示例：
    from app.graph.state import ReviewTaskState, TextBlock, AuditLogEntry
    state: ReviewTaskState = {
        "task_id": "xxx",
        "task_no": "T20260918001",
        ...
    }
"""

from datetime import datetime
from enum import Enum, unique
from typing import Annotated, Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from app.constants import (
    RISK_LEVEL_ORDER,
    RULE_ENGINE_SOURCE,
    ReviewTaskStatus,
    RiskLevel,
    WriteBackStatus,
)
from app.models.documents.review_task import BlockedReason, RouteType

# ======================================================================
# 零、列表归约器
# 说明：LangGraph 中标注为 Annotated[List, add] 的字段，在并行分支
#       同时写入时会自动做列表拼接，而不是后者覆盖前者。
#       审计日志与节点执行历史的正确性完全依赖该归约器。
# ======================================================================
from operator import add as _list_add

ListReducer = Annotated[List[Any], _list_add]


# ======================================================================
# 一、枚举定义（图内专用，避免污染全局 constants）
# ======================================================================
@unique
class ParseStatus(str, Enum):
    """
    文档解析状态

    流转路径：
        not_started → parsing → success
                            ↘ failed / blocked
    """

    NOT_STARTED = "not_started"    # 尚未开始解析
    PARSING = "parsing"            # 解析中（文本抽取或 OCR）
    SUCCESS = "success"            # 解析成功
    FAILED = "failed"              # 解析失败（技术性失败，可重试）
    BLOCKED = "blocked"            # 解析被阻断（加密 / 模糊 / 空内容，不可自动重试）


@unique
class WriteBackState(str, Enum):
    """
    回写状态

    说明：与全局 WriteBackStatus 保持语义一致，此处独立定义是为了
          在状态对象内部使用更贴近图语义的命名，避免歧义。
    """

    NOT_WRITTEN = "not_written"    # 尚未回写
    WRITING = "writing"            # 回写中
    SUCCESS = "success"            # 回写成功
    FAILED = "failed"              # 回写失败


@unique
class AuditAction(str, Enum):
    """审计动作类型，用于审计日志分类检索"""

    NODE_ENTER = "node_enter"          # 节点进入
    NODE_EXIT = "node_exit"            # 节点正常退出
    NODE_RETRY = "node_retry"          # 节点重试
    NODE_SKIP = "node_skip"            # 节点跳过（路由未选中）
    NODE_ERROR = "node_error"          # 节点异常
    STATE_CHANGE = "state_change"      # 状态流转
    REDLINE_DENY = "redline_deny"      # 红线拦截（越权写入被拒）
    TASK_BLOCKED = "task_blocked"      # 任务被置为阻塞
    TASK_FINISHED = "task_finished"    # 任务完成


# ======================================================================
# 二、子结构定义
# ======================================================================
class TextBlock(BaseModel):
    """
    文本块 — 文档解析产物的最小单元

    说明：
        文本块是条款切分的输入，也是"原文 ↔ 结构化结果"双向溯源的锚点。
        每个块必须记录字符区间与段落索引，缺失位置信息的块视为无效。

    字段：
        block_index: 块在全文中的序号，从 0 开始
        page: 所在页码，从 1 开始
        start_idx: 块在全文中的起始字符位置（含）
        end_idx: 块在全文中的结束字符位置（不含）
        paragraph_index: 所属段落索引，从 0 开始
        text: 块文本内容
        ocr_confidence: OCR 置信度（0-1），仅走 OCR 的块有值
        engine: 产出该块的解析引擎标识
    """

    block_index: int = Field(ge=0, description="块序号，从 0 开始")
    page: int = Field(ge=1, description="页码，从 1 开始")
    start_idx: int = Field(ge=0, description="全文起始字符位置（含）")
    end_idx: int = Field(gt=0, description="全文结束字符位置（不含）")
    paragraph_index: int = Field(ge=0, description="段落索引，从 0 开始")
    text: str = Field(default="", description="块文本内容")
    ocr_confidence: Optional[float] = Field(
        default=None, ge=0.0, le=1.0, description="OCR 置信度，仅扫描件有值"
    )
    engine: str = Field(default="", description="产出该块的解析引擎标识")

    @model_validator(mode="after")
    def validate_char_range(self) -> "TextBlock":
        """
        跨字段校验：字符区间必须合法

        返回：
            校验通过的本实例

        异常：
            ValueError：end_idx 不大于 start_idx 时抛出
        """
        if self.end_idx <= self.start_idx:
            raise ValueError(
                f"文本块字符区间非法：start_idx={self.start_idx}, "
                f"end_idx={self.end_idx}，结束位置必须大于起始位置"
            )
        return self

    @property
    def char_length(self) -> int:
        """
        块字符数

        返回：
            end_idx - start_idx
        """
        return self.end_idx - self.start_idx


class ParagraphNode(BaseModel):
    """
    段落树节点 — 记录文档的层级结构

    说明：
        合同条款存在"章 → 条 → 款 → 项"的层级关系，段落树用于还原该结构，
        供条款结构化节点按层级切分，避免把一条完整条款切断。

    字段：
        paragraph_index: 段落索引，从 0 开始
        level: 层级深度，0 为顶层（章），依次递减
        title: 段落标题（如"第三条 违约责任"）
        content: 段落正文
        page: 所在页码
        start_idx / end_idx: 全文字符区间
        children: 子节点列表（递归结构）
    """

    paragraph_index: int = Field(ge=0, description="段落索引")
    level: int = Field(default=0, ge=0, description="层级深度，0 为顶层")
    title: str = Field(default="", description="段落标题")
    content: str = Field(default="", description="段落正文")
    page: int = Field(default=1, ge=1, description="所在页码")
    start_idx: int = Field(default=0, ge=0, description="全文起始字符位置")
    end_idx: int = Field(default=0, ge=0, description="全文结束字符位置")
    children: List["ParagraphNode"] = Field(
        default_factory=list, description="子段落列表"
    )


# 启用自引用类型的前向引用解析
ParagraphNode.model_rebuild()


class DocMetadata(BaseModel):
    """
    文档元数据 — 解析阶段产出的文档级描述信息

    字段：
        file_name: 原始文件名
        file_ext: 扩展名（小写，含点）
        file_size: 文件大小（字节）
        file_hash: 文件内容指纹（SHA-256），用于重复提交检测与断点续跑校验
        page_count: 总页数
        source_type: 来源类型 digital / scanned
        parse_engine: 实际使用的解析引擎
        is_encrypted: 是否为加密文档
        has_text_layer: 是否含文字层（决定是否需要 OCR）
        avg_ocr_confidence: 平均 OCR 置信度，仅扫描件有值
        total_chars: 抽取正文总字符数
        parsed_at: 解析完成时间
    """

    file_name: str = Field(default="", description="原始文件名")
    file_ext: str = Field(default="", description="扩展名，小写含点")
    file_size: int = Field(default=0, ge=0, description="文件大小（字节）")
    file_hash: str = Field(default="", description="文件内容 SHA-256 指纹")
    page_count: int = Field(default=0, ge=0, description="总页数")
    source_type: str = Field(default="digital", description="来源类型")
    parse_engine: str = Field(default="", description="实际使用的解析引擎")
    is_encrypted: bool = Field(default=False, description="是否为加密文档")
    has_text_layer: bool = Field(default=True, description="是否含文字层")
    avg_ocr_confidence: Optional[float] = Field(
        default=None, ge=0.0, le=1.0, description="平均 OCR 置信度"
    )
    total_chars: int = Field(default=0, ge=0, description="抽取正文总字符数")
    parsed_at: Optional[datetime] = Field(default=None, description="解析完成时间")


class ClauseItem(BaseModel):
    """
    条款项 — 结构化后的单条条款

    说明：
        本模型是"条款"在流程图内的轻量表达，与 models/documents/clause.py
        的 ContractClause 保持字段语义一致，但只保留流程节点需要的字段，
        避免图状态对象过于臃肿（落库时再转换为完整 ContractClause）。

    字段：
        clause_id: 条款标识（图内临时 ID）
        clause_index: 条款序号，从 1 开始
        clause_type: 条款类型
        clause_title: 条款标题
        content: 条款正文
        char_start / char_end: 全文字符区间（反向溯源锚点）
        page_start / page_end: 页码区间（正向溯源锚点）
        paragraph_index: 段落索引
    """

    clause_id: str = Field(description="条款标识")
    clause_index: int = Field(ge=1, description="条款序号")
    clause_type: str = Field(default="other", description="条款类型")
    clause_title: str = Field(default="", description="条款标题")
    content: str = Field(default="", description="条款正文")
    char_start: int = Field(default=0, ge=0, description="全文起始字符位置")
    char_end: int = Field(default=0, ge=0, description="全文结束字符位置")
    page_start: int = Field(default=1, ge=1, description="起始页码")
    page_end: int = Field(default=1, ge=1, description="结束页码")
    paragraph_index: int = Field(default=0, ge=0, description="段落索引")


class LLMCandidate(BaseModel):
    """
    大模型疑似风险候选

    【架构红线（数据结构级强制）】
        本模型刻意不提供 risk_level / decision / severity 字段。
        大模型的产出物在数据结构层面就无法承载"风险等级"，
        因此不可能冒充规则引擎的正式判定结果。

    字段：
        candidate_id: 候选标识
        clause_id: 关联条款 ID
        clause_type: 条款类型
        risk_type: 疑似风险类型描述（如"付款条件模糊"）
        evidence_text: 证据原文片段
        char_start / char_end: 证据字符区间
        page_no: 证据页码
        confidence: 模型自评置信度（0-1）
        suggestion_text: 修改建议初稿
        reason: 模型给出的判断理由（仅供人工参考）
        raw_output: 模型原始输出，便于问题回溯
    """

    candidate_id: str = Field(description="候选标识")
    clause_id: Optional[str] = Field(default=None, description="关联条款 ID")
    clause_type: Optional[str] = Field(default=None, description="条款类型")
    risk_type: str = Field(default="", description="疑似风险类型描述")
    evidence_text: str = Field(default="", description="证据原文片段")
    char_start: Optional[int] = Field(default=None, ge=0, description="证据起始位置")
    char_end: Optional[int] = Field(default=None, gt=0, description="证据结束位置")
    page_no: Optional[int] = Field(default=None, ge=1, description="证据页码")
    confidence: Optional[float] = Field(
        default=None, ge=0.0, le=1.0, description="模型自评置信度"
    )
    suggestion_text: str = Field(default="", description="修改建议初稿")
    reason: str = Field(default="", description="模型判断理由，仅供人工参考")
    raw_output: Dict[str, Any] = Field(
        default_factory=dict, description="模型原始输出，便于回溯"
    )


class VectorCandidate(BaseModel):
    """
    向量语义召回候选

    【架构红线（数据结构级强制）】
        与 LLMCandidate 一致，本模型不含风险等级字段。
        向量召回的产出物仅为"相似条款匹配线索"，供规则引擎研判。

    字段：
        candidate_id: 候选标识
        clause_id: 关联条款 ID
        matched_clause_id: 命中的标准条款库条目 ID
        matched_clause_title: 命中标准条款标题
        similarity: 余弦相似度
        evidence_text: 条款原文片段
        char_start / char_end: 字符区间
        page_no: 页码
        suggestion_text: 依据标准条款给出的建议方向
    """

    candidate_id: str = Field(description="候选标识")
    clause_id: Optional[str] = Field(default=None, description="关联条款 ID")
    matched_clause_id: Optional[str] = Field(
        default=None, description="命中的标准条款库条目 ID"
    )
    matched_clause_title: str = Field(default="", description="命中标准条款标题")
    similarity: float = Field(default=0.0, ge=-1.0, le=1.0, description="余弦相似度")
    evidence_text: str = Field(default="", description="条款原文片段")
    char_start: Optional[int] = Field(default=None, ge=0, description="起始字符位置")
    char_end: Optional[int] = Field(default=None, gt=0, description="结束字符位置")
    page_no: Optional[int] = Field(default=None, ge=1, description="页码")
    suggestion_text: str = Field(default="", description="建议方向")


class RiskItem(BaseModel):
    """
    最终风险项 — 融合后的正式风险记录

    【架构红线】
        risk_level 字段只能来源于规则引擎（rule_engine）。
        融合节点必须丢弃任何来自大模型/向量候选的等级字段，
        仅保留其证据与建议文本作为"参考内容"。

    字段：
        risk_id: 风险项标识
        rule_code: 规则编码（规则引擎命中时必填）
        rule_name: 规则名称
        clause_id: 命中条款 ID
        clause_type: 条款类型
        evidence_text: 证据原文
        char_start / char_end: 字符区间
        page_no: 页码
        risk_level: 风险等级（仅规则引擎可写）
        decision: 处理决策（仅规则引擎可写）
        severity: 规则严重度（仅规则引擎可写）
        judge_source: 判定来源，固定为 rule_engine
        suggestion_text: 修改建议
        legal_basis: 法律依据
        reference_only: 该风险项是否仅含参考内容（无规则判定）
        matched_sources: 命中的多路召回来源列表、冲突标记与判定来源快照
    """

    risk_id: str = Field(description="风险项标识")
    rule_code: Optional[str] = Field(default=None, description="规则编码")
    rule_name: str = Field(default="", description="规则名称")
    clause_id: Optional[str] = Field(default=None, description="命中条款 ID")
    clause_type: Optional[str] = Field(default=None, description="条款类型")
    evidence_text: str = Field(default="", description="证据原文")
    char_start: Optional[int] = Field(default=None, ge=0, description="起始字符位置")
    char_end: Optional[int] = Field(default=None, gt=0, description="结束字符位置")
    page_no: Optional[int] = Field(default=None, ge=1, description="页码")

    # ---------------------- 判定字段（仅规则引擎可写） ----------------------
    risk_level: str = Field(description="风险等级，仅规则引擎可写")
    decision: str = Field(default="", description="处理决策，仅规则引擎可写")
    severity: Optional[str] = Field(
        default=None, description="规则严重度，仅规则引擎可写"
    )
    judge_source: str = Field(
        default=RULE_ENGINE_SOURCE,
        description="判定来源，固定为 rule_engine，不接受其它值",
    )

    # ---------------------- 参考内容 ----------------------
    suggestion_text: str = Field(default="", description="修改建议")
    legal_basis: str = Field(default="", description="法律依据")
    reference_only: bool = Field(
        default=False,
        description="是否为纯参考项（无规则判定支撑，仅供人工参考，不计入定级）",
    )

    # ---------------------- 多路召回溯源 ----------------------
    matched_sources: List[str] = Field(
        default_factory=list,
        description="命中的召回来源列表，如 ['rule_engine','vec_candidate']",
    )
    conflict_flag: bool = Field(
        default=False, description="是否存在多路召回冲突（已在融合阶段按规则优先原则裁决）"
    )
    resolution_note: str = Field(default="", description="冲突裁决说明，保证可追溯")

    @field_validator("risk_level")
    @classmethod
    def validate_risk_level(cls, value: str) -> str:
        """
        校验风险等级合法性

        参数：
            value: 风险等级字符串
        返回：
            校验通过的等级值
        异常：
            ValueError：取值不在 RiskLevel 枚举内时抛出
        """
        allowed = {item.value for item in RiskLevel}
        if value not in allowed:
            raise ValueError(f"风险等级非法：{value}，允许值：{sorted(allowed)}")
        return value

    @field_validator("judge_source")
    @classmethod
    def validate_judge_source(cls, value: str) -> str:
        """
        校验判定来源必须为规则引擎

        参数：
            value: 判定来源字符串
        返回：
            固定值 rule_engine
        异常：
            ValueError：传入非 rule_engine 的值时抛出
        """
        if value != RULE_ENGINE_SOURCE:
            raise ValueError(
                f"风险项判定来源只允许为 '{RULE_ENGINE_SOURCE}'，当前传入 '{value}'。"
                f"大模型与向量召回均无权产出正式风险项。"
            )
        return value

    @property
    def risk_order(self) -> int:
        """
        风险等级数值序位，供排序使用

        返回：
            等级序位（low=1, medium=2, high=3, critical=4）
        """
        return RISK_LEVEL_ORDER.get(self.risk_level, 0)


class BlockedInfo(BaseModel):
    """
    阻塞信息 — 任务被置为阻塞时的完整上下文

    说明：
        任何阻塞都必须给出明确原因码，禁止出现"未知原因阻塞"。
        原因码取值见 models/documents/review_task.py 的 BlockedReason。

    字段：
        reason: 阻塞原因码，取值见 BlockedReason
        detail: 阻塞详情，供人工定位处理
        blocked_at: 阻塞发生时间
        blocked_node: 触发阻塞的节点名
        retry_count: 阻塞时该节点的已重试次数
        retriable: 是否可由人工触发重跑
    """

    reason: str = Field(description="阻塞原因码")
    detail: str = Field(default="", description="阻塞详情")
    blocked_at: Optional[datetime] = Field(default=None, description="阻塞时间")
    blocked_node: str = Field(default="", description="触发阻塞的节点")
    retry_count: int = Field(default=0, ge=0, description="阻塞时已重试次数")
    retriable: bool = Field(default=True, description="是否支持人工重跑")

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        """
        校验阻塞原因码合法性

        参数：
            value: 原因码字符串
        返回：
            校验通过的原因码
        异常：
            ValueError：取值不在 BlockedReason 枚举内时抛出
        """
        allowed = {item.value for item in BlockedReason}
        if value not in allowed:
            raise ValueError(
                f"阻塞原因码非法：{value}，允许值：{sorted(allowed)}。"
                f"禁止出现未知原因阻塞。"
            )
        return value


class WriteBackInfo(BaseModel):
    """
    回写信息 — 审批评论区回写的结果载体

    【红线约束】
        回写内容只允许为评论区文本，接口路径必须通过
        app.utils.approval_guard.ensure_comment_only 校验。
        严禁承载任何改变审批结论的操作结果。

    字段：
        status: 回写状态，取值见 WriteBackState
        comment_payload: 提交给审批系统的评论内容字典
        comment_text: 评论正文文本
        api_path: 实际调用的接口路径（必须命中白名单）
        response_body: 审批系统返回的原始响应体
        response_code: 审批系统返回的业务码
        http_status: HTTP 状态码
        error: 失败时的错误摘要
        written_at: 回写完成时间
        retry_count: 回写重试次数
    """

    status: str = Field(
        default=WriteBackState.NOT_WRITTEN.value, description="回写状态"
    )
    comment_payload: Dict[str, Any] = Field(
        default_factory=dict, description="提交给审批系统的评论内容"
    )
    comment_text: str = Field(default="", description="评论正文文本")
    api_path: str = Field(default="", description="实际调用的接口路径")
    response_body: Dict[str, Any] = Field(
        default_factory=dict, description="审批系统返回的原始响应体"
    )
    response_code: Optional[int] = Field(default=None, description="审批系统业务码")
    http_status: Optional[int] = Field(default=None, description="HTTP 状态码")
    error: str = Field(default="", description="失败错误摘要")
    written_at: Optional[datetime] = Field(default=None, description="回写完成时间")
    retry_count: int = Field(default=0, ge=0, description="回写重试次数")

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        """
        校验回写状态合法性

        参数：
            value: 状态字符串
        返回：
            校验通过的状态值
        异常：
            ValueError：取值不在允许集合内时抛出
        """
        allowed = {item.value for item in WriteBackState}
        if value not in allowed:
            raise ValueError(f"回写状态非法：{value}，允许值：{sorted(allowed)}")
        return value


class AuditLogEntry(BaseModel):
    """
    审计日志条目 — 累加型，每个节点操作自动追加

    说明：
        本结构是"全流程可追溯"的落点。任何节点进入、退出、重试、
        异常、状态流转、红线拦截、阻塞与完成都必须留下一条记录。
        日志列表使用 operator.add 归约，保证并行分支的日志不会互相覆盖。

    字段：
        sequence: 全局单调序号（由节点内自增计数器生成）
        node: 产生该日志的节点名
        action: 动作类型，取值见 AuditAction
        message: 人类可读的操作描述
        detail: 结构化详情，便于程序化检索
        elapsed_ms: 该次操作耗时（毫秒）
        timestamp: 记录时间
    """

    sequence: int = Field(default=0, ge=0, description="全局单调序号")
    node: str = Field(description="节点名")
    action: str = Field(description="动作类型")
    message: str = Field(default="", description="人类可读描述")
    detail: Dict[str, Any] = Field(default_factory=dict, description="结构化详情")
    elapsed_ms: Optional[float] = Field(default=None, description="耗时毫秒")
    timestamp: datetime = Field(default_factory=datetime.now, description="记录时间")

    @field_validator("action")
    @classmethod
    def validate_action(cls, value: str) -> str:
        """
        校验动作类型合法性

        参数：
            value: 动作字符串
        返回：
            校验通过的动作值
        异常：
            ValueError：取值不在 AuditAction 枚举内时抛出
        """
        allowed = {item.value for item in AuditAction}
        if value not in allowed:
            raise ValueError(f"审计动作非法：{value}，允许值：{sorted(allowed)}")
        return value


class NodeExecutionRecord(BaseModel):
    """
    节点执行记录 — 记录单个节点的一次执行（含重试）

    说明：
        与 AuditLogEntry 的区别：审计日志记录"发生了什么"，
        本结构记录"这个节点跑成什么样"，供前端进度条与重跑定位使用。

    字段：
        node: 节点名
        status: 执行状态 pending / running / success / failed / skipped / blocked
        attempt: 第几次尝试（从 1 开始）
        started_at / finished_at: 起止时间
        elapsed_ms: 耗时毫秒
        retry_count: 该节点累计重试次数
        error: 错误摘要
    """

    node: str = Field(description="节点名")
    status: str = Field(default="pending", description="执行状态")
    attempt: int = Field(default=1, ge=1, description="第几次尝试")
    started_at: Optional[datetime] = Field(default=None, description="开始时间")
    finished_at: Optional[datetime] = Field(default=None, description="结束时间")
    elapsed_ms: Optional[float] = Field(default=None, description="耗时毫秒")
    retry_count: int = Field(default=0, ge=0, description="累计重试次数")
    error: str = Field(default="", description="错误摘要")


# ======================================================================
# 三、LangGraph 主状态对象
# ======================================================================
class ReviewTaskState(BaseModel):
    """
    合同审查流程状态 — LangGraph 图状态契约

    【字段分组说明】
        A. 任务基础信息：流程启动时注入，全程只读
        B. 文档解析状态：由 parse_document 节点写入
        C. 风险审查状态：由 rule_review / vector_review / llm_review / risk_merge 写入
        D. 回写状态：由 generate_comment / write_comment 写入
        E. 审计日志：所有节点追加（累加型）

    【架构红线】
        - risk_level（综合风险等级）只能由规则引擎判定后写入
        - is_blocked（是否阻塞）只能由规则引擎判定后写入
        - llm_candidates 为"候选"语义，不含任何等级字段
    """

    # ==================================================================
    # A. 任务基础信息（流程启动时注入，全程只读）
    # ==================================================================
    task_id: str = Field(description="审查任务 ID，全局唯一，用于会话与目录隔离")
    task_no: str = Field(description="审查任务编号，对外展示")
    thread_id: str = Field(default="", description="LangGraph 会话 ID，断点续跑依据")
    work_dir: str = Field(default="", description="任务独立工作目录，文件隔离依据")

    approval_id: str = Field(default="", description="企业审批系统中的审批单 ID")
    contract_id: str = Field(default="", description="合同主档 ID")

    # ---------------------- 合同核心字段（路由决策依据） ----------------------
    contract_title: str = Field(default="", description="合同标题")
    contract_no: str = Field(default="", description="合同编号")
    submit_dept: str = Field(default="", description="送审部门")
    applicant: str = Field(default="", description="申请人")
    counterparty: str = Field(default="", description="合同相对方")
    contract_amount: float = Field(
        default=0.0, ge=0.0, description="合同金额，路由深度复核依据"
    )
    currency: str = Field(default="CNY", description="币种，默认人民币")
    performance_period: str = Field(
        default="", description="履约周期描述，如'2026-01-01 至 2026-12-31'"
    )
    business_type: str = Field(default="", description="业务类型")
    contract_type: str = Field(default="other", description="合同类型")
    template_flag: bool = Field(
        default=False, description="是否模板合同，路由快速通道依据"
    )
    source_type: str = Field(
        default="digital", description="合同来源类型 digital / scanned"
    )
    file_object_key: str = Field(
        default="", description="合同原文在 MinIO 中的对象键"
    )

    # ---------------------- 流程控制 ----------------------
    route_type: str = Field(
        default=RouteType.STANDARD.value,
        description="差异化路由类型 fast / standard / deep",
    )
    status: str = Field(
        default=ReviewTaskStatus.PENDING.value,
        description="任务状态，取值见 ReviewTaskStatus 枚举",
    )
    progress: int = Field(default=0, ge=0, le=100, description="进度百分比")
    current_node: str = Field(default="", description="当前执行节点名")

    # ==================================================================
    # B. 文档解析状态（由 parse_document 节点写入）
    # ==================================================================
    parse_status: str = Field(
        default=ParseStatus.NOT_STARTED.value, description="文档解析状态"
    )
    text_blocks: List[TextBlock] = Field(
        default_factory=list, description="文本块列表，每块记录 page/start_idx/end_idx/段落索引"
    )
    paragraphs: List[ParagraphNode] = Field(
        default_factory=list, description="段落树，还原合同的层级结构"
    )
    full_text: str = Field(default="", description="抽取的合同全文，条款切分输入")
    clauses: List[ClauseItem] = Field(
        default_factory=list, description="条款结构化结果"
    )
    doc_metadata: DocMetadata = Field(
        default_factory=DocMetadata, description="文档元数据"
    )
    parse_error: str = Field(default="", description="解析错误信息列表（换行分隔）")
    parse_retry_count: int = Field(default=0, ge=0, description="解析重试次数")
    file_fingerprint: str = Field(
        default="", description="文件指纹，断点续跑时校验文件一致性"
    )
    # 文档级未结构化补充内容（如无明确条款边界的说明段），务必保留以便可追溯
    unstructured_text: str = Field(
        default="", description="未能归入任何条款的正文残片，供人工核查"
    )

    # ==================================================================
    # C. 风险审查状态
    # ==================================================================
    # ---------------------- 规则引擎结果（唯一判定来源） ----------------------
    rule_results: List[RiskItem] = Field(
        default_factory=list,
        description="规则引擎命中结果，携带风险等级与处理决策（唯一有权定级的来源）",
    )
    rule_hit_codes: List[str] = Field(
        default_factory=list, description="规则引擎命中的规则编码列表"
    )
    rule_engine_version: str = Field(
        default="", description="参与本次判定的规则引擎版本，保证可追溯"
    )
    rule_evaluated: bool = Field(
        default=False, description="规则引擎是否已完成判定（未判定不得放行）"
    )

    # ---------------------- 向量召回结果（候选语义，无等级） ----------------------
    vector_results: List[VectorCandidate] = Field(
        default_factory=list, description="向量语义召回候选，不含风险等级"
    )
    vector_recall_count: int = Field(
        default=0, ge=0, description="向量召回候选总数（去重前）"
    )

    # ---------------------- 大模型结果（候选语义，无等级） ----------------------
    llm_results: List[LLMCandidate] = Field(
        default_factory=list,
        description="大模型疑似风险候选，数据结构上不含风险等级与处理决策",
    )
    llm_raw_summary: str = Field(default="", description="大模型输出摘要，仅供人工参考")
    llm_called: bool = Field(
        default=False, description="本次流程是否实际调用过大模型"
    )

    # ---------------------- 融合结果（最终风险项） ----------------------
    final_risk_items: List[RiskItem] = Field(
        default_factory=list, description="融合去重后的最终风险项列表"
    )
    # 【架构红线】综合风险等级只能由规则引擎写入
    risk_level: Optional[str] = Field(
        default=None,
        description=(
            "综合风险等级 high/medium/low/critical。"
            "【架构红线】该字段只能由规则引擎判定后写入，"
            "大模型与向量召回的结果不得影响本字段"
        ),
    )
    is_blocked: bool = Field(
        default=False,
        description="是否阻塞任务。【架构红线】只能由规则引擎依据配置判定",
    )
    blocked_info: Optional[BlockedInfo] = Field(
        default=None, description="阻塞上下文，仅阻塞时有值"
    )
    risk_distribution: Dict[str, int] = Field(
        default_factory=dict, description="各风险等级命中数量统计"
    )
    merge_summary: str = Field(default="", description="风险融合摘要，说明裁决过程")

    # ==================================================================
    # D. 报告与建议
    # ==================================================================
    conclusion_text: str = Field(
        default="", description="审查结论文本，由规则引擎模板拼装"
    )
    conclusion_hash: str = Field(
        default="", description="结论防篡改哈希（SHA-256，以 task_id 加盐）"
    )
    suggestion_text: str = Field(default="", description="修改建议汇总文本")
    quality_check_passed: bool = Field(
        default=False, description="生成质量校验是否通过"
    )
    quality_fix_rounds: int = Field(
        default=0, ge=0, description="质量校验失败后的自动修正轮次"
    )
    report_object_key: str = Field(
        default="", description="报告文件在 MinIO 中的对象键"
    )
    report_url: str = Field(default="", description="报告预签名访问链接")
    report_generated: bool = Field(default=False, description="报告是否已生成")

    # ==================================================================
    # E. 回写状态（由 generate_comment / write_comment 写入）
    # ==================================================================
    write_back_status: str = Field(
        default=WriteBackState.NOT_WRITTEN.value,
        description="回写状态：not_written / writing / success / failed",
    )
    write_back_content: WriteBackInfo = Field(
        default_factory=WriteBackInfo, description="回写内容与接口返回信息"
    )
    write_back_retry_count: int = Field(
        default=0, ge=0, description="回写重试次数"
    )

    # ==================================================================
    # F. 审计日志（累加型，每个节点操作自动追加）
    # 说明：使用 operator.add 归约器，保证并行分支的日志拼接而非覆盖
    # ==================================================================
    audit_logs: ListReducer = Field(
        default_factory=list,
        description="审计日志列表（累加型）。每个节点操作自动追加，不可覆盖",
    )
    audit_sequence: int = Field(
        default=0, ge=0, description="审计日志序号计数器，保证全局单调递增"
    )
    node_history: ListReducer = Field(
        default_factory=list,
        description="节点执行历史列表（累加型），支持单节点重试追溯",
    )

    # ==================================================================
    # G. 异常与错误
    # ==================================================================
    error_message: str = Field(default="", description="流程级错误摘要")
    retry_count: int = Field(default=0, ge=0, description="当前节点重试计数")
    max_retry: int = Field(default=3, ge=1, description="节点重试上限")
    exhausted_nodes: List[str] = Field(
        default_factory=list, description="重试耗尽的节点列表"
    )
    skipped_nodes: List[str] = Field(
        default_factory=list, description="因路由未执行的节点列表"
    )

    # ==================================================================
    # H. 时间戳
    # ==================================================================
    started_at: Optional[datetime] = Field(default=None, description="流程开始时间")
    finished_at: Optional[datetime] = Field(default=None, description="流程结束时间")
    created_at: datetime = Field(default_factory=datetime.now, description="状态创建时间")
    updated_at: datetime = Field(
        default_factory=datetime.now, description="状态最后更新时间"
    )

    # ==================================================================
    # 校验器
    # ==================================================================
    @field_validator("route_type")
    @classmethod
    def validate_route_type(cls, value: str) -> str:
        """
        校验路由类型合法性

        参数：
            value: 路由类型字符串
        返回：
            校验通过的路由类型
        异常：
            ValueError：取值不在 RouteType 枚举内时抛出
        """
        allowed = {item.value for item in RouteType}
        if value not in allowed:
            raise ValueError(f"路由类型非法：{value}，允许值：{sorted(allowed)}")
        return value

    @field_validator("risk_level")
    @classmethod
    def validate_risk_level(cls, value: Optional[str]) -> Optional[str]:
        """
        校验综合风险等级合法性

        参数：
            value: 风险等级字符串或 None
        返回：
            校验通过的等级值
        异常：
            ValueError：取值不在 RiskLevel 枚举内时抛出
        """
        if value is None:
            return value
        allowed = {item.value for item in RiskLevel}
        if value not in allowed:
            raise ValueError(f"综合风险等级非法：{value}，允许值：{sorted(allowed)}")
        return value

    @field_validator("write_back_status")
    @classmethod
    def validate_write_back_status(cls, value: str) -> str:
        """
        校验回写状态合法性

        参数：
            value: 回写状态字符串
        返回：
            校验通过的状态值
        异常：
            ValueError：取值不在 WriteBackState 枚举内时抛出
        """
        allowed = {item.value for item in WriteBackState}
        if value not in allowed:
            raise ValueError(f"回写状态非法：{value}，允许值：{sorted(allowed)}")
        return value

    # ==================================================================
    # 派生属性（供路由函数与节点使用）
    # ==================================================================
    @property
    def is_parse_ok(self) -> bool:
        """
        判断文档解析是否成功

        返回：
            True 表示解析成功且条款已结构化
        """
        return self.parse_status == ParseStatus.SUCCESS.value

    @property
    def is_parse_blocked(self) -> bool:
        """
        判断解析是否被阻断（不可自动重试的法定阻塞）

        返回：
            True 表示解析被阻断，需人工处理
        """
        return self.parse_status == ParseStatus.BLOCKED.value

    @property
    def is_route_fast(self) -> bool:
        """
        判断是否走快速通道

        返回：
            True 表示模板合同快速通道，仅跑结构化规则
        """
        return self.route_type == RouteType.FAST.value

    @property
    def is_route_deep(self) -> bool:
        """
        判断是否走深度复核通道

        返回：
            True 表示大额或高风险类型，需追加大模型审查
        """
        return self.route_type == RouteType.DEEP.value

    @property
    def needs_llm_review(self) -> bool:
        """
        判断是否需要执行大模型辅助审查

        说明：
            仅标准审查与深度复核通道需要大模型辅助。
            快速通道（模板合同）与解析失败分支均不调用大模型，
            以降低耗时与资源消耗。

        返回：
            True 表示应执行 llm_review 节点
        """
        if self.is_parse_blocked or not self.is_parse_ok:
            return False
        if self.is_blocked:
            return False
        return self.route_type in (RouteType.STANDARD.value, RouteType.DEEP.value)

    @property
    def needs_vector_review(self) -> bool:
        """
        判断是否需要执行向量语义召回

        说明：
            快速通道为模板合同，条款与标准库高度一致，语义召回无增量价值，
            故跳过以缩短链路；标准与深度通道均执行。

        返回：
            True 表示应执行 vector_review 节点
        """
        if self.is_parse_blocked or not self.is_parse_ok:
            return False
        return self.route_type in (RouteType.STANDARD.value, RouteType.DEEP.value)

    @property
    def needs_write_back(self) -> bool:
        """
        判断是否需要执行审批回写

        说明：
            阻塞任务不触发回写，避免向审批流写入可能被误读的内容。
            规则引擎判定完成后才允许回写。

        返回：
            True 表示应执行 write_comment 节点
        """
        return self.rule_evaluated and not self.is_blocked

    @property
    def total_risk_count(self) -> int:
        """
        最终风险项总数

        返回：
            final_risk_items 的长度
        """
        return len(self.final_risk_items)

    @property
    def official_risk_count(self) -> int:
        """
        有效判定风险项数量（有规则引擎支撑）

        返回：
            final_risk_items 中 reference_only 为 False 的项数
        """
        return sum(1 for item in self.final_risk_items if not item.reference_only)

    def append_audit(
        self,
        node: str,
        action: str,
        message: str = "",
        detail: Optional[Dict[str, Any]] = None,
        elapsed_ms: Optional[float] = None,
    ) -> AuditLogEntry:
        """
        构造一条审计日志（不修改自身，供节点返回增量使用）

        【重要设计说明】
            本方法刻意不直接修改 self.audit_logs。
            LangGraph 的状态更新遵循"节点返回增量字典"的契约，
            节点内部直接改 self 会被框架忽略，导致日志丢失。
            因此这里只负责"构造条目并递增序号"，由节点把结果写回增量字典。

        参数：
            node: 节点名
            action: 动作类型，取值见 AuditAction
            message: 人类可读描述
            detail: 结构化详情
            elapsed_ms: 耗时毫秒

        返回：
            构造好的 AuditLogEntry 实例

        异常：
            ValueError：action 取值非法时抛出
        """
        entry = AuditLogEntry(
            sequence=self.audit_sequence + 1,
            node=node,
            action=action,
            message=message,
            detail=detail or {},
            elapsed_ms=elapsed_ms,
        )
        return entry

    def model_dump_summary(self) -> Dict[str, Any]:
        """
        导出状态摘要（不含大文本字段）

        说明：
            用于日志输出与接口返回，剔除 full_text、unstructured_text 等
            可能达到数十万字符的字段，避免日志文件被迅速撑满。

        返回：
            精简后的状态字典
        """
        data = self.model_dump(
            exclude={
                "full_text",
                "unstructured_text",
                "text_blocks",
                "paragraphs",
                "llm_results",
                "vector_results",
                "rule_results",
                "final_risk_items",
                "audit_logs",
                "node_history",
            }
        )
        data["text_block_count"] = len(self.text_blocks)
        data["paragraph_count"] = len(self.paragraphs)
        data["clause_count"] = len(self.clauses)
        data["rule_hit_count"] = len(self.rule_results)
        data["vector_count"] = len(self.vector_results)
        data["llm_count"] = len(self.llm_results)
        data["final_risk_count"] = len(self.final_risk_items)
        data["audit_log_count"] = len(self.audit_logs)
        return data
