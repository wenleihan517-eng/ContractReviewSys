# D:/ContractReviewSys-合同审批智能审查项目/docs/demo_samples/gen_exception_samples.py
"""
异常场景演示样本生成脚本

【用途】
    为端到端演示生成三类"法定阻断"的最小可复现样本：
        1. 加密 PDF      → 触发 DOC_ENCRYPTED
        2. 模糊扫描件    → 触发 SCAN_TOO_BLURRY（OCR 置信度 < 0.6）
        3. 空白 PDF      → 触发 EMPTY_CONTENT（正文 < 50 字符）

【为什么需要本脚本】
    演示前临时找"一份打不开的合同"是靠不住的：加密方式不对、模糊度不够、
    或者文件恰好还能抽出点文字，都会导致本该阻塞的任务正常通过，
    在评委面前表现为"系统没有正确识别异常"。把异常样本**程序化生成**，
    可以保证每次演示触发的原因码完全一致。

【运行方式】
    cd docs/demo_samples
    python gen_exception_samples.py

【依赖】
    pypdf   —— 生成加密 PDF 与空白 PDF
    Pillow  —— 生成模糊扫描件图片

    若缺少依赖，脚本会给出明确的安装提示，不会静默跳过。
"""

import os
import sys

# 输出目录：脚本所在目录
OUTPUT_DIR = os.path.dirname(os.path.abspath(__file__))


def _require(module_name: str, pip_name: str):
    """
    导入可选依赖，缺失时给出明确安装提示

    参数：
        module_name: 导入名
        pip_name: pip 安装名（可能与导入名不同）

    返回：
        导入成功的模块对象

    异常：
        SystemExit：依赖缺失时退出，并提示安装命令
    """
    import importlib

    try:
        return importlib.import_module(module_name)
    except ImportError:
        print(
            f"[缺少依赖] 需要 {pip_name} 才能生成该样本。\n"
            f"           请先执行：pip install {pip_name}",
            file=sys.stderr,
        )
        raise SystemExit(2)


def gen_encrypted_pdf() -> str:
    """
    生成带口令保护的 PDF（触发 DOC_ENCRYPTED）

    说明：
        解析服务通过检测 PDF 文件结构中的加密标记（/Encrypt）来判定文档加密。
        这里用 pypdf 的 encrypt() 写入标准加密字典，与真实加密文档结构一致。

    返回：
        生成的文件绝对路径
    """
    pypdf = _require("pypdf", "pypdf")

    writer = pypdf.PdfWriter()
    page = writer.add_blank_page(width=595, height=842)  # A4

    # 写入可辨识文字，便于排查"到底是加密拦住了还是内容为空"
    # 注意：加密后这段文字对解析服务不可见，这正是 DOC_ENCRYPTED 的语义
    try:
        from pypdf.generic import RectangleObject

        writer.add_metadata(
            {
                "/Title": "演示用加密合同",
                "/Subject": "本文件用于演示 DOC_ENCRYPTED 阻断场景",
            }
        )
        _ = RectangleObject  # 保留导入以说明页面对象可用
    except Exception:
        # 元数据写入失败不影响加密判定，降级继续
        pass

    writer.encrypt(user_password="demo-user-pass", owner_password="demo-owner-pass")

    path = os.path.join(OUTPUT_DIR, "exception1_加密合同.pdf")
    with open(path, "wb") as fp:
        writer.write(fp)

    size = os.path.getsize(path)
    print(f"[生成] {os.path.basename(path)}  ({size} 字节)")
    print("       预期阻断原因码：DOC_ENCRYPTED")
    print("       预期界面提示：合同文档已加密，无法抽取正文内容，请上传未加密版本")
    return path


def gen_blank_pdf() -> str:
    """
    生成几乎无文字的 PDF（触发 EMPTY_CONTENT）

    说明：
        只加一个空白页，不写入任何文字。解析后抽取正文长度约等于 0，
        低于 MIN_CONTENT_LENGTH（默认 50 字符）即触发阻断。

    返回：
        生成的文件绝对路径
    """
    pypdf = _require("pypdf", "pypdf")

    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=595, height=842)

    path = os.path.join(OUTPUT_DIR, "exception3_空白合同.pdf")
    with open(path, "wb") as fp:
        writer.write(fp)

    size = os.path.getsize(path)
    print(f"[生成] {os.path.basename(path)}  ({size} 字节)")
    print("       预期阻断原因码：EMPTY_CONTENT")
    print("       预期界面提示：抽取正文长度 0 字符，低于下限 50，判定正文为空")
    return path


def _require_attr(module_path: str, attr: str, pip_name: str):
    """
    导入子模块并取其属性，缺失时给出明确安装提示

    说明：
        不能简单地对 "PIL.Image" 调用 __import__ 后直接取属性 ——
        __import__ 返回的是顶层包 PIL，而 PIL 并不自动暴露 Image 属性。
        必须用 importlib 按完整路径导入，再逐级取属性。

    参数：
        module_path: 完整模块路径，如 "PIL.Image"
        attr: 需要取得的属性名，如 "new"
        pip_name: pip 安装名，供报错提示使用

    返回：
        目标属性对象
    """
    import importlib

    try:
        module = importlib.import_module(module_path)
    except ImportError:
        print(
            f"[缺少依赖] 需要 {pip_name} 才能生成该样本。\n"
            f"           请先执行：pip install {pip_name}",
            file=sys.stderr,
        )
        raise SystemExit(2)

    if not hasattr(module, attr):
        # 模块存在但属性缺失属异常情况，同样明确报错而非静默降级
        print(
            f"[依赖异常] {module_path} 中不存在属性 {attr}，"
            f"请检查 {pip_name} 版本是否完整。",
            file=sys.stderr,
        )
        raise SystemExit(2)

    return getattr(module, attr)


def gen_blurry_scan() -> str:
    """
    生成高度模糊的扫描件图片（触发 SCAN_TOO_BLURRY）

    说明：
        OCR 平均置信度低于阈值（默认 0.6）即判定影像模糊。
        这里通过"低分辨率放大 + 重度高斯模糊 + 高 JPEG 压缩"三重劣化，
        保证 OCR 无法得到可靠结果（置信度必然低于阈值），
        而不是"碰运气刚好低于 0.6"。

    返回：
        生成的文件绝对路径
    """
    image_new = _require_attr("PIL.Image", "new", "Pillow")
    image_lanczos = _require_attr("PIL.Image", "LANCZOS", "Pillow")
    filter_blur = _require_attr("PIL.ImageFilter", "GaussianBlur", "Pillow")
    draw_module = _require_attr("PIL.ImageDraw", "Draw", "Pillow")

    # 第一步：低分辨率画布上写字，制造"低 DPI 扫描"的信息缺失
    low_w, low_h = 260, 360
    img = image_new("RGB", (low_w, low_h), "white")
    draw = draw_module(img)

    lines = [
        "软件采购合同",
        "合同编号：DEMO-CG-2026-0918",
        "甲方：甲方甲科技有限公司",
        "乙方：乙方乙软件有限公司",
        "第一条 合同标的与金额",
        "合同总价为人民币捌拾陆万元整",
        "第二条 付款方式",
        "本合同签订后即支付全部合同价款",
        "第三条 知识产权",
        "乙方保证不侵犯第三方知识产权",
        "第四条 违约责任",
        "第五条 保密义务",
        "第六条 争议解决",
    ]
    y = 14
    for line in lines:
        draw.text((12, y), line, fill="black")
        y += 24

    # 第二步：放大到 A4 尺寸。低分辨率源图放大后文字边缘天然失真
    img = img.resize((1240, 1754), image_lanczos)

    # 第三步：重度高斯模糊，摧毁笔画结构
    img = img.filter(filter_blur(radius=8))

    # 第四步：高压缩 JPEG，叠加块状伪影，进一步降低 OCR 置信度
    path = os.path.join(OUTPUT_DIR, "exception2_模糊扫描件.jpg")
    img.save(path, "JPEG", quality=12)

    size = os.path.getsize(path)
    print(f"[生成] {os.path.basename(path)}  ({size} 字节)")
    print("       预期阻断原因码：SCAN_TOO_BLURRY")
    print("       预期界面提示：扫描件识别置信度 0.xxxx 低于阈值 0.6，请重新扫描上传")
    return path


def main() -> int:
    print("=" * 72)
    print("异常场景演示样本生成")
    print("=" * 72)
    print(f"输出目录：{OUTPUT_DIR}")
    print()

    generated = []

    for name, func in (
        ("加密 PDF", gen_encrypted_pdf),
        ("模糊扫描件", gen_blurry_scan),
        ("空白 PDF", gen_blank_pdf),
    ):
        try:
            generated.append(func())
            print()
        except SystemExit:
            # 依赖缺失：明确提示后整体退出，避免生成一半样本
            raise
        except Exception as exc:  # noqa: BLE001 - 单个样本失败不影响其它样本
            print(f"[失败] {name} 生成异常：{exc}", file=sys.stderr)
            print()

    print("=" * 72)
    print(f"完成：成功生成 {len(generated)} / 3 个异常样本")
    print("=" * 72)
    print()
    print("演示用法：")
    print("  1. 上传 exception1_加密合同.pdf      → 任务 blocked，原因码 DOC_ENCRYPTED")
    print("  2. 上传 exception2_模糊扫描件.jpg    → 任务 blocked，原因码 SCAN_TOO_BLURRY")
    print("  3. 上传 exception3_空白合同.pdf      → 任务 blocked，原因码 EMPTY_CONTENT")
    print("  4. 在大盘筛选状态 blocked，对任务执行「重试」或「驳回」")

    return 0 if len(generated) == 3 else 1


if __name__ == "__main__":
    sys.exit(main())
