# D:/ContractReviewSys-合同审批智能审查项目/backend/app/api/v1/endpoints/rules.py
"""
规则管理接口

【接口清单】
    GET    /rules                        规则列表（按合同类型过滤）
    POST   /rules                        创建规则
    PUT    /rules/{rule_code}            更新规则
    PUT    /rules/{rule_code}/enabled    启用/禁用规则
    DELETE /rules/{rule_code}            删除规则
    GET    /rules/standard-clauses       示范条款库列表
    POST   /rules/standard-clauses       创建示范条款

【设计要点】
    规则是规则引擎的配置来源，接口只做 CRUD，不改变规则引擎的判定逻辑。
"""

from typing import Optional

from fastapi import APIRouter, Query

from app.core.dependencies import get_current_request_id
from app.core.exceptions import BusinessException, ErrorCode
from app.core.logging_config import get_logger
from app.core.response import ApiResponse, page_response, success_response
from app.schemas.rule import (
    RuleEnableRequest,
    RuleEnableResponse,
    RuleItemResponse,
    RuleUpsertRequest,
    StandardClauseResponse,
    StandardClauseUpsertRequest,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/rules", tags=["规则管理"])


@router.get("", summary="规则列表", description="按合同类型查询规则模板列表（含停用规则）。")
async def list_rules(
    contract_type: Optional[str] = Query(default=None, description="合同类型过滤"),
    page: int = Query(default=1, ge=1, description="页码"),
    page_size: int = Query(default=20, ge=1, le=200, description="每页条数"),
) -> ApiResponse:
    """
    规则列表

    出参：
        data: 分页结果（items 为 RuleItemResponse 列表）
    """
    from app.repositories import rule_template_repo

    items, total = await rule_template_repo.list_rules(
        contract_type=contract_type, skip=(page - 1) * page_size, limit=page_size
    )

    item_list = [
        RuleItemResponse(
            rule_code=r.rule_code, rule_name=r.rule_name, description=r.description,
            match_type=r.match_type, match_config=r.match_config,
            contract_types=r.contract_types, severity=r.severity,
            override_risk_level=r.override_risk_level,
            suggestion_template=r.suggestion_template, legal_basis=r.legal_basis,
            priority=r.priority, enabled=r.is_active, version=r.version,
        ).model_dump()
        for r in items
    ]

    return page_response(
        items=item_list, total=total, page=page, page_size=page_size,
        message="规则列表查询成功", request_id=get_current_request_id(),
    )


@router.post("", summary="创建规则", description="创建规则模板（按 rule_code 幂等）。")
async def create_rule(body: RuleUpsertRequest) -> ApiResponse:
    """
    创建规则

    入参：
        body: RuleUpsertRequest
    出参：
        data: {"rule_code": 规则编码}
    """
    from app.models.documents.rule_template import RuleTemplate
    from app.repositories import rule_template_repo

    rule = RuleTemplate(
        rule_code=body.rule_code,
        rule_name=body.rule_name,
        description=body.description,
        match_type=body.match_type,
        match_config=body.match_config,
        contract_types=body.contract_types,
        severity=body.severity,
        override_risk_level=body.override_risk_level,
        suggestion_template=body.suggestion_template,
        legal_basis=body.legal_basis,
        priority=body.priority,
        status="enabled" if body.enabled else "disabled",
    )

    await rule_template_repo.upsert_rule(rule)
    return success_response(data={"rule_code": body.rule_code}, message="规则创建成功",
                            request_id=get_current_request_id())


@router.put("/{rule_code}", summary="更新规则", description="更新规则模板配置。")
async def update_rule(rule_code: str, body: RuleUpsertRequest) -> ApiResponse:
    """
    更新规则

    入参：
        rule_code: 规则编码（路径参数）
        body: RuleUpsertRequest
    出参：
        data: {"rule_code": 规则编码}
    """
    from app.models.documents.rule_template import RuleTemplate
    from app.repositories import rule_template_repo

    existing = await rule_template_repo.find_rule(rule_code)
    if existing is None:
        raise BusinessException(
            code=ErrorCode.NOT_FOUND, message=f"规则不存在：{rule_code}", http_status=404
        )

    rule = RuleTemplate(
        rule_code=rule_code,
        rule_name=body.rule_name,
        description=body.description,
        match_type=body.match_type,
        match_config=body.match_config,
        contract_types=body.contract_types,
        severity=body.severity,
        override_risk_level=body.override_risk_level,
        suggestion_template=body.suggestion_template,
        legal_basis=body.legal_basis,
        priority=body.priority,
        status="enabled" if body.enabled else "disabled",
        version=existing.version + 1,
    )

    await rule_template_repo.upsert_rule(rule)
    return success_response(data={"rule_code": rule_code}, message="规则更新成功",
                            request_id=get_current_request_id())


@router.put("/{rule_code}/enabled", summary="启用/禁用规则", description="切换规则启用状态。")
async def set_rule_enabled(rule_code: str, body: RuleEnableRequest) -> ApiResponse:
    """
    启用/禁用规则

    入参：
        rule_code: 规则编码（路径参数）
        body: RuleEnableRequest（enabled）
    出参：
        data: RuleEnableResponse
    """
    from app.repositories import rule_template_repo

    ok = await rule_template_repo.set_rule_enabled(rule_code, body.enabled)
    if not ok:
        raise BusinessException(
            code=ErrorCode.NOT_FOUND, message=f"规则不存在：{rule_code}", http_status=404
        )

    return success_response(
        data=RuleEnableResponse(
            rule_code=rule_code, enabled=body.enabled,
            message="规则已启用" if body.enabled else "规则已禁用",
        ).model_dump(),
        message="规则状态更新成功",
        request_id=get_current_request_id(),
    )


@router.delete("/{rule_code}", summary="删除规则", description="删除规则模板。")
async def delete_rule(rule_code: str) -> ApiResponse:
    """
    删除规则

    入参：
        rule_code: 规则编码（路径参数）
    """
    from app.repositories import rule_template_repo

    ok = await rule_template_repo.delete_rule(rule_code)
    if not ok:
        raise BusinessException(
            code=ErrorCode.NOT_FOUND, message=f"规则不存在：{rule_code}", http_status=404
        )
    return success_response(data={"rule_code": rule_code}, message="规则删除成功",
                            request_id=get_current_request_id())


@router.get("/standard-clauses", summary="示范条款库列表", description="查询示范条款库。")
async def list_standard_clauses(
    clause_type: Optional[str] = Query(default=None, description="条款类型过滤"),
    page: int = Query(default=1, ge=1, description="页码"),
    page_size: int = Query(default=20, ge=1, le=200, description="每页条数"),
) -> ApiResponse:
    """
    示范条款库列表

    出参：
        data: 分页结果（items 为 StandardClauseResponse 列表）
    """
    from app.constants import MongoCollection
    from app.storage.mongo_client import mongo_client

    collection = mongo_client.get_collection(MongoCollection.STANDARD_CLAUSE)
    filter_dict = {}
    if clause_type:
        filter_dict["clause_type"] = clause_type

    total = await collection.count_documents(filter_dict)
    cursor = collection.find(filter_dict).skip((page - 1) * page_size).limit(page_size)

    items = []
    async for raw in cursor:
        items.append(
            StandardClauseResponse(
                clause_id=str(raw.get("_id", "")),
                clause_title=raw.get("clause_title", ""),
                clause_text=raw.get("clause_text", ""),
                clause_type=raw.get("clause_type", "other"),
                contract_types=raw.get("contract_types", ["all"]),
            ).model_dump()
        )

    return page_response(
        items=items, total=total, page=page, page_size=page_size,
        message="示范条款库查询成功", request_id=get_current_request_id(),
    )


@router.post("/standard-clauses", summary="创建示范条款", description="创建示范条款。")
async def create_standard_clause(body: StandardClauseUpsertRequest) -> ApiResponse:
    """
    创建示范条款

    入参：
        body: StandardClauseUpsertRequest
    出参：
        data: {"clause_id": 条款 ID}
    """
    from app.constants import MongoCollection
    from app.storage.mongo_client import mongo_client

    collection = mongo_client.get_collection(MongoCollection.STANDARD_CLAUSE)
    result = await collection.insert_one({
        "clause_title": body.clause_title,
        "clause_text": body.clause_text,
        "clause_type": body.clause_type,
        "contract_types": body.contract_types,
    })
    return success_response(data={"clause_id": str(result.inserted_id)},
                            message="示范条款创建成功",
                            request_id=get_current_request_id())
