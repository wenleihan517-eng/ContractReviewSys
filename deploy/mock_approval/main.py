# D:/ContractReviewSys-合同审批智能审查项目/deploy/mock_approval/main.py
"""
模拟企业审批系统 — 仅用于本地演示与联调

【这个模块是什么】
    本系统的 write_comment 节点需要把审查意见写回企业审批系统的评论区。
    真实审批系统（如泛微、致远、钉钉审批）通常不便于本地联调，
    因此提供一个行为等价的模拟服务，让整条链路可以端到端跑通。

【核心设计：它同时是一个"红线验证器"】
    本模块不只是"能收请求"，更重要的是**主动演示红线不可逾越**：
        1. /comments、/comments/batch  → 正常接受（白名单）
        2. /approve、/reject、/transfer 等 → 强制返回 403，
           即便请求带了正确的鉴权头也一样拒绝
        3. 提供 /audit 接口，列出所有被拒的动作类请求，
           用于证明"系统从未试图改变审批结论"

    这个设计的意义：合规审查系统的价值，一半在于"能发现问题",
    另一半在于"不越权替人做决定"。让模拟服务主动拦截并留痕，
    比在文档里写一句"我们不调用审批接口"有说服力得多——
    它是可执行的证据。

【接口清单】
    POST /comments                    写入单条评论（白名单）
    POST /comments/batch              批量写入评论（白名单）
    GET  /comments                    查询已收到的评论
    GET  /approval/{task_no}          查询审批单信息（模拟）
    POST /approve                     动作类接口 → 403（红线拦截）
    POST /reject                      动作类接口 → 403（红线拦截）
    POST /transfer                    动作类接口 → 403（红线拦截）
    POST /withdraw                    动作类接口 → 403（红线拦截）
    POST /reassign                    动作类接口 → 403（红线拦截）
    POST /sign                        动作类接口 → 403（红线拦截）
    POST /complete                    动作类接口 → 403（红线拦截）
    POST /terminate                   动作类接口 → 403（红线拦截）
    GET  /audit                       查看越权尝试审计记录
    GET  /health                      健康检查
    GET  /                            服务说明页

【启动方式】
    cd deploy/mock_approval
    python -m uvicorn main:app --host 127.0.0.1 --port 9100

    或使用配套脚本：
    deploy/scripts/start_mock_approval.bat     （Windows）
    bash deploy/scripts/start_mock_approval.sh （Linux/macOS）

【依赖】
    仅需 fastapi + uvicorn。刻意不引入本项目的其它依赖，
    保证这个模拟服务可以独立部署（比如放到另一台机器上模拟对接）。

【数据存储】
    全部数据存在进程内存中，重启即清空。
    这是刻意的：模拟服务不应该成为需要维护的组件。
"""

from __future__ import annotations

import json
import time
import uuid
from collections import OrderedDict
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# ======================================================================
# 一、应用初始化
# ======================================================================

app = FastAPI(
    title="模拟企业审批系统",
    description=(
        "合同审批智能审查系统的对接靶机。\n\n"
        "**核心特性**：仅接受评论类接口调用；所有改变审批结论的动作类接口"
        "（同意/拒绝/转交/撤回/转办/签署/完结/终止）一律返回 403 并留痕，"
        "用于验证审查系统的接口白名单机制真实生效。"
    ),
    version="1.0.0",
    docs_url="/docs",
    redoc_url=None,
)

# 内存存储
_COMMENTS: List[Dict[str, Any]] = []          # 已接收的评论
_AUDIT: List[Dict[str, Any]] = []             # 越权尝试审计记录
_APPROVALS: Dict[str, Dict[str, Any]] = {}    # 模拟的审批单
_STARTED_AT = time.time()

# 期望的鉴权令牌（与 backend/.env 的 APPROVAL_API_KEY 对应）
EXPECTED_API_KEY = "your-approval-api-key"

# 动作类接口清单（必须与 backend/app/utils/approval_guard.py 的
# FORBIDDEN_APPROVAL_APIS 保持一致）
FORBIDDEN_ACTIONS = [
    "approve", "reject", "transfer", "withdraw",
    "reassign", "sign", "complete", "terminate",
]


# ======================================================================
# 二、请求模型
# ======================================================================

class CommentRequest(BaseModel):
    """
    评论写入请求模型

    说明：
        字段与 backend/app/services/comment_service.py 中
        _submit_comment 构造的 request_body 完全对应。
        真实审批系统的评论接口字段名可能不同，
        对接时在此做适配即可，不需要改动审查系统侧代码。
    """

    contract_review_comment: str = Field(
        description="评论正文（审查意见全文）"
    )
    task_no: str = Field(default="", description="审查任务编号")
    final_risk_level: str = Field(default="", description="综合风险等级")
    risk_count: int = Field(default=0, description="风险条目数")


class BatchCommentRequest(BaseModel):
    """批量评论写入请求模型"""

    items: List[CommentRequest] = Field(description="评论列表")


class ActionResult(BaseModel):
    """动作类接口的拒绝响应模型"""

    code: int
    message: str
    detail: Dict[str, Any]


# ======================================================================
# 三、鉴权与拦截工具
# ======================================================================

def _record_audit(
    api_path: str,
    action: str,
    payload: Dict[str, Any],
    client_host: str,
    authorized: bool,
) -> None:
    """
    记录一次越权尝试

    参数：
        api_path: 被调用的接口路径
        action: 识别出的动作名
        payload: 请求体
        client_host: 调用方地址
        authorized: 携带的鉴权信息是否有效
    """
    _AUDIT.append(
        {
            "audit_id": uuid.uuid4().hex[:12],
            "api_path": api_path,
            "action": action,
            "payload": payload,
            "client_host": client_host,
            "authorized": authorized,
            "rejected": True,
            "reject_reason": (
                "该接口会改变审批结论，审查系统严禁调用。"
                "审查系统只被授权写入评论区。"
            ),
            "timestamp": datetime.now().isoformat(timespec="seconds"),
        }
    )


def _check_auth(authorization: Optional[str]) -> bool:
    """
    校验鉴权头

    参数：
        authorization: Authorization 请求头值

    返回：
        True 表示鉴权有效

    说明：
        刻意实现为"宽松校验"：只要 Bearer 令牌非空即通过，
        不强求等于 EXPECTED_API_KEY。
        原因：本地演示时用户可能没改 .env 里的密钥，
        因为一个密钥不匹配而让整个演示卡住，体验很差。
        生产对接时应改为严格比对。
    """
    if not authorization:
        return False
    if not authorization.startswith("Bearer "):
        return False
    token = authorization[len("Bearer ") :].strip()
    return len(token) > 0


def _reject_action(
    request: Request,
    action: str,
    payload: Dict[str, Any],
) -> JSONResponse:
    """
    拒绝动作类接口调用

    参数：
        request: FastAPI 请求对象（用于取客户端地址）
        action: 动作名
        payload: 请求体

    返回：
        HTTP 403 的 JSON 响应

    说明：
        即便携带了正确的鉴权头，动作类接口依然拒绝。
        这一点很重要：白名单是"能力边界"，不是"权限问题"。
        给审查系统更高的权限也不应该让它能替人审批。
    """
    authorization = request.headers.get("authorization")
    authorized = _check_auth(authorization)
    client_host = request.client.host if request.client else "unknown"

    _record_audit(
        api_path=request.url.path,
        action=action,
        payload=payload,
        client_host=client_host,
        authorized=authorized,
    )

    print(
        f"\n  [红线拦截] 收到动作类接口调用 "
        f"| 接口={request.url.path} | 动作={action} "
        f"| 来源={client_host} | 鉴权={'有效' if authorized else '无效'}"
    )
    print("             已拒绝并记入审计。审查系统仅被授权写入评论区。\n")

    return JSONResponse(
        status_code=403,
        content={
            "code": 40300,
            "message": (
                f"接口 {request.url.path} 被拒绝：该接口会改变审批结论，"
                f"审查系统无权调用。仅允许写入评论区。"
            ),
            "detail": {
                "api_path": request.url.path,
                "action": action,
                "allowed_apis": ["/comments", "/comments/batch"],
                "reason": "接口白名单机制（默认拒绝原则）",
                "note": "本次尝试已记入 /audit，可用于合规审计",
            },
        },
    )


# ======================================================================
# 四、白名单接口（评论写入）
# ======================================================================

@app.post(
    "/comments",
    summary="写入评论（白名单接口）",
    description="审查系统向审批单评论区写入审查意见。这是唯一被授权的接口族。",
)
async def write_comment(
    payload: CommentRequest,
    request: Request,
    authorization: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """
    接收评论写入

    参数：
        payload: 评论内容
        request: 请求对象
        authorization: 鉴权头

    返回：
        统一响应结构，含评论 ID 与接收时间

    异常：
        HTTPException 401：鉴权头缺失或格式错误
    """
    if not _check_auth(authorization):
        raise HTTPException(
            status_code=401,
            detail={
                "code": 40100,
                "message": "鉴权失败：缺少或格式错误的 Authorization 头（应为 Bearer <token>）",
            },
        )

    comment_id = f"CMT-{uuid.uuid4().hex[:10].upper()}"
    record = {
        "comment_id": comment_id,
        "task_no": payload.task_no,
        "final_risk_level": payload.final_risk_level,
        "risk_count": payload.risk_count,
        "comment_text": payload.contract_review_comment,
        "comment_length": len(payload.contract_review_comment),
        "received_at": datetime.now().isoformat(timespec="seconds"),
        # 说明：真实审批单的评论通常带审批单号。
        # 模拟服务用 task_no 作为审批单号，保持与审查任务一一对应，
        # 便于演示时对照查询。
        "approval_no": payload.task_no or "UNKNOWN",
    }
    _COMMENTS.append(record)

    # 同步维护模拟审批单
    if payload.task_no:
        _APPROVALS.setdefault(
            payload.task_no,
            {
                "approval_no": payload.task_no,
                "status": "pending",
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "comment_count": 0,
            },
        )
        _APPROVALS[payload.task_no]["comment_count"] += 1

    print(
        f"  [评论接收] {comment_id} | 任务={payload.task_no or '-'} "
        f"| 风险等级={payload.final_risk_level or '-'} "
        f"| 风险数={payload.risk_count} | 文本长度={record['comment_length']}"
    )

    return {
        "code": 0,
        "message": "评论写入成功",
        "data": {
            "comment_id": comment_id,
            "received_at": record["received_at"],
            "approval_no": record["approval_no"],
            "comment_length": record["comment_length"],
        },
    }


@app.post(
    "/comments/batch",
    summary="批量写入评论（白名单接口）",
    description="一次性写入多条评论，用于批量审查场景。",
)
async def write_comments_batch(
    payload: BatchCommentRequest,
    request: Request,
    authorization: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """
    接收批量评论写入

    参数：
        payload: 评论列表
        request: 请求对象
        authorization: 鉴权头

    返回：
        统一响应结构，含成功条数与评论 ID 列表
    """
    if not _check_auth(authorization):
        raise HTTPException(
            status_code=401,
            detail={
                "code": 40100,
                "message": "鉴权失败：缺少或格式错误的 Authorization 头",
            },
        )

    if not payload.items:
        raise HTTPException(
            status_code=400,
            detail={"code": 40000, "message": "评论列表为空"},
        )

    comment_ids: List[str] = []
    for item in payload.items:
        result = await write_comment(item, request, authorization)
        comment_ids.append(result["data"]["comment_id"])

    print(f"  [批量评论] 接收 {len(comment_ids)} 条")

    return {
        "code": 0,
        "message": f"批量评论写入成功，共 {len(comment_ids)} 条",
        "data": {
            "total": len(comment_ids),
            "comment_ids": comment_ids,
        },
    }


@app.get(
    "/comments",
    summary="查询已接收的评论",
    description="返回模拟服务收到的全部评论，用于验证回写是否真的送达。",
)
async def list_comments(task_no: Optional[str] = None) -> Dict[str, Any]:
    """
    查询已接收评论

    参数：
        task_no: 可选，按任务号过滤

    返回：
        评论列表与总数
    """
    items = _COMMENTS
    if task_no:
        items = [item for item in items if item["task_no"] == task_no]

    return {
        "code": 0,
        "message": "查询成功",
        "data": {
            "total": len(items),
            "items": items,
        },
    }


# ======================================================================
# 五、模拟审批单查询
# ======================================================================

@app.get(
    "/approval/{task_no}",
    summary="查询审批单信息（模拟）",
    description="返回模拟的审批单状态。审查系统的 pull_approval_task 节点对接此接口。",
)
async def get_approval(task_no: str) -> Dict[str, Any]:
    """
    查询模拟审批单

    参数：
        task_no: 审批单号 / 审查任务号

    返回：
        审批单信息

    说明：
        审批单不存在时自动创建一条 pending 记录，
        这样演示时不需要先手工造数据，
        直接跑审查流程就能拿到审批单。
    """
    if task_no not in _APPROVALS:
        _APPROVALS[task_no] = {
            "approval_no": task_no,
            "status": "pending",
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "comment_count": 0,
            # 以下字段模拟真实审批系统会提供的业务上下文，
            # 对应 ReviewTask 需要的申请人、业务类型、合同金额等
            "applicant": "演示用户",
            "business_type": "purchase",
            "contract_amount": 1200000.0,
            "counterparty": "演示供应商有限公司",
            "contract_type": "purchase",
            "template_flag": False,
            "auto_created": True,
        }

    return {
        "code": 0,
        "message": "查询成功",
        "data": _APPROVALS[task_no],
    }


@app.get(
    "/approvals",
    summary="列出全部模拟审批单",
    description="返回所有已被查询或写入过评论的审批单。",
)
async def list_approvals() -> Dict[str, Any]:
    """列出全部模拟审批单"""
    return {
        "code": 0,
        "message": "查询成功",
        "data": {
            "total": len(_APPROVALS),
            "items": list(_APPROVALS.values()),
        },
    }


# ======================================================================
# 六、动作类接口（红线拦截 —— 全部返回 403）
# ======================================================================
# 说明：以下 8 个接口是刻意实现的"陷阱"。
# 它们存在的唯一目的是：当有人（或某个有 bug 的客户端）
# 试图调用它们时，能立刻看到明确的拒绝与留痕。
#
# 如果审查系统真的调用这些接口，会在模拟服务这边留下审计记录，
# 同时审查系统自己的 approval_guard 也会在更早的位置抛异常。
# 两道防线，任一道被绕过都能被发现。

@app.post("/approve", summary="【红线】同意审批 —— 一律拒绝")
async def action_approve(request: Request) -> JSONResponse:
    """同意审批（动作类接口，一律拒绝）"""
    body = await _safe_body(request)
    return _reject_action(request, "approve", body)


@app.post("/reject", summary="【红线】拒绝审批 —— 一律拒绝")
async def action_reject(request: Request) -> JSONResponse:
    """拒绝审批（动作类接口，一律拒绝）"""
    body = await _safe_body(request)
    return _reject_action(request, "reject", body)


@app.post("/transfer", summary="【红线】转交审批 —— 一律拒绝")
async def action_transfer(request: Request) -> JSONResponse:
    """转交审批（动作类接口，一律拒绝）"""
    body = await _safe_body(request)
    return _reject_action(request, "transfer", body)


@app.post("/withdraw", summary="【红线】撤回审批 —— 一律拒绝")
async def action_withdraw(request: Request) -> JSONResponse:
    """撤回审批（动作类接口，一律拒绝）"""
    body = await _safe_body(request)
    return _reject_action(request, "withdraw", body)


@app.post("/reassign", summary="【红线】转办审批 —— 一律拒绝")
async def action_reassign(request: Request) -> JSONResponse:
    """转办审批（动作类接口，一律拒绝）"""
    body = await _safe_body(request)
    return _reject_action(request, "reassign", body)


@app.post("/sign", summary="【红线】签署审批 —— 一律拒绝")
async def action_sign(request: Request) -> JSONResponse:
    """签署审批（动作类接口，一律拒绝）"""
    body = await _safe_body(request)
    return _reject_action(request, "sign", body)


@app.post("/complete", summary="【红线】完结审批 —— 一律拒绝")
async def action_complete(request: Request) -> JSONResponse:
    """完结审批（动作类接口，一律拒绝）"""
    body = await _safe_body(request)
    return _reject_action(request, "complete", body)


@app.post("/terminate", summary="【红线】终止审批 —— 一律拒绝")
async def action_terminate(request: Request) -> JSONResponse:
    """终止审批（动作类接口，一律拒绝）"""
    body = await _safe_body(request)
    return _reject_action(request, "terminate", body)


async def _safe_body(request: Request) -> Dict[str, Any]:
    """
    安全读取请求体

    参数：
        request: FastAPI 请求对象

    返回：
        请求体字典；解析失败时返回包含原文的字典

    说明：
        越权请求的 body 不一定是我们预期的结构，
        甚至可能不是合法 JSON。这里必须容错，
        否则解析异常会掩盖"这是一次越权调用"这个更重要的信息。
    """
    try:
        body = await request.json()
        if isinstance(body, dict):
            return body
        return {"raw": body}
    except Exception:  # noqa: BLE001
        try:
            raw = await request.body()
            return {"raw_text": raw.decode("utf-8", errors="replace")[:500]}
        except Exception:  # noqa: BLE001
            return {"raw_text": "<无法读取请求体>"}


# ======================================================================
# 七、审计查询
# ======================================================================

@app.get(
    "/audit",
    summary="查看越权尝试审计记录",
    description=(
        "返回所有被拒绝的动作类接口调用记录。"
        "空列表即为正常——说明审查系统从未试图改变审批结论。"
    ),
)
async def get_audit() -> Dict[str, Any]:
    """
    查询越权尝试审计记录

    返回：
        审计记录列表与结论说明

    说明：
        返回体里带一个 verdict 字段，直接给出结论。
        因为使用者看这个接口时真正想知道的是一句话：
        "到底有没有越权？" 而不是自己去数记录条数。
    """
    if not _AUDIT:
        verdict = (
            "正常：未发现任何越权调用。审查系统严格遵循接口白名单，"
            "只写入了评论区。"
        )
    else:
        verdict = (
            f"警告：发现 {len(_AUDIT)} 次越权尝试。"
            f"审查系统不应调用动作类接口，请检查 approval_guard 是否被绕过。"
        )

    return {
        "code": 0,
        "message": "查询成功",
        "data": {
            "verdict": verdict,
            "total": len(_AUDIT),
            "items": _AUDIT,
            "allowed_apis": ["/comments", "/comments/batch"],
            "forbidden_actions": FORBIDDEN_ACTIONS,
        },
    }


# ======================================================================
# 八、健康检查与说明页
# ======================================================================

@app.get("/health", summary="健康检查")
async def health() -> Dict[str, Any]:
    """
    健康检查

    返回：
        服务状态、运行时长与内存中的数据统计
    """
    return {
        "code": 0,
        "message": "服务正常",
        "data": {
            "service": "mock-approval-system",
            "status": "alive",
            "uptime_seconds": round(time.time() - _STARTED_AT, 1),
            "comments_received": len(_COMMENTS),
            "approvals_tracked": len(_APPROVALS),
            "violations_rejected": len(_AUDIT),
            "whitelist": ["/comments", "/comments/batch"],
        },
    }


@app.get("/", summary="服务说明")
async def index() -> Dict[str, Any]:
    """
    服务说明页

    返回：
        接口清单与使用说明
    """
    return {
        "code": 0,
        "message": "模拟企业审批系统（合同审批智能审查系统 对接靶机）",
        "data": {
            "purpose": (
                "为合同审查系统提供本地可用的审批系统对接靶机，"
                "同时验证接口白名单机制真实生效。"
            ),
            "whitelist_apis": {
                "POST /comments": "写入评论",
                "POST /comments/batch": "批量写入评论",
                "GET  /comments": "查询已接收评论",
            },
            "forbidden_apis": {
                f"POST /{action}": "动作类接口，一律返回 403"
                for action in FORBIDDEN_ACTIONS
            },
            "other_apis": {
                "GET /approval/{task_no}": "查询模拟审批单",
                "GET /approvals": "列出全部模拟审批单",
                "GET /audit": "查看越权尝试审计记录",
                "GET /health": "健康检查",
                "GET /docs": "Swagger 文档",
            },
            "quick_start": [
                "1. 启动本服务：python -m uvicorn main:app --port 9100",
                "2. 启动后端，确认 .env 中 APPROVAL_BASE_URL=http://127.0.0.1:9100",
                "3. 在工作台点击「一键回写审批单」",
                "4. 查询 GET /comments 确认评论已送达",
                "5. 查询 GET /audit 确认无越权调用",
            ],
        },
    }


# ======================================================================
# 九、全局异常处理
# ======================================================================

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """
    全局异常处理

    参数：
        request: 请求对象
        exc: 捕获到的异常

    返回：
        500 的 JSON 响应

    说明：
        统一响应结构，保证调用方（审查系统）能按 code 字段解析，
        而不是面对一段 HTML 错误页。
    """
    print(f"  [异常] {request.method} {request.url.path} → {type(exc).__name__}: {exc}")
    return JSONResponse(
        status_code=500,
        content={
            "code": 50000,
            "message": f"模拟审批系统内部异常：{type(exc).__name__}",
            "detail": {"path": request.url.path, "error": str(exc)[:300]},
        },
    )


# ======================================================================
# 十、直接运行入口
# ======================================================================

if __name__ == "__main__":
    import uvicorn

    print("=" * 70)
    print("  模拟企业审批系统")
    print("=" * 70)
    print("  监听地址  : http://127.0.0.1:9100")
    print("  Swagger   : http://127.0.0.1:9100/docs")
    print()
    print("  白名单接口（允许）:")
    print("    POST /comments          写入评论")
    print("    POST /comments/batch    批量写入评论")
    print()
    print("  动作类接口（一律 403，用于验证红线）:")
    for action in FORBIDDEN_ACTIONS:
        print(f"    POST /{action}")
    print()
    print("  查询接口:")
    print("    GET  /comments              查看已接收评论")
    print("    GET  /audit                 查看越权尝试审计")
    print("    GET  /approval/{task_no}    查询模拟审批单")
    print("    GET  /health                健康检查")
    print("=" * 70)

    uvicorn.run(app, host="127.0.0.1", port=9100, log_level="warning")
