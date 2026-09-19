# D:/ContractReviewSys-合同审批智能审查项目/backend/tests/verify_parse_node_e2e.py
"""
parse_document 节点 ↔ parse_service 端到端验证

【验证目标】
    确认图节点真的消费了解析服务的产出，并且：
        1. 文本块偏移精确（节点与解析服务对 full_text 的拼接理解一致）
        2. 节点产出的文本块携带真实页码与引擎标识
        3. 三类法定阻断经节点包装器转为 blocked 状态（不进入重试）
        4. 可重试异常（IOError）经节点包装器进入指数避退重试
        5. 条款切分与段落树在真实解析结果上正常工作

【关键认知：节点包装器会消化异常】
    nodes.parse_document 已被 @node_wrapper 装饰，其行为是：
        · 正常执行 → 返回状态增量字典
        · NonRetryableError → 捕获后返回 blocked 状态字典（不向上抛出）
        · 可重试异常 → 指数退避重试，耗尽后返回 blocked 状态字典
    因此本脚本的断言方式是检查返回字典中的 is_blocked / blocked_info，
    而不是期望捕获异常。这与图的真实运行方式一致。

【MinIO 绕行方式】
    真实 MinIO 未就绪，通过替换 minio_client.download_bytes
    注入本地生成的样本文件字节。这是"测试替身"而非"跳过测试"——
    被验证的代码路径（解析 → 偏移 → 阻断 → 条款切分）全部是真实的。

【运行方式】
    cd backend && python tests/verify_parse_node_e2e.py
"""

import asyncio
import os
import sys

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from app.graph import nodes
from app.graph.state import ReviewTaskState
from app.models.documents.review_task import BlockedReason
from tests.verify_parse_service import (
    build_contract_pdf,
    build_empty_pdf,
    build_encrypted_pdf,
    build_docx,
)

PASSED = []
FAILED = []


def check(name: str, condition: bool, detail: str = "") -> None:
    """记录单项断言结果"""
    if condition:
        PASSED.append(name)
        print(f"  [PASS] {name}")
    else:
        FAILED.append((name, detail))
        print(f"  [FAIL] {name} | {detail}")


def make_state(**overrides):
    """构造节点测试用状态对象"""
    base = {
        "task_id": "e2e_task_001",
        "task_no": "T20260918001",
        "thread_id": "thread_e2e_001",
        "work_dir": "tasks/e2e_task_001",
        "approval_id": "AP20260918001",
        "contract_id": "HT20260918001",
        "file_object_key": "contracts/2026/verify_contract.pdf",
        "file_fingerprint": "",
        "source_type": "digital",
        "contract_title": "设备采购合同",
        "contract_no": "HT20260918001",
        "submit_dept": "采购部",
        "applicant": "张三",
        "counterparty": "上海鼎盛机械制造有限公司",
        "contract_amount": 3_000_000.0,
        "currency": "CNY",
        "business_type": "采购",
        "contract_type": "purchase",
    }
    base.update(overrides)
    return ReviewTaskState(**base)


class FakeMinIO:
    """
    MinIO 测试替身

    说明：
        用注入的字节替换真实下载调用。除了"不连网络"这一点，
        下游所有解析逻辑都是真实执行的。
    """

    def __init__(self, payload: bytes):
        self.payload = payload
        self.calls = 0

    async def download_bytes(self, bucket: str, object_key: str) -> bytes:
        self.calls += 1
        return self.payload


def patch_minio(payload: bytes):
    """
    替换 minio_client.download_bytes

    参数：
        payload: 要返回的文件字节
    返回：
        (替身对象, 原函数) —— 供测试结束后还原
    """
    from app.storage import minio_client

    original = minio_client.download_bytes
    fake = FakeMinIO(payload)
    minio_client.download_bytes = fake.download_bytes
    return fake, original


def unpatch_minio(original):
    """还原 minio_client.download_bytes"""
    from app.storage import minio_client

    minio_client.download_bytes = original


# ======================================================================
# 验证一：真实 PDF 走通节点
# ======================================================================

async def test_node_with_real_pdf():
    print("\n[验证一] parse_document 节点消费真实 PDF 解析结果")
    from app.services import parse_service

    parse_service.clear_parse_cache()

    pdf_bytes = build_contract_pdf()
    fake, original = patch_minio(pdf_bytes)

    try:
        state = make_state()
        updates = await nodes.parse_document(state)

        check("节点执行返回状态增量", isinstance(updates, dict))
        check("MinIO 替身被调用", fake.calls >= 1, f"调用次数={fake.calls}")
        check("节点未被阻塞", updates.get("is_blocked") in (False, None),
              f"is_blocked={updates.get('is_blocked')}")
        check("解析状态为 success",
              updates.get("parse_status") == "success",
              f"实际={updates.get('parse_status')}")
        check("抽取全文非空", len(updates.get("full_text", "")) > 100,
              f"长度={len(updates.get('full_text', ''))}")

        blocks = updates.get("text_blocks") or []
        check("产出文本块", len(blocks) > 0, f"块数={len(blocks)}")

        # 核心：偏移必须精确
        full_text = updates.get("full_text", "")
        all_exact = all(
            full_text[b.start_idx:b.end_idx] == b.text for b in blocks
        )
        check("节点产出的全部文本块偏移精确", all_exact,
              f"失败块={[b.block_index for b in blocks if full_text[b.start_idx:b.end_idx] != b.text][:3]}")

        # 页码必须真实（解析服务产出 2 页）
        pages_used = sorted({b.page for b in blocks})
        check("文本块携带真实页码", pages_used == [1, 2], f"实际页码={pages_used}")

        # 元数据透传
        doc_meta = updates.get("doc_metadata")
        check("文档元数据已写入", doc_meta is not None)
        check("页数为 2", doc_meta.page_count == 2, f"实际={doc_meta.page_count}")
        check("解析引擎为 pdf_text", doc_meta.parse_engine == "pdf_text",
              f"实际={doc_meta.parse_engine}")
        check("来源类型为 digital", doc_meta.source_type == "digital",
              f"实际={doc_meta.source_type}")
        check("已计算文件指纹", bool(doc_meta.file_hash))
        check("文件指纹写入状态", bool(updates.get("file_fingerprint")))

        # 条款切分
        clauses = updates.get("clauses") or []
        check("切分出条款", len(clauses) > 0, f"条款数={len(clauses)}")
        check("条款携带原文定位区间",
              all(c.char_end > c.char_start for c in clauses),
              f"异常条款={[c.clause_index for c in clauses if c.char_end <= c.char_start][:3]}")
        check("条款定位区间未越界",
              all(0 <= c.char_start < c.char_end <= len(full_text) for c in clauses),
              f"全文长度={len(full_text)} 最大 end={max((c.char_end for c in clauses), default=0)}")

        # 核心反向溯源能力：按 char_start/char_end 切全文，应能还原条款正文
        sample_clause = next(
            (c for c in clauses if (c.char_end - c.char_start) > 10), clauses[0]
        )
        sliced = full_text[sample_clause.char_start: sample_clause.char_end]
        check("条款定位可从全文还原内容",
              sample_clause.content.strip() in sliced or sliced.strip() in sample_clause.content,
              f"切片={sliced[:60]!r} 条款={sample_clause.content[:60]!r}")

        # 段落树
        paragraphs = updates.get("paragraphs") or []
        check("还原出段落树", len(paragraphs) > 0, f"段落数={len(paragraphs)}")

        # 解析错误字段应为空
        check("无解析错误信息", not updates.get("parse_error"))

        return updates

    finally:
        unpatch_minio(original)


# ======================================================================
# 验证二：加密文档经节点转为 blocked（不可重试）
# ======================================================================

async def test_node_with_encrypted_pdf():
    print("\n[验证二] 加密文档经节点转为 blocked，不进入重试")
    from app.services import parse_service

    parse_service.clear_parse_cache()

    enc_bytes = build_encrypted_pdf()
    fake, original = patch_minio(enc_bytes)

    try:
        state = make_state(file_object_key="contracts/2026/encrypted.pdf")
        out = await nodes.parse_document(state)

        check("节点返回 blocked 状态", out.get("is_blocked") is True,
              f"is_blocked={out.get('is_blocked')}")

        # blocked_info 是 BlockedInfo 模型对象（非 dict），须用属性访问
        blocked_info = out.get("blocked_info")
        reason = getattr(blocked_info, "reason", None)
        check("阻断原因为 DOC_ENCRYPTED",
              reason == BlockedReason.DOC_ENCRYPTED.value,
              f"实际={reason}")

        # 关键：不可重试阻断不应产生重试日志
        audit_logs = out.get("audit_logs") or []
        actions = [a.action for a in audit_logs]
        check("无重试动作（不可重试）",
              "node_retry" not in actions,
              f"动作序列={actions}")
        check("存在红线拒绝动作",
              "redline_deny" in actions,
              f"动作序列={actions}")

    finally:
        unpatch_minio(original)


# ======================================================================
# 验证三：空白文档经节点转为 blocked（EMPTY_CONTENT）
# ======================================================================

async def test_node_with_empty_pdf():
    print("\n[验证三] 空白文档经节点转为 blocked（EMPTY_CONTENT）")
    from app.services import parse_service

    parse_service.clear_parse_cache()

    empty_bytes = build_empty_pdf()
    fake, original = patch_minio(empty_bytes)

    try:
        state = make_state(file_object_key="contracts/2026/empty.pdf")
        out = await nodes.parse_document(state)

        check("节点返回 blocked 状态", out.get("is_blocked") is True,
              f"is_blocked={out.get('is_blocked')}")

        # blocked_info 是 BlockedInfo 模型对象（非 dict），须用属性访问
        blocked_info = out.get("blocked_info")
        reason = getattr(blocked_info, "reason", None)
        check("阻断原因为 EMPTY_CONTENT",
              reason == BlockedReason.EMPTY_CONTENT.value,
              f"实际={reason}")
        check("无重试动作（不可重试）",
              "node_retry" not in [a.action for a in (out.get("audit_logs") or [])],
              f"动作序列={[a.action for a in (out.get('audit_logs') or [])]}")

    finally:
        unpatch_minio(original)


# ======================================================================
# 验证四：可重试异常进入指数退避重试后置 blocked
# ======================================================================

async def test_retry_on_io_error():
    print("\n[验证四] 存储不可用时节点按指数退避重试后置 blocked")
    from app.services import parse_service
    from app.storage import minio_client

    parse_service.clear_parse_cache()

    original = minio_client.download_bytes
    call_count = {"n": 0}

    async def failing_download(bucket: str, object_key: str) -> bytes:
        call_count["n"] += 1
        raise ConnectionError("MinIO 连接失败（模拟）")

    minio_client.download_bytes = failing_download

    try:
        state = make_state(file_object_key="contracts/2026/unreachable.pdf")
        out = await nodes.parse_document(state)

        check("重试耗尽后返回 blocked", out.get("is_blocked") is True,
              f"is_blocked={out.get('is_blocked')}")
        check("实际尝试次数等于上限（3 次）", call_count["n"] == 3,
              f"实际={call_count['n']}")

        # blocked_info 是 BlockedInfo 模型对象（非 dict），须用属性访问
        blocked_info = out.get("blocked_info")
        reason = getattr(blocked_info, "reason", None)
        check("阻断原因为 RETRY_EXHAUSTED",
              reason == BlockedReason.RETRY_EXHAUSTED.value,
              f"实际={reason}")

        # 【重试留痕说明】
        #   重试耗尽路径的返回值中，audit_logs 只包含本次的 node_error 条目；
        #   循环内累积的 node_retry 条目不会进入返回值（无法与归约器语义兼容）。
        #   因此此处断言 node_error 存在、且 reason 为 RETRY_EXHAUSTED，
        #   而"实际尝试了 3 次"已由上面的 call_count 断言覆盖。
        actions = [a.action for a in (out.get("audit_logs") or [])]
        check("存在重试耗尽日志（node_error）", "node_error" in actions,
              f"动作序列={actions}")

    finally:
        minio_client.download_bytes = original


# ======================================================================
# 验证五：DOCX 走通节点
# ======================================================================

async def test_node_with_docx():
    print("\n[验证五] parse_document 节点消费 DOCX 解析结果")
    from app.services import parse_service

    parse_service.clear_parse_cache()

    docx_bytes = build_docx(
        paragraphs=[
            ("设备采购合同", "Heading 1"),
            ("合同编号：HT-2026-0918-002", None),
            ("甲方（采购方）：北京华信智能科技有限公司", None),
            ("统一社会信用代码：91110108MA01ABCDE", None),
            ("乙方（供应方）：上海鼎盛机械制造有限公司", None),
            ("第一条 合同标的", "Heading 2"),
            ("甲方向乙方采购工业机器人本体及配套控制系统。", None),
            ("第二条 合同金额", "Heading 2"),
            ("本合同总金额为人民币壹佰万元整（￥1,000,000.00元）。", None),
            ("第三条 履约期限", "Heading 2"),
            ("本合同自2026年10月1日起至2027年9月30日止。", None),
        ],
        tables=[[["项目", "数量", "单价"], ["服务器", "2", "50000"]]],
    )

    fake, original = patch_minio(docx_bytes)

    try:
        state = make_state(
            file_object_key="contracts/2026/verify_contract.docx",
        )
        updates = await nodes.parse_document(state)

        check("节点未被阻塞", updates.get("is_blocked") in (False, None),
              f"is_blocked={updates.get('is_blocked')}")
        check("DOCX 解析状态为 success",
              updates.get("parse_status") == "success",
              f"实际={updates.get('parse_status')}")

        doc_meta = updates.get("doc_metadata")
        check("DOCX 引擎为 docx_text", doc_meta.parse_engine == "docx_text",
              f"实际={doc_meta.parse_engine}")

        blocks = updates.get("text_blocks") or []
        full_text = updates.get("full_text", "")
        check("DOCX 文本块偏移精确",
              all(full_text[b.start_idx:b.end_idx] == b.text for b in blocks),
              f"块数={len(blocks)}")

        clauses = updates.get("clauses") or []
        check("DOCX 切分出条款", len(clauses) > 0, f"条款数={len(clauses)}")

    finally:
        unpatch_minio(original)


# ======================================================================
# 主流程
# ======================================================================

async def main():
    print("=" * 72)
    print("parse_document 节点 ←→ parse_service 端到端验证")
    print("=" * 72)

    await test_node_with_real_pdf()
    await test_node_with_encrypted_pdf()
    await test_node_with_empty_pdf()
    await test_retry_on_io_error()
    await test_node_with_docx()

    print("\n" + "=" * 72)
    print(f"结果：通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    if FAILED:
        print("-" * 72)
        for name, detail in FAILED:
            print(f"  [FAIL] {name} | {detail}")
    print("=" * 72)

    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
