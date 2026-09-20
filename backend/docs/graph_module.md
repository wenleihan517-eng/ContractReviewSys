# D:/ContractReviewSys-合同审批智能审查项目/backend/docs/graph_module.md
# LangGraph 有状态审查流程图 — 模块交付说明

> 本文档对应"模块三：合同审查流程的状态定义与有状态流程图"。
> 代码位于 `backend/app/graph/`，共 6 个文件、5170 行。

---

## 一、文件清单与职责

| 文件 | 行数 | 职责 |
|---|---|---|
| `app/graph/state.py` | 1124 | 状态契约：ReviewTaskState 八大分组 + 12 个数据模型 + 3 个枚举 |
| `app/graph/node_utils.py` | 825 | 节点执行外壳：异常捕获、指数退避重试、审计埋点、终态判定 |
| `app/graph/routes.py` | 376 | 三条差异化路由 + 路由依据快照 |
| `app/graph/nodes.py` | 1957 | 十个节点实现 + 全部确定性辅助函数 |
| `app/graph/builder.py` | 747 | 图组装、MemorySaver 检查点、执行与断点续跑入口 |
| `app/graph/__init__.py` | 141 | 聚合出口，含分层架构图与红线声明 |

---

## 二、状态对象（state.py）

### 分组结构

| 分组 | 关键字段 |
|---|---|
| **A 任务基础信息** | task_id / task_no / thread_id / approval_id / contract_title / contract_no / submit_dept / applicant / counterparty / contract_amount / currency / business_type / contract_type / template_flag / source_type / file_object_key |
| **B 文档解析状态** | parse_status / text_blocks / paragraph_tree / doc_metadata / parse_error / parse_retry_count / file_fingerprint / raw_text / quality_score / clauses |
| **C 风险审查状态** | rule_results / rule_evaluated / vector_candidates / llm_candidates / risk_items / risk_level / risk_summary |
| **D 回写状态** | write_back_state / write_back_info / write_back_retry_count |
| **E 生成产物** | report_markdown / report_object_key / comment_content |
| **F 审计日志** | audit_logs（累加型）/ audit_sequence / node_history（累加型） |
| **G 异常与错误** | error_message / retry_count / max_retry / exhausted_nodes / skipped_nodes |
| **H 流程控制** | route_type / status / progress / current_node / is_blocked / blocked_info |

### 累加型字段（**关键**）

```python
from operator import add as _list_add
ListReducer = Annotated[List[Any], _list_add]

audit_logs: ListReducer = Field(default_factory=list)
node_history: ListReducer = Field(default_factory=list)
```

> **铁律**：节点返回值中，这两个字段**只能放本次新增的条目**，绝不能写 `list(state.audit_logs) + [...]`。
> 归约器会负责拼接；若节点自己又拼了历史，会造成历史重复累加（O(n²) 膨胀）。

### 红线在数据层的落地

```python
class LLMCandidate(BaseModel):
    # 【架构红线】刻意不提供 risk_level / decision / severity 字段
    # 大模型产出物在数据结构层面就无法承载"风险等级"
    clause_type: Optional[str] = None
    risk_type: str = ""
    evidence_text: str = ""
    confidence: Optional[float] = None
    suggestion_text: str = ""
```

```python
class RiskItem(BaseModel):
    judge_source: str

    @field_validator("judge_source")
    @classmethod
    def force_rule_engine(cls, v: str) -> str:
        if v != "rule_engine":
            raise ValueError("风险判定来源只能是规则引擎")
        return v
```

```python
risk_level: Optional[str] = Field(
    default=None,
    description="【架构红线】该字段只能由规则引擎判定后写入，"
                "大模型与向量召回的结果不得影响本字段",
)
```

### 派生属性

`is_parse_ok` / `is_parse_blocked` / `is_route_fast` / `is_route_deep` /
`needs_llm_review` / `needs_vector_review` / `needs_write_back` /
`total_risk_count` / `official_risk_count`

> **注意**：`needs_vector_review` / `needs_llm_review` 均以 `is_parse_ok` 为**前置条件**。
> 这是刻意的——真实流程中向量审查与大模型审查只可能在解析成功后到达。

### 关键方法

- `append_audit()` —— **纯函数**，只构造条目并返回，**不修改 self**（由节点写回增量字典）
- `model_dump_summary()` —— 剔除大文本字段，用于日志与前端摘要

---

## 三、节点执行外壳（node_utils.py）

### 四类执行路径

| 路径 | 触发条件 | 结果 |
|---|---|---|
| 正常执行 | handler 正常返回 | 写入 updates，追加 enter/exit 两条审计 |
| 不可重试阻断 | 抛 `NonRetryableError` | 立即置 blocked，**不重试**，追加 redline_deny 审计 |
| 重试耗尽 | 可重试异常达上限 | 置 blocked(`RETRY_EXHAUSTED`)，记录 exhausted_nodes |
| 主动跳过 | 前置条件不满足 | 记录 skipped_nodes，追加 node_skip 审计 |

### 指数退避

```python
base_delay = settings.TASK_RETRY_BASE_DELAY      # 默认 1.0 秒
max_delay  = settings.TASK_RETRY_MAX_DELAY       # 默认 8.0 秒
delay = min(base_delay * (2 ** (retry_count - 1)), max_delay)
```

退避序列：`1s → 2s → 4s`（封顶 8s），重试上限取 `state.max_retry`（默认 3）。
重试期间每次append一条 `node_retry` 审计。

### 审计序号唯一性

```python
def build_audit_entry(state, node, action, message="", detail=None,
                      elapsed_ms=None, offset: int = 0) -> AuditLogEntry:
    return AuditLogEntry(
        sequence=state.audit_sequence + offset + 1,
        ...
    )
```

> 同一节点内需要写多条日志时，第 2 条起必须依次传 `offset=1, 2, ...`，
> 否则序号重复，破坏"全局单调递增"的审计可追溯性。

---

## 四、三条差异化路由（routes.py）

### 路由类型判定

```python
def decide_route_type(state) -> str:
    # 优先级：deep > fast > standard
    # 大额模板合同优先走 deep（金额风险高于效率诉求）
```

| 类型 | 触发条件 | 链路 |
|---|---|---|
| `fast` | `template_flag=True` 且非大额 | 规则审查 → **直接 risk_merge**（跳过 vector_review 与 llm_review） |
| `standard` | 默认 | 规则 → 向量 → 大模型 → 融合 |
| `deep` | 金额 ≥ 500万 或 高风险类型（涉外合作/股权转让/担保/借款） | 同 standard，追加人工复核网关 + 强化提示词 |

### 五个条件路由函数

| 函数 | 判定 | 分支 |
|---|---|---|
| `route_after_pull` | 拉取任务后 | blocked → `audit`；否则 → `parse_document` |
| `route_after_parse` | **分支一** | blocked / failed / 未完成 / 无条款 → `audit`；否则 → `rule_review` |
| `route_after_rule` | **分支二/三** | 未判定 → `audit`；fast → `risk_merge`；deep/standard → `vector_review` |
| `route_after_risk_merge` | 融合后 | → `generate_report` |
| `route_after_write_back` | 回写后 | → `audit` |

> **安全默认**：规则引擎未完成判定时一律走 `audit`，**不允许"未判定放行"**。

---

## 五、十个节点（nodes.py）

| 节点 | 进度 | 核心职责 |
|---|---|---|
| `pull_approval_task` | 5% | 拉取审批任务，组装合同元数据，判定路由类型 |
| `parse_document` | 25% | 下载原文 → 三类法定阻断判定 → 文本块 / 段落树 / 条款切分 |
| `rule_review` | 55% | **唯一判定方**：加载规则模板 → 五模式匹配 → 规则引擎定级 |
| `vector_review` | 65% | BGE-M3 向量化 + Milvus 检索标准条款库，产出候选（**无等级**） |
| `llm_review` | 75% | 本地 LLM 辅助识别疑似风险，产出候选（**无等级**） |
| `risk_merge` | 82% | 多路融合，冲突裁决（规则优先 → 同级取最高风险） |
| `generate_report` | 88% | 渲染 Markdown 报告并上传 MinIO |
| `generate_comment` | 92% | 生成审批评论区文本（**仅建议性内容**） |
| `write_comment` | 96% | 白名单回写审批系统评论区 |
| `audit` | 100% | 流程收敛，确定最终任务状态 |

### 三类法定阻断（`parse_document`）

| 原因码 | 判定依据 |
|---|---|
| `DOC_ENCRYPTED` | 文件头含 `/Encrypt` |
| `SCAN_TOO_BLURRY` | OCR 平均置信度 < 0.6（配置 `OCR_MIN_CONFIDENCE_THRESHOLD`） |
| `EMPTY_CONTENT` | 抽取正文 < 50 字符（配置 `MIN_CONTENT_LENGTH`） |

### LLM 越权字段拦截

```python
forbidden_keys = {"risk_level", "risk_score", "severity", "decision",
                  "is_blocked", "blocked", "conclusion", "final_risk_level",
                  "is_violated", "violation", "pass", "approve"}
# 收集到 dropped_fields 并输出 WARNING，构造 LLMCandidate 时完全忽略
```

### 回写请求体剔除动作字段

```python
forbidden_body_keys = {"action", "approve", "reject", "transfer", "withdraw",
                       "reassign", "sign", "complete", "terminate",
                       "result", "decision"}
request_body = {
    "contract_review_comment": ...,
    "task_no": ...,
    "final_risk_level": ...,
    "risk_count": ...,
}
```

---

## 六、图组装与检查点（builder.py）

### 图结构

```
节点 10 个，边 15 条，条件分支 5 个

START
  ↓
pull_approval_task ──(blocked)──────────────────────────────┐
  ↓                                                          │
parse_document ──(blocked/failed/无条款)────────────────────┤
  ↓                                                          │
rule_review ──(未判定)─────────────────────────────────────┤
  ├─(fast)→ risk_merge                                       │
  └─(deep/standard)→ vector_review → llm_review → risk_merge │
                                                    ↓        │
                                            generate_report  │
                                                    ↓        │
                                            generate_comment │
                                                    ↓        │
                                             write_comment ──┤
                                                             ↓
                                                           audit
                                                             ↓
                                                            END
```

### 检查点

```python
def _build_checkpointer():
    # 生产替换点：MongoDBSaver / SqliteSaver
    return MemorySaver()      # 进程内存储，重启即丢

def build_thread_config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}, "recursion_limit": 50}
```

### 执行与续跑

```python
# 首次执行
final = await run_review_flow(initial_state, thread_id)

# 断点续跑（含解除阻塞）
final = await resume_review_flow(thread_id, updates)
# 内部：updates 会 setdefault is_blocked=False / blocked_info=None
#                / retry_count=0 / status="running"
#       然后 aupdate_state(config, updates) + ainvoke(None, config)
```

### 其他入口

`get_review_graph()` / `get_review_graph_async()`（双检锁）/ `reset_review_graph()` /
`get_flow_snapshot()` / `get_flow_history()` / `describe_graph()`

---

## 七、测试

`backend/tests/test_graph_smoke.py` —— **90 项断言，全部通过**，不依赖外部服务。

| 测试组 | 覆盖内容 |
|---|---|
| 测试一 | 图结构：节点数 10、节点名精确匹配、边数 15、条件分支 5 |
| 测试二 | 路由类型判定：standard / fast / deep / 大额模板优先级 |
| 测试三 | 分支一（解析失败）：blocked / failed / 未开始 / 无条款 → audit |
| 测试四 | 分支二/三：fast 跳过向量与大模型；deep 触发深度复核 |
| 测试五 | 节点异常：正常 / 不可重试阻断 / 重试耗尽 / 主动跳过 / 已阻塞拒执行 |
| 测试六 | 审计累加与检查点：序号递增、归约语义、会话隔离、空 thread_id 抛错 |
| 测试七 | 状态契约与红线：字段覆盖、文本块索引、LLM/向量候选无等级字段、非法值拒绝 |
| 测试八 | **归约器增量语义与审计序号唯一性**（缺陷防回归） |

### 运行方式

```bash
cd backend
"C:/Users/Lenovo/.workbuddy/binaries/python/envs/default/Scripts/python.exe" tests/test_graph_smoke.py
```

> 系统 python 3.13.12 未安装 langgraph，必须使用上述隔离环境。

---

## 八、端到端实测结果

### 路径一：高风险合同（规则 MAJOR）

```
规则引擎判定 risk_level = high
→ 命中阻塞等级配置 ['high', 'critical']
→ 任务正确阻断，进度停在 55%
→ 审计日志 13 条，序号 1-13 严格递增
```

### 路径二：低风险合同（规则 MINOR）

```
进入标准通道 → vector_review
→ 因真实 Milvus / BGE-M3 未就绪，重试 3 次耗尽
→ 置 blocked(RETRY_EXHAUSTED)
→ 审计日志 14 条，序号 1-14 严格递增
```

> 这是**预期行为**：外部服务未就绪时显式失败，
> 而非静默返回"解析成功但内容为空"的假成功状态。

---

## 九、依赖版本（requirements.txt 已同步）

| 包 | 声明版本 | 说明 |
|---|---|---|
| langgraph | 1.2.11 | 原声明 0.2.60，实测需 1.2.x |
| langgraph-checkpoint | 4.2.0 | 原声明 2.0.9，需与 1.2.x 配套 |
| langchain-core | 1.6.3 | 原声明 0.3.28 |
| pydantic | 2.13.5 | 原声明 2.10.4；2.11 起废弃实例访问 model_fields |
| pydantic-settings | 2.15.0 | 原声明 2.7.0 |

---

## 十、待接入依赖（后续模块交付）

| 依赖 | 位置 | 当前行为 |
|---|---|---|
| `app.services.parse_service` | `nodes._load_contract_text` | 未就绪时抛 `IOError`，触发重试 |
| `app.repositories.rule_template_repo` | `nodes._load_active_rule_templates` | 未就绪时抛 `IOError`，触发重试 |
| 真实 Milvus / BGE-M3 | `nodes._search_standard_clauses` | 未就绪时抛 `EmbeddingException`，重试耗尽置 blocked |
| 真实 MinIO | `nodes.generate_report` | 未就绪时抛 `IOError` |
| 本地 LLM 服务 | `nodes.llm_review` | 未就绪时抛异常 |
