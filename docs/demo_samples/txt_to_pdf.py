# D:/ContractReviewSys-合同审批智能审查项目/docs/demo_samples/txt_to_pdf.py
"""
把演示样例文本（.txt）转换为可上传的 PDF 合同文件

背景：
    /api/v1/parse/upload 仅接受 PDF / Word / 图片（.pdf/.doc/.docx/.jpg 等），
    不接受 .txt。而 docs/demo_samples 下的样例正文是纯文本，需转成 PDF 才能
    在真实链路中上传，避免演示时被接口白名单拦下。

实现：
    使用项目已依赖的 PyMuPDF（fitz）直接排版输出中文 PDF。
    字体优先取系统中文字体（msyh/simhei/simsun），缺失时退化为内置 CJK 字体。

用法：
    cd backend
    "<venv>/Scripts/python.exe" ../docs/demo_samples/txt_to_pdf.py

输出：
    与输入同名、后缀改为 .pdf 的文件，写在同目录下
"""

import os
import sys

try:
    import fitz  # PyMuPDF
except ImportError:
    print("未安装 PyMuPDF，请先 pip install pymupdf")
    sys.exit(1)

_HERE = os.path.dirname(os.path.abspath(__file__))

# 输入样例 → 输出文件名
JOBS = [
    ("sample1_软件采购合同.txt", "sample1_软件采购合同.pdf"),
    ("sample2_大额销售合同.txt", "sample2_大额销售合同.pdf"),
]

# 候选中文字体（Windows 常见路径），按优先级尝试
FONT_CANDIDATES = [
    "C:/Windows/Fonts/msyh.ttc",     # 微软雅黑
    "C:/Windows/Fonts/simhei.ttf",   # 黑体
    "C:/Windows/Fonts/simsun.ttc",   # 宋体
    "C:/Windows/Fonts/simkai.ttf",   # 楷体
]

PAGE_W, PAGE_H = 595, 842      # A4
MARGIN = 56                    # 页边距
FONT_SIZE = 10.5
LINE_HEIGHT = 17.0


def pick_font() -> tuple:
    """返回 (字体名, 字体文件路径)；找不到中文字体时回退到内置 CJK 字体"""
    for path in FONT_CANDIDATES:
        if os.path.exists(path):
            return "cjk", path
    return "china-s", None


def wrap_line(text: str, font, size: float, max_width: float) -> list:
    """
    按可用宽度对单行文本做折行

    参数：
        text      原始行文本
        font      fitz.Font 实例
        size      字号
        max_width 可用宽度（磅）
    返回：
        折行后的行列表（保留空行以维持段落间距）
    """
    if not text.strip():
        return [""]
    lines, buf = [], ""
    for ch in text:
        if font.text_length(buf + ch, size) <= max_width:
            buf += ch
        else:
            lines.append(buf)
            buf = ch
    if buf:
        lines.append(buf)
    return lines


def convert(src: str, dst: str) -> int:
    """把单个 txt 转为 PDF，返回写入的页数"""
    with open(src, "r", encoding="utf-8") as fh:
        raw_lines = fh.read().splitlines()

    font_name, font_path = pick_font()
    if font_path:
        font = fitz.Font(fontfile=font_path)
    else:
        font = fitz.Font("china-s")

    doc = fitz.open()
    page = doc.new_page(width=PAGE_W, height=PAGE_H)
    y = MARGIN
    pages = 1
    max_width = PAGE_W - MARGIN * 2

    def new_page():
        nonlocal page, y, pages
        page = doc.new_page(width=PAGE_W, height=PAGE_H)
        y = MARGIN
        pages += 1

    for raw in raw_lines:
        for seg in wrap_line(raw, font, FONT_SIZE, max_width):
            if y + LINE_HEIGHT > PAGE_H - MARGIN:
                new_page()
            tw = fitz.TextWriter(page.rect)
            tw.append((MARGIN, y + FONT_SIZE), seg, font=font, fontsize=FONT_SIZE)
            tw.write_text(page)
            y += LINE_HEIGHT

    # 关键：先做字体子集化再做增量保存，否则整个字体文件（msyh 约 12MB）会被完整内嵌，
    # 导致 2 页的合同 PDF 膨胀到 12MB。
    # subset_fonts() 会把每页实际用到的字形抽出来重写，体积通常降到几十 KB 量级。
    try:
        doc.subset_fonts(verbose=False)
    except Exception as exc:  # noqa: BLE001 - 子集化失败不影响可读性，仅体积偏大
        print(f"  [提示] 字体子集化失败（PDF 体积会偏大）：{exc}")

    doc.save(dst, deflate=True, garbage=3, clean=True)
    doc.close()
    return pages


def main() -> int:
    ok = 0
    for src_name, dst_name in JOBS:
        src = os.path.join(_HERE, src_name)
        dst = os.path.join(_HERE, dst_name)
        if not os.path.exists(src):
            print(f"[跳过] 源文件不存在：{src}")
            continue
        pages = convert(src, dst)
        size_kb = os.path.getsize(dst) / 1024
        print(f"[完成] {src_name} -> {dst_name} | 页数={pages} | 大小={size_kb:.1f} KB")
        ok += 1

    print(f"\n共转换 {ok} 个文件，输出目录：{_HERE}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
