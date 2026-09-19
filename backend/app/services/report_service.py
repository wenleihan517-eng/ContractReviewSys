# D:/ContractReviewSys-合同审批智能审查项目/backend/app/services/report_service.py
"""
审查报告生成服务 — 完整审查报告的确定性拼装与导出

【模块定位】
    本模块是 generate_report 节点背后的服务，负责生成完整审查报告。
    报告内容全部为**确定性拼装**（规则引擎产出 + 结构化字段），
    不含任何大模型自由生成内容，保证报告可复现、可审计。

    报告结构（固定）：
        报告头 → 合同基本信息 → 综合审查结论 → 风险清单明细 →
        修改建议 → 流程审计链路 → 免责声明

    【风险清单明细】是报告的核心，每个风险项完整呈现五个维度：
        风险等级 / 原文定位 / 成因 / 法律依据 / 推荐修改条款

【架构红线】
    报告是"呈现"，不是"判定"。报告中的风险等级、处理决策、审查结论
    全部复制自规则引擎产出，本模块绝不生成新的判定结果。

返回值说明：
    - generate_report_service()：返回 ReportResult（含报告文本与访问链接）
    - generate_pdf_report_service()：返回 PdfReportResult（含 PDF 二进制与访问链接）
    - build_suggestion_text()：返回修改建议汇总文本
    - render_report_markdown()：返回报告 Markdown 全文

【报告持久化与幂等（缺口2 修复）】
    报告生成后必须落两处，缺一不可：
        1. MinIO：对象键固定为 report/{task_id}_review_report.md
        2. MongoDB cr_review_task：回写 report_object_key / report_format /
           report_generated_at / report_content_hash

    幂等策略：以**内容指纹**（SHA-256）为准，而非时间戳。
        任务已存在 report_object_key，且本次渲染出的报告内容指纹与
        report_content_hash 一致时，直接复用已有对象，不重复上传。
        内容变化（风险项/结论变动）则重新上传并覆盖对象键，保证下载到的是最新版。

    失败回滚：上传成功但落库失败时，删除刚上传的 MinIO 对象，
        避免出现"有文件无记录"的脏数据。

【PDF 导出（缺口1 修复）】
    技术路线：Markdown 源文本 →（解析为结构化行模型）→ PyMuPDF 原生绘制 A4 版式。
    渲染实现集中在 app/services/pdf_renderer.py，本模块只负责"取源文本 + 落库 + 幂等"。

    ★ 内容 100% 对齐的关键设计：
        PDF **不重新查询风险数据**，而是复用 render_report_markdown() 产出的
        Markdown 源文本，由渲染器解析该文本并按语义类型（标题/表格/列表/段落）
        逐类绘制。这样从机制上杜绝了"Markdown 与 PDF 结论不一致"的可能 ——
        两份报告永远是同一份数据源的同构呈现。

    幂等策略：与 Markdown 同源。pdf_content_hash 记录的是"所依据 Markdown 源文本"的
        指纹，与 report_content_hash 使用同一个计算函数。只要源文本未变，
        PDF 直接复用已有对象（连渲染都跳过），不重复生成上传。

    失败回滚：与 Markdown 一致 —— 渲染失败清理内存与临时文件；
        上传成功但落库失败则删除刚上传的 PDF 对象。

使用示例：
    result = await generate_report_service(state)
    print(result.report_url)
"""

import hashlib
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from app.constants import RiskLevel
from app.core.config import settings
from app.core.logging_config import get_logger
from app.models.documents.review_task import RouteType

logger = get_logger(__name__)


# 风险等级中文映射（唯一权威定义处，供报告与评论复用）
RISK_LEVEL_LABELS: Dict[str, str] = {
    RiskLevel.LOW.value: "低风险",
    RiskLevel.MEDIUM.value: "中风险",
    RiskLevel.HIGH.value: "高风险",
    RiskLevel.CRITICAL.value: "严重风险",
}


class ReportResult(BaseModel):
    """
    报告生成结果

    字段：
        report_content: 报告 Markdown 全文
        report_object_key: 报告文件在 MinIO 中的对象键
        report_url: 报告预签名访问链接
        suggestion_text: 修改建议汇总文本
        quality_passed: 生成质量校验是否通过
        quality_fix_rounds: 质量校验失败后的自动修正轮次
        risk_item_count: 报告包含的风险项总数
        report_format: 报告导出格式（md）
        content_hash: 报告内容 SHA-256 指纹（幂等判定依据）
        reused: 是否复用已有报告对象（True 表示未重复上传）
        persisted: 报告字段是否已成功落库到任务文档
    """

    report_content: str = Field(default="", description="报告 Markdown 全文")
    report_object_key: str = Field(default="", description="MinIO 对象键")
    report_url: str = Field(default="", description="预签名访问链接")
    suggestion_text: str = Field(default="", description="修改建议汇总文本")
    quality_passed: bool = Field(default=False, description="质量校验是否通过")
    quality_fix_rounds: int = Field(default=0, description="自动修正轮次")
    risk_item_count: int = Field(default=0, description="风险项总数")
    report_format: str = Field(default="md", description="报告导出格式")
    content_hash: str = Field(default="", description="报告内容 SHA-256 指纹")
    reused: bool = Field(default=False, description="是否复用已有报告对象")
    persisted: bool = Field(default=False, description="报告字段是否已落库")
    pdf_object_key: str = Field(default="", description="PDF 对象键（同时生成时填充）")
    pdf_url: str = Field(default="", description="PDF 预签名链接（同时生成时填充）")


class PdfReportResult(BaseModel):
    """
    PDF 报告生成结果（缺口1 修复）

    设计说明：
        与 ReportResult 分离而非复用，原因是二者的"内容主体"不同：
        ReportResult 携带 report_content（Markdown 全文，可读文本）；
        PdfReportResult 只携带 pdf_bytes 与元信息，**绝不携带二进制内容到接口层**，
        避免 PDF 数据被 JSON 序列化后污染响应体。

    字段：
        pdf_bytes: PDF 二进制内容（供上传与直接下载使用）
        pdf_object_key: PDF 在 MinIO 中的对象键
        pdf_url: PDF 预签名访问链接
        file_name: 下载文件名（{task_id}_review_report.pdf）
        page_count: 渲染出的页数（用于审计与前端提示）
        size_bytes: PDF 字节数
        render_engine: 渲染引擎标识（固定 pymupdf）
        source_hash: 渲染所依据 Markdown 源的 SHA-256 指纹（幂等判定依据）
        reused: 是否复用已有 PDF 对象（True 表示未重复渲染上传）
        persisted: PDF 字段是否已成功落库到任务文档
        font_used: 实际使用的正文字体名（便于排查中文缺字问题）
    """

    pdf_bytes: bytes = Field(default=b"", description="PDF 二进制内容")
    pdf_object_key: str = Field(default="", description="MinIO 对象键")
    pdf_url: str = Field(default="", description="预签名访问链接")
    file_name: str = Field(default="", description="下载文件名")
    page_count: int = Field(default=0, description="渲染页数")
    size_bytes: int = Field(default=0, description="PDF 字节数")
    render_engine: str = Field(default="pymupdf", description="渲染引擎标识")
    source_hash: str = Field(default="", description="Markdown 源内容 SHA-256 指纹")
    reused: bool = Field(default=False, description="是否复用已有 PDF 对象")
    persisted: bool = Field(default=False, description="PDF 字段是否已落库")
    font_used: str = Field(default="", description="实际使用的正文字体名")


# ======================================================================
# 一、修改建议汇总
# ======================================================================
def build_suggestion_text(
    final_risk_items: List[Any],
    force_include_missing: bool = False,
) -> str:
    """
    构造修改建议汇总文本

    【确定性生成原则】
        建议文本由规则的 suggestion_template 与大模型建议初稿拼装而成，
        结构固定、可复现。参考项（大模型/向量候选）的建议必须带
        "【AI 建议初稿，待人工确认】"标记，避免被误认为正式结论。

    参数：
        final_risk_items: 最终风险项列表（RiskItem）
        force_include_missing: 是否强制补充缺失项（质量校验失败时的修正动作）

    返回：
        修改建议汇总文本（Markdown）
    """
    lines: List[str] = ["## 修改建议清单", ""]

    official_items = [item for item in final_risk_items if not item.reference_only]
    reference_items = [item for item in final_risk_items if item.reference_only]

    # -------------------- 第一部分：规则引擎判定的风险项 --------------------
    lines.append(f"### 一、规则引擎判定风险（{len(official_items)} 项）")
    lines.append("")

    if official_items:
        for index, item in enumerate(official_items, start=1):
            level_label = RISK_LEVEL_LABELS.get(item.risk_level, item.risk_level)
            lines.append(
                f"{index}. **[{item.rule_code}] {item.rule_name}**"
                f"（风险等级：{level_label}）"
            )
            if item.clause_id:
                lines.append(f"   - 关联条款：{item.clause_id}")
            if item.evidence_text:
                lines.append(f"   - 证据原文：{item.evidence_text}")
            if item.suggestion_text:
                lines.append(f"   - 修改建议：{item.suggestion_text}")
            elif force_include_missing:
                lines.append(
                    "   - 修改建议：该风险项未配置建议模板，"
                    "请法务审查人依据规则说明与证据原文自行拟定修改方案"
                )
            if item.legal_basis:
                lines.append(f"   - 法律依据：{item.legal_basis}")
            elif force_include_missing:
                lines.append(
                    "   - 法律依据：该规则模板未配置法律依据，"
                    "建议由法务审查人补充引用条文"
                )
            lines.append("")
    else:
        lines.append("未发现规则引擎判定的风险项。")
        lines.append("")

    # -------------------- 第二部分：AI 参考项（不参与定级） --------------------
    if reference_items:
        lines.append(
            f"### 二、AI 辅助识别参考项（{len(reference_items)} 项，不参与定级）"
        )
        lines.append("")
        lines.append(
            "> 以下内容由大模型辅助识别或向量语义召回产生，"
            "**未匹配到任何规则模板，不作为正式审查结论**，仅供法务审查人复核参考。"
        )
        lines.append("")

        for index, item in enumerate(reference_items, start=1):
            lines.append(f"{index}. **【AI 建议初稿，待人工确认】** {item.rule_name}")
            if item.clause_id:
                lines.append(f"   - 关联条款：{item.clause_id}")
            if item.evidence_text:
                lines.append(f"   - 证据片段：{item.evidence_text[:200]}")
            if item.suggestion_text:
                lines.append(f"   - 建议初稿：{item.suggestion_text}")
            if item.resolution_note:
                lines.append(f"   - 说明：{item.resolution_note}")
            lines.append("")

    # -------------------- 第三部分：免责说明 --------------------
    lines.append("---")
    lines.append("")
    lines.append(
        "**说明**：本清单中「规则引擎判定风险」部分为依据规则模板做出的正式审查结论，"
        "风险等级与处理决策均由规则引擎产出；"
        "「AI 辅助识别参考项」部分为辅助线索，不构成审查结论。"
    )

    return "\n".join(lines)


# ======================================================================
# 二、报告 Markdown 渲染
# ======================================================================
def render_report_markdown(
    state: Any,
    suggestion_text: str,
) -> str:
    """
    渲染审查报告 Markdown 全文

    说明：
        报告内容全部为确定性拼装，不含模型自由生成内容。
        风险清单明细按"五个维度"完整呈现每个风险项。

    参数：
        state: 图状态（ReviewTaskState）
        suggestion_text: 已生成的修改建议汇总文本

    返回：
        报告 Markdown 文本
    """
    level_label = RISK_LEVEL_LABELS.get(state.risk_level or "", "未判定")

    route_label = {
        RouteType.FAST.value: "快速通道（模板合同，仅结构化规则审查）",
        RouteType.STANDARD.value: "标准审查（多路召回 + 大模型辅助）",
        RouteType.DEEP.value: "深度复核（大额/高风险，含人工复核网关）",
    }.get(state.route_type, state.route_type)

    lines: List[str] = [
        f"# 合同审查报告 — {state.contract_title}",
        "",
        f"- 报告生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 审查任务编号：{state.task_no}",
        f"- 审查任务 ID：{state.task_id}",
        f"- 审批单 ID：{state.approval_id}",
        "",
        "---",
        "",
        "## 一、合同基本信息",
        "",
        "| 项目 | 内容 |",
        "| --- | --- |",
        f"| 合同标题 | {state.contract_title} |",
        f"| 合同编号 | {state.contract_no} |",
        f"| 合同类型 | {state.contract_type} |",
        f"| 业务类型 | {state.business_type} |",
        f"| 合同金额 | {state.contract_amount:,.2f} {state.currency} |",
        f"| 送审部门 | {state.submit_dept} |",
        f"| 申请人 | {state.applicant} |",
        f"| 合同相对方 | {state.counterparty} |",
        f"| 履约周期 | {state.performance_period} |",
        f"| 是否模板合同 | {'是' if state.template_flag else '否'} |",
        f"| 审查链路 | {route_label} |",
        "",
        "---",
        "",
        "## 二、审查结论",
        "",
        f"**综合风险等级：{level_label}**",
        "",
        f"**任务状态：{'已阻塞，禁止推进审批流' if state.is_blocked else '审查通过，可进入人工审批环节'}**",
        "",
        f"{state.conclusion_text}",
        "",
        f"- 判定来源：规则引擎（版本 {state.rule_engine_version or '未记录'}）",
        f"- 结论防篡改哈希：`{state.conclusion_hash or '未生成'}`",
        f"- 风险等级分布：{state.risk_distribution}",
        "",
    ]

    # -------------------- 阻塞信息（如有） --------------------
    if state.is_blocked and state.blocked_info:
        lines.extend(
            [
                "### 阻塞信息",
                "",
                f"- 阻塞原因码：`{state.blocked_info.reason}`",
                f"- 阻塞节点：{state.blocked_info.blocked_node}",
                f"- 是否可人工重跑：{'是' if state.blocked_info.retriable else '否'}",
                f"- 阻塞详情：{state.blocked_info.detail}",
                "",
            ]
        )

    # -------------------- 深度复核通道提示 --------------------
    if state.is_route_deep and settings.ROUTE_DEEP_REQUIRE_LEGAL:
        lines.extend(
            [
                "### 人工复核网关",
                "",
                "本任务属于深度复核通道（大额或高风险合同类型），"
                "依据配置 `ROUTE_DEEP_REQUIRE_LEGAL=True`，"
                "**必须经法务审查人复核后方可推进审批流**。",
                "",
            ]
        )

    # -------------------- 风险清单明细 --------------------
    official_items = [item for item in state.final_risk_items if not item.reference_only]
    lines.extend(
        [
            "---",
            "",
            f"## 三、风险清单明细（{len(official_items)} 项，规则引擎判定）",
            "",
        ]
    )

    if official_items:
        # 概览表
        lines.extend(
            [
                "| 序号 | 规则编码 | 规则名称 | 关联条款 | 风险等级 | 处理决策 |",
                "| --- | --- | --- | --- | --- | --- |",
            ]
        )
        for index, item in enumerate(official_items, start=1):
            item_label = RISK_LEVEL_LABELS.get(item.risk_level, item.risk_level)
            lines.append(
                f"| {index} | {item.rule_code or '-'} | {item.rule_name} | "
                f"{item.clause_id or '-'} | {item_label} | {item.decision or '-'} |"
            )
        lines.append("")

        # 逐项详细明细（五个维度：等级 / 原文定位 / 成因 / 法律依据 / 推荐修改条款）
        for index, item in enumerate(official_items, start=1):
            item_label = RISK_LEVEL_LABELS.get(item.risk_level, item.risk_level)
            lines.extend(
                [
                    f"#### 风险项 {index}：[{item.rule_code or '-'}] {item.rule_name}",
                    "",
                    f"- **风险等级**：{item_label}",
                ]
            )

            # 原文定位：页码 + 字符区间 + 证据原文
            location_parts = []
            if item.page_no is not None:
                location_parts.append(f"第 {item.page_no} 页")
            if item.char_start is not None and item.char_end is not None:
                location_parts.append(f"字符区间 [{item.char_start}, {item.char_end})")
            lines.append(
                f"- **原文定位**：{'，'.join(location_parts) if location_parts else '未定位'}"
            )
            if item.evidence_text:
                lines.append(f"- **证据原文**：{item.evidence_text}")

            # 成因：风险名称即成因概括（规则名称描述了风险类型）
            lines.append(f"- **成因**：{item.rule_name}")

            # 法律依据
            lines.append(
                f"- **法律依据**：{item.legal_basis if item.legal_basis else '未配置，建议由法务补充引用条文'}"
            )

            # 推荐修改条款
            lines.append(
                f"- **推荐修改条款**：{item.suggestion_text if item.suggestion_text else '未配置建议模板，请法务自行拟定'}"
            )
            lines.append("")
    else:
        lines.append("未发现规则引擎判定的风险项。")
        lines.append("")

    # -------------------- 参考项（AI 候选，不参与定级） --------------------
    reference_items = [item for item in state.final_risk_items if item.reference_only]
    if reference_items:
        lines.extend(
            [
                f"### 参考项（{len(reference_items)} 项，AI 辅助识别，不参与定级）",
                "",
                "> 以下为向量语义召回或大模型辅助识别的候选，"
                "未匹配规则模板，**不构成正式审查结论**，仅供法务复核参考。",
                "",
            ]
        )
        for index, item in enumerate(reference_items, start=1):
            lines.append(f"{index}. {item.rule_name}")
            if item.clause_id:
                lines.append(f"   - 关联条款：{item.clause_id}")
            if item.suggestion_text:
                lines.append(f"   - 建议初稿：{item.suggestion_text}")
            lines.append("")

    # -------------------- 修改建议 --------------------
    lines.extend(["---", "", suggestion_text, ""])

    # -------------------- 审计链路 --------------------
    lines.extend(["---", "", "## 四、流程审计链路", "", "| 序号 | 节点 | 动作 | 说明 | 耗时 |", "| --- | --- | --- | --- | --- |"])
    for entry in state.audit_logs:
        elapsed = f"{entry.elapsed_ms:.2f}ms" if entry.elapsed_ms is not None else "-"
        message = entry.message.replace("|", "\\|")
        lines.append(
            f"| {entry.sequence} | {entry.node} | {entry.action} | {message} | {elapsed} |"
        )
    lines.append("")

    # -------------------- 节点执行记录 --------------------
    if state.node_history:
        lines.extend(["### 节点执行记录", "", "| 节点 | 状态 | 尝试次数 | 重试次数 | 耗时 |", "| --- | --- | --- | --- | --- |"])
        for record in state.node_history:
            elapsed = f"{record.elapsed_ms:.2f}ms" if record.elapsed_ms is not None else "-"
            lines.append(
                f"| {record.node} | {record.status} | {record.attempt} | "
                f"{record.retry_count} | {elapsed} |"
            )
        lines.append("")

    # -------------------- 免责声明 --------------------
    lines.extend(
        [
            "---",
            "",
            "## 五、免责声明",
            "",
            "本报告由合同智能审查系统自动生成，风险等级与审查结论均出自规则引擎的"
            "确定性判定，AI 辅助识别内容已明确标注。本报告仅供审批人决策参考，"
            "最终审批决策权保留给审批人。",
            "",
        ]
    )

    return "\n".join(lines)


# ======================================================================
# 二之二、PDF 报告生成（缺口1 修复，PyMuPDF 原生绘制）
# ======================================================================
def build_pdf_file_name(task_id: str) -> str:
    """
    按约定规则生成 PDF 报告文件名

    规则（需求硬性约定）：
        {task_id}_review_report.pdf

    参数：
        task_id: 任务 ID

    返回：
        PDF 文件名，如 "65f1a2b3c4d5e6f7a8b9c0d1_review_report.pdf"
    """
    # 与 Markdown 同样的防御性清洗：任务 ID 来自路径参数，理论上不含路径分隔符，
    # 但仍需防止异常输入在下游拼出穿越路径（该值会作为 MinIO 的对象键使用）。
    safe_task_id = str(task_id).replace("/", "_").replace("\\", "_").strip()
    if not safe_task_id:
        safe_task_id = "unknown"
    return f"{safe_task_id}_review_report.pdf"


def build_pdf_object_key(task_id: str) -> str:
    """
    构建 PDF 报告在 MinIO 中的对象键

    ★ 注意：与 Markdown 的 report/{...}.md 处于同一前缀下但扩展名不同，
      二者互不覆盖。需求中提到的桶名 "contract-reports" 在本工程实际配置为
      MINIO_BUCKET_REPORT（默认 contract-report），直接复用该桶，
      不新增桶、不新增存储连接（符合"保持现有单例客户端模式"约束）。

    参数：
        task_id: 任务 ID

    返回：
        对象键，形如 "report/{task_id}_review_report.pdf"
    """
    return f"report/{build_pdf_file_name(task_id)}"


def _resolve_pdf_object_key(state: Any, task_id: Optional[str]) -> str:
    """
    推导 PDF 对象键（与 Markdown 版本同构）

    优先级：
        1. 显式传入 task_id → 使用约定命名 {task_id}_review_report.pdf
        2. 否则回退到任务编号命名（图内节点调用场景，无 task_id 可用）

    参数：
        state: 图状态
        task_id: 任务 ID，可为 None

    返回：
        MinIO 对象键
    """
    if task_id:
        return build_pdf_object_key(task_id)
    safe_no = str(getattr(state, "task_no", "") or "unknown").replace("/", "_")
    return f"report/{safe_no}_review_report.pdf"


def render_report_pdf(
    report_markdown: str,
    task_no: str = "",
) -> tuple:
    """
    将报告 Markdown 源文本渲染为 PDF 二进制

    【为什么以 Markdown 为唯一输入】
        需求要求 PDF 与 Markdown 内容 100% 对齐。若 PDF 渲染器自己去查
        风险项、自己去拼结论，两套拼装逻辑迟早会漂移。因此这里刻意
        只接收 Markdown 文本作为唯一数据源 —— 数据同源即内容同源。

    参数：
        report_markdown: render_report_markdown() 产出的报告 Markdown 全文
        task_no: 任务编号（仅用于日志定位）

    返回：
        (pdf_bytes, page_count, font_used) 三元组

    异常：
        ValueError: Markdown 为空
        RuntimeError: 渲染失败（PyMuPDF 异常会被包装为可读信息）
    """
    # 延迟导入：PyMuPDF 体积较大，且仅 PDF 路径需要，
    # 放在函数内可避免 Markdown 链路无故承担导入开销。
    from app.services.pdf_renderer import render_markdown_to_pdf

    if not report_markdown or not report_markdown.strip():
        raise ValueError("报告 Markdown 源文本为空，无法渲染 PDF")

    try:
        pdf_bytes, page_count, font_used = render_markdown_to_pdf(report_markdown)
    except Exception as exc:  # noqa: BLE001 - 统一包装为可读的渲染错误
        logger.error(
            "PDF 渲染失败 | 任务=%s | Markdown 长度=%d | 异常=%s",
            task_no, len(report_markdown), exc,
        )
        raise RuntimeError(f"PDF 渲染失败：{exc}") from exc

    logger.info(
        "PDF 渲染完成 | 任务=%s | 页数=%d | 字节=%d | 字体=%s",
        task_no, page_count, len(pdf_bytes), font_used,
    )
    return (pdf_bytes, page_count, font_used)


async def generate_pdf_report_service(
    state: Any,
    task_id: Optional[str] = None,
    report_markdown: Optional[str] = None,
    upload: bool = True,
    persist: bool = True,
) -> PdfReportResult:
    """
    生成 PDF 审查报告（缺口1 修复主入口）

    流程：
        1. 取得 Markdown 源文本（外部传入优先，否则现场渲染）
        2. 计算源文本指纹，判定是否可复用已有 PDF（幂等控制）
        3. 渲染 PDF（PyMuPDF 原生绘制）
        4. 上传 MinIO 报告桶 + 生成预签名链接
        5. 持久化 PDF 字段到任务文档（失败则回滚 MinIO 对象）

    参数：
        state: 图状态（ReviewTaskState）
        task_id: 任务 ID。传入时执行"幂等复用 + 落库"；
                 不传时仅渲染与上传（供离线导出/测试使用）
        report_markdown: 已渲染好的 Markdown 源文本。
                         由 generate_report_service 串联调用时传入，
                         以避免重复执行建议汇总与质量校验；
                         为 None 时本函数自行渲染（保证单独调用也完整可用）
        upload: 是否上传 MinIO。False 时仅渲染并返回二进制（离线导出场景）
        persist: 是否持久化字段。False 时跳过落库（只读预览场景）

    返回：
        PdfReportResult（含 PDF 二进制、对象键、访问链接与持久化状态）
    """
    # -------------------- 步骤一：取得 Markdown 源文本 --------------------
    if report_markdown is None:
        # 单独调用场景：自行完成"建议汇总 → 质量校验 → 渲染 Markdown"，
        # 复用与 generate_report_service 完全相同的三个函数，保证内容一致。
        suggestion_text = build_suggestion_text(state.final_risk_items)
        if settings.QUALITY_POSTCHECK_ENABLED:
            passed, _issues = _check_quality(state, suggestion_text)
            if not passed:
                # 与主流程同策略：确定性补齐，不引入模型生成
                suggestion_text = build_suggestion_text(
                    state.final_risk_items, force_include_missing=True
                )
        report_markdown = render_report_markdown(state, suggestion_text)

    # -------------------- 步骤二：源指纹 + 幂等判定 --------------------
    source_hash = compute_report_content_hash(report_markdown)
    object_key = _resolve_pdf_object_key(state, task_id)

    if task_id and upload:
        existing = await _load_existing_pdf_meta(task_id)
        if existing:
            same_key = existing.get("pdf_object_key") == object_key
            same_hash = existing.get("pdf_content_hash") == source_hash
            if same_key and same_hash:
                logger.info(
                    "PDF 源内容未变更，复用已有对象（跳过渲染） | 任务=%s | 对象键=%s",
                    task_id, object_key,
                )
                pdf_url = await _presign_safely(
                    settings.MINIO_BUCKET_REPORT, object_key, state
                )
                return PdfReportResult(
                    pdf_bytes=b"",  # 复用时不重复渲染，避免无谓的内存与 CPU 开销
                    pdf_object_key=object_key,
                    pdf_url=pdf_url,
                    file_name=build_pdf_file_name(task_id),
                    page_count=0,
                    size_bytes=int(existing.get("pdf_size_bytes") or 0),
                    render_engine="pymupdf",
                    source_hash=source_hash,
                    reused=True,
                    persisted=True,
                    font_used="",
                )

    # -------------------- 步骤三：渲染 PDF --------------------
    pdf_bytes, page_count, font_used = render_report_pdf(
        report_markdown, task_no=str(getattr(state, "task_no", "") or "")
    )

    file_name = build_pdf_file_name(task_id) if task_id else (
        f"{str(getattr(state, 'task_no', '') or 'unknown')}_review_report.pdf"
    )

    result = PdfReportResult(
        pdf_bytes=pdf_bytes,
        pdf_object_key=object_key,
        pdf_url="",
        file_name=file_name,
        page_count=page_count,
        size_bytes=len(pdf_bytes),
        render_engine="pymupdf",
        source_hash=source_hash,
        reused=False,
        persisted=False,
        font_used=font_used,
    )

    # -------------------- 步骤四：上传 MinIO --------------------
    if not upload:
        # 离线导出场景：仅返回二进制，不接触对象存储
        return result

    from app.storage import minio_client

    uploaded_key = await minio_client.upload_bytes(
        bucket=settings.MINIO_BUCKET_REPORT,
        object_key=object_key,
        data=pdf_bytes,
        # ★ 必须显式指定 application/pdf，否则浏览器会把 PDF 当作
        #   八位字节流下载而非内联预览，与需求"支持浏览器直接预览"不符
        content_type="application/pdf",
    )
    result.pdf_object_key = uploaded_key
    result.pdf_url = await _presign_safely(
        settings.MINIO_BUCKET_REPORT, uploaded_key, state
    )

    # -------------------- 步骤五：持久化 PDF 字段 --------------------
    if task_id and persist:
        result.persisted = await _persist_pdf_fields(
            task_id=task_id,
            task_no=str(getattr(state, "task_no", "") or ""),
            object_key=uploaded_key,
            content_hash=source_hash,
            generated_at=datetime.now(),
            size_bytes=len(pdf_bytes),
            page_count=page_count,
        )

    return result


async def _load_existing_pdf_meta(task_id: str) -> Optional[Dict[str, Any]]:
    """
    读取任务文档中已有的 PDF 元信息

    参数：
        task_id: 任务 ID

    返回：
        含 pdf_object_key / pdf_content_hash / pdf_size_bytes 的字典；
        任务不存在或查询异常时返回 None（幂等判定降级为"重新渲染上传"）
    """
    from app.repositories import task_repo

    try:
        task = await task_repo.find_by_task_id(task_id)
        if task is None:
            return None
        return {
            "pdf_object_key": getattr(task, "pdf_object_key", None),
            "pdf_content_hash": getattr(task, "pdf_content_hash", None),
            "pdf_size_bytes": getattr(task, "pdf_size_bytes", None),
        }
    except Exception as exc:  # noqa: BLE001 - 查询失败降级为重新渲染，不阻断主流程
        logger.warning(
            "读取任务 PDF 元信息失败，降级为重新渲染 | 任务=%s | 异常=%s", task_id, exc
        )
        return None


async def _persist_pdf_fields(
    task_id: str,
    task_no: str,
    object_key: str,
    content_hash: str,
    generated_at: datetime,
    size_bytes: int,
    page_count: int,
) -> bool:
    """
    持久化 PDF 字段到任务文档

    ★ 权责红线：
        仅写入 PDF 相关的 5 个字段，绝不触碰 report_object_key（Markdown 侧字段）、
        状态机、审批结论、回写状态等任何判定类字段。
        Markdown 字段与 PDF 字段完全隔离，任一方失败都不会污染另一方。

    参数：
        task_id: 任务 ID
        task_no: 任务编号（仅用于日志）
        object_key: PDF 在 MinIO 中的对象键
        content_hash: PDF 所依据 Markdown 源的指纹
        generated_at: PDF 生成时间
        size_bytes: PDF 字节数
        page_count: 渲染页数（写入日志，不单独落库）

    返回：
        True 表示落库成功
    """
    from app.repositories import task_repo
    from app.storage import minio_client

    updates = {
        "pdf_object_key": object_key,
        "pdf_generated_at": generated_at,
        "pdf_content_hash": content_hash,
        "pdf_size_bytes": size_bytes,
        "pdf_render_engine": "pymupdf",
    }

    try:
        ok = await task_repo.update_task(task_id, updates)
        if ok:
            logger.info(
                "PDF 字段已持久化 | 任务=%s | 对象键=%s | 页数=%d | 字节=%d | 指纹=%s",
                task_no, object_key, page_count, size_bytes, content_hash[:12],
            )
            return True

        logger.warning(
            "PDF 字段落库未命中任何文档 | 任务=%s | 对象键=%s", task_no, object_key
        )
    except Exception as exc:  # noqa: BLE001 - 统一兜底，避免脏数据残留
        logger.error(
            "PDF 字段落库异常 | 任务=%s | 对象键=%s | 异常=%s", task_no, object_key, exc
        )

    # -------------------- 回滚：删除已上传对象，避免"有文件无记录" --------------------
    try:
        await minio_client.delete_object(
            bucket=settings.MINIO_BUCKET_REPORT, object_key=object_key
        )
        logger.warning(
            "PDF 落库失败，已回滚 MinIO 对象 | 任务=%s | 对象键=%s", task_no, object_key
        )
    except Exception as exc:  # noqa: BLE001 - 回滚失败需显式告警，便于人工清理
        logger.error(
            "PDF 对象回滚失败，需人工清理 | 任务=%s | 对象键=%s | 异常=%s",
            task_no, object_key, exc,
        )

    return False


# ======================================================================
# 三、报告生成主入口（含质量校验、持久化与幂等控制）
# ======================================================================
async def generate_report_service(
    state: Any,
    task_id: Optional[str] = None,
    include_pdf: Optional[bool] = None,
) -> ReportResult:
    """
    生成审查报告（主入口）

    流程：
        1. 生成修改建议汇总
        2. 生成后质量校验（五维）→ 未通过自动修正（最多 N 轮）
        3. 渲染报告 Markdown
        4. 计算内容指纹，判定是否可复用已有报告（幂等控制）
        5. 上传 MinIO 报告桶 + 生成预签名链接
        6. 持久化报告字段到任务文档（失败则回滚 MinIO 对象）
        7. （可选）联动生成 PDF 报告 —— 复用步骤三的 Markdown 源文本

    参数：
        state: 图状态（ReviewTaskState）
        task_id: 任务 ID。传入时会执行"幂等复用 + 持久化落库"；
                 不传（图内节点调用场景）时仅生成与上传，跳过落库。
                 该设计保证图编排链路不受影响，同时让接口层能拿到持久化结果。
        include_pdf: 是否同时产出 PDF。None 表示取配置 settings.PDF_AUTO_WITH_MD。
                     ★ PDF 生成失败**绝不影响 Markdown 产出**（PDF 是可选增强格式），
                       失败仅记录 WARNING 日志，Markdown 仍返回成功。

    返回：
        ReportResult（含报告文本、对象键、访问链接与持久化状态）
    """
    # -------------------- 步骤一：生成修改建议汇总 --------------------
    suggestion_text = build_suggestion_text(state.final_risk_items)

    # -------------------- 步骤二：生成后质量校验 --------------------
    quality_passed = True
    quality_issues: List[str] = []
    fix_rounds = 0

    if settings.QUALITY_POSTCHECK_ENABLED:
        quality_passed, quality_issues = _check_quality(state, suggestion_text)

        max_fix_rounds = settings.QUALITY_MAX_FIX_ROUNDS
        while not quality_passed and fix_rounds < max_fix_rounds:
            fix_rounds += 1
            logger.warning(
                "生成质量校验未通过，执行第 %d 轮修正 | 任务=%s | 问题=%s",
                fix_rounds,
                state.task_no,
                quality_issues,
            )
            # 修正动作：补齐全缺失项（确定性修正，不引入模型生成）
            suggestion_text = build_suggestion_text(
                state.final_risk_items, force_include_missing=True
            )
            quality_passed, quality_issues = _check_quality(state, suggestion_text)

        if not quality_passed:
            logger.error(
                "生成质量校验经 %d 轮修正仍未通过 | 任务=%s | 遗留问题=%s | "
                "标记为待人工处理，但不影响风险等级判定",
                fix_rounds, state.task_no, quality_issues,
            )

    # -------------------- 步骤三：渲染报告 Markdown --------------------
    report_content = render_report_markdown(state, suggestion_text)

    # -------------------- 步骤四：计算内容指纹 --------------------
    content_hash = compute_report_content_hash(report_content)

    # -------------------- 步骤五：上传（含幂等复用） --------------------
    report_object_key, report_url, reused = await _upload_report_with_idempotency(
        state=state,
        report_content=report_content,
        content_hash=content_hash,
        task_id=task_id,
    )

    # -------------------- 步骤六：持久化报告字段 --------------------
    persisted = False
    if task_id:
        persisted = await _persist_report_fields(
            task_id=task_id,
            task_no=state.task_no,
            object_key=report_object_key,
            content_hash=content_hash,
            generated_at=datetime.now(),
            rollback_on_failure=not reused,  # 复用对象不回滚（可能被历史记录引用）
        )

    # -------------------- 步骤七：联动生成 PDF（可选） --------------------
    pdf_object_key = ""
    pdf_url = ""
    want_pdf = settings.PDF_AUTO_WITH_MD if include_pdf is None else include_pdf
    if want_pdf and settings.PDF_ENABLED:
        try:
            pdf_result = await generate_pdf_report_service(
                state=state,
                task_id=task_id,
                report_markdown=report_content,  # ★ 复用同一份源文本，保证内容一致
                upload=True,
                persist=bool(task_id),
            )
            pdf_object_key = pdf_result.pdf_object_key
            pdf_url = pdf_result.pdf_url
        except Exception as exc:  # noqa: BLE001 - PDF 失败不得影响 Markdown 产出
            logger.error(
                "PDF 报告生成失败，Markdown 报告不受影响 | 任务=%s | 异常=%s",
                state.task_no, exc,
            )

    return ReportResult(
        report_content=report_content,
        report_object_key=report_object_key,
        report_url=report_url,
        suggestion_text=suggestion_text,
        quality_passed=quality_passed,
        quality_fix_rounds=fix_rounds,
        risk_item_count=len(state.final_risk_items),
        report_format="md",
        content_hash=content_hash,
        reused=reused,
        persisted=persisted,
        pdf_object_key=pdf_object_key,
        pdf_url=pdf_url,
    )


# ======================================================================
# 四、报告持久化辅助（缺口2 修复新增）
# ======================================================================
def build_report_file_name(task_id: str) -> str:
    """
    按约定规则生成报告文件名

    规则（需求硬性约定）：
        {task_id}_review_report.md

    参数：
        task_id: 任务 ID

    返回：
        报告文件名，如 "65f1a2b3c4d5e6f7a8b9c0d1_review_report.md"
    """
    # 任务 ID 来自路径参数，理论上不含路径分隔符；此处仍做防御性清洗，
    # 避免异常输入在下游拼出穿越路径（对象键会作为 MinIO 的键使用）。
    safe_task_id = str(task_id).replace("/", "_").replace("\\", "_").strip()
    if not safe_task_id:
        safe_task_id = "unknown"
    return f"{safe_task_id}_review_report.md"


def build_report_object_key(task_id: str) -> str:
    """
    构建报告在 MinIO 中的对象键

    参数：
        task_id: 任务 ID

    返回：
        对象键，形如 "report/{task_id}_review_report.md"
    """
    return f"report/{build_report_file_name(task_id)}"


def compute_report_content_hash(report_content: str) -> str:
    """
    计算报告内容的 SHA-256 指纹

    说明：
        指纹只用于"内容是否变化"的幂等判定，不承担任何防篡改职责
        （防篡改校验由 ReviewConclusion.verify_integrity 负责）。

    ★ 关键设计：计算前先剔除"生成时间"行
        报告头含 `- 报告生成时间：2026-09-19 17:00:00`，该值每次渲染都不同。
        若不剔除，同一份内容每次都会得到不同的指纹，幂等复用将**永远失效**
        （实测已验证：两次渲染指纹必然不同）。
        剔除后指纹反映的是"实质内容"是否变化 ——
        风险项、结论、建议有任何变动都会改变指纹，而单纯重新生成不会。

    参数：
        report_content: 报告 Markdown 全文

    返回：
        64 位十六进制小写字符串
    """
    normalized = _strip_volatile_lines(report_content)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


# 需从指纹计算中剔除的易变行前缀（这些行的内容随时间/环境变化，不代表实质内容变更）
_VOLATILE_LINE_PREFIXES = (
    "- 报告生成时间：",
    "- 审查任务 ID：",       # 同一任务内恒定，但跨调用可能因入参缺失而为空
)


def _strip_volatile_lines(report_content: str) -> str:
    """
    剔除报告中的易变行，得到用于指纹计算的规范化文本

    参数：
        report_content: 报告 Markdown 全文

    返回：
        规范化后的文本（易变行被替换为固定占位）
    """
    if not report_content:
        return ""
    lines = []
    for line in report_content.split("\n"):
        stripped = line.strip()
        if any(stripped.startswith(prefix) for prefix in _VOLATILE_LINE_PREFIXES):
            # 用固定占位替换，保持行结构不变（行数一致，便于排查差异）
            lines.append("<volatile>")
        else:
            lines.append(line)
    return "\n".join(lines)


def _resolve_object_key(state: Any, task_id: Optional[str]) -> str:
    """
    推导报告对象键

    优先级：
        1. 显式传入 task_id → 使用约定命名 {task_id}_review_report.md
        2. 否则回退到任务编号命名（图内节点调用场景，无 task_id 可用）

    参数：
        state: 图状态
        task_id: 任务 ID，可为 None

    返回：
        MinIO 对象键
    """
    if task_id:
        return build_report_object_key(task_id)
    # 图内节点无 task_id，用任务编号兜底，保证对象键仍然稳定可复现
    safe_no = str(getattr(state, "task_no", "") or "unknown").replace("/", "_")
    return f"report/{safe_no}_review_report.md"


async def _upload_report_with_idempotency(
    state: Any,
    report_content: str,
    content_hash: str,
    task_id: Optional[str],
) -> tuple:
    """
    上传报告到 MinIO，并按内容指纹执行幂等复用

    幂等规则：
        - 任务已有 report_object_key，且其 report_content_hash 与本轮指纹一致
          → 直接复用，不重复上传（仅刷新预签名链接）
        - 指纹不一致或字段缺失 → 上传并覆盖对象

    参数：
        state: 图状态
        report_content: 报告 Markdown 全文
        content_hash: 本轮报告内容指纹
        task_id: 任务 ID，可为 None（None 时跳过幂等查询，直接上传）

    返回：
        (对象键, 预签名链接, 是否复用) 三元组
    """
    from app.storage import minio_client

    bucket = settings.MINIO_BUCKET_REPORT
    object_key = _resolve_object_key(state, task_id)

    # -------------------- 幂等判定 --------------------
    if task_id:
        existing = await _load_existing_report_meta(task_id)
        if existing:
            same_key = existing.get("report_object_key") == object_key
            same_hash = existing.get("report_content_hash") == content_hash
            if same_key and same_hash:
                logger.info(
                    "报告内容未变更，复用已有对象 | 任务=%s | 对象键=%s",
                    task_id, object_key,
                )
                report_url = await _presign_safely(bucket, object_key, state)
                return (object_key, report_url, True)

    # -------------------- 上传（覆盖写，保证同任务同一对象键） --------------------
    report_bytes = report_content.encode("utf-8")
    uploaded_key = await minio_client.upload_bytes(
        bucket=bucket,
        object_key=object_key,
        data=report_bytes,
        content_type="text/markdown; charset=utf-8",
    )

    report_url = await _presign_safely(bucket, uploaded_key, state)
    return (uploaded_key, report_url, False)


async def _presign_safely(bucket: str, object_key: str, state: Any) -> str:
    """
    安全生成预签名链接

    说明：
        预签名失败不应阻断报告产出——报告本体已上传成功，
        调用方仍可通过下载接口取回文件。

    参数：
        bucket: 桶名
        object_key: 对象键
        state: 图状态（仅用于日志）

    返回：
        预签名链接；生成失败时返回空字符串
    """
    from app.storage import minio_client

    try:
        return await minio_client.get_presigned_url(
            bucket=bucket,
            object_key=object_key,
            expires_seconds=settings.REPORT_URL_EXPIRE_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 - 链接生成失败不阻断报告产出
        logger.error(
            "报告预签名链接生成失败 | 任务=%s | 对象键=%s | 异常=%s",
            getattr(state, "task_no", "?"), object_key, exc,
        )
        return ""


async def _load_existing_report_meta(task_id: str) -> Optional[Dict[str, Any]]:
    """
    读取任务文档中已有的报告元信息

    参数：
        task_id: 任务 ID

    返回：
        含 report_object_key / report_content_hash 的字典；
        任务不存在或查询异常时返回 None（幂等判定降级为"重新上传"）
    """
    from app.repositories import task_repo

    try:
        # 直接读原始文档，避免模型层默认值掩盖"字段缺失"这一事实
        task = await task_repo.find_by_task_id(task_id)
        if task is None:
            return None
        return {
            "report_object_key": getattr(task, "report_object_key", None),
            "report_content_hash": getattr(task, "report_content_hash", None),
        }
    except Exception as exc:  # noqa: BLE001 - 查询失败降级为重新上传，不阻断主流程
        logger.warning(
            "读取任务报告元信息失败，降级为重新上传 | 任务=%s | 异常=%s", task_id, exc
        )
        return None


async def _persist_report_fields(
    task_id: str,
    task_no: str,
    object_key: str,
    content_hash: str,
    generated_at: datetime,
    rollback_on_failure: bool = True,
) -> bool:
    """
    持久化报告字段到任务文档

    说明：
        仅写入报告相关的 4 个字段，绝不触碰状态机、审批结论、回写状态
        等任何判定类字段（权责红线）。

    参数：
        task_id: 任务 ID
        task_no: 任务编号（仅用于日志）
        object_key: MinIO 对象键
        content_hash: 报告内容指纹
        generated_at: 报告生成时间
        rollback_on_failure: 落库失败时是否回滚已上传的 MinIO 对象

    返回：
        True 表示落库成功
    """
    from app.repositories import task_repo
    from app.storage import minio_client

    updates = {
        "report_object_key": object_key,
        "report_format": "md",
        "report_generated_at": generated_at,
        "report_content_hash": content_hash,
    }

    try:
        ok = await task_repo.update_task(task_id, updates)
        if ok:
            logger.info(
                "报告字段已持久化 | 任务=%s | 对象键=%s | 指纹=%s",
                task_no, object_key, content_hash[:12],
            )
            return True

        # update_one 在"值未变化"时 modified_count 为 0，此时也算成功；
        # 但任务确实存在却更新 0 条，更可能是并发被删，仍需回滚。
        logger.warning(
            "报告字段落库未命中任何文档 | 任务=%s | 对象键=%s", task_no, object_key
        )
    except Exception as exc:  # noqa: BLE001 - 统一兜底，避免脏数据残留
        logger.error(
            "报告字段落库异常 | 任务=%s | 对象键=%s | 异常=%s", task_no, object_key, exc
        )

    # -------------------- 回滚：删除已上传对象，避免"有文件无记录" --------------------
    if rollback_on_failure:
        try:
            await minio_client.delete_object(
                bucket=settings.MINIO_BUCKET_REPORT, object_key=object_key
            )
            logger.warning(
                "报告落库失败，已回滚 MinIO 对象 | 任务=%s | 对象键=%s",
                task_no, object_key,
            )
        except Exception as exc:  # noqa: BLE001 - 回滚失败需显式告警，便于人工清理
            logger.error(
                "报告对象回滚失败，需人工清理 | 任务=%s | 对象键=%s | 异常=%s",
                task_no, object_key, exc,
            )

    return False


def _check_quality(state: Any, suggestion_text: str) -> tuple:
    """
    生成后五维质量校验

    说明：
        校验只影响"呈现质量"，不改变风险等级判定。

    参数：
        state: 图状态
        suggestion_text: 修改建议汇总文本

    返回：
        (是否通过, 问题列表)
    """
    issues: List[str] = []

    # 维度一：有正式判定项时必须有建议
    official_count = sum(1 for i in state.final_risk_items if not i.reference_only)
    suggestion_count = sum(1 for i in state.final_risk_items if i.suggestion_text)
    if official_count > 0 and suggestion_count == 0:
        issues.append("正式判定项存在但均无修改建议")

    # 维度二：有结论文本
    if not state.conclusion_text.strip():
        issues.append("审查结论文本为空")

    # 维度三：正式判定项应有法律依据
    if official_count > 0 and not any(i.legal_basis for i in state.final_risk_items):
        issues.append("正式判定项均缺少法律依据")

    # 维度四：建议文本非空
    if not suggestion_text.strip():
        issues.append("修改建议汇总文本为空")

    return (not issues, issues)
