# D:/ContractReviewSys-合同审批智能审查项目/docs/RUNBOOK.md
# 合同审批智能审查系统 — 可运行程序与运行说明

> **文档版本**：v1.0
> **文档性质**：交付物清单 + 从零跑通的完整操作说明。
> **配套文档**：[PRD.md](PRD.md) 产品需求｜[SPEC.md](SPEC.md) 技术规格｜[UX_UI.md](UX_UI.md) 交互与视觉
> **详细部署指南**：[DEPLOYMENT.md](DEPLOYMENT.md)（含各组件安装细节与排障）
> **演示脚本**：[DEMO_GUIDE.md](DEMO_GUIDE.md)（两条业务场景 + 三类异常场景）

---

## 一、交付物清单

### 1.1 可运行程序

| 模块 | 目录 | 内容 |
|---|---|---|
| 后端服务 | `backend/` | FastAPI 应用，37 个接口端点，10 节点工作流 |
| 前端界面 | `frontend/` | React 单页应用，4 个页面 |
| 模拟审批系统 | `deploy/mock_approval/` | 演示用审批前台（含红线验证器） |
| 运维脚本 | `deploy/scripts/` | 进程管理、一键初始化、红线验证 |
| 容器编排 | `deploy/docker/` | MongoDB / MinIO 编排文件 |

### 1.2 文档

| 文档 | 用途 |
|---|---|
| `README.md` | 项目总览与快速开始 |
| `docs/PRD.md` | 产品需求文档 |
| `docs/SPEC.md` | 技术规格说明书 |
| `docs/UX_UI.md` | 交互与视觉设计说明书 |
| `docs/RUNBOOK.md` | **本文件**：运行说明 |
| `docs/DEPLOYMENT.md` | 部署与启动详细指南 |
| `docs/DEMO_GUIDE.md` | 端到端演示方案与验收手册 |
| `docs/prompt_guide.md` | 常见问题修正话术（验收基线） |
| `docs/demo_samples/` | 演示样本（虚构脱敏数据） |
| `docs/screenshots/` | 界面实景截图 |

### 1.3 配置与数据

| 项 | 位置 |
|---|---|
| 环境变量模板 | `backend/.env.example` |
| 环境变量（实际） | `backend/.env`（不入库） |
| 风险规则配置 | `backend/config/risk_rules.yaml`（34 条规则） |
| 后端测试套件 | `backend/tests/`（7 套） |
| 前端冒烟测试 | `frontend/scripts/run_smoke.mjs` |
| 运行时数据 | `storage/`（全部不入库） |

---

## 二、环境依赖

### 2.1 硬性要求

| 组件 | 版本 | 说明 |
|---|---|---|
| Python | **3.10.x（固定）** | 3.11+ 部分依赖支持不完整 |
| Node.js | 18.x LTS 或更高 | 前端构建 |
| MongoDB | 7.0.x | 业务主数据 |
| MinIO | RELEASE.2024-10-13T13-34-11Z | 对象存储 |
| Milvus | 2.5.0 | **复用现有实例** `192.168.10.130:7000` |
| Embedding 服务 | BGE-M3 / 1024 维 | 本地服务，OpenAI 兼容协议 |
| 大模型服务 | 本地部署 | 辅助能力，**不可用不阻塞核心审查** |

### 2.2 端口占用

| 端口 | 服务 |
|---|---|
| 5173 | 前端开发服务器 |
| 8000 | 后端 API |
| 9100 | 模拟审批系统 |
| 27017 | MongoDB |
| 9000 | MinIO |

> **端口冲突提醒（实测踩坑）**：本机 VMware 的 `vmnat.exe` 会占用 **8000** 端口。
> 表现为 `netstat` 显示 LISTENING 但 `curl` 全部超时——**"端口在监听"不等于"我们的服务在监听"**。
> 排查命令：`netstat -ano | findstr ":8000"` 找到 PID，再用 `tasklist | findstr <PID>` 确认占用进程名。

### 2.3 数据隔离要求

| 组件 | 隔离方式 | 约束 |
|---|---|---|
| MongoDB | 独立库 `contract_review`，集合 `cr_` 前缀 | — |
| Milvus | **独立 database** `contract_review` | **严禁使用 default 库** |
| MinIO | 独立桶（5 个） | — |

---

## 三、从零跑通（四步）

### 步骤 1：准备环境变量

```bash
cd backend
copy .env.example .env
```

然后按实际环境修改 `.env`。**必须确认的项**：

| 配置项 | 说明 |
|---|---|
| MongoDB 连接串 | 指向实际 MongoDB |
| MinIO 端点与密钥 | 指向实际 MinIO |
| Milvus 主机与端口 | `192.168.10.130` / `7000` |
| Milvus database | **必须为 `contract_review`** |
| 向量化服务地址 | BGE-M3 服务 |
| 大模型服务地址 | 本地 LLM（可留空，不影响核心审查） |
| JWT 密钥 | **生产环境必须替换**，不得使用模板默认值 |

> **配置自检**：`GET /api/v1/config-check` 可验证配置是否就绪，敏感字段已脱敏。

### 步骤 2：安装依赖

```bash
# 后端
cd backend
py -3.10 -m venv .venv
.venv\Scripts\activate                # Windows
# source .venv/bin/activate           # Linux / macOS
pip install -r requirements.txt

# 前端
cd ../frontend
npm install
```

> **依赖注意**：`requirements.txt` 中 **`bcrypt==4.0.1` 必须锁定**，不可升级。
> 更高的版本会与 passlib 1.7.4 不兼容，导致密码哈希直接失败。

### 步骤 3：初始化

```bash
cd deploy/scripts
python init_all.py
```

**执行内容（四步编排）**：

| 顺序 | 步骤 | 内容 |
|---|---|---|
| 1 | MongoDB 初始化 | 建库、建集合、建索引 |
| 2 | Milvus 初始化 | 建 database、建集合 |
| 3 | 规则向量化 | 将规则模板写入向量库 |
| 4 | 全链路自检 | 验证各组件连通性 |

**可选参数**：

| 参数 | 说明 |
|---|---|
| `--dry-run` | 只打印计划，不执行 |
| `--only <步骤>` | 只执行指定步骤 |
| `--skip <步骤>` | 跳过指定步骤 |
| `--recreate` | 重建（**会清空数据**，非交互环境自动取消） |

### 步骤 4：启动

```bash
cd deploy/scripts
python devctl.py start
```

**启动顺序（按依赖关系）**：模拟审批 → 后端 → 前端。

**或分别启动**：

```bash
python devctl.py start mock-approval   # 模拟审批系统
python devctl.py start backend         # 后端
python devctl.py start frontend        # 前端
```

**验证状态**：

```bash
python devctl.py status
```

状态含义：

| 状态 | 含义 |
|---|---|
| `[RUN]` | 服务正在运行 |
| `[WAIT]` | 等待启动完成 |
| `[BUSY]` | **端口被其他程序占用**（需排查） |
| `[STOP]` | 未运行 |

### 访问地址

| 地址 | 说明 |
|---|---|
| http://127.0.0.1:5173 | 前端界面 |
| http://127.0.0.1:8000/docs | 后端接口文档 |
| http://127.0.0.1:9100/docs | 模拟审批系统 |

---

## 四、内置测试账号

系统**无自助注册功能**，仅内置三个演示账号。

| 账号 | 密码 | 角色 | 登录后落点 | 显示名 |
|---|---|---|---|---|
| `user_business` | `123456` | 业务经办人 | 合同审查大盘 | 张经办（业务经办人） |
| `user_law` | `123456` | 法务审查人 | 智能审查工作台 | 李法务（法务审查人） |
| `user_admin` | `123456` | 系统管理员 | 合规规则与模板 | 王管理（系统管理员） |

> **安全提醒**：这三个账号密码为演示用途。**生产环境部署前必须删除内置账号常量并修改全部密码**。

---

## 五、验证清单

> 按顺序执行，作为交付验收的标准动作。

### 5.1 后端测试（7 套）

```bash
cd backend
.venv\Scripts\activate
python tests/verify_rule_engine.py        # 规则引擎
python tests/test_graph_smoke.py          # 图流程冒烟
python tests/verify_review_flow.py        # 审查流程
python tests/verify_report_comment.py     # 报告与评论
python tests/verify_api.py                # 接口
python tests/verify_parse_service.py      # 解析服务
python tests/verify_parse_node_e2e.py     # 解析节点端到端
```

**预期结果**：全部通过（合计 402+ 项断言）。

### 5.2 鉴权端到端

```bash
cd backend
python scripts/test_auth_e2e.py
```

**预期结果**：**132/132 通过**。

覆盖：登录 / 刷新 / 登出 / 用户信息 / 权限校验 / 权限矩阵 /
密码修改 / 账号枚举防护 / 时序攻击防护 / 路径穿越防护 /
令牌类型隔离 / 水平越权防护。

### 5.3 前端验证

```bash
cd frontend

# 类型检查（严格模式全开）
npx tsc --noEmit
# 预期：0 error

# 生产构建
npx vite build
# 预期：构建成功

# 冒烟测试（真实渲染）
node scripts/run_smoke.mjs
# 预期：45/45 通过
```

**冒烟测试覆盖分组**：

| 分组 | 内容 |
|---|---|
| 展示层 | 文案、枚举、状态映射 |
| 权责红线 | 源码级核查：无审批动作调用、无定级字段写入 |
| 组件层 | 真实渲染六个场景 |
| 状态层 | Store 逻辑验证 |
| 接口层 | 契约与边界 |
| 鉴权层 | 登录态、角色映射、越权拦截 |

### 5.4 系统自检（8 分节）

```bash
cd backend
python scripts/system_check.py
```

**输出**：8 个分节的诊断报告，含配置检查、依赖连通性、规则配置校验等。

### 5.5 审批红线验证

```bash
cd deploy/scripts
bash verify_mock_approval.sh
```

**预期结果**：**13/13 通过**。

验证内容：评论白名单接口放行；8 个审批动作接口全部拒绝；
越权尝试留痕。

> **注意**：本机若配置了 HTTP 代理，可能拦截 `127.0.0.1` 请求并返回 502，
> 导致误判为"接口契约不匹配"。此时需设置 `NO_PROXY=127.0.0.1,localhost`。

---

## 六、端到端演示

### 6.1 快速演示（两个业务场景）

| 场景 | 样本文件 | 金额 | 预期路由 | 预期结论 |
|---|---|---|---|---|
| 软件采购合同 | `docs/demo_samples/sample1_软件采购合同.txt` | 86 万 | 标准审查 | 严重风险，阻塞 |
| 大额销售合同 | `docs/demo_samples/sample2_大额销售合同.txt` | 1200 万 | **深度复核** | 高风险，阻塞 |

**预期命中**（已实跑验证，结果确定）：

| 场景 | 命中规则 | 等级分布 |
|---|---|---|
| 场景 1 | 无验收即全款支付、缺交付验收、缺质量保证、赔偿不对等、知识产权责任转嫁 | 严重 3 / 高 2 |
| 场景 2 | 赔偿不对等、大额提示 | 高 1 / 低 1 |

### 6.2 异常场景（三类法定阻断）

生成异常样本：

```bash
cd docs/demo_samples
python gen_exception_samples.py
```

| 样本 | 预期阻塞码 |
|---|---|
| 加密合同 | `DOC_ENCRYPTED` |
| 模糊扫描件 | `SCAN_TOO_BLURRY` |
| 空白合同 | `EMPTY_CONTENT` |

### 6.3 阻塞恢复演示

| 操作 | 接口 | 效果 |
|---|---|---|
| 重跑 | `POST /tasks/{task_no}/resolve` 带 `{"action":"retry"}` | 回到待执行，清阻塞标记 |
| 驳回 | 同上带 `{"action":"dismiss"}` | 置为失败 |

### 6.4 权限演示

| 演示动作 | 预期结果 |
|---|---|
| 用经办人账号登录 | 落点为大盘；导航只有大盘一项；访问 `/rules` 被弹回 |
| 手法务账号登录 | 落点为工作台；导航两项；访问 `/rules` 被弹回 |
| 用管理员账号登录 | 落点为规则页；导航三项全可见 |
| 手输无权限页面地址 | 被拦截并提示，跳回该角色首页 |

---

## 七、常用运维命令

```bash
cd deploy/scripts

# 进程管理
python devctl.py probe                  # 基础设施连通性探测
python devctl.py status                 # 服务状态（区分 RUN/WAIT/BUSY/STOP）
python devctl.py start                  # 启动全部
python devctl.py start backend          # 启动单个服务
python devctl.py restart backend        # 重启单个服务
python devctl.py logs backend           # 查看日志（默认尾 100 行）
python devctl.py stop                   # 停止全部

# 初始化
python init_all.py --dry-run            # 查看计划
python init_all.py --only milvus        # 只执行指定步骤
python init_all.py --recreate           # 重建（清空数据）

# 一键封装
start_all.bat / start_all.sh
stop_all.bat  / stop_all.sh
init_all.bat  / init_all.sh
```

---

## 八、常见故障处置

### 8.1 启动类

| 现象 | 首查项 | 处置 |
|---|---|---|
| 后端起不来，提示端口占用 | `netstat -ano \| findstr ":8000"` | 确认占用进程；若是无关程序（如 VMware），换端口或关闭该服务 |
| `devctl.py status` 显示 `[BUSY]` | 该端口被其他程序占用 | 按提示的进程名判断，不要误以为服务已启动 |
| 后端启动即退出 | 日志中是否有存储初始化异常 | 依赖未就绪时 `lifespan` 会直接抛异常，进程不监听端口 |
| 停止服务后端口仍被占用 | PID 可能记录的是启动器而非真实监听进程 | 使用 `devctl.py stop`，它会按端口反查真实进程 |

### 8.2 依赖类

| 现象 | 原因 | 处置 |
|---|---|---|
| 报 MongoDB/MinIO/Milvus 连接失败 | 对应服务未启动或地址配置错误 | `devctl.py probe` 探测，逐项修复 |
| Milvus 连上了但报集合不存在 | 未执行初始化 | `python init_all.py --only milvus` |
| 向量维度报错 | Embedding 服务输出维度不是 1024 | 检查 Embedding 服务配置 |
| 大模型报错 | 本地 LLM 未启动 | **不阻塞核心审查**，规则引擎仍可完成定级与结论 |

### 8.3 解析类

| 现象 | 阻塞码 | 说明 |
|---|---|---|
| 合同加密无法解析 | `DOC_ENCRYPTED` | 需上传未加密文件 |
| 扫描件识别不出文字 | `SCAN_TOO_BLURRY` | OCR 置信度低于 0.6，需重新扫描 |
| 正文内容为空 | `EMPTY_CONTENT` | 文本少于 50 字符 |
| 解析失败 | `PARSE_FAILED` | 文件格式问题 |
| 多次重试仍失败 | `RETRY_EXHAUSTED` | 需管理员人工处理 |

**所有阻塞都有明确原因码，不会出现"未知原因阻塞"或无声卡死。**

### 8.4 登录类

| 现象 | 错误码 | 处置 |
|---|---|---|
| 账号密码错误 | 60001 | 核对账号密码 |
| 未携带凭证 | 60002 | 重新登录 |
| 凭证非法 | 60003 | 清空浏览器本地存储后重新登录 |
| 凭证过期 | 60004 | 重新登录 |
| 令牌类型不匹配 | 60005 | 重新登录（刷新令牌不可当访问令牌用） |
| 刷新令牌失效 | 60006 | 重新登录 |
| 账号已停用 | 60007 | 联系管理员 |
| 账号已锁定 | 60008 | 等待 30 分钟或联系管理员 |
| 无权限访问 | 60009 | 当前角色无该接口权限（正常拦截） |

> **注意**：`60003` 非法与 `60004` 过期必须区分。若日志里过期被报成非法，
> 说明令牌解码时未先关闭过期校验再手工比对时间。

### 8.5 回写类

| 现象 | 原因 | 处置 |
|---|---|---|
| 回写返回 403 | 调用了审批动作类接口 | 这是**红线拦截的正常行为**；正确入口是评论写回 |
| 回写返回 502/503/504 | 请求未到达审批系统 | 检查审批系统地址与 `NO_PROXY` 配置 |
| 回写失败 | 审批系统异常 | 界面显示失败状态，可重试 |

### 8.6 前端类

| 现象 | 原因 | 处置 |
|---|---|---|
| 页面打开显示连接失败 | 后端未启动 | 先启动后端 |
| 刷新页面后跳登录页 | 凭证过期或被清除 | 正常行为，重新登录 |
| 界面数据都是 `—` | 后端对应字段暂无数据 | **不是 bug**：系统禁止编造展示数据，缺失字段显示占位符 |
| 构建报 antd 深层类型错误 | 类型解析路径未指向真实声明文件 | 检查 `tsconfig.app.json` 的 `paths` 配置 |
| PDF 预览空白 | Worker 未配置 | 检查 `GlobalWorkerOptions.workerSrc` 是否已设置 |

---

## 九、运行时数据说明

| 目录 | 内容 | 是否入库 |
|---|---|---|
| `storage/logs/` | 后端日志 | 否 |
| `storage/tmp/` | 临时文件 | 否 |
| `storage/mongo_data/` | MongoDB 本地数据 | 否 |
| `storage/etcd_data/` | etcd 本地数据 | 否 |
| `storage/milvus_data/` | Milvus 本地数据 | 否 |
| `storage/minio_data/` | MinIO 本地数据 | 否 |
| `deploy/runtime/` | PID 文件与运行日志 | 否 |

> **日志位置提示**：日志真实位置是 `<仓库根>/storage/logs/contract_review.log`，
> **不是** `backend/storage/logs/`。日志目录通过从 `backend/app/core/` 上溯三级解析，
> 落到的是仓库根目录。

---

## 十、交付验收步骤（清单式）

> 按此顺序执行，全部通过即视为交付验收完成。

| # | 步骤 | 命令 | 通过判据 |
|---|---|---|---|
| 1 | 环境变量就绪 | 检查 `backend/.env` | 密钥已替换、地址正确 |
| 2 | 依赖安装 | `pip install -r requirements.txt` | 无报错，bcrypt 版本为 4.0.1 |
| 3 | 基础设施探测 | `python devctl.py probe` | 4 项基础设施可达 |
| 4 | 初始化 | `python init_all.py` | 四步全部成功 |
| 5 | 启动服务 | `python devctl.py start` | 三个服务均为 `[RUN]` |
| 6 | 系统自检 | `python scripts/system_check.py` | 无 FAIL |
| 7 | 后端测试 | 7 套测试脚本 | 全部通过 |
| 8 | 鉴权测试 | `python scripts/test_auth_e2e.py` | 132/132 |
| 9 | 前端类型检查 | `npx tsc --noEmit` | 0 error |
| 10 | 前端构建 | `npx vite build` | 成功 |
| 11 | 前端冒烟 | `node scripts/run_smoke.mjs` | 45/45 |
| 12 | 审批红线 | `bash verify_mock_approval.sh` | 13/13 |
| 13 | 浏览器访问 | http://127.0.0.1:5173 | 页面正常加载 |
| 14 | 三角色登录 | 三个内置账号 | 各自落到正确首页 |
| 15 | 越权验证 | 手输受限地址 | 被拦截并跳回 |
| 16 | 业务场景演示 | 两份样本合同 | 命中结果与预期一致 |
| 17 | 异常场景演示 | 三类异常样本 | 阻塞码正确 |
| 18 | 阻塞恢复 | 重跑 / 驳回 | 状态正确流转 |

---

## 十一、停止与清理

```bash
cd deploy/scripts
python devctl.py stop        # 停止全部服务
python devctl.py status      # 确认全部 [STOP]
```

**清理运行时数据**（谨慎操作，会丢失数据）：

```bash
python init_all.py --recreate    # 重建数据（交互确认）
```

> **提示**：`--recreate` 是破坏性操作。在非交互环境（如 CI、重定向输入）
> 下脚本会**自动按取消处理**，不会静默执行清空。
