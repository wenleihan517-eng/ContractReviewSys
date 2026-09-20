# D:/ContractReviewSys-合同审批智能审查项目/backend/app/api/v1/endpoints/report_comment.py
"""
报告与评论接口

【接口清单】
    POST /report/{task_id}/generate                生成审查报告（含持久化）
    POST /report/{task_id}/comment/preview         预览审批评论
    POST /report/{task_id}/comment/write-back      正式回写审批评论
    GET  /report/{task_id}/download                下载报告

【权责红线】
    评论回写仅写入审批系统评论区，不改变审批结论。
    接口路径经 approval_guard 白名单校验。

【下载通道双模式（缺口2 修复）】
    下载接口保留原 JSON 响应结构作为默认模式，同时新增文件流模式，
    两条通道并存，前端无需改动即可继续用 JSON 模式：
        - 默认（mode 省略 / mode=json）：返回 ReportDownloadResponse，
          含报告 Markdown 正文 + 重新签发的预签名链接；响应结构向后兼容
        - mode=stream：返回 application/octet-stream 文件流，
          带 Content-Disposition 文件名，可直接落盘

【权限模型】
    下载需 PERM_REPORT_DOWNLOAD 权限点（三角色均持有）；
    且仅「任务提交人 / 法务 / 管理员」可下载对应报告，其余角色视为越权。
"""

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response

from app.constants import PERM_REPORT_DOWNLOAD, UserRole
from app.core.dependencies import CurrentUser, get_current_user, get_current_request_id
from app.core.exceptions import BusinessException, ErrorCode
from app.core.logging_config import get_logger
from app.core.response import ApiResponse, success_response
from app.schemas.report import (
    CommentPreviewRequest,
    CommentPreviewResponse,
    CommentWriteBackRequest,
    CommentWriteBackResponse,
    ReportDownloadResponse,
    ReportGenerateRequest,
    ReportGenerateResponse,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/report", tags=["报告与评论"])


@router.post("/{task_id}/generate", summary="生成审查报告",
             description="生成完整审查报告，上传 MinIO、持久化报告字段并返回访问链接。"
                         "format=md（默认）产出 Markdown 源文本；"
                         "format=pdf 额外产出 PyMuPDF 绘制的 A4 版式 PDF。")
async def generate_report(
    task_id: str,
    body: ReportGenerateRequest,
    user: CurrentUser = Depends(get_current_user),
) -> ApiResponse:
    """
    生成审查报告

    流程：
        组装最小图状态 → 调用报告服务（渲染 + 上传 + 幂等复用 + 落库）

    【format 参数语义（缺口1 修复）】
        - md（默认）：生成 Markdown 报告。同时按配置 PDF_AUTO_WITH_MD
          决定是否顺带产出 PDF，因此默认行为下两种格式都会就绪。
        - pdf：强制产出 PDF（即使 PDF_AUTO_WITH_MD 关闭也会生成）。
          Markdown 源文本无论如何都会生成并落库 ——
          因为 PDF 由该源文本渲染而来，跳过它 PDF 就失去了唯一数据源。

    入参：
        task_id: 任务 ID（路径参数）
        body: ReportGenerateRequest（format: md / pdf）
        user: 当前登录用户（鉴权中间件已强制，此处用于权限预检）
    出参：
        data: ReportGenerateResponse（report_url / report_object_key /
              pdf_object_key / pdf_url / quality_passed / risk_item_count /
              report_format / reused / persisted）
    """
    from app.core.config import settings
    from app.services import task_service

    task = await task_service.get_task_by_id(task_id)
    if task is None:
        raise BusinessException(
            code=ErrorCode.NOT_FOUND, message=f"任务不存在：{task_id}", http_status=404
        )

    # 生成报告同样受"报告查看权限"约束，避免无权用户触发产物生成
    _assert_report_access(task, user, action="generate")

    # -------------------- 参数规范化与可用性校验 --------------------
    fmt = (body.format or "md").strip().lower()
    if fmt not in ("md", "pdf"):
        raise BusinessException(
            code=ErrorCode.VALIDATION_ERROR,
            message=f"不支持的报告格式：{body.format}，可选值：md / pdf",
            http_status=400,
        )
    if fmt == "pdf" and not settings.PDF_ENABLED:
        raise BusinessException(
            code=ErrorCode.VALIDATION_ERROR,
            message="PDF 报告导出功能已关闭（PDF_ENABLED=False），请使用 format=md",
            http_status=400,
        )

    # 报告生成需要完整图状态。此前仅组装了最小状态（无结论、无风险项），
    # 会导致生成的报告"结论为空、风险清单 0 项"——与审查结果接口的数据不一致。
    # 现改为从数据库重建：任务主档 + cr_review_conclusion + cr_review_risk_hit。
    from app.services import report_service
    from app.services import orchestration_service
    from app.graph.state import ReviewTaskState, RiskItem

    # 从数据库重建审查结论与风险项（保证报告与审查结果接口口径一致）
    # 说明：入参可能是 task_no，结论/风险集合以 _id 为键，须传真实 _id。
    conclusion_doc, risk_docs = await _load_conclusion_and_risks(str(task.id))

    risk_items: List[RiskItem] = []
    for doc in risk_docs:
        try:
            risk_items.append(RiskItem(
                risk_id=doc.get("risk_id", ""),
                rule_code=doc.get("rule_code"),
                rule_name=doc.get("rule_name", "") or "",
                clause_id=doc.get("clause_id"),
                clause_type=doc.get("clause_type"),
                evidence_text=doc.get("evidence_text", "") or "",
                char_start=doc.get("char_start"),
                char_end=doc.get("char_end"),
                page_no=doc.get("page_no"),
                risk_level=doc.get("risk_level") or "low",
                decision=doc.get("decision", "") or "",
                severity=doc.get("severity"),
                suggestion_text=doc.get("suggestion_text", "") or "",
                legal_basis=doc.get("legal_basis", "") or "",
                reference_only=False,
            ))
        except Exception as exc:  # noqa: BLE001 - 单条异常不阻断整体报告生成
            logger.warning("风险项转换失败，已跳过 | risk_id=%s | 异常=%s",
                           doc.get("risk_id"), exc)

    distribution = (conclusion_doc or {}).get("risk_distribution", {}) or {}
    if not distribution and risk_items:
        for item in risk_items:
            distribution[item.risk_level] = distribution.get(item.risk_level, 0) + 1

    # 组装 ReviewTaskState（供报告渲染使用）
    state = ReviewTaskState(
        task_id=task_id,
        task_no=task.task_no,
        contract_id=task.contract_id,
        # 合同业务字段：任务主档未存这些字段，从审批系统上下文补齐
        contract_title=getattr(task, "contract_title", "") or "",
        contract_no=getattr(task, "contract_no", "") or task.task_no,
        contract_type=getattr(task, "contract_type", "other") or "other",
        contract_amount=getattr(task, "contract_amount", 0.0) or 0.0,
        currency=getattr(task, "currency", "CNY") or "CNY",
        # 审查结论（规则引擎产出，原样回填）
        final_risk_items=risk_items,
        risk_level=(conclusion_doc or {}).get("risk_level"),
        is_blocked=bool((conclusion_doc or {}).get("is_blocked", False)),
        risk_distribution=distribution,
        conclusion_text=(conclusion_doc or {}).get("conclusion", "") or "",
        conclusion_hash=(conclusion_doc or {}).get("conclusion_hash", "") or "",
        rule_engine_version=(conclusion_doc or {}).get("engine_version", "") or "",
        rule_evaluated=conclusion_doc is not None,
    )

    # 传入 task_id → 启用"幂等复用 + 报告字段持久化落库"
    # include_pdf：显式请求 pdf 时强制生成；否则交给配置 PDF_AUTO_WITH_MD 决定
    include_pdf = True if fmt == "pdf" else None
    result = await report_service.generate_report_service(
        state, task_id=task_id, include_pdf=include_pdf
    )

    # 显式请求 pdf 但联动生成失败时（PDF 生成异常不阻断 Markdown），
    # 需要向调用方明确暴露失败，避免"返回 200 但拿不到 PDF"的静默失真。
    if fmt == "pdf" and not result.pdf_object_key:
        await _write_audit(
            task=task,
            user=user,
            action="report_generate_pdf_failed",
            message="PDF 报告生成失败，Markdown 报告已正常产出",
            success=False,
        )
        raise BusinessException(
            code=ErrorCode.REVIEW_TASK_ERROR,
            message="PDF 报告生成失败，Markdown 报告已正常产出，请查看服务日志",
            detail=f"task_id={task_id}",
            http_status=500,
        )

    # 关键操作留痕（审计失败不影响报告产出）
    await _write_audit(
        task=task,
        user=user,
        action="report_generate",
        message=(
            f"生成审查报告 | 请求格式={fmt} | Markdown对象键={result.report_object_key} | "
            f"PDF对象键={result.pdf_object_key or '-'} | "
            f"复用={result.reused} | 落库={result.persisted} | "
            f"风险项={result.risk_item_count}"
        ),
    )

    # 取 PDF 元信息用于响应（页数/引擎需从渲染结果读取，此处用文件大小反推不成立，
    # 故仅回传对象键与链接，页数以 0 表示"未在本响应中统计"）
    return success_response(
        data=ReportGenerateResponse(
            task_id=task_id,
            report_url=result.report_url,
            report_object_key=result.report_object_key,
            quality_passed=result.quality_passed,
            risk_item_count=result.risk_item_count,
            report_format="md",
            reused=result.reused,
            persisted=result.persisted,
            pdf_object_key=result.pdf_object_key,
            pdf_url=result.pdf_url,
            pdf_size_bytes=int(getattr(task, "pdf_size_bytes", 0) or 0),
            pdf_page_count=0,
            pdf_render_engine="pymupdf" if result.pdf_object_key else "",
            pdf_reused=False,
        ).model_dump(),
        message=(
            "审查报告生成成功（Markdown 与 PDF 均已产出）"
            if result.pdf_object_key
            else ("审查报告生成成功（内容未变更，已复用既有文件）" if result.reused
                  else "审查报告生成成功")
        ),
        request_id=get_current_request_id(),
    )


@router.post("/{task_id}/comment/preview", summary="预览审批评论",
             description="按模板拼装审批评论并预览（不写入审批系统）。")
async def preview_comment(task_id: str, body: CommentPreviewRequest) -> ApiResponse:
    """
    预览审批评论

    入参：
        task_id: 任务 ID（路径参数）
        body: CommentPreviewRequest
    出参：
        data: CommentPreviewResponse（comment_text / validation_passed）
    """
    from app.services import task_service

    task = await task_service.get_task_by_id(task_id)
    if task is None:
        raise BusinessException(
            code=ErrorCode.NOT_FOUND, message=f"任务不存在：{task_id}", http_status=404
        )

    # 组装最小状态并生成评论
    from app.services import comment_service
    from app.graph.state import ReviewTaskState

    # 说明：state.task_id 须传真实 _id（comment_service 依此查询结论/风险，
    # 入参可能是 task_no）
    state = ReviewTaskState(
        task_id=str(task.id), task_no=task.task_no,
        risk_level=None, conclusion_text="",
    )
    result = await comment_service.generate_comment_service(state)

    return success_response(
        data=CommentPreviewResponse(
            task_id=task_id,
            comment_text=result.comment_text,
            api_path=result.api_path,
            risk_item_count=result.risk_item_count,
            validation_passed=result.validation_passed,
            validation_issues=result.validation_issues,
        ).model_dump(),
        message="评论预览成功",
        request_id=get_current_request_id(),
    )


@router.post("/{task_id}/comment/write-back", summary="正式回写审批评论",
             description="将审查意见写入审批系统评论区（支持预览模式）。")
async def write_back_comment(task_id: str, body: CommentWriteBackRequest) -> ApiResponse:
    """
    正式回写审批评论

    【权责红线】仅写入评论区，不改变审批结论。

    入参：
        task_id: 任务 ID（路径参数）
        body: CommentWriteBackRequest（preview 预览模式开关）
    出参：
        data: CommentWriteBackResponse（write_back_status / preview / error）
    """
    from app.services import task_service

    task = await task_service.get_task_by_id(task_id)
    if task is None:
        raise BusinessException(
            code=ErrorCode.NOT_FOUND, message=f"任务不存在：{task_id}", http_status=404
        )

    from app.services import comment_service
    from app.graph.state import ReviewTaskState, WriteBackInfo, WriteBackState

    # 组装状态：先生成评论，再执行回写（state.task_id 传真实 _id）
    state = ReviewTaskState(
        task_id=str(task.id), task_no=task.task_no,
        risk_level=None, conclusion_text="",
    )
    comment_result = await comment_service.generate_comment_service(state)

    state.write_back_content = WriteBackInfo(
        status=WriteBackState.WRITING.value,
        comment_payload=comment_result.payload,
        comment_text=comment_result.comment_text,
        api_path=comment_result.api_path,
    )
    state.write_back_status = WriteBackState.WRITING.value

    info = await comment_service.write_comment_service(state, preview=body.preview)

    return success_response(
        data=CommentWriteBackResponse(
            task_id=task_id,
            write_back_status=info.status,
            preview=body.preview,
            http_status=info.http_status,
            response_code=info.response_code,
            error=info.error,
        ).model_dump(),
        message="评论回写完成" if info.status == "success" else f"评论回写结果：{info.status}",
        request_id=get_current_request_id(),
    )


@router.get("/{task_id}/download", summary="下载报告",
            description="下载指定任务的审查报告。format 选择 md（默认，Markdown 源文本）"
                        "或 pdf（PyMuPDF 绘制的 A4 版式报告）；"
                        "mode=json 返回内容与链接，mode=stream 返回带文件名的文件流。")
async def download_report(
    task_id: str,
    mode: str = Query(
        default="json",
        description="响应模式：json（默认，兼容原结构）或 stream（文件流下载）",
    ),
    user: CurrentUser = Depends(get_current_user),
    format: str = Query(
        default="md",
        description="报告格式：md（默认，兼容原有调用）或 pdf",
    ),
):
    """
    下载审查报告

    流程：
        1. 查询任务，不存在 → 404
        2. 权限校验：任务提交人 / 法务 / 管理员，且需持有 report:download 权限点
        3. 规范化 format 参数并校验是否受支持（PDF 可被配置关闭）
        4. 校验对应格式的报告是否已生成 → 不存在 → 404
        5. 从 MinIO 读取报告内容
        6. 按 mode 返回：json（兼容原结构）或 stream（文件流）

    【format 参数设计（缺口1 修复）】
        - 默认 "md"：与改造前行为完全一致，老前端不传该参数仍走 Markdown，
          响应结构、字段、Content-Type 均未变化，属于纯增量扩展。
        - "pdf"：返回 PDF 文件流。浏览器收到 application/pdf 后会内联预览
          （用户可直接在标签页中查看），点击下载则按 Content-Disposition 落盘。
        - 大小写与空格容错：入参统一 strip + lower，避免 "PDF" / " pdf " 被判非法。
        - PDF 未生成但任务已有 Markdown 时：返回明确提示"请先以 format=pdf 生成"，
          而不是含糊的 404，便于前端给出正确引导。

    ★ 参数顺序说明（兼容性约束，勿调整）
        新参数 format 必须排在 (mode, user) 之后 —— 也就是说它只能是
        第 4 个位置参数。原因：改造前签名为 download_report(task_id, mode, user)，
        已有调用方以**位置参数**方式传参（tests/verify_report_export.py 中多处
        形如 download_report(task.id, "json", user)）。
        若把 format 插到 user 之前，这些调用的第三个位置参数 user 会被 format 抢占，
        导致鉴权拿到 Depends 默认值而报 AttributeError。
        排在最后则完全不影响既有调用：位置参数仍按 (task_id, mode, user) 对齐，
        format 只能由关键字或查询串传入。

    入参：
        task_id: 任务 ID（路径参数）
        mode: 响应模式，json（默认）/ stream
        user: 当前登录用户
        format: 报告格式，md（默认）/ pdf

    出参：
        mode=json  → ApiResponse，data 为 ReportDownloadResponse
        mode=stream → md: text/markdown 文件流；pdf: application/pdf 文件流
                     两者均带 Content-Disposition 与 Content-Length

    异常：
        400 格式参数非法 / PDF 功能已关闭
        404 任务不存在 / 对应格式报告未生成
        403 无权下载该任务报告
        30001 MinIO 读取失败
    """
    from app.core.config import settings
    from app.services import report_service
    from app.storage import minio_client

    # -------------------- 步骤一：查询任务 --------------------
    from app.services import task_service

    task = await task_service.get_task_by_id(task_id)
    if task is None:
        raise BusinessException(
            code=ErrorCode.NOT_FOUND, message=f"任务不存在：{task_id}", http_status=404
        )

    # -------------------- 步骤二：权限校验 --------------------
    # 权限规则不区分格式：有权下载该任务报告的人，两种格式都可下载
    _assert_report_access(task, user, action="download")

    # -------------------- 步骤三：规范化并校验 format / mode --------------------
    # ★ 必须做类型防御：以普通 Python 函数方式直接调用本端点时
    #   （测试与内部调用场景），未显式传入的查询参数拿到的不是默认字符串，
    #   而是 FastAPI 的 QueryInfo/Depends 占位对象。
    #   若不判断类型就 .strip() 或比较，会抛 AttributeError 或误判。
    fmt = format if isinstance(format, str) else "md"
    fmt = (fmt or "md").strip().lower()
    resp_mode = mode if isinstance(mode, str) else "json"
    if fmt not in ("md", "pdf"):
        raise BusinessException(
            code=ErrorCode.VALIDATION_ERROR,
            message=f"不支持的报告格式：{format}，可选值：md / pdf",
            http_status=400,
        )
    if fmt == "pdf" and not settings.PDF_ENABLED:
        raise BusinessException(
            code=ErrorCode.VALIDATION_ERROR,
            message="PDF 报告导出功能已关闭（PDF_ENABLED=False），请使用 format=md",
            http_status=400,
        )

    # -------------------- 步骤四：校验对应格式报告是否已生成 --------------------
    if fmt == "pdf":
        object_key = getattr(task, "pdf_object_key", None)
        file_name = report_service.build_pdf_file_name(task_id)
        not_ready_hint = (
            f"该任务的 PDF 报告尚未生成，请先调用生成接口（format=pdf）后重试：{task_id}"
        )
        content_type = "application/pdf"
        generated_at = getattr(task, "pdf_generated_at", None)
        media_type = "application/pdf"
    else:
        object_key = getattr(task, "report_object_key", None)
        file_name = report_service.build_report_file_name(task_id)
        not_ready_hint = f"报告尚未生成，请先调用生成接口：{task_id}"
        content_type = "text/markdown; charset=utf-8"
        generated_at = getattr(task, "report_generated_at", None)
        media_type = "text/markdown; charset=utf-8"

    if not object_key:
        # 历史任务无报告字段时同样落到这里：明确提示"尚未生成"，
        # 而不是返回空内容（这是缺口2 修复的核心问题）。
        await _write_audit(
            task=task,
            user=user,
            action="report_download_missing",
            message=f"{fmt.upper()} 报告尚未生成，拒绝下载",
            success=False,
        )
        raise BusinessException(
            code=ErrorCode.NOT_FOUND, message=not_ready_hint, http_status=404
        )

    # -------------------- 步骤五：从 MinIO 读取报告 --------------------
    try:
        raw_bytes = await minio_client.download_bytes(
            bucket=settings.MINIO_BUCKET_REPORT, object_key=object_key
        )
    except Exception as exc:  # noqa: BLE001 - 统一转为业务错误码，避免 500 裸奔
        logger.error(
            "报告下载失败 | 任务=%s | 格式=%s | 对象键=%s | 异常=%s",
            task_id, fmt, object_key, exc,
        )
        await _write_audit(
            task=task,
            user=user,
            action="report_download_failed",
            message=f"对象存储读取失败 | 格式={fmt} | 对象键={object_key}",
            success=False,
        )
        raise BusinessException(
            code=ErrorCode.MINIO_ERROR,
            message="报告文件读取失败，请稍后重试",
            detail=f"bucket={settings.MINIO_BUCKET_REPORT}, key={object_key}",
            http_status=502,
        ) from exc

    # -------------------- 步骤六：留痕 --------------------
    await _write_audit(
        task=task,
        user=user,
        action="report_download",
        message=(
            f"下载审查报告 | 格式={fmt} | 模式={resp_mode} | 对象键={object_key} | "
            f"大小={len(raw_bytes)}字节"
        ),
    )

    # -------------------- 步骤七：按模式返回 --------------------
    if resp_mode == "stream":
        # 文件流模式：md 走原样二进制；pdf 直传二进制，浏览器可内联预览。
        # ★ Content-Disposition 用 inline 而非 attachment：
        #   PDF 场景需求要求"支持浏览器直接预览与下载"，
        #   inline 允许预览，用户仍可自行另存；同时附上 filename 供另存命名。
        disposition = "inline" if fmt == "pdf" else "attachment"
        return Response(
            content=raw_bytes,
            media_type=media_type,
            headers={
                "Content-Disposition": f'{disposition}; filename="{file_name}"',
                "Content-Length": str(len(raw_bytes)),
                # 暴露给前端 JS 读取文件名（跨域场景下必须显式 expose）
                "Access-Control-Expose-Headers": "Content-Disposition",
                "X-Report-Object-Key": object_key,
                "X-Report-Format": fmt,
            },
        )

    # -------------------- JSON 模式：兼容原有响应结构 --------------------
    report_url = ""
    try:
        report_url = await minio_client.get_presigned_url(
            bucket=settings.MINIO_BUCKET_REPORT,
            object_key=object_key,
            expires_seconds=settings.REPORT_URL_EXPIRE_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 - 链接失败不影响正文返回
        logger.warning("报告预签名链接重新签发失败 | 任务=%s | 异常=%s", task_id, exc)

    # PDF 是二进制，不能塞进 JSON 的 content 字段（会破坏原有契约），
    # 因此 JSON 模式下 PDF 仅返回元信息 + 预签名链接，正文留空。
    content = ""
    if fmt == "md":
        content = raw_bytes.decode("utf-8", errors="replace")

    return success_response(
        data=ReportDownloadResponse(
            task_id=task_id,
            content=content,
            report_url=report_url,
            report_object_key=object_key,
            file_name=file_name,
            report_format=fmt,
            report_generated_at=generated_at.isoformat() if generated_at else None,
            report_available=True,
        ).model_dump(mode="json"),
        message=(
            "PDF 报告链接已签发（二进制正文请使用 mode=stream 获取）"
            if fmt == "pdf"
            else "报告下载成功"
        ),
        request_id=get_current_request_id(),
    )


# ======================================================================
# 内部辅助：权限校验与审计留痕
# ======================================================================
async def _load_conclusion_and_risks(task_id: str) -> tuple:
    """
    从数据库读取审查结论与规则引擎风险项

    说明：
        报告生成需要完整的审查结论与风险清单，而这些数据由审查流程
        落库到 cr_review_conclusion 与 cr_review_risk_hit 两个集合。
        本函数负责把它们读回来，保证"报告内容"与"审查结果接口"口径一致。

    参数：
        task_id: 审查任务 ID

    返回：
        (conclusion_doc, risk_docs) 二元组
        conclusion_doc: 结论文档字典；未审查时返回 None
        risk_docs: 规则引擎命中的风险项字典列表（不含 AI 参考候选）
    """
    from app.constants import MongoCollection
    from app.storage.mongo_client import mongo_client

    conclusion_col = mongo_client.get_collection(MongoCollection.REVIEW_CONCLUSION)
    conclusion_doc = await conclusion_col.find_one({"task_id": task_id})

    risk_col = mongo_client.get_collection(MongoCollection.REVIEW_RISK_HIT)
    risk_docs = []
    cursor = risk_col.find({"task_id": task_id, "hit_source": "rule_engine"}).sort("risk_id", 1)
    async for doc in cursor:
        doc.pop("_id", None)
        risk_docs.append(doc)

    return conclusion_doc, risk_docs


def _assert_report_access(task, user: CurrentUser, action: str) -> None:
    """
    校验当前用户是否有权访问该任务的报告

    规则（与需求一致）：
        1. 必须持有 report:download 权限点（三角色均持有）
        2. 且满足以下任一：
           - 是任务提交人（task.created_by == user.username 或 user.user_id）
           - 是法务审查人（law_user）
           - 是系统管理员（admin）

    参数：
        task: 审查任务模型
        user: 当前登录用户
        action: 操作类型（generate / download），仅用于日志与错误文案

    返回：
        无

    异常：
        BusinessException：权限不足时抛出 403（错误码 60009）
    """
    # 第一层：权限点校验
    if not user.has_permission(PERM_REPORT_DOWNLOAD):
        logger.warning(
            "报告操作被拒：缺少权限点 | 用户=%s | 角色=%s | 操作=%s",
            user.username, user.role, action,
        )
        raise BusinessException(
            code=ErrorCode.PERMISSION_DENIED,
            message="当前账号无报告下载权限",
            http_status=403,
        )

    # 第二层：所有者 / 法务 / 管理员校验
    role_privileged = user.role in (UserRole.LAW_USER.value, UserRole.ADMIN.value)
    owner_ref = getattr(task, "created_by", "") or ""
    is_owner = owner_ref != "" and owner_ref in (user.username, user.user_id)

    if not (role_privileged or is_owner):
        logger.warning(
            "报告操作被拒：非本人任务且非特权角色 | 用户=%s | 任务=%s | 提交人=%s",
            user.username, getattr(task, "task_no", "?"), owner_ref,
        )
        raise BusinessException(
            code=ErrorCode.PERMISSION_DENIED,
            message="仅任务提交人、法务审查人或系统管理员可访问该报告",
            http_status=403,
        )


async def _write_audit(task, user: CurrentUser, action: str, message: str,
                       success: bool = True) -> None:
    """
    写入报告相关操作的审计日志

    说明：
        审计日志写入失败只告警、绝不阻断主流程——否则一次审计库抖动
        会让用户无法下载已经存在的报告。

    参数：
        task: 审查任务模型
        user: 当前登录用户
        action: 动作标识（report_generate / report_download / ...）
        message: 审计描述
        success: 该操作是否成功

    返回：
        无
    """
    from app.repositories import audit_repo

    try:
        await audit_repo.append_log(
            {
                "task_id": getattr(task, "id", None),
                "task_no": getattr(task, "task_no", ""),
                "node": "report_export",
                "action": action,
                "message": message,
                "operator": user.username,
                "operator_role": user.role,
                "request_id": get_current_request_id(),
                "success": success,
            }
        )
    except Exception as exc:  # noqa: BLE001 - 审计失败不影响主流程
        logger.error("报告审计日志写入失败 | 动作=%s | 异常=%s", action, exc)
