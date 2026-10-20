# D:/ContractReviewSys-合同审批智能审查项目/backend/app/api/v1/endpoints/review.py
"""
风险审查接口

【接口清单】
    POST /review/{task_id}/trigger                    触发审查
    GET  /review/{task_id}/result                     审查结果查询
    PUT  /review/risk-items/{risk_id}                 人工编辑风险项（仅建议类字段）
    PUT  /review/risk-items/{risk_id}/recommend       人工修改推荐条款

【权责红线】
    风险等级 / 处理决策 / 严重度由规则引擎产出，接口层不提供定级写入入口。
    人工编辑仅允许修改建议类字段（suggestion_text / legal_basis / recommended_text），
    不允许修改判定字段。

【鉴权约定（2026-09-21 补全）】
    本模块此前缺少接口级鉴权，仅靠全局登录中间件保护，导致业务经办人
    枚举 task_no 即可读取他人合同的审查结论（水平越权）。
    现按"能力 + 归属"两层补齐：
      · 读类（result）      → 任务归属校验（本人 / task:view_all）
      · 触发类（trigger）   → 任务归属校验（本人 / task:view_all）
      · 判定类修改（risk-items）→ 权限点 risk_item:edit（法务 / 管理员）
"""

from fastapi import APIRouter, Depends

from app.constants import PERM_RISK_ITEM_EDIT
from app.core.dependencies import (
    CurrentUser,
    assert_task_access,
    get_current_request_id,
    get_current_user,
    require_permissions,
)
from app.core.exceptions import BusinessException, ErrorCode
from app.core.logging_config import get_logger
from app.core.response import ApiResponse, success_response
from app.schemas.review import (
    ClauseRecommendRequest,
    ClauseRecommendResponse,
    ReviewResultResponse,
    ReviewTriggerRequest,
    ReviewTriggerResponse,
    RiskItemEditRequest,
    RiskItemEditResponse,
    RiskItemResponse,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/review", tags=["风险审查"])


@router.post("/{task_id}/trigger", summary="触发审查",
             description="对指定任务触发风险审查（规则引擎 + 向量 + 大模型多路召回），同步执行并落库结论。")
async def trigger_review(
    task_id: str,
    body: ReviewTriggerRequest,
    user: CurrentUser = Depends(get_current_user),
) -> ApiResponse:
    """
    触发审查

    入参：
        task_id: 任务 ID（路径参数）
        body: ReviewTriggerRequest
        user: 当前登录用户（用于任务归属校验）
    出参：
        data: ReviewTriggerResponse（accepted / message / 判定摘要）

    权限：
        任务提交人本人，或拥有 task:view_all 的法务/管理员；
        其他用户返回 403 —— 防止经办人触发他人任务的审查。
    """
    from app.services import orchestration_service, task_service

    # 先取任务用于归属校验：不存在的任务保持原有 404 语义
    task = await task_service.get_task_by_id(task_id)
    if task is None:
        raise BusinessException(
            code=ErrorCode.NOT_FOUND, message=f"任务不存在：{task_id}", http_status=404
        )
    assert_task_access(user, task, action="trigger_review")

    outcome = await orchestration_service.trigger_review(
        task_id=task_id,
        object_key=body.object_key or "",
        source_type=body.source_type or "digital",
        file_name=body.file_name or "",
    )

    return success_response(
        data=ReviewTriggerResponse(
            task_id=task_id,
            accepted=outcome.accepted,
            message=outcome.message,
        ).model_dump(),
        message="审查完成" if outcome.accepted else "审查失败",
        request_id=get_current_request_id(),
    )


@router.get("/{task_id}/result", summary="审查结果查询", description="查询指定任务的审查结果（风险项/结论/等级分布）。")
async def review_result(
    task_id: str,
    user: CurrentUser = Depends(get_current_user),
) -> ApiResponse:
    """
    审查结果查询

    入参：
        task_id: 任务 ID（路径参数）
        user: 当前登录用户（用于任务归属校验）
    出参：
        data: ReviewResultResponse（风险项列表/结论/等级分布）

    权限：
        任务提交人本人，或拥有 task:view_all 的法务/管理员。
        其他用户返回 403 —— 审查结论含合同风险明细，属敏感数据，
        不能因为"知道 task_no"就允许读取（水平越权防护）。
    """
    from app.services import task_service

    task = await task_service.get_task_by_id(task_id)
    if task is None:
        raise BusinessException(
            code=ErrorCode.NOT_FOUND, message=f"任务不存在：{task_id}", http_status=404
        )

    # 【红线】归属校验必须在读取结论之前：
    # 审查结论 / 风险项属敏感数据，先查后拒同样构成信息泄露。
    assert_task_access(user, task, action="view_review_result")

    # 统一取任务真实 _id：入参可能是 task_no（前端工作台以编号调用），
    # 而结论/风险集合以 _id 为归属键，必须用 canonical_id 查询，否则查空。
    canonical_id = str(task.id)

    # 审查结论从 cr_review_conclusion 集合读取（规则引擎产出）
    from app.constants import MongoCollection
    from app.storage.mongo_client import mongo_client

    collection = mongo_client.get_collection(MongoCollection.REVIEW_CONCLUSION)
    raw = await collection.find_one({"task_id": canonical_id})

    rule_evaluated = raw is not None
    risk_level = raw.get("risk_level") if raw else None
    is_blocked = raw.get("is_blocked", False) if raw else False
    conclusion_text = raw.get("conclusion", "") if raw else ""
    risk_distribution = raw.get("risk_distribution", {}) if raw else {}

    # 风险清单从 cr_review_risk_hit 集合读取（规则引擎命中的正式风险项）。
    # 说明：此处只回传规则引擎判定项（hit_source == rule_engine）；
    #      大模型/向量召回的候选记录仅作参考线索，不进入正式风险清单。
    risk_collection = mongo_client.get_collection(MongoCollection.REVIEW_RISK_HIT)
    risk_items = []
    cursor = risk_collection.find(
        {"task_id": canonical_id, "hit_source": "rule_engine"}
    ).sort("risk_id", 1)
    async for item in cursor:
        risk_items.append(RiskItemResponse(
            risk_id=item.get("risk_id", ""),
            rule_code=item.get("rule_code"),
            rule_name=item.get("rule_name", ""),
            clause_type=item.get("clause_type"),
            risk_level=item.get("risk_level"),
            decision=item.get("decision", ""),
            severity=item.get("severity"),
            evidence_text=item.get("evidence_text", ""),
            suggestion_text=item.get("suggestion_text", ""),
            legal_basis=item.get("legal_basis", ""),
            char_start=item.get("char_start"),
            char_end=item.get("char_end"),
            page_no=item.get("page_no"),
        ).model_dump())

    result = ReviewResultResponse(
        task_id=canonical_id,
        rule_evaluated=rule_evaluated,
        risk_level=risk_level,
        is_blocked=is_blocked,
        risk_distribution=risk_distribution,
        conclusion_text=conclusion_text,
        risk_items=risk_items,
        merge_summary="",
    )
    return success_response(data=result.model_dump(), message="审查结果查询成功",
                            request_id=get_current_request_id())


@router.put("/risk-items/{risk_id}", summary="人工编辑风险项",
            description="人工修改风险项的建议与法律依据（不允许修改风险等级等判定字段）。",
            dependencies=[Depends(require_permissions(PERM_RISK_ITEM_EDIT))])
async def edit_risk_item(risk_id: str, body: RiskItemEditRequest) -> ApiResponse:
    """
    人工编辑风险项

    【权责红线】仅允许修改 suggestion_text / legal_basis 建议类字段，
    不允许修改 risk_level / decision / severity 判定字段。

    【权限】需 risk_item:edit（法务审查人 / 系统管理员）。
    业务经办人无该权限点 → 403。风险项编辑属法务复核职责，
    经办人的职责边界止于"提交合同"与"查看报告"。

    入参：
        risk_id: 风险项 ID（路径参数）
        body: RiskItemEditRequest
    出参：
        data: RiskItemEditResponse
    """
    from app.constants import MongoCollection
    from app.storage.mongo_client import mongo_client

    collection = mongo_client.get_collection(MongoCollection.REVIEW_RISK_HIT)
    raw = await collection.find_one({"risk_id": risk_id})
    if raw is None:
        raise BusinessException(
            code=ErrorCode.NOT_FOUND, message=f"风险项不存在：{risk_id}", http_status=404
        )

    updates = {}
    if body.suggestion_text is not None:
        updates["suggestion_text"] = body.suggestion_text
    if body.legal_basis is not None:
        updates["legal_basis"] = body.legal_basis

    if updates:
        await collection.update_one({"risk_id": risk_id}, {"$set": updates})

    return success_response(
        data=RiskItemEditResponse(risk_id=risk_id, updated=True,
                                  message="风险项已更新").model_dump(),
        message="风险项编辑成功",
        request_id=get_current_request_id(),
    )


@router.put("/risk-items/{risk_id}/recommend", summary="人工修改推荐条款",
            description="人工修订示范修改条款文本。",
            dependencies=[Depends(require_permissions(PERM_RISK_ITEM_EDIT))])
async def update_recommend(risk_id: str, body: ClauseRecommendRequest) -> ApiResponse:
    """
    人工修改推荐条款

    【权限】需 risk_item:edit（法务审查人 / 系统管理员）；
    业务经办人返回 403（推荐条款属法务复核产出）。

    入参：
        risk_id: 风险项 ID（路径参数）
        body: ClauseRecommendRequest（recommended_text）
    出参：
        data: ClauseRecommendResponse
    """
    from app.constants import MongoCollection
    from app.storage.mongo_client import mongo_client

    collection = mongo_client.get_collection(MongoCollection.REVIEW_RISK_HIT)
    raw = await collection.find_one({"risk_id": risk_id})
    if raw is None:
        raise BusinessException(
            code=ErrorCode.NOT_FOUND, message=f"风险项不存在：{risk_id}", http_status=404
        )

    await collection.update_one(
        {"risk_id": risk_id}, {"$set": {"suggestion_text": body.recommended_text}}
    )

    return success_response(
        data=ClauseRecommendResponse(risk_id=risk_id, updated=True,
                                     message="推荐条款已更新").model_dump(),
        message="推荐条款修改成功",
        request_id=get_current_request_id(),
    )
