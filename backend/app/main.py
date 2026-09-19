# D:/ContractReviewSys-合同审批智能审查项目/backend/app/main.py
"""
FastAPI 应用主入口

本模块职责：
    1. 创建 FastAPI 应用实例，配置元信息与文档路径
    2. 注册 lifespan 生命周期：启动时初始化日志 / 存储 / 模型客户端，关闭时逆序释放
    3. 注册跨域中间件与请求链路追踪中间件
    4. 注册全局异常处理器（业务异常 / 参数校验 / HTTP 异常 / 未知异常）
    5. 挂载 v1 路由，并记录启动摘要日志
    6. 提供直接运行入口（python main.py）

【启动顺序（严格不可调换）】
    日志系统 → MongoDB → MinIO → Milvus → LLM 客户端 → Embedding 客户端
    说明：日志必须最先，保证后续所有初始化动作都有记录；存储先于模型，
          避免模型客户端就绪但依赖缺失导致的"半可用"状态。

【失败策略】
    任一依赖初始化失败即中断启动并抛出明确业务异常。
    合同审查场景不允许静默降级——带残缺依赖对外服务会产出不可信的审查结果。

返回值说明：
    - create_app()：返回配置完整的 FastAPI 实例
    - app：模块级应用实例，供 uvicorn 加载（uvicorn app.main:app）

使用示例：
    uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
"""

import time
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Dict

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_router
from app.core.auth_middleware import AuthMiddleware
from app.core.config import settings
from app.core.exceptions import register_exception_handlers
from app.core.logging_config import get_logger, init_logging
from app.core.middleware import RequestIdMiddleware

# 模块级日志器；日志系统在 lifespan 中完成初始化
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    应用生命周期管理器

    说明：
        yield 之前为启动阶段，yield 之后为关闭阶段。
        启动阶段任一依赖失败会向上抛出异常，FastAPI 会中止启动流程。

    参数：
        app: FastAPI 应用实例（未直接使用，遵循框架签名）

    产出：
        None（异步生成器，yield 后进入应用运行期）
    """
    # ==================== 启动阶段 ====================
    startup_begin = time.perf_counter()

    # 步骤 1：初始化日志系统（必须最先，保证后续动作均可被记录）
    init_logging()
    logger.info("=" * 78)
    logger.info(
        "【启动】%s v%s | 环境=%s | 调试=%s",
        settings.APP_NAME,
        settings.APP_VERSION,
        settings.APP_ENV,
        settings.APP_DEBUG,
    )
    logger.info("=" * 78)

    # 步骤 2：初始化存储中间件（MongoDB → MinIO → Milvus）
    from app.storage import init_all_storages

    await init_all_storages()

    # 步骤 3：初始化模型服务客户端（仅建立连接池，不做推理）
    from app.engines import embedding_client, llm_client

    await llm_client.init_client()
    await embedding_client.init_client()

    # 步骤 3.5：初始化鉴权账号（内置演示账号 + 用户集合索引）
    #
    # 说明：
    #   本系统为演示系统，不提供自助注册，仅内置三个测试账号。
    #   采用幂等 upsert（见 user_repo.upsert_builtin_user）：
    #       账号不存在 → 新建并写入初始密码的 bcrypt 哈希
    #       账号已存在 → 只刷新角色/展示名等静态字段，不覆盖已改过的密码
    #   因此重复启动不会把用户改过的密码重置回去。
    #
    #   初始化失败（如 MongoDB 索引冲突）不阻断服务启动——
    #   登录鉴权本身仍可工作，只是缺少演示账号；
    #   若在此处抛出，会导致整个服务无法启动，问题放大。
    if settings.SEED_BUILTIN_USERS:
        try:
            from app.services import auth_service

            seed_result = await auth_service.init_builtin_users()
            logger.info(
                "  内置账号  : 总数=%d / 新建=%d / 已存在=%d",
                seed_result.total,
                seed_result.created,
                seed_result.existing,
            )
        except Exception as exc:  # noqa: BLE001 - 不阻断启动，仅告警
            logger.error("内置账号初始化失败（服务继续启动）| 异常=%s", exc)
    else:
        logger.info("  内置账号  : 已关闭（SEED_BUILTIN_USERS=False）")

    # 步骤 4：输出启动摘要
    elapsed_ms = (time.perf_counter() - startup_begin) * 1000.0
    logger.info("-" * 78)
    logger.info("【启动摘要】")
    logger.info("  监听地址  : http://%s:%d", settings.SERVER_HOST, settings.SERVER_PORT)
    logger.info("  接口前缀  : %s", settings.API_V1_PREFIX)
    logger.info(
        "  接口文档  : %s",
        "已关闭（生产环境）"
        if settings.is_production
        else "http://127.0.0.1:%d/docs" % settings.SERVER_PORT,
    )
    logger.info("  MongoDB   : %s / 库=%s", settings.MONGO_URI, settings.MONGO_DB_NAME)
    logger.info("  MinIO     : %s / 加密=%s", settings.MINIO_ENDPOINT, settings.MINIO_SECURE)
    logger.info("  Milvus    : %s / 库=%s", settings.MILVUS_URI, settings.MILVUS_DB_NAME)
    logger.info("  大模型    : %s / 模型=%s", settings.LLM_BASE_URL, settings.LLM_MODEL_NAME)
    logger.info(
        "  向量化    : %s / %s / %d维",
        settings.EMBEDDING_BASE_URL,
        settings.EMBEDDING_MODEL_NAME,
        settings.EMBEDDING_DIM,
    )
    logger.info(
        "  规则引擎  : 启用=%s / 版本=%s / 阻塞等级=%s",
        settings.RULE_ENGINE_ENABLED,
        settings.RULE_ENGINE_VERSION,
        settings.RULE_ENGINE_BLOCK_LEVELS,
    )
    # 鉴权摘要：明确回显是否启用与密钥是否已替换，避免"以为开了其实没开"
    logger.info(
        "  登录鉴权  : 启用=%s / 角色=%s / 密钥已替换=%s",
        settings.AUTH_ENABLED,
        "/".join(settings.role_values),
        settings.is_auth_ready,
    )
    logger.info("  启动耗时  : %.2f ms", elapsed_ms)
    logger.info("-" * 78)
    logger.info("【启动完成】服务已就绪，等待请求 ...")

    yield  # ← 应用运行期

    # ==================== 关闭阶段 ====================
    logger.info("=" * 78)
    logger.info("【关闭】%s 正在停止服务 ...", settings.APP_NAME)

    # 逆序释放：模型客户端 → 存储中间件
    await llm_client.close()
    await embedding_client.close()

    from app.storage import close_all_storages

    await close_all_storages()

    logger.info("【关闭完成】服务已安全退出")
    logger.info("=" * 78)


def create_app() -> FastAPI:
    """
    创建并配置 FastAPI 应用实例

    职责：
        1. 构建应用实例（标题 / 版本 / 文档路径 / 生命周期 / 文档标签）
        2. 注册中间件（CORS + 请求链路追踪）
        3. 注册全局异常处理器
        4. 挂载 v1 路由与根路径

    返回：
        配置完整的 FastAPI 实例
    """
    # 生产环境关闭交互式文档，避免接口结构外泄
    docs_url = None if settings.is_production else "/docs"
    redoc_url = None if settings.is_production else "/redoc"
    openapi_url = None if settings.is_production else "/openapi.json"

    application = FastAPI(
        title=settings.APP_NAME,
        version=settings.APP_VERSION,
        description=(
            "企业合同智能审查系统后端服务。\n\n"
            "**核心架构约束**：\n"
            "- 大模型仅承担辅助能力：条款语义解析、疑似风险识别（候选）、"
            "修改建议初稿生成、合规解读\n"
            "- 所有正式业务判定由规则引擎兜底：风险等级定级、违规判定、"
            "任务阻塞判定、最终审查结论\n"
            "- 审批回写仅写入审批系统评论区，不改变审批结论\n\n"
            "**技术栈**：Python 3.10 + FastAPI + LangGraph ｜ "
            "MongoDB + MinIO + Milvus ｜ 本地大模型 + BGE-M3"
        ),
        lifespan=lifespan,
        docs_url=docs_url,
        redoc_url=redoc_url,
        openapi_url=openapi_url,
        # 生产环境强制关闭调试，隐藏异常细节
        debug=settings.APP_DEBUG and not settings.is_production,
        openapi_tags=[
            {"name": "认证-登录鉴权", "description": "登录、令牌刷新、登出、当前用户信息、权限校验"},
            {"name": "系统-健康检查", "description": "存活探针、就绪探针、配置自检"},
            {"name": "合同管理", "description": "合同提交、元数据维护、文件资产管理"},
            {"name": "审查任务", "description": "任务创建、状态查询、节点级进度"},
            {"name": "审查工作台", "description": "风险明细、原文高亮定位、建议调整"},
            {"name": "规则模板", "description": "合规规则与标准条款库维护"},
            {"name": "审批回写", "description": "审查意见写入审批系统评论区（不改审批结论）"},
            {"name": "报告导出", "description": "Markdown / PDF 审查报告生成与下载"},
        ],
    )

    # ------------------------------------------------------------------
    # 中间件注册
    # 注意：Starlette 中间件为栈结构，"后注册的先执行"。
    #      因此请求链路追踪后注册，位于最外层，可记录完整耗时。
    #
    # 执行顺序（由外到内）：
    #     RequestIdMiddleware  → 生成/透传 request_id，后续日志均可串联
    #     AuthMiddleware       → 全局鉴权（白名单外一律要求合法令牌）
    #     CORSMiddleware       → 跨域头处理
    #     → 业务路由
    #
    # 【为什么鉴权中间件要在 RequestIdMiddleware 之内、CORS 之外？】
    #   - 在 RequestId 之内：鉴权失败时日志能带上 request_id 便于排查
    #   - 在 CORS 之外（更早执行）：鉴权直接拒绝的请求也能带上跨域头，
    #     否则浏览器会把 401 报成 CORS 错误，严重误导排查方向
    # ------------------------------------------------------------------
    # 跨域中间件（最先注册 → 最后执行，位于最内层）
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=settings.CORS_ALLOW_CREDENTIALS,
        allow_methods=settings.CORS_ALLOW_METHODS,
        allow_headers=settings.CORS_ALLOW_HEADERS,
        # 暴露链路 ID，便于前端在反馈问题时携带定位
        expose_headers=["X-Request-Id"],
    )

    # 全局鉴权中间件（默认拒绝：白名单外的接口一律要求合法访问令牌）
    # 【红线】AUTH_ENABLED=False 时本中间件整体放行，仅用于本地调试。
    #        该开关在生产环境必须为 True（.env 中已显式配置）。
    application.add_middleware(AuthMiddleware)

    # 请求链路追踪中间件（最后注册 → 最先执行，位于最外层）
    application.add_middleware(RequestIdMiddleware)

    # ------------------------------------------------------------------
    # 全局异常处理器注册
    # 覆盖：业务异常 / 请求参数校验失败 / HTTP 异常 / 未知异常
    # ------------------------------------------------------------------
    register_exception_handlers(application)

    # ------------------------------------------------------------------
    # 路由挂载
    # ------------------------------------------------------------------
    application.include_router(api_router, prefix=settings.API_V1_PREFIX)

    # ------------------------------------------------------------------
    # 根路径：服务基础信息
    # ------------------------------------------------------------------
    @application.get("/", include_in_schema=False)
    async def root() -> Dict[str, Any]:
        """
        根路径信息

        返回：
            服务基础信息字典（应用名、版本、环境、文档地址、接口前缀）
        """
        return {
            "app": settings.APP_NAME,
            "version": settings.APP_VERSION,
            "env": settings.APP_ENV,
            "docs": docs_url or "已关闭（生产环境）",
            "api_prefix": settings.API_V1_PREFIX,
            "rule_engine_enabled": settings.RULE_ENGINE_ENABLED,
        }

    return application


# 模块级应用实例，供 uvicorn 以 "app.main:app" 加载
app = create_app()


if __name__ == "__main__":
    # 直接运行入口：python app/main.py
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.SERVER_HOST,
        port=settings.SERVER_PORT,
        reload=settings.SERVER_RELOAD,
        # 热重载模式只能单进程；生产模式使用配置的多进程
        workers=1 if settings.SERVER_RELOAD else settings.SERVER_WORKERS,
        log_level=settings.LOG_LEVEL.lower(),
    )
