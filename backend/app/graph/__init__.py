# D:/ContractReviewSys-合同审批智能审查项目/backend/app/graph/__init__.py
"""
LangGraph 编排层统一出口

【架构定位】
    本层是系统的 L3 编排层（主调度器），负责把 L2 能力引擎（规则引擎 / 大模型 /
    向量化）与 L1 存储层串联成完整的审查业务流程。它是唯一有权决定
    "何时调用哪个能力、按什么顺序调用"的地方。

    L4 接口层 (api)
         │
         ▼
    L3 编排层 (graph) ← 本层：状态机 + 差异化路由 + 异常重试 + 断点续跑
         │
         ├──► L2 能力层 (engines)：规则判定 / 模型调用 / 向量化
         └──► L1 存储层 (storage)：MongoDB / MinIO / Milvus

【模块构成】
    ┌──────────────────┬────────────────────────────────────────────────┐
    │ 模块              │ 职责                                            │
    ├──────────────────┼────────────────────────────────────────────────┤
    │ state            │ 图状态契约 ReviewTaskState（Pydantic v2）        │
    │ node_utils       │ 节点执行外壳：异常捕获 + 指数退避 + 审计埋点       │
    │ nodes            │ 十个节点的业务实现                               │
    │ routes           │ 三条差异化路由分支的判定逻辑                      │
    │ builder          │ 图组装、MemorySaver 检查点、执行与续跑入口        │
    └──────────────────┴────────────────────────────────────────────────┘

【架构红线（本层的代码级强制点）】
    1. 十个节点中，只有 rule_review 有权产出风险等级、处理决策与阻塞判定。
    2. vector_review 与 llm_review 的产出结构在数据模型层面就不含风险等级字段。
    3. risk_merge 融合时绝不生成新的等级，所有等级直接复制自规则引擎结果。
    4. write_comment 调用审批接口前必须通过白名单校验，且请求体不含任何
       审批动作字段（同意/拒绝/转交）。
    5. 每个节点执行都必须留下审计日志，流程可完整回放。

【断点续跑】
    图编译时注入 MemorySaver。以 thread_id 为键持久化每个超级步的状态，
    通过 resume_review_flow(thread_id) 可从最近检查点继续执行。

返回值说明：
    本模块仅做聚合导出，无函数返回值

使用示例：
    from app.graph import run_review_flow, get_flow_snapshot

    # 执行完整审查
    final_state = await run_review_flow(initial_state, thread_id="xxx")

    # 查询进度
    snapshot = await get_flow_snapshot("xxx")

    # 人工修复后断点续跑
    final_state = await resume_review_flow("xxx", updates={"file_object_key": "new/key.pdf"})
"""

from app.graph.builder import (
    build_review_graph,
    build_thread_config,
    describe_graph,
    get_flow_history,
    get_flow_snapshot,
    get_review_graph,
    get_review_graph_async,
    reset_review_graph,
    resume_review_flow,
    run_review_flow,
)
from app.graph.node_utils import (
    NodeResult,
    NonRetryableError,
    node_wrapper,
)
from app.graph.routes import (
    build_route_snapshot,
    decide_route_type,
    route_after_parse,
    route_after_pull,
    route_after_risk_merge,
    route_after_rule,
    route_after_write_back,
)
from app.graph.state import (
    AuditAction,
    AuditLogEntry,
    BlockedInfo,
    ClauseItem,
    DocMetadata,
    LLMCandidate,
    NodeExecutionRecord,
    ParagraphNode,
    ParseStatus,
    ReviewTaskState,
    RiskItem,
    TextBlock,
    VectorCandidate,
    WriteBackInfo,
    WriteBackState,
)

__all__ = [
    # 状态契约
    "ReviewTaskState",
    "TextBlock",
    "ParagraphNode",
    "DocMetadata",
    "ClauseItem",
    "LLMCandidate",
    "VectorCandidate",
    "RiskItem",
    "BlockedInfo",
    "WriteBackInfo",
    "AuditLogEntry",
    "NodeExecutionRecord",
    "ParseStatus",
    "WriteBackState",
    "AuditAction",
    # 节点基础设施
    "node_wrapper",
    "NodeResult",
    "NonRetryableError",
    # 路由
    "decide_route_type",
    "route_after_pull",
    "route_after_parse",
    "route_after_rule",
    "route_after_risk_merge",
    "route_after_write_back",
    "build_route_snapshot",
    # 图构建与执行
    "build_review_graph",
    "get_review_graph",
    "get_review_graph_async",
    "reset_review_graph",
    "build_thread_config",
    "run_review_flow",
    "resume_review_flow",
    "get_flow_snapshot",
    "get_flow_history",
    "describe_graph",
]
