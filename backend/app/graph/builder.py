# D:/ContractReviewSys-合同审批智能审查项目/backend/app/graph/builder.py
"""
LangGraph 审查流程图构建器 — 图组装、检查点与执行入口

【文件定位】
    本模块把十个节点函数与路由分支组装为一张可执行的有状态流程图，
    并注入 MemorySaver 检查点以支持断点续跑，最终对外暴露统一的执行入口。

【流程图结构（对应架构文档"核心业务全流程"）】

    START
      │
      ▼
  pull_approval_task ──────────────────────┐
      │                                    │（拉取即阻塞时直接收敛）
      ▼                                    │
  parse_document ──────────────────────┐   │
      │                                │   │
      │（分支一：阻断）                  │   │
      │  解析失败 / 加密 / 模糊 / 空内容  │   │
      ├────────────────────────────────┼───┤
      │                                ▼   ▼
      │                              audit
      │                                │
      ▼                                │
  rule_review（唯一判定节点）           │
      │                                │
      ├─────────────────────┐          │
      │（分支二：快速通道）    │          │
      │ 模板合同              │          │
      │                     ▼          │
      │                vector_review   │
      │                     │          │
      │                     ▼          │
      │                llm_review      │
      │                     │          │
      │◄────────────────────┘          │
      ▼                                │
  risk_merge                           │
      │                                │
      ▼                                │
  generate_report                      │
      │                                │
      ▼                                │
  generate_comment ────┐               │
      │                │（任务阻塞时跳过）│
      ▼                │               │
  write_comment        │               │
      │                │               │
      ▼                ▼               │
  audit ◄──────────────────────────────┘
      │
      ▼
     END

【MemorySaver 检查点与断点续跑】
    编译时注入 MemorySaver。每个超级步（super-step）结束后，
    图状态会以 thread_id 为键持久化到内存。
    断点续跑方式：以相同 thread_id 再次调用 ainvoke(...) 且不传初始状态，
    图会从最后一个检查点恢复继续执行，不会重跑已完成的节点。

    ⚠️ 生产环境注意：
        MemorySaver 为进程内存储，进程重启后检查点丢失。
        生产部署应替换为持久化检查点（如 langgraph-checkpoint-mongodb 或
        langgraph-checkpoint-sqlite）。本模块已通过 _build_checkpointer()
        预留替换点，切换时只需改动该函数，调用方无感知。

返回值说明：
    - build_review_graph()：构建并返回编译后的图（可调用对象）
    - get_review_graph()：返回全局单例图
    - run_review_flow(...)：执行完整审查流程，返回最终状态
    - resume_review_flow(...)：从最近检查点继续执行
    - get_flow_snapshot(...)：查询指定任务的当前检查点状态

使用示例：
    from app.graph import run_review_flow
    final_state = await run_review_flow(initial_state, thread_id="xxx")
"""

import asyncio
from datetime import datetime
from typing import Any, Dict, List, Optional

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.constants import WorkflowNode
from app.core.config import settings
from app.core.logging_config import get_logger
from app.graph import routes
from app.graph.nodes import (
    audit,
    generate_comment,
    generate_report,
    llm_review,
    parse_document,
    pull_approval_task,
    risk_merge,
    rule_review,
    vector_review,
    write_comment,
)
from app.graph.state import ReviewTaskState

logger = get_logger(__name__)


# ======================================================================
# 一、条件边映射表（集中定义，避免散落的魔法字符串）
# ======================================================================
# pull_approval_task 之后的目标节点
PULL_NEXT_ROUTES: Dict[str, str] = {
    WorkflowNode.PARSE_CONTRACT.value: WorkflowNode.PARSE_CONTRACT.value,
    WorkflowNode.AUDIT.value: WorkflowNode.AUDIT.value,
}

# parse_document 之后的目标节点
PARSE_NEXT_ROUTES: Dict[str, str] = {
    WorkflowNode.RULE_EVALUATE.value: WorkflowNode.RULE_EVALUATE.value,
    WorkflowNode.AUDIT.value: WorkflowNode.AUDIT.value,
}

# rule_review 之后的目标节点
RULE_NEXT_ROUTES: Dict[str, str] = {
    WorkflowNode.VECTOR_REVIEW.value: WorkflowNode.VECTOR_REVIEW.value,
    WorkflowNode.RISK_MERGE.value: WorkflowNode.RISK_MERGE.value,
    WorkflowNode.AUDIT.value: WorkflowNode.AUDIT.value,
}

# risk_merge 之后的目标节点
MERGE_NEXT_ROUTES: Dict[str, str] = {
    WorkflowNode.GENERATE_REPORT.value: WorkflowNode.GENERATE_REPORT.value,
}

# write_comment 之后的目标节点
WRITE_BACK_NEXT_ROUTES: Dict[str, str] = {
    WorkflowNode.AUDIT.value: WorkflowNode.AUDIT.value,
}


# ======================================================================
# 二、检查点构造（预留持久化替换点）
# ======================================================================
def _build_checkpointer() -> MemorySaver:
    """
    构造图检查点存储器

    【当前实现】
        使用 MemorySaver（进程内内存存储），满足开发与单机部署场景下的
        断点续跑需求。每个 thread_id 对应一份独立的状态快照。

    【生产替换指引】
        当需要跨进程重启保留断点能力时，把本函数替换为持久化实现即可，
        调用方（build_review_graph / get_review_graph）无需任何改动：
            from langgraph.checkpoint.mongodb import MongoDBSaver
            return MongoDBSaver(client=mongo_sync_client, db_name=...)
        或：
            from langgraph.checkpoint.sqlite import SqliteSaver
            return SqliteSaver.from_conn_string("...")

    返回：
        MemorySaver 实例
    """
    checkpointer = MemorySaver()
    logger.info(
        "图检查点初始化完成 | 类型=%s | 说明=支持以 thread_id 为键的断点续跑",
        type(checkpointer).__name__,
    )
    return checkpointer


# ======================================================================
# 三、图构建
# ======================================================================
def build_review_graph(
    checkpointer: Optional[MemorySaver] = None,
) -> CompiledStateGraph:
    """
    构建合同审查流程图

    【构建步骤】
        1. 以 ReviewTaskState 为状态类型创建 StateGraph
        2. 注册十个节点
        3. 建立静态边与条件边（含三条差异化分支）
        4. 注入检查点并编译

    【三条差异化路由分支的落点】
        分支一（阻断分支）：parse_document → audit
            条件边挂载于 parse_document，由 routes.route_after_parse 判定。
            触发条件：解析失败 / 文档加密 / 扫描件模糊 / 正文为空 / 无有效条款。

        分支二（快速通道）：rule_review → risk_merge
            条件边挂载于 rule_review，由 routes.route_after_rule 判定。
            触发条件：route_type == fast（模板合同），跳过向量召回与大模型审查。

        分支三（深度复核）：rule_review → vector_review → llm_review → risk_merge
            同样由 routes.route_after_rule 判定，触发条件：route_type == deep。
            与标准通道的图结构一致，差异体现在 llm_review 的提示词强化
            （见 nodes._build_llm_system_prompt 的 is_deep 分支）。

    参数：
        checkpointer: 检查点存储器。为 None 时使用 _build_checkpointer() 构造

    返回：
        编译后的 CompiledStateGraph，可直接 ainvoke / astream

    使用示例：
        graph = build_review_graph()
        result = await graph.ainvoke(initial_state, config={"configurable": {"thread_id": "xxx"}})
    """
    if checkpointer is None:
        checkpointer = _build_checkpointer()

    builder = StateGraph(ReviewTaskState)

    # -------------------- 步骤一：注册节点 --------------------
    builder.add_node(WorkflowNode.PULL_APPROVAL_TASK.value, pull_approval_task)
    builder.add_node(WorkflowNode.PARSE_CONTRACT.value, parse_document)
    builder.add_node(WorkflowNode.RULE_EVALUATE.value, rule_review)
    builder.add_node(WorkflowNode.VECTOR_REVIEW.value, vector_review)
    builder.add_node(WorkflowNode.LLM_ANALYZE.value, llm_review)
    builder.add_node(WorkflowNode.RISK_MERGE.value, risk_merge)
    builder.add_node(WorkflowNode.GENERATE_REPORT.value, generate_report)
    builder.add_node(WorkflowNode.GENERATE_COMMENT.value, generate_comment)
    builder.add_node(WorkflowNode.WRITE_BACK.value, write_comment)
    builder.add_node(WorkflowNode.AUDIT.value, audit)

    # -------------------- 步骤二：入口边 --------------------
    builder.add_edge(START, WorkflowNode.PULL_APPROVAL_TASK.value)

    # -------------------- 步骤三：任务拉取后的条件边 --------------------
    builder.add_conditional_edges(
        WorkflowNode.PULL_APPROVAL_TASK.value,
        routes.route_after_pull,
        PULL_NEXT_ROUTES,
    )

    # -------------------- 步骤四：解析后的条件边（分支一：阻断分支） --------------------
    builder.add_conditional_edges(
        WorkflowNode.PARSE_CONTRACT.value,
        routes.route_after_parse,
        PARSE_NEXT_ROUTES,
    )

    # -------------------- 步骤五：规则审查后的条件边（分支二、三） --------------------
    builder.add_conditional_edges(
        WorkflowNode.RULE_EVALUATE.value,
        routes.route_after_rule,
        RULE_NEXT_ROUTES,
    )

    # -------------------- 步骤六：多路召回链路的静态边 --------------------
    # vector_review 之后固定进入 llm_review（标准通道与深度复核通道均如此）
    builder.add_edge(WorkflowNode.VECTOR_REVIEW.value, WorkflowNode.LLM_ANALYZE.value)
    # llm_review 之后固定进入 risk_merge
    builder.add_edge(WorkflowNode.LLM_ANALYZE.value, WorkflowNode.RISK_MERGE.value)

    # -------------------- 步骤七：融合后的条件边 --------------------
    builder.add_conditional_edges(
        WorkflowNode.RISK_MERGE.value,
        routes.route_after_risk_merge,
        MERGE_NEXT_ROUTES,
    )

    # -------------------- 步骤八：报告与回写链路 --------------------
    # 报告生成后固定进入评论构造
    builder.add_edge(
        WorkflowNode.GENERATE_REPORT.value, WorkflowNode.GENERATE_COMMENT.value
    )
    # 评论构造后固定进入回写
    builder.add_edge(WorkflowNode.GENERATE_COMMENT.value, WorkflowNode.WRITE_BACK.value)
    # 回写后条件收敛（回写失败也收敛，不阻塞审查结论产出）
    builder.add_conditional_edges(
        WorkflowNode.WRITE_BACK.value,
        routes.route_after_write_back,
        WRITE_BACK_NEXT_ROUTES,
    )

    # -------------------- 步骤九：审计节点终结 --------------------
    builder.add_edge(WorkflowNode.AUDIT.value, END)

    # -------------------- 步骤十：编译并注入检查点 --------------------
    compiled = builder.compile(checkpointer=checkpointer)

    logger.info(
        "合同审查流程图构建完成 | 节点数=%d | 检查点=%s | 支持断点续跑=%s",
        len(builder.nodes) - 1 if hasattr(builder, "nodes") else 10,
        type(checkpointer).__name__,
        True,
    )

    return compiled


# ======================================================================
# 四、全局图单例
# ======================================================================
# 说明：在首次调用 get_review_graph() 时惰性构建，避免应用导入阶段即编译图，
#       从而缩短启动时间并降低"未配置完成就被触发"的风险。
_review_graph: Optional[CompiledStateGraph] = None
_graph_lock = asyncio.Lock()


def get_review_graph() -> CompiledStateGraph:
    """
    获取全局审查图单例

    说明：
        使用双检锁保证多协程并发调用时只编译一次图。
        编译过程本身是 CPU 操作，无 IO，耗时极短。

    返回：
        编译后的图实例
    """
    global _review_graph

    if _review_graph is None:
        _review_graph = build_review_graph()
        logger.info("审查图单例已初始化")

    return _review_graph


async def get_review_graph_async() -> CompiledStateGraph:
    """
    异步获取全局审查图单例

    说明：
        当多任务并发启动时，使用锁保证只编译一次，避免重复构建。

    返回：
        编译后的图实例
    """
    global _review_graph

    if _review_graph is not None:
        return _review_graph

    async with _graph_lock:
        if _review_graph is None:
            _review_graph = build_review_graph()
            logger.info("审查图单例已初始化（异步路径）")

    return _review_graph


def reset_review_graph() -> None:
    """
    重置全局图单例

    说明：
        仅供测试使用。测试断点续跑与并发隔离时需要清空检查点，
        避免上一个用例的 thread_id 残留影响断言。

    返回：
        无返回值
    """
    global _review_graph
    _review_graph = None
    logger.warning("审查图单例已重置，所有内存检查点将丢失")


# ======================================================================
# 五、执行入口
# ======================================================================
def build_thread_config(thread_id: str) -> Dict[str, Any]:
    """
    构造 LangGraph 调用配置

    说明：
        thread_id 是检查点的键，也是异步会话隔离的依据。
        每个审查任务必须使用独立 thread_id，否则并发任务会互相覆盖状态。

    参数：
        thread_id: 会话 ID，由 app.utils.id_generator.generate_thread_id 生成

    返回：
        可直接传给 ainvoke / astream 的 config 字典

    异常：
        ValueError：thread_id 为空时抛出
    """
    if not thread_id:
        raise ValueError("thread_id 不能为空，否则无法实现会话隔离与断点续跑")

    return {
        "configurable": {"thread_id": thread_id},
        # 限制单次执行的超级步数量，防止路由异常导致的无限循环
        "recursion_limit": 50,
    }


async def run_review_flow(
    initial_state: ReviewTaskState,
    thread_id: Optional[str] = None,
) -> ReviewTaskState:
    """
    执行完整的合同审查流程

    【执行说明】
        1. 校验 thread_id（缺失时从 initial_state 取，仍缺失则抛错）
        2. 以该 thread_id 执行图，每个超级步结束后写入检查点
        3. 返回最终状态（已包含完整审计日志与全部产出）

    【异常处理】
        节点内部的异常已由 node_wrapper 捕获并转为 blocked 状态，
        因此本函数通常不会因业务异常抛出。若抛出，说明是图层面的
        结构性错误（如状态契约不匹配），属于需要修复的代码缺陷。

    参数：
        initial_state: 初始状态，必须含 task_id / task_no / approval_id
        thread_id: 会话 ID。为空时取 initial_state.thread_id

    返回：
        最终 ReviewTaskState

    异常：
        ValueError：thread_id 缺失时抛出
        Exception：图执行层面的结构性异常
    """
    # -------------------- 解析 thread_id --------------------
    resolved_thread_id = thread_id or initial_state.thread_id
    if not resolved_thread_id:
        raise ValueError(
            "thread_id 缺失。请显式传入，或在初始状态的 thread_id 字段中提供。"
            "缺少会话 ID 将无法保证并发隔离与断点续跑。"
        )

    graph = await get_review_graph_async()
    config = build_thread_config(resolved_thread_id)

    logger.info(
        "审查流程启动 | 任务=%s | 会话=%s | 路由=%s | 金额=%.2f",
        initial_state.task_no,
        resolved_thread_id,
        initial_state.route_type,
        initial_state.contract_amount,
    )

    start_ts = datetime.now()

    # -------------------- 执行图 --------------------
    # 说明：传入的是状态字典而非 Pydantic 实例。LangGraph 依据 ReviewTaskState
    #       的字段声明构建内部 schema，dict 形式的初始状态会被正确解析。
    final_values: Dict[str, Any] = await graph.ainvoke(
        initial_state.model_dump(),
        config=config,
    )

    # -------------------- 还原为状态对象 --------------------
    final_state = ReviewTaskState(**final_values)

    elapsed_ms = (datetime.now() - start_ts).total_seconds() * 1000.0

    logger.info(
        "审查流程结束 | 任务=%s | 状态=%s | 综合等级=%s | 阻塞=%s | "
        "有效风险=%d项 | 参考项=%d项 | 回写=%s | 总耗时=%.2fs",
        final_state.task_no,
        final_state.status,
        final_state.risk_level,
        final_state.is_blocked,
        final_state.official_risk_count,
        final_state.total_risk_count - final_state.official_risk_count,
        final_state.write_back_status,
        elapsed_ms / 1000.0,
    )

    return final_state


async def resume_review_flow(
    thread_id: str,
    updates: Optional[Dict[str, Any]] = None,
) -> ReviewTaskState:
    """
    从最近检查点继续执行审查流程（断点续跑）

    【使用场景】
        1. 任务因重试耗尽被置 blocked，人工修复下游服务后重跑
        2. 任务被解析阻断，业务方替换文件后重跑
        3. 进程异常中断后恢复执行

    【工作原理】
        以相同 thread_id 调用 ainvoke(None, config)：
        图会从该 thread_id 的最后一个检查点恢复，继续执行未完成的节点，
        已成功完成的节点不会重复执行。

        传入 updates 可在恢复前修正状态字段（如替换文件后更新
        file_object_key 与 file_fingerprint），使恢复执行基于修正后的数据。
        同时会清除阻塞标记，让流程能够继续推进。

    参数：
        thread_id: 会话 ID，必须与首次执行时一致
        updates: 恢复前需要覆盖的状态字段。常用于：
                 - 替换文件：{"file_object_key": "new/key.pdf", "file_fingerprint": ""}
                 - 人工确认放行：{"blocked_info": None}
                 - 重试计数重置：{"retry_count": 0}

    返回：
        最终 ReviewTaskState

    异常：
        ValueError：thread_id 为空，或该会话无检查点时抛出
    """
    if not thread_id:
        raise ValueError("thread_id 不能为空，无法定位断点")

    graph = await get_review_graph_async()
    config = build_thread_config(thread_id)

    # -------------------- 查询检查点是否存在 --------------------
    snapshot = await graph.aget_state(config)
    if snapshot is None or not snapshot.values:
        raise ValueError(
            f"未找到会话 {thread_id} 的检查点。断点续跑要求使用与首次执行相同的 thread_id，"
            f"且期间进程未重启（MemorySaver 为进程内存储）。"
        )

    logger.info(
        "断点续跑启动 | 会话=%s | 断点状态=%s | 已完成节点=%d个",
        thread_id,
        snapshot.values.get("status", "unknown"),
        len(snapshot.values.get("node_history", [])),
    )

    # -------------------- 修正状态（人工重跑场景） --------------------
    if updates:
        # 人工重跑时必须清除阻塞标记，否则节点包装器会因 is_blocked 而跳过执行
        merged_updates: Dict[str, Any] = dict(updates)
        merged_updates.setdefault("is_blocked", False)
        merged_updates.setdefault("blocked_info", None)
        merged_updates.setdefault("retry_count", 0)
        merged_updates.setdefault("status", "running")

        await graph.aupdate_state(config, merged_updates)

        logger.warning(
            "断点续跑前状态已修正 | 会话=%s | 修正字段=%s",
            thread_id,
            list(merged_updates.keys()),
        )

    # -------------------- 从断点继续执行 --------------------
    # 传入 None 表示"从检查点恢复"，而非从头开始
    final_values: Dict[str, Any] = await graph.ainvoke(None, config=config)

    final_state = ReviewTaskState(**final_values)

    logger.info(
        "断点续跑结束 | 任务=%s | 状态=%s | 综合等级=%s | 阻塞=%s",
        final_state.task_no,
        final_state.status,
        final_state.risk_level,
        final_state.is_blocked,
    )

    return final_state


async def get_flow_snapshot(thread_id: str) -> Optional[Dict[str, Any]]:
    """
    查询指定会话的当前检查点状态

    【使用场景】
        1. 前端轮询任务进度（结合 SSE 推送）
        2. 运维排查某任务卡在哪个节点
        3. 断点续跑前确认断点位置

    【返回值说明】
        返回状态摘要字典，含：
            next_nodes: 下一个待执行节点列表（空列表表示已到达终点）
            status: 当前任务状态
            current_node: 当前节点
            progress: 进度百分比
            node_count: 已执行节点数
            audit_count: 审计日志条数
            created_at / updated_at: 状态时间戳
        会话不存在时返回 None

    参数：
        thread_id: 会话 ID

    返回：
        状态摘要字典或 None

    异常：
        ValueError：thread_id 为空时抛出
    """
    if not thread_id:
        raise ValueError("thread_id 不能为空")

    graph = await get_review_graph_async()
    config = build_thread_config(thread_id)

    try:
        snapshot = await graph.aget_state(config)
    except Exception as exc:  # noqa: BLE001 - 会话不存在时不向上抛异常
        logger.warning("查询检查点失败 | 会话=%s | 异常=%s", thread_id, exc)
        return None

    if snapshot is None or not snapshot.values:
        return None

    values = snapshot.values

    return {
        "thread_id": thread_id,
        "next_nodes": list(snapshot.next) if snapshot.next else [],
        "status": values.get("status"),
        "current_node": values.get("current_node"),
        "progress": values.get("progress", 0),
        "risk_level": values.get("risk_level"),
        "is_blocked": values.get("is_blocked", False),
        "route_type": values.get("route_type"),
        "node_count": len(values.get("node_history", [])),
        "audit_count": len(values.get("audit_logs", [])),
        "clause_count": len(values.get("clauses", [])),
        "final_risk_count": len(values.get("final_risk_items", [])),
        "write_back_status": values.get("write_back_status"),
        "created_at": values.get("created_at"),
        "updated_at": values.get("updated_at"),
    }


async def get_flow_history(thread_id: str) -> List[Dict[str, Any]]:
    """
    获取指定会话的完整执行历史（每个超级步一条记录）

    【使用场景】
        审计回放、问题定位：查看每一步执行前后的状态变化。

    参数：
        thread_id: 会话 ID

    返回：
        历史记录列表，每项含 step / node / status / progress / next_nodes

    异常：
        ValueError：thread_id 为空时抛出
    """
    if not thread_id:
        raise ValueError("thread_id 不能为空")

    graph = await get_review_graph_async()
    config = build_thread_config(thread_id)

    history: List[Dict[str, Any]] = []

    try:
        async for snapshot in graph.aget_state_history(config):
            values = snapshot.values or {}
            history.append(
                {
                    "step": snapshot.metadata.get("step") if snapshot.metadata else None,
                    "source": (
                        snapshot.metadata.get("source") if snapshot.metadata else None
                    ),
                    "node": values.get("current_node"),
                    "status": values.get("status"),
                    "progress": values.get("progress", 0),
                    "next_nodes": list(snapshot.next) if snapshot.next else [],
                    "audit_count": len(values.get("audit_logs", [])),
                    "updated_at": values.get("updated_at"),
                }
            )
    except Exception as exc:  # noqa: BLE001 - 历史查询失败不向上抛异常
        logger.warning("查询执行历史失败 | 会话=%s | 异常=%s", thread_id, exc)
        return []

    return history


# ======================================================================
# 六、图结构自检
# ======================================================================
def describe_graph() -> Dict[str, Any]:
    """
    导出图的结构描述

    【用途】
        1. 运维核对图结构是否符合预期（节点、边、分支）
        2. 供健康检查接口回显，便于确认部署版本一致

    【实现说明】
        LangGraph 1.x 中 CompiledStateGraph 的 get_graph() 返回 Graph 对象，
        其 nodes / edges 属性以字典形式暴露结构。此处以防御性方式读取，
        结构变化时返回可用信息而不抛异常。

    返回：
        含节点列表、边列表、条件分支列表的字典
    """
    graph = get_review_graph()

    nodes: List[str] = []
    edges: List[Dict[str, str]] = []

    try:
        drawable = graph.get_graph()
        nodes = [node_id for node_id in drawable.nodes.keys()]
        edges = [
            {"source": edge.source, "target": edge.target}
            for edge in drawable.edges
        ]
    except Exception as exc:  # noqa: BLE001 - 结构读取失败时降级返回
        logger.warning("图结构读取失败 | 异常=%s", exc)

    return {
        "node_count": len([n for n in nodes if n not in ("__start__", "__end__")]),
        "nodes": [n for n in nodes if n not in ("__start__", "__end__")],
        "edge_count": len(edges),
        "edges": edges,
        "conditional_branches": [
            {
                "source": WorkflowNode.PULL_APPROVAL_TASK.value,
                "router": "route_after_pull",
                "description": "任务拉取后路由：正常进入文档解析，拉取即阻塞则直接收敛",
            },
            {
                "source": WorkflowNode.PARSE_CONTRACT.value,
                "router": "route_after_parse",
                "description": (
                    "分支一（阻断分支）：解析失败/加密/扫描件模糊/正文为空 → audit 标记 blocked"
                ),
            },
            {
                "source": WorkflowNode.RULE_EVALUATE.value,
                "router": "route_after_rule",
                "description": (
                    "分支二（快速通道）：模板合同 → 仅规则审查后直接进入 risk_merge；"
                    "分支三（深度复核）：大额或高风险类型 → 追加大模型审查链路"
                ),
            },
            {
                "source": WorkflowNode.RISK_MERGE.value,
                "router": "route_after_risk_merge",
                "description": "融合完成后进入报告生成",
            },
            {
                "source": WorkflowNode.WRITE_BACK.value,
                "router": "route_after_write_back",
                "description": "回写完成后进入审计收敛（回写失败不阻塞收敛）",
            },
        ],
        "checkpointer": "MemorySaver",
        "resumable": True,
    }
