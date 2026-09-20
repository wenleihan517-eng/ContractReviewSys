# D:/ContractReviewSys-合同审批智能审查项目/README.md
# 合同审批智能审查系统

> 基于 LangGraph 的多智能体合同审查系统。大模型仅承担辅助能力，所有正式业务判定由规则引擎裁决。

---

## 一、项目定位

面向企业合同审批场景，提供从合同上传、解析、条款结构化，到合规风险审查、智能建议生成、
审批回写、报告导出的全链路自动化能力。

### 1.1 核心业务权责红线（架构强制约束）

| 能力 | 承担方 | 说明 |
| --- | --- | --- |
| 条款语义解析 | 大模型（辅助） | 输出结构化条款，供后续处理 |
| 疑似风险识别 | 大模型（辅助） | 仅输出**候选项**，不定级 |
| 修改建议初稿 | 大模型（辅助） | 输出草稿，经规则引擎复核 |
| 合规条文解读 | 大模型（辅助） | 解释性内容，不参与判定 |
| **风险等级定级** | **规则引擎** | 大模型无权参与 |
| **违规判定** | **规则引擎** | 大模型无权参与 |
| **任务阻塞判定** | **规则引擎** | 大模型无权参与 |
| **最终审查结论** | **规则引擎** | 大模型无权参与 |

代码级保障：
- 大模型产出物统一标记 `hit_source = llm_assistant`，规则引擎产出标记 `hit_source = rule_engine`
- 审查结论记录 `decision_source = rule_engine` 与 `engine_version`，支持审计追溯
- 结论内容生成 `conclusion_hash`（SHA-256），防篡改校验
- 配置项 `RULE_ENGINE_ENABLED` 关闭时，规则引擎直接抛出异常，**拒绝未判定放行**

---

## 二、技术栈（固定不可变更）

### 后端
- Python 3.10
- FastAPI（Web 框架）
- LangGraph（工作流编排）

### 存储中间件
- MongoDB（业务主数据、审查记录、规则模板）
- MinIO（合同原文、OCR 图片、导出报告）
- Milvus（条款向量检索，复用现有实例 + 独立 database 隔离）

### 前端
- React 18
- TypeScript
- Vite
- Ant Design 5
- pdfjs-dist（PDF 在线预览）

### 模型服务（全部本地部署，禁止公网 API）
- LLM：本地部署，OpenAI 兼容协议，`base_url` 可配置
- Embedding：BGE-M3，输出 1024 维向量

---

## 三、数据隔离策略

### Milvus
复用现有实例 `192.168.10.130:7000`，但使用**独立 database** 做物理隔离：

| 层级 | 本项目取值 | 说明 |
| --- | --- | --- |
| database | `contract_review` | 独立库，**严禁使用 default 库** |
| collection | `cr_clauses` | 条款向量集合 |
| collection | `cr_rule_templates` | 规则模板向量集合 |

### MongoDB
数据库名 `contract_review`，集合统一使用 `cr_` 前缀。

### MinIO
| 存储桶 | 用途 |
| --- | --- |
| `contract-origin` | 合同原文 |
| `contract-ocr-image` | OCR 页面图片 |
| `contract-report` | 导出报告 |

---

## 四、业务边界

仅围绕以下范围开发，不做范围外功能：

1. 合同解析
2. OCR 识别
3. 条款结构化
4. 合规风险审查
5. 智能建议生成
6. 审批回写
7. 报告导出
8. 规则模板管理

---

## 五、目录结构

```
D:/ContractReviewSys-合同审批智能审查项目/
├── backend/                            # 后端服务
│   ├── app/
│   │   ├── main.py                     # FastAPI 入口（lifespan / 中间件 / 路由挂载）
│   │   ├── core/                       # 核心基础设施
│   │   │   ├── config.py               # 配置中心（全局唯一配置入口）
│   │   │   ├── logging_config.py       # 日志系统
│   │   │   ├── exceptions.py           # 全局异常与错误码
│   │   │   ├── response.py             # 统一响应封装
│   │   │   ├── middleware.py           # 请求链路追踪中间件
│   │   │   └── dependencies.py         # 依赖注入提供者
│   │   ├── constants/                  # 常量与业务枚举（权威定义处）
│   │   │   ├── __init__.py             # 集合名 / 条款类型 / 状态枚举
│   │   │   └── enums.py
│   │   ├── storage/                    # 存储客户端层（单例封装）
│   │   │   ├── mongo_client.py         # MongoDB 异步连接 + 索引初始化
│   │   │   ├── minio_client.py         # MinIO 对象存储管理
│   │   │   ├── milvus_client.py        # Milvus 向量库管理（独立库隔离）
│   │   │   └── __init__.py             # 统一生命周期入口
│   │   ├── engines/                    # 能力引擎层（纯计算，不碰数据库）
│   │   │   ├── rule_engine.py          # 规则引擎（所有正式判定的唯一裁决方）
│   │   │   ├── vector_engine.py        # 向量检索引擎
│   │   │   ├── llm_engine.py           # 大模型辅助引擎
│   │   │   ├── clause_recommend_engine.py  # 条款推荐引擎
│   │   │   ├── embedding_client.py     # BGE-M3 向量化客户端
│   │   │   └── llm_client.py           # LLM 客户端（OpenAI 兼容）
│   │   ├── graph/                      # LangGraph 编排层
│   │   │   ├── state.py                # 状态契约定义
│   │   │   ├── nodes.py                # 十个节点函数
│   │   │   ├── routes.py               # 条件分支路由
│   │   │   ├── builder.py              # 图构建与检查点
│   │   │   └── node_utils.py           # 节点公共工具
│   │   ├── models/                     # 数据存储模型（与 MongoDB 集合一一对应）
│   │   │   └── documents/
│   │   │       ├── contract.py
│   │   │       ├── clause.py
│   │   │       ├── review_task.py
│   │   │       ├── risk_hit.py
│   │   │       ├── review_conclusion.py
│   │   │       └── rule_template.py
│   │   ├── schemas/                    # 接口契约模型（面向前端）
│   │   ├── services/                   # 业务编排层
│   │   │   ├── parse_service.py        # 解析编排（含 OCR 降级链路）
│   │   │   ├── parsers/                # PDF / DOCX / OCR 解析器
│   │   │   ├── review_service.py       # 审查编排
│   │   │   ├── report_service.py       # 报告生成
│   │   │   ├── comment_service.py      # 评论生成与回写
│   │   │   ├── task_service.py         # 任务生命周期
│   │   │   └── metadata_extractor.py   # 合同元数据抽取
│   │   ├── repositories/               # 数据访问层
│   │   │   ├── task_repo.py            # 审查任务读写
│   │   │   ├── audit_repo.py           # 审计日志读写
│   │   │   └── rule_template_repo.py   # 规则模板读写
│   │   ├── api/v1/                     # 接口层
│   │   │   ├── router.py               # v1 路由聚合器
│   │   │   └── endpoints/              # health / parse / review / tasks
│   │   │                               #   / rules / report_comment / audit
│   │   └── utils/                      # 工具层
│   │       ├── approval_guard.py       # 审批白名单守卫（红线强制）
│   │       ├── file_hash.py            # 文件指纹
│   │       ├── id_generator.py         # ID 生成
│   │       ├── retry.py                # 重试装饰器
│   │       └── time_utils.py
│   ├── config/
│   │   └── risk_rules.yaml             # 风险规则配置（34 条规则）
│   ├── scripts/                        # 运维脚本
│   │   ├── init_mongodb.py             # MongoDB 初始化（库/集合/索引）
│   │   ├── init_milvus_vectors.py      # Milvus 建库建集合 + 规则向量化
│   │   ├── system_check.py             # 系统自检
│   │   └── diag_rule_patterns.py       # 规则正则体检（诊断工具）
│   ├── tests/                          # 验证脚本（7 套，402 项断言）
│   ├── requirements.txt                # 依赖清单
│   ├── .env.example                    # 环境变量模板（18 组配置）
│   └── .env                            # 环境变量（不入库）
├── frontend/                           # 前端工程
│   ├── src/
│   │   ├── api/                        # 接口封装
│   │   ├── components/                 # 通用组件
│   │   ├── constants/                  # 常量
│   │   ├── hooks/                      # 自定义 Hook
│   │   ├── layouts/                    # 布局
│   │   ├── pages/                      # 页面（大盘 / 工作台 / 规则模板）
│   │   ├── router/                     # 路由
│   │   ├── stores/                     # Zustand 状态管理（task / workbench / rule）
│   │   ├── styles/                     # 全局样式
│   │   ├── types/                      # 类型定义
│   │   └── utils/                      # 工具函数
│   ├── vite.config.ts                  # Vite 配置（含 /api 代理，仅绑 127.0.0.1）
│   ├── scripts/
│   │   └── run_smoke.mjs               # 冒烟测试执行器（Vite ssrLoadModule + JSDOM，33 项）
│   └── package.json
├── deploy/                             # 部署配置与脚本
│   ├── docker/
│   │   └── docker-compose.yml          # MongoDB / MinIO 编排
│   ├── mock_approval/                  # 模拟审批系统（含红线验证器）
│   │   ├── main.py
│   │   ├── README.md
│   │   ├── start_mock_approval.bat
│   │   └── start_mock_approval.sh
│   ├── scripts/                        # 运维脚本
│   │   ├── devctl.py                   # 进程管理（start/stop/status/logs/probe）
│   │   ├── init_all.py                 # 一键初始化（4 步编排）
│   │   ├── env_probe.sh                # 环境预检
│   │   ├── verify_mock_approval.sh     # 审批红线验证（13 项）
│   │   ├── start_all.bat / .sh
│   │   ├── stop_all.bat / .sh
│   │   └── init_all.bat / .sh
│   └── runtime/                        # 运行时数据（PID 与日志，不入库）
├── docs/
│   ├── PRD.md                          # 产品需求文档（角色权限 / 功能需求 / 验收标准）
│   ├── SPEC.md                         # 技术规格说明书（架构 / 数据模型 / 接口契约 / 错误码）
│   ├── UX_UI.md                        # 交互与视觉设计说明书（页面布局 / 交互流程 / 视觉规范）
│   ├── RUNBOOK.md                      # 可运行程序与运行说明（交付清单 / 跑通步骤 / 验证清单）
│   ├── prompt_guide.md                 # 常见问题修正话术（验收基线）
│   ├── DEPLOYMENT.md                   # 部署与启动指南
│   ├── DEMO_GUIDE.md                   # 端到端演示方案与验收手册
│   ├── demo_samples/                   # 演示样本（全部为虚构脱敏数据）
│   │   ├── README.md                   # 脱敏声明 + 安全边界 + 已验命中结果
│   │   ├── sample1_软件采购合同.txt
│   │   ├── sample2_大额销售合同.txt
│   │   └── gen_exception_samples.py    # 生成异常样本（加密/模糊/空白）
│   └── screenshots/                    # 界面实景截图（3 张）
├── storage/                            # 运行时数据（全部不入库）
│   ├── logs/                           # 后端日志（logging_config 解析到仓库根）
│   ├── tmp/                            # 临时文件
│   ├── mongo_data/                     # MongoDB 本地数据（docker 挂载）
│   ├── etcd_data/                      # etcd 本地数据（docker 挂载）
│   ├── milvus_data/                    # Milvus 本地数据（用于本地起 Milvus）
│   └── minio_data/                     # MinIO 本地数据
└── .gitignore
```

---

## 六、快速开始

> **完整运行说明见 [docs/RUNBOOK.md](docs/RUNBOOK.md)**（交付清单 / 四步跑通 / 验证清单 / 故障处置）。
> **完整部署说明见 [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)**，含各组件安装方式与排障。
> 以下为最精简的跑通路径。

### 6.1 前置条件

| 组件 | 版本要求 | 说明 |
| --- | --- | --- |
| Python | **3.10.x（固定）** | 3.11+ 依赖支持不完整 |
| Node.js | 18.x LTS 或更高 | 前端构建 |
| MongoDB | 7.0.x | 业务主数据 |
| MinIO | RELEASE.2024-10-13T13-34-11Z | 对象存储 |
| Milvus | 2.5.0 | 复用现有实例，独立 database 隔离 |
| Embedding | BGE-M3 / 1024 维 | 本地服务，OpenAI 兼容协议 |
| LLM | 本地部署 | 辅助能力，**不可用不阻塞核心审查** |

### 6.2 起基础设施

```bash
cd deploy/docker
docker compose up -d mongodb minio
```

Milvus 复用 `192.168.10.130:7000`，无需本地部署。

### 6.3 一键初始化

```bash
cd deploy/scripts
python init_all.py
```

依次完成：MongoDB 建库建集合建索引 → Milvus 建库建集合 → 规则向量化 → 全链路自检。

### 6.4 一键启动

```bash
cd deploy/scripts
python devctl.py start
```

或分别启动：

```bash
python devctl.py start backend    # 后端
python devctl.py start frontend   # 前端
python devctl.py start mock-approval   # 模拟审批系统（演示用）
```

### 6.5 访问

| 地址 | 说明 |
| --- | --- |
| http://127.0.0.1:5173 | 前端界面 |
| http://127.0.0.1:8000/docs | 后端接口文档 |
| http://127.0.0.1:9100/docs | 模拟审批系统 |

### 6.6 手工启动（不使用脚本）

```bash
# 后端
cd backend
py -3.10 -m venv .venv
.venv\Scripts\activate              # Windows
pip install -r requirements.txt
copy .env.example .env              # 按实际环境修改
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload

# 前端
cd frontend
npm install
npm run dev
```

### 6.7 停止与自检

```bash
cd deploy/scripts
python devctl.py status      # 查看状态
python devctl.py stop        # 停止全部
python devctl.py probe       # 探测基础设施

cd ../../backend
python scripts/system_check.py   # 系统自检（8 分节诊断）
```

> **端口冲突提醒**：实测发现 VMware 的 `vmnat.exe` 会占用 **8000** 端口。
> 若后端起不来且提示端口被占，先执行 `netstat -ano | findstr ":8000"` 确认占用者。

---

## 七、已有接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/health` | 存活探针，不触碰外部依赖 |
| GET | `/api/v1/ready` | 就绪探针，并发检查全部依赖连通性 |
| GET | `/api/v1/config-check` | 配置自检，敏感字段已脱敏 |

完整接口列表（含解析 / 审查 / 任务 / 规则 / 报告 / 审计）见
http://127.0.0.1:8000/docs

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

---

## 八、开发规范

1. **代码文件首行必须标注完整绝对文件路径**
2. **代码内部配备详细中文注释**，核心逻辑、参数、流程、返回值全覆盖
3. **禁止任何占位代码**：不允许出现 todo / 待实现 / 略 / 自行扩展等内容，交付代码核心逻辑完整可运行
4. **分步迭代开发**：单次只开发当前指定模块
5. **禁止硬编码**：连接串、密钥、端口、阈值一律从 `app/core/config.py` 读取
6. **遵守分层调用规则**：
   - `api` 层禁止直接调用 `engines` 与 `storage`，必须经过 `services`
   - `services` 层禁止直接操作 HTTP 请求/响应对象
   - `engines` 层禁止访问数据库，只做纯能力计算
   - `storage` 层禁止包含业务逻辑，只做数据存取

---

## 九、当前进度

已完成模块：

- [x] 目录骨架（工程初始化 + 分层结构 + 配置中心）
- [x] 合同解析与 OCR（PDF / DOCX 解析 + 扫描件 OCR 降级链路）
- [x] 条款结构化
- [x] 规则引擎（34 条风险规则，7 种匹配方式）
- [x] 合规风险审查（LangGraph 十节点工作流 + 差异化路由）
- [x] 向量检索与大模型辅助引擎
- [x] 智能建议生成与条款推荐
- [x] 报告生成与导出
- [x] 审批评论回写（含白名单守卫）
- [x] 后端 API 接口层（7 个 endpoint 模块）
- [x] 前端页面（审查大盘 / 智能审查工作台 / 合规规则与模板）
- [x] 部署配套（初始化脚本 / 进程管理 / 系统自检 / 模拟审批系统）
- [x] 端到端演示方案与验收手册（2 个业务场景 + 3 类异常场景 + 20 项验收对照）

