# D:/ContractReviewSys-合同审批智能审查项目/backend/tests/verify_report_comment.py
"""
审查报告 / 审查意见 / 评论回写服务端到端验证

【验证目标】
    1. report_service：报告含风险清单五维度明细（等级/原文定位/成因/法律依据/推荐条款）
    2. comment_service.generate_comment_service：评论四段落 + 分级明细 + 格式校验
    3. comment_service.write_comment_service：幂等校验 + 预览模式 + 请求体清洗
    4. 权责红线：接口白名单、请求体剔除审批动作字段、"不改变审批结论"声明

【外部依赖隔离】
    MinIO / 审批系统均未就绪，通过 monkeypatch 注入 fake 隔离。
    被验证的代码路径（报告渲染、评论拼装、格式校验、幂等、请求体清洗、
    白名单校验）全部是真实执行的。

【运行方式】
    cd backend && python tests/verify_report_comment.py
"""

import asyncio
import os
import sys

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from app.constants import RiskLevel
from app.graph.state import (
    ReviewTaskState,
    RiskItem,
    WriteBackState,
    WriteBackInfo,
)
from app.services import comment_service, report_service
from app.utils.approval_guard import ensure_comment_only

PASSED = []
FAILED = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  [PASS] {name}")
    else:
        FAILED.append((name, detail))
        print(f"  [FAIL] {name} | {detail}")


def make_state():
    """构造含完整审查结果的测试状态"""
    risk_items = [
        RiskItem(
            risk_id="R1", rule_code="RISK-001", rule_name="单方免责表述",
            clause_id="C2", clause_type="liability",
            evidence_text="任何一方违约概不负责，双方互不追究。",
            char_start=50, char_end=80, page_no=1,
            risk_level=RiskLevel.CRITICAL.value,
            decision="block", severity="fatal",
            suggestion_text="建议修改为对等的责任承担条款，删除单方免责表述。",
            legal_basis="《中华人民共和国民法典》第四百九十七条",
            reference_only=False, matched_sources=["rule_engine"],
        ),
        RiskItem(
            risk_id="R2", rule_code="NUM-001", rule_name="违约金比例过高",
            clause_id="C3", clause_type="liability",
            evidence_text="应支付合同总额的百分之五十作为违约金。",
            char_start=100, char_end=130, page_no=1,
            risk_level=RiskLevel.HIGH.value,
            decision="need_revision", severity="major",
            suggestion_text="建议下调违约金比例至不超过合同总额的30%。",
            legal_basis="《中华人民共和国民法典》第五百八十五条第二款",
            reference_only=False, matched_sources=["rule_engine"],
        ),
    ]

    return ReviewTaskState(
        task_id="t1", task_no="T001", thread_id="th1", work_dir="w",
        approval_id="a1", contract_id="c1", contract_title="设备采购合同",
        contract_no="HT001", submit_dept="采购部", applicant="张三",
        counterparty="上海鼎盛机械制造有限公司", contract_amount=3000000.0,
        currency="CNY", business_type="采购", contract_type="purchase",
        route_type="standard",
        final_risk_items=risk_items,
        risk_level=RiskLevel.CRITICAL.value,
        risk_distribution={"critical": 1, "high": 1, "medium": 0, "low": 0},
        conclusion_text="本次合同审查最终风险等级为【严重风险】，需整改后重新提交。",
        conclusion_hash="abc123",
        rule_evaluated=True,
        rule_engine_version="v1.0.0",
        report_url="",
    )


# ======================================================================
# 验证一：报告生成（风险清单五维度明细）
# ======================================================================

def test_report_markdown():
    print("\n[验证一] 报告生成（风险清单五维度明细）")
    state = make_state()

    suggestion = report_service.build_suggestion_text(state.final_risk_items)
    markdown = report_service.render_report_markdown(state, suggestion)

    check("报告含合同标题", "设备采购合同" in markdown)
    check("报告含合同基本信息", "合同基本信息" in markdown)
    check("报告含审查结论", "审查结论" in markdown)
    check("报告含风险清单明细", "风险清单明细" in markdown)

    # 五维度明细检查
    check("含风险等级维度", "风险等级" in markdown)
    check("含原文定位维度", "原文定位" in markdown)
    check("含成因维度", "成因" in markdown)
    check("含法律依据维度", "法律依据" in markdown)
    check("含推荐修改条款维度", "推荐修改条款" in markdown)

    # 具体风险项内容
    check("含风险项 1 名称", "单方免责表述" in markdown)
    check("含风险项 2 名称", "违约金比例过高" in markdown)
    check("含法律依据条文", "第四百九十七条" in markdown)
    check("含修改建议", "对等的责任承担条款" in markdown)
    check("含原文定位字符区间", "字符区间" in markdown)

    # 免责声明
    check("含免责声明", "免责声明" in markdown)
    check("声明最终决策权归审批人", "最终审批决策权保留给审批人" in markdown)

    return markdown


# ======================================================================
# 验证二：评论生成（四段落 + 分级明细 + 格式校验）
# ======================================================================

async def test_generate_comment():
    print("\n[验证二] 评论生成（四段落 + 分级明细 + 格式校验）")
    state = make_state()

    result = await comment_service.generate_comment_service(state)

    check("返回 CommentResult", isinstance(result, comment_service.CommentResult))
    check("评论非空", bool(result.comment_text))
    check("风险项数为 2", result.risk_item_count == 2, f"实际={result.risk_item_count}")

    # 四段落检查
    check("含风险摘要段落", "一、风险摘要" in result.comment_text)
    check("含分级风险明细段落", "二、分级风险明细" in result.comment_text)
    check("含修改建议汇总段落", "三、修改建议汇总" in result.comment_text)
    check("含人工提示段落", "四、人工提示" in result.comment_text)

    # 分级明细：按等级分组
    check("含严重风险分组", "【严重风险】" in result.comment_text)
    check("含高风险分组", "【高风险】" in result.comment_text)
    check("严重风险排在高风险之前",
          result.comment_text.index("【严重风险】") < result.comment_text.index("【高风险】"))

    # 红线声明
    check("含'不改变审批结论'声明", "不改变审批结论" in result.comment_text)
    check("含'最终决策权保留给审批人'", "最终决策权保留给审批人" in result.comment_text)

    # 格式校验
    check("格式校验通过", result.validation_passed is True,
          f"问题={result.validation_issues}")

    return result


# ======================================================================
# 验证三：评论回写（幂等 + 预览 + 请求体清洗）
# ======================================================================

async def test_write_comment():
    print("\n[验证三] 评论回写（幂等 + 预览模式 + 请求体清洗）")
    state = make_state()

    # 先构造评论内容（模拟 generate_comment 已完成）
    comment_result = await comment_service.generate_comment_service(state)
    state.write_back_content = WriteBackInfo(
        status=WriteBackState.WRITING.value,
        comment_payload=comment_result.payload,
        comment_text=comment_result.comment_text,
        api_path=comment_result.api_path,
    )
    state.write_back_status = WriteBackState.WRITING.value

    # -------------------- 幂等校验：success 状态应跳过（不重复提交） --------------------
    state_success = state.model_copy(deep=True)
    state_success.write_back_status = WriteBackState.SUCCESS.value
    skipped = await comment_service.write_comment_service(state_success, preview=False)
    # 幂等跳过的语义：返回原 write_back_content，不做任何修改/不重复提交
    check("success 状态幂等跳过（返回原 write_back_content 不重写）",
          skipped == state_success.write_back_content,
          f"返回 status={skipped.status}，原 status={state_success.write_back_content.status}")

    # -------------------- 预览模式：仅记录不写入 --------------------
    preview_info = await comment_service.write_comment_service(state, preview=True)
    check("预览模式返回 writing 状态", preview_info.status == WriteBackState.WRITING.value,
          f"实际={preview_info.status}")
    check("预览模式未实际写入（response_body 含 preview 标记）",
          preview_info.response_body.get("preview") is True,
          f"response_body={preview_info.response_body}")
    check("预览模式错误信息说明未写入", "未实际写入" in preview_info.error)

    # -------------------- 请求体清洗：剔除审批动作字段 --------------------
    # 构造含 action 字段的 payload，验证 write_comment_service 会剔除
    state_polluted = state.model_copy(deep=True)
    state_polluted.write_back_content = WriteBackInfo(
        status=WriteBackState.WRITING.value,
        comment_payload={
            **comment_result.payload,
            "action": "approve",  # 越权字段
            "decision": "block",
        },
        comment_text=comment_result.comment_text,
        api_path=comment_result.api_path,
    )

    # 用 mock httpx 捕获实际提交的请求体
    submitted = {}

    class FakeResponse:
        status_code = 200
        text = '{"code": 0, "message": "ok"}'

        def json(self):
            return {"code": 0, "message": "ok"}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, json=None, headers=None):
            submitted["body"] = json
            submitted["url"] = url
            return FakeResponse()

    # 注入 fake client
    import httpx
    original_async_client = httpx.AsyncClient
    httpx.AsyncClient = FakeAsyncClient
    try:
        write_info = await comment_service.write_comment_service(
            state_polluted, preview=False
        )
    finally:
        httpx.AsyncClient = original_async_client

    check("正式模式回写成功", write_info.status == WriteBackState.SUCCESS.value,
          f"实际={write_info.status}")
    check("正式模式实际提交了请求", "body" in submitted)
    check("请求体不含 action 字段", "action" not in submitted.get("body", {}),
          f"body keys={sorted(submitted.get('body', {}).keys())}")
    check("请求体不含 decision 字段", "decision" not in submitted.get("body", {}))
    check("请求体含评论文本", "contract_review_comment" in submitted.get("body", {}))
    check("请求 URL 指向评论接口", "/comments" in submitted.get("url", ""))


# ======================================================================
# 验证四：权责红线（白名单 + 不改变审批结论）
# ======================================================================

def test_redline():
    print("\n[验证四] 权责红线（白名单 + 不改变审批结论）")

    # 白名单校验：评论接口通过
    check("评论接口通过白名单校验", ensure_comment_only("/comments") is True)

    # 黑名单拦截：审批动作接口拒绝
    from app.utils.approval_guard import ApprovalPermissionException

    for forbidden in ["/approve", "/reject", "/transfer", "/withdraw"]:
        try:
            ensure_comment_only(forbidden)
            check(f"审批动作接口 {forbidden} 被拒绝", False, "未拒绝")
        except ApprovalPermissionException:
            check(f"审批动作接口 {forbidden} 被拒绝", True)

    # 未授权接口拒绝
    try:
        ensure_comment_only("/unknown")
        check("未授权接口被拒绝", False, "未拒绝")
    except ApprovalPermissionException:
        check("未授权接口被拒绝", True)


# ======================================================================
# 主流程
# ======================================================================

async def main() -> int:
    print("=" * 72)
    print("审查报告 / 审查意见 / 评论回写服务端到端验证")
    print("=" * 72)

    test_report_markdown()
    await test_generate_comment()
    await test_write_comment()
    test_redline()

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
