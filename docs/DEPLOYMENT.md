# D:/ContractReviewSys-合同审批智能审查项目/docs/DEPLOYMENT.md

# 合同审批智能审查系统 — 部署与启动指南

> 面向开发人员，兼顾新手。按本文顺序执行即可从零跑通系统。
> 全文中所有命令均已在 Windows 10/11 + Git Bash 环境实测。

---

## 目录

| 章节 | 内容 | 适用场景 |
| --- | --- | --- |
| [第一章](#第一章-环境准备) | 环境准备（Python / Node / MongoDB / MinIO / Milvus / 模型服务） | 首次部署 |
| [第二章](#第二章-数据库初始化) | MongoDB 库、集合、索引、角色、初始数据 | 首次部署 |
| [第三章](#第三章-向量库初始化) | 规则批量向量化写入 Milvus | 首次部署 |
| [第四章](#第四章-后端启动) | 后端依赖、配置、启动命令、端口 | 日常开发 |
| [第五章](#第五章-前端启动) | 前端依赖、启动命令、访问地址 | 日常开发 |
| [第六章](#第六章-审批回写模拟模块) | 模拟审批系统部署（含红线验证器） | 演示 / 联调 |
| [第七章](#第七章-系统自检) | 各组件连通性与可用性验证 | 部署后 / 排障 |
| [第八章](#第八章-常见问题排查) | 启动失败、连接超时、OCR 异常、回写报错、阻塞任务 | 排障 |

---

## 写在最前面：两条红线

在开始部署前，请先理解本系统的两条架构红线。它们不是"建议"，而是**代码级强制**的，
部署时如果配置错误，系统会直接拒绝启动或拒绝执行，而不是降级运行。

### 红线一：所有正式业务判定由规则引擎裁决，大模型无权参与

| 能力 | 承担方 |
| --- | --- |
| 条款语义解析 / 风险候选识别 / 建议初稿 / 合规解读 | 大模型（**仅辅助**） |
| **风险等级定级 / 违规判定 / 任务阻塞判定 / 最终结论** | **规则引擎（唯一裁决方）** |

代码保障：
- `RULE_ENGINE_ENABLED` 必须为 `True`。设为 `False` 时规则引擎直接抛异常，**系统拒绝执行审查流程**。
- 大模型产出物一律标记 `hit_source = llm_assistant`，且数据结构上就不含 `risk_level` 字段，无法越权定级。
- 结论记录 `decision_source = rule_engine` 与 `engine_version`，并生成 SHA-256 校验值防篡改。

### 红线二：审批系统只写评论区，绝不做审批动作

| 类别 | 接口 | 是否允许 |
| --- | --- | --- |
| 白名单 | `/comments`、`/comments/batch` | ✅ 允许 |
| 黑名单 | `/approve`、`/reject`、`/transfer`、`/withdraw`、`/reassign`、`/sign`、`/complete`、`/terminate` | ❌ 强制拒绝 |
| 兜底 | 不在白名单内的**任何**路径 | ❌ 默认拒绝 |

这一条在**三层**都有强制：
1. 后端 `approval_guard.ensure_comment_only()` —— 调用前拦截，越权直接抛异常
2. 请求体清洗 —— 剔除 `action` / `approve` / `reject` 等动作类字段
3. 模拟审批系统 —— 即使前两层被绕过，动作类接口也返回 `403`

---

# 第一章 环境准备

## 1.1 环境总览

系统运行依赖 5 个外部组件 + 2 个应用服务：

```
┌──────────────────────────────────────────────────────────────┐
│ 基础设施（必须外部提供）                                        │
├──────────────────────────────────────────────────────────────┤
│  MongoDB      127.0.0.1:27017    业务主数据存储                │
│  MinIO        127.0.0.1:9000     对象存储（9001 为控制台）      │
│  Milvus       192.168.10.130:7000 向量数据库                   │
│  Embedding    127.0.0.1:9997     BGE-M3 向量化服务             │
│  LLM          127.0.0.1:8001     大模型服务（辅助，非阻塞）      │
├──────────────────────────────────────────────────────────────┤
│ 应用服务（本项目）                                             │
├──────────────────────────────────────────────────────────────┤
│  backend      127.0.0.1:8000     FastAPI + LangGraph          │
│  frontend     127.0.0.1:5173     React + Vite                 │
│  mock-approval 127.0.0.1:9100    模拟审批系统（演示用）          │
└──────────────────────────────────────────────────────────────┘
```

> **端口冲突提醒**：实测发现 VMware 的 `vmnat.exe` 会占用 **8000** 端口。
> 如果后端起不来且提示端口被占，先确认是不是这个原因：
> ```bash
> netstat -ano | findstr ":8000"
> ```
> 若占用者不是 Python，需要在 VMware 虚拟网络编辑器中关闭 NAT 服务，
> 或修改 `backend/.env` 的 `SERVER_PORT` 换一个端口。

## 1.2 Python 3.10

**版本要求：Python 3.10.x（固定，不可变更）**

为什么必须 3.10：
- 依赖清单中 `paddlepaddle==2.6.2`、`langgraph==1.2.11` 等对 3.11+ 支持不完整
- 团队统一版本可避免"我这儿能跑"类问题

### 安装（Windows）

1. 从 https://www.python.org/downloads/release/python-31011/ 下载
   `Windows installer (64-bit)`
2. 安装时**务必勾选** `Add Python 3.10 to PATH`
3. 验证：

```bash
python --version
# 期望输出：Python 3.10.11
```

### 安装（Linux / macOS）

```bash
# Ubuntu / Debian
sudo apt update
sudo apt install -y python3.10 python3.10-venv python3.10-dev

# macOS（Homebrew）
brew install python@3.10
```

### 多版本共存时的处理

如果机器上同时装了多个 Python（比如 3.13），必须**显式指定 3.10** 创建虚拟环境：

```bash
# Windows：用 py 启动器指定版本
py -3.10 -m venv .venv

# Linux / macOS
python3.10 -m venv .venv
```

虚拟环境创建后，该环境内的 `python` 就固定是 3.10，后续命令无需再指定版本。

## 1.3 Node 18

**版本要求：Node.js 18.x LTS 或更高**

```bash
node --version    # 期望 v18.x 或更高
npm --version
```

### 安装

- **推荐**：从 https://nodejs.org/ 下载 LTS 版本安装包
- 或使用版本管理器（便于多版本切换）：

```bash
# nvm-windows: https://github.com/coreybutler/nvm-windows
nvm install 18
nvm use 18
```

> **Windows 安装注意**：npm 在 Windows 上是 `npm.cmd`。
> 本项目的 `devctl.py` 已处理该差异，但如果你自己写脚本调用 npm，
> 需要注意直接用 `subprocess` 调 `"npm"` 会报 `FileNotFoundError`。

## 1.4 MongoDB 7.0

**版本要求：MongoDB 7.0.x**

### 方式一：Docker（推荐，项目已提供编排文件）

```bash
cd deploy/docker
docker compose up -d mongodb
```

验证：

```bash
docker ps | grep cr-mongodb
# 期望看到 cr-mongodb 容器状态为 Up (healthy)
```

### 方式二：Windows 本机安装

1. 下载 MongoDB Community Server 7.0：
   https://www.mongodb.com/try/download/community
2. 安装时选择 `Complete`，并勾选 `Install MongoDB as a Service`
3. 启动服务：

```bash
net start MongoDB
```

4. 验证：

```bash
mongosh --eval "db.adminCommand('ping')"
# 期望输出：{ ok: 1 }
```

### 方式三：Linux

```bash
sudo systemctl start mongod
sudo systemctl enable mongod    # 开机自启
```

### 关键配置说明

- **默认不开鉴权**（开发环境）。生产环境必须开启，并同步修改 `MONGO_URI` 带上账号密码。
- 数据目录：Docker 方式映射到 `storage/mongo_data`，便于备份与清理。

## 1.5 MinIO

**版本要求：RELEASE.2024-10-13T13-34-11Z（项目已锁定该版本）**

```bash
cd deploy/docker
docker compose up -d minio
```

验证：

```bash
# 1. 端口探测
curl -I http://127.0.0.1:9000/minio/health/live
# 期望：HTTP/1.1 200 OK

# 2. 控制台登录
# 浏览器打开 http://127.0.0.1:9001
# 账号 minioadmin / 密码 minioadmin
```

### 存储桶

**无需手工创建**。后端启动时 `minio_client.connect()` 会自动创建全部 5 个桶：

| 存储桶 | 用途 | 是否必需 |
| --- | --- | --- |
| `contract-origin` | 合同原文 | 必需 |
| `contract-ocr-image` | OCR 页面图片 | 必需 |
| `contract-report` | 导出报告 | 必需 |
| `contract-clause-lib` | 标准条款库附件 | 可选 |
| `contract-tmp` | 任务临时文件 | 可选 |

## 1.6 Milvus 2.5.0

**版本要求：Milvus 2.5.0**

本项目**复用现有 Milvus 实例**，地址 `192.168.10.130:7000`，不需要本地部署。
但必须做**数据隔离**，见下方说明。

### 验证远程实例可达

```bash
# 1. 网络连通性
ping 192.168.10.130

# 2. 端口连通性
telnet 192.168.10.130 7000

# 3. 健康检查（Milvus 的 metrics 端口通常为 9091）
curl http://192.168.10.130:9091/healthz
# 期望输出：OK
```

如果通过虚拟机提供服务，需确认：
- 虚拟机已做端口映射（Milvus 默认 `19530` → 本项目使用 `7000`）
- 虚拟机防火墙已放行该端口
- 网络模式下虚拟机与宿主机可互访

### 数据隔离（强制）

系统使用**独立 database** 与其他项目物理隔离：

| 层级 | 取值 | 说明 |
| --- | --- | --- |
| database | `contract_review` | 独立库，**严禁使用 default 库** |
| collection | `cr_clauses` | 合同条款向量 |
| collection | `cr_standard_clause_lib` | 标准条款库（条款推荐数据源） |
| collection | `cr_rule_templates` | 规则模板向量（语义规则召回） |

隔离由 `MILVUS_DB_NAME=contract_review` 控制，代码中 `milvus_client.connect()`
会先建库再切换，全程不触碰 `default` 库。自检脚本会校验这一点。

### 如需本机部署 Milvus

编辑 `deploy/docker/docker-compose.yml`，解开文件末尾 `etcd` 与 `milvus`
的注释后启动：

```bash
cd deploy/docker
docker compose up -d etcd milvus
```

同时把 `backend/.env` 改为：

```ini
MILVUS_HOST=127.0.0.1
MILVUS_PORT=19530
```

## 1.7 模型服务（本地部署，禁止公网 API）

### Embedding：BGE-M3

**这是一项硬依赖**。系统所有向量检索与规则向量化都依赖它。

```ini
EMBEDDING_BASE_URL=http://127.0.0.1:9997/v1
EMBEDDING_MODEL_NAME=bge-m3
EMBEDDING_DIM=1024        # BGE-M3 固定 1024 维，禁止修改
```

服务需兼容 OpenAI `/v1/embeddings` 协议。验证：

```bash
curl http://127.0.0.1:9997/v1/models
```

> **维度警告**：`EMBEDDING_DIM` 已在配置层做校验器强制为 1024。
> 若更换向量模型，必须同步修改该值**并重建全部 Milvus 集合**，
> 否则写入会因维度不匹配而失败。

### LLM：本地大模型

**这不是硬依赖**。不可用时系统降级运行，核心审查功能不受影响。

```ini
LLM_BASE_URL=http://127.0.0.1:8001/v1
LLM_MODEL_NAME=qwen2.5-14b-instruct
LLM_TEMPERATURE=0.1        # 合同场景要求输出稳定，禁止调高
```

服务需兼容 OpenAI `/v1/chat/completions` 协议。验证：

```bash
curl http://127.0.0.1:8001/v1/models
```

**LLM 不可用时的降级行为**：

| 能力 | 降级后表现 |
| --- | --- |
| 条款语义解析 | 退化为规则匹配 |
| 风险候选识别 | 无 AI 候选标记 |
| 修改建议初稿 | 仅输出规则模板建议 |
| 合规条文解读 | 无 |

> 风险定级、违规判定、阻塞判定、最终结论**全部由规则引擎产出**，
> 不受 LLM 可用性影响。这是红线一的具体体现。

---

# 第二章 数据库初始化

## 2.1 一键初始化（推荐）

本项目已提供编排脚本，按正确顺序完成全部初始化：

```bash
cd deploy/scripts

# 预览将执行的步骤（不真正执行）
python init_all.py --dry-run

# 完整初始化：MongoDB → Milvus → 规则向量化 → 全链路自检
python init_all.py
```

Windows 用户也可直接双击 `deploy/scripts/init_all.bat`。

脚本会依次执行 4 步，任一步失败即停止并给出修复建议：

| 步骤 | 脚本 | 做什么 |
| --- | --- | --- |
| 1 | `backend/scripts/init_mongodb.py` | 建库、集合、索引、角色、系统配置、规则模板 |
| 2 | `backend/scripts/init_milvus_vectors.py --skip-rules` | 建独立 database 与 3 个集合 |
| 3 | `backend/scripts/init_milvus_vectors.py` | 34 条规则批量向量化写入 |
| 4 | `backend/scripts/system_check.py` | 全链路自检 |

常用参数：

```bash
python init_all.py --only mongodb       # 只做 MongoDB
python init_all.py --skip vectors       # 跳过向量化
python init_all.py --recreate           # 删除重建（会丢数据，需输入 yes 确认）
```

## 2.2 分步执行

如需单独执行某一步，或想了解每步的细节。

### 步骤 1：MongoDB 初始化

```bash
cd backend
python scripts/init_mongodb.py
```

执行内容：

1. **创建集合**（16 个业务集合，统一 `cr_` 前缀）

   | 集合名 | 用途 |
   | --- | --- |
   | `cr_contract` | 合同主记录 |
   | `cr_contract_clause` | 条款结构化结果 |
   | `cr_ocr_result` | OCR 识别结果与置信度 |
   | `cr_review_task` | 审查任务与状态机 |
   | `cr_review_risk_hit` | 风险命中明细 |
   | `cr_review_conclusion` | 审查结论（含防篡改校验值） |
   | `cr_rule_template` | 规则模板 |
   | `cr_rule_hit_log` | 规则命中留痕 |
   | `cr_audit_log` | 全链路审计日志 |
   | `cr_standard_clause` | 标准条款库 |
   | `cr_suggestion` | 修改建议 |
   | `cr_approval_record` | 审批记录 |
   | `cr_write_back_log` | 回写日志 |
   | `cr_report` | 导出报告记录 |
   | `cr_file_object` | 文件对象元数据 |
   | `cr_sys_config` | 系统配置 |

2. **创建索引**

   为每个集合创建必要的业务索引，覆盖：
   - 唯一索引：`task_no`、`contract_no`、`rule_code` 等
   - 查询索引：状态、时间范围、合同类型组合索引
   - 审计索引：`task_id + created_at` 便于按任务追溯

3. **创建角色与用户**（最小权限原则）

   | 角色 | 权限 |
   | --- | --- |
   | `cr_app_rw` | 业务库读写（应用使用） |
   | `cr_readonly` | 只读（报表 / 排查使用） |

   | 用户 | 角色 |
   | --- | --- |
   | `cr_app` | `cr_app_rw` |
   | `cr_readonly_user` | `cr_readonly` |

   > 生产环境请务必修改默认密码，并同步更新 `MONGO_URI`。

4. **导入系统配置**（9 项 `cr_sys_config`）

   主要配置项：

   | 配置键 | 说明 | 是否可改 |
   | --- | --- | --- |
   | `rule_engine.enabled` | 规则引擎开关 | **否（红线）** |
   | `rule_engine.block_levels` | 触发阻塞的风险等级 | 是 |
   | `rule_engine.semantic_threshold` | 语义匹配阈值 | 是 |
   | `route.deep_amount_threshold` | 深度复核金额阈值 | 是 |
   | `route.deep_force_types` | 强制深度复核的合同类型 | 是 |
   | `ocr.min_confidence_threshold` | OCR 最低置信度 | 是 |
   | `parse.min_content_length` | 正文最小字符数 | 是 |
   | `task.max_retry` | 节点最大重试次数 | 是 |
   | `approval.allowed_apis` | 审批白名单 | **否（红线）** |

   > **幂等设计**：已存在的 `config_value` 不会被覆盖，
   > 这样重复执行初始化脚本不会把运维改过的运行时配置冲掉。

5. **导入规则样例数据**

   把 `backend/config/risk_rules.yaml` 的 34 条规则同步到 `cr_rule_template` 集合，
   供前端"合规规则与模板页"展示。

   > 注意：规则引擎运行时**从 YAML 文件加载**规则，不依赖该集合。
   > 因此即使这一步跳过，审查功能也不受影响，只是前端规则列表页会显示为空。

常用参数：

```bash
python scripts/init_mongodb.py --verify-only    # 只检查不创建
python scripts/init_mongodb.py --skip-rules     # 不导入规则样例
python scripts/init_mongodb.py --init-roles     # 同时创建角色与用户
python scripts/init_mongodb.py --drop-all       # 删除全部集合（危险）
```

### 步骤 2：Milvus 建库建集合

```bash
cd backend
python scripts/init_milvus_vectors.py --skip-rules
```

执行内容：

1. **映射自检**（步骤 0）
   校验 14 个 `ClauseType` 枚举值是否都有对应的中文映射。
   这是一道防护栏：如果枚举新增了成员但映射表没更新，
   向量化出来的规则文本会出现"未知条款类型"，导致检索质量静默下降。

2. **连通性探测** —— Milvus 是否可达

3. **维度实测校验** —— 调用 Embedding 服务实测返回维度是否为 1024

4. **创建集合** —— 在 `contract_review` 独立库中创建 3 个集合

   集合 Schema（三个集合一致）：

   | 字段 | 类型 | 说明 |
   | --- | --- | --- |
   | `id` | VarChar(64) | 主键 |
   | `contract_id` | VarChar(64) | 合同 ID |
   | `clause_id` | VarChar(64) | 条款 ID |
   | `clause_type` | VarChar(32) | 条款类型 |
   | `content` | VarChar(8192) | 文本内容 |
   | `embedding` | FLOAT_VECTOR(1024) | 向量 |
   | `created_at` | Int64 | 创建时间戳 |

   索引：`HNSW`，度量方式 `COSINE`，参数 `M=16` / `efConstruction=200`

### 步骤 3：规则批量向量化

```bash
cd backend
python scripts/init_milvus_vectors.py
```

这是**核心步骤**：把 `risk_rules.yaml` 的 34 条风险规则转成向量写入 Milvus，
使规则也能被语义检索召回。

**向量化文本的构造方式**（关键设计）：

每条规则被编码为结构化中文文本：

```
[合同类型] 采购合同
[条款类型] 付款结算
[规则名称] 无验收即全款支付
[风险说明] 约定合同签订后全额预付，未设置验收前置条件...
[法律依据] 《民法典》第六百二十六条...
[匹配方式] keyword
```

**刻意排除 `suggestion_template`（修改建议）**：
修改建议描述的是"怎么改"，而不是"什么算违规"。
若把它一起向量化，会引入语义漂移——检索"验收"时可能召回
与验收无关但建议文案相似的规则，反而降低准确率。

其他特性：

- **ID 设计**：规则向量使用 `rule:` 前缀（如 `rule:PUR-001`），
  与合同条款向量（`clause:` 前缀）在同一集合内互不干扰
- **批量并发 + 逐条兜底**：批量调用失败时自动降级为逐条调用并输出 WARNING
- **自召回验证**（步骤 6）：用规则原文反向检索，检查 Top-K 是否命中自己，
  Top1 命中率应 ≥ 80%，低于该值会告警

常用参数：

```bash
python scripts/init_milvus_vectors.py --dry-run          # 只打印不写入
python scripts/init_milvus_vectors.py --test-search      # 只做自召回验证
python scripts/init_milvus_vectors.py --verify-only      # 只检查集合状态
python scripts/init_milvus_vectors.py --drop-collections # 删除集合（危险）
```

---

# 第三章 向量库初始化

第三章的内容已合并到 [第二章步骤 3](#步骤-3规则批量向量化)。
单独说明向量库的日常维护方式。

## 3.1 何时需要重新向量化

| 场景 | 是否需要重新向量化 |
| --- | --- |
| 修改了 `risk_rules.yaml` 中的规则文案 | ✅ 需要 |
| 新增 / 删除规则 | ✅ 需要 |
| 只改了 `enabled` 开关 | ❌ 不需要（开关在规则引擎层生效） |
| 只改了 `suggestion_template` | ❌ 不需要（该字段不参与向量化） |
| 新增了标准条款库内容 | ✅ 需要（写入 `cr_standard_clause_lib`） |
| 更换了 Embedding 模型 | ✅ 需要，且必须重建集合 |

## 3.2 验证向量库状态

```bash
cd backend

# 查看集合状态与文档数
python scripts/init_milvus_vectors.py --verify-only

# 执行自召回测试，验证检索质量
python scripts/init_milvus_vectors.py --test-search --top-k 5
```

自检脚本也会覆盖这部分：

```bash
python scripts/system_check.py --only milvus
```

## 3.3 数据量参考

| 集合 | 初始数据量 | 说明 |
| --- | --- | --- |
| `cr_rule_templates` | 34 | 风险规则向量 |
| `cr_standard_clause_lib` | 按需导入 | 标准条款库 |
| `cr_clauses` | 运行时增长 | 每份合同解析后写入 |

---

# 第四章 后端启动

## 4.1 安装依赖

```bash
cd backend

# 1. 创建虚拟环境（必须 Python 3.10）
py -3.10 -m venv .venv           # Windows
python3.10 -m venv .venv         # Linux / macOS

# 2. 激活虚拟环境
.venv\Scripts\activate           # Windows (CMD)
source .venv/Scripts/activate    # Windows (Git Bash)
source .venv/bin/activate        # Linux / macOS

# 3. 升级 pip（避免旧版解析依赖失败）
python -m pip install --upgrade pip

# 4. 安装依赖
pip install -r requirements.txt
```

> **安装耗时提示**：`paddlepaddle`、`opencv-python-headless` 体积较大，
> 首次安装可能需要 5~15 分钟，属正常现象。
>
> **国内网络加速**：
> ```bash
> pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
> ```

### PaddleOCR 模型准备

首次运行 OCR 时会自动下载模型到 `~/.paddleocr`。
**内网环境**需提前离线放置模型文件，否则 OCR 相关功能会失败。

## 4.2 环境配置

```bash
cd backend

# 复制模板
copy .env.example .env           # Windows
cp .env.example .env             # Linux / macOS

# 编辑 .env，按实际环境修改以下必改项
```

### 必改项清单

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `MONGO_URI` | `mongodb://127.0.0.1:27017` | 开启鉴权后需带账号密码 |
| `MINIO_ENDPOINT` | `127.0.0.1:9000` | MinIO 地址 |
| `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` | `minioadmin` | **生产必须修改** |
| `MILVUS_HOST` / `MILVUS_PORT` | `192.168.10.130:7000` | Milvus 实例地址 |
| `EMBEDDING_BASE_URL` | `http://127.0.0.1:9997/v1` | BGE-M3 服务地址 |
| `LLM_BASE_URL` | `http://127.0.0.1:8001/v1` | 大模型服务地址 |
| `APPROVAL_BASE_URL` | `http://127.0.0.1:9100` | 审批系统地址 |
| `APPROVAL_API_KEY` | `your-approval-api-key` | 对接密钥 |
| `JWT_SECRET_KEY` | `change-this-...` | **生产必须改为强随机值** |
| `APP_ENV` | `dev` | 生产改 `prod` |
| `APP_DEBUG` | `True` | 生产必须 `False` |
| `SERVER_RELOAD` | `True` | 生产必须 `False` |

### 关键配置分组（共 18 组）

| 组 | 主要变量 |
| --- | --- |
| 一、基础应用 | `APP_NAME` / `APP_ENV` / `APP_DEBUG` |
| 二、服务监听 | `SERVER_HOST` / `SERVER_PORT` / `SERVER_WORKERS` / `SERVER_RELOAD` |
| 三、跨域配置 | `CORS_ORIGINS` |
| 四、MongoDB | `MONGO_URI` / `MONGO_DB_NAME` / 连接池 |
| 五、MinIO | `MINIO_ENDPOINT` / 密钥 / 5 个桶名 |
| 六、Milvus | `MILVUS_HOST/PORT` / `MILVUS_DB_NAME` / 3 个集合名 / 索引参数 |
| 七、Embedding | `EMBEDDING_BASE_URL` / `EMBEDDING_DIM=1024` |
| 八、LLM | `LLM_BASE_URL` / `LLM_MODEL_NAME` / `LLM_TEMPERATURE=0.1` |
| 九、文件上传 | `UPLOAD_MAX_SIZE_MB=100` / 允许扩展名 |
| 十、文档解析 | `OCR_MIN_CONFIDENCE_THRESHOLD=0.6` / `MIN_CONTENT_LENGTH=50` |
| 十一、规则引擎 | `RULE_ENGINE_ENABLED=True` / `RULE_ENGINE_BLOCK_LEVELS` |
| 十二、差异化路由 | `ROUTE_DEEP_AMOUNT_THRESHOLD=5000000` / `ROUTE_DEEP_FORCE_TYPES` |
| 十三、任务与重试 | `TASK_MAX_RETRY=3` / 退避参数 |
| 十四、生成质量闭环 | `QUALITY_MAX_FIX_ROUNDS=2` |
| 十五、报告导出 | `REPORT_FORMATS` / `REPORT_URL_EXPIRE_SECONDS` |
| 十六、审批系统 | `APPROVAL_BASE_URL` / `APPROVAL_COMMENT_API_PATH=/comments` |
| 十七、角色与鉴权 | `AUTH_ENABLED` / `JWT_SECRET_KEY` / 三个角色标识 |
| 十八、日志 | `LOG_LEVEL` / `LOG_DIR` / `LOG_RETENTION_DAYS` |

## 4.3 启动方式

### 方式一：一键启动（推荐）

```bash
cd deploy/scripts
python devctl.py start backend
```

优点：自动做依赖预检、后台运行、日志落盘、记录 PID 便于停止。

### 方式二：直接启动（前台，便于看实时日志）

```bash
cd backend

# 开发模式（热重载）
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload

# 或使用 main.py 内置入口
python app/main.py
```

### 方式三：生产模式

```ini
# .env
APP_ENV=prod
APP_DEBUG=False
SERVER_RELOAD=False
SERVER_WORKERS=4
```

```bash
cd backend
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4
```

## 4.4 启动过程说明

后端启动时 `lifespan` 会按序执行以下初始化，**任一步失败都会导致启动失败**：

```
1. init_logging()           日志系统
2. init_all_storages()      MongoDB → MinIO → Milvus（依次连接 + 建集合/桶）
3. llm_client.init_client()       LLM 客户端
4. embedding_client.init_client() Embedding 客户端
```

因此，如果基础设施没就绪，后端会启动失败并打印具体哪个组件连不上。

## 4.5 端口与访问地址

| 地址 | 说明 |
| --- | --- |
| http://127.0.0.1:8000 | 后端服务根地址 |
| http://127.0.0.1:8000/docs | Swagger 接口文档（交互式） |
| http://127.0.0.1:8000/redoc | ReDoc 接口文档 |
| http://127.0.0.1:8000/api/v1/health | 存活探针（不触碰外部依赖） |
| http://127.0.0.1:8000/api/v1/ready | 就绪探针（并发检查全部依赖） |
| http://127.0.0.1:8000/api/v1/config-check | 配置自检（敏感字段脱敏） |

### 统一响应结构

```json
{
  "code": 0,
  "message": "操作成功",
  "data": {},
  "request_id": "56416943-6aa8-4785-a5cf-0c06a7c9d2a8"
}
```

`code = 0` 表示成功；非 0 为业务错误码，详见 `app/core/exceptions.py` 的 `ErrorCode`。

## 4.6 停止后端

```bash
cd deploy/scripts
python devctl.py stop backend
```

---

# 第五章 前端启动

## 5.1 安装依赖

```bash
cd frontend
npm install
```

> 国内网络加速：
> ```bash
> npm install --registry=https://registry.npmmirror.com
> ```

## 5.2 启动开发服务器

```bash
cd frontend
npm run dev
```

**访问地址：http://127.0.0.1:5173**

### 代理配置说明

前端通过 Vite 代理转发 `/api` 请求到后端，规避浏览器跨域：

```ts
// frontend/vite.config.ts
server: {
  port: 5173,
  host: '127.0.0.1',
  proxy: {
    '/api': {
      target: 'http://127.0.0.1:8000',
      changeOrigin: true,
    },
  },
}
```

因此：
- 前端代码里请求 `/api/v1/...` 即可，无需写完整后端地址
- 修改后端端口时需同步修改此处 `target`

## 5.3 其他命令

| 命令 | 作用 |
| --- | --- |
| `npm run dev` | 启动开发服务器 |
| `npm run build` | 类型检查 + 生产构建（输出到 `dist/`） |
| `npm run preview` | 预览生产构建产物 |
| `npm run type-check` | 只做 TypeScript 类型检查 |
| `npm run lint` | ESLint 检查 |

> **注意**：`npm run build` 会先执行 `tsc -b`。
> 如果存在类型错误，构建会失败并列出所有问题——这是有意设计，
> 避免类型错误被 Vite 的 esbuild 静默忽略后带到生产环境。

## 5.4 CORS 配置对应关系

后端 CORS 白名单需包含前端地址（已在 `.env.example` 中配好）：

```ini
CORS_ORIGINS=["http://localhost:5173","http://127.0.0.1:5173"]
```

如果前端端口不是 5173，必须同步修改，否则浏览器会拦截请求。

## 5.5 停止前端

```bash
cd deploy/scripts
python devctl.py stop frontend
```

---

# 第六章 审批回写模拟模块

## 6.1 为什么需要模拟审批系统

真实企业审批系统（OA / BPM）通常不具备测试环境，或接入成本高。
本项目提供了模拟实现，用途有三：

1. **演示完整性** —— 展示"审查意见回写到审批评论区"的完整链路
2. **联调便利** —— 本地即可验证回写逻辑、重试机制、超时处理
3. **红线验证器** —— 这是最重要的用途，见下

## 6.2 它同时是一个红线验证器

模拟审批系统**不是**一个"什么请求都答应"的假服务。它内置了与生产系统
完全一致的红线规则：

| 类别 | 接口 | 模拟系统的行为 |
| --- | --- | --- |
| 白名单 | `POST /comments` | ✅ 正常接收，返回 200 |
| 白名单 | `POST /comments/batch` | ✅ 正常接收，返回 200 |
| 黑名单 | `POST /approve` | ❌ **强制 403** |
| 黑名单 | `POST /reject` | ❌ **强制 403** |
| 黑名单 | `POST /transfer` | ❌ **强制 403** |
| 黑名单 | `POST /withdraw` | ❌ **强制 403** |
| 黑名单 | `POST /reassign` | ❌ **强制 403** |
| 黑名单 | `POST /sign` | ❌ **强制 403** |
| 黑名单 | `POST /complete` | ❌ **强制 403** |
| 黑名单 | `POST /terminate` | ❌ **强制 403** |

**设计意图**：即使前端或后端被误改，导致发出了"批准 / 驳回"类请求，
也会在这里被硬拒绝。**即使携带了合法的 Authorization 头也照样拒绝**——
因为"有权限"不等于"应该做这个动作"。

这样可以做到"用一次演示证明系统不会越权"，而不是只在文档里声明。

## 6.3 启动模拟审批系统

### 方式一：一键启动（推荐）

```bash
cd deploy/scripts
python devctl.py start mock-approval
```

### 方式二：直接启动（前台）

```bash
cd deploy/mock_approval
python -m uvicorn main:app --host 127.0.0.1 --port 9100
```

Windows 可直接双击 `deploy/mock_approval/start_mock_approval.bat`。

### 访问地址

| 地址 | 说明 |
| --- | --- |
| http://127.0.0.1:9100/docs | 接口文档 |
| http://127.0.0.1:9100/health | 健康检查 |
| http://127.0.0.1:9100/audit | **审计记录（含红线验证结论）** |
| http://127.0.0.1:9100/comments | 已接收的评论列表 |
| http://127.0.0.1:9100/approvals | 已跟踪的审批单列表 |

## 6.4 后端配置对齐

`backend/.env` 中审批相关配置需与模拟系统一致：

```ini
APPROVAL_BASE_URL=http://127.0.0.1:9100
APPROVAL_COMMENT_API_PATH=/comments
APPROVAL_API_KEY=your-approval-api-key
APPROVAL_TIMEOUT_SECONDS=30.0
APPROVAL_MAX_RETRIES=3
APPROVAL_CONNECT_TIMEOUT_SECONDS=10.0
```

> `EXPECTED_API_KEY` 在 `deploy/mock_approval/main.py` 中定义，
> 需与 `APPROVAL_API_KEY` 保持一致。

## 6.5 验证红线（推荐执行）

项目提供了自动化验证脚本，一次执行验证 13 项：

```bash
cd deploy/scripts
bash verify_mock_approval.sh
```

脚本覆盖 5 组共 13 项检查：

| 组 | 检查内容 | 项数 |
| --- | --- | --- |
| 1 | 服务存活 | 1 |
| 2 | 白名单接口写入 + 读回验证 | 2 |
| 3 | 8 个动作类接口必须全部 403（**故意携带合法 Authorization**） | 8 |
| 4 | 无鉴权请求应返回 401 | 1 |
| 5 | 审计留痕可查 | 1 |

期望结果：**13/13 全部通过**。

### 手工验证单条

```bash
# 1. 白名单：应成功，返回 200
curl -X POST http://127.0.0.1:9100/comments \
  -H "Authorization: Bearer your-approval-api-key" \
  -H "Content-Type: application/json" \
  -d '{"contract_review_comment":"测试评论","task_no":"T-001","final_risk_level":"low","risk_count":0}'

# 2. 黑名单：应被拒绝，返回 403
curl -X POST http://127.0.0.1:9100/approve \
  -H "Authorization: Bearer your-approval-api-key" \
  -H "Content-Type: application/json" \
  -d '{"task_no":"T-001"}'

# 3. 查看审计记录（verdict 字段给出一句话结论）
curl http://127.0.0.1:9100/audit
```

## 6.6 设计说明

| 设计 | 原因 |
| --- | --- |
| 数据存内存 | 演示用途，无需持久化；重启即清空，避免脏数据干扰 |
| 鉴权宽松（只检查 Bearer 非空） | 便于联调；重点验证的是"动作被拒"，而非"鉴权严格" |
| 审批单自动创建 | `GET /approval/{task_no}` 遇到不存在的单号自动建，避免联调时手工准备数据 |
| 全局异常处理器 | 保证任何异常都返回结构化 JSON，便于脚本解析 |

---

# 第七章 系统自检

## 7.1 一键自检

```bash
cd backend
python scripts/system_check.py
```

输出 8 个分节的诊断报告，每项失败都附带**具体的排查动作**：

| 分节 | 检查内容 |
| --- | --- |
| 一、配置自检 | 维度、端口、路径、红线开关（不依赖外部服务） |
| 二、MongoDB | 连接、库、集合完整性、索引完整性、规则与配置数据 |
| 三、MinIO | 连接、5 个存储桶 |
| 四、Milvus | 连接、独立库确认、3 个集合、向量维度、索引 |
| 五、Embedding 服务 | 连通性 + **实测维度** |
| 六、LLM 服务 | 连通性（非阻塞，WARN 级别） |
| 七、审批系统 | 连通性、白名单可达性、动作类接口应拒绝 |
| 八、规则配置 | YAML 可加载、规则数量、四类高风险规则齐备 |

### 分级说明

| 标记 | 含义 | 是否需要处理 |
| --- | --- | --- |
| `[PASS]` | 正常 | 否 |
| `[WARN]` | 异常但不阻塞核心功能 | 建议处理 |
| `[FAIL]` | 异常且阻塞核心功能 | **必须处理** |
| `[SKIP]` | 已跳过 | 视情况 |

### 常用参数

```bash
python scripts/system_check.py              # 完整自检
python scripts/system_check.py --quick      # 快速模式
python scripts/system_check.py --json       # JSON 输出（供 CI 消费）
python scripts/system_check.py --only milvus # 只检查指定组件
```

### 退出码

- `0` = 无 FAIL（可能有 WARN）
- `1` = 存在 FAIL

可直接用于 CI 流水线门禁。

## 7.2 快速连通性探测

启动服务前，先确认基础设施是否就绪：

```bash
cd deploy/scripts
python devctl.py probe
```

输出：

```
  [ OK ] MongoDB                127.0.0.1:27017  可达（12ms）
  [FAIL] MinIO                  127.0.0.1:9000   不可达
         → cd deploy/docker && docker compose up -d minio
  ...
```

## 7.3 服务状态总览

```bash
cd deploy/scripts
python devctl.py status
```

输出包含三部分：

1. **基础设施**状态（`[ OK ]` / `[DOWN]`）
2. **应用服务**状态，四种标记：
   - `[RUN ]` 运行中（由本脚本启动，显示 PID）
   - `[RUN ]` 运行中（非本脚本启动，PID 未知）
   - `[WAIT]` PID 存活但端口未监听（可能仍在启动）
   - `[BUSY]` **端口被无关程序占用**（需处理，会显示占用进程名）
   - `[STOP]` 未运行
3. **访问入口**地址

> `[BUSY]` 是实测中发现的真实问题：VMware 的 `vmnat.exe` 默认占用 8000 端口，
> 会导致后端无法启动。`status` 会明确指出占用进程名，便于快速定位。

## 7.4 各组件独立验证命令

| 组件 | 验证命令 | 期望结果 |
| --- | --- | --- |
| MongoDB | `mongosh --eval "db.adminCommand('ping')"` | `{ ok: 1 }` |
| MinIO | `curl -I http://127.0.0.1:9000/minio/health/live` | `200 OK` |
| Milvus | `curl http://192.168.10.130:9091/healthz` | `OK` |
| Embedding | `curl http://127.0.0.1:9997/v1/models` | 返回模型列表 |
| LLM | `curl http://127.0.0.1:8001/v1/models` | 返回模型列表 |
| 后端 | `curl http://127.0.0.1:8000/api/v1/health` | `{"code":0,...}` |
| 前端 | `curl -I http://127.0.0.1:5173` | `200 OK` |
| 模拟审批 | `curl http://127.0.0.1:9100/health` | `{"code":0,...}` |

## 7.5 CI 集成示例

```yaml
# .github/workflows/deploy-check.yml（示例）
- name: 系统自检
  run: |
    cd backend
    python scripts/system_check.py --json > check-result.json
  # 退出码 1 会直接让流水线失败
```

---

# 第八章 常见问题排查

## 8.1 启动失败

### 症状：后端启动即退出，日志报某个组件连接失败

**排查顺序**（从外到内）：

```bash
# 1. 先看基础设施是否都活着
cd deploy/scripts
python devctl.py probe

# 2. 再看详细诊断
cd backend
python scripts/system_check.py
```

后端 `lifespan` 会依次初始化日志 → 存储 → 模型客户端，
**任一步失败即整体失败**。所以要在日志里找**第一个**失败点：

```
INFO  | 日志系统初始化完成
INFO  | 正在连接 MongoDB | ...
ERROR | MongoDB 连接失败     ← 就是这里
```

### 症状：端口被占用

```bash
# 查看占用者
netstat -ano | findstr ":8000"

# 确认进程名（把 <PID> 换成上一步查到的 PID）
tasklist /FI "PID eq <PID>"

# 是本项目残留进程 → 用脚本停止
python devctl.py stop backend

# 是无关程序（如 vmnat.exe）→ 换端口或关闭该服务
```

换端口的做法：修改 `backend/.env` 的 `SERVER_PORT`，
同时修改 `frontend/vite.config.ts` 中的 `proxy.target` 与 `CORS_ORIGINS`。

### 症状：依赖导入失败

```bash
# 确认用的是 3.10 且依赖装齐
python --version
python -c "import fastapi, uvicorn, langgraph, motor, minio, pymilvus; print('OK')"
```

若报 `ModuleNotFoundError`：

```bash
# 确认虚拟环境已激活（命令行前应有 (.venv)）
pip install -r requirements.txt
```

> **最常见的坑**：在未激活虚拟环境的情况下执行命令，
> 用的是全局 Python（可能是 3.13），自然找不到依赖。

## 8.2 连接超时

### MongoDB 连接超时

```bash
# 1. 服务是否在跑
docker ps | grep cr-mongodb     # Docker 方式
net start | findstr MongoDB     # Windows 服务方式

# 2. 端口是否通
telnet 127.0.0.1 27017

# 3. 连接串是否正确
# 检查 backend/.env 的 MONGO_URI
# 开启鉴权后格式：mongodb://user:password@127.0.0.1:27017/?authSource=admin
```

**常见原因**：
- 服务未启动
- `MONGO_URI` 缺少 `authSource=admin`（开启鉴权后）
- 连接池超时设置过小

### Milvus 连接超时（报 DEADLINE_EXCEEDED）

```bash
# 1. 网络可达性
ping 192.168.10.130

# 2. 端口
telnet 192.168.10.130 7000

# 3. 服务健康
curl http://192.168.10.130:9091/healthz
```

**可能原因与处理**：

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| ping 不通 | 网络 / 路由问题 | 检查虚拟机网络模式 |
| 端口不通但 ping 通 | 端口未映射或防火墙拦截 | 检查端口映射与防火墙规则 |
| 端口通但报 DEADLINE_EXCEEDED | Milvus 正在加载集合 | 等待后重试；首次加载大集合可能需数分钟 |
| 偶发超时 | 负载高 | 调大 `MILVUS_TIMEOUT_SECONDS` |

### MinIO 连接超时

```bash
docker ps | grep cr-minio
curl -I http://127.0.0.1:9000/minio/health/live
```

**常见原因**：密钥不匹配。`MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY`
必须与容器启动时的 `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` 一致。

### Embedding / LLM 连接超时

```bash
curl http://127.0.0.1:9997/v1/models    # Embedding
curl http://127.0.0.1:8001/v1/models    # LLM
```

**若返回 404**：地址少了 `/v1` 后缀，或服务协议不兼容 OpenAI 规范。

## 8.3 文档解析与 OCR 异常

### 症状：任务卡在 `parsing` 不前进

按阻塞原因码分别处理。系统定义了 6 种阻塞原因：

| 原因码 | 触发条件 | 处理方式 |
| --- | --- | --- |
| `DOC_ENCRYPTED` | 文件头含 `/Encrypt`（加密 PDF） | 要求用户提供解密版本 |
| `SCAN_TOO_BLURRY` | OCR 平均置信度 < `0.6` | 要求重新扫描，提高分辨率 |
| `EMPTY_CONTENT` | 抽取正文 < `50` 字符 | 确认文件是否为纯图片无文字 |
| `PARSE_FAILED` | 解析器抛异常 | 查看日志中的具体异常 |
| `RETRY_EXHAUSTED` | 重试超过 `TASK_MAX_RETRY` | 排查根因后人工重跑 |
| `MANUAL` | 人工标记阻塞 | 人工处理后重跑 |

**查看阻塞详情**：

```bash
# 查任务状态
curl http://127.0.0.1:8000/api/v1/tasks/<task_id>
```

### 症状：OCR 置信度偏低，正常扫描件被判定模糊

调整阈值（`backend/.env`）：

```ini
OCR_MIN_CONFIDENCE_THRESHOLD=0.6    # 默认值；扫描质量差时可降到 0.5
```

> 降低阈值会让更多低质量扫描件通过，但会增加误识别风险。
> 更推荐的做法是提高扫描质量（300 DPI 以上）。

### 症状：OCR 首次运行很慢或失败

PaddleOCR 首次运行会下载模型到 `~/.paddleocr`。内网环境需离线放置：

```bash
# 在有网机器上下载后，拷贝到目标机器的 ~/.paddleocr 目录
```

### 症状：扫描件 PDF 解析不出文字

系统对扫描件有 OCR 降级链路：

```
文本抽取 → 文本量为空 → PDF 转图片 → 图像前处理（去噪/二值化/倾斜校正）→ OCR 识别
```

排查时确认：
1. `pdf2image` 依赖的 **poppler** 是否已安装（Windows 需单独下载并加入 PATH）
2. 页面图片是否正常生成（检查 `contract-ocr-image` 桶）

## 8.4 回写接口报错

### 症状：回写返回 403

**403 通常是"正确行为"**，不是 bug。可能原因：

1. **请求路径不在白名单内** —— 系统只允许 `/comments` 和 `/comments/batch`。
   这是红线二的强制体现。检查 `APPROVAL_COMMENT_API_PATH` 是否被改动。

2. **代码直接调用了动作类接口** —— 如 `/approve`、`/reject`。
   这属于违规调用，`approval_guard.ensure_comment_only()` 会抛
   `ApprovalPermissionException`。

```bash
# 确认白名单配置正确
cd backend
python -c "from app.utils.approval_guard import ALLOWED_APPROVAL_APIS; print(ALLOWED_APPROVAL_APIS)"
# 期望：['/comments', '/comments/batch']
```

### 症状：回写返回 401

鉴权失败。检查：
- `APPROVAL_API_KEY` 是否与审批系统的 `EXPECTED_API_KEY` 一致
- 请求头是否为 `Authorization: Bearer <key>` 格式

### 症状：连接被拒绝（Connection refused）

审批系统未启动：

```bash
cd deploy/scripts
python devctl.py start mock-approval
```

生产环境则确认 `APPROVAL_BASE_URL` 正确且网络可达。

### 症状：返回 502 / 503 / 504（网关错误）

**这类响应说明请求根本没到达审批系统**，而是被中间的代理或网关拦下了。
不要误判为"接口契约不匹配"，否则会白查半天。

最常见的原因是**本机配了 HTTP 代理，把 127.0.0.1 的请求也代理走了**：

```bash
# 检查是否设置了代理
echo $HTTP_PROXY
echo $HTTPS_PROXY
```

处理方式：把本地地址加入 `NO_PROXY` 白名单。

```bash
# Git Bash / Linux / macOS —— 加入当前会话
export NO_PROXY=127.0.0.1,localhost
export no_proxy=127.0.0.1,localhost

# 永久生效：写入 ~/.bashrc 或系统环境变量
```

系统自检脚本会识别 502/503/504 并直接给出这个排查方向：

```bash
cd backend
python scripts/system_check.py --only approval
```

### 症状：回写超时

```ini
APPROVAL_TIMEOUT_SECONDS=30.0        # 整体超时
APPROVAL_CONNECT_TIMEOUT_SECONDS=10.0 # 连接超时
APPROVAL_MAX_RETRIES=3                # 重试次数
```

回写失败会记录到 `cr_write_back_log`，状态流转为
`not_written → writing → failed`，重试上限后需人工重试。

### 症状：回写内容不完整 / 缺少字段

检查请求体构造。系统只发送 4 个字段：

```json
{
  "contract_review_comment": "审查意见正文",
  "task_no": "任务编号",
  "final_risk_level": "风险等级",
  "risk_count": 3
}
```

`forbidden_body_keys` 会强制剔除以下字段（防止越权）：
`action` / `approve` / `reject` / `transfer` / `withdraw` / `reassign` /
`sign` / `complete` / `terminate` / `result` / `decision`

## 8.5 阻塞任务处理

### 状态机

```
pending → parsing → reviewing → completed
             ↓          ↓
           blocked ─────┘  （任意阶段可进入）
             ↓
        人工重跑 → 回到原阶段
```

### 处理流程

```bash
# 1. 查看阻塞任务
curl "http://127.0.0.1:8000/api/v1/tasks?status=blocked"

# 2. 查看阻塞原因
curl http://127.0.0.1:8000/api/v1/tasks/<task_id>
# 响应中的 block_reason 字段说明具体原因

# 3. 按原因处理（见 8.3 的阻塞原因码表）

# 4. 人工重跑（断点续跑，从原阶段继续，不重新开始）
```

> **断点续跑机制**：系统使用 LangGraph 检查点保存流程状态，
> 重跑时通过 `resume_review_flow(thread_id, updates)` 修正阻塞标记后
> 从检查点继续，不会重复已完成的节点。
>
> **当前限制**：开发环境使用 `MemorySaver`（进程内存储），
> **重启后进程内的检查点会丢失**。生产环境需替换为持久化后端
> （`MongoDBSaver` / `SqliteSaver`），替换点在
> `app/engines/graph_builder.py` 的 `_build_checkpointer()`。

### 重试耗尽的处理

```ini
TASK_MAX_RETRY=3                # 最大重试次数
TASK_RETRY_BASE_DELAY=1.0       # 退避基数：1s → 2s → 4s
TASK_RETRY_MAX_DELAY=8.0        # 单次退避上限
```

重试耗尽后任务标记为 `blocked`，原因码 `RETRY_EXHAUSTED`。
**不要直接盲目重跑**，先定位根因（通常是外部依赖不稳定）。

## 8.6 规则引擎相关问题

### 症状：规则不生效

```bash
# 1. 确认规则文件可加载
cd backend
python -c "
from app.engines.rule_engine import rule_registry, settings
rule_registry.load_yaml(settings.RULE_RULES_PATH)
print('规则数:', rule_registry.size)
"

# 2. 检查规则是否 enabled
# 打开 backend/config/risk_rules.yaml，确认目标规则的 enabled: true

# 3. 检查规则适用合同类型
# 规则有 contract_type 过滤，不匹配的合同类型不会被应用
```

### 症状：规则引擎被禁用导致系统拒绝执行

```
RULE_ENGINE_ENABLED=False
```

**这是设计行为，不是 bug**。规则引擎是所有正式判定的唯一裁决方，
禁用后系统拒绝执行审查流程，以保证绝不出现"未经规则判定即放行"。

```ini
# backend/.env
RULE_ENGINE_ENABLED=True
```

### 症状：语义类规则召回率低

```bash
# 1. 确认规则已向量化
cd backend
python scripts/init_milvus_vectors.py --test-search --top-k 5
# 关注 Top1 命中率，应 ≥ 80%

# 2. 调整语义阈值
```

```ini
RULE_SEMANTIC_DEFAULT_THRESHOLD=0.82   # 降低可提升召回，但会增加误召
```

## 8.7 前端相关问题

### 症状：前端报接口 404 / CORS 错误

```bash
# 1. 后端是否在跑
curl http://127.0.0.1:8000/api/v1/health

# 2. 代理配置是否指向正确的后端端口
# 检查 frontend/vite.config.ts 的 proxy.target

# 3. CORS 白名单是否包含前端地址
# 检查 backend/.env 的 CORS_ORIGINS
```

### 症状：`npm run build` 类型报错

这是**有意设计**，避免类型错误被静默忽略。先单独检查：

```bash
cd frontend
npm run type-check
```

### 症状：pdfjs-dist 相关报错

pdf.js 需要 worker 文件正确加载。检查 `PDFViewer` 组件中的
`GlobalWorkerOptions.workerSrc` 配置是否指向正确路径。

---

## 附录 A：脚本清单

| 脚本 | 路径 | 用途 |
| --- | --- | --- |
| 进程管理器 | `deploy/scripts/devctl.py` | start / stop / restart / status / logs / probe |
| 一键初始化 | `deploy/scripts/init_all.py` | 按序完成 4 步初始化 |
| 环境预检 | `deploy/scripts/env_probe.sh` | 运行时版本、依赖连通性、端口占用 |
| 红线验证 | `deploy/scripts/verify_mock_approval.sh` | 13 项审批红线检查 |
| MongoDB 初始化 | `backend/scripts/init_mongodb.py` | 库 / 集合 / 索引 / 角色 / 数据 |
| Milvus 向量化 | `backend/scripts/init_milvus_vectors.py` | 建库建集合 + 规则向量化 |
| 系统自检 | `backend/scripts/system_check.py` | 8 分节全链路诊断 |
| 模拟审批 | `deploy/mock_approval/main.py` | 审批系统模拟 + 红线验证 |

Windows 便捷入口：

| 批处理 | 等价命令 |
| --- | --- |
| `deploy/scripts/start_all.bat` | `python devctl.py start` |
| `deploy/scripts/stop_all.bat` | `python devctl.py stop` |
| `deploy/scripts/init_all.bat` | `python init_all.py` |
| `deploy/mock_approval/start_mock_approval.bat` | 前台启动模拟审批 |

## 附录 B：端口清单

| 端口 | 组件 | 可修改位置 |
| --- | --- | --- |
| 27017 | MongoDB | `MONGO_URI` |
| 9000 | MinIO API | `MINIO_ENDPOINT` |
| 9001 | MinIO 控制台 | docker-compose 端口映射 |
| 7000 | Milvus | `MILVUS_PORT` |
| 9997 | Embedding | `EMBEDDING_BASE_URL` |
| 8001 | LLM | `LLM_BASE_URL` |
| 8000 | 后端 | `SERVER_PORT` + vite proxy |
| 5173 | 前端 | vite.config.ts |
| 9100 | 模拟审批 | `APPROVAL_BASE_URL` + mock main.py |

## 附录 C：日志位置

| 日志 | 路径 |
| --- | --- |
| 后端应用日志 | `backend/storage/logs/contract_review.log` |
| 服务运行日志（devctl 启动的） | `deploy/runtime/logs/<service>.log` |
| 服务 PID 文件 | `deploy/runtime/pids/<service>.pid` |

```bash
# 查看服务日志
cd deploy/scripts
python devctl.py logs backend          # 末尾 50 行
python devctl.py logs backend -n 200   # 末尾 200 行
python devctl.py logs -f backend       # 持续跟踪
```
