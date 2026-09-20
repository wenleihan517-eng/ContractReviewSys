# D:/ContractReviewSys-合同审批智能审查项目/docs/SPEC.md
# 合同审批智能审查系统 — 技术规格说明书（SPEC）

> **文档版本**：v1.0
> **文档性质**：技术实现规格与接口契约。本文件描述"系统是怎么实现的、接口长什么样"。
> **配套文档**：[PRD.md](PRD.md) 产品需求｜[UX_UI.md](UX_UI.md) 交互与视觉｜[RUNBOOK.md](RUNBOOK.md) 运行说明
>
> **权威源声明**：本文件中的枚举、接口、错误码均从代码库实测提取。
> 若本文件与代码不一致，**以代码为准**，并请修正本文件。

---

## 一、技术栈

### 1.1 后端

| 组件 | 选型 | 说明 |
|---|---|---|
| 语言 | Python 3.10 | 3.11+ 依赖支持不完整，固定 3.10 |
| Web 框架 | FastAPI | 异步接口 |
| 数据校验 | Pydantic v2 | 模型校验与序列化 |
| 工作流编排 | LangGraph | 十节点有状态图 |
| 令牌 | PyJWT | 签发与校验 |
| 密码哈希 | bcrypt | **版本必须锁定 4.0.1**（原因见 §9.1） |

### 1.2 存储中间件

| 组件 | 用途 | 隔离方式 |
|---|---|---|
| MongoDB 7.0 | 业务主数据、审查记录、规则模板 | 独立库 `contract_review`，集合 `cr_` 前缀 |
| MinIO | 合同原文、OCR 图片、导出报告 | 独立桶 |
| Milvus 2.5 | 条款向量检索 | **独立 database**，严禁使用默认库 |

### 1.3 模型服务（全部本地部署）

| 服务 | 选型 | 协议 |
|---|---|---|
| 大模型 | 本地部署 | OpenAI 兼容协议，地址可配置 |
| 向量化 | BGE-M3 | 输出维度 **固定 1024**，强制校验 |

> **约束**：禁止调用任何公网模型 API。合同原文属敏感数据，不得出内网。

### 1.4 前端

| 组件 | 版本 | 用途 |
|---|---|---|
| React | 18.3.1 | 界面框架 |
| TypeScript | 5.7.2 | 严格模式全开 |
| Vite | 6.4.3 | 构建与开发服务器 |
| Ant Design | 5.29.3 | 组件库 |
| Zustand | 5 | 状态管理 |
| pdfjs-dist | 4.10.38 | PDF 在线预览 |
| axios | — | HTTP 客户端 |
| react-router-dom | 6 | 路由 |
| dayjs | — | 时间处理 |

---

## 二、系统架构

### 2.1 分层结构

```
backend/app/
├── main.py                 FastAPI 入口（lifespan / 中间件 / 路由挂载）
├── api/v1/                 接口层
│   ├── router.py           路由聚合器
│   ├── auth.py             认证接口
│   └── endpoints/          health / tasks / parse / review
│                           / report_comment / rules / audit
├── core/                   核心基础设施
│   ├── config.py           配置中心（全局唯一配置入口）
│   ├── logging_config.py   日志系统
│   ├── exceptions.py       全局异常与错误码
│   ├── response.py         统一响应封装
│   ├── middleware.py       请求链路追踪
│   ├── dependencies.py     依赖注入（含权限校验依赖族）
│   ├── security.py         密码哈希 + JWT 原语
│   └── auth_middleware.py  全局鉴权中间件
├── constants/              常量与枚举（唯一权威源）
├── storage/                存储客户端（单例）
│   ├── mongo_client.py
│   ├── minio_client.py
│   └── milvus_client.py
├── engines/                能力引擎（纯计算，不碰数据库）
│   ├── rule_engine.py      规则引擎（唯一判定方）
│   ├── vector_engine.py    向量检索引擎
│   ├── llm_engine.py       大模型辅助引擎
│   ├── clause_recommend_engine.py
│   ├── embedding_client.py
│   └── llm_client.py
├── graph/                  LangGraph 编排层
│   ├── state.py            状态契约定义
│   ├── nodes.py            十个节点函数
│   ├── routes.py           条件分支路由
│   ├── builder.py          图构建与检查点
│   └── node_utils.py       节点公共工具
├── models/documents/       数据存储模型（与集合一一对应）
├── schemas/                接口契约模型（面向前端）
├── services/               业务编排层
│   ├── parse_service.py    解析编排（含 OCR 降级链路）
│   ├── parsers/            PDF / DOCX / OCR 解析器
│   ├── review_service.py   审查编排
│   ├── report_service.py   报告生成
│   ├── comment_service.py  评论生成与回写
│   ├── task_service.py     任务生命周期
│   ├── auth_service.py     登录编排
│   └── metadata_extractor.py
├── repositories/           数据访问层
└── utils/                  工具层
    ├── approval_guard.py   审批白名单守卫
    ├── id_generator.py
    ├── retry.py
    ├── file_hash.py
    └── time_utils.py
```

### 2.2 分层调用规则（强制）

| 层级 | 禁止事项 |
|---|---|
| `api` | 禁止直接调用 `engines` 与 `storage`，必须经 `services` |
| `services` | 禁止操作 HTTP 请求/响应对象 |
| `engines` | 禁止访问数据库，只做纯能力计算 |
| `storage` | 禁止包含业务逻辑，只做数据存取 |
| `models` | 保持纯粹，不含业务方法 |
| `constants` | 枚举与集合名的唯一权威源，禁止散落硬编码 |

### 2.3 前端分层

```
frontend/src/
├── api/
│   ├── client.ts           axios 实例 + 拦截器 + 错误码表
│   └── endpoints.ts        接口统一收口（组件禁止裸调 axios）
├── components/             DocumentViewer / RiskCard
│                           ReviewFooterPanel / UploadContractModal
├── constants/index.ts      共享枚举唯一权威处
├── layouts/MainLayout.tsx  主布局（侧边导航 + 顶栏）
├── pages/                  Login / Dashboard / Workbench / Rules
├── router/
│   ├── index.tsx           路由表
│   └── guards.tsx          四类路由守卫
├── stores/                 authStore / taskStore
│                           workbenchStore / ruleStore
├── types/index.ts          与后端 schemas 一一对应
└── utils/
```

---

## 三、数据模型

### 3.1 MongoDB 集合（17 个）

| 集合名 | 用途 |
|---|---|
| `cr_contract` | 合同主档 |
| `cr_contract_clause` | 合同条款（结构化结果） |
| `cr_ocr_result` | OCR 识别结果 |
| `cr_review_task` | 审查任务 |
| `cr_review_risk_hit` | 风险命中记录（含辅助候选） |
| `cr_review_conclusion` | 审查结论（规则引擎产出） |
| `cr_rule_template` | 规则模板 |
| `cr_rule_hit_log` | 规则命中审计日志 |
| `cr_audit_log` | 任务全链路操作审计日志 |
| `cr_standard_clause` | 示范条款库 |
| `cr_suggestion` | 修改建议 |
| `cr_approval_record` | 审批记录 |
| `cr_write_back_log` | 回写日志 |
| `cr_report` | 报告记录 |
| `cr_file_object` | 文件对象元数据 |
| `cr_sys_config` | 系统配置（运行期可改项） |
| `cr_user` | 系统用户（账号主档） |

### 3.2 Milvus 集合（3 个）

| 集合名 | 用途 |
|---|---|
| `cr_clauses` | 条款向量 |
| `cr_standard_clause_lib` | 示范条款向量 |
| `cr_rule_templates` | 规则模板向量 |

**连接约束**：`192.168.10.130:7000`，`database = contract_review`，**严禁 default 库**。

### 3.3 MinIO 桶（5 个）

| 桶名 | 用途 |
|---|---|
| `contract-origin` | 合同原文 |
| `contract-ocr-image` | OCR 页面图片 |
| `contract-report` | 导出报告 |
| `contract-clause-lib` | 条款库文件 |
| `contract-tmp` | 临时文件 |

---

## 四、枚举与状态机

> 全部枚举定义于 `backend/app/constants/__init__.py`，是**唯一权威源**。

### 4.1 风险等级（4 级）

| 值 | 名称 | 排序权重 | 配色 | 语义 |
|---|---|---|---|---|
| `low` | 低风险 | 1 | 绿 `#52c41a` | 可提示，不影响推进 |
| `medium` | 中风险 | 2 | 橙 `#faad14` | 需人工确认 |
| `high` | 高风险 | 3 | 橙红 `#fa541c` | 需整改后推进 |
| `critical` | 严重风险 | 4 | 红 `#cf1322` | 直接阻塞审批流 |

> **配色依据**：遵循境内金融/风控场景"红高绿低"惯例。

### 4.2 规则严重度（4 级）

| 值 | 名称 |
|---|---|
| `info` | 提示级 |
| `minor` | 轻微 |
| `major` | 重大 |
| `fatal` | 致命 |

**映射关系**：规则严重度经**固定映射表**得到风险等级，过程完全确定，不引入模型判断。

### 4.3 审查任务状态（8 态）

| 值 | 名称 | 含义 |
|---|---|---|
| `pending` | 待执行 | 已受理等待执行 |
| `running` | 执行中 | 流程执行中 |
| `llm_done` | 大模型完成 | 辅助分析完成，尚未定级 |
| `rule_done` | 规则定级完成 | 规则引擎已定级 |
| `completed` | 审查完成 | 全流程结束 |
| `blocked` | 阻塞 | 被规则引擎判定阻塞 |
| `failed` | 失败 | 执行失败 |
| `cancelled` | 已取消 | 人工取消 |

**约束**：`blocked` 只能由规则引擎依据风险等级判定后写入。

### 4.4 阻塞原因码（6 类）

| 值 | 含义 | 判定方 |
|---|---|---|
| `DOC_ENCRYPTED` | 文档加密，无法解析 | 解析阶段 |
| `SCAN_TOO_BLURRY` | 扫描件模糊，OCR 置信度低于 0.6 | 解析阶段 |
| `EMPTY_CONTENT` | 正文为空，文本长度少于 50 字符 | 解析阶段 |
| `PARSE_FAILED` | 解析失败 | 异常重试机制 |
| `RETRY_EXHAUSTED` | 重试次数耗尽 | 异常重试机制 |
| `MANUAL` | 管理员人工置阻塞 | 人工 |

**约束**：禁止出现"未知原因阻塞"。

### 4.5 差异化路由（3 条）

| 值 | 名称 | 链路 |
|---|---|---|
| `fast` | 快速通道 | 模板合同，仅跑结构化规则 |
| `standard` | 标准审查 | 完整多路召回 + 大模型辅助 |
| `deep` | 深度复核 | 大额高风险，含人工复核网关 |

**优先级**：`deep` > `fast` > `standard`

**深度复核触发**：金额达到阈值（默认 500 万）**或**属于高风险合同类型
（涉外合作 / 股权转让 / 担保 / 借款）。

### 4.6 条款类型（14 类）

| 值 | 名称 |
|---|---|
| `party_info` | 当事人信息 |
| `subject_matter` | 标的与金额 |
| `payment` | 付款条款 |
| `delivery` | 交付与验收 |
| `warranty` | 质量保证 |
| `liability` | 违约责任 |
| `confidentiality` | 保密条款 |
| `ip` | 知识产权 |
| `termination` | 合同解除与终止 |
| `force_majeure` | 不可抗力 |
| `dispute` | 争议解决 |
| `data_protection` | 数据与个人信息保护 |
| `compliance` | 合规与反腐败 |
| `other` | 其它/未分类 |

**分类规则**：标题优先。标题含"违约责任"的条款必须归为 `liability`，
不因正文含"赔偿"而错分。

### 4.7 规则匹配方式（7 种）

| 值 | 名称 |
|---|---|
| `keyword` | 关键词命中 |
| `regex` | 正则命中 |
| `clause_type` | 条款类型命中 |
| `semantic` | 语义相似度命中 |
| `field_compare` | 字段数值比较 |
| `missing_clause` | 必备条款缺失检查 |
| `numeric_threshold` | 数值阈值校验 |

### 4.8 风险命中决策（4 类）

| 值 | 含义 |
|---|---|
| `pass` | 放行（仅提示） |
| `warn` | 告警，记录但不阻断 |
| `need_revision` | 需修订后重新提交 |
| `block` | 阻塞，禁止推进审批 |

### 4.9 回写状态（4 态）

| 值 | 名称 |
|---|---|
| `pending` | 待回写 |
| `success` | 回写成功 |
| `failed` | 回写失败 |
| `retrying` | 重试中 |

### 4.10 用户角色（3 种）

| 值 | 名称 | 权限数 |
|---|---|---|
| `business_user` | 业务经办人 | 3 |
| `law_user` | 法务审查人 | 6 |
| `admin` | 系统管理员 | 11 |

**账号状态**：`active` 启用 / `disabled` 停用 / `locked` 锁定

**令牌类型**：`access` 访问令牌 / `refresh` 刷新令牌

### 4.11 工作流节点（10 个）

| 顺序 | 节点名 | 职责 |
|---|---|---|
| 1 | `pull_approval_task` | 拉取审批任务，注入合同基础信息 |
| 2 | `parse_document` | 文档解析 + 条款结构化 |
| 3 | `rule_review` | **规则引擎判定（定级 + 阻塞，唯一判定方）** |
| 4 | `vector_review` | 向量语义召回（无定级权） |
| 5 | `llm_review` | 大模型辅助审查（无定级权） |
| 6 | `risk_merge` | 多路召回融合与冲突裁决 |
| 7 | `generate_report` | 报告与修改建议生成 |
| 8 | `generate_comment` | 审批评论区内容构造 |
| 9 | `write_comment` | 审批评论区回写 |
| 10 | `audit` | 流程审计与收敛（终点节点） |

**分支说明**：
- `rule_review` 之后按路由类型决定是否走 `vector_review` + `llm_review`
- 快速通道跳过向量与大模型
- 图中共 5 处条件分支

---

## 五、接口契约

> **基础前缀**：`/api/v1`
> **统一响应结构**：
> ```json
> {
>   "code": 0,
>   "message": "操作成功",
>   "data": {},
>   "request_id": "56416943-6aa8-4785-a5cf-0c06a7c9d2a8"
> }
> ```
> `code = 0` 表示成功，非 0 为业务错误码。

### 5.1 认证接口（8 个）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/auth/login` | 账号密码登录，返回双令牌与用户信息 |
| POST | `/auth/refresh` | 刷新访问令牌（双令牌轮换） |
| POST | `/auth/logout` | 退出登录（令牌立即失效） |
| GET | `/auth/me` | 获取当前登录用户信息 |
| POST | `/auth/check-permission` | 权限校验（前端路由鉴权用） |
| GET | `/auth/check-permission` | 权限校验（查询形式） |
| GET | `/auth/permission-matrix` | 获取角色权限矩阵 |
| POST | `/auth/change-password` | 修改密码 |

**登录请求**：
```json
{ "username": "user_law", "password": "123456" }
```
- `username`：3–50 字符，字母开头
- `password`：1–64 字符

**登录响应**：
```json
{
  "code": 0,
  "data": {
    "access_token": "...",
    "refresh_token": "...",
    "token_type": "Bearer",
    "expires_in": 7200,
    "refresh_expires_in": 604800,
    "user": {
      "user_id": "...",
      "username": "user_law",
      "display_name": "法务审查人",
      "role": "law_user",
      "role_label": "法务审查人",
      "status": "active",
      "department": null,
      "email": null,
      "permissions": ["task:view_own", "task:view_all", "..."],
      "is_admin": false,
      "can_view_all_tasks": true
    }
  }
}
```

**令牌有效期**：访问令牌 120 分钟 / 刷新令牌 7 天。刷新即轮换，旧刷新令牌立即失效。

**权限校验请求**：
```json
{ "permission": "rule_template:manage" }
```
或
```json
{ "role": "admin" }
```

**权限校验响应**：
```json
{
  "passed": true,
  "checked_type": "permission",
  "checked_value": "rule_template:manage",
  "permissions": ["..."]
}
```

### 5.2 健康检查（3 个）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 存活探针，**不触碰外部依赖** |
| GET | `/ready` | 就绪探针，并发检查全部依赖连通性 |
| GET | `/config-check` | 配置自检，敏感字段已脱敏 |

### 5.3 审查任务（6 个）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/tasks` | 创建审查任务，自动执行路由决策 |
| GET | `/tasks` | 分页查询任务列表（状态过滤 + 关键字搜索） |
| GET | `/tasks/approval-pending` | 拉取审批待办 |
| GET | `/tasks/{task_no}` | 任务详情 |
| POST | `/tasks/{task_no}/retry` | 任务重试（回到待执行，清阻塞标记） |
| POST | `/tasks/{task_no}/resolve` | 处理阻塞任务 |

**阻塞处理请求**：
```json
{ "action": "retry", "reason": "已重新提供清晰扫描件" }
```
- `action`：`retry` 重跑 / `dismiss` 驳回
- `dismiss` 后任务置为 `failed`

### 5.4 文档解析（3 个）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/parse/upload` | 上传合同文件（PDF / DOCX / 图片） |
| POST | `/parse/{task_id}/trigger` | 触发解析（异步受理） |
| GET | `/parse/{task_id}/result` | 查询解析结果（全文 / 文本块 / 条款 / 元数据） |

### 5.5 风险审查（4 个）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/review/{task_id}/trigger` | 触发审查（规则 + 向量 + 大模型多路召回） |
| GET | `/review/{task_id}/result` | 审查结果（风险项 / 结论 / 等级分布） |
| PUT | `/review/risk-items/{risk_id}` | 人工编辑风险项 |
| PUT | `/review/risk-items/{risk_id}/recommend` | 人工修改推荐条款 |

### 5.6 报告与评论（4 个）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/report/{task_id}/generate` | 生成审查报告（`format=md` 或 `pdf`） |
| POST | `/report/{task_id}/comment/preview` | 预览审批评论 |
| POST | `/report/{task_id}/comment/write-back` | **正式回写审批评论（白名单接口）** |
| GET | `/report/{task_id}/download` | 下载报告（`format=md\|pdf`，默认 md；`mode=json\|stream`） |

> **报告格式说明**：Markdown 为纯文本报告，PDF 为正式版式报告（PyMuPDF 原生绘制，A4 / 页眉页脚 / 风险等级配色）。
> 两者内容由同一份 Markdown 源文本驱动，保证结论一致。`format=pdf` 且 `mode=stream` 时响应 `Content-Type: application/pdf`
> 且 `Content-Disposition: inline`，支持浏览器内直接预览。

### 5.7 规则管理（7 个）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/rules` | 规则列表（可含停用规则） |
| POST | `/rules` | 创建规则（按编码幂等） |
| PUT | `/rules/{rule_code}` | 更新规则 |
| PUT | `/rules/{rule_code}/enabled` | 启用/禁用 |
| DELETE | `/rules/{rule_code}` | 删除规则 |
| GET | `/rules/standard-clauses` | 示范条款库列表 |
| POST | `/rules/standard-clauses` | 创建示范条款 |

### 5.8 审计（2 个）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/audit/tasks/{task_id}/logs` | 任务操作记录查询 |
| GET | `/audit/tasks/{task_id}/logs/all` | 任务全量操作记录 |

**接口总数**：8 + 3 + 6 + 3 + 4 + 4 + 7 + 2 = **37 个端点**，分 8 组。

---

## 六、错误码表

> 定义于 `backend/app/core/exceptions.py`。

### 6.1 通用（1xxxx）

| 码 | 含义 |
|---|---|
| 0 | 成功 |
| 10000 | 未知错误 |
| 10001 | 服务器内部错误 |
| 10002 | 服务不可用 |
| 10003 | 资源不存在 |
| 10004 | 请求方法不允许 |

### 6.2 参数校验（2xxxx）

| 码 | 含义 |
|---|---|
| 20000 | 请求参数校验失败 |
| 20001 | 缺少必填参数 |

### 6.3 存储中间件（3xxxx）

| 码 | 含义 |
|---|---|
| 30000 | MongoDB 操作异常 |
| 30001 | MinIO 操作异常 |
| 30002 | Milvus 操作异常 |

### 6.4 模型服务（4xxxx）

| 码 | 含义 |
|---|---|
| 40000 | 大模型调用异常 |
| 40001 | 大模型调用超时 |
| 40002 | 向量化服务异常 |

### 6.5 业务处理（5xxxx）

| 码 | 含义 |
|---|---|
| 50000 | 文件上传失败 |
| 50001 | 文件类型不允许 |
| 50002 | 文件大小超限 |
| 50003 | 合同解析失败 |
| 50004 | OCR 识别失败 |
| 50005 | 规则引擎执行异常 |
| 50006 | 审查任务异常 |
| 50007 | 任务处于阻塞状态，需人工处理 |
| 50008 | **审批接口调用越权（严禁改变审批结论）** |
| 50009 | 审批回写失败 |

### 6.6 鉴权（6xxxx）

| 码 | 含义 | HTTP |
|---|---|---|
| 60000 | 鉴权通用失败（兜底） | 401 |
| 60001 | 账号或密码错误 | 401 |
| 60002 | 未携带凭证 | 401 |
| 60003 | 凭证非法/被篡改 | 401 |
| 60004 | 凭证已过期 | 401 |
| 60005 | 令牌类型不匹配 | 401 |
| 60006 | 刷新令牌失效 | 401 |
| 60007 | 账号已被停用 | 403 |
| 60008 | 账号已被锁定 | 403 |
| 60009 | 已登录但无权限访问该资源 | 403 |

> **重要区分**：`60004` 过期与 `60003` 非法必须分开返回。
> 实现上需在解码时先关闭过期校验，再手工比对时间，否则过期会被误判为非法。

### 6.7 配置（9xxxx）

| 码 | 含义 |
|---|---|
| 90000 | 配置错误 |

---

## 七、权责红线的代码级保障

> 本章是 SPEC 中**最关键的章节**。红线的落地方式必须是**结构性**的，
> 而不是靠"开发者记得遵守"。

### 7.1 保障手段总表

| 红线 | 强制手段 | 实现位置 |
|---|---|---|
| 大模型不得定级 | `RiskHit` 的 `model_validator` 拦截 `llm_candidate` 携带 `risk_level` / `decision` / `severity` | `models/documents/risk_hit.py` |
| 候选对象结构上无定级字段 | `LLMCandidate` / `VectorCandidate` **不含**定级相关字段 | `graph/state.py` |
| 节点解析模型返回时丢弃越权字段 | 黑名单过滤 + 告警日志 | `graph/nodes.py` |
| 结论来源唯一 | `ReviewConclusion.decision_source` 只接受 `rule_engine` | `models/documents/review_conclusion.py` |
| 风险项判定来源唯一 | `RiskItem.judge_source` 的 `field_validator` 强制为 `rule_engine` | 同上 |
| 规则命中可追溯 | 规则引擎来源必须提供 `rule_code` | `engines/rule_engine.py` |
| 法务意见不改判定 | `set_legal_opinion()` 只写 B 类字段，A 类判定字段永不触碰 | `services/review_service.py` |
| 结论防篡改 | `verify_integrity()` 用 `task_id` 加盐 SHA-256 | `models/documents/review_conclusion.py` |
| 审批接口白名单 | `approval_guard.ensure_comment_only()` 三重校验 | `utils/approval_guard.py` |
| 明文密码不得入库 | `User.validate_password_hash` 正则拒绝非 bcrypt 串 | `models/user.py` |
| 权限矩阵唯一源 | `constants.ROLE_PERMISSIONS` | `constants/__init__.py` |
| 规则引擎关闭拒绝放行 | `RULE_ENGINE_ENABLED` 关闭时直接抛异常 | `engines/rule_engine.py` |

### 7.2 审批接口白名单（红线强制）

**白名单**（仅这两个）：
- `/comments`
- `/comments/batch`

**黑名单**（明确拒绝）：
`/approve`、`/reject`、`/transfer`、`/withdraw`、`/reassign`、`/sign`、
`/complete`、`/terminate`

**兜底策略**：**默认拒绝**。白名单外的一切路径均被拦截，
包括系统未预料到的新路径。

**回写请求体约束**：必须剔除动作类字段（`action` / `approve` / `reject` /
`transfer` / `sign`）。

---

## 八、LangGraph 编排规格

### 8.1 状态契约铁律

| # | 规则 | 原因 |
|---|---|---|
| 1 | 节点**返回增量字典，不修改 state** | 直接改会被框架忽略，导致日志丢失 + 状态静默还原 |
| 2 | 累加型字段**只返回增量** | `audit_logs` / `node_history` 使用 `operator.add` 归约器，写全量会导致重复累加 |
| 3 | 非累加型字段**返回全量** | `skipped_nodes` / `exhausted_nodes` 是普通列表（替换语义），必须拼历史 |
| 4 | 同节点多条日志**必须传偏移量** | 第 2 条起传 1、2…，避免时间戳相同导致排序错乱 |

**新增字段时的检查动作**：先确认该字段的归约器类型，再决定返回增量还是全量。

### 8.2 检查点

- 当前实现：`MemorySaver`（进程内）
- 生产替换点：`builder._build_checkpointer()`
- 续跑接口：`resume_review_flow(thread_id, updates)`
  等价于 `aupdate_state` 修正阻塞标记 + `ainvoke(None, config)`

### 8.3 解析服务契约点

| 接口 | 签名 | 说明 |
|---|---|---|
| 加载合同文本 | `_load_contract_text(...) -> (full_text, page_texts, doc_meta)` | `page_texts` 索引 0 = 第 1 页 |
| 加载规则模板 | `_load_active_rule_templates() -> List[RuleTemplate]` | **必须返回模型实例**，不可返回字典 |

**容错要求**：未就绪时**显式抛出异常并阻断**，绝不静默返回假成功。

---

## 九、已知陷阱与规避（实现层必读）

### 9.1 依赖版本

| 陷阱 | 现象 | 规避 |
|---|---|---|
| bcrypt 版本 | passlib 1.7.4 读 `bcrypt.__about__`，bcrypt ≥ 4.1 已移除该属性 → `AttributeError`，退化成密码长度报错 | **锁定 `bcrypt==4.0.1`** |
| Python 版本 | 3.11+ 部分依赖不完整 | 固定 3.10 |

### 9.2 框架行为

| 陷阱 | 现象 | 规避 |
|---|---|---|
| FastAPI 路由摘要尾逗号 | `description=(...)` 末尾多一个逗号变成 tuple → `'tuple' object has no attribute 'split'` | `api/v1/` 下所有 `description=` 不得以逗号结尾 |
| `model_dump()` 丢弃 None 字段 | 依赖"字段是否为 None"做分支的逻辑，经一次序列化往返后分支静默退化 | 显式保留键，或改从语义字段自行推导 |
| 令牌撤销失效 | `token_version` 未写入初始插入语句 → 自增从 0 起算 → 与默认值相同 → 登出/改密后旧令牌仍可用 | `token_version` 必须进初始插入；并在撤销前主动补齐历史数据 |
| 路径穿越 | 仅用 `startswith` 做白名单匹配，会被 `/auth/login/../admin/users` 绕过 | 归一化路径（解码 → 折叠斜杠 → 消除 `..`）后再匹配，且用 `item + "/"` 边界匹配 |
| 全局诊断被库日志淹没 | 底层 SDK 直接写标准错误，try/except 拦不住 | 需劫持文件描述符按正则过滤 |

### 9.3 前端

| 陷阱 | 现象 | 规避 |
|---|---|---|
| antd 深层类型 | `moduleResolution: bundler` 解析不到深层 `.d.ts` | 在 `tsconfig.app.json` 的 `paths` 指向真实 `.d.ts`；**严禁**在全局声明文件里写 `declare module 'antd/es/*'`（会用 any 覆盖真实类型） |
| PDF Worker | 预览报 worker 未配置 | 用 `?url` 导入并赋给 `GlobalWorkerOptions.workerSrc` |
| Store 与请求客户端循环依赖 | 拦截器需要令牌、令牌在 store、store 又要调接口 | 客户端只暴露钩子注册函数，由 store 在模块末尾注册实现，依赖保持单向 |
| Zustand v5 与只读渲染 | 渲染时读不到 `setState` 写入的值 | 该版本只读渲染使用创建时快照；测试须走客户端渲染路径 |
| 开发服务器暴露 | 敏感数据外泄风险 | **只能绑 `127.0.0.1`**，禁 `0.0.0.0` |

---

## 十、配置项

配置文件：`backend/.env`（模板 `.env.example`）。
**唯一读取入口**：`backend/app/core/config.py`。**禁止硬编码**。

### 10.1 主要配置组

| 组 | 示例项 |
|---|---|
| 应用 | 应用名、环境、调试开关、API 前缀 |
| 服务 | 监听地址、端口 |
| MongoDB | 连接串、库名、连接池 |
| MinIO | 端点、密钥、桶名 |
| Milvus | 主机、端口、**database（必须为 `contract_review`）** |
| 向量化 | 服务地址、模型名、**维度（固定 1024）** |
| 大模型 | 服务地址、模型名、超时、开关 |
| 规则引擎 | 启用开关、规则文件路径、语义召回阈值 |
| 检索 | 召回条数上限 |
| 分块 | 分块大小、重叠长度 |
| 鉴权 | 密钥、算法、访问令牌时长、刷新令牌时长 |
| 账号安全 | 最大失败次数、锁定时长、密码长度范围 |
| 审批回写 | 审批系统地址、回写模式、超时 |
| OCR | 清晰度阈值、置信度阈值 |
| 任务 | 重试次数、退避基数、并发数 |
| 文件 | 允许类型、大小上限 |
| 存储 | 临时目录、日志目录 |
| 初始化 | 是否播种内置账号 |

### 10.2 关键默认值

| 项 | 默认值 | 说明 |
|---|---|---|
| 访问令牌时长 | 120 分钟 | — |
| 刷新令牌时长 | 7 天 | — |
| 最大登录失败次数 | 5 | 超过则锁定 |
| 锁定时长 | 30 分钟 | — |
| 密码长度 | 6–64 字符 | 含字母 + 数字，禁空格 |
| OCR 置信度阈值 | 0.6 | 低于则判定为模糊 |
| 最小正文长度 | 50 字符 | 低于则判定为空内容 |
| 深度复核金额阈值 | 500 万 | — |
| 向量维度 | 1024 | 强制校验 |
| 分页默认/上限 | 20 / 200 | — |

---

## 十一、测试与验证规格

### 11.1 后端测试套件（7 套）

| 套件 | 断言数 |
|---|---|
| 规则引擎验证 | 69 |
| 图流程冒烟 | 98 |
| 审查流程验证 | 51 |
| 报告与评论验证 | 45 |
| 接口验证 | 37 |
| 鉴权端到端 | 132 |
| 其他 | 其余 |
| **合计** | **402+** |

### 11.2 前端验证

| 项 | 方式 | 基线 |
|---|---|---|
| 类型检查 | TypeScript 严格模式编译 | 0 error |
| 生产构建 | Vite 构建 | 成功 |
| 冒烟测试 | Vite 模块加载 + JSDOM 真渲染 | **45/45** |

### 11.3 前端冒烟测试覆盖分组

| 分组 | 内容 |
|---|---|
| 展示层 | 文案、枚举、状态映射 |
| 权责红线 | 源码级核查：无审批动作调用、无定级字段写入 |
| 组件层 | 真实渲染：布局 / 规则页 / 工作台 / 风险卡 / 底栏 / 上传弹窗 |
| 状态层 | Store 逻辑：筛选重置、状态清理、分组排序 |
| 接口层 | 契约与边界：分组覆盖、前缀一致、错误码、响应解包 |
| 鉴权层 | 登录态判定、角色映射、越权拦截、常量对齐、拦截器钩子 |
