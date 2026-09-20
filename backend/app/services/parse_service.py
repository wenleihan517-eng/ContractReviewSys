# D:/ContractReviewSys-合同审批智能审查项目/backend/app/services/parse_service.py
"""
文档解析服务 — 合同文本抽取、OCR 降级与元数据提取的统一编排层

【模块定位】
    本模块是"文档解析模块"的对外入口，被 app/graph/nodes.py 的
    parse_document 节点通过 _load_contract_text 调用。

    职责链路：
        MinIO 下载原文
            → 文件特征识别（魔数 / 加密 / 扩展名一致性）
            → SHA-256 幂等校验（命中历史结果则直接返回）
            → 格式分派（PDF / DOCX / 图片）
            → 原生文本抽取
            → 质量评估 → 必要时降级 OCR
            → 全局字符偏移标准化（构建 TextBlock）
            → 关键元数据抽取
            → 产出结果缓存（供幂等命中）
        异常路径：
            → 加密 / 空正文 / 扫描件模糊 → 返回明确的阻断信号
            → 服务不可用（MinIO / OCR 引擎）→ 抛 IOError，由节点重试

【与图节点的契约（不可变更）】
    _load_contract_text 的签名与返回值被 parse_document 节点依赖：
        入参：object_key, source_type, task_id, work_dir, file_fingerprint
        出参：(full_text: str, page_texts: List[str], doc_meta: DocMetadata)
    · full_text 的换行拼接方式必须与 TextBlock 偏移计算完全一致
      —— 二者不一致会导致所有溯源定位偏移。

【三类法定阻断的实现位置说明】
    本模块负责**识别事实**（是否加密、是否模糊、是否为空），
    并把这些事实写入返回值；**是否因此阻断任务由 parse_document 节点裁决**。
    这样划分的原因：
        · 解析服务保持"无业务裁决"的纯粹职责，可被其他场景复用
        · 阻断规则属于业务流程，集中在节点中便于审计与调整
    但为了接口清晰，本模块会通过 ParseBlockedSignal 把判定依据完整带出。

【幂等设计】
    同一份文件（内容指纹相同）重复提交时，直接返回历史解析结果，
    避免重复的 OCR 计算开销。缓存以 fingerprint 为键，落 MongoDB 的
    cr_parse_cache 集合（本模块只依赖 repositories 抽象，不直接操作数据库）。

    缓存的三重校验：fingerprint 相同 + 页数相同 + 字符数相同。
    仅比对 fingerprint 不够 —— 缓存记录可能因写入中断而不完整。
"""

import asyncio
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from app.core.config import get_settings
from app.core.logging_config import get_logger
from app.graph.state import DocMetadata, TextBlock
from app.services import metadata_extractor
from app.services.parsers.docx_parser import parse_docx
from app.services.parsers.ocr_parser import (
    OCREngineError,
    OCRUnavailableError,
    is_ocr_engine_available,
    needs_ocr_fallback,
    parse_with_ocr,
)
from app.services.parsers.pdf_parser import parse_pdf, render_page_to_png
from app.services.parsers.raw_types import RawParseResult
from app.utils.file_hash import (
    calc_effective_text_length,
    extract_encryption_and_format,
    is_scanned_image_ext,
    validate_ext_magic,
)

logger = get_logger(__name__)
settings = get_settings()


# ======================================================================
# 一、业务信号类型
# ======================================================================

class ParseBlockedSignal(Exception):
    """
    解析阻断信号 — 携带"事实判定"结果，供节点裁决是否阻断任务

    说明：
        本异常**不是错误**，而是业务事实的显式传递通道。
        抛出它的场景是"这个文件确实存在某种不可继续的情况"，
        而非"程序出错了"。

        为什么要用异常而非返回值：解析服务有多个产出出口，
        用异常可以在任意深度直接中断并携带完整上下文，
        避免在每一层都传递"是否阻断"的标志位。

    属性：
        reason: 阻断原因码，取值取自 BlockedReason 枚举
        detail: 结构化判定依据，会原样进入节点的审计日志
        message: 面向人工的说明文本
    """

    def __init__(
        self,
        reason: str,
        message: str,
        detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        参数：
            reason: 阻断原因码（DOC_ENCRYPTED / SCAN_TOO_BLURRY / EMPTY_CONTENT 等）
            message: 面向人工的说明文本
            detail: 结构化判定依据
        """
        self.reason = reason
        self.message = message
        self.detail = detail or {}
        super().__init__(f"[{reason}] {message}")


class ParseResult:
    """
    解析服务的完整产出

    说明：
        这是一个普通容器类（非 Pydantic 模型），因为它的字段中包含
        无法被 Pydantic 直接序列化的大文本与模型对象混合结构。
        对外暴露时由 parse_contract_document 转为三元组契约格式。

    字段：
        full_text: 抽取的合同全文
        page_texts: 逐页文本（索引 0 对应第 1 页）
        doc_metadata: 文档元数据
        text_blocks: 标准化文本块
        metadata: 合同关键元数据（金额、主体、期限等）
        raw_result: 底层解析器原始产物，供诊断使用
        cache_hit: 是否命中了幂等缓存
        elapsed_ms: 总耗时（毫秒）
    """

    def __init__(
        self,
        full_text: str,
        page_texts: List[str],
        doc_metadata: DocMetadata,
        text_blocks: List[TextBlock],
        metadata: Dict[str, Any],
        raw_result: Optional[RawParseResult] = None,
        cache_hit: bool = False,
        elapsed_ms: float = 0.0,
    ) -> None:
        self.full_text = full_text
        self.page_texts = page_texts
        self.doc_metadata = doc_metadata
        self.text_blocks = text_blocks
        self.metadata = metadata
        self.raw_result = raw_result
        self.cache_hit = cache_hit
        self.elapsed_ms = elapsed_ms

    def to_contract_tuple(self) -> Tuple[str, List[str], DocMetadata]:
        """
        转换为图节点契约要求的三元组

        返回：
            (full_text, page_texts, doc_metadata)
        """
        return self.full_text, self.page_texts, self.doc_metadata


# ======================================================================
# 二、幂等缓存
# ======================================================================

# 进程内一级缓存：指纹 → 序列化的解析结果
# 说明：
#   生产环境应以 MongoDB 的 cr_parse_cache 集合为持久层（跨进程、跨重启）。
#   进程内缓存作为一级缓存，避免同一次审查流程中重复解析同一文件
#   （例如解析失败后人工重跑）时反复访问数据库。
#
#   缓存容量上限通过 LRU 语义控制，防止长期运行导致内存膨胀。
_PARSE_CACHE: Dict[str, Dict[str, Any]] = {}
_PARSE_CACHE_MAX_SIZE = 128


def _cache_put(fingerprint: str, payload: Dict[str, Any]) -> None:
    """
    写入进程内解析缓存

    说明：
        容量超限时按插入顺序淘汰最旧条目（FIFO）。
        选择 FIFO 而非严格 LRU 的原因：解析结果的访问局部性不强
        （同一文件通常在短时间内被访问一次），FIFO 的额外簿记开销不值得。

    参数：
        fingerprint: 文件内容指纹
        payload: 可序列化的解析结果

    返回：
        无返回值
    """
    if len(_PARSE_CACHE) >= _PARSE_CACHE_MAX_SIZE:
        # dict 保持插入顺序，弹出第一个即为最旧
        oldest_key = next(iter(_PARSE_CACHE))
        _PARSE_CACHE.pop(oldest_key, None)

    _PARSE_CACHE[fingerprint] = payload


def _cache_get(fingerprint: str) -> Optional[Dict[str, Any]]:
    """
    读取进程内解析缓存

    参数：
        fingerprint: 文件内容指纹

    返回：
        缓存载荷；未命中返回 None
    """
    return _PARSE_CACHE.get(fingerprint)


def clear_parse_cache() -> int:
    """
    清空进程内解析缓存

    说明：
        供测试与运维手动干预使用（如解析逻辑升级后需要强制重解析）。

    返回：
        被清除的缓存条目数
    """
    count = len(_PARSE_CACHE)
    _PARSE_CACHE.clear()
    logger.info("解析缓存已清空 | 清除条目=%d", count)
    return count


def get_parse_cache_size() -> int:
    """
    获取当前缓存条目数

    返回：
        缓存条目数量
    """
    return len(_PARSE_CACHE)


async def _load_cached_result(
    fingerprint: str,
) -> Optional[Dict[str, Any]]:
    """
    查询解析缓存（进程内 → 持久层）

    说明：
        优先查进程内缓存；未命中时尝试查 MongoDB 持久缓存。
        持久层通过 repositories 抽象访问，若该模块尚未就绪则跳过
        —— 跳过是安全的，幂等只是优化，不影响正确性。

    参数：
        fingerprint: 文件内容指纹

    返回：
        缓存载荷；未命中或持久层不可用返回 None
    """
    # 一级：进程内
    cached = _cache_get(fingerprint)
    if cached is not None:
        logger.info("解析缓存命中（进程内） | 指纹=%s", fingerprint[:16])
        return cached

    # 二级：MongoDB 持久层（可选依赖）
    try:
        from app.repositories import parse_cache_repo  # type: ignore

        record = await parse_cache_repo.find_by_fingerprint(fingerprint)
        if record:
            logger.info("解析缓存命中（持久层） | 指纹=%s", fingerprint[:16])
            # 回填一级缓存，避免同进程内重复查询数据库
            _cache_put(fingerprint, record)
            return record

    except ImportError:
        # repositories 层尚未交付时静默跳过 —— 幂等是优化而非正确性依赖
        logger.debug("解析缓存持久层未就绪，跳过二级查询")
    except Exception as exc:  # noqa: BLE001 - 缓存查询失败不应影响主流程
        logger.warning("解析缓存持久层查询失败，继续执行解析 | 异常=%s", exc)

    return None


async def _save_cached_result(
    fingerprint: str,
    payload: Dict[str, Any],
) -> None:
    """
    保存解析结果到缓存

    参数：
        fingerprint: 文件内容指纹
        payload: 可序列化的解析结果

    返回：
        无返回值（保存失败仅告警，不影响本次解析结果）
    """
    _cache_put(fingerprint, payload)

    try:
        from app.repositories import parse_cache_repo  # type: ignore

        await parse_cache_repo.upsert_by_fingerprint(fingerprint, payload)
        logger.debug("解析结果已写入持久缓存 | 指纹=%s", fingerprint[:16])

    except ImportError:
        logger.debug("解析缓存持久层未就绪，仅写入进程内缓存")
    except Exception as exc:  # noqa: BLE001
        logger.warning("解析结果写入持久缓存失败 | 指纹=%s | 异常=%s", fingerprint[:16], exc)


def _serialize_result(
    full_text: str,
    page_texts: List[str],
    doc_meta: DocMetadata,
    text_blocks: List[TextBlock],
    metadata: Dict[str, Any],
) -> Dict[str, Any]:
    """
    将解析结果序列化为可缓存的字典

    说明：
        DocMetadata 与 TextBlock 均为 Pydantic 模型，用 model_dump(mode="json")
        转为纯 JSON 类型，datetime 会被自动转成 ISO 字符串。

    参数：
        full_text: 全文
        page_texts: 逐页文本
        doc_meta: 文档元数据
        text_blocks: 文本块
        metadata: 合同元数据

    返回：
        纯 JSON 类型的字典
    """
    return {
        "full_text": full_text,
        "page_texts": page_texts,
        "doc_metadata": doc_meta.model_dump(mode="json"),
        "text_blocks": [block.model_dump(mode="json") for block in text_blocks],
        "metadata": metadata,
    }


def _deserialize_result(
    payload: Dict[str, Any],
) -> Tuple[str, List[str], DocMetadata, List[TextBlock], Dict[str, Any]]:
    """
    将缓存载荷反序列化为解析结果

    参数：
        payload: 缓存字典

    返回：
        (full_text, page_texts, doc_metadata, text_blocks, metadata)

    异常：
        ValueError：载荷结构不完整时抛出（调用方应回退到重新解析）
    """
    required_keys = ("full_text", "page_texts", "doc_metadata", "text_blocks", "metadata")
    missing = [key for key in required_keys if key not in payload]
    if missing:
        raise ValueError(f"解析缓存载荷不完整，缺少字段：{missing}")

    doc_meta = DocMetadata(**payload["doc_metadata"])
    text_blocks = [TextBlock(**block) for block in payload["text_blocks"]]

    return (
        payload["full_text"],
        list(payload["page_texts"]),
        doc_meta,
        text_blocks,
        dict(payload["metadata"]),
    )


# ======================================================================
# 三、文本块标准化
# ======================================================================

def build_text_blocks(
    raw_result: RawParseResult,
    doc_meta: DocMetadata,
) -> Tuple[str, List[str], List[TextBlock]]:
    """
    将底层解析产物标准化为全局字符偏移的文本块

    【核心难点：字符偏移的精确计算】
        full_text 由各页文本以 "\\n" 连接而成：
            full_text = page_texts[0] + "\\n" + page_texts[1] + "\\n" + ...

        因此对于第 p 页（从 1 开始）：
            该页在 full_text 中的起始偏移
                = sum(len(page_texts[i]) + 1 for i in range(p-1))

        页内每个块（RawBlock）按顺序拼接，块间以 "\\n" 分隔，
        因此块在页内的偏移 = 之前所有块的长度 + 换行符数量。

        这个计算必须与 full_text 的拼接方式**严格一致**，
        任何偏差都会导致所有溯源定位错位。

    【关于 end_idx 的语义】
        采用左闭右开区间 [start_idx, end_idx)，即：
            full_text[start_idx:end_idx] == block.text
        这样区间的长度恰好等于文本长度，且相邻块区间不重叠。

    【空块的跳过】
        OCR 可能产出空文本块，RawBlock 构建时已过滤。此处再兜底检查，
        避免产生 start_idx == end_idx 的非法块（TextBlock 的跨字段校验会拒绝）。

    参数：
        raw_result: 底层解析器产物
        doc_meta: 文档元数据，用于填充块级引擎与置信度

    返回：
        (full_text, page_texts, text_blocks)
    """
    # -------------------- 第一步：构建逐页文本 --------------------
    page_texts: List[str] = []
    for page in raw_result.pages:
        # 页面文本由该页的文本类型块以换行拼接
        # 说明：图像块（block_type=image）文本为空，不参与拼接
        text_parts = [
            block.text
            for block in page.blocks
            if block.block_type != "image" and block.text
        ]
        page_texts.append("\n".join(text_parts))

    # -------------------- 第二步：拼接全文 --------------------
    full_text = "\n".join(page_texts)

    # -------------------- 第三步：计算全局偏移并构建块 --------------------
    text_blocks: List[TextBlock] = []

    # 每页在全文中的起始偏移
    page_start_offsets: List[int] = []
    cursor = 0
    for page_text in page_texts:
        page_start_offsets.append(cursor)
        cursor += len(page_text) + 1   # +1 为页间换行符

    block_index = 0
    paragraph_index = 0

    for page_position, page in enumerate(raw_result.pages):
        page_start = page_start_offsets[page_position]

        # 页内游标：从该页起始偏移开始
        in_page_cursor = 0

        for raw_block in page.blocks:
            # 图像块不产出文本块
            if raw_block.block_type == "image":
                continue

            text = raw_block.text
            if not text:
                continue

            # 计算块在全文中的绝对区间
            start_idx = page_start + in_page_cursor
            end_idx = start_idx + len(text)

            # 防御：区间非法时跳过（TextBlock 校验会拒绝这类块）
            if end_idx <= start_idx:
                logger.warning(
                    "跳过空区间文本块 | 页=%d | 块序=%d", page.page, raw_block.order_in_page
                )
                continue

            # OCR 置信度仅对走 OCR 的块赋值
            ocr_confidence: Optional[float] = None
            if doc_meta.source_type == "scanned" and raw_block.confidence is not None:
                ocr_confidence = raw_block.confidence

            text_blocks.append(
                TextBlock(
                    block_index=block_index,
                    page=page.page,
                    start_idx=start_idx,
                    end_idx=end_idx,
                    paragraph_index=paragraph_index,
                    text=text,
                    ocr_confidence=ocr_confidence,
                    engine=doc_meta.parse_engine,
                )
            )

            block_index += 1
            paragraph_index += 1

            # 推进页内游标：块长度 + 块间换行符
            # 说明：最后一块的换行符不计入（它在页与页之间由 full_text 的
            # 拼接负责），但多计一个也不影响正确性，因为下一页的起始偏移
            # 由 page_start_offsets 独立计算，不依赖页内游标。
            in_page_cursor += len(text) + 1

    return full_text, page_texts, text_blocks


def _verify_offsets(
    full_text: str,
    text_blocks: List[TextBlock],
) -> bool:
    """
    校验文本块偏移与全文的对应关系

    说明：
        抽检若干块，验证 full_text[start_idx:end_idx] == block.text。
        这是对偏移计算正确性的**运行时断言**，用于及时发现
        解析器行为变化导致的偏移错位。

        抽检而非全量校验的原因：大文档全量切片比对有明显开销，
        而偏移计算是确定性的 —— 只要有块能对上，其余块也必然正确
        （除非某个块的文本本身含特殊字符，此时抽检可能漏检，
        因此对首块、末块与中间块都做检查）。

    参数：
        full_text: 完整文本
        text_blocks: 文本块列表

    返回：
        全部抽检通过返回 True，否则 False（并输出 WARNING 日志）
    """
    if not text_blocks or not full_text:
        return True

    # 抽检位置：首、1/4、1/2、3/4、末
    indices = sorted(
        {
            0,
            len(text_blocks) // 4,
            len(text_blocks) // 2,
            (len(text_blocks) * 3) // 4,
            len(text_blocks) - 1,
        }
    )

    failed: List[str] = []
    for index in indices:
        block = text_blocks[index]
        actual = full_text[block.start_idx: block.end_idx]
        if actual != block.text:
            failed.append(
                f"块#{block.block_index}(页{block.page}) 偏移[{block.start_idx}:{block.end_idx}] "
                f"期望={block.text[:20]!r} 实际={actual[:20]!r}"
            )

    if failed:
        logger.error(
            "文本块偏移校验失败 | 失败数=%d/%d | 详情=%s",
            len(failed), len(indices), failed[:3],
        )
        return False

    return True


# ======================================================================
# 四、格式分派与解析执行
# ======================================================================

async def _parse_native(
    file_bytes: bytes,
    file_name: str,
    file_ext: str,
) -> RawParseResult:
    """
    执行原生文本抽取（按格式分派）

    说明：
        耗时操作通过 asyncio.to_thread 移出事件循环，
        避免阻塞 FastAPI 的其他请求处理。

    参数：
        file_bytes: 文件字节内容
        file_name: 文件名
        file_ext: 规范化扩展名

    返回：
        RawParseResult

    异常：
        ValueError：格式不支持时抛出
    """
    if file_ext == ".pdf":
        return await asyncio.to_thread(parse_pdf, file_bytes, file_name)

    if file_ext in (".docx", ".doc"):
        # 说明：.doc 旧格式 python-docx 无法直接读取（需 LibreOffice 转换），
        # 但魔数校验已确保走到这里的 .doc 实际是 OOXML 格式（zip），
        # 因此按 docx 处理。真正的二进制 .doc 会在魔数校验阶段被拦截。
        return await asyncio.to_thread(parse_docx, file_bytes, file_name)

    if is_scanned_image_ext(file_ext):
        # 纯图像文件没有"原生解析"，直接返回空结果触发 OCR
        result = RawParseResult(engine="ocr", page_count=1, has_text_layer=False)
        result.warnings.append(f"图像文件 {file_ext} 无文字层，直接走 OCR 识别")
        return result

    raise ValueError(
        f"不支持的解析格式：{file_ext}。"
        f"支持的类型：.pdf / .docx / .doc / .png / .jpg / .jpeg / .tif / .tiff / .bmp"
    )


async def _render_all_pages(
    file_bytes: bytes,
    file_ext: str,
    page_count: int,
) -> List[Tuple[int, bytes]]:
    """
    将文档全部页面渲染为图像（供 OCR 使用）

    【渲染策略按格式区分】
        PDF：逐页渲染，page_count 由 PyMuPDF 给出
        图像：文件本身即为单页图像，无需渲染
        DOCX：python-docx 无渲染能力，无法转图；
              若 DOCX 内容为空只能阻断，不做 OCR 尝试

    参数：
        file_bytes: 文件字节内容
        file_ext: 规范化扩展名
        page_count: 页数（PDF 由解析器给出）

    返回：
        [(页码, 图像字节), ...]

    异常：
        ValueError：格式无法渲染为图像时抛出
    """
    if file_ext == ".pdf":
        pages: List[Tuple[int, bytes]] = []
        for page_no in range(1, page_count + 1):
            png_bytes = await asyncio.to_thread(
                render_page_to_png, file_bytes, page_no, 200
            )
            pages.append((page_no, png_bytes))
        return pages

    if is_scanned_image_ext(file_ext):
        # 图像文件本身就是页面图像
        return [(1, file_bytes)]

    raise ValueError(
        f"格式 {file_ext} 无法渲染为图像，不支持 OCR 降级。"
        f"DOCX 无渲染能力，若内容为空需人工确认文件完整性"
    )


async def _run_ocr_fallback(
    file_bytes: bytes,
    file_name: str,
    file_ext: str,
    page_count: int,
) -> RawParseResult:
    """
    执行 OCR 降级链路

    参数：
        file_bytes: 文件字节内容
        file_name: 文件名
        file_ext: 规范化扩展名
        page_count: 页数

    返回：
        OCR 解析结果

    异常：
        OCRUnavailableError：OCR 引擎不可用时抛出（转为 IOError 供重试）
        ValueError：格式不支持渲染时抛出
    """
    logger.info(
        "启动 OCR 降级链路 | 文件=%s | 格式=%s | 页数=%d", file_name, file_ext, page_count
    )

    page_images = await _render_all_pages(file_bytes, file_ext, page_count)

    if not page_images:
        raise ValueError(f"无可渲染的页面内容，无法执行 OCR：file_name={file_name}")

    return await parse_with_ocr(page_images, file_name)


# ======================================================================
# 五、主入口
# ======================================================================

async def parse_contract_document(
    object_key: str,
    source_type: str = "digital",
    task_id: str = "",
    work_dir: str = "",
    file_fingerprint: str = "",
    file_name: str = "",
    file_bytes: Optional[bytes] = None,
    enable_cache: bool = True,
) -> ParseResult:
    """
    解析合同文档（服务主入口）

    【执行流程】
        1. 获取文件字节（优先使用传入的 file_bytes，否则从 MinIO 下载）
        2. 文件特征识别：指纹 / 魔数 / 扩展名一致性 / 加密
        3. 幂等校验：指纹命中则返回历史结果
        4. 格式分派并执行原生解析
        5. 质量评估：必要时降级 OCR
        6. 标准化：构建带全局字符偏移的文本块
        7. 元数据抽取
        8. 写入缓存

    【异常语义（与节点重试策略配合）】
        IOError     服务不可用（MinIO 下载失败 / OCR 引擎缺失）
                    → 节点按可重试异常处理，指数退避后重试
        ValueError  文件内容问题（格式不支持 / 文件损坏）
                    → 节点按可重试异常处理（换文件后可成功）
        ParseBlockedSignal
                    业务事实阻断（加密 / 空正文 / 扫描件模糊）
                    → 节点转为 NonRetryableError，直接置任务 blocked

    参数：
        object_key: MinIO 对象键
        source_type: 来源类型 digital / scanned
        task_id: 任务 ID，用于日志与工作目录隔离
        work_dir: 任务工作目录（当前实现不落本地磁盘，保留参数以兼容契约）
        file_fingerprint: 已知文件指纹，用于一致性校验
        file_name: 原始文件名；为空时从 object_key 推断
        file_bytes: 文件字节内容；为空时从 MinIO 下载
        enable_cache: 是否启用幂等缓存

    返回：
        ParseResult 实例

    异常：
        IOError：MinIO 不可用或对象不存在
        ValueError：文件格式不支持或内容损坏
        ParseBlockedSignal：命中三类法定阻断之一
    """
    start_ts = time.perf_counter()

    # -------------------- 第一步：获取文件字节 --------------------
    if file_name:
        resolved_name = file_name
    else:
        # 从对象键末段推断文件名（对象键形如 contracts/2026/xxx.pdf）
        resolved_name = object_key.rsplit("/", 1)[-1] if object_key else ""

    if file_bytes is None:
        file_bytes = await _download_from_minio(object_key)

    if not file_bytes:
        raise IOError(f"合同原文内容为空：object_key={object_key}")

    logger.info(
        "开始解析合同 | 任务=%s | 文件=%s | 字节=%d | 来源=%s",
        task_id, resolved_name, len(file_bytes), source_type,
    )

    # -------------------- 第二步：文件特征识别 --------------------
    features = extract_encryption_and_format(file_bytes, resolved_name)
    actual_fingerprint = str(features["file_hash"])

    # 指纹一致性校验：传入的指纹与实测不符时以实测为准并告警
    # 不一致通常意味着文件在上传后被替换，必须重新解析而不能复用缓存
    if file_fingerprint and file_fingerprint != actual_fingerprint:
        logger.warning(
            "文件指纹与预期不一致，将以实测指纹重新解析 | 任务=%s | 预期=%s | 实测=%s",
            task_id, file_fingerprint[:16], actual_fingerprint[:16],
        )

    # 扩展名与魔数一致性校验（防改扩展名绕过白名单）
    try:
        validate_ext_magic(resolved_name, file_bytes)
    except ValueError as exc:
        # 格式问题属于可重试异常（用户换文件后即可成功）
        raise ValueError(f"文件格式校验失败：{exc}") from exc

    file_ext = str(features["file_ext"])

    # 加密检测：命中即抛阻断信号，交由节点裁决
    if features["is_encrypted"]:
        raise ParseBlockedSignal(
            reason="DOC_ENCRYPTED",
            message=(
                f"合同文档已加密（文件={resolved_name}），无法抽取正文内容，"
                f"请上传未加密版本后重跑"
            ),
            detail={
                "file_name": resolved_name,
                "file_hash": actual_fingerprint,
                "magic_type": features["magic_type"],
                "file_size": features["file_size"],
                "task_id": task_id,
            },
        )

    # -------------------- 第三步：幂等校验 --------------------
    if enable_cache:
        cached_payload = await _load_cached_result(actual_fingerprint)
        if cached_payload is not None:
            try:
                (
                    cached_text,
                    cached_pages,
                    cached_meta,
                    cached_blocks,
                    cached_metadata,
                ) = _deserialize_result(cached_payload)

                # 三重校验：指纹已匹配，再校验页数与字符数
                # 防止写入中断导致的残缺缓存被误用
                if (
                    cached_meta.file_hash == actual_fingerprint
                    and cached_meta.page_count == len(cached_pages)
                    and cached_meta.total_chars == calc_effective_text_length(cached_text)
                ):
                    elapsed_ms = (time.perf_counter() - start_ts) * 1000.0
                    logger.info(
                        "解析结果幂等命中，跳过重复解析 | 任务=%s | 指纹=%s | 页数=%d | 耗时=%.0fms",
                        task_id, actual_fingerprint[:16], cached_meta.page_count, elapsed_ms,
                    )
                    # 刷新解析时间戳，反映本次命中时刻
                    cached_meta.parsed_at = datetime.now()

                    return ParseResult(
                        full_text=cached_text,
                        page_texts=cached_pages,
                        doc_metadata=cached_meta,
                        text_blocks=cached_blocks,
                        metadata=cached_metadata,
                        raw_result=None,
                        cache_hit=True,
                        elapsed_ms=elapsed_ms,
                    )

                logger.warning(
                    "缓存记录校验不通过，将重新解析 | 指纹=%s | "
                    "页数=%s/%d | 字符数=%s/%d",
                    actual_fingerprint[:16],
                    cached_meta.page_count, len(cached_pages),
                    cached_meta.total_chars, calc_effective_text_length(cached_text),
                )

            except Exception as exc:  # noqa: BLE001 - 缓存损坏时回退到重新解析
                logger.warning(
                    "缓存载荷反序列化失败，将重新解析 | 指纹=%s | 异常=%s",
                    actual_fingerprint[:16], exc,
                )

    # -------------------- 第四步：原生解析 --------------------
    raw_result = await _parse_native(file_bytes, resolved_name, file_ext)

    # 解析器侧识别出加密（兜底，正常已在第二步拦截）
    if raw_result.is_encrypted:
        raise ParseBlockedSignal(
            reason="DOC_ENCRYPTED",
            message=f"合同文档已加密，无法抽取正文内容：file_name={resolved_name}",
            detail={
                "file_name": resolved_name,
                "file_hash": actual_fingerprint,
                "page_count": raw_result.page_count,
                "task_id": task_id,
            },
        )

    # -------------------- 第四步之后：正文为空的快速预判 --------------------
    # 【为什么要在 OCR 降级之前做这一步】
    #   EMPTY_CONTENT 是"文件本身没有内容"的客观事实判定，
    #   SCAN_TOO_BLURRY 是"扫描质量差"的判定，二者都不应受环境依赖影响。
    #   但 OCR 引擎是否安装属于环境问题（抛 IOError 触发重试）。
    #
    #   若不在此处先判空，会出现这样的错误链路：
    #       一个完全空白的电子 PDF → 原生解析无文字层 → 触发 OCR 降级
    #       → OCR 引擎未安装 → 抛 IOError → 任务进入重试并最终 blocked
    #       阻塞原因变成"OCR 服务不可用"，而真实原因是"文件没有内容"。
    #   这会让运维去检查 OCR 服务，完全走偏方向。
    #
    # 【适用范围的严格限定——此处极易踩坑】
    #   本预判**只针对电子文档**（PDF/DOCX 这类本应含文字层的格式）。
    #   对扫描件与图像文件必须跳过，原因：
    #       扫描件的原生解析**必然**抽不到文字（它本来就是图片），
    #       OCR 是获取内容的唯一途径。若在此处判空，
    #       所有扫描件都会被误判为 EMPTY_CONTENT，
    #       真实原因（模糊/清晰）永远无法得出。
    #
    #   判定阈值说明：
    #       使用"绝对空"（0 个非空白字符）而非 MIN_CONTENT_LENGTH ——
    #       MIN_CONTENT_LENGTH（50 字符）是最终交付门槛，须走完全部
    #       解析链路后才能裁定。此处只拦截最极端的情况，避免误伤
    #       "内容偏少但经 OCR 能补全"的文档。
    is_image_source = is_scanned_image_ext(file_ext)
    native_stripped = raw_result.full_text.strip()

    if not native_stripped and not is_image_source:
        raise ParseBlockedSignal(
            reason="EMPTY_CONTENT",
            message=(
                f"电子文档未抽取到任何正文内容（页数={raw_result.page_count}，"
                f"引擎={raw_result.engine}），判定正文为空，无法开展条款审查"
            ),
            detail={
                "effective_length": 0,
                "raw_length": len(raw_result.full_text),
                "min_content_length": settings.MIN_CONTENT_LENGTH,
                "page_count": raw_result.page_count,
                "source_type": source_type,
                "parse_engine": raw_result.engine,
                "used_ocr": False,
                "empty_page_count": raw_result.empty_page_count,
                "file_name": resolved_name,
                "file_hash": actual_fingerprint,
                "task_id": task_id,
                "blocked_stage": "native_parse_empty",
            },
        )

    # -------------------- 第五步：质量评估与 OCR 降级 --------------------
    used_ocr = False

    # 扫描件来源：直接走 OCR，不做原生解析质量的反复评估
    force_ocr = source_type == "scanned" or is_image_source

    if not force_ocr:
        need_ocr, reason = needs_ocr_fallback(raw_result)
        if need_ocr:
            logger.info(
                "原生解析质量不达标，触发 OCR 降级 | 任务=%s | 原因=%s", task_id, reason
            )
            raw_result.warnings.append(f"原生解析不达标触发 OCR 降级：{reason}")
            force_ocr = True

    if force_ocr:
        # OCR 前先探测引擎可用性，给出明确的运维提示
        available, engine_reason = is_ocr_engine_available()
        if not available:
            # 引擎缺失属于环境问题，抛 IOError 让节点重试（运维装好后重跑即可）
            raise IOError(
                f"OCR 引擎不可用，无法处理该文档：{engine_reason}。"
                f"任务={task_id}, 文件={resolved_name}"
            )

        try:
            raw_result = await _run_ocr_fallback(
                file_bytes, resolved_name, file_ext, raw_result.page_count or 1
            )
            used_ocr = True

        except OCRUnavailableError as exc:
            raise IOError(
                f"OCR 引擎初始化失败：{exc}。任务={task_id}, 文件={resolved_name}"
            ) from exc

        except OCREngineError as exc:
            raise IOError(
                f"OCR 识别失败：{exc}。任务={task_id}, 文件={resolved_name}"
            ) from exc

        except ValueError as exc:
            # 格式无法渲染为图像（如 DOCX）：此时无法降级
            # 若原生解析仍有内容，则降级失败不致命，继续使用原生结果
            if raw_result.total_chars >= settings.MIN_CONTENT_LENGTH:
                logger.warning(
                    "OCR 降级不可用但原生结果可用，继续使用原生结果 | 任务=%s | 原因=%s",
                    task_id, exc,
                )
                used_ocr = False
            else:
                raise

    # -------------------- 第六步：构建文档元数据 --------------------
    doc_meta = _build_doc_metadata(
        resolved_name=resolved_name,
        file_ext=file_ext,
        features=features,
        raw_result=raw_result,
        source_type=source_type,
        used_ocr=used_ocr,
    )

    # -------------------- 第七步：标准化文本块 --------------------
    full_text, page_texts, text_blocks = build_text_blocks(raw_result, doc_meta)

    doc_meta.total_chars = calc_effective_text_length(full_text)
    doc_meta.parsed_at = datetime.now()

    # 偏移正确性运行时校验（发现解析器行为变化导致的错位）
    if text_blocks:
        _verify_offsets(full_text, text_blocks)

    # -------------------- 第八步：三类阻断判定 --------------------
    _check_blocked_conditions(
        full_text=full_text,
        doc_meta=doc_meta,
        raw_result=raw_result,
        resolved_name=resolved_name,
        task_id=task_id,
        used_ocr=used_ocr,
        actual_fingerprint=actual_fingerprint,
    )

    # -------------------- 第九步：元数据抽取 --------------------
    metadata = metadata_extractor.extract_all_metadata(full_text)

    # 附带解析层面的元数据，便于前端展示与问题排查
    metadata.update(
        {
            "file_name": resolved_name,
            "file_ext": file_ext,
            "file_size": features["file_size"],
            "file_hash": actual_fingerprint,
            "page_count": doc_meta.page_count,
            "parse_engine": doc_meta.parse_engine,
            "source_type": doc_meta.source_type,
            "avg_ocr_confidence": doc_meta.avg_ocr_confidence,
            "used_ocr": used_ocr,
        }
    )

    # -------------------- 第十步：写入缓存 --------------------
    if enable_cache:
        payload = _serialize_result(
            full_text, page_texts, doc_meta, text_blocks, metadata
        )
        await _save_cached_result(actual_fingerprint, payload)

    # -------------------- 完成 --------------------
    elapsed_ms = (time.perf_counter() - start_ts) * 1000.0

    logger.info(
        "合同解析完成 | 任务=%s | 文件=%s | 页数=%d | 字符=%d | 块=%d | "
        "条款元数据命中=%s | OCR=%s | 耗时=%.0fms",
        task_id,
        resolved_name,
        doc_meta.page_count,
        doc_meta.total_chars,
        len(text_blocks),
        metadata.get("extraction_success_rate"),
        used_ocr,
        elapsed_ms,
    )

    return ParseResult(
        full_text=full_text,
        page_texts=page_texts,
        doc_metadata=doc_meta,
        text_blocks=text_blocks,
        metadata=metadata,
        raw_result=raw_result,
        cache_hit=False,
        elapsed_ms=elapsed_ms,
    )


async def _download_from_minio(object_key: str) -> bytes:
    """
    从 MinIO 下载合同原文

    参数：
        object_key: 对象键

    返回：
        文件字节内容

    异常：
        IOError：对象键为空、存储不可用或下载失败时抛出
    """
    if not object_key:
        raise IOError("合同文件对象键为空，无法定位原文文件")

    from app.storage import minio_client

    bucket = settings.MINIO_BUCKET_CONTRACT

    try:
        file_bytes = await minio_client.download_bytes(bucket, object_key)
    except Exception as exc:  # noqa: BLE001 - 统一转为可重试 IO 异常
        logger.error(
            "合同原文下载失败 | 桶=%s | 键=%s | 异常=%s", bucket, object_key, exc
        )
        raise IOError(
            f"从对象存储下载合同原文失败：object_key={object_key}, error={exc}"
        ) from exc

    if not file_bytes:
        raise IOError(f"合同原文内容为空：object_key={object_key}")

    return file_bytes


def _build_doc_metadata(
    resolved_name: str,
    file_ext: str,
    features: Dict[str, Any],
    raw_result: RawParseResult,
    source_type: str,
    used_ocr: bool,
) -> DocMetadata:
    """
    构建文档元数据

    说明：
        来源类型（source_type）的确立规则：
            · 若实际走了 OCR，来源类型必定为 scanned
              —— 因为文本来自图像识别，与用户声明的来源无关
            · 否则沿用传入的 source_type
        这样处理的原因：source_type 会影响 TextBlock 的 OCR 置信度赋值，
        以及下游对"该文档是否可信"的判断，必须以实际情况为准。

    参数：
        resolved_name: 文件名
        file_ext: 扩展名
        features: 文件特征字典
        raw_result: 解析产物
        source_type: 声明的来源类型
        used_ocr: 是否实际使用了 OCR

    返回：
        DocMetadata 实例
    """
    # 实际走了 OCR 则来源必为 scanned
    effective_source_type = "scanned" if used_ocr else source_type

    # 解析引擎标识
    if used_ocr:
        engine = raw_result.engine
    else:
        engine = raw_result.engine or ("pdf_text" if file_ext == ".pdf" else "docx_text")

    return DocMetadata(
        file_name=resolved_name,
        file_ext=file_ext,
        file_size=int(features["file_size"]),
        file_hash=str(features["file_hash"]),
        page_count=raw_result.page_count,
        source_type=effective_source_type,
        parse_engine=engine,
        is_encrypted=False,          # 加密文件已在上游抛阻断信号，此处必为 False
        has_text_layer=raw_result.has_text_layer,
        avg_ocr_confidence=raw_result.avg_confidence if used_ocr else None,
    )


def _check_blocked_conditions(
    full_text: str,
    doc_meta: DocMetadata,
    raw_result: RawParseResult,
    resolved_name: str,
    task_id: str,
    used_ocr: bool,
    actual_fingerprint: str,
) -> None:
    """
    执行三类法定阻断判定

    【三类阻断】
        SCAN_TOO_BLURRY  OCR 平均置信度低于配置阈值（默认 0.6）
        EMPTY_CONTENT    有效正文字符数低于配置下限（默认 50）
        DOC_ENCRYPTED    已在文件特征识别阶段处理，此处不再重复

    【判定顺序说明】
        先判模糊再判空。原因：
            扫描件模糊时，OCR 产出内容往往也很少，若先判空会得到
            EMPTY_CONTENT，但这个原因对人工是误导性的 ——
            用户需要知道"是扫描质量差"，而不是"文件没有内容"。
            优先给出更根本、更可操作的原因。

    参数：
        full_text: 全文
        doc_meta: 文档元数据
        raw_result: 解析产物
        resolved_name: 文件名
        task_id: 任务 ID
        used_ocr: 是否使用了 OCR
        actual_fingerprint: 文件指纹

    返回：
        无返回值

    异常：
        ParseBlockedSignal：命中任一类阻断时抛出
    """
    # -------------------- 判定一：扫描件模糊 --------------------
    if used_ocr and doc_meta.avg_ocr_confidence is not None:
        threshold = settings.OCR_MIN_CONFIDENCE_THRESHOLD

        if doc_meta.avg_ocr_confidence < threshold:
            raise ParseBlockedSignal(
                reason="SCAN_TOO_BLURRY",
                message=(
                    f"扫描件 OCR 平均置信度 {doc_meta.avg_ocr_confidence:.4f} "
                    f"低于阈值 {threshold}，判定影像模糊，请重新扫描后上传"
                ),
                detail={
                    "avg_ocr_confidence": doc_meta.avg_ocr_confidence,
                    "threshold": threshold,
                    "page_count": doc_meta.page_count,
                    "file_name": resolved_name,
                    "file_hash": actual_fingerprint,
                    "task_id": task_id,
                    "ocr_warnings": raw_result.warnings[:5],
                },
            )

    # -------------------- 判定二：正文为空 --------------------
    min_length = settings.MIN_CONTENT_LENGTH
    effective_length = calc_effective_text_length(full_text)

    if effective_length < min_length:
        raise ParseBlockedSignal(
            reason="EMPTY_CONTENT",
            message=(
                f"抽取正文有效字符数 {effective_length} 低于下限 {min_length}，"
                f"判定正文为空，无法开展条款审查"
                + (
                    f"（该文档经 OCR 识别，平均置信度 "
                    f"{doc_meta.avg_ocr_confidence}）"
                    if used_ocr and doc_meta.avg_ocr_confidence is not None
                    else ""
                )
            ),
            detail={
                "effective_length": effective_length,
                "raw_length": len(full_text),
                "min_content_length": min_length,
                "page_count": doc_meta.page_count,
                "source_type": doc_meta.source_type,
                "parse_engine": doc_meta.parse_engine,
                "avg_ocr_confidence": doc_meta.avg_ocr_confidence,
                "used_ocr": used_ocr,
                "empty_page_count": raw_result.empty_page_count,
                "file_name": resolved_name,
                "file_hash": actual_fingerprint,
                "task_id": task_id,
            },
        )


# ======================================================================
# 六、节点适配入口
# ======================================================================

async def load_contract_text_for_node(
    object_key: str,
    source_type: str,
    task_id: str,
    work_dir: str,
    file_fingerprint: str,
) -> Tuple[str, List[str], DocMetadata]:
    """
    供 app/graph/nodes.py 的 _load_contract_text 调用的适配入口

    【契约（不可变更，由 parse_document 节点依赖）】
        出参：(full_text: str, page_texts: List[str], doc_meta: DocMetadata)
        · page_texts 索引 0 对应第 1 页
        · full_text 的换行拼接方式与 TextBlock 偏移计算一致

    【异常转换（关键）】
        本入口负责把解析服务的异常体系转换为节点期望的异常类型：
            ParseBlockedSignal → 保持原样抛出
                由节点捕获并转为 NonRetryableError，携带 reason 与 detail
            OCRUnavailableError / OCREngineError → 转为 IOError
                属于环境问题，节点按可重试异常处理（指数退避）
            ValueError（格式/损坏） → 转为 IOError
                节点可重试 —— 因为用户可能上传了错误文件，
                重试期间若文件被替换，重试即可成功

    参数：
        object_key: MinIO 对象键
        source_type: 来源类型 digital / scanned
        task_id: 任务 ID
        work_dir: 工作目录（保留以兼容契约，当前实现不落盘）
        file_fingerprint: 已知文件指纹

    返回：
        (full_text, page_texts, doc_metadata)

    异常：
        ParseBlockedSignal：三类法定阻断
        IOError：服务不可用或文件内容问题
    """
    try:
        result = await parse_contract_document(
            object_key=object_key,
            source_type=source_type,
            task_id=task_id,
            work_dir=work_dir,
            file_fingerprint=file_fingerprint,
        )

    except ParseBlockedSignal:
        # 业务事实阻断：原样上抛，由节点决定如何裁决
        raise

    except (OCRUnavailableError, OCREngineError) as exc:
        raise IOError(f"OCR 服务不可用：{exc}") from exc

    except ValueError as exc:
        # 格式或内容问题：转为 IOError 以进入节点的重试链路
        raise IOError(f"文档解析失败：{exc}") from exc

    return result.to_contract_tuple()


async def parse_contract_with_details(
    object_key: str,
    source_type: str = "digital",
    task_id: str = "",
    work_dir: str = "",
    file_fingerprint: str = "",
    file_name: str = "",
) -> ParseResult:
    """
    解析合同并返回完整结果（含文本块与元数据）

    说明：
        供需要完整解析产物的调用方使用（如条款结构化服务、
        前端预览接口）。图节点走 load_contract_text_for_node 精简入口。

    参数：
        object_key: MinIO 对象键
        source_type: 来源类型
        task_id: 任务 ID
        work_dir: 工作目录
        file_fingerprint: 已知指纹
        file_name: 文件名

    返回：
        ParseResult 实例

    异常：
        IOError：服务不可用
        ParseBlockedSignal：三类法定阻断
    """
    return await parse_contract_document(
        object_key=object_key,
        source_type=source_type,
        task_id=task_id,
        work_dir=work_dir,
        file_fingerprint=file_fingerprint,
        file_name=file_name,
    )


async def parse_local_file(
    file_path: str,
    source_type: str = "digital",
    task_id: str = "local",
) -> ParseResult:
    """
    解析本地文件（供测试、批处理与运维排查使用）

    说明：
        绕过 MinIO 直接读取本地文件，便于在存储服务未就绪时
        验证解析逻辑，也用于离线批量导入历史合同。

    参数：
        file_path: 本地文件绝对路径
        source_type: 来源类型
        task_id: 任务标识

    返回：
        ParseResult 实例

    异常：
        IOError：文件不存在或读取失败
        ValueError：格式不支持
        ParseBlockedSignal：三类法定阻断
    """
    import os

    if not os.path.isfile(file_path):
        raise IOError(f"本地文件不存在：{file_path}")

    try:
        with open(file_path, "rb") as stream:
            file_bytes = stream.read()
    except OSError as exc:
        raise IOError(f"读取本地文件失败：file_path={file_path}, error={exc}") from exc

    file_name = os.path.basename(file_path)

    return await parse_contract_document(
        object_key=f"local/{file_name}",
        source_type=source_type,
        task_id=task_id,
        file_name=file_name,
        file_bytes=file_bytes,
    )
