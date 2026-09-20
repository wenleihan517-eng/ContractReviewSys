# D:/ContractReviewSys-合同审批智能审查项目/backend/app/utils/file_hash.py
"""
文件指纹与文件特征识别工具

【模块定位】
    本模块属于工具层（utils），为解析服务提供两类确定性能力：
        1. 文件内容指纹计算（SHA-256）—— 幂等校验的基础
        2. 文件特征识别 —— 加密判定、扩展名/魔数一致性校验、MIME 类型推断

【设计原则】
    1. 全部为纯函数，无副作用，不访问数据库与网络
    2. 分块流式计算哈希，避免大文件一次性载入内存导致 OOM
    3. 加密判定基于文件结构特征，不依赖第三方库的容错行为
       —— 即使解析库"恰好能打开"加密文件，我们也必须显式识别并阻断

【为什么指纹要覆盖"字节 + 大小"】
    仅用字节流的 SHA-256 已足够唯一标识内容。额外把 size 纳入校验，
    是为了在指纹存储被截断或人工篡改时提供第二道判据。
"""

import hashlib
import io
from typing import Dict, Tuple

# ======================================================================
# 一、哈希计算
# ======================================================================

# 分块计算哈希时的块大小（1 MiB）。
# 选择 1 MiB 的原因：小于该值会导致大量系统调用，大于该值会在
# 处理数百 MB 合同时抬高单次内存峰值，1 MiB 是吞吐与内存的平衡点。
_HASH_CHUNK_SIZE = 1024 * 1024


def compute_sha256(data: bytes) -> str:
    """
    计算字节内容的 SHA-256 十六进制指纹

    说明：
        适合已知内容已在内存中的场景（如从 MinIO 下载后的字节流）。
        若内容体积很大，优先使用 compute_sha256_stream 以降低内存峰值。

    参数：
        data: 待计算的字节内容

    返回：
        64 位小写十六进制字符串

    异常：
        TypeError：data 不是 bytes / bytearray 时抛出
    """
    if isinstance(data, (bytearray, memoryview)):
        data = bytes(data)
    if not isinstance(data, bytes):
        raise TypeError(f"compute_sha256 只接受字节内容，实际类型：{type(data).__name__}")

    return hashlib.sha256(data).hexdigest()


def compute_sha256_stream(
    stream: io.BufferedIOBase,
    chunk_size: int = _HASH_CHUNK_SIZE,
) -> Tuple[str, int]:
    """
    以分块流式方式计算 SHA-256 指纹与内容字节数

    说明：
        逐块读取并增量更新哈希器，内存占用恒定为 chunk_size，
        适合处理超大合同文件（如数百页扫描件 PDF）。

    参数：
        stream: 已打开的二进制可读流，调用方负责关闭
        chunk_size: 单次读取字节数，默认 1 MiB

    返回：
        (指纹十六进制字符串, 内容总字节数)

    异常：
        ValueError：chunk_size 非正数时抛出
    """
    if chunk_size <= 0:
        raise ValueError(f"chunk_size 必须为正整数，实际：{chunk_size}")

    hasher = hashlib.sha256()
    total_size = 0

    while True:
        chunk = stream.read(chunk_size)
        if not chunk:
            break
        hasher.update(chunk)
        total_size += len(chunk)

    return hasher.hexdigest(), total_size


def compute_sha256_of_bytes(data: bytes, with_size: bool = False) -> str:
    """
    计算字节内容指纹，可选附带内容长度

    参数：
        data: 待计算的字节内容
        with_size: 为 True 时返回 "指纹:字节数" 形式，用于需要长度参与的场景

    返回：
        with_size 为 False 时返回纯指纹；为 True 时返回 "指纹:字节数"
    """
    digest = compute_sha256(data)
    if with_size:
        return f"{digest}:{len(data)}"
    return digest


def short_hash(full_hash: str, length: int = 12) -> str:
    """
    截取指纹前若干位，用于日志与对象键命名

    说明：
        截断后的指纹不可用于幂等判等，仅用于人工辨识与命名。

    参数：
        full_hash: 完整指纹
        length: 截取长度，默认 12

    返回：
        截取后的短指纹

    异常：
        ValueError：length 非正数或超过指纹长度时抛出
    """
    if length <= 0:
        raise ValueError(f"length 必须为正整数，实际：{length}")
    if length > len(full_hash):
        raise ValueError(
            f"length={length} 超出指纹长度 {len(full_hash)}，无法截取"
        )
    return full_hash[:length]


# ======================================================================
# 二、文件魔数（Magic Number）特征表
# ======================================================================
# 说明：扩展名可被随意修改，魔数才是文件真实格式的判据。
#       解析前用魔数校验，可避免"扩展名是 pdf 实际是 docx"导致的
#       解析库抛出难以理解的底层异常。

_MAGIC_SIGNATURES: Tuple[Tuple[bytes, str], ...] = (
    (b"%PDF-", "pdf"),
    (b"PK\x03\x04", "zip"),        # docx / xlsx / pptx 等 OOXML 均为 zip 容器
    (b"\xd0\xcf\x11\xe0", "ole"),  # doc（旧版 Word 二进制复合文档）
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpeg"),
    (b"II*\x00", "tiff"),          # 小端 TIFF
    (b"MM\x00*", "tiff"),          # 大端 TIFF
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"BM", "bmp"),
)


def detect_magic_type(data: bytes) -> str:
    """
    依据文件头魔数判定真实文件格式

    参数：
        data: 文件字节内容（至少包含文件头若干字节）

    返回：
        格式标识符，取值：pdf / zip / ole / png / jpeg / tiff / gif / bmp / unknown
    """
    if not data:
        return "unknown"

    # 取前 16 字节做比对，最长魔数为 8 字节，16 足以覆盖
    head = data[:16]

    for signature, format_name in _MAGIC_SIGNATURES:
        if head.startswith(signature):
            return format_name

    return "unknown"


def detect_docx_or_generic_zip(data: bytes) -> bool:
    """
    判定 zip 容器是否为 OOXML 文档（docx / xlsx / pptx）

    说明：
        所有 OOXML 文件都是 zip，靠魔数无法与普通 zip 区分。
        OOXML 容器内必然包含 "[Content_Types].xml" 条目，
        通过搜索该文件名在字节流中的出现位置进行判定。

    参数：
        data: 文件字节内容

    返回：
        为 OOXML 文档返回 True，否则 False
    """
    if not data or not data.startswith(b"PK\x03\x04"):
        return False

    # zip 的中央目录记录中会出现条目名明文；[Content_Types].xml 是 OOXML 的标志
    return b"[Content_Types].xml" in data[: 64 * 1024] or b"[Content_Types].xml" in data


# ======================================================================
# 三、加密特征识别
# ======================================================================

# PDF 加密标志：加密字典在文件尾部（trailer）以 /Encrypt 引用出现
_PDF_ENCRYPT_MARKERS: Tuple[bytes, ...] = (b"/Encrypt",)


def is_encrypted_pdf(data: bytes) -> bool:
    """
    判定 PDF 是否加密

    说明：
        判定依据为文件尾部的 trailer 字典中是否存在 /Encrypt 引用。
        该标志是 PDF 规范中加密的**必要**特征，比"尝试打开看是否报错"
        更可靠 —— 某些加密 PDF 使用空用户密码，解析库能正常打开，
        但内容抽取实际受限，必须显式识别后阻断。

        检查范围限定为文件头尾各若干 KiB，避免为超大文件做全量扫描。

    参数：
        data: PDF 文件字节内容

    返回：
        加密返回 True，否则 False
    """
    if not data:
        return False

    # 尾部 8 KiB 覆盖 trailer；头部 4 KiB 防御异常生成的加密字典位置
    tail = data[-8192:]
    head = data[:4096]

    for marker in _PDF_ENCRYPT_MARKERS:
        if marker in tail or marker in head:
            return True

    return False


def is_encrypted_office(data: bytes) -> bool:
    """
    判定 Office 文档（OLE 复合文档格式，即 .doc/.xls 旧版）是否加密

    说明：
        OLE 复合文档的加密信息位于名为 "EncryptedPackage" 的流中。
        在字节流中搜索该名称即可判定。.docx 若加密则表现为
        OLE 容器（而非 zip），因此同样适用本判定。

    参数：
        data: 文件字节内容

    返回：
        加密返回 True，否则 False
    """
    if not data:
        return False

    return b"EncryptedPackage" in data or b"EncryptionInfo" in data


def detect_encryption(data: bytes, magic_type: str = "") -> bool:
    """
    统一的加密判定入口

    参数：
        data: 文件字节内容
        magic_type: 已判定的魔数类型，为空时内部自动判定

    返回：
        加密返回 True，否则 False
    """
    if not data:
        return False

    if not magic_type:
        magic_type = detect_magic_type(data)

    if magic_type == "pdf":
        return is_encrypted_pdf(data)
    if magic_type in ("ole", "zip"):
        return is_encrypted_office(data)

    # 图片格式本身无加密字典，视为未加密
    return False


# ======================================================================
# 四、扩展名与格式一致性校验
# ======================================================================

# 允许上传的扩展名 → 可接受的魔数类型集合
_EXT_MAGIC_EXPECTATION: Dict[str, Tuple[str, ...]] = {
    ".pdf": ("pdf",),
    ".docx": ("zip",),
    ".doc": ("ole", "zip"),   # 部分工具导出的 .doc 实为 docx（zip）
    ".png": ("png",),
    ".jpg": ("jpeg",),
    ".jpeg": ("jpeg",),
    ".tif": ("tiff",),
    ".tiff": ("tiff",),
    ".bmp": ("bmp",),
    ".gif": ("gif",),
}


def normalize_ext(file_name: str) -> str:
    """
    规范化扩展名（转小写、含点）

    【输入容错——为什么必须支持纯扩展名】
        本函数是公共工具，调用方既有持文件名的场景（上传校验），
        也有只持扩展名的场景（格式分派、来源判定）。
        必须同时接受两种输入：
            normalize_ext("合同.pdf")    → ".pdf"
            normalize_ext("a/b/合同.PDF") → ".pdf"
            normalize_ext(".pdf")        → ".pdf"     ← 纯扩展名
            normalize_ext("README")      → ""         ← 确无扩展名

        容错的边界刻意收窄：只把"以点开头且仅含一个点"的输入视作
        纯扩展名（如 ".pdf"）。像 "README" 这种无点名称仍返回空串，
        因为把它猜成 ".readme" 会让下游白名单校验放行不该放行的文件
        —— 宁可漏判扩展名，也不能放行错误格式。

    参数：
        file_name: 文件名、路径，或纯扩展名（需带前导点）

    返回：
        形如 ".pdf" 的扩展名；确无扩展名时返回空字符串
    """
    if not file_name:
        return ""

    # 取路径末段，去掉可能的目录部分
    base_name = file_name.replace("\\", "/").rsplit("/", 1)[-1]

    if not base_name:
        return ""

    # 情况一：本身就是一个纯扩展名（以点开头，且只含这一个点）
    # 例如 ".pdf"、".PNG"
    if base_name.startswith(".") and base_name.count(".") == 1 and len(base_name) > 1:
        return base_name.lower()

    # 情况二：常规文件名，用 rfind 定位最后一个点
    # 避免文件名中带点（如 "合同v1.2.pdf"）误判
    dot_index = base_name.rfind(".")
    if dot_index <= 0 or dot_index == len(base_name) - 1:
        return ""

    return base_name[dot_index:].lower()


def validate_ext_magic(
    file_name: str,
    data: bytes,
) -> Tuple[bool, str, str]:
    """
    校验扩展名与文件真实格式是否一致

    说明：
        防止"改扩展名绕过白名单"。例如把 .exe 改名为 .pdf 上传，
        魔数校验会发现头部不是 %PDF- 而拒绝。

    参数：
        file_name: 原始文件名
        data: 文件字节内容

    返回：
        (是否通过, 规范化扩展名, 魔数类型)

    异常：
        ValueError：扩展名不在允许清单内，或扩展名与魔数不匹配时抛出，
                    异常消息中给出可读的原因说明
    """
    ext = normalize_ext(file_name)
    if not ext:
        raise ValueError(f"文件名缺少扩展名，无法判定格式：{file_name}")

    if ext not in _EXT_MAGIC_EXPECTATION:
        allowed = "、".join(sorted(_EXT_MAGIC_EXPECTATION.keys()))
        raise ValueError(f"不支持的文件扩展名：{ext}，允许的类型：{allowed}")

    magic_type = detect_magic_type(data)

    # OOXML 容器需进一步确认确为 OOXML，而非普通 zip
    if magic_type == "zip" and ext in (".docx", ".doc"):
        if not detect_docx_or_generic_zip(data):
            raise ValueError(
                f"文件 {file_name} 是 zip 容器，但内部不含 OOXML 标记"
                f"（[Content_Types].xml），不是有效的 Word 文档"
            )

    expected = _EXT_MAGIC_EXPECTATION[ext]
    if magic_type not in expected:
        raise ValueError(
            f"文件格式与扩展名不符：扩展名 {ext} 期望格式 {expected}，"
            f"实际检测到 {magic_type}。文件可能已被篡改或损坏"
        )

    return True, ext, magic_type


def is_scanned_image_ext(ext: str) -> bool:
    """
    判定扩展名是否属于"必须走 OCR"的图像类型

    参数：
        ext: 规范化扩展名（含点）

    返回：
        图像类型返回 True
    """
    return normalize_ext(ext) in (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif")


# ======================================================================
# 五、MIME 类型推断
# ======================================================================

_MIME_MAP: Dict[str, str] = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".bmp": "image/bmp",
    ".gif": "image/gif",
}


def guess_mime_type(file_name: str) -> str:
    """
    依据扩展名推断 MIME 类型

    参数：
        file_name: 文件名

    返回：
        MIME 类型字符串；未知扩展名返回 "application/octet-stream"
    """
    return _MIME_MAP.get(normalize_ext(file_name), "application/octet-stream")


# ======================================================================
# 六、文本质量评估
# ======================================================================

def calc_printable_ratio(text: str) -> float:
    """
    计算文本中可打印字符占比

    说明：
        PDF 文本抽取失败时，常产出大量不可打印字符（如 \\x00、控制符）。
        该比例可作为"文本质量差，需降级 OCR"的客观判据。

    参数：
        text: 待评估文本

    返回：
        可打印字符占比（0.0 - 1.0）；空文本返回 0.0
    """
    if not text:
        return 0.0

    printable_count = sum(1 for ch in text if ch.isprintable() or ch in "\n\r\t")
    return printable_count / len(text)


def calc_cjk_ratio(text: str) -> float:
    """
    计算中日韩字符占比

    说明：
        合同正文以中文为主。若抽取结果中 CJK 字符占比极低，
        通常意味着字体未正确嵌入或抽取到的是乱码，需降级 OCR。

    参数：
        text: 待评估文本

    返回：
        CJK 字符占比（0.0 - 1.0）；空文本返回 0.0
    """
    if not text:
        return 0.0

    cjk_count = 0
    for ch in text:
        code = ord(ch)
        # CJK 统一表意文字及扩展区
        if 0x4E00 <= code <= 0x9FFF or 0x3400 <= code <= 0x4DBF:
            cjk_count += 1

    return cjk_count / len(text)


def calc_effective_text_length(text: str) -> int:
    """
    计算有效正文字符数（剔除空白字符）

    说明：
        与 len(text) 的区别在于剔除所有空白。判定"正文为空"时必须用
        有效长度 —— 一份全是换行与空格的 PDF 不应被判定为"有内容"。

    参数：
        text: 待评估文本

    返回：
        非空白字符数量
    """
    if not text:
        return 0

    return sum(1 for ch in text if not ch.isspace())


def extract_encryption_and_format(
    data: bytes,
    file_name: str,
) -> Dict[str, object]:
    """
    一次性提取文件的格式与安全特征

    说明：
        解析服务在入口处调用本函数，避免对同一份字节流重复做
        魔数识别、加密判定等扫描操作。

    参数：
        data: 文件字节内容
        file_name: 原始文件名

    返回：
        字典，含以下键：
            file_hash: SHA-256 指纹
            file_size: 字节数
            file_ext: 规范化扩展名
            magic_type: 魔数类型
            is_encrypted: 是否加密
            mime_type: MIME 类型
            is_image: 是否为图像类型
    """
    magic_type = detect_magic_type(data)

    return {
        "file_hash": compute_sha256(data),
        "file_size": len(data),
        "file_ext": normalize_ext(file_name),
        "magic_type": magic_type,
        "is_encrypted": detect_encryption(data, magic_type),
        "mime_type": guess_mime_type(file_name),
        "is_image": is_scanned_image_ext(normalize_ext(file_name)),
    }
