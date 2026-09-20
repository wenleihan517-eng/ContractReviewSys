# D:/ContractReviewSys-合同审批智能审查项目/backend/app/storage/__init__.py
"""
存储层统一出口 — 三类存储客户端单例聚合

本包职责：
    1. 聚合 MongoDB / MinIO / Milvus 三类存储客户端单例，业务代码统一从此处导入
    2. 提供统一的连接生命周期入口（init_all / close_all），供 FastAPI lifespan 调用
    3. 提供整体健康探针，供就绪检查接口使用

设计要点：
    - 单例模式：每个客户端在本模块以模块级变量实例化，进程内唯一
    - 初始化顺序：MongoDB（业务主数据）→ MinIO（文件资产）→ Milvus（向量数据）
    - 释放顺序与之相反，且单个释放失败不影响其余释放流程
    - 任一存储初始化失败即抛出业务异常，中断应用启动（合同场景不允许静默降级）

单例清单：
    - mongo_client   ：MongoDB 异步客户端（Motor 驱动）
    - minio_client   ：MinIO 对象存储客户端
    - milvus_manager ：Milvus 向量库客户端（独立 database 隔离）

返回值说明：
    - init_all_storages()：无返回值，副作用为建立全部存储连接
    - close_all_storages()：无返回值，副作用为释放全部存储连接
    - check_all_storages()：返回 Dict[str, bool]，各存储连通性

使用示例：
    from app.storage import mongo_client, minio_client, milvus_manager
    db = mongo_client.get_database()
"""

from typing import Dict, Tuple

from app.core.logging_config import get_logger
from app.storage.milvus_client import milvus_manager
from app.storage.minio_client import minio_client
from app.storage.mongo_client import mongo_client

logger = get_logger(__name__)

__all__ = [
    "mongo_client",
    "minio_client",
    "milvus_manager",
    "init_all_storages",
    "close_all_storages",
    "check_all_storages",
]


async def init_all_storages() -> None:
    """
    初始化全部存储连接

    说明：
        在 FastAPI lifespan 启动阶段调用，按固定顺序串行初始化。
        任一环节失败会抛出对应业务异常并中断应用启动，
        避免带着残缺依赖对外提供服务。

    返回：
        无返回值

    异常：
        MongoDBException / MinIOException / MilvusException：
            对应存储初始化失败时抛出
    """
    logger.info("开始初始化存储中间件 ...")

    # MongoDB 为业务主数据源，优先级最高，失败即中断
    await mongo_client.connect()
    # MinIO 承载合同文件资产，缺失将导致上传链路不可用
    await minio_client.connect()
    # Milvus 承载条款与规则向量，缺失将导致语义召回不可用
    await milvus_manager.connect()

    logger.info("存储中间件初始化完成 | MongoDB / MinIO / Milvus 均可用")


async def close_all_storages() -> None:
    """
    释放全部存储连接

    说明：
        按初始化逆序释放；单个存储释放失败仅记录告警，不向上抛出，
        保证应用退出时全部释放流程都能走完。

    返回：
        无返回值
    """
    logger.info("开始释放存储中间件连接 ...")

    # 逆序释放：Milvus → MinIO → MongoDB
    closers: Tuple[Tuple[str, object], ...] = (
        ("Milvus", milvus_manager.close),
        ("MinIO", minio_client.close),
        ("MongoDB", mongo_client.close),
    )

    for component_name, closer in closers:
        try:
            await closer()  # type: ignore[misc]
        except Exception as exc:  # noqa: BLE001 - 释放阶段异常仅记录
            logger.warning("存储连接释放异常 | 组件=%s | 异常=%s", component_name, exc)

    logger.info("存储中间件连接释放完成")


async def check_all_storages() -> Dict[str, bool]:
    """
    检查全部存储连通性

    说明：
        供就绪探针调用。各存储探测并发执行，任一探测异常均视为不可用，
        不向上抛出异常，保证探针接口本身始终可返回。

    返回：
        形如 {"mongodb": True, "minio": False, "milvus": True} 的字典
    """
    import asyncio

    async def _probe(component_name: str, probe) -> Tuple[str, bool]:
        """
        执行单个存储探测

        参数：
            component_name: 组件名，用于结果字典键
            probe: 异步探测函数
        返回：
            (组件名, 是否可用) 元组
        """
        try:
            return component_name, bool(await probe())
        except Exception as exc:  # noqa: BLE001 - 探测异常视为不可用
            logger.error("存储连通性探测异常 | 组件=%s | 异常=%s", component_name, exc)
            return component_name, False

    results = await asyncio.gather(
        _probe("mongodb", mongo_client.ping),
        _probe("minio", minio_client.health_check),
        _probe("milvus", milvus_manager.health_check),
    )

    return dict(results)
