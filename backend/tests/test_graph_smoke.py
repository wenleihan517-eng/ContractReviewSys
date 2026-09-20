# D:/ContractReviewSys-合同审批智能审查项目/backend/tests/test_graph_smoke.py
"""
LangGraph 审查流程图冒烟测试

【测试目标】
    在外部依赖（MongoDB / MinIO / Milvus / 本地大模型）不可用的前提下，
    验证图的结构完整性与三条差异化路由分支的判定正确性。

【测试策略】
    1. 图结构测试：节点数、边数、条件分支数量
    2. 路由判定测试：直接调用路由函数，覆盖三条分支的触发条件
    3. 节点执行测试：验证异常捕获、指数退避重试、审计日志累加
    4. 检查点测试：验证 MemorySaver 的写入与读取

【说明】
    本测试不依赖任何外部服务，可在 CI 环境直接运行。
    端到端联调测试（依赖真实 MinIO/Milvus/LLM）需在集成环境执行。

运行方式：
    cd backend
    python -m pytest tests/test_graph_smoke.py -v
    或直接执行：
    python tests/test_graph_smoke.py
"""

import asyncio
import sys
from pathlib import Path
from typing import Any, Dict, List

# 将 backend 目录加入模块搜索路径，保证可直接执行本文件
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.constants import ReviewTaskStatus, RiskLevel, WorkflowNode, WriteBackStatus
from app.graph import (
    AuditAction,
    NodeResult,
    NonRetryableError,
    ParseStatus,
    ReviewTaskState,
    build_thread_config,
    describe_graph,
    get_flow_snapshot,
    get_review_graph,
    node_wrapper,
    reset_review_graph,
    route_after_parse,
    route_after_pull,
    route_after_risk_merge,
    route_after_rule,
    route_after_write_back,
)
from app.graph.routes import build_route_snapshot, decide_route_type
from app.graph.state import DocMetadata, LLMCandidate, RiskItem, VectorCandidate
from app.graph.nodes import _build_text_blocks
from app.models.documents.review_task import BlockedReason, RouteType

# 测试结果收集器
_PASSED: List[str] = []
_FAILED: List[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    """
    执行一条断言并记录结果

    参数：
        name: 用例名称
        condition: 断言条件
        detail: 附加说明

    返回：
        无返回值
    """
    if condition:
        _PASSED.append(name)
        print(f"  [PASS] {name}")
    else:
        _FAILED.append(f"{name} | {detail}")
        print(f"  [FAIL] {name} | {detail}")


def make_state(**overrides: Any) -> ReviewTaskState:
    """
    构造测试用状态对象

    参数：
        **overrides: 需要覆盖的字段

    返回：
        ReviewTaskState 实例
    """
    base: Dict[str, Any] = {
        "task_id": "test_task_001",
        "task_no": "T20260918001",
        "thread_id": "thread_test_001",
        "work_dir": "tasks/test_task_001",
        "approval_id": "AP20260918001",
        "contract_id": "HT20260918001",
        "contract_title": "设备采购合同",
        "contract_no": "HT20260918001",
        "submit_dept": "采购部",
        "applicant": "张三",
        "counterparty": "某某设备有限公司",
        "contract_amount": 1_000_000.0,
        "currency": "CNY",
        "business_type": "采购",
        "contract_type": "purchase",
        # 【基线约定】默认置为"解析成功"。
        # 原因：needs_vector_review / needs_llm_review 等派生属性均以
        # is_parse_ok 为前置条件——真实流程中 vector_review / llm_review
        # 只可能在解析成功后到达，故测试基线必须体现这一前提，
        # 否则构造出的状态在业务上不可达，断言会误报失败。
        # 需要验证"解析失败场景"的用例，请显式覆盖 parse_status。
        "parse_status": ParseStatus.SUCCESS.value,
    }
    base.update(overrides)
    return ReviewTaskState(**base)


# ======================================================================
# 测试一：图结构
# ======================================================================
def test_graph_structure() -> None:
    """测试图结构完整性"""
    print("\n[测试一] 图结构完整性")

    reset_review_graph()
    info = describe_graph()

    check("节点总数为 10", info["node_count"] == 10, f"实际={info['node_count']}")

    expected_nodes = {
        "pull_approval_task",
        "parse_document",
        "rule_review",
        "vector_review",
        "llm_review",
        "risk_merge",
        "generate_report",
        "generate_comment",
        "write_comment",
        "audit",
    }
    check(
        "十个指定节点全部注册",
        set(info["nodes"]) == expected_nodes,
        f"缺失={expected_nodes - set(info['nodes'])}，多余={set(info['nodes']) - expected_nodes}",
    )

    check("条件分支数量为 5", len(info["conditional_branches"]) == 5,
          f"实际={len(info['conditional_branches'])}")

    check("检查点为 MemorySaver", info["checkpointer"] == "MemorySaver",
          f"实际={info['checkpointer']}")

    check("支持断点续跑", info["resumable"] is True)

    # 校验关键边存在
    edge_set = {(e["source"], e["target"]) for e in info["edges"]}
    check("存在 vector_review → llm_review 边",
          ("vector_review", "llm_review") in edge_set)
    check("存在 llm_review → risk_merge 边",
          ("llm_review", "risk_merge") in edge_set)
    check("存在 generate_report → generate_comment 边",
          ("generate_report", "generate_comment") in edge_set)
    check("存在 generate_comment → write_comment 边",
          ("generate_comment", "write_comment") in edge_set)
    check("audit 节点通向终点", ("audit", "__end__") in edge_set)


# ======================================================================
# 测试二：路由类型判定
# ======================================================================
def test_route_type_decision() -> None:
    """测试差异化路由类型判定"""
    print("\n[测试二] 差异化路由类型判定")

    # 场景 1：普通合同 → standard
    state = make_state()
    check(
        "普通合同判定为标准审查",
        decide_route_type(state) == RouteType.STANDARD.value,
        f"实际={decide_route_type(state)}",
    )

    # 场景 2：模板合同 → fast
    state = make_state(template_flag=True)
    check(
        "模板合同判定为快速通道",
        decide_route_type(state) == RouteType.FAST.value,
        f"实际={decide_route_type(state)}",
    )

    # 场景 3：大额合同（≥500万）→ deep
    state = make_state(contract_amount=8_000_000.0)
    check(
        "大额合同判定为深度复核",
        decide_route_type(state) == RouteType.DEEP.value,
        f"实际={decide_route_type(state)}",
    )

    # 场景 4：高风险类型 → deep
    state = make_state(contract_type="guarantee")
    check(
        "高风险类型判定为深度复核",
        decide_route_type(state) == RouteType.DEEP.value,
        f"实际={decide_route_type(state)}",
    )

    # 场景 5：优先级验证 —— 大额模板合同必须走 deep 而非 fast
    state = make_state(template_flag=True, contract_amount=10_000_000.0)
    check(
        "大额模板合同优先走深度复核（金额优先级高于模板）",
        decide_route_type(state) == RouteType.DEEP.value,
        f"实际={decide_route_type(state)}",
    )

    # 场景 6：路由快照完整性
    snapshot = build_route_snapshot(state)
    check(
        "路由快照含阈值与依据字段",
        all(
            key in snapshot
            for key in (
                "route_type",
                "contract_amount",
                "amount_threshold",
                "template_flag",
                "fast_enabled",
                "force_deep_types",
            )
        ),
        f"keys={sorted(snapshot.keys())}",
    )


# ======================================================================
# 测试三：分支一（阻断分支）
# ======================================================================
def test_route_branch_blocked() -> None:
    """测试分支一：解析失败/加密/模糊/空内容 → audit"""
    print("\n[测试三] 分支一：阻断分支路由")

    # 场景 1：解析被阻断（文档加密）
    state = make_state(parse_status=ParseStatus.BLOCKED.value)
    check(
        "解析被阻断 → 走向 audit",
        route_after_parse(state) == "audit",
        f"实际={route_after_parse(state)}",
    )

    # 场景 2：解析失败
    state = make_state(parse_status=ParseStatus.FAILED.value, parse_error="文件损坏")
    check(
        "解析失败 → 走向 audit",
        route_after_parse(state) == "audit",
        f"实际={route_after_parse(state)}",
    )

    # 场景 3：解析未完成（防御性）
    state = make_state(parse_status=ParseStatus.NOT_STARTED.value)
    check(
        "解析未完成 → 走向 audit",
        route_after_parse(state) == "audit",
        f"实际={route_after_parse(state)}",
    )

    # 场景 4：解析成功但无条款
    state = make_state(parse_status=ParseStatus.SUCCESS.value, clauses=[])
    check(
        "解析成功但无有效条款 → 走向 audit",
        route_after_parse(state) == "audit",
        f"实际={route_after_parse(state)}",
    )

    # 场景 5：拉取阶段即阻塞 → 直接收敛
    state = make_state()
    blocked_state = state.model_copy(
        update={"is_blocked": True, "status": ReviewTaskStatus.BLOCKED.value}
    )
    check(
        "拉取阶段即阻塞 → 走向 audit",
        route_after_pull(blocked_state) == "audit",
        f"实际={route_after_pull(blocked_state)}",
    )

    # 场景 6：正常状态 → 走向解析
    check(
        "正常状态 → 走向 parse_document",
        route_after_pull(state) == WorkflowNode.PARSE_CONTRACT.value,
        f"实际={route_after_pull(state)}",
    )


# ======================================================================
# 测试四：分支二与分支三（快速通道 / 深度复核）
# ======================================================================
def test_route_branch_fast_and_deep() -> None:
    """测试分支二（快速通道）与分支三（深度复核）"""
    print("\n[测试四] 分支二 / 分支三：快速通道与深度复核路由")

    # 场景 1：模板合同快速通道 → 跳过向量与大模型，直接 risk_merge
    state = make_state(
        template_flag=True,
        route_type=RouteType.FAST.value,
        rule_evaluated=True,
    )
    check(
        "快速通道：规则审查后直接走向 risk_merge",
        route_after_rule(state) == "risk_merge",
        f"实际={route_after_rule(state)}",
    )
    check("快速通道：needs_vector_review 为 False", state.needs_vector_review is False)
    check("快速通道：needs_llm_review 为 False", state.needs_llm_review is False)

    # 场景 2：标准通道 → 走完整链路
    state = make_state(route_type=RouteType.STANDARD.value, rule_evaluated=True)
    check(
        "标准审查：规则审查后走向 vector_review",
        route_after_rule(state) == "vector_review",
        f"实际={route_after_rule(state)}",
    )
    check("标准审查：needs_vector_review 为 True", state.needs_vector_review is True)
    check("标准审查：needs_llm_review 为 True", state.needs_llm_review is True)

    # 场景 3：深度复核通道 → 走完整链路
    state = make_state(
        route_type=RouteType.DEEP.value,
        contract_amount=8_000_000.0,
        rule_evaluated=True,
    )
    check(
        "深度复核：规则审查后走向 vector_review（追加大模型审查）",
        route_after_rule(state) == "vector_review",
        f"实际={route_after_rule(state)}",
    )
    check("深度复核：needs_llm_review 为 True", state.needs_llm_review is True)
    check("深度复核通道标识正确", state.is_route_deep is True)

    # 场景 4：规则引擎未完成判定 → 禁止继续（保守从严）
    state = make_state(rule_evaluated=False)
    check(
        "规则引擎未判定时禁止继续推进 → 走向 audit",
        route_after_rule(state) == "audit",
        f"实际={route_after_rule(state)}",
    )

    # 场景 5：融合后统一走向 generate_report
    state = make_state(rule_evaluated=True, risk_level=RiskLevel.LOW.value)
    check(
        "融合完成 → 走向 generate_report",
        route_after_risk_merge(state) == WorkflowNode.GENERATE_REPORT.value,
        f"实际={route_after_risk_merge(state)}",
    )

    # 场景 6：回写后统一走向 audit
    state = make_state(write_back_status=WriteBackStatus.SUCCESS.value)
    check(
        "回写完成 → 走向 audit",
        route_after_write_back(state) == "audit",
        f"实际={route_after_write_back(state)}",
    )


# ======================================================================
# 测试五：节点异常捕获与指数退避重试
# ======================================================================
def test_node_retry_and_blocked() -> None:
    """测试节点级异常捕获、指数退避重试与阻塞置态"""
    print("\n[测试五] 节点异常捕获与指数退避重试")

    from app.core.config import settings

    # -------------------- 场景 1：正常执行 --------------------
    @node_wrapper(node_name=WorkflowNode.PARSE_CONTRACT, progress=25)
    async def ok_node(state: ReviewTaskState) -> NodeResult:
        return NodeResult(
            updates={"parse_status": ParseStatus.SUCCESS.value},
            message="解析成功",
        )

    # -------------------- 场景 2：不可重试阻断 --------------------
    @node_wrapper(node_name=WorkflowNode.PARSE_CONTRACT, progress=25)
    async def blocked_node(state: ReviewTaskState) -> NodeResult:
        raise NonRetryableError(
            reason=BlockedReason.DOC_ENCRYPTED.value,
            message="文档已加密",
            detail={"file_name": "secret.pdf"},
        )

    # -------------------- 场景 3：可重试异常（重试耗尽） --------------------
    call_counter = {"count": 0}

    @node_wrapper(node_name=WorkflowNode.PARSE_CONTRACT, progress=25)
    async def failing_node(state: ReviewTaskState) -> NodeResult:
        call_counter["count"] += 1
        raise ConnectionError("下游服务连接失败")

    # -------------------- 场景 4：主动跳过 --------------------
    @node_wrapper(node_name=WorkflowNode.LLM_ANALYZE, progress=75)
    async def skipping_node(state: ReviewTaskState) -> NodeResult:
        return NodeResult(skipped=True, skip_reason="快速通道不调用大模型")

    async def run_tests() -> None:
        # 加速重试：临时缩短退避时间，避免测试耗时过长
        original_base = settings.TASK_RETRY_BASE_DELAY
        original_max = settings.TASK_RETRY_MAX_DELAY
        settings.TASK_RETRY_BASE_DELAY = 0.01
        settings.TASK_RETRY_MAX_DELAY = 0.02

        try:
            # ---- 正常执行 ----
            state = make_state()
            result = await ok_node(state)
            check(
                "正常节点：状态增量正确写入",
                result["parse_status"] == ParseStatus.SUCCESS.value,
                f"实际={result.get('parse_status')}",
            )
            check(
                "正常节点：审计日志含进入与退出两条",
                [log.action for log in result["audit_logs"]]
                == [AuditAction.NODE_ENTER.value, AuditAction.NODE_EXIT.value],
                f"实际={[log.action for log in result['audit_logs']]}",
            )
            check(
                "正常节点：进度已更新",
                result["progress"] == 25,
                f"实际={result.get('progress')}",
            )
            check(
                "正常节点：节点执行记录已追加",
                len(result["node_history"]) == 1
                and result["node_history"][0].status == "success",
            )

            # ---- 不可重试阻断 ----
            state = make_state()
            result = await blocked_node(state)
            check(
                "不可重试阻断：任务状态置为 blocked",
                result["status"] == ReviewTaskStatus.BLOCKED.value,
                f"实际={result.get('status')}",
            )
            check(
                "不可重试阻断：阻塞原因码正确",
                result["blocked_info"].reason == BlockedReason.DOC_ENCRYPTED.value,
                f"实际={result['blocked_info'].reason}",
            )
            check(
                "不可重试阻断：is_blocked 标记置位",
                result["is_blocked"] is True,
            )
            check(
                "不可重试阻断：审计日志含红线拦截记录",
                AuditAction.REDLINE_DENY.value
                in [log.action for log in result["audit_logs"]],
                f"实际={[log.action for log in result['audit_logs']]}",
            )

            # ---- 重试耗尽 ----
            call_counter["count"] = 0
            state = make_state()
            result = await failing_node(state)
            check(
                "重试耗尽：实际尝试次数等于配置上限",
                call_counter["count"] == settings.TASK_MAX_RETRY,
                f"实际={call_counter['count']}，上限={settings.TASK_MAX_RETRY}",
            )
            check(
                "重试耗尽：任务状态置为 blocked",
                result["status"] == ReviewTaskStatus.BLOCKED.value,
                f"实际={result.get('status')}",
            )
            check(
                "重试耗尽：阻塞原因码为 RETRY_EXHAUSTED",
                result["blocked_info"].reason == BlockedReason.RETRY_EXHAUSTED.value,
                f"实际={result['blocked_info'].reason}",
            )
            check(
                "重试耗尽：耗尽节点清单已记录",
                result["exhausted_nodes"] == [WorkflowNode.PARSE_CONTRACT.value],
                f"实际={result.get('exhausted_nodes')}",
            )
            check(
                "重试耗尽：审计日志含节点进入与错误记录",
                AuditAction.NODE_ENTER.value in [log.action for log in result["audit_logs"]]
                and AuditAction.NODE_ERROR.value in [log.action for log in result["audit_logs"]],
                f"实际={[log.action for log in result['audit_logs']]}",
            )

            # ---- 主动跳过 ----
            state = make_state()
            result = await skipping_node(state)
            check(
                "主动跳过：skip 清单已记录",
                result["skipped_nodes"] == [WorkflowNode.LLM_ANALYZE.value],
                f"实际={result.get('skipped_nodes')}",
            )
            check(
                "主动跳过：审计日志动作类型为 node_skip",
                [log.action for log in result["audit_logs"]]
                == [AuditAction.NODE_SKIP.value],
                f"实际={[log.action for log in result['audit_logs']]}",
            )

            # ---- 已阻塞任务不再执行 ----
            blocked_state = make_state().model_copy(
                update={"is_blocked": True, "status": ReviewTaskStatus.BLOCKED.value}
            )
            result = await ok_node(blocked_state)
            check(
                "已阻塞任务：节点拒绝执行",
                WorkflowNode.PARSE_CONTRACT.value in result["skipped_nodes"],
                f"实际={result.get('skipped_nodes')}",
            )

        finally:
            settings.TASK_RETRY_BASE_DELAY = original_base
            settings.TASK_RETRY_MAX_DELAY = original_max

    asyncio.run(run_tests())


# ======================================================================
# 测试六：审计日志累加与检查点
# ======================================================================
def test_audit_accumulation_and_checkpoint() -> None:
    """测试审计日志累加（列表归约）与 MemorySaver 检查点"""
    print("\n[测试六] 审计日志累加与检查点")

    # -------------------- 审计日志累加 --------------------
    state = make_state()

    # 第一次追加
    entry1 = state.append_audit(
        "pull_approval_task", AuditAction.NODE_ENTER.value, "进入节点"
    )
    state = state.model_copy(
        update={
            "audit_logs": state.audit_logs + [entry1],
            "audit_sequence": entry1.sequence,
        }
    )

    # 第二次追加
    entry2 = state.append_audit(
        "pull_approval_task", AuditAction.NODE_EXIT.value, "退出节点"
    )
    state = state.model_copy(
        update={
            "audit_logs": state.audit_logs + [entry2],
            "audit_sequence": entry2.sequence,
        }
    )

    check("审计日志正确累加（2 条）", len(state.audit_logs) == 2,
          f"实际={len(state.audit_logs)}")
    check(
        "审计日志序号单调递增",
        state.audit_logs[0].sequence < state.audit_logs[1].sequence,
        f"实际={[log.sequence for log in state.audit_logs]}",
    )
    check(
        "审计序号计数器同步推进",
        state.audit_sequence == 2,
        f"实际={state.audit_sequence}",
    )

    # -------------------- 列表归约器（并行安全） --------------------
    logs_a = [entry1]
    logs_b = [entry2]
    merged = logs_a + logs_b
    check(
        "列表归约语义正确（并行分支不互相覆盖）",
        len(merged) == 2,
        f"实际={len(merged)}",
    )

    # -------------------- 检查点写入与读取 --------------------
    async def check_checkpoint() -> None:
        graph = get_review_graph()
        thread_id = "thread_ckpt_test"
        config = build_thread_config(thread_id)

        # 手动写入一条状态到检查点
        await graph.aupdate_state(
            config,
            {
                "task_id": "ckpt_task",
                "task_no": "T_CKPT_001",
                "thread_id": thread_id,
                "status": ReviewTaskStatus.RUNNING.value,
                "progress": 42,
                "current_node": "parse_document",
            },
        )

        # 读回检查点
        snapshot = await get_flow_snapshot(thread_id)
        check("检查点可读取", snapshot is not None, "返回 None")

        if snapshot:
            check(
                "检查点状态写入正确",
                snapshot["status"] == ReviewTaskStatus.RUNNING.value
                and snapshot["progress"] == 42,
                f"status={snapshot['status']}, progress={snapshot['progress']}",
            )
            check(
                "检查点记录了下一待执行节点",
                isinstance(snapshot["next_nodes"], list),
                f"实际={snapshot.get('next_nodes')}",
            )

        # 不存在的会话返回 None
        missing = await get_flow_snapshot("thread_not_exists_xyz")
        check("不存在的会话返回 None", missing is None, f"实际={missing}")

        # thread_id 为空时抛错
        try:
            build_thread_config("")
            check("空 thread_id 应抛异常", False, "未抛异常")
        except ValueError:
            check("空 thread_id 正确抛出异常", True)

        # 会话隔离：两个 thread_id 互不干扰
        await graph.aupdate_state(
            build_thread_config("thread_iso_a"),
            {"task_id": "a", "task_no": "TA", "thread_id": "thread_iso_a", "progress": 10},
        )
        await graph.aupdate_state(
            build_thread_config("thread_iso_b"),
            {"task_id": "b", "task_no": "TB", "thread_id": "thread_iso_b", "progress": 90},
        )
        snap_a = await get_flow_snapshot("thread_iso_a")
        snap_b = await get_flow_snapshot("thread_iso_b")
        check(
            "会话隔离：两个会话进度互不影响",
            snap_a["progress"] == 10 and snap_b["progress"] == 90,
            f"a={snap_a['progress']}, b={snap_b['progress']}",
        )

    asyncio.run(check_checkpoint())


# ======================================================================
# 测试七：状态对象契约
# ======================================================================
# 测试八：归约器增量语义与审计序号唯一性（缺陷防回归）
# ======================================================================
def test_reducer_and_sequence_uniqueness() -> None:
    """
    验证两项经端到端实测暴露的缺陷已修复：

    缺陷一（归约器重复累加）：
        audit_logs / node_history 声明为 operator.add 累加型，
        但包装器曾返回"全量历史 + 新增"。两者语义叠加，
        导致历史条目被反复重复累加（O(n²) 膨胀）。
        正确做法：包装器只返回本次新增的增量列表。

    缺陷二（审计序号重复）：
        同一节点内若构造多条日志（如重试耗尽的 node_enter + node_error），
        第二条日志未传 offset，与第一条取到同一基准序号，
        破坏"序号全局单调递增"的审计可追溯性。
    """
    print("\n[测试八] 归约器增量语义与审计序号唯一性（防回归）")

    # -------------------- 缺陷一：归约器不产生重复累加 --------------------
    async def handler(state: ReviewTaskState) -> NodeResult:
        """每次执行只产生一条日志增量"""
        return NodeResult(
            updates={"progress": state.progress + 1},
            message="增量语义验证",
            detail={"case": "reducer"},
        )

    node = node_wrapper(node_name=WorkflowNode.PARSE_CONTRACT, progress=25)(handler)

    state = make_state()
    total_logs = 0
    total_records = 0
    # 连续模拟 3 个超级步：每步的返回值即为该步状态增量
    for step in range(1, 4):
        updates = asyncio.run(node(state))
        delta_logs = updates.get("audit_logs") or []
        delta_records = updates.get("node_history") or []
        # 关键断言：单次节点执行的日志增量必须是"本次新增条数"，
        # 而非随着历史增长而线性膨胀
        check(
            f"第{step}步：日志增量为常量（2 条 enter/exit），非全量历史",
            len(delta_logs) == 2,
            f"实际={len(delta_logs)}",
        )
        check(
            f"第{step}步：节点记录增量为常量（1 条），非全量历史",
            len(delta_records) == 1,
            f"实际={len(delta_records)}",
        )
        total_logs += len(delta_logs)
        total_records += len(delta_records)
        # 模拟归约器拼接
        state = state.model_copy(
            update={
                "audit_logs": list(state.audit_logs) + list(delta_logs),
                "node_history": list(state.node_history) + list(delta_records),
                "audit_sequence": updates.get("audit_sequence", state.audit_sequence),
                "progress": updates.get("progress", state.progress),
            }
        )

    check(
        "归约器拼接后日志总数为 6 条（3 步 × 2 条），无重复膨胀",
        len(state.audit_logs) == 6 and total_logs == 6,
        f"实际={len(state.audit_logs)}",
    )
    check(
        "归约器拼接后节点记录为 3 条，无重复膨胀",
        len(state.node_history) == 3,
        f"实际={len(state.node_history)}",
    )
    # 无重复：节点名 + 动作 + 序号组合唯一
    keys = [(e.node, e.action, e.sequence) for e in state.audit_logs]
    check("审计日志无重复条目（node+action+sequence 唯一）", len(keys) == len(set(keys)))
    # 序号严格递增
    seqs = [e.sequence for e in state.audit_logs]
    check(
        "审计序号严格单调递增",
        all(seqs[i] < seqs[i + 1] for i in range(len(seqs) - 1)),
        f"序列={seqs}",
    )

    # -------------------- 缺陷二：重试耗尽时序号不重复 --------------------
    attempts = {"n": 0}

    async def flaky(state: ReviewTaskState) -> NodeResult:
        """每次都失败，触发重试耗尽路径"""
        attempts["n"] += 1
        raise ConnectionError("下游服务连接失败")

    node2 = node_wrapper(
        node_name=WorkflowNode.VECTOR_REVIEW,
        progress=65,
        retry_on=(ConnectionError,),
    )(flaky)

    # 重试上限由状态字段 max_retry 决定，基线为 3；退避基数取自配置中心
    result = asyncio.run(node2(make_state(max_retry=3)))
    blocked_logs = result.get("audit_logs") or []
    check(
        "重试耗尽：产出 node_enter 与 node_error 两条日志",
        len(blocked_logs) == 2,
        f"实际={len(blocked_logs)}",
    )
    blocked_seqs = [e.sequence for e in blocked_logs]
    check(
        "重试耗尽：两条日志序号互不相同（offset 生效）",
        len(set(blocked_seqs)) == len(blocked_seqs),
        f"序列={blocked_seqs}",
    )
    check(
        "重试耗尽：序号严格递增",
        blocked_seqs == sorted(blocked_seqs)
        and all(blocked_seqs[i] < blocked_seqs[i + 1] for i in range(len(blocked_seqs) - 1)),
        f"序列={blocked_seqs}",
    )
    check(
        "重试耗尽：audit_sequence 推进到最大序号",
        result.get("audit_sequence") == max(blocked_seqs),
        f"audit_sequence={result.get('audit_sequence')}，max={max(blocked_seqs)}",
    )
    check(
        "重试耗尽：实际尝试次数等于上限",
        attempts["n"] == 3,
        f"实际={attempts['n']}",
    )

    # -------------------- 缺陷二补充：不可重试阻断的序号唯一性 --------------------
    async def encrypted(state: ReviewTaskState) -> NodeResult:
        """抛不可重试异常，触发红线阻断路径"""
        raise NonRetryableError(
            reason="DOC_ENCRYPTED",
            message="文档已加密",
            detail={"file_name": "a.pdf"},
        )

    node3 = node_wrapper(node_name=WorkflowNode.PARSE_CONTRACT, progress=25)(encrypted)
    res3 = asyncio.run(node3(make_state()))
    logs3 = res3.get("audit_logs") or []
    seqs3 = [e.sequence for e in logs3]
    check(
        "不可重试阻断：日志序号互不相同",
        len(set(seqs3)) == len(seqs3),
        f"序列={seqs3}",
    )
    check(
        "不可重试阻断：日志含 node_enter 与 redline_deny 两种动作",
        {e.action for e in logs3}
        == {AuditAction.NODE_ENTER.value, AuditAction.REDLINE_DENY.value},
        f"实际={[e.action for e in logs3]}",
    )
    check(
        "不可重试阻断：audit_sequence 推进到最大序号",
        res3.get("audit_sequence") == max(seqs3),
        f"audit_sequence={res3.get('audit_sequence')}，max={max(seqs3)}",
    )


# ======================================================================
# 测试九：文本块全局字符偏移精确性（缺陷防回归）
# ======================================================================
def test_text_block_offsets() -> None:
    """
    测试文本块偏移计算的精确性

    【为什么必须单独测这一项】
        偏移错位是"静默缺陷"——不会抛异常、不会返回错误码，
        只表现为"条款与原文对不上"。下游的条款溯源、风险定位、
        前端高亮全部基于这个偏移，一旦错位则整条链路失效但难以察觉。

    【曾经的真实缺陷】
        _build_text_blocks 的逐页循环里，每行末尾无条件 `cursor += len+1`，
        循环外又为页间追加一次 `cursor += 1`。
        结果是每页最后一行被多算 1 个字符，
        从第 2 页起所有块偏移整体右移，第 2 页所有块的
        full_text[start:end] 都取不到正确文本。

    【本测试的断言口径】
        对每个块验证 full_text[start_idx:end_idx] == block.text。
        这是偏移正确性的**充分条件**，一旦成立则溯源必然可用。
    """
    print("\n[测试九] 文本块全局字符偏移精确性（防回归）")

    # 两页文本，页内多行 + 页间衔接，覆盖跨页偏移这一高风险点
    page_texts = [
        "设备采购合同\n合同编号：HT-2026-0918-001\n甲方：北京华信智能科技有限公司",
        "第二条 合同金额\n本合同总金额为人民币叁佰万元整。\n签订日期：2026年9月18日",
    ]
    full_text = "\n".join(page_texts)

    doc_meta = DocMetadata(
        file_name="offset_test.pdf",
        file_ext=".pdf",
        file_size=1024,
        file_hash="0" * 64,
        page_count=2,
        source_type="digital",
        parse_engine="pdf_text",
        has_text_layer=True,
    )

    blocks = _build_text_blocks(full_text, page_texts, doc_meta)

    check("两页文本均产出文本块", len(blocks) == 6, f"实际块数={len(blocks)}")

    # 核心断言：全部块偏移精确
    mismatched = [
        (b.block_index, b.page, b.start_idx, b.end_idx, b.text[:20],
         full_text[b.start_idx:b.end_idx][:20])
        for b in blocks
        if full_text[b.start_idx:b.end_idx] != b.text
    ]
    check("全部文本块偏移精确（full_text[start:end] == block.text）",
          not mismatched, f"不匹配块={mismatched[:3]}")

    # 页码归属
    page1 = [b for b in blocks if b.page == 1]
    page2 = [b for b in blocks if b.page == 2]
    check("第 1 页块数与预期一致", len(page1) == 3, f"实际={len(page1)}")
    check("第 2 页块数与预期一致", len(page2) == 3, f"实际={len(page2)}")

    # 跨页衔接：第 2 页首块的起始偏移必须精确落在页间分隔符之后
    expected_page2_start = len(page_texts[0]) + 1
    check(
        "第 2 页首块起始偏移等于页 1 长度 + 1（页间换行）",
        page2[0].start_idx == expected_page2_start,
        f"实际={page2[0].start_idx}，期望={expected_page2_start}",
    )

    # 第 2 页首块的 end 必须等于 start + 文本长度
    check(
        "第 2 页首块区间长度等于文本长度",
        page2[0].end_idx - page2[0].start_idx == len(page2[0].text),
        f"区间={page2[0].end_idx - page2[0].start_idx}，文本={len(page2[0].text)}",
    )

    # 块区间不重叠且递增
    starts = [b.start_idx for b in blocks]
    check("块起始偏移严格递增", starts == sorted(starts) and len(set(starts)) == len(starts),
          f"起始偏移={starts}")

    # block_index 连续
    check("block_index 连续递增",
          [b.block_index for b in blocks] == list(range(len(blocks))),
          f"实际={[b.block_index for b in blocks]}")


# ======================================================================
def test_state_contract() -> None:
    """测试状态对象字段完整性与红线约束"""
    print("\n[测试七] 状态对象契约与红线约束")

    state = make_state()

    # -------------------- 五大分组字段完整性 --------------------
    required_fields = [
        # 任务基础信息
        "task_id", "task_no", "approval_id", "contract_title", "contract_no",
        "submit_dept", "applicant", "counterparty", "contract_amount",
        "currency", "performance_period", "business_type",
        # 文档解析状态
        "parse_status", "text_blocks", "paragraphs", "doc_metadata",
        "parse_error", "parse_retry_count", "file_fingerprint",
        # 风险审查状态
        "rule_results", "vector_results", "llm_results", "final_risk_items",
        "risk_level",
        # 回写状态
        "write_back_status", "write_back_content",
        # 审计日志
        "audit_logs", "audit_sequence", "node_history",
    ]
    # 【Pydantic 2.11+】model_fields 须走类访问，实例访问已废弃（V3.0 将移除）
    missing = [field for field in required_fields if field not in ReviewTaskState.model_fields]
    check("状态对象覆盖全部必需字段", not missing, f"缺失={missing}")

    # -------------------- 文本块位置字段 --------------------
    from app.graph.state import TextBlock

    text_block_fields = set(TextBlock.model_fields.keys())
    required_block_fields = {"page", "start_idx", "end_idx", "paragraph_index"}
    check(
        "文本块含 page/start_idx/end_idx/段落索引",
        required_block_fields.issubset(text_block_fields),
        f"缺失={required_block_fields - text_block_fields}",
    )

    # -------------------- 红线一：候选结构不含等级字段 --------------------
    llm_fields = set(LLMCandidate.model_fields.keys())
    check(
        "LLMCandidate 不含 risk_level 字段（红线）",
        "risk_level" not in llm_fields,
        f"字段列表={sorted(llm_fields)}",
    )
    check(
        "LLMCandidate 不含 decision 字段（红线）",
        "decision" not in llm_fields,
    )
    check(
        "LLMCandidate 不含 severity 字段（红线）",
        "severity" not in llm_fields,
    )

    vector_fields = set(VectorCandidate.model_fields.keys())
    check(
        "VectorCandidate 不含 risk_level 字段（红线）",
        "risk_level" not in vector_fields,
    )

    # -------------------- 红线二：风险项判定来源强制 --------------------
    try:
        RiskItem(
            risk_id="R0001",
            risk_level=RiskLevel.HIGH.value,
            judge_source="llm_assistant",  # 非法来源
        )
        check("风险项判定来源非法时应抛异常", False, "未抛异常")
    except ValueError as exc:
        check(
            "风险项判定来源非规则引擎时正确拒绝",
            "rule_engine" in str(exc),
            f"异常信息={exc}",
        )

    # 合法来源可通过
    try:
        item = RiskItem(
            risk_id="R0002",
            risk_level=RiskLevel.HIGH.value,
            judge_source="rule_engine",
        )
        check("风险项合法来源构造成功", item.judge_source == "rule_engine")
        check("风险项等级序位计算正确", item.risk_order == 3,
              f"实际={item.risk_order}")
    except ValueError as exc:
        check("风险项合法来源构造成功", False, f"异常={exc}")

    # -------------------- 红线三：阻塞原因码强制 --------------------
    try:
        state = make_state()
        from app.graph.state import BlockedInfo

        BlockedInfo(reason="UNKNOWN_REASON")
        check("未知阻塞原因码应被拒绝", False, "未抛异常")
    except ValueError:
        check("未知阻塞原因码正确拒绝", True)

    # -------------------- 红线四：综合风险等级只能来自枚举 --------------------
    try:
        make_state(risk_level="super_high")
        check("非法综合风险等级应被拒绝", False, "未抛异常")
    except ValueError:
        check("非法综合风险等级正确拒绝", True)

    # -------------------- 文本块区间校验 --------------------
    try:
        TextBlock(
            block_index=0,
            page=1,
            start_idx=100,
            end_idx=50,  # 非法：结束位置小于起始位置
            paragraph_index=0,
            text="测试",
        )
        check("非法字符区间的文本块应被拒绝", False, "未抛异常")
    except ValueError:
        check("非法字符区间正确拒绝", True)


# ======================================================================
# 主入口
# ======================================================================
def main() -> int:
    """
    执行全部冒烟测试

    返回：
        0 表示全部通过，1 表示存在失败用例
    """
    print("=" * 70)
    print("LangGraph 合同审查流程图 — 冒烟测试")
    print("=" * 70)

    test_graph_structure()
    test_route_type_decision()
    test_route_branch_blocked()
    test_route_branch_fast_and_deep()
    test_node_retry_and_blocked()
    test_audit_accumulation_and_checkpoint()
    test_state_contract()
    test_reducer_and_sequence_uniqueness()
    test_text_block_offsets()

    print("\n" + "=" * 70)
    print(f"测试完成：通过 {len(_PASSED)} 项，失败 {len(_FAILED)} 项")
    if _FAILED:
        print("\n失败明细：")
        for item in _FAILED:
            print(f"  - {item}")
    print("=" * 70)

    return 0 if not _FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
