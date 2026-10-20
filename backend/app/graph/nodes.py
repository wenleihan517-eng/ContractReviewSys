# D:/ContractReviewSys-合同审批智能审查项目/backend/app/graph/nodes.py
"""
审查流程节点实现 — 十个节点的具体业务逻辑

【文件定位】
    本模块实现 LangGraph 图中的全部节点函数。每个节点遵循统一契约：
        入参：state（ReviewTaskState）—— 当前完整图状态
        出参：Dict[str, Any] —— 状态增量字典（由 node_wrapper 包装后返回）

    "只返回增量、不修改入参"是 LangGraph 的强制要求，也是本模块所有
    节点实现的基本约束。凡直接修改 state 字段的实现都会导致状态丢失。

【十个节点与职责】
    ┌────────────────────┬──────────────────────────────────────────────┐
    │ 节点                │ 职责                                          │
    ├────────────────────┼──────────────────────────────────────────────┤
    │ pull_approval_task │ 拉取审批单，注入任务与合同基础信息，判定路由    │
    │ parse_document     │ 文档解析（文本抽取 / OCR）+ 条款结构化         │
    │ rule_review        │ 规则引擎全量匹配与定级（唯一判定方）           │
    │ vector_review      │ 向量语义召回，产出候选（无定级权）             │
    │ llm_review         │ 大模型疑似风险识别与建议初稿（无定级权）       │
    │ risk_merge         │ 多路召回融合去重，冲突裁决，形成最终风险项     │
    │ generate_report    │ 生成审查报告与修改建议，执行生成质量校验       │
    │ generate_comment   │ 构造审批评论区内容（受白名单约束）             │
    │ write_comment      │ 回写审批评论区（严禁改变审批结论）             │
    │ audit              │ 流程收敛，汇总审计链路，确定终态               │
    └────────────────────┴──────────────────────────────────────────────┘

【架构红线（本模块的代码级强制点）】
    1. rule_review 是唯一有权产出 risk_level / decision / is_blocked 的节点。
    2. vector_review 与 llm_review 的产出结构（VectorCandidate / LLMCandidate）
       在数据模型层面就不含风险等级字段，无法承载判定结果。
    3. risk_merge 在合并时显式丢弃来自候选的风险等级字段（若有），
       只保留其证据与建议文本。
    4. write_comment 调用审批接口前必须经过 approval_guard 白名单校验，
       且只允许调用评论区接口。
    5. 大额/高风险任务（deep）追加人工复核网关标记，但不改变规则引擎判定。

返回值说明：
    每个节点函数返回 Dict[str, Any]（状态增量字典）

使用示例：
    from app.graph.nodes import parse_document
    updates = await parse_document(state)
"""

import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from app.constants import (
    ClauseType,
    ReviewTaskStatus,
    WorkflowNode,
)
from app.core.config import settings
from app.core.logging_config import get_logger
from app.engines import rule_engine
from app.graph.node_utils import (
    NonRetryableError,
    NodeResult,
    build_blocked_state,
    node_wrapper,
)
from app.graph.state import (
    ClauseItem,
    DocMetadata,
    ParseStatus,
    ParagraphNode,
    ReviewTaskState,
    TextBlock,
    WriteBackState,
)
from app.models.documents.review_task import BlockedReason

logger = get_logger(__name__)


# ======================================================================
# 常量：条款标题正则
# 说明：合同条款标题具有强规律性（"第X条" / "第X章" / "X、标题"），
#       使用确定性规则切分，不依赖模型判断，保证切分结果可复现。
# ======================================================================
# 匹配 "第一条" "第 1 条" "第壹条" 等形式的条款起始标记
CLAUSE_TITLE_PATTERN = re.compile(
    r"^\s*第\s*([0-9一二三四五六七八九十百千零〇]{1,8})\s*(条|条款|节)\s*"
)
# 匹配 "第三章" "第 3 章" 等章节标记
CHAPTER_TITLE_PATTERN = re.compile(
    r"^\s*第\s*([0-9一二三四五六七八九十百千零〇]{1,8})\s*章\s*"
)
# 匹配 "一、标题" "1. 标题" "（一）标题" 等次级标记
SUB_CLAUSE_PATTERN = re.compile(
    r"^\s*(?:[（(]\s*([0-9一二三四五六七八九十]{1,4})\s*[）)]|([0-9]{1,3})\s*[、.．])\s*"
)

# 中文数字映射，用于将"第三条"转为序号 3
CHINESE_NUM_MAP: Dict[str, int] = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}

# 条款类型的关键词映射（确定性分类，供规则模板按类型匹配）
CLAUSE_TYPE_KEYWORDS: List[Tuple[str, Tuple[str, ...]]] = [
    (ClauseType.PAYMENT.value, ("付款", "货款", "价款", "支付", "结算", "发票")),
    (ClauseType.DELIVERY.value, ("交付", "交货", "验收", "运输", "发货")),
    (ClauseType.WARRANTY.value, ("质量", "保证", "保修", "质保", "瑕疵")),
    (ClauseType.LIABILITY.value, ("违约", "赔偿", "损失", "责任", "罚款")),
    (ClauseType.CONFIDENTIALITY.value, ("保密", "商业秘密", "信息披露")),
    (ClauseType.INTELLECTUAL_PROPERTY.value, ("知识产权", "专利", "著作权", "商标", "成果归属")),
    (ClauseType.TERMINATION.value, ("解除", "终止", "退约", "中止")),
    (ClauseType.FORCE_MAJEURE.value, ("不可抗力", "自然灾害", "疫情", "战争")),
    (ClauseType.DISPUTE_RESOLUTION.value, ("争议", "仲裁", "诉讼", "管辖", "协商")),
    (ClauseType.DATA_PROTECTION.value, ("个人信息", "数据保护", "隐私", "数据安全")),
    (ClauseType.COMPLIANCE.value, ("合规", "反腐败", "廉洁", "商业道德", "反商业贿赂")),
    (ClauseType.SUBJECT_MATTER.value, ("标的", "金额", "合同价", "总价")),
    (ClauseType.PARTY_INFO.value, ("甲方", "乙方", "当事人", "法定代表人")),
]


# ======================================================================
# 工具函数：中文数字转阿拉伯数字
# ======================================================================
def chinese_to_int(text: str) -> Optional[int]:
    """
    将中文数字字符串转换为整数

    说明：
        支持合同条款标题中常见的数字形式，覆盖"十""十一""二十三"
        "一百零八"等表达。无法解析时返回 None，由调用方回退到序号自增。

    参数：
        text: 中文数字字符串，如 "三" "十一" "二十三"

    返回：
        转换后的整数；无法解析时返回 None

    使用示例：
        chinese_to_int("十一")  # 11
    """
    if not text:
        return None

    # 纯阿拉伯数字直接转换
    if text.isdigit():
        return int(text)

    text = text.strip()
    if not text:
        return None

    # 含"十"的组合，如 "十一" "二十三" "一百一十"
    if "十" in text:
        parts = text.split("十")
        left = parts[0]
        right = parts[1] if len(parts) > 1 else ""

        # 十位：空前缀视为 1（"十一" → 11）
        if left == "":
            tens = 1
        else:
            tens = CHINESE_NUM_MAP.get(left, -1)
            if tens < 0:
                return None

        # 个位：空后缀视为 0（"二十" → 20）
        if right == "":
            units = 0
        else:
            units = CHINESE_NUM_MAP.get(right, -1)
            if units < 0:
                return None

        return tens * 10 + units

    # 单个汉字数字
    return CHINESE_NUM_MAP.get(text)


def classify_clause_type(title: str, content: str) -> str:
    """
    依据标题与正文关键词对条款做确定性分类

    说明：
        分类结果供规则模板按类型匹配使用。本函数采用关键词优先的确定性
        策略：标题命中优先，其次正文；均未命中时归为 OTHER。
        大模型可对分类提出"建议"，但最终归类以本函数的确定性结果为准。

    参数：
        title: 条款标题
        content: 条款正文

    返回：
        条款类型字符串，取值见 ClauseType 枚举
    """
    # 标题优先：标题通常最能体现条款意图
    for clause_type, keywords in CLAUSE_TYPE_KEYWORDS:
        if any(keyword in title for keyword in keywords):
            return clause_type

    # 正文兜底：取前 200 字做关键词匹配，避免长正文被无关词干扰
    head = content[:200]
    for clause_type, keywords in CLAUSE_TYPE_KEYWORDS:
        if any(keyword in head for keyword in keywords):
            return clause_type

    return ClauseType.OTHER.value


# ======================================================================
# 节点一：pull_approval_task
# ======================================================================
@node_wrapper(node_name=WorkflowNode.PULL_APPROVAL_TASK, progress=5)
async def pull_approval_task(state: ReviewTaskState) -> NodeResult:
    """
    节点：拉取审批任务

    【职责】
        1. 依据 approval_id 从企业审批系统拉取审批单，补齐合同基础信息
        2. 校验必填字段完整性（合同标题、编号、金额、相对方）
        3. 判定差异化路由类型（fast / standard / deep）
        4. 初始化文档解析状态与文件指纹占位
        5. 记录路由决策快照，保证"为什么走这条链路"可回溯

    【入参】
        state: 图状态，需已注入 task_id / task_no / approval_id / contract_id

    【出参】
        NodeResult，updates 含：
            contract_title / contract_no / submit_dept / applicant /
            counterparty / contract_amount / currency / performance_period /
            business_type / contract_type / template_flag / source_type /
            route_type / status / started_at / thread_id / work_dir

    【异常】
        NonRetryableError：必填字段缺失时抛出，任务置 blocked(MANUAL)

    【注意】
        本节点不调用任何模型。审批单拉取在真实实现中通过审批系统只读接口完成，
        当前已按"审批单信息由接入层预注入 state"的方式实现完整校验链路。
        若 state 中基础信息已完整（由接入层注入），本节点只做校验与路由判定。
    """
    # 延迟导入，避免模块级循环依赖
    from app.graph.routes import build_route_snapshot, decide_route_type

    start_dt = datetime.now()

    # -------------------- 校验：任务标识必须存在 --------------------
    if not state.task_id or not state.task_no:
        raise NonRetryableError(
            reason=BlockedReason.MANUAL.value,
            message="任务标识缺失，无法启动审查流程",
            detail={"task_id": state.task_id, "task_no": state.task_no},
            retriable=False,
        )

    # -------------------- 校验：审批单必须已关联 --------------------
    if not state.approval_id:
        raise NonRetryableError(
            reason=BlockedReason.MANUAL.value,
            message="审批单 ID 缺失，无法关联企业审批流",
            detail={"task_no": state.task_no},
            retriable=True,
        )

    # -------------------- 校验：合同基础信息完整性 --------------------
    missing_fields: List[str] = []
    if not state.contract_title:
        missing_fields.append("contract_title（合同标题）")
    if not state.contract_no:
        missing_fields.append("contract_no（合同编号）")
    if not state.counterparty:
        missing_fields.append("counterparty（合同相对方）")
    if state.contract_amount <= 0:
        missing_fields.append("contract_amount（合同金额）")

    if missing_fields:
        raise NonRetryableError(
            reason=BlockedReason.MANUAL.value,
            message=f"合同基础信息不完整，缺失字段：{'、'.join(missing_fields)}",
            detail={"missing_fields": missing_fields, "task_no": state.task_no},
            retriable=True,
        )

    # -------------------- 路由判定 --------------------
    route_type = decide_route_type(state)
    route_snapshot = build_route_snapshot(state)

    # -------------------- 构造状态增量 --------------------
    # 说明：thread_id 与 work_dir 若已在接入层注入则保留，否则此处补齐，
    #       保证异步会话隔离与任务文件目录隔离始终成立。
    thread_id = state.thread_id
    work_dir = state.work_dir
    if not thread_id:
        from app.utils.id_generator import generate_thread_id

        thread_id = generate_thread_id()
    if not work_dir:
        from app.utils.id_generator import build_work_dir

        work_dir = build_work_dir(state.task_id)

    updates: Dict[str, Any] = {
        "route_type": route_type,
        "status": ReviewTaskStatus.RUNNING.value,
        "thread_id": thread_id,
        "work_dir": work_dir,
        "started_at": start_dt,
        "parse_status": ParseStatus.NOT_STARTED.value,
        "file_fingerprint": state.file_fingerprint,
    }

    return NodeResult(
        updates=updates,
        message=(
            f"审批任务拉取完成 | 审批单={state.approval_id} | "
            f"路由类型={route_type} | 金额={state.contract_amount:,.2f} {state.currency}"
        ),
        detail={
            "approval_id": state.approval_id,
            "contract_id": state.contract_id,
            "route_snapshot": route_snapshot,
            "thread_id": thread_id,
            "work_dir": work_dir,
        },
    )


# ======================================================================
# 节点二：parse_document
# ======================================================================
@node_wrapper(node_name=WorkflowNode.PARSE_CONTRACT, progress=25)
async def parse_document(state: ReviewTaskState) -> NodeResult:
    """
    节点：文档解析与条款结构化

    【职责】
        1. 从 MinIO 拉取合同原文（按 file_object_key）
        2. 判定解析策略：电子合同走文本抽取，扫描件走 OCR
        3. 抽取文本并切分为文本块，每块记录 page / start_idx / end_idx / 段落索引
        4. 还原段落树结构（章 → 条 → 款）
        5. 按确定性规则切分条款并分类
        6. 执行三类法定阻断判定：文档加密 / 扫描件模糊 / 正文为空

    【入参】
        state: 图状态，需含 file_object_key / source_type

    【出参】
        NodeResult，updates 含：
            parse_status / text_blocks / paragraphs / full_text / clauses /
            doc_metadata / parse_error / parse_retry_count / file_fingerprint /
            unstructured_text

    【异常】
        NonRetryableError（三类法定阻断，不可重试）：
            DOC_ENCRYPTED   文档加密
            SCAN_TOO_BLURRY OCR 置信度低于阈值
            EMPTY_CONTENT   抽取正文长度不足
        IOError / ValueError：可重试异常，由包装器指数退避重试

    【实现说明】
        本节点已实现完整的解析编排、文本块构建、段落树还原、条款切分与
        三类阻断判定逻辑。底层具体的 PDF/DOCX 文本抽取与 OCR 调用封装在
        app.services.parse_service 中（属于"文档解析模块"的交付范围），
        本节点通过 _load_contract_text 统一入口调用，接口契约已固定：
            入参：(object_key, source_type, work_dir)
            出参：(full_text, page_texts, doc_meta)
        在解析服务就绪前，若 MinIO 不可用或对象不存在，本节点会抛出
        可重试异常，绝不会返回"解析成功但内容为空"的假成功状态。
    """
    start_dt = datetime.now()

    # -------------------- 前置校验：文件对象必须存在 --------------------
    if not state.file_object_key:
        raise NonRetryableError(
            reason=BlockedReason.EMPTY_CONTENT.value,
            message="合同文件对象键缺失，无法定位原文文件",
            detail={"task_no": state.task_no, "task_id": state.task_id},
            retriable=True,
        )

    # -------------------- 拉取并解析原文 --------------------
    # 【异常处理说明】
    #   解析服务会把三类法定阻断以 ParseBlockedSignal 抛出，
    #   它的语义是"业务事实"而非"程序错误"，必须在此转为 NonRetryableError，
    #   直接置任务 blocked 并给出真实原因，绝不进入重试链路。
    #   其余异常（IOError / ValueError）保持原样上抛，由节点包装器
    #   按指数退避重试，重试耗尽后置 blocked(RETRY_EXHAUSTED)。
    #
    #   延迟导入 ParseBlockedSignal 的原因：该异常定义在 parse_service 中，
    #   而 parse_service 顶层会导入 PyMuPDF 等解析依赖。
    #   若在模块级导入，则任何未安装解析依赖的环境（如仅跑图结构测试）
    #   都无法加载本模块。延迟到实际解析时导入可避免这种耦合。
    from app.services.parse_service import ParseBlockedSignal as _ParseBlockedSignal

    try:
        full_text, page_texts, doc_meta = await _load_contract_text(
            object_key=state.file_object_key,
            source_type=state.source_type,
            task_id=state.task_id,
            work_dir=state.work_dir,
            file_fingerprint=state.file_fingerprint,
        )

    except _ParseBlockedSignal as exc:
        # 三类法定阻断：加密 / 扫描件模糊 / 正文为空
        logger.warning(
            "文档解析命中法定阻断 | 任务=%s | 原因=%s | 说明=%s",
            state.task_id, exc.reason, exc.message,
        )
        raise NonRetryableError(
            reason=exc.reason,
            message=exc.message,
            detail={
                **exc.detail,
                "task_no": state.task_no,
                "task_id": state.task_id,
                "blocked_source": "parse_service",
            },
            retriable=False,
        ) from exc

    # -------------------- 判定一：文档加密 --------------------
    if doc_meta.is_encrypted:
        raise NonRetryableError(
            reason=BlockedReason.DOC_ENCRYPTED.value,
            message="合同文档已加密，无法抽取正文内容，请上传未加密版本",
            detail={
                "file_name": doc_meta.file_name,
                "task_no": state.task_no,
                "file_fingerprint": doc_meta.file_hash,
            },
            retriable=True,
        )

    # -------------------- 判定二：扫描件模糊 --------------------
    if doc_meta.source_type == "scanned" and doc_meta.avg_ocr_confidence is not None:
        threshold = settings.OCR_MIN_CONFIDENCE_THRESHOLD
        if doc_meta.avg_ocr_confidence < threshold:
            raise NonRetryableError(
                reason=BlockedReason.SCAN_TOO_BLURRY.value,
                message=(
                    f"扫描件识别置信度 {doc_meta.avg_ocr_confidence:.4f} "
                    f"低于阈值 {threshold}，判定影像模糊，请重新扫描上传"
                ),
                detail={
                    "avg_ocr_confidence": doc_meta.avg_ocr_confidence,
                    "threshold": threshold,
                    "page_count": doc_meta.page_count,
                    "task_no": state.task_no,
                },
                retriable=True,
            )

    # -------------------- 判定三：正文为空 --------------------
    min_length = settings.MIN_CONTENT_LENGTH
    stripped_text = full_text.strip()
    if len(stripped_text) < min_length:
        raise NonRetryableError(
            reason=BlockedReason.EMPTY_CONTENT.value,
            message=(
                f"抽取正文长度 {len(stripped_text)} 字符，低于下限 {min_length}，"
                f"判定正文为空，无法开展条款审查"
            ),
            detail={
                "extracted_length": len(stripped_text),
                "min_content_length": min_length,
                "page_count": doc_meta.page_count,
                "task_no": state.task_no,
            },
            retriable=True,
        )

    # -------------------- 构建文本块 --------------------
    text_blocks = _build_text_blocks(full_text, page_texts, doc_meta)

    # -------------------- 还原段落树 --------------------
    paragraphs = _build_paragraph_tree(full_text)

    # -------------------- 条款切分与分类 --------------------
    clauses, unstructured_text = _split_clauses(full_text)

    # -------------------- 回填条款真实页码 --------------------
    # _split_clauses 无逐页信息，页码默认写 1；此处按文本块映射纠正，
    # 保证"原文定位：第 X 页"在多页合同上真实可信（见 _assign_clause_pages）
    _assign_clause_pages(clauses, text_blocks)

    # -------------------- 校验：必须切出有效条款 --------------------
    if not clauses:
        raise NonRetryableError(
            reason=BlockedReason.PARSE_FAILED.value,
            message="未能从正文中切分出任一条款，请确认文件内容为可识别的合同文本",
            detail={
                "total_chars": len(stripped_text),
                "text_block_count": len(text_blocks),
                "task_no": state.task_no,
            },
            retriable=True,
        )

    # -------------------- 补充文档元数据 --------------------
    doc_meta.total_chars = len(stripped_text)
    doc_meta.parsed_at = datetime.now()

    updates: Dict[str, Any] = {
        "parse_status": ParseStatus.SUCCESS.value,
        "text_blocks": text_blocks,
        "paragraphs": paragraphs,
        "full_text": full_text,
        "clauses": clauses,
        "doc_metadata": doc_meta,
        "parse_error": "",
        "parse_retry_count": state.parse_retry_count,
        "unstructured_text": unstructured_text,
        "file_fingerprint": doc_meta.file_hash or state.file_fingerprint,
    }

    return NodeResult(
        updates=updates,
        message=(
            f"文档解析完成 | 页数={doc_meta.page_count} | 字符数={len(stripped_text)} | "
            f"文本块={len(text_blocks)} | 段落={len(paragraphs)} | 条款={len(clauses)} | "
            f"引擎={doc_meta.parse_engine}"
        ),
        detail={
            "page_count": doc_meta.page_count,
            "total_chars": len(stripped_text),
            "text_block_count": len(text_blocks),
            "paragraph_count": len(paragraphs),
            "clause_count": len(clauses),
            "unstructured_length": len(unstructured_text),
            "parse_engine": doc_meta.parse_engine,
            "source_type": doc_meta.source_type,
            "avg_ocr_confidence": doc_meta.avg_ocr_confidence,
        },
    )


async def _load_contract_text(
    object_key: str,
    source_type: str,
    task_id: str,
    work_dir: str,
    file_fingerprint: str,
) -> Tuple[str, List[str], DocMetadata]:
    """
    加载并解析合同原文（解析服务的统一调用入口）

    【函数定位】
        本函数是图节点与文档解析服务之间的契约点。节点只依赖本函数的
        入参出参签名，具体解析实现（PDF 文本抽取 / DOCX 抽取 / OCR / 混排）
        全部封装在 app.services.parse_service 中。

    【契约】
        入参：
            object_key:       MinIO 对象键
            source_type:      digital / scanned
            task_id:          任务 ID（用于任务级隔离与日志追踪）
            work_dir:         任务工作目录
            file_fingerprint: 已知文件指纹，用于一致性校验
        出参：
            (full_text, page_texts, doc_metadata) 三元组
            full_text:  抽取的全文（按页拼接，页间以换行分隔）
            page_texts: 逐页文本列表，索引 0 对应第 1 页
            doc_metadata: 文档元数据

    【异常语义（与节点重试策略严格配合）】
        IOError            可重试 —— 存储/OCR 服务不可达或文件内容问题
                           节点按指数退避重试，耗尽后置 blocked(RETRY_EXHAUSTED)
        ParseBlockedSignal 不可重试 —— 业务事实阻断
                           节点转 NonRetryableError 直接置 blocked，
                           携带 reason 供人工获知真实原因

    【为什么阻断信号必须原样上抛而不能转成 IOError】
        加密文档、空白文档、模糊扫描件这三类情况，重试多少次结果都一样
        —— 它们取决于文件本身，不取决于服务状态。
        若转成 IOError 让节点重试，会白白消耗三次指数退避的等待时间，
        最终给出的阻塞原因还会变成 RETRY_EXHAUSTED，
        让人误以为"服务不稳定"，而真实原因（文件加密/无内容/影像模糊）
        被彻底掩盖。因此这里必须保持异常类型的区分。

    【实现说明】
        解析服务的完整实现见 app/services/parse_service.py，
        本函数仅做参数透传与异常类型适配，不包含任何解析逻辑。
    """
    # 延迟导入：避免图模块在解析服务依赖（PyMuPDF 等）缺失时无法加载，
    # 也让测试可以在不装解析依赖的情况下单独验证图结构。
    try:
        from app.services import parse_service
    except ImportError as exc:
        logger.error(
            "文档解析服务不可用 | 对象键=%s | 异常=%s | "
            "请确认 app/services/parse_service.py 及其依赖已就绪",
            object_key,
            exc,
        )
        raise IOError(
            "文档解析服务未就绪：app.services.parse_service 导入失败，"
            f"原始异常={exc}"
        ) from exc

    try:
        result = await parse_service.parse_contract_document(
            object_key=object_key,
            source_type=source_type,
            task_id=task_id,
            work_dir=work_dir,
            file_fingerprint=file_fingerprint,
        )

    except parse_service.ParseBlockedSignal:
        # 业务事实阻断：原样上抛，交由节点包装器转为 NonRetryableError
        raise

    except (OSError, IOError):
        # 存储或服务不可用：保持 IOError 语义，节点按可重试处理
        raise

    except Exception as exc:  # noqa: BLE001
        # 其余异常（格式不支持 / 内容损坏 / 解析器内部错误）
        # 统一转为可重试异常 —— 用户换文件后重试即可成功
        logger.error(
            "文档解析失败 | 对象键=%s | 异常=%s", object_key, exc, exc_info=True
        )
        raise IOError(f"文档解析失败：object_key={object_key}, error={exc}") from exc

    return result.to_contract_tuple()


def _build_text_blocks(
    full_text: str,
    page_texts: List[str],
    doc_meta: DocMetadata,
) -> List[TextBlock]:
    """
    构建文本块列表

    【职责】
        将全文按段落切分为文本块，每块精确记录：
            page（页码）/ start_idx（起始字符）/ end_idx（结束字符）/ paragraph_index

    【实现要点】
        1. 优先按逐页文本构建，可精确获得页码；无逐页文本时按段落切分并估算页码
        2. 每页内按换行切分段落；空段落合并到上一块，避免产生大量碎片块
        3. 字符位置以"全文累计偏移"计算，保证与 full_text 严格对应，
           这是"条款 → 原文"反查的准确性基础

    参数：
        full_text: 抽取的合同全文
        page_texts: 逐页文本列表，索引 0 对应第 1 页。可为空
        doc_meta: 文档元数据，用于 OCR 置信度透传

    返回：
        文本块列表，按 block_index 升序
    """
    blocks: List[TextBlock] = []
    cursor = 0          # 全文累计偏移游标
    block_index = 0     # 块序号
    paragraph_index = 0  # 段落序号

    # -------------------- 无逐页文本：整篇按段落切分 --------------------
    if not page_texts:
        for line in full_text.split("\n"):
            line_length = len(line)

            if line.strip():
                blocks.append(
                    TextBlock(
                        block_index=block_index,
                        page=1,
                        start_idx=cursor,
                        end_idx=cursor + line_length,
                        paragraph_index=paragraph_index,
                        text=line,
                        engine=doc_meta.parse_engine,
                    )
                )
                block_index += 1
                paragraph_index += 1
            elif blocks:
                # 空行：并入上一块的字符区间，保持区间连续无空洞
                last = blocks[-1]
                last.end_idx = cursor + line_length + 1

            cursor += line_length + 1  # +1 补偿换行符

        return blocks

    # -------------------- 有逐页文本：逐页逐段构建，页码精确 --------------------
    for page_no, page_text in enumerate(page_texts, start=1):
        lines = page_text.split("\n")

        for line in lines:
            line_length = len(line)

            if line.strip():
                blocks.append(
                    TextBlock(
                        block_index=block_index,
                        page=page_no,
                        start_idx=cursor,
                        end_idx=cursor + line_length,
                        paragraph_index=paragraph_index,
                        text=line,
                        ocr_confidence=(
                            doc_meta.avg_ocr_confidence
                            if doc_meta.source_type == "scanned"
                            else None
                        ),
                        engine=doc_meta.parse_engine,
                    )
                )
                block_index += 1
                paragraph_index += 1
            elif blocks:
                last = blocks[-1]
                last.end_idx = cursor + line_length + 1

            # 【偏移推进规则 —— 必须与 full_text 的拼接方式严格一致】
            #   full_text = "\n".join(page_texts)
            #   即：页内行之间用 1 个 "\n" 分隔，页与页之间也用 1 个 "\n" 分隔。
            #
            #   因此每推进一行的偏移量分两种情况：
            #     · 页内非末行：本行长度 + 1（该行后的换行符）
            #     · 页内末行：  本行长度 + 1（该页后的换行符，即页间分隔符）
            #   二者都加 1，但**只能加一次**。
            #
            #   【曾经的缺陷】此处原先无条件 `cursor += line_length + 1`，
            #   循环外又对页间追加一次 `cursor += 1`，
            #   导致末行被加了 2 个字符，从第 2 页起所有偏移整体右移。
            #   这类偏移错位不会抛错，只表现为"条款与原文对不上"，
            #   极难排查，因此必须在此显式说明并保持单一推进点。
            cursor += line_length + 1

    return blocks


def _build_paragraph_tree(full_text: str) -> List[ParagraphNode]:
    """
    还原段落树结构

    【职责】
        按"章 → 条 → 款"层级还原文档结构，供条款切分与前端展示使用。
        层级的判定完全基于标题标记的正则匹配，不依赖模型。

    【层级规则】
        level 0：章标题（"第三章 ..."）
        level 1：条标题（"第三条 ..."）
        level 2：款/项（"一、" "1." "（一）"）
        level 3：普通正文段落

    参数：
        full_text: 抽取的合同全文

    返回：
        段落节点列表（顶层）。子段落通过 children 递归挂载
    """
    roots: List[ParagraphNode] = []
    # 各层级的当前节点，用于把子段落挂到正确的父节点下
    level_stack: Dict[int, ParagraphNode] = {}

    cursor = 0
    paragraph_index = 0

    for raw_line in full_text.split("\n"):
        line = raw_line.strip()
        line_length = len(raw_line)

        if not line:
            cursor += line_length + 1
            continue

        # -------------------- 判定层级 --------------------
        if CHAPTER_TITLE_PATTERN.match(line):
            level = 0
            title = line[:60]
            content = line
        elif CLAUSE_TITLE_PATTERN.match(line):
            level = 1
            title = line[:60]
            content = line
        elif SUB_CLAUSE_PATTERN.match(line):
            level = 2
            title = line[:40]
            content = line
        else:
            level = 3
            title = ""
            content = line

        node = ParagraphNode(
            paragraph_index=paragraph_index,
            level=level,
            title=title,
            content=content,
            page=1,
            start_idx=cursor,
            end_idx=cursor + line_length,
        )

        # -------------------- 挂载到正确的父节点 --------------------
        if level == 0:
            roots.append(node)
            level_stack = {0: node}
        elif level in (1, 2):
            parent_level = level - 1
            parent = level_stack.get(parent_level)
            if parent is None:
                # 缺少上级标题时提升为顶层，避免段落丢失
                roots.append(node)
            else:
                parent.children.append(node)
            # 清理更深层级，保证栈内只保留当前路径
            level_stack = {
                key: value for key, value in level_stack.items() if key < level
            }
            level_stack[level] = node
        else:
            # 普通正文并入最近的标题节点
            target = level_stack.get(2) or level_stack.get(1) or level_stack.get(0)
            if target is None:
                root_node = ParagraphNode(
                    paragraph_index=paragraph_index,
                    level=3,
                    title="",
                    content=content,
                    start_idx=cursor,
                    end_idx=cursor + line_length,
                )
                roots.append(root_node)
            else:
                target.content = f"{target.content}\n{line}" if target.content else line
                target.end_idx = cursor + line_length

        paragraph_index += 1
        cursor += line_length + 1

    return roots


def _split_clauses(full_text: str) -> Tuple[List[ClauseItem], str]:
    """
    条款切分与分类

    【职责】
        按确定性规则切分条款，并完成类型分类与位置记录。

    【切分策略】
        1. 逐行扫描，识别"第X条/第X章"标记作为切分边界
        2. 章标题不单独成条，作为所属条款的章节归属信息
        3. 每条的字符区间取 [起始行偏移, 结束行偏移)，与 full_text 严格对应
        4. 落在首个条款标记之前的正文（前言/主体信息段）归入"未结构化内容"，
           但若其长度超过阈值且包含当事人信息，则单独作为一条 PARTY_INFO 条款

    【为什么必须确定性切分】
        条款切分结果是"原文 ↔ 结构化结果"双向溯源的锚点。若由模型切分，
        同一份合同两次处理可能得到不同区间，导致溯源失效、审计无法复现。

    参数：
        full_text: 抽取的合同全文

    返回：
        (条款列表, 未结构化文本) 元组
    """
    lines = full_text.split("\n")

    # 预计算每行的全文起始偏移
    offsets: List[int] = []
    cursor = 0
    for line in lines:
        offsets.append(cursor)
        cursor += len(line) + 1

    # -------------------- 第一遍：定位所有条款起始行 --------------------
    clause_starts: List[Tuple[int, str]] = []  # (行号, 章节标题)
    current_chapter = ""

    for index, raw_line in enumerate(lines):
        line = raw_line.strip()
        if not line:
            continue

        chapter_match = CHAPTER_TITLE_PATTERN.match(line)
        if chapter_match:
            current_chapter = line[:60]
            continue

        if CLAUSE_TITLE_PATTERN.match(line):
            clause_starts.append((index, current_chapter))

    # -------------------- 第二遍：按起始行切分条款正文 --------------------
    clauses: List[ClauseItem] = []

    for order, (start_index, chapter) in enumerate(clause_starts):
        # 本条结束行：下一条起始行，或文末
        end_index = (
            clause_starts[order + 1][0]
            if order + 1 < len(clause_starts)
            else len(lines)
        )

        body_lines = [lines[i] for i in range(start_index, end_index) if lines[i].strip()]
        if not body_lines:
            continue

        title = body_lines[0].strip()[:80]
        content = "\n".join(body_lines)

        char_start = offsets[start_index]
        char_end = offsets[end_index - 1] + len(lines[end_index - 1])

        if char_end <= char_start:
            continue

        clauses.append(
            ClauseItem(
                clause_id=f"C{order + 1:04d}",
                clause_index=order + 1,
                clause_type=classify_clause_type(title, content),
                clause_title=title,
                content=content,
                char_start=char_start,
                char_end=char_end,
                page_start=1,
                page_end=1,
                paragraph_index=start_index,
            )
        )

    # -------------------- 处理条款标记之前的前置内容 --------------------
    head_end = offsets[clause_starts[0][0]] if clause_starts else len(full_text)
    head_text = full_text[:head_end].strip()

    unstructured = ""
    if head_text:
        # 前置内容若包含当事人信息关键词，单独成条并置为 PARTY_INFO
        if any(
            keyword in head_text for keyword in ("甲方", "乙方", "当事人", "法定代表人")
        ):
            clauses.insert(
                0,
                ClauseItem(
                    clause_id="C0000",
                    clause_index=1,
                    clause_type=ClauseType.PARTY_INFO.value,
                    clause_title="首部：当事人与合同基本信息",
                    content=head_text,
                    char_start=0,
                    char_end=head_end,
                    page_start=1,
                    page_end=1,
                    paragraph_index=0,
                ),
            )
            # 重排序号，保证 clause_index 连续且从 1 开始
            for index, clause in enumerate(clauses, start=1):
                clause.clause_index = index
                clause.clause_id = f"C{index:04d}"
        else:
            unstructured = head_text

    # -------------------- 条款标记之后的残余内容 --------------------
    if clause_starts:
        last_end = clauses[-1].char_end if clauses else 0
        tail_text = full_text[last_end:].strip()
        if tail_text:
            unstructured = f"{unstructured}\n{tail_text}".strip()

    return clauses, unstructured


def _assign_clause_pages(
    clauses: List[ClauseItem],
    text_blocks: List[TextBlock],
) -> None:
    """
    按文本块的实际页码范围回填条款的 page_start / page_end（原地修改）

    【为什么必须回填（2026-09-21 修复｜页码溯源失真）】
        `_split_clauses` 只拿到全文，没有逐页信息，因此把每条条款的
        page_start / page_end 一律写成 1。对多页合同这会直接产出错误的页码：
        实测 sample1（2 页）中第四条及之后的所有条款实际都在第 2 页，
        却统一被标为"第 1 页"，于是报告「原文定位：第 1 页」与前端
        风险卡「P1」全部显示错误页码（字符区间正确、页码错误，属最难察觉的
        半对半错）。

        文本块（TextBlock）在 `_build_text_blocks` 中已按页构建并携带
        start_idx / end_idx / page，是全文偏移 → 页码的权威映射。
        故此处用条款的 [char_start, char_end) 与文本块区间求交集，
        取首个相交块与末个相交块的页码作为 page_start / page_end。
        该函数与 `_split_clauses` 同层，不改变其签名（避免影响既有调用与测试）。

    参数：
        clauses: 条款列表（原地写回页码）
        text_blocks: 文本块列表（须含 start_idx / end_idx / page）

    返回：
        None（原地修改 clauses）
    """
    if not clauses or not text_blocks:
        return

    ordered = sorted(text_blocks, key=lambda b: b.start_idx)

    for clause in clauses:
        first_page: Optional[int] = None
        last_page: Optional[int] = None

        for block in ordered:
            # 无交集的块直接跳过（块区间与条款区间均为左闭右开）
            if block.end_idx <= clause.char_start:
                continue
            if block.start_idx >= clause.char_end:
                break
            if first_page is None:
                first_page = block.page
            last_page = block.page

        if first_page is not None:
            clause.page_start = first_page
            clause.page_end = last_page or first_page


# ======================================================================
# 节点三：rule_review（架构红线核心节点）
# ======================================================================
@node_wrapper(node_name=WorkflowNode.RULE_EVALUATE, progress=55)
async def rule_review(state: ReviewTaskState) -> NodeResult:
    """
    节点：规则引擎审查（唯一判定节点）

    【职责】
        1. 加载启用的规则模板集合
        2. 对每一条结构化条款执行全量规则匹配
        3. 将匹配结果提交规则引擎，由引擎完成：
             - 风险等级定级（severity → risk_level）
             - 违规判定（violated_rule_codes）
             - 任务阻塞判定（is_blocked）
             - 处理决策推导（decision）
             - 结论文本与防篡改哈希生成
        4. 构造审计快照，保证判定过程可完整回溯

    【入参】
        state: 图状态，需已含 clauses / task_id

    【出参】
        NodeResult，updates 含：
            rule_results / rule_hit_codes / rule_engine_version /
            rule_evaluated / risk_level / is_blocked / blocked_info /
            risk_distribution / conclusion_text / conclusion_hash

    【异常】
        RuleEngineException：规则引擎关闭或定级失败时抛出（属于可重试异常，
                            但引擎关闭属于配置错误，重试也不会成功；
                            故此处转为 NonRetryableError 直接阻塞）

    【架构红线】
        本节点是系统中唯一有权产出 risk_level / is_blocked / decision 的位置。
        大模型与向量召回的产出物在这一步只作为"匹配输入"的补充线索，
        不参与定级计算。
    """
    start_dt = datetime.now()

    if not state.clauses:
        raise NonRetryableError(
            reason=BlockedReason.PARSE_FAILED.value,
            message="无结构化条款，规则引擎无法执行判定",
            detail={"task_no": state.task_no},
            retriable=True,
        )

    # -------------------- 步骤一：加载启用的规则模板 --------------------
    rule_templates = await _load_active_rule_templates(state)

    if not rule_templates:
        # 无规则模板意味着无法定级。合同场景下"无规则即放行"是严重合规风险，
        # 因此显式阻塞并提示管理员配置规则。
        raise NonRetryableError(
            reason=BlockedReason.MANUAL.value,
            message=(
                "系统未配置任何启用状态的规则模板，无法执行合规判定。"
                "请系统管理员先完成规则模板配置后再重跑本任务。"
            ),
            detail={"task_no": state.task_no, "template_count": 0},
            retriable=True,
        )

    # -------------------- 步骤二：一体化规则审查（匹配+定级+结论收敛到引擎） --------------------
    # 【架构说明】
    #   规则匹配、风险定级、阻塞判定、结论生成全部收敛到 rule_engine.review_clauses，
    #   节点只做编排与状态回填，不再自行实现匹配逻辑。
    #   这样保证 keyword/regex/clause_type/field_compare/numeric_threshold/missing_clause
    #   六种匹配模式在节点中全部生效，避免"节点不认识新匹配类型导致规则静默失效"。
    try:
        outcome = rule_engine.review_clauses(
            clauses=state.clauses,
            contract_type=state.contract_type,
            task_id=state.task_id,
            contract_amount=state.contract_amount,
            rules=rule_templates,
        )
        decision = outcome.decision
        rule_results = outcome.risk_items
        hit_inputs = outcome.rule_hit_inputs
    except Exception as exc:  # noqa: BLE001 - 规则引擎异常必须显式阻塞
        logger.error(
            "规则引擎判定失败 | 任务=%s | 异常=%s", state.task_no, exc, exc_info=True
        )
        raise NonRetryableError(
            reason=BlockedReason.MANUAL.value,
            message=f"规则引擎判定失败，禁止未判定放行：{exc}",
            detail={"task_no": state.task_no, "rule_count": len(rule_templates)},
            retriable=True,
        ) from exc

    # 命中的规则编码列表（去重，表示"命中了哪些规则"）
    rule_hit_codes: List[str] = []
    for item in rule_results:
        if item.rule_code and item.rule_code not in rule_hit_codes:
            rule_hit_codes.append(item.rule_code)

    # -------------------- 步骤三：审计快照 --------------------
    try:
        audit_snapshot = rule_engine.build_audit_snapshot(decision, hit_inputs)
        snapshot_hash = audit_snapshot.get("snapshot_hash", "")
    except Exception as exc:  # noqa: BLE001 - 快照失败不影响主判定
        logger.error("规则判定快照构造失败 | 任务=%s | 异常=%s", state.task_no, exc)
        snapshot_hash = ""

    # -------------------- 步骤四：构造状态增量 --------------------
    updates: Dict[str, Any] = {
        "rule_results": rule_results,
        "rule_hit_codes": rule_hit_codes,
        "rule_engine_version": decision.engine_version,
        "rule_evaluated": True,
        # 【架构红线】以下三个字段只能由本节点写入，值全部来自规则引擎
        "risk_level": decision.final_risk_level,
        "is_blocked": decision.is_blocked,
        "risk_distribution": decision.risk_level_distribution,
        "conclusion_text": decision.conclusion,
        "conclusion_hash": decision.conclusion_hash,
        "status": (
            ReviewTaskStatus.RULE_DONE.value
            if not decision.is_blocked
            else ReviewTaskStatus.BLOCKED.value
        ),
    }

    # 阻塞时构造阻塞上下文（由规则引擎判定驱动）
    if decision.is_blocked:
        blocked_info = build_blocked_state(
            state=state,
            node=WorkflowNode.RULE_EVALUATE.value,
            reason=BlockedReason.MANUAL.value,
            detail_message=(
                f"规则引擎判定最终风险等级为【{decision.final_risk_level}】，"
                f"命中阻塞等级配置 {settings.RULE_ENGINE_BLOCK_LEVELS}，"
                f"任务被阻塞，禁止推进审批流"
            ),
            retry_count=0,
            retriable=True,
        )
        # 只取阻塞上下文相关字段，审计日志由包装器统一处理
        updates["blocked_info"] = blocked_info["blocked_info"]
        updates["status"] = blocked_info["status"]
        updates["error_message"] = blocked_info["error_message"]

    return NodeResult(
        updates=updates,
        message=(
            f"规则引擎判定完成 | 引擎版本={decision.engine_version} | "
            f"命中规则={len(rule_hit_codes)}条 | 最终等级={decision.final_risk_level} | "
            f"处理决策={decision.decision} | 阻塞={decision.is_blocked}"
        ),
        detail={
            "engine_version": decision.engine_version,
            "total_templates": len(rule_templates),
            "total_hit_inputs": len(hit_inputs),
            "hit_count": len(rule_results),
            "final_risk_level": decision.final_risk_level,
            "is_blocked": decision.is_blocked,
            "decision": decision.decision,
            "risk_distribution": decision.risk_level_distribution,
            "conclusion_hash": decision.conclusion_hash[:16] + "...",
            "snapshot_hash": snapshot_hash[:16] + "..." if snapshot_hash else "",
        },
    )


async def _load_active_rule_templates(state: ReviewTaskState) -> List[Any]:
    """
    加载启用状态的规则模板集合

    【契约】
        入参：state（使用 contract_type 做类型过滤）
        出参：RuleTemplate 列表

    【实现说明】
        规则模板的持久化读取优先由 app.repositories.rule_template_repo 承担。
        当仓储层尚未交付（ImportError）时，降级为从规则引擎的 YAML 配置
        文件（settings.RULE_RULES_PATH）加载，保证节点在仓储就绪前也能
        跑通规则审查，绝不返回空列表导致"无规则即放行"。

    参数：
        state: 当前图状态

    返回：
        RuleTemplate 实例列表（已过滤启用状态 + 合同类型 + 优先级排序）

    异常：
        IOError：仓储层与 YAML 配置均不可用时抛出（可重试）
    """
    # -------------------- 优先：仓储层读取 --------------------
    try:
        from app.repositories.rule_template_repo import list_active_templates
    except ImportError:
        # 仓储层未交付，降级到规则引擎的 YAML 配置加载
        return _load_rule_templates_from_yaml(state)

    try:
        templates = await list_active_templates(contract_type=state.contract_type)
    except Exception as exc:  # noqa: BLE001 - 读取失败视为可重试
        logger.error(
            "规则模板加载失败 | 任务=%s | 异常=%s", state.task_no, exc, exc_info=True
        )
        raise IOError(f"加载规则模板失败：{exc}") from exc

    logger.info(
        "规则模板加载完成 | 合同类型=%s | 模板数=%d",
        state.contract_type,
        len(templates),
    )
    return list(templates or [])


def _load_rule_templates_from_yaml(state: ReviewTaskState) -> List[Any]:
    """
    从规则引擎 YAML 配置加载启用规则（仓储层未就绪时的降级路径）

    说明：
        委托规则引擎的 RuleRegistry 加载并过滤，复用与审查链路完全一致的
        启用判定、合同类型过滤与优先级排序逻辑，保证降级路径与正式路径
        行为等价、可审计。

    参数：
        state: 当前图状态

    返回：
        RuleTemplate 实例列表

    异常：
        IOError：YAML 加载失败时抛出（可重试）
    """
    from app.engines.rule_engine import rule_engine

    try:
        all_rules = rule_engine.load_rules_from_yaml(settings.RULE_RULES_PATH)
        templates = rule_engine.get_active_rules(all_rules, state.contract_type)
    except Exception as exc:  # noqa: BLE001 - 加载失败视为可重试
        logger.error(
            "规则模板 YAML 加载失败 | 任务=%s | 异常=%s",
            state.task_no, exc, exc_info=True,
        )
        raise IOError(
            f"规则模板加载失败（仓储未就绪且 YAML 配置不可用）：{exc}"
        ) from exc

    logger.info(
        "规则模板从 YAML 加载（仓储未就绪降级） | 合同类型=%s | 模板数=%d",
        state.contract_type,
        len(templates),
    )
    return list(templates or [])




# ======================================================================
# 节点四：vector_review（候选语义，无定级权）
# ======================================================================
@node_wrapper(node_name=WorkflowNode.VECTOR_REVIEW, progress=65)
async def vector_review(state: ReviewTaskState) -> NodeResult:
    """
    节点：向量语义召回审查

    【职责】
        1. 将结构化条款向量化（BGE-M3）
        2. 在 Milvus 标准条款库中检索语义相近的标准条款
        3. 产出 VectorCandidate 候选列表（仅含相似度与建议方向）

    【入参】
        state: 图状态，需已含 clauses / task_id

    【出参】
        NodeResult，updates 含 vector_results / vector_recall_count

    【异常】
        EmbeddingException / MilvusException：由包装器指数退避重试，
                                         重试耗尽后置 blocked(RETRY_EXHAUSTED)

    【架构红线】
        本节点的产物 VectorCandidate 在数据结构上不含 risk_level 字段，
        仅提供"相似条款线索"。相似度高不代表风险高——是否构成风险，
        必须由规则引擎依据规则模板判定。
    """
    # -------------------- 前置判定：快速通道跳过 --------------------
    if not state.needs_vector_review:
        return NodeResult(
            skipped=True,
            skip_reason=(
                f"路由类型={state.route_type}，"
                f"不执行向量语义召回（仅标准审查与深度复核通道需要）"
            ),
            updates={},
        )

    if not state.clauses:
        return NodeResult(
            updates={"vector_results": [], "vector_recall_count": 0},
            message="无结构化条款，向量召回跳过",
            detail={"clause_count": 0},
        )

    # -------------------- 步骤一：委托审查服务执行向量召回 --------------------
    # 【架构说明】向量化 + Milvus 检索能力已下沉到 app.engines.vector_engine，
    # 本节点只做编排与状态回填，不再自行实现检索逻辑。
    from app.services import review_service

    try:
        candidates = await review_service.vector_review_service(state.clauses)
    except Exception as exc:  # noqa: BLE001 - 向量召回失败转为可重试异常
        logger.error(
            "向量语义召回失败 | 任务=%s | 异常=%s", state.task_no, exc, exc_info=True
        )
        raise IOError(f"向量语义召回失败：{exc}") from exc

    return NodeResult(
        updates={
            "vector_results": candidates,
            "vector_recall_count": len(candidates),
        },
        message=(
            f"向量语义召回完成 | 条款数={len(state.clauses)}条 | "
            f"召回候选={len(candidates)}条 | 标准条款库={settings.MILVUS_COLLECTION_STANDARD_CLAUSE}"
        ),
        detail={
            "clause_count": len(state.clauses),
            "recall_count": len(candidates),
            "collection": settings.MILVUS_COLLECTION_STANDARD_CLAUSE,
            "metric_type": settings.MILVUS_METRIC_TYPE,
            "ef": settings.MILVUS_SEARCH_PARAMS_EF,
            "top_k": settings.MILVUS_SEARCH_TOP_K,
        },
    )


# ======================================================================
# 节点五：llm_review（候选语义，无定级权）
# ======================================================================
@node_wrapper(node_name=WorkflowNode.LLM_ANALYZE, progress=75)
async def llm_review(state: ReviewTaskState) -> NodeResult:
    """
    节点：大模型辅助审查

    【职责】
        1. 对条款执行语义解析，识别疑似风险点
        2. 生成修改建议初稿
        3. 解读相关合规条文
        产出 LLMCandidate 候选列表，供风险融合与人工复核参考

    【入参】
        state: 图状态，需已含 clauses / task_id / contract_type

    【出参】
        NodeResult，updates 含 llm_results / llm_raw_summary / llm_called

    【异常】
        LLMException：由包装器指数退避重试，重试耗尽后置 blocked(RETRY_EXHAUSTED)

    【架构红线（本节点的强制约束）】
        1. 提示词中明确要求模型"不得输出风险等级、不得给出通过/驳回结论"。
        2. 产出结构 LLMCandidate 不含 risk_level / decision / severity 字段，
           模型即使返回这些字段也会在解析时被丢弃（见 _parse_llm_candidates）。
        3. 本节点的任何产出都不参与 rule_review 的定级计算，
           仅作为 risk_merge 阶段的参考项。

    【深度复核通道的额外动作】
        当 route_type == deep 时，提示词中追加"高风险合同强化审查清单"，
        要求模型逐项核查担保、违约、解除、争议解决等关键条款，
        输出更完整的候选清单供法务复核。
    """
    # -------------------- 前置判定 --------------------
    if not state.needs_llm_review:
        return NodeResult(
            skipped=True,
            skip_reason=(
                f"路由类型={state.route_type}，"
                f"不执行大模型辅助审查（快速通道仅跑结构化规则）"
            ),
            updates={"llm_called": False},
        )

    if not state.clauses:
        return NodeResult(
            updates={"llm_results": [], "llm_raw_summary": "", "llm_called": False},
            message="无结构化条款，大模型审查跳过",
            detail={"clause_count": 0},
        )

    # -------------------- 步骤一：委托审查服务执行大模型辅助审查 --------------------
    # 【架构说明】提示词构造、大模型调用、候选解析、越权字段拦截、失败降级
    # 能力已下沉到 app.engines.llm_engine，本节点只做编排与状态回填。
    # 【失败降级】大模型是辅助能力，调用失败时审查服务返回 failed=True
    # 而非抛异常，本节点记录告警后正常返回，不中断以规则引擎为兜底的主流程。
    from app.services import review_service

    contract_context = {
        "contract_title": state.contract_title,
        "contract_no": state.contract_no,
        "contract_type": state.contract_type,
        "business_type": state.business_type,
        "contract_amount": state.contract_amount,
        "currency": state.currency,
        "submit_dept": state.submit_dept,
        "counterparty": state.counterparty,
        "performance_period": state.performance_period,
    }

    result = await review_service.llm_review_service(
        clauses=state.clauses,
        contract_context=contract_context,
        is_deep=state.is_route_deep,
    )

    candidates = result.candidates
    dropped_fields = result.dropped_fields
    llm_summary = result.summary

    # 越权字段被丢弃时必须有明确告警，这是红线被触发的信号
    if dropped_fields:
        logger.warning(
            "【红线防御】大模型输出包含越权判定字段，已丢弃 | 任务=%s | 丢弃字段=%s",
            state.task_no,
            dropped_fields,
        )

    # 降级失败时记录告警（不中断主流程）
    if result.failed:
        logger.warning(
            "大模型辅助审查降级（不中断主流程） | 任务=%s | 原因=%s",
            state.task_no,
            result.error,
        )

    return NodeResult(
        updates={
            "llm_results": candidates,
            "llm_raw_summary": llm_summary,
            "llm_called": True,
        },
        message=(
            f"大模型辅助审查{'降级完成' if result.failed else '完成'} | 模型={settings.LLM_MODEL_NAME} | "
            f"疑似风险候选={len(candidates)}项 | 越权字段丢弃={len(dropped_fields)}个"
        ),
        detail={
            "model_name": settings.LLM_MODEL_NAME,
            "temperature": settings.LLM_TEMPERATURE,
            "candidate_count": len(candidates),
            "dropped_illegal_fields": dropped_fields,
            "is_deep_channel": state.is_route_deep,
            "clause_count": len(state.clauses),
            "degraded": result.failed,
            "degrade_reason": result.error,
        },
    )



# ======================================================================
# 节点六：risk_merge
# ======================================================================
@node_wrapper(node_name=WorkflowNode.RISK_MERGE, progress=82)
async def risk_merge(state: ReviewTaskState) -> NodeResult:
    """
    节点：多路召回风险融合

    【职责】
        1. 汇总四路结果：结构化规则、语义向量、大模型、字段比较
        2. 按条款维度去重合并，避免同一问题重复计数
        3. 冲突裁决：规则引擎优先级 > 语义/模型候选；
           同级冲突取更高风险等级（保守从严）
        4. 未能与规则命中关联的候选，作为"参考项"保留（reference_only=True），
           不计入定级统计，但展示给人工复核
        5. 重新统计风险等级分布

    【入参】
        state: 图状态，需已含 rule_results / vector_results / llm_results

    【出参】
        NodeResult，updates 含：
            final_risk_items / risk_distribution / merge_summary

    【异常】
        无。

    【架构红线（本节点的强制约束）】
        1. 融合过程绝不生成新的风险等级。所有 risk_level 均直接复制自
           rule_results（规则引擎产出）。
        2. 语义候选与大模型候选的"等级"字段即使存在也一律丢弃——
           它们的载体模型 VectorCandidate / LLMCandidate 本身就不含该字段，
           此处是第二重保障。
        3. 参考项 reference_only=True 必须在 UI 与报告中明确标注，
           避免被误认为正式判定结论。
    """
    # -------------------- 委托审查服务执行三路融合 --------------------
    # 【架构说明】去重合并、冲突裁决、分布重算、整体综合风险计算能力
    # 已下沉到 app.services.review_service.risk_merge_service。
    # 融合以规则引擎为准兜底：综合风险 = 规则命中项的最高风险等级，
    # 向量/大模型候选不参与定级。
    from app.services import review_service

    merge_result = review_service.risk_merge_service(
        rule_results=state.rule_results,
        vector_results=state.vector_results,
        llm_results=state.llm_results,
    )

    final_items = merge_result.final_items
    distribution = merge_result.risk_distribution
    merge_summary = merge_result.merge_summary

    return NodeResult(
        updates={
            "final_risk_items": final_items,
            "risk_distribution": distribution,
            "merge_summary": merge_summary,
        },
        message=merge_summary,
        detail={
            "rule_hit_count": merge_result.official_count,
            "reference_count": merge_result.reference_count,
            "dedup_count": merge_result.dedup_count,
            "conflict_count": merge_result.conflict_count,
            "risk_distribution": distribution,
            "overall_risk_level": merge_result.overall_risk_level,
            "final_risk_level": state.risk_level,
            "vector_candidate_count": len(state.vector_results),
            "llm_candidate_count": len(state.llm_results),
        },
    )


# ======================================================================
# 节点七：generate_report
# ======================================================================
@node_wrapper(node_name=WorkflowNode.GENERATE_REPORT, progress=88)
async def generate_report(state: ReviewTaskState) -> NodeResult:
    """
    节点：审查报告与修改建议生成

    【职责】
        1. 汇总风险项，生成结构化修改建议文本
        2. 执行生成后五维校验（确定性部分：结论完整性、法条引用、条款完整性）
        3. 校验失败时自动修正（最多 QUALITY_MAX_FIX_ROUNDS 轮）
        4. 渲染审查报告并上传至 MinIO 报告桶
        5. 生成预签名访问链接

    【入参】
        state: 图状态，需已含 final_risk_items / conclusion_text / risk_level

    【出参】
        NodeResult，updates 含：
            suggestion_text / quality_check_passed / quality_fix_rounds /
            report_object_key / report_url / report_generated

    【异常】
        MinIOException：由包装器重试，重试耗尽后置 blocked(RETRY_EXHAUSTED)

    【质量闭环设计】
        生成前：注入规则约束（结论文本只允许来自规则引擎模板）
        生成后：五维校验 → 未通过则自动修正（最多 2 轮）→ 仍未通过则标记待人工处理
        报告生成不是判定环节，质量校验只影响"呈现质量"，不改变风险等级。
    """
    # -------------------- 委托报告服务生成审查报告 --------------------
    # 【架构说明】建议汇总、质量校验、报告渲染、MinIO 上传能力已下沉到
    # app.services.report_service，本节点只做编排与状态回填。
    from app.services import report_service

    try:
        result = await report_service.generate_report_service(state)
    except Exception as exc:  # noqa: BLE001 - 报告生成失败转为可重试异常
        logger.error(
            "审查报告生成失败 | 任务=%s | 异常=%s", state.task_no, exc, exc_info=True
        )
        raise IOError(f"审查报告生成失败：{exc}") from exc

    report_bytes = result.report_content.encode("utf-8")

    return NodeResult(
        updates={
            "suggestion_text": result.suggestion_text,
            "quality_check_passed": result.quality_passed,
            "quality_fix_rounds": result.quality_fix_rounds,
            "report_object_key": result.report_object_key,
            "report_url": result.report_url,
            "report_generated": True,
        },
        message=(
            f"审查报告生成完成 | 报告大小={len(report_bytes)}字节 | "
            f"质量校验={'通过' if result.quality_passed else '未通过'} | "
            f"修正轮次={result.quality_fix_rounds}"
        ),
        detail={
            "report_object_key": result.report_object_key,
            "report_size_bytes": len(report_bytes),
            "report_bucket": settings.MINIO_BUCKET_REPORT,
            "quality_passed": result.quality_passed,
            "quality_fix_rounds": result.quality_fix_rounds,
            "suggestion_length": len(result.suggestion_text),
            "risk_item_count": result.risk_item_count,
        },
    )



# ======================================================================
# 节点八：generate_comment
# ======================================================================
@node_wrapper(node_name=WorkflowNode.GENERATE_COMMENT, progress=92)
async def generate_comment(state: ReviewTaskState) -> NodeResult:
    """
    节点：构造审批评论区内容

    【职责】
        1. 校验回写前置条件（规则引擎已判定、任务未阻塞）
        2. 构造审批评论区文本（含任务号、风险等级、结论、风险明细、报告链接）
        3. 校验接口路径命中白名单（approval_guard.ensure_comment_only）
        4. 输出待回写的评论内容，交由 write_comment 节点提交

    【入参】
        state: 图状态，需已含 conclusion_text / risk_level / final_risk_items

    【出参】
        NodeResult，updates 含 write_back_status / write_back_content

    【异常】
        ApprovalPermissionException：接口越权时抛出（不可重试，直接阻塞）

    【架构红线】
        本节点构造的内容只能是"评论"。任何试图构造审批动作
        （同意/拒绝/转交）的实现都会在白名单校验处被拦截。
    """
    # -------------------- 前置判定：是否需要回写 --------------------
    if not state.needs_write_back:
        reason_parts: List[str] = []
        if not state.rule_evaluated:
            reason_parts.append("规则引擎尚未完成判定")
        if state.is_blocked:
            reason_parts.append(
                f"任务已阻塞（原因={state.blocked_info.reason if state.blocked_info else '未知'}）"
            )
        if not reason_parts:
            reason_parts.append("前置条件不满足")

        return NodeResult(
            skipped=True,
            skip_reason=f"不执行审批回写：{'；'.join(reason_parts)}",
            updates={
                "write_back_status": WriteBackState.NOT_WRITTEN.value,
            },
        )

    # -------------------- 委托评论服务构造审批评论 --------------------
    # 【架构说明】评论模板拼装、格式校验、接口白名单校验能力已下沉到
    # app.services.comment_service，本节点只做编排与状态回填。
    from app.services import comment_service

    try:
        comment_result = await comment_service.generate_comment_service(state)
    except Exception as exc:  # noqa: BLE001 - 越权必须阻塞，不允许重试绕过
        logger.error(
            "【红线拦截】审批评论构造失败 | 任务=%s | 异常=%s",
            state.task_no, exc,
        )
        raise NonRetryableError(
            reason=BlockedReason.MANUAL.value,
            message=(
                f"审批评论构造失败：{exc}。"
                f"系统仅允许写入审批评论区，严禁调用改变审批结论的接口。"
            ),
            detail={"task_no": state.task_no},
            retriable=False,
        ) from exc

    comment_text = comment_result.comment_text

    # -------------------- 构造回写信息载体 --------------------
    from app.graph.state import WriteBackInfo

    write_back_info = WriteBackInfo(
        status=WriteBackState.WRITING.value,
        comment_payload=comment_result.payload,
        comment_text=comment_text,
        api_path=comment_result.api_path,
    )

    # 格式校验未通过时告警（不阻断，供法务二次编辑时参考）
    if not comment_result.validation_passed:
        logger.warning(
            "审批评论格式校验未通过 | 任务=%s | 问题=%s",
            state.task_no, comment_result.validation_issues,
        )

    return NodeResult(
        updates={
            "write_back_status": WriteBackState.WRITING.value,
            "write_back_content": write_back_info,
        },
        message=(
            f"审批评论内容构造完成 | 接口路径={comment_result.api_path}"
            f"（已通过白名单校验）| 风险项={comment_result.risk_item_count}项 | "
            f"评论长度={len(comment_text)}字符 | "
            f"格式校验={'通过' if comment_result.validation_passed else '未通过'}"
        ),
        detail={
            "api_path": comment_result.api_path,
            "comment_length": len(comment_text),
            "risk_item_count": comment_result.risk_item_count,
            "validation_passed": comment_result.validation_passed,
            "validation_issues": comment_result.validation_issues,
            "reference_items_excluded": sum(
                1 for item in state.final_risk_items if item.reference_only
            ),
            "report_url_included": bool(state.report_url),
            "comment_preview": comment_text[:300],
        },
    )


# ======================================================================
# 节点九：write_comment
# ======================================================================
@node_wrapper(node_name=WorkflowNode.WRITE_BACK, progress=96)
async def write_comment(state: ReviewTaskState) -> NodeResult:
    """
    节点：审批评论区回写

    【职责】
        1. 二次校验接口路径白名单（防御性，防止上游被绕过）
        2. 向审批系统评论区提交审查意见
        3. 记录接口返回信息，成功则状态置 success，失败则置 failed

    【入参】
        state: 图状态，需已含 write_back_content

    【出参】
        NodeResult，updates 含 write_back_status / write_back_content /
               write_back_retry_count

    【异常】
        无。回写失败属于业务结果，记录后继续流程收敛，
        由系统管理员通过重推接口人工处理。
        （这一点很重要：回写失败不应阻塞审查结论的产出与报告下载。）

    【绝对禁止项（代码级强制）】
        本节点只允许 POST 到白名单内的评论接口。
        请求体只包含评论内容，绝不能携带任何审批动作字段
        （action / approve / reject 等）。若上游被篡改导致请求体包含
        这些字段，本节点会主动剔除并记录 ERROR 日志。
    """
    # -------------------- 前置幂等判定：已回写则跳过 --------------------
    if state.write_back_status in (
        WriteBackState.SUCCESS.value,
        WriteBackState.NOT_WRITTEN.value,
    ):
        return NodeResult(
            updates={},
            message=f"回写状态为 {state.write_back_status}，无需重复提交（幂等跳过）",
            detail={"write_back_status": state.write_back_status},
        )

    # -------------------- 委托评论服务执行回写 --------------------
    # 【架构说明】幂等校验、接口白名单校验、请求体清洗、预览/正式模式、
    # 失败重试能力已下沉到 app.services.comment_service，本节点只做编排。
    from app.services import comment_service

    preview_mode = settings.APPROVAL_WRITE_BACK_MODE == "preview"

    try:
        write_back_info = await comment_service.write_comment_service(
            state, preview=preview_mode
        )
    except Exception as exc:  # noqa: BLE001 - 二次校验失败立即终止
        logger.error(
            "【红线拦截】回写二次校验失败 | 任务=%s | 异常=%s", state.task_no, exc
        )
        raise NonRetryableError(
            reason=BlockedReason.MANUAL.value,
            message=f"回写接口二次校验失败，流程终止：{exc}",
            detail={"task_no": state.task_no},
            retriable=False,
        ) from exc

    status = write_back_info.status

    # 依据回写结果构造返回
    if status == WriteBackState.SUCCESS.value:
        message = (
            f"审批评论区回写成功 | 接口={write_back_info.api_path} | "
            f"HTTP={write_back_info.http_status}"
            + (f" | 业务码={write_back_info.response_code}" if write_back_info.response_code is not None else "")
        )
    elif status == WriteBackState.WRITING.value:
        message = (
            f"审批评论回写预览模式 | 接口={write_back_info.api_path} | "
            f"内容已生成未实际写入"
        )
    else:
        message = f"审批评论区回写失败 | 接口={write_back_info.api_path} | {write_back_info.error}"

    return NodeResult(
        updates={
            "write_back_status": status,
            "write_back_content": write_back_info,
            "write_back_retry_count": write_back_info.retry_count,
        },
        message=message,
        detail={
            "http_status": write_back_info.http_status,
            "response_code": write_back_info.response_code,
            "api_path": write_back_info.api_path,
            "comment_length": len(write_back_info.comment_text),
            "preview": preview_mode,
            "error": write_back_info.error,
        },
    )


# ======================================================================
# 节点十：audit
# ======================================================================
@node_wrapper(node_name=WorkflowNode.AUDIT, progress=100)
async def audit(state: ReviewTaskState) -> NodeResult:
    """
    节点：流程审计与收敛

    【职责】
        1. 汇总全流程审计链路，统计各阶段耗时与节点执行情况
        2. 确定任务终态（blocked / failed / completed）
        3. 校验关键产出完整性（结论哈希、风险项、报告）
        4. 对阻塞任务给出明确的后续处理指引
        5. 输出流程执行摘要，供前端展示与运维监控

    【入参】
        state: 图状态（此时已有完整的执行历史与产出）

    【出参】
        NodeResult，updates 含：
            status / finished_at / progress / 审计汇总字段

    【异常】
        无。审计节点必须始终成功，否则流程无法收敛。

    【终态判定规则（严格遵循架构约束）】
        blocked   ：规则引擎判定阻塞，或节点重试耗尽，或解析被阻断
        failed    ：文档解析技术性失败
        completed ：正常完成（含回写失败但审查结论已产出的情况）
    """
    from app.graph.node_utils import finalize_state

    # -------------------- 步骤一：完整性校验 --------------------
    integrity_issues: List[str] = []

    if state.rule_evaluated and not state.conclusion_hash:
        integrity_issues.append("规则引擎已判定但未生成结论哈希，结论不可采信")

    if state.rule_evaluated and not state.risk_level:
        integrity_issues.append("规则引擎已判定但未产出综合风险等级")

    if state.final_risk_items and not state.merge_summary:
        integrity_issues.append("存在最终风险项但缺少融合摘要，融合过程不可追溯")

    if state.report_generated and not state.report_object_key:
        integrity_issues.append("报告标记为已生成但缺少对象键，报告可能未实际上传")

    if integrity_issues:
        logger.error(
            "流程产出完整性校验未通过 | 任务=%s | 问题=%s",
            state.task_no,
            integrity_issues,
        )

    # -------------------- 步骤二：统计审计链路 --------------------
    node_stats: Dict[str, Dict[str, Any]] = {}
    for record in state.node_history:
        stats = node_stats.setdefault(
            record.node,
            {"status": record.status, "attempts": 0, "retries": 0, "elapsed_ms": 0.0},
        )
        stats["attempts"] += 1
        stats["retries"] += record.retry_count
        stats["elapsed_ms"] += record.elapsed_ms or 0.0
        # 只要有一次非 success，就记录为异常态，便于运维定位
        if record.status != "success":
            stats["status"] = record.status

    total_elapsed_ms: Optional[float] = None
    if state.started_at is not None:
        end_time = state.finished_at or datetime.now()
        total_elapsed_ms = (end_time - state.started_at).total_seconds() * 1000.0

    # -------------------- 步骤三：确定终态 --------------------
    updates = finalize_state(
        state=state,
        node=WorkflowNode.AUDIT.value,
        message=(
            f"流程收敛完成 | 最终状态={state.status} | 综合等级={state.risk_level} | "
            f"阻塞={state.is_blocked} | 有效风险={state.official_risk_count}项 | "
            f"参考项={state.total_risk_count - state.official_risk_count}项"
        ),
        detail={
            "node_stats": node_stats,
            "integrity_issues": integrity_issues,
            "exhausted_nodes": state.exhausted_nodes,
            "skipped_nodes": state.skipped_nodes,
            "total_audit_logs": len(state.audit_logs),
            "total_elapsed_ms": total_elapsed_ms,
        },
    )

    # -------------------- 步骤四：阻塞任务的后续指引 --------------------
    if state.is_blocked and state.blocked_info:
        handling_guide = _build_blocked_guide(state)
        updates["error_message"] = (
            f"{state.error_message}｜处理指引：{handling_guide}"
            if state.error_message
            else f"处理指引：{handling_guide}"
        )

    return NodeResult(
        updates=updates,
        message=(
            f"流程审计完成 | 任务={state.task_no} | 最终状态={updates['status']} | "
            f"审计日志={len(state.audit_logs)}条 | 节点执行={len(state.node_history)}次 | "
            f"总耗时={(total_elapsed_ms / 1000.0) if total_elapsed_ms is not None else 0.0:.2f}s"
        ),
        detail={
            "final_status": updates["status"],
            "node_stats": node_stats,
            "integrity_issues": integrity_issues,
            "audit_log_count": len(state.audit_logs),
            "node_execution_count": len(state.node_history),
            "total_elapsed_ms": total_elapsed_ms,
            "blocked_reason": (
                state.blocked_info.reason if state.blocked_info else None
            ),
        },
    )


def _build_blocked_guide(state: ReviewTaskState) -> str:
    """
    依据阻塞原因码生成人工处理指引

    说明：
        阻塞任务必须给出可执行的处理建议，避免运维人员看到"blocked"
        却不知如何处理。指引内容按原因码确定性映射，不含模型生成内容。

    参数：
        state: 当前图状态

    返回：
        处理指引文本

    异常：
        ValueError：blocked_info 缺失时抛出（该场景说明状态异常）
    """
    if state.blocked_info is None:
        return "任务处于阻塞状态但缺少阻塞信息，请联系系统管理员核查状态一致性"

    guides: Dict[str, str] = {
        BlockedReason.DOC_ENCRYPTED.value: (
            "文档为加密文件。请业务经办人解除文档加密后重新上传，"
            "或在审批系统中替换为未加密版本，然后由系统管理员触发任务重跑。"
        ),
        BlockedReason.SCAN_TOO_BLURRY.value: (
            "扫描件影像模糊，识别置信度低于系统阈值。"
            "请业务经办人重新扫描（建议 300dpi 以上、确保页面平整无阴影）后重新上传，"
            "再由系统管理员触发任务重跑。"
        ),
        BlockedReason.EMPTY_CONTENT.value: (
            "文档抽取正文为空或长度不足。请确认上传的是合同正文文件"
            "（而非封面扫描件、空模板或图片压缩包），替换文件后触发任务重跑。"
        ),
        BlockedReason.PARSE_FAILED.value: (
            "文档解析失败，可能是文件损坏或格式不受支持。"
            "请确认文件可正常打开，必要时另存为标准 PDF 后重新上传。"
        ),
        BlockedReason.RETRY_EXHAUSTED.value: (
            f"节点重试次数耗尽，涉及节点：{state.exhausted_nodes}。"
            "请系统管理员先检查下游服务（MongoDB / MinIO / Milvus / 本地大模型服务）"
            "的运行状态与网络连通性，确认恢复正常后触发任务重跑。"
        ),
        BlockedReason.MANUAL.value: (
            f"任务因业务或配置原因被判定阻塞：{state.blocked_info.detail}。"
            "请系统管理员核查规则模板配置与任务输入数据，"
            "确认无误后触发任务重跑。"
        ),
    }

    return guides.get(
        state.blocked_info.reason,
        f"未识别的阻塞原因码 {state.blocked_info.reason}，请联系系统管理员处理",
    )
