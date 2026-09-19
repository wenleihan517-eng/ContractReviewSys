# D:/ContractReviewSys-合同审批智能审查项目/backend/app/storage/mongo_client.py
"""
MongoDB 连接管理器 — 异步驱动 Motor

本模块职责：
    1. 基于 lifespan 生命周期，在应用启动时建立 MongoDB 连接池
    2. 在应用关闭时释放连接
    3. 提供连接健康检查
    4. 提供数据库对象与集合对象的获取入口
    5. 提供必要的索引初始化（保证查询性能与唯一约束）

设计要点：
    - 使用 motor.motor_asyncio 异步驱动，避免阻塞 FastAPI 事件循环
    - 全局单例，进程内只维护一个连接池
    - 所有异常统一转换为 MongoDBException，携带上下文便于排查

返回值说明：
    - MongoClient.get_client()：返回 AsyncIOMotorClient 或 None
    - MongoClient.get_database()：返回 AsyncIOMotorDatabase
    - MongoClient.get_collection(name)：返回 AsyncIOMotorCollection
    - MongoClient.ping()：返回 bool 表示连通性

使用示例：
    await mongodb_manager.connect()
    coll = mongodb_manager.get_collection(MongoCollection.CONTRACT)
"""

from typing import Any, List, Optional, Tuple

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorCollection, AsyncIOMotorDatabase
from pymongo import ASCENDING, DESCENDING, IndexModel
from pymongo.errors import PyMongoError

from app.core.config import settings
from app.constants import MongoCollection
from app.core.exceptions import MongoDBException
from app.core.logging_config import get_logger

logger = get_logger(__name__)


class MongoClient:
    """
    MongoDB 异步连接管理器

    属性：
        _client: Motor 异步客户端实例，未连接时为 None
    """

    def __init__(self) -> None:
        # 客户端持有，None 表示尚未初始化
        self._client: Optional[AsyncIOMotorClient] = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def connect(self) -> None:
        """
        建立 MongoDB 连接

        说明：
            在 FastAPI lifespan 启动阶段调用。
            连接建立后立即执行 ping 验证连通性，避免"假连接"。
            初始化完成后自动创建所需索引。

        返回：
            无返回值

        异常：
            MongoDBException：连接失败或 ping 不通时抛出
        """
        try:
            logger.info(
                "正在连接 MongoDB | 地址=%s | 数据库=%s",
                settings.MONGO_URI,
                settings.MONGO_DB_NAME,
            )

            self._client = AsyncIOMotorClient(
                settings.MONGO_URI,
                maxPoolSize=settings.MONGO_MAX_POOL_SIZE,
                minPoolSize=settings.MONGO_MIN_POOL_SIZE,
                serverSelectionTimeoutMS=settings.MONGO_SERVER_SELECTION_TIMEOUT_MS,
                # 统一使用 UTC 时间，避免时区混乱
                tz_aware=True,
            )

            # 主动 ping 验证连通性，失败立即暴露
            await self.ping()

            # 建立索引，保证核心查询性能
            await self._ensure_indexes()

            logger.info("MongoDB 连接成功 | 数据库=%s", settings.MONGO_DB_NAME)

        except PyMongoError as exc:
            logger.error("MongoDB 连接失败 | 异常=%s", exc, exc_info=True)
            raise MongoDBException(
                message="MongoDB 连接失败，请检查服务状态与连接配置",
                detail=str(exc),
            ) from exc

    async def close(self) -> None:
        """
        关闭 MongoDB 连接

        说明：在 FastAPI lifespan 关闭阶段调用，释放连接池资源。

        返回：
            无返回值
        """
        if self._client is not None:
            self._client.close()
            self._client = None
            logger.info("MongoDB 连接已关闭")

    async def ping(self) -> bool:
        """
        检查 MongoDB 连通性

        返回：
            True 表示连通，False 表示不可达

        异常：
            MongoDBException：客户端未初始化时抛出
        """
        if self._client is None:
            raise MongoDBException(message="MongoDB 客户端尚未初始化，无法执行 ping")

        try:
            await self._client.admin.command("ping")
            return True
        except PyMongoError as exc:
            logger.error("MongoDB ping 失败 | 异常=%s", exc)
            return False

    # ------------------------------------------------------------------
    # 对象获取
    # ------------------------------------------------------------------
    def get_client(self) -> Optional[AsyncIOMotorClient]:
        """
        获取原始客户端对象

        返回：
            AsyncIOMotorClient 实例；未初始化时返回 None
        """
        return self._client

    def get_database(self) -> AsyncIOMotorDatabase:
        """
        获取本项目数据库对象

        返回：
            AsyncIOMotorDatabase 实例

        异常：
            MongoDBException：客户端未初始化时抛出
        """
        if self._client is None:
            raise MongoDBException(message="MongoDB 客户端尚未初始化")
        return self._client[settings.MONGO_DB_NAME]

    def get_collection(self, collection_name: str) -> AsyncIOMotorCollection:
        """
        获取指定集合对象

        参数：
            collection_name: 集合名，建议取自 MongoCollection 常量表

        返回：
            AsyncIOMotorCollection 实例

        异常：
            MongoDBException：客户端未初始化时抛出
        """
        return self.get_database()[collection_name]

    # ------------------------------------------------------------------
    # 索引初始化
    # ------------------------------------------------------------------
    async def _ensure_indexes(self) -> None:
        """
        创建业务所需索引

        说明：
            索引创建具备幂等性，重复执行不会报错。
            此处登记的是核心查询路径依赖的索引，业务模块新增查询模式时同步补充。

        返回：
            无返回值
        """
        # 索引登记表：集合名 -> 索引模型列表
        index_plan: List[Tuple[str, List[IndexModel]]] = [
            # 合同主档：合同编号唯一，支持按状态、上传时间检索
            (
                MongoCollection.CONTRACT,
                [
                    IndexModel([("contract_no", ASCENDING)], unique=True, name="uk_contract_no"),
                    IndexModel([("status", ASCENDING)], name="idx_status"),
                    IndexModel([("created_at", DESCENDING)], name="idx_created_at"),
                    IndexModel([("file_hash", ASCENDING)], name="idx_file_hash"),
                ],
            ),
            # 合同条款：按合同 ID + 条款序号检索，支持按类型过滤
            (
                MongoCollection.CONTRACT_CLAUSE,
                [
                    IndexModel(
                        [("contract_id", ASCENDING), ("clause_index", ASCENDING)],
                        unique=True,
                        name="uk_contract_clause_index",
                    ),
                    IndexModel([("clause_type", ASCENDING)], name="idx_clause_type"),
                ],
            ),
            # OCR 结果：按合同 ID + 页码检索
            (
                MongoCollection.OCR_RESULT,
                [
                    IndexModel(
                        [("contract_id", ASCENDING), ("page_no", ASCENDING)],
                        name="idx_ocr_contract_page",
                    ),
                ],
            ),
            # 审查任务：按合同 ID 与状态检索
            (
                MongoCollection.REVIEW_TASK,
                [
                    IndexModel([("contract_id", ASCENDING)], name="idx_task_contract"),
                    IndexModel([("status", ASCENDING)], name="idx_task_status"),
                    IndexModel([("created_at", DESCENDING)], name="idx_task_created_at"),
                ],
            ),
            # 风险命中：按任务 ID + 风险等级检索
            (
                MongoCollection.REVIEW_RISK_HIT,
                [
                    IndexModel([("task_id", ASCENDING)], name="idx_hit_task"),
                    IndexModel([("risk_level", ASCENDING)], name="idx_hit_risk_level"),
                    IndexModel(
                        [("hit_source", ASCENDING)], name="idx_hit_source"
                    ),  # 区分 llm_assistant / rule_engine
                ],
            ),
            # 审查结论：任务 ID 唯一，保证一个任务只有一份最终结论
            (
                MongoCollection.REVIEW_CONCLUSION,
                [
                    IndexModel([("task_id", ASCENDING)], unique=True, name="uk_conclusion_task"),
                    IndexModel([("final_risk_level", ASCENDING)], name="idx_conclusion_level"),
                    IndexModel([("is_blocked", ASCENDING)], name="idx_conclusion_blocked"),
                ],
            ),
            # 规则模板：规则编码唯一，支持按状态与条款类型检索
            (
                MongoCollection.RULE_TEMPLATE,
                [
                    IndexModel([("rule_code", ASCENDING)], unique=True, name="uk_rule_code"),
                    IndexModel([("status", ASCENDING)], name="idx_rule_status"),
                    IndexModel([("clause_type", ASCENDING)], name="idx_rule_clause_type"),
                ],
            ),
            # 规则命中审计日志：按任务与规则检索，保证判定可追溯
            (
                MongoCollection.RULE_HIT_LOG,
                [
                    IndexModel([("task_id", ASCENDING)], name="idx_rulelog_task"),
                    IndexModel([("rule_code", ASCENDING)], name="idx_rulelog_rule"),
                    IndexModel([("created_at", DESCENDING)], name="idx_rulelog_created_at"),
                ],
            ),
            # 修改建议：按任务与条款检索
            (
                MongoCollection.SUGGESTION,
                [
                    IndexModel([("task_id", ASCENDING)], name="idx_sug_task"),
                    IndexModel(
                        [("clause_id", ASCENDING)], name="idx_sug_clause"
                    ),
                    IndexModel(
                        [("is_llm_draft", ASCENDING)], name="idx_sug_is_llm_draft"
                    ),  # 标记草稿来源，便于区分模型产出
                ],
            ),
            # 审批记录：按合同与审批时间检索
            (
                MongoCollection.APPROVAL_RECORD,
                [
                    IndexModel([("contract_id", ASCENDING)], name="idx_approval_contract"),
                    IndexModel([("created_at", DESCENDING)], name="idx_approval_created_at"),
                ],
            ),
            # 回写日志：按状态检索，便于重试失败回写
            (
                MongoCollection.WRITE_BACK_LOG,
                [
                    IndexModel([("status", ASCENDING)], name="idx_wb_status"),
                    IndexModel([("contract_id", ASCENDING)], name="idx_wb_contract"),
                ],
            ),
            # 报告记录：按任务与状态检索
            (
                MongoCollection.REPORT,
                [
                    IndexModel([("task_id", ASCENDING)], name="idx_report_task"),
                    IndexModel([("status", ASCENDING)], name="idx_report_status"),
                ],
            ),
            # 文件对象元数据：按对象键唯一
            (
                MongoCollection.FILE_OBJECT,
                [
                    IndexModel([("object_key", ASCENDING)], unique=True, name="uk_object_key"),
                    IndexModel([("contract_id", ASCENDING)], name="idx_file_contract"),
                ],
            ),
            # 系统配置：配置键唯一
            (
                MongoCollection.SYS_CONFIG,
                [
                    IndexModel([("config_key", ASCENDING)], unique=True, name="uk_config_key"),
                ],
            ),
        ]

        db = self.get_database()
        for collection_name, indexes in index_plan:
            await db[collection_name].create_indexes(indexes)
            logger.debug("集合索引已就绪 | 集合=%s | 索引数=%d", collection_name, len(indexes))

        logger.info("MongoDB 索引初始化完成 | 集合数=%d", len(index_plan))


# 全局单例，业务代码通过依赖注入取用
mongo_client = MongoClient()
