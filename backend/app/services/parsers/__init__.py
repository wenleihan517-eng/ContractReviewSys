# D:/ContractReviewSys-合同审批智能审查项目/backend/app/services/parsers/__init__.py
"""
文档解析器子包

【职责】
    本子包是"文档解析模块"的底层实现层，按文件格式分派到具体解析器：
        pdf_parser.py   PyMuPDF（fitz）原生 PDF 文本抽取
        docx_parser.py  python-docx 的 DOCX 段落与表格抽取
        ocr_parser.py   OCR 降级链路（扫描件与原生解析失败时启用）

【统一契约】
    三类解析器均返回 RawParseResult，包含：
        pages           逐页原始文本（列表，索引 0 对应第 1 页）
        blocks          带位置信息的原始文本块
        page_count      总页数
        engine          实际使用的解析引擎标识
        avg_confidence  平均 OCR 置信度（仅 OCR 有值）
        extras          解析器特有的附加信息（如表单域、批注）

【为什么解析器只产出"原始结果"，不做标准化】
    标准化（全局字符偏移、段落索引、text_blocks 结构）统一由
    parse_service 完成。这样各格式解析器只需专注"把这一种格式读准确"，
    标准化逻辑只有一份，避免三处实现产生不一致的偏移计算。

返回值说明：
    本模块仅做包声明，无函数返回值
"""

from app.services.parsers.raw_types import RawPage, RawBlock, RawParseResult

__all__ = [
    "RawPage",
    "RawBlock",
    "RawParseResult",
]
