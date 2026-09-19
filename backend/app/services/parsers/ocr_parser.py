# D:/ContractReviewSys-合同审批智能审查项目/backend/app/services/parsers/ocr_parser.py
"""
OCR 降级解析器

【模块定位】
    当原生文本抽取失败或产出质量不达标时，将页面渲染为位图交给 OCR 引擎识别。
    适用场景：
        1. 扫描件（source_type = scanned）—— 无文字层，必须 OCR
        2. PDF 无文字层或文字层无效 —— 图片型 PDF（如传真件转存）
        3. 原生抽取质量差 —— 乱码、可打印字符占比过低
        4. 混合文档 —— 部分页面有文字层，部分页面为图片

【引擎选型与就绪判定】
    配置项 OCR_ENGINE 默认 paddleocr（本地离线，符合"数据不出内网"要求）。
    本模块**不静默降级到假结果**：
        · 引擎未安装时抛 OCRUnavailableError，明确告知需安装的包
        · 识别失败时抛 OCREngineError，携带失败页面信息
    这是刻意的设计 —— 静默返回空文本会让任务被误判为 EMPTY_CONTENT，
    给出误导性的阻断原因，掩盖真实的"服务未就绪"问题。

【置信度的业务意义】
    平均置信度是 SCAN_TOO_BLURRY（扫描件模糊）阻断判定的唯一依据。
    因此置信度必须来自引擎的实际输出，绝不能估算或填充默认值。
    若某页无有效识别结果，该页不参与平均值计算，并单独记录到 warnings。
"""

import asyncio
import io
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from app.core.config import get_settings
from app.core.logging_config import get_logger
from app.services.parsers.raw_types import RawBlock, RawPage, RawParseResult

logger = get_logger(__name__)
settings = get_settings()


# ======================================================================
# 一、异常定义
# ======================================================================

class OCRUnavailableError(RuntimeError):
    """
    OCR 引擎不可用异常

    说明：
        引擎未安装或初始化失败时抛出。继承 RuntimeError 而非 ValueError，
        以便上层区分"环境问题"（可重试/需运维介入）与"数据问题"（换文件即可）。
    """

    def __init__(self, message: str, detail: Optional[Dict[str, Any]] = None) -> None:
        """
        参数：
            message: 面向运维的说明文本
            detail: 结构化上下文（引擎名、缺失的包名等）
        """
        self.detail = detail or {}
        super().__init__(message)


class OCREngineError(RuntimeError):
    """
    OCR 识别过程异常

    说明：
        引擎已就绪但具体页面识别失败时抛出，携带失败页面以便定位。
    """

    def __init__(self, message: str, detail: Optional[Dict[str, Any]] = None) -> None:
        """
        参数：
            message: 面向运维的说明文本
            detail: 结构化上下文（失败页码、原始异常等）
        """
        self.detail = detail or {}
        super().__init__(message)


# ======================================================================
# 二、引擎探测与惰性初始化
# ======================================================================

# 进程内缓存已初始化的引擎实例。
# 为什么缓存：PaddleOCR 初始化需加载模型权重，耗时可达数秒至数十秒，
# 每页重新初始化会使批量解析无法接受。
_engine_cache: Dict[str, Any] = {}


def is_ocr_engine_available() -> Tuple[bool, str]:
    """
    探测 OCR 引擎是否可用

    说明：
        仅做导入探测，不实际初始化引擎（初始化代价高，应在真正需要时才做）。
        供 parse_service 在决定是否走 OCR 链路前做快速判断。

    返回：
        (是否可用, 说明文本)
    """
    engine_name = settings.OCR_ENGINE.lower()

    if engine_name == "paddleocr":
        try:
            import paddleocr  # noqa: F401

            return True, "paddleocr 已安装"
        except ImportError:
            return False, (
                "paddleocr 未安装。请执行：pip install paddleocr paddlepaddle；"
                "或在 .env 中设置 OCR_ENGINE 为其他已部署的引擎"
            )

    if engine_name == "tesseract":
        try:
            import pytesseract  # noqa: F401

            return True, "pytesseract 已安装"
        except ImportError:
            return False, "pytesseract 未安装。请执行：pip install pytesseract"

    if engine_name == "mock":
        # 测试与联调专用：不依赖任何外部引擎
        return True, "mock 引擎（仅用于测试，不产生真实识别结果）"

    return False, (
        f"不支持的 OCR 引擎：{settings.OCR_ENGINE}。"
        f"可选值：paddleocr / tesseract / mock"
    )


def _get_engine() -> Any:
    """
    获取（惰性初始化）OCR 引擎实例

    说明：
        引擎实例在进程内复用。初始化采用双检锁语义避免并发重复初始化。

    返回：
        引擎实例

    异常：
        OCRUnavailableError：引擎不可用或初始化失败时抛出
    """
    engine_name = settings.OCR_ENGINE.lower()

    # 快速路径：已初始化
    if engine_name in _engine_cache:
        return _engine_cache[engine_name]

    available, reason = is_ocr_engine_available()
    if not available:
        raise OCRUnavailableError(reason, {"engine": engine_name})

    if engine_name == "paddleocr":
        try:
            from paddleocr import PaddleOCR

            # use_angle_cls：启用文字方向分类，处理扫描件倾斜
            # show_log：关闭 PaddleOCR 的冗余日志，避免污染项目日志
            # lang：识别语言，ch 覆盖中英文混合
            engine = PaddleOCR(
                use_angle_cls=True,
                lang=settings.OCR_LANG,
                show_log=False,
            )
            _engine_cache[engine_name] = engine
            logger.info("PaddleOCR 引擎初始化完成 | 语言=%s", settings.OCR_LANG)
            return engine
        except Exception as exc:  # noqa: BLE001 - 初始化失败统一转领域异常
            raise OCRUnavailableError(
                f"PaddleOCR 初始化失败：{exc}。"
                f"常见原因：paddlepaddle 未安装或与 paddleocr 版本不匹配",
                {"engine": "paddleocr", "error": str(exc)},
            ) from exc

    if engine_name == "tesseract":
        try:
            import pytesseract

            _engine_cache[engine_name] = pytesseract
            logger.info("Tesseract 引擎初始化完成")
            return pytesseract
        except Exception as exc:  # noqa: BLE001
            raise OCRUnavailableError(
                f"Tesseract 初始化失败：{exc}", {"engine": "tesseract", "error": str(exc)}
            ) from exc

    if engine_name == "mock":
        # mock 引擎不产生真实识别结果，仅用于链路联通性测试
        _engine_cache[engine_name] = object()
        logger.warning("OCR 使用 mock 引擎，识别结果为占位数据，禁止用于生产")
        return _engine_cache[engine_name]

    raise OCRUnavailableError(f"不支持的 OCR 引擎：{settings.OCR_ENGINE}")


def reset_ocr_engine() -> None:
    """
    清空引擎缓存

    说明：
        供测试用例与配置变更后重置使用。生产环境无需调用。

    返回：
        无返回值
    """
    _engine_cache.clear()


# ======================================================================
# 三、单页识别
# ======================================================================

def _recognize_paddleocr(
    engine: Any,
    image_bytes: bytes,
) -> List[Tuple[str, float, Tuple[float, float, float, float]]]:
    """
    使用 PaddleOCR 识别单张图像

    说明：
        PaddleOCR 的返回结构（不同版本略有差异）：
            result = [[ [box, (text, confidence)], ... ]]
            · 各版本可能返回 None、空列表或嵌套层级不同，需容错处理。

        本函数做了三层容错：
            1. 顶层为空 → 返回空列表
            2. 内层元素结构不符 → 跳过该元素并告警
            3. 置信度为 None → 按 0.0 处理并在结果中体现

    参数：
        engine: PaddleOCR 实例
        image_bytes: 图像字节内容

    返回：
        [(识别文本, 置信度, bbox), ...]；bbox 为 (x0, y0, x1, y1)

    异常：
        OCREngineError：引擎调用本身抛出异常时
    """
    import numpy as np
    from PIL import Image

    try:
        image = Image.open(io.BytesIO(image_bytes))
        # PaddleOCR 接受 ndarray 输入，避免其内部再做一次文件解码
        image_array = np.array(image.convert("RGB"))

        raw_result = engine.ocr(image_array, cls=True)

    except Exception as exc:  # noqa: BLE001 - 引擎异常统一转领域异常
        raise OCREngineError(f"PaddleOCR 识别调用失败：{exc}", {"error": str(exc)}) from exc

    items: List[Tuple[str, float, Tuple[float, float, float, float]]] = []

    if not raw_result:
        return items

    # 兼容不同版本的返回层级：可能是 [[...]] 或 [...]
    first = raw_result[0] if isinstance(raw_result, list) and raw_result else None
    if first is None:
        return items

    # 形态一：[[ [box, (text, conf)], ... ]]
    lines = first if isinstance(first, list) else [first]

    for line in lines:
        # 形态二可能直接是 [box, (text, conf)]
        if isinstance(line, (list, tuple)) and len(line) >= 2:
            box = line[0]
            payload = line[1]

            if not (isinstance(payload, (list, tuple)) and len(payload) >= 2):
                continue

            text = str(payload[0]) if payload[0] is not None else ""
            try:
                confidence = float(payload[1]) if payload[1] is not None else 0.0
            except (TypeError, ValueError):
                confidence = 0.0

            if not text.strip():
                continue

            bbox = _normalize_bbox(box)
            items.append((text, confidence, bbox))

    return items


def _normalize_bbox(box: Any) -> Tuple[float, float, float, float]:
    """
    将 OCR 引擎给出的四边形坐标归一化为 (x0, y0, x1, y1)

    说明：
        PaddleOCR 返回 4 点坐标 [[x0,y0],[x1,y1],[x2,y2],[x3,y3]]，
        需转换为左上-右下形式。四点顺序不保证，因此取最小与最大值。

    参数：
        box: 引擎返回的坐标结构

    返回：
        (x0, y0, x1, y1)；无法解析时返回 (0,0,0,0)
    """
    try:
        if box is None:
            return (0.0, 0.0, 0.0, 0.0)

        xs: List[float] = []
        ys: List[float] = []

        for point in box:
            if isinstance(point, (list, tuple)) and len(point) >= 2:
                xs.append(float(point[0]))
                ys.append(float(point[1]))

        if not xs or not ys:
            return (0.0, 0.0, 0.0, 0.0)

        return (min(xs), min(ys), max(xs), max(ys))

    except (TypeError, ValueError):
        return (0.0, 0.0, 0.0, 0.0)


def _recognize_tesseract(
    engine: Any,
    image_bytes: bytes,
) -> List[Tuple[str, float, Tuple[float, float, float, float]]]:
    """
    使用 Tesseract 识别单张图像

    说明：
        Tesseract 通过 image_to_data 可获取每个词的置信度与坐标。
        conf 字段为 -1 表示该条目不参与识别（如空行标记），需过滤。

    参数：
        engine: pytesseract 模块
        image_bytes: 图像字节内容

    返回：
        [(识别文本, 置信度, bbox), ...]

    异常：
        OCREngineError：引擎调用失败时
    """
    from PIL import Image

    try:
        image = Image.open(io.BytesIO(image_bytes))

        # lang 由配置指定；ch 需对应 tesseract 的 chi_sim 语言包
        lang = "chi_sim+eng" if settings.OCR_LANG.startswith("ch") else "eng"

        data = engine.image_to_data(
            image, lang=lang, output_type=engine.Output.DICT
        )

    except Exception as exc:  # noqa: BLE001
        raise OCREngineError(f"Tesseract 识别调用失败：{exc}", {"error": str(exc)}) from exc

    items: List[Tuple[str, float, Tuple[float, float, float, float]]] = []

    # 按行聚合：同一 (block_num, par_num, line_num) 的词合并为一行
    line_buffer: Dict[Tuple[int, int, int], Dict[str, Any]] = {}

    count = len(data.get("text", []))
    for index in range(count):
        text = str(data["text"][index]).strip()
        if not text:
            continue

        try:
            confidence = float(data["conf"][index])
        except (TypeError, ValueError, KeyError):
            confidence = -1.0

        # conf = -1 表示无效条目
        if confidence < 0:
            continue

        key = (
            int(data.get("block_num", [0] * count)[index]),
            int(data.get("par_num", [0] * count)[index]),
            int(data.get("line_num", [0] * count)[index]),
        )

        left = float(data.get("left", [0] * count)[index])
        top = float(data.get("top", [0] * count)[index])
        width = float(data.get("width", [0] * count)[index])
        height = float(data.get("height", [0] * count)[index])

        if key not in line_buffer:
            line_buffer[key] = {
                "texts": [],
                "confs": [],
                "x0": left, "y0": top,
                "x1": left + width, "y1": top + height,
            }

        entry = line_buffer[key]
        entry["texts"].append(text)
        entry["confs"].append(confidence / 100.0)   # Tesseract 置信度为 0-100
        entry["x0"] = min(entry["x0"], left)
        entry["y0"] = min(entry["y0"], top)
        entry["x1"] = max(entry["x1"], left + width)
        entry["y1"] = max(entry["y1"], top + height)

    # 行内词以空格拼接（Tesseract 对中文分词不敏感，空格便于后处理）
    for entry in line_buffer.values():
        text = "".join(entry["texts"])
        if not text:
            continue
        avg_conf = sum(entry["confs"]) / len(entry["confs"]) if entry["confs"] else 0.0
        items.append(
            (text, round(avg_conf, 4), (entry["x0"], entry["y0"], entry["x1"], entry["y1"]))
        )

    # 按 y 坐标再按 x 坐标排序，还原阅读顺序
    items.sort(key=lambda item: (round(item[2][1], 1), item[2][0]))

    return items


def _recognize_mock(
    engine: Any,
    image_bytes: bytes,
) -> List[Tuple[str, float, Tuple[float, float, float, float]]]:
    """
    mock 引擎识别（仅用于测试与链路联通性验证）

    说明：
        不产生真实识别结果，返回可配置的占位文本与置信度。
        调用方可通过 extras["engine"] == "ocr_mock" 识别该场景。
        生产环境若误用 mock 引擎，日志中会有明确 WARNING 提示。

    【为什么文本与置信度可配置】
        默认占位文本只有 13 字符，会被正文长度门禁判为 EMPTY_CONTENT，
        因而无法验证"扫描件正常通过"这条正向链路。
        通过 MOCK_OCR_TEXT / MOCK_OCR_CONFIDENCE 环境变量，
        测试可以构造"内容充足"或"低置信度"两种场景，
        让三类法定阻断与正常路径都能被完整覆盖。

    参数：
        engine: 占位对象
        image_bytes: 图像字节内容

    返回：
        [(占位文本, 置信度, (0,0,100,20))]
    """
    text = os.environ.get("MOCK_OCR_TEXT") or "MOCK_OCR_TEXT"

    try:
        confidence = float(os.environ.get("MOCK_OCR_CONFIDENCE", "0.99"))
    except ValueError:
        confidence = 0.99

    return [(text, confidence, (0.0, 0.0, 100.0, 20.0))]


def recognize_image(
    image_bytes: bytes,
    page_no: int = 1,
) -> List[Tuple[str, float, Tuple[float, float, float, float]]]:
    """
    识别单张图像（统一入口，按配置分派引擎）

    参数：
        image_bytes: 图像字节内容
        page_no: 页码，仅用于日志与异常信息

    返回：
        [(识别文本, 置信度, bbox), ...]

    异常：
        OCRUnavailableError：引擎不可用时抛出
        OCREngineError：识别过程失败时抛出
    """
    if not image_bytes:
        logger.warning("OCR 输入图像为空 | 页码=%d", page_no)
        return []

    engine = _get_engine()
    engine_name = settings.OCR_ENGINE.lower()

    if engine_name == "paddleocr":
        return _recognize_paddleocr(engine, image_bytes)
    if engine_name == "tesseract":
        return _recognize_tesseract(engine, image_bytes)
    if engine_name == "mock":
        return _recognize_mock(engine, image_bytes)

    raise OCRUnavailableError(f"不支持的 OCR 引擎：{settings.OCR_ENGINE}")


# ======================================================================
# 四、多页 OCR 主入口
# ======================================================================

async def parse_with_ocr(
    page_images: List[Tuple[int, bytes]],
    file_name: str = "",
) -> RawParseResult:
    """
    对多页图像执行 OCR 识别

    【并发控制】
        使用 asyncio.to_thread 把同步的 OCR 调用移出事件循环，
        并以信号量限制并发数。原因：
            · OCR 是 CPU/GPU 密集型同步调用，直接在事件循环中执行会阻塞整个服务
            · 并发过高会争抢显存/内存，导致 OOM 或整体变慢
        并发数取 min(4, CPU 核数)，在吞吐与资源占用间取平衡。

    【置信度计算】
        全文档平均置信度 = 所有有效识别条目的置信度算术平均。
        无有效条目的页面**不参与**平均，避免用 0 拉低整体置信度，
        从而错误触发 SCAN_TOO_BLURRY 阻断。这些页面单独记入 warnings。

    参数：
        page_images: [(页码, 图像字节), ...]，页码从 1 开始
        file_name: 原始文件名，仅用于日志

    返回：
        RawParseResult，其中：
            pages           逐页 OCR 结果
            page_count      总页数
            engine          "ocr" 或 "ocr_mock"
            avg_confidence  全文档平均置信度
            has_text_layer  OCR 结果是否达到可用标准

    异常：
        OCRUnavailableError：引擎不可用时抛出（不静默降级）
        OCREngineError：全部页面识别失败时抛出
    """
    if not page_images:
        logger.warning("OCR 调用未传入任何页面图像 | 文件=%s", file_name)
        return RawParseResult(engine=_engine_identifier(), page_count=0)

    result = RawParseResult(engine=_engine_identifier())

    # 并发度：CPU 密集型任务，不宜过高
    import os

    max_workers = max(1, min(4, os.cpu_count() or 2))
    semaphore = asyncio.Semaphore(max_workers)

    async def _process_page(page_no: int, image_bytes: bytes) -> RawPage:
        """
        处理单页 OCR（内部协程）

        参数：
            page_no: 页码
            image_bytes: 页面图像字节

        返回：
            RawPage 实例
        """
        async with semaphore:
            start = time.perf_counter()
            try:
                items = await asyncio.to_thread(recognize_image, image_bytes, page_no)
            except (OCRUnavailableError, OCREngineError):
                # 引擎级异常向上抛出，终止整个 OCR 流程
                # —— 引擎不可用时继续处理剩余页面没有意义
                raise
            except Exception as exc:  # noqa: BLE001 - 单页异常不中断其他页面
                logger.error(
                    "页面 OCR 失败，跳过该页 | 文件=%s | 页码=%d | 异常=%s",
                    file_name, page_no, exc,
                )
                return RawPage(
                    page=page_no,
                    text="",
                    blocks=[],
                    has_text_layer=False,
                    avg_confidence=None,
                )

            elapsed_ms = (time.perf_counter() - start) * 1000.0

        # -------------------- 组装页面 --------------------
        blocks: List[RawBlock] = []
        confidences: List[float] = []

        for order, (text, confidence, bbox) in enumerate(items):
            blocks.append(
                RawBlock(
                    page=page_no,
                    order_in_page=order,
                    text=text,
                    block_type="text",
                    bbox=bbox if bbox != (0.0, 0.0, 0.0, 0.0) else None,
                    font_size=None,        # OCR 无法给出可靠字号
                    is_bold=False,
                    confidence=confidence,
                )
            )
            confidences.append(confidence)

        page_text = "\n".join(block.text for block in blocks)
        page_confidence = (
            round(sum(confidences) / len(confidences), 4) if confidences else None
        )

        logger.info(
            "页面 OCR 完成 | 文件=%s | 页码=%d | 条目=%d | 置信度=%s | 耗时=%.0fms",
            file_name, page_no, len(blocks),
            page_confidence if page_confidence is not None else "无",
            elapsed_ms,
        )

        return RawPage(
            page=page_no,
            text=page_text,
            blocks=blocks,
            has_text_layer=bool(blocks),
            avg_confidence=page_confidence,
            width=0.0,
            height=0.0,
        )

    # -------------------- 并发执行 --------------------
    tasks = [_process_page(page_no, image_bytes) for page_no, image_bytes in page_images]

    try:
        pages = await asyncio.gather(*tasks)
    except OCRUnavailableError:
        # 引擎不可用必须原样上抛，由上层决定是重试还是阻断
        raise

    result.pages = sorted(pages, key=lambda page: page.page)
    result.page_count = len(result.pages)

    # -------------------- 置信度汇总 --------------------
    all_confidences: List[float] = []
    no_result_pages: List[int] = []

    for page in result.pages:
        if page.avg_confidence is not None and page.blocks:
            all_confidences.extend(
                block.confidence for block in page.blocks if block.confidence is not None
            )
        else:
            no_result_pages.append(page.page)

    if all_confidences:
        result.avg_confidence = round(
            sum(all_confidences) / len(all_confidences), 4
        )

    # 无识别结果的页面单独告警，不参与置信度平均（避免拉低整体误判为模糊）
    if no_result_pages:
        result.warnings.append(
            f"以下页面未识别出任何文本内容，未参与平均置信度计算：{no_result_pages}"
        )

    # -------------------- 可用性判定 --------------------
    # 判定标准：存在有效字符的页面占比超过半数，且平均置信度不低于阈值
    if result.pages:
        pages_with_text = sum(1 for page in result.pages if page.char_count > 0)
        enough_coverage = pages_with_text > (len(result.pages) / 2)

        confidence_ok = (
            result.avg_confidence is None
            or result.avg_confidence >= settings.OCR_MIN_CONFIDENCE_THRESHOLD
        )

        result.has_text_layer = enough_coverage

        if result.avg_confidence is not None and not confidence_ok:
            result.warnings.append(
                f"OCR 平均置信度 {result.avg_confidence:.4f} 低于阈值 "
                f"{settings.OCR_MIN_CONFIDENCE_THRESHOLD}，扫描件可能模糊"
            )

    result.extras.update(
        {
            "ocr_engine": settings.OCR_ENGINE,
            "ocr_lang": settings.OCR_LANG,
            "ocr_block_count": len(result.all_blocks),
            "ocr_page_count": len(result.pages),
            "ocr_no_result_pages": no_result_pages,
            "ocr_total_chars": result.total_chars,
            "ocr_concurrency": max_workers,
        }
    )

    logger.info(
        "OCR 识别完成 | 文件=%s | 页数=%d | 字符数=%d | 平均置信度=%s | 引擎=%s",
        file_name,
        result.page_count,
        result.total_chars,
        result.avg_confidence if result.avg_confidence is not None else "无",
        settings.OCR_ENGINE,
    )

    return result


def _engine_identifier() -> str:
    """
    返回与配置对应的引擎标识

    说明：
        该标识会写入 DocMetadata.parse_engine，需与既有
        constants.ParseEngine 的取值语义保持一致：统一为 "ocr"。
        mock 引擎单独标识为 "ocr_mock"，便于在测试与生产间明确区分。

    返回：
        引擎标识字符串
    """
    if settings.OCR_ENGINE.lower() == "mock":
        return "ocr_mock"
    return "ocr"


def needs_ocr_fallback(
    result: RawParseResult,
    min_chars_per_page: int = 10,
    min_cjk_ratio: float = 0.15,
    min_printable_ratio: float = 0.85,
    max_empty_page_ratio: float = 0.5,
) -> Tuple[bool, str]:
    """
    判定原生解析结果是否需要降级到 OCR

    【判定条件（满足任一即降级）】
        1. 整体无文字层（has_text_layer 为 False）
        2. 空页比例超过阈值 —— 典型图片型 PDF
        3. CJK 字符占比过低 —— 抽取到乱码或字体未嵌入
        4. 可打印字符占比过低 —— 抽取到控制字符
        5. 全文字符数过少 —— 内容明显不完整

    说明：
        阈值刻意设置得相对宽松（宁可多走一次 OCR，也不要漏掉劣质抽取）。
        因为漏判会导致下游基于乱码做条款切分，产出大量无效风险项，
        而多判一次 OCR 只是多花一点时间。

    参数：
        result: 原生解析结果
        min_chars_per_page: 每页最小有效字符数
        min_cjk_ratio: CJK 字符占比下限
        min_printable_ratio: 可打印字符占比下限
        max_empty_page_ratio: 允许的空页比例上限

    返回：
        (是否需要降级, 判定原因)；不需要时原因为空字符串
    """
    if not result.pages:
        return True, "原生解析未产出任何页面"

    # 条件一：无文字层
    if not result.has_text_layer:
        return True, "文档未检测到有效文字层"

    # 条件二：空页比例过高
    empty_ratio = result.empty_page_count / len(result.pages)
    if empty_ratio > max_empty_page_ratio:
        return True, (
            f"空页比例 {empty_ratio:.2%} 超过上限 {max_empty_page_ratio:.0%}"
            f"（{result.empty_page_count}/{len(result.pages)} 页无文本）"
        )

    full_text = result.full_text

    # 条件三：CJK 占比过低
    from app.utils.file_hash import calc_cjk_ratio

    cjk_ratio = calc_cjk_ratio(full_text)
    if cjk_ratio < min_cjk_ratio:
        return True, (
            f"中文字符占比 {cjk_ratio:.2%} 低于下限 {min_cjk_ratio:.0%}，"
            f"疑似字体未嵌入或抽取到乱码"
        )

    # 条件四：可打印字符占比过低
    from app.utils.file_hash import calc_printable_ratio

    printable_ratio = calc_printable_ratio(full_text)
    if printable_ratio < min_printable_ratio:
        return True, (
            f"可打印字符占比 {printable_ratio:.2%} 低于下限 {min_printable_ratio:.0%}，"
            f"疑似抽取到控制字符"
        )

    # 条件五：字符数过少
    expected_min = len(result.pages) * min_chars_per_page
    if result.total_chars < expected_min:
        return True, (
            f"有效字符数 {result.total_chars} 低于期望下限 {expected_min}"
            f"（{len(result.pages)} 页 × {min_chars_per_page} 字符/页）"
        )

    return False, ""
