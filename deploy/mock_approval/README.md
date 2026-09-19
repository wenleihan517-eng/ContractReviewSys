# 模拟企业审批系统

> 合同审批智能审查系统的对接靶机。**仅用于本地演示与联调，不可用于生产。**

---

## 一、为什么需要它

审查系统的最后一步是「审批回写」——把规则引擎产出的审查意见写进企业审批系统的评论区。

真实审批系统（泛微、致远、钉钉审批等）有三个问题让本地联调变得困难：

1. 需要企业账号和权限，开发机通常拿不到
2. 接口字段与返回结构不透明，出问题无法定位
3. 无法构造"接口返回异常"的场景来验证容错逻辑

本模块提供行为等价的模拟服务，让整条链路（生成意见 → 白名单校验 → HTTP 提交 → 结果记录）可以完整跑通。

---

## 二、它同时是一个红线验证器

这是本模块最重要的设计。它不只是"能收请求"，而是**主动演示红线不可逾越**：

| 接口族 | 行为 | 目的 |
|---|---|---|
| `/comments`、`/comments/batch` | 正常接受 | 白名单能力，唯一被授权的接口 |
| `/approve`、`/reject`、`/transfer` 等 8 个 | **强制返回 403** | 即便带了正确鉴权头也拒绝 |
| `/audit` | 列出所有被拒的越权尝试 | 提供可执行的合规证据 |

**关键点：给审查系统更高的权限，也不应该让它能替人审批。**

白名单是"能力边界"，不是"权限问题"。所以模拟服务在收到动作类调用时，不会因为鉴权有效就放行，而是一律拒绝并留痕。

于是有一个可以直接展示的验证结论：

```bash
# 查审计记录
curl http://127.0.0.1:9100/audit
```

若返回 `"total": 0`，即证明**审查系统在整个运行过程中从未试图改变审批结论**。

这比在文档里写一句"我们不调用审批接口"有说服力得多——它是可执行的。

---

## 三、启动

```bash
cd deploy/mock_approval

# 方式一：直接运行
python main.py

# 方式二：uvicorn（推荐，便于调整参数）
python -m uvicorn main:app --host 127.0.0.1 --port 9100
```

或使用配套启动脚本（自带解释器探测与提示信息）：

```bash
# Windows：双击运行，或命令行执行
deploy\mock_approval\start_mock_approval.bat

# Linux / macOS / Git Bash
bash deploy/mock_approval/start_mock_approval.sh
```

或使用统一进程管理器（与后端、前端共用一套 PID 与日志管理）：

```bash
cd deploy/scripts
python devctl.py start mock-approval    # 后台启动
python devctl.py logs mock-approval     # 查看日志
python devctl.py stop mock-approval     # 停止
```

启动后：

- 服务地址：http://127.0.0.1:9100
- Swagger 文档：http://127.0.0.1:9100/docs

**依赖**：仅需 `fastapi` + `uvicorn`。刻意不引入本项目其它依赖，保证可以独立部署到任意机器模拟对接。

---

## 四、接口清单

### 4.1 白名单接口（允许调用）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/comments` | 写入单条评论 |
| POST | `/comments/batch` | 批量写入评论 |
| GET | `/comments` | 查询已接收的评论（支持 `?task_no=` 过滤） |

### 4.2 动作类接口（一律 403）

| 方法 | 路径 | 对应动作 |
|---|---|---|
| POST | `/approve` | 同意 |
| POST | `/reject` | 拒绝 |
| POST | `/transfer` | 转交 |
| POST | `/withdraw` | 撤回 |
| POST | `/reassign` | 转办 |
| POST | `/sign` | 签署 |
| POST | `/complete` | 完结 |
| POST | `/terminate` | 终止 |

### 4.3 辅助接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/approval/{task_no}` | 查询模拟审批单（不存在时自动创建） |
| GET | `/approvals` | 列出全部模拟审批单 |
| GET | `/audit` | 查看越权尝试审计记录 |
| GET | `/health` | 健康检查 |
| GET | `/` | 服务说明 |
| GET | `/docs` | Swagger 文档 |

---

## 五、与审查系统的对接配置

在 `backend/.env` 中确认以下配置（默认值即为本模拟服务的地址）：

```ini
APPROVAL_BASE_URL=http://127.0.0.1:9100
APPROVAL_COMMENT_API_PATH=/comments
APPROVAL_API_KEY=your-approval-api-key
APPROVAL_TIMEOUT_SECONDS=30.0
APPROVAL_MAX_RETRIES=3
APPROVAL_CONNECT_TIMEOUT_SECONDS=10.0
```

审查系统会以如下形式发起请求：

```http
POST http://127.0.0.1:9100/comments
Authorization: Bearer your-approval-api-key
Content-Type: application/json

{
  "contract_review_comment": "【合同智能审查意见】\n任务编号：...\n综合风险等级：高风险\n...",
  "task_no": "TASK-20260919-001",
  "final_risk_level": "high",
  "risk_count": 3
}
```

注意请求体只有 4 个字段，**不含任何审批动作字段**。审查系统在提交前会主动剔除
`action`/`approve`/`reject`/`result`/`decision` 等键（见 `comment_service.py` 的 `forbidden_body_keys`），
这是第三道防线。

---

## 六、验证流程

### 6.1 确认服务正常

```bash
curl http://127.0.0.1:9100/health
```

预期返回 `"status": "alive"`。

### 6.2 走通回写链路

1. 启动模拟审批系统（9100）
2. 启动后端（8000），确认 `.env` 中 `APPROVAL_BASE_URL` 指向 9100
3. 打开前端工作台（5173），进入某个任务的审查页面
4. 点击底部「一键回写审批单」
5. 查询评论是否送达：

```bash
curl http://127.0.0.1:9100/comments
```

预期能看到刚提交的评论，含 `comment_id` 与 `comment_length`。

### 6.3 验证红线未越界

```bash
curl http://127.0.0.1:9100/audit
```

预期返回：

```json
{
  "code": 0,
  "data": {
    "verdict": "正常：未发现任何越权调用。审查系统严格遵循接口白名单，只写入了评论区。",
    "total": 0,
    "items": []
  }
}
```

**`total: 0` 就是红线守住的证据。**

### 6.4 手工验证拦截确实生效

```bash
# 携带正确鉴权头，依然应被拒绝
curl -i -X POST http://127.0.0.1:9100/approve \
  -H "Authorization: Bearer your-approval-api-key" \
  -H "Content-Type: application/json" \
  -d '{"task_no":"TEST","result":"agree"}'
```

预期 `HTTP/1.1 403 Forbidden`，返回体含：

```json
{
  "code": 40300,
  "detail": {
    "reason": "接口白名单机制（默认拒绝原则）",
    "note": "本次尝试已记入 /audit，可用于合规审计"
  }
}
```

### 6.5 一次性验证全部 8 个动作接口

```bash
bash deploy/scripts/verify_mock_approval.sh
```

---

## 七、设计说明

### 7.1 数据只存内存

所有数据存在进程内存，重启即清空。

这是**刻意的**：模拟服务不应该成为需要维护的组件。如果它有自己的数据库，
就会产生"模拟数据要清理""状态不一致"之类的运维负担，
而它的价值只在于演示期间。

### 7.2 鉴权刻意宽松

`_check_auth()` 只校验 `Authorization: Bearer <非空令牌>`，不比对具体值。

原因：本地演示时用户常常没改 `.env` 里的 `APPROVAL_API_KEY`，
因为一个密钥不匹配就让整个演示卡住，体验很差。
生产对接时应改为严格比对。

### 7.3 审批单自动创建

`GET /approval/{task_no}` 在审批单不存在时自动创建一条 `pending` 记录。

这样演示时不需要先手工造数据，直接跑审查流程就能拿到审批单。
自动创建的记录带 `"auto_created": true` 标记，便于区分。

### 7.4 评论正文原样保存

模拟服务不解析评论正文的格式，只记录长度与原样文本。

原因：评论格式由审查系统负责（见 `approval_guard.build_comment_payload`），
模拟服务不该对它做假设。真实审批系统对接时，
字段名可能不同，在 `CommentRequest` 里做适配即可，
不需要改动审查系统侧代码。

---

## 八、常见问题

**Q: 启动报 `Address already in use`**

9100 端口被占用。查看占用进程或换端口：

```bash
# Windows
netstat -ano | findstr :9100
# 换端口（同时改 backend/.env 的 APPROVAL_BASE_URL）
python -m uvicorn main:app --port 9200
```

**Q: 审查系统回写报「连接被拒绝」**

模拟审批系统没启动，或 `APPROVAL_BASE_URL` 配置的端口不对。先 `curl /health` 确认。

**Q: 回写报 `HTTP 403`**

说明审查系统试图调用动作类接口，或 `APPROVAL_COMMENT_API_PATH` 配错了。
正常情况下该值必须是 `/comments`。这是**严重问题**，
请检查 `backend/app/utils/approval_guard.py` 是否被绕过。

**Q: `/audit` 有记录但审查系统日志没报错**

说明某处绕过了 `approval_guard` 直接发起了 HTTP 请求。
检查是否有代码直接使用 `httpx` 调审批系统而未先调用 `ensure_comment_only()`。

**Q: 回写成功但 `/comments` 查不到**

检查 `task_no` 是否一致。评论按 `task_no` 记录，
传空 `task_no` 时记录为 `UNKNOWN`。
