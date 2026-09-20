# 合同审批智能审查系统 — 前端工程

`D:/ContractReviewSys-合同审批智能审查项目/frontend`

React 18 + TypeScript + Vite 6 + Ant Design 5 + Zustand 5 + pdfjs-dist 4

---

## 一、目录结构

```
frontend/
├── index.html                      HTML 挂载点（#root）
├── package.json                    依赖与脚本
├── tsconfig.json                   TS 工程引用入口
├── tsconfig.app.json               应用源码编译配置（strict + noUnusedLocals）
├── tsconfig.node.json              构建脚本编译配置
├── vite.config.ts                  构建配置（别名 @ / 代理 / 端口）
└── src/
    ├── main.tsx                    应用入口（Provider 注入：主题、路由、中文）
    ├── router/index.tsx            路由表（3 页面 + 懒加载）
    ├── layouts/
    │   └── MainLayout.tsx          主布局（侧边导航 + 顶栏 + 内容出口）
    ├── constants/index.ts          ★ 枚举与标签的权威来源（对齐后端）
    ├── types/index.ts              ★ 接口契约类型（对齐后端 schemas）
    ├── api/
    │   ├── client.ts               axios 实例 + 统一响应解包 + 错误码表
    │   └── endpoints.ts            强类型接口封装（6 组 API）
    ├── stores/
    │   ├── taskStore.ts            大盘状态（列表聚合 / 筛选 / 轮询）
    │   ├── workbenchStore.ts       工作台状态（双栏联动 / 回写）
    │   └── ruleStore.ts            规则状态（树构建 / 增删改 / 条款库）
    ├── utils/index.ts              纯函数工具（格式化 / 区间 / 高亮切分）
    ├── components/
    │   ├── DocumentViewer.tsx      左栏：文档视窗（PDF + 高亮 + 定位）
    │   ├── RiskCard.tsx            右栏：风险卡片
    │   ├── ReviewFooterPanel.tsx   底栏：协同回写区
    │   └── UploadContractModal.tsx 上传送审弹窗
    └── pages/
        ├── DashboardPage.tsx       页面一：合同审查大盘
        ├── WorkbenchPage.tsx       页面二：智能审查工作台（核心）
        └── RulesPage.tsx           页面三：合规规则与模板
```

---

## 二、启动方式

```bash
cd "D:/ContractReviewSys-合同审批智能审查项目/frontend"

# 安装依赖（使用托管 Node 运行时）
node "C:/Users/Lenovo/.workbuddy/binaries/node/versions/22.22.2-2/node_modules/npm/bin/npm-cli.js" install

# 类型检查（严格模式，零错误才算通过）
node node_modules/typescript/bin/tsc --noEmit -p tsconfig.app.json

# 生产构建
node node_modules/vite/bin/vite.js build

# 开发调试（默认 http://127.0.0.1:5173）
node node_modules/vite/bin/vite.js
```

后端地址通过 `vite.config.ts` 的 `server.proxy` 转发到 `http://127.0.0.1:8000`，
前端一律以 `/api/v1/...` 相对路径发起请求，开发期无跨域问题。

---

## 三、三个核心页面

### 页面一：合同审查大盘 `/dashboard`

| 能力 | 实现要点 |
|---|---|
| 待审合同列表 | 合同名称、申请人、业务类型、合同金额、审查状态、综合风险等级、创建时间 |
| 多条件筛选 | 任务状态 / 风险等级 / 合同类型 / 关键字 + 5 档快捷筛选 |
| 手动上传入口 | 三步串行：上传文件 → 创建任务 → 触发解析，任一步失败明确提示 |
| 批量操作 | 仅对失败/阻塞/已取消任务开放勾选，批量重跑串行执行 |
| 阻塞任务重试 | 展示阻塞原因码，二次确认后从阻塞点续跑 |
| 长任务进度条 | 行内进度条 + 3 秒轮询，全部结束自动停止 |

**列表聚合机制**：后端 `/tasks` 列表接口只返回任务基础字段。store 在拿到列表后
用 `Promise.allSettled` 并发拉取每个任务的详情补齐字段，单条失败不影响整页。

### 页面二：智能审查工作台 `/workbench/:taskNo`

三段式布局：

**左栏 · 文档视窗**
- PDF 分页渲染（pdfjs-dist，Worker 通过 Vite `?url` 导入，避免打包后路径失效）
- 文本模式兜底：Word / 扫描件走解析结果渲染
- 风险条款高亮：按**全文字符区间**着色，颜色对应风险等级
- 页码跳转 + 页内段落级定位（`data-risk-id` 精确定位，不用文本匹配）
- 点击高亮文本反选右栏卡片

**右栏 · 审查结果与建议区**
- 风险卡片按 严重 → 高 → 中 → 低 排序
- 卡片含：风险描述、法律依据、原文片段、AI 推荐修改条款、命中来源明细
- 支持一键复制完整建议（风险点 + 依据 + 条款三要素）
- 支持人工编辑建议（仅建议类字段）
- 等级筛选：全部 / 严重 / 高 / 中 / 低（带数量）

**底栏 · 协同回写区**
- 综合风险摘要（等级 + 分布 + 口径说明）
- 审批关注要点（从高风险项提炼，可直接引用）
- 法务人工批注输入框
- 审查意见预览（含后端格式校验结果）→ 演练 / 正式回写
- 报告生成与 Markdown 预览抽屉 + 下载

### 页面三：合规规则与模板 `/rules`

- 规则资产统计（总数 / 启用 / 禁用 / 致命级 / 重大级）
- 树状列表：按 通用 → 采购 → 销售 → 服务 → 劳动 分组
- 跨类型规则明确标注（一条规则归属多个类型，改动即全类型生效）
- 规则详情：严重度、定级结果、匹配方式、优先级、匹配条件 JSON、法律依据、建议模板
- 新增/编辑规则：编码正则校验、匹配方式切换时自动重置配置示例、JSON 格式校验
- 启用/禁用：二次确认并说明影响范围
- 标准示范条款库：按条款类型筛选、新增、一键复制条款正文

---

## 四、关键设计决策

### 1. 权责红线在前端的落地

**前端不提供任何风险定级的修改入口。** 风险等级、判定结论、规则严重度
一律只读展示。可编辑的只有 `suggestion_text` 与 `legal_basis` 两个建议类字段，
与后端 `RiskItem` 模型的字段级约束一一对应。

**底栏回写只写评论。** 界面中不存在"审批通过""驳回""转办"等任何动作按钮——
这不是遗漏，而是刻意约束。后端 `approval_guard` 采用默认拒绝原则
（白名单仅 `/comments`、`/comments/batch`），前端主动不提供入口，形成双重防线。

### 2. 左右栏联动为什么用字符区间而非文本匹配

风险项自带 `char_start` / `char_end`（与 `text_blocks` 同一坐标系）。
高亮切分函数 `splitTextByRisks` 把原始文本按这些区间切成片段，
保证**拼接后与原文完全一致**，且同一段文字在合同中多次出现时
只有真正命中的那一处被点亮。

若改用"在全文里搜索证据文本"，同文异处会全部点亮，定位必然跳错。

### 3. 无数据源的字段不编造

实测确认后端 `ReviewTask` 模型未持久化 申请人 / 业务类型 / 合同金额 / 相对方，
读侧无数据源。前端统一置为 `'—'` 与 `0`，界面按占位符渲染。

在合规场景中展示一个虚假的金额比展示空值危险得多——使用者可能据此
判断是否达到 500 万深度复核门槛。待后端补齐后只需改 `mergeRow` 一处。

### 4. 规则的写操作不做乐观更新

规则的启用/禁用会直接改变审查判定结果。若前端先行更新而请求失败，
法务会看到"已禁用"的假状态并据此判断。因此所有写操作都等待后端确认。

唯一的例外是启用状态切换：成功后本地同步更新该条规则并重建树，
避免整表重载导致树展开状态与滚动位置丢失。

---

## 五、与后端契约的对齐

| 前端文件 | 对齐的后端来源 |
|---|---|
| `constants/index.ts` | `app/constants/__init__.py`（RiskLevel / ReviewTaskStatus / RuleSeverity / ClauseType / RuleMatchType / 阻塞原因码 / WriteBackState） |
| `types/index.ts` | `app/schemas/*.py`（字段名逐字一致，不做驼峰转换） |
| `api/client.ts` | 统一响应封装 `{code, message, data, request_id}` 与 ErrorCode 错误码表 |
| `api/endpoints.ts` | `app/api/v1/endpoints/*.py`（6 组接口全量覆盖） |

api 前缀：`/api/v1`。前端 `client.ts` 的 `baseURL` 已包含此前缀，
`endpoints.ts` 中只写业务段路径。

---

## 六、严格模式约束

`tsconfig.app.json` 开启了全部严格检查：

```json
"strict": true,
"noUnusedLocals": true,
"noUnusedParameters": true,
"noFallthroughCasesInSwitch": true
```

这意味着**任何未使用的导入或变量都会导致构建失败**。
新增代码时若临时保留了未使用的符号，必须先清理再提交。
