# D:/ContractReviewSys-合同审批智能审查项目/backend/app/api/v1/endpoints/health.py
"""
健康检查接口

本模块职责：
    1. 提供存活探针（liveness）：仅检查进程是否存活，不触碰外部依赖
    2. 提供就绪探针（readiness）：逐一检查 MongoDB / MinIO / Milvus / LLM / Embedding 连通性
    3. 提供配置自检接口：回显关键配置项（脱敏），便于部署排错

设计要点：
    - 存活探针必须极轻量，不能被下游依赖拖死
    - 就绪探针并发检查各依赖，缩短总耗时
    - 配置自检接口对敏感字段做掩码处理，禁止明文回显密钥

返回值说明：
    统一使用 ApiResponse 结构返回
"""

import asyncio
from fastapi import APIRouter

from app.core.config import settings
from app.core.dependencies import get_current_request_id, get_permission_matrix
from app.core.logging_config import get_logger
from app.core.response import ApiResponse, success_response
from app.utils.approval_guard import ALLOWED_APPROVAL_APIS, FORBIDDEN_APPROVAL_APIS

logger = get_logger(__name__)

# 路由对象，统一由 v1 路由聚合器注册
router = APIRouter(tags=["系统-健康检查"])


def _mask_secret(value: str, keep: int = 3) -> str:
    """
    对敏感字符串做掩码处理

    参数：
        value: 原始敏感字符串
        keep: 保留的首部字符数

    返回：
        掩码后的字符串，如 "min***"
    """
    if not value:
        return ""
    if len(value) <= keep:
        return "*" * len(value)
    return value[:keep] + "*" * (len(value) - keep)


@router.get(
    "/health",
    summary="存活探针",
    description="检查服务进程是否存活。不触碰任何外部依赖，供容器 livenessProbe 使用。",
)
async def health() -> ApiResponse:
    """
    存活检查

    返回：
        统一响应体，data 含应用名、版本、环境、存活标记
    """
    return success_response(
        data={
            "status": "alive",
            "app_name": settings.APP_NAME,
            "version": settings.APP_VERSION,
            "env": settings.APP_ENV,
        },
        message="服务存活",
        request_id=get_current_request_id(),
    )


@router.get(
    "/ready",
    summary="就绪探针",
    description="检查全部外部依赖连通性。全部可用才返回就绪，供容器 readinessProbe 使用。",
)
async def ready() -> ApiResponse:
    """
    就绪检查

    说明：
        并发检查 MongoDB / MinIO / Milvus / LLM / Embedding 五类依赖。
        使用 asyncio.gather 并发执行，避免串行等待放大耗时。

    返回：
        统一响应体，data 含各依赖的检查结果与总体就绪状态
    """
    # 延迟导入，避免应用启动阶段即触发依赖初始化
    from app.storage import milvus_manager, minio_client, mongo_client
    from app.engines import embedding_client, llm_client

    async def _check_mongo() -> bool:
        """检查 MongoDB 连通性"""
        try:
            return await mongo_client.ping()
        except Exception as exc:  # noqa: BLE001
            logger.error("就绪检查-MongoDB 异常 | %s", exc)
            return False

    async def _check_minio() -> bool:
        """检查 MinIO 连通性"""
        try:
            return await minio_client.health_check()
        except Exception as exc:  # noqa: BLE001
            logger.error("就绪检查-MinIO 异常 | %s", exc)
            return False

    async def _check_milvus() -> bool:
        """检查 Milvus 连通性"""
        try:
            return await milvus_manager.health_check()
        except Exception as exc:  # noqa: BLE001
            logger.error("就绪检查-Milvus 异常 | %s", exc)
            return False

    async def _check_llm() -> bool:
        """检查本地大模型服务连通性"""
        try:
            return await llm_client.health_check()
        except Exception as exc:  # noqa: BLE001
            logger.error("就绪检查-LLM 异常 | %s", exc)
            return False

    async def _check_embedding() -> bool:
        """检查向量化服务连通性"""
        try:
            return await embedding_client.health_check()
        except Exception as exc:  # noqa: BLE001
            logger.error("就绪检查-Embedding 异常 | %s", exc)
            return False

    # 并发执行全部检查
    mongo_ok, minio_ok, milvus_ok, llm_ok, emb_ok = await asyncio.gather(
        _check_mongo(),
        _check_minio(),
        _check_milvus(),
        _check_llm(),
        _check_embedding(),
    )

    dependencies = {
        "mongodb": mongo_ok,
        "minio": minio_ok,
        "milvus": milvus_ok,
        "llm": llm_ok,
        "embedding": emb_ok,
    }

    # 全部依赖可用才视为就绪
    all_ready = all(dependencies.values())

    return success_response(
        data={
            "ready": all_ready,
            "dependencies": dependencies,
        },
        message="服务就绪" if all_ready else "部分依赖不可用，请检查",
        request_id=get_current_request_id(),
    )


@router.get(
    "/config-check",
    summary="配置自检",
    description="回显关键配置项（敏感字段已掩码），用于部署阶段快速排错。",
)
async def config_check() -> ApiResponse:
    """
    配置自检查

    说明：
        回显连接地址、库名、模型名等非敏感配置，密钥类字段做掩码处理，
        帮助运维确认服务实际加载的配置是否与预期一致。

    返回：
        统一响应体，data 为脱敏后的配置摘要
    """
    return success_response(
        data={
            "app": {
                "name": settings.APP_NAME,
                "version": settings.APP_VERSION,
                "env": settings.APP_ENV,
                "debug": settings.APP_DEBUG,
                "api_prefix": settings.API_V1_PREFIX,
            },
            "mongodb": {
                "uri": settings.MONGO_URI,
                "db_name": settings.MONGO_DB_NAME,
            },
            "minio": {
                "endpoint": settings.MINIO_ENDPOINT,
                "secure": settings.MINIO_SECURE,
                "access_key": _mask_secret(settings.MINIO_ACCESS_KEY),
                "buckets": settings.all_minio_buckets,
            },
            "milvus": {
                "uri": settings.MILVUS_URI,
                "db_name": settings.MILVUS_DB_NAME,
                "collection_clause": settings.MILVUS_COLLECTION_CLAUSE,
                "collection_standard_clause": settings.MILVUS_COLLECTION_STANDARD_CLAUSE,
                "collection_rule_template": settings.MILVUS_COLLECTION_RULE_TEMPLATE,
                "index_type": settings.MILVUS_INDEX_TYPE,
                "metric_type": settings.MILVUS_METRIC_TYPE,
            },
            "embedding": {
                "base_url": settings.EMBEDDING_BASE_URL,
                "model_name": settings.EMBEDDING_MODEL_NAME,
                "dim": settings.EMBEDDING_DIM,
            },
            "llm": {
                "base_url": settings.LLM_BASE_URL,
                "model_name": settings.LLM_MODEL_NAME,
                "temperature": settings.LLM_TEMPERATURE,
                "api_key": _mask_secret(settings.LLM_API_KEY),
            },
            "rule_engine": {
                "enabled": settings.RULE_ENGINE_ENABLED,
                "version": settings.RULE_ENGINE_VERSION,
                "block_levels": settings.RULE_ENGINE_BLOCK_LEVELS,
                "risk_levels": settings.RULE_RISK_LEVELS,
                "semantic_default_threshold": settings.RULE_SEMANTIC_DEFAULT_THRESHOLD,
            },
            "routing": {
                "fast_enabled": settings.ROUTE_FAST_ENABLED,
                "deep_amount_threshold": settings.ROUTE_DEEP_AMOUNT_THRESHOLD,
                "deep_require_legal": settings.ROUTE_DEEP_REQUIRE_LEGAL,
                "deep_force_types": settings.deep_force_type_list,
            },
            "task": {
                "max_retry": settings.TASK_MAX_RETRY,
                "retry_base_delay": settings.TASK_RETRY_BASE_DELAY,
                "retry_max_delay": settings.TASK_RETRY_MAX_DELAY,
                "max_concurrency": settings.TASK_MAX_CONCURRENCY,
                "tmp_retention_days": settings.TASK_TMP_RETENTION_DAYS,
            },
            "parsing": {
                "ocr_engine": settings.OCR_ENGINE,
                "ocr_lang": settings.OCR_LANG,
                "ocr_min_confidence_threshold": settings.OCR_MIN_CONFIDENCE_THRESHOLD,
                "min_content_length": settings.MIN_CONTENT_LENGTH,
                "upload_max_size_mb": settings.UPLOAD_MAX_SIZE_MB,
            },
            "quality": {
                "max_fix_rounds": settings.QUALITY_MAX_FIX_ROUNDS,
                "preconstraint_enabled": settings.QUALITY_PRECONSTRAINT_ENABLED,
                "postcheck_enabled": settings.QUALITY_POSTCHECK_ENABLED,
                "suggestion_max_length": settings.SUGGESTION_MAX_LENGTH,
            },
            "approval": {
                "base_url": settings.APPROVAL_BASE_URL,
                "comment_url": settings.approval_comment_url,
                "api_key": _mask_secret(settings.APPROVAL_API_KEY),
                "max_retries": settings.APPROVAL_MAX_RETRIES,
                # 回显接口白名单与黑名单，便于运维确认红线约束已生效
                "allowed_apis": ALLOWED_APPROVAL_APIS,
                "forbidden_apis": FORBIDDEN_APPROVAL_APIS,
            },
            "auth": {
                "enabled": settings.AUTH_ENABLED,
                "jwt_algorithm": settings.JWT_ALGORITHM,
                "jwt_expire_minutes": settings.JWT_EXPIRE_MINUTES,
                "jwt_refresh_expire_minutes": settings.JWT_REFRESH_EXPIRE_MINUTES,
                "jwt_issuer": settings.JWT_ISSUER,
                "bcrypt_rounds": settings.BCRYPT_ROUNDS,
                "secret_customized": settings.is_auth_ready,
                "max_login_failures": settings.AUTH_MAX_LOGIN_FAILURES,
                "login_lock_minutes": settings.AUTH_LOGIN_LOCK_MINUTES,
                # 回显角色标识与权限矩阵，便于运维核对鉴权配置已正确加载
                "roles": list(settings.role_values),
                "permission_matrix": get_permission_matrix(),
                "whitelist_paths": sorted(settings.auth_whitelist_paths),
            },
        },
        message="配置读取成功",
        request_id=get_current_request_id(),
    )
