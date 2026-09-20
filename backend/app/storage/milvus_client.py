# D:/ContractReviewSys-合同审批智能审查项目/backend/app/storage/milvus_client.py
"""
Milvus 向量库管理器 — 复用现有实例 + 独立 database 隔离

【架构要点】
    1. 复用既有 Milvus 实例（地址由配置中心 MILVUS_HOST/MILVUS_PORT 决定），不新建实例
    2. 本项目使用独立 database（contract_review），与其它项目物理隔离
    3. 集合统一使用 cr_ 前缀，杜绝与外部集合重名
    4. 集合 schema 固定 1024 维（BGE-M3），建集合时强制校验

设计要点：
    - pymilvus 的 MilvusClient 为同步库，网络调用统一放入线程池
    - 建 database / collection 均做幂等判断，可安全重复启动
    - 索引与检索参数全部取自配置中心，不硬编码
    - 【pymilvus 3.x 兼容】库级管理能力（list_databases/create_database）已由
      ORM 的 utility 模块迁移至 MilvusClient，本文件不使用任何旧 ORM API

返回值说明：
    - MilvusClientManager.get_client()：返回 MilvusClient 或 None
    - ensure_collections()：无返回值，副作用为建库建集合
    - health_check()：返回 bool

使用示例：
    await milvus_manager.connect()
    client = milvus_manager.get_client()
"""

import asyncio
from typing import Any, Dict, List, Optional

from pymilvus import (
    CollectionSchema,
    DataType,
    FieldSchema,
    MilvusClient,
)
from pymilvus.exceptions import MilvusException as PyMilvusException

from app.core.config import settings
from app.core.exceptions import MilvusException
from app.core.logging_config import get_logger

logger = get_logger(__name__)


class MilvusClientManager:
    """
    Milvus 向量库管理器

    属性：
        _client: MilvusClient 实例，未初始化时为 None
    """

    def __init__(self) -> None:
        self._client: Optional[MilvusClient] = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def connect(self) -> None:
        """
        连接 Milvus 并初始化独立 database 与集合

        说明：
            在 FastAPI lifespan 启动阶段调用。
            连接后立即创建项目专属 database（若不存在），
            再切换到该 database 建集合，全程不触碰 default 库。

        返回：
            无返回值

        异常：
            MilvusException：连接失败或初始化失败时抛出
        """
        try:
            logger.info(
                "正在连接 Milvus | 地址=%s | 目标库=%s",
                settings.MILVUS_URI,
                settings.MILVUS_DB_NAME,
            )

            # 第一步：先建一个"管理连接"（不绑定任何 database，落在 default 库），
            #         仅用于执行 create_database / list_databases。
            #
            # 【pymilvus 3.x 适配说明】
            #   旧写法使用 ORM 风格的 connections.connect(alias=...) + utility.list_databases()，
            #   但 pymilvus 3.x 已移除 utility 模块下的数据库级 API。
            #   3.x 中数据库管理能力统一收敛到 MilvusClient（list_databases / create_database）。
            #   这里刻意不传 db_name，让客户端落在服务端默认库（仅用于建库，不承载业务数据），
            #   建库完成后再构造绑定业务库的客户端，实现"绝不把业务集合建到 default 库"的约束。
            admin_client = await asyncio.to_thread(
                MilvusClient,
                uri=settings.MILVUS_URI,
                timeout=settings.MILVUS_TIMEOUT_SECONDS,
            )

            # 第二步：创建本项目独立 database（幂等）
            await self._ensure_database(admin_client)

            # 第三步：创建 MilvusClient（指定本项目 database，实现数据隔离）
            self._client = await asyncio.to_thread(
                MilvusClient,
                uri=settings.MILVUS_URI,
                db_name=settings.MILVUS_DB_NAME,
                timeout=settings.MILVUS_TIMEOUT_SECONDS,
            )

            # 第四步：创建业务集合
            await self.ensure_collections()

            logger.info(
                "Milvus 连接成功 | 库=%s | 集合已就绪", settings.MILVUS_DB_NAME
            )

        except PyMilvusException as exc:
            logger.error("Milvus 连接失败 | 异常=%s", exc, exc_info=True)
            raise MilvusException(
                message="Milvus 连接失败，请检查服务状态与网络连通性",
                detail=str(exc),
            ) from exc

    async def close(self) -> None:
        """
        关闭 Milvus 连接

        说明：
            pymilvus 3.x 不再需要显式断开 ORM 连接别名（connections.disconnect 已废弃），
            MilvusClient 自带连接池，close() 即可释放。

        返回：
            无返回值
        """
        try:
            if self._client is not None:
                await asyncio.to_thread(self._client.close)
                self._client = None
            logger.info("Milvus 连接已关闭")
        except Exception as exc:  # noqa: BLE001 - 关闭失败不阻断退出流程
            logger.warning("关闭 Milvus 连接异常 | 异常=%s", exc)

    def get_client(self) -> Optional[MilvusClient]:
        """
        获取 Milvus 客户端

        返回：
            MilvusClient 实例（已绑定本项目 database）；未初始化时返回 None
        """
        return self._client

    def _require_client(self) -> MilvusClient:
        """
        获取客户端并校验已初始化

        返回：
            MilvusClient 实例

        异常：
            MilvusException：客户端未初始化时抛出
        """
        if self._client is None:
            raise MilvusException(message="Milvus 客户端尚未初始化")
        return self._client

    # ------------------------------------------------------------------
    # 数据库初始化
    # ------------------------------------------------------------------
    async def _ensure_database(self, admin_client: MilvusClient) -> None:
        """
        创建本项目独立 database（幂等）

        参数：
            admin_client: 未绑定业务库的管理客户端，用于执行建库/列举库操作。
                          使用独立入参而非 self._client，是因为此刻业务库尚不存在，
                          绑定业务库的客户端还无法建立。

        说明：
            通过 admin_client.list_databases() 判断是否已存在，
            不存在则创建。绝不使用 default 库存放业务集合，确保与其它项目隔离。

        返回：
            无返回值

        异常：
            MilvusException：建库失败时抛出
        """
        try:
            existing_dbs: List[str] = await asyncio.to_thread(
                admin_client.list_databases
            )

            if settings.MILVUS_DB_NAME in existing_dbs:
                logger.info("Milvus 数据库已存在 | 库=%s", settings.MILVUS_DB_NAME)
                return

            await asyncio.to_thread(
                admin_client.create_database, settings.MILVUS_DB_NAME
            )
            logger.info("Milvus 数据库已创建 | 库=%s", settings.MILVUS_DB_NAME)

        except PyMilvusException as exc:
            logger.error("Milvus 数据库创建失败 | 库=%s | 异常=%s", settings.MILVUS_DB_NAME, exc)
            raise MilvusException(
                message="Milvus 独立数据库创建失败",
                detail=f"db={settings.MILVUS_DB_NAME}, error={exc}",
            ) from exc
        finally:
            # 管理客户端用完即关，避免长期占用一条连接
            try:
                await asyncio.to_thread(admin_client.close)
            except Exception as exc:  # noqa: BLE001 - 关闭失败不影响主流程
                logger.debug("管理客户端关闭异常 | 异常=%s", exc)

    # ------------------------------------------------------------------
    # 集合初始化
    # ------------------------------------------------------------------
    async def ensure_collections(self) -> None:
        """
        创建业务所需集合（幂等）

        说明：
            当前创建三类集合：
                1. cr_clauses              — 条款向量（相似条款召回、重复条款检测）
                2. cr_standard_clause_lib  — 标准条款库向量（推荐条款检索数据源）
                3. cr_rule_templates       — 规则模板向量（语义规则匹配召回）
            【2026-09-19 修复】此前 collection_plan 漏配 standard_clause_lib，
            导致 vector_review 节点检索时 MilvusException(code=100 collection not found)，
            全部任务在向量召回阶段重试耗尽后阻塞。
            集合不存在时创建，已存在则跳过。
            建成后自动加载（load）到内存，保证检索可用。

        返回：
            无返回值

        异常：
            MilvusException：建集合失败时抛出
        """
        client = self._require_client()

        collection_plan: Dict[str, str] = {
            settings.MILVUS_COLLECTION_CLAUSE: "条款向量集合，存放条款文本的 BGE-M3 向量",
            settings.MILVUS_COLLECTION_STANDARD_CLAUSE: "标准条款库向量集合，条款推荐数据源",
            settings.MILVUS_COLLECTION_RULE_TEMPLATE: "规则模板向量集合，存放规则描述的向量",
        }

        for collection_name, description in collection_plan.items():
            # 判断集合是否已存在
            exists = await asyncio.to_thread(client.has_collection, collection_name)
            if exists:
                logger.info("Milvus 集合已存在 | 集合=%s", collection_name)
            else:
                await self._create_collection(client, collection_name, description)
                logger.info("Milvus 集合已创建 | 集合=%s | 说明=%s", collection_name, description)

            # 加载集合到内存，保证可检索（幂等操作）
            try:
                await asyncio.to_thread(client.load_collection, collection_name)
            except PyMilvusException as exc:
                # 已加载时重复 load 可能报错，此处仅记录告警
                logger.warning("集合加载告警 | 集合=%s | 异常=%s", collection_name, exc)

    async def _create_collection(
        self,
        client: MilvusClient,
        collection_name: str,
        description: str,
    ) -> None:
        """
        创建单个集合（含 schema 与索引）

        参数：
            client: Milvus 客户端
            collection_name: 集合名
            description: 集合用途说明，写入集合元信息

        返回：
            无返回值

        异常：
            MilvusException：创建失败时抛出
        """
        try:
            # 字段定义：
            #   id          主键，VarChar(64)，存业务侧生成的 UUID
            #   contract_id 合同 ID，用于按合同过滤（VarChar(64)）
            #   clause_id   条款 ID，与 MongoDB 条款记录一一对应（VarChar(64)）
            #   clause_type 条款类型，便于按类型过滤（VarChar(32)）
            #   content     条款原文，用于结果回显与二次校验（VarChar(8192)）
            #   embedding   向量字段，维度固定为 settings.EMBEDDING_DIM（1024）
            #   created_at  创建时间戳（Int64，秒级）
            fields = [
                FieldSchema(
                    name="id",
                    dtype=DataType.VARCHAR,
                    max_length=64,
                    is_primary=True,
                    description="主键，业务侧生成的 UUID",
                ),
                FieldSchema(
                    name="contract_id",
                    dtype=DataType.VARCHAR,
                    max_length=64,
                    description="所属合同 ID",
                ),
                FieldSchema(
                    name="clause_id",
                    dtype=DataType.VARCHAR,
                    max_length=64,
                    description="条款记录 ID，对应 MongoDB cr_contract_clause._id",
                ),
                FieldSchema(
                    name="clause_type",
                    dtype=DataType.VARCHAR,
                    max_length=32,
                    description="条款类型，取值见 ClauseType 枚举",
                ),
                FieldSchema(
                    name="content",
                    dtype=DataType.VARCHAR,
                    max_length=8192,
                    description="条款原文，用于检索结果回显",
                ),
                FieldSchema(
                    name="embedding",
                    dtype=DataType.FLOAT_VECTOR,
                    dim=settings.EMBEDDING_DIM,
                    description=f"条款向量，BGE-M3 输出，维度 {settings.EMBEDDING_DIM}",
                ),
                FieldSchema(
                    name="created_at",
                    dtype=DataType.INT64,
                    description="创建时间戳（Unix 秒）",
                ),
            ]

            schema = CollectionSchema(
                fields=fields,
                description=description,
                enable_dynamic_field=False,  # 关闭动态字段，保证写入结构可控
            )

            # 创建集合
            await asyncio.to_thread(
                client.create_collection,
                collection_name=collection_name,
                schema=schema,
            )

            # 创建 HNSW 索引（参数取自配置中心）
            index_params = client.prepare_index_params()
            index_params.add_index(
                field_name="embedding",
                index_type=settings.MILVUS_INDEX_TYPE,
                metric_type=settings.MILVUS_METRIC_TYPE,
                params={
                    "M": settings.MILVUS_INDEX_PARAMS_M,
                    "efConstruction": settings.MILVUS_INDEX_PARAMS_EF_CONSTRUCTION,
                },
            )
            await asyncio.to_thread(
                client.create_index,
                collection_name=collection_name,
                index_params=index_params,
            )

            logger.info(
                "集合索引创建完成 | 集合=%s | 类型=%s | 度量=%s | 维度=%d",
                collection_name,
                settings.MILVUS_INDEX_TYPE,
                settings.MILVUS_METRIC_TYPE,
                settings.EMBEDDING_DIM,
            )

        except PyMilvusException as exc:
            logger.error("集合创建失败 | 集合=%s | 异常=%s", collection_name, exc, exc_info=True)
            raise MilvusException(
                message=f"Milvus 集合 {collection_name} 创建失败",
                detail=str(exc),
            ) from exc

    # ------------------------------------------------------------------
    # 检索参数
    # ------------------------------------------------------------------
    def build_search_params(self) -> Dict[str, Any]:
        """
        构造检索参数

        返回：
            形如 {"metric_type": "COSINE", "params": {"ef": 128}} 的检索参数字典，
            可直接传给 MilvusClient.search()
        """
        return {
            "metric_type": settings.MILVUS_METRIC_TYPE,
            "params": {"ef": settings.MILVUS_SEARCH_PARAMS_EF},
        }

    # ------------------------------------------------------------------
    # 健康检查
    # ------------------------------------------------------------------
    async def health_check(self) -> bool:
        """
        检查 Milvus 连通性

        返回：
            True 表示连通，False 表示不可达
        """
        try:
            client = self._require_client()
            await asyncio.to_thread(client.list_collections)
            return True
        except Exception as exc:  # noqa: BLE001 - 健康检查需吞掉全部异常
            logger.error("Milvus 健康检查失败 | 异常=%s", exc)
            return False


# 全局单例
milvus_manager = MilvusClientManager()
