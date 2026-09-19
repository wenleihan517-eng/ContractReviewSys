# D:/ContractReviewSys-合同审批智能审查项目/backend/app/storage/minio_client.py
"""
MinIO 对象存储管理器

本模块职责：
    1. 初始化 MinIO 客户端并校验连通性
    2. 自动创建业务所需存储桶（合同原文 / OCR 图片 / 审查报告）
    3. 提供对象上传、下载、删除、预签名 URL 生成等基础能力
    4. 统一异常转换为 MinIOException

设计要点：
    - MinIO SDK 为同步库，所有涉及网络的方法均放入线程池执行，避免阻塞事件循环
    - 存储桶创建具备幂等性（exists 判断 + make_bucket）
    - 对象键（object_key）统一规范：业务前缀/年/月/日/UUID.扩展名，便于归档与检索

返回值说明：
    - MinioClient.get_client()：返回 Minio 客户端或 None
    - upload_bytes()：返回对象键字符串
    - download_bytes()：返回字节内容
    - delete_object()：返回 bool 表示是否成功
    - get_presigned_url()：返回预签名访问 URL

使用示例：
    await minio_manager.connect()
    key = await minio_manager.upload_bytes(bucket, "a/b.pdf", data, "application/pdf")
"""

import asyncio
import io
import uuid
from datetime import datetime, timedelta
from typing import Any, List, Optional

from minio import Minio
from minio.error import S3Error

from app.core.config import settings
from app.core.exceptions import MinIOException
from app.core.logging_config import get_logger

logger = get_logger(__name__)


class MinioClient:
    """
    MinIO 对象存储管理器

    属性：
        _client: Minio 客户端实例，未初始化时为 None
    """

    def __init__(self) -> None:
        self._client: Optional[Minio] = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def connect(self) -> None:
        """
        初始化 MinIO 客户端并创建所需存储桶

        说明：
            在 FastAPI lifespan 启动阶段调用。
            客户端本身为懒连接，因此通过 exists_bucket 探测连通性。

        返回：
            无返回值

        异常：
            MinIOException：连接失败或存储桶创建失败时抛出
        """
        try:
            logger.info("正在连接 MinIO | 地址=%s | 加密=%s", settings.MINIO_ENDPOINT, settings.MINIO_SECURE)

            self._client = Minio(
                endpoint=settings.MINIO_ENDPOINT,
                access_key=settings.MINIO_ACCESS_KEY,
                secret_key=settings.MINIO_SECRET_KEY,
                secure=settings.MINIO_SECURE,
            )

            # 创建业务存储桶（同步调用放入线程池）
            await self._ensure_buckets()

            logger.info("MinIO 连接成功 | 存储桶初始化完成")

        except S3Error as exc:
            logger.error("MinIO 连接失败 | 异常=%s", exc, exc_info=True)
            raise MinIOException(
                message="MinIO 连接失败，请检查服务状态与密钥配置",
                detail=str(exc),
            ) from exc

    async def close(self) -> None:
        """
        关闭 MinIO 客户端

        说明：MinIO SDK 无显式关闭方法，此处仅释放引用。

        返回：
            无返回值
        """
        self._client = None
        logger.info("MinIO 客户端引用已释放")

    def get_client(self) -> Optional[Minio]:
        """
        获取原始 MinIO 客户端

        返回：
            Minio 实例；未初始化时返回 None
        """
        return self._client

    def _require_client(self) -> Minio:
        """
        获取客户端并校验已初始化

        返回：
            Minio 实例

        异常：
            MinIOException：客户端未初始化时抛出
        """
        if self._client is None:
            raise MinIOException(message="MinIO 客户端尚未初始化")
        return self._client

    # ------------------------------------------------------------------
    # 存储桶管理
    # ------------------------------------------------------------------
    async def _ensure_buckets(self) -> None:
        """
        创建业务所需的全部存储桶

        说明：
            采用"存在则跳过"的幂等策略，可安全重复执行。

        返回：
            无返回值
        """
        client = self._require_client()
        buckets: List[str] = [
            settings.MINIO_BUCKET_CONTRACT,
            settings.MINIO_BUCKET_OCR_IMAGE,
            settings.MINIO_BUCKET_REPORT,
        ]

        for bucket in buckets:
            # MinIO SDK 为同步调用，统一通过 to_thread 包装
            exists = await asyncio.to_thread(client.bucket_exists, bucket)
            if not exists:
                await asyncio.to_thread(client.make_bucket, bucket)
                logger.info("MinIO 存储桶已创建 | 桶=%s", bucket)
            else:
                logger.debug("MinIO 存储桶已存在 | 桶=%s", bucket)

    # ------------------------------------------------------------------
    # 对象键生成
    # ------------------------------------------------------------------
    @staticmethod
    def build_object_key(prefix: str, file_name: str) -> str:
        """
        生成规范化对象键

        参数：
            prefix: 业务前缀，如 contract / ocr / report
            file_name: 原始文件名，用于提取扩展名

        返回：
            形如 contract/2026/09/18/3f2a....pdf 的对象键
        """
        # 提取扩展名（含点），无扩展名时留空
        ext = ""
        if "." in file_name:
            ext = "." + file_name.rsplit(".", 1)[-1].lower()

        today = datetime.now()
        unique = uuid.uuid4().hex

        return f"{prefix}/{today:%Y/%m/%d}/{unique}{ext}"

    # ------------------------------------------------------------------
    # 上传
    # ------------------------------------------------------------------
    async def upload_bytes(
        self,
        bucket: str,
        object_key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
    ) -> str:
        """
        上传字节内容到 MinIO

        参数：
            bucket: 目标存储桶名
            object_key: 对象键（由 build_object_key 生成）
            data: 文件字节内容
            content_type: MIME 类型，影响浏览器直接打开的展示方式

        返回：
            上传成功后的对象键

        异常：
            MinIOException：上传失败时抛出
        """
        client = self._require_client()
        try:
            await asyncio.to_thread(
                client.put_object,
                bucket,
                object_key,
                io.BytesIO(data),
                length=len(data),
                content_type=content_type,
            )
            logger.info(
                "对象上传成功 | 桶=%s | 键=%s | 大小=%d字节",
                bucket,
                object_key,
                len(data),
            )
            return object_key

        except S3Error as exc:
            logger.error("对象上传失败 | 桶=%s | 键=%s | 异常=%s", bucket, object_key, exc)
            raise MinIOException(
                message="文件上传到对象存储失败",
                detail=f"bucket={bucket}, key={object_key}, error={exc}",
            ) from exc

    # ------------------------------------------------------------------
    # 下载
    # ------------------------------------------------------------------
    async def download_bytes(self, bucket: str, object_key: str) -> bytes:
        """
        从 MinIO 下载对象内容

        参数：
            bucket: 存储桶名
            object_key: 对象键

        返回：
            文件字节内容

        异常：
            MinIOException：下载失败或对象不存在时抛出
        """
        client = self._require_client()
        response = None
        try:
            response = await asyncio.to_thread(client.get_object, bucket, object_key)
            data = await asyncio.to_thread(response.read)
            logger.debug(
                "对象下载成功 | 桶=%s | 键=%s | 大小=%d字节",
                bucket,
                object_key,
                len(data),
            )
            return data

        except S3Error as exc:
            logger.error("对象下载失败 | 桶=%s | 键=%s | 异常=%s", bucket, object_key, exc)
            raise MinIOException(
                message="从对象存储下载文件失败",
                detail=f"bucket={bucket}, key={object_key}, error={exc}",
            ) from exc

        finally:
            # 必须显式关闭响应流，否则连接会泄漏
            if response is not None:
                try:
                    await asyncio.to_thread(response.close)
                    await asyncio.to_thread(response.release_conn)
                except Exception:  # noqa: BLE001 - 释放失败不应影响主流程
                    logger.warning("释放 MinIO 响应流失败 | 键=%s", object_key)

    # ------------------------------------------------------------------
    # 删除
    # ------------------------------------------------------------------
    async def delete_object(self, bucket: str, object_key: str) -> bool:
        """
        删除指定对象

        参数：
            bucket: 存储桶名
            object_key: 对象键

        返回：
            True 表示删除成功；对象不存在时也返回 True（幂等）

        异常：
            MinIOException：删除失败时抛出
        """
        client = self._require_client()
        try:
            await asyncio.to_thread(client.remove_object, bucket, object_key)
            logger.info("对象删除成功 | 桶=%s | 键=%s", bucket, object_key)
            return True

        except S3Error as exc:
            logger.error("对象删除失败 | 桶=%s | 键=%s | 异常=%s", bucket, object_key, exc)
            raise MinIOException(
                message="删除对象存储文件失败",
                detail=f"bucket={bucket}, key={object_key}, error={exc}",
            ) from exc

    # ------------------------------------------------------------------
    # 预签名 URL
    # ------------------------------------------------------------------
    async def get_presigned_url(
        self,
        bucket: str,
        object_key: str,
        expires_seconds: int = 3600,
    ) -> str:
        """
        生成对象预签名访问 URL

        参数：
            bucket: 存储桶名
            object_key: 对象键
            expires_seconds: URL 有效期（秒），默认 1 小时

        返回：
            带签名的临时访问 URL

        异常：
            MinIOException：生成失败时抛出
        """
        client = self._require_client()
        try:
            url = await asyncio.to_thread(
                client.presigned_get_object,
                bucket,
                object_key,
                expires=timedelta(seconds=expires_seconds),
            )
            logger.debug("预签名 URL 生成成功 | 桶=%s | 键=%s", bucket, object_key)
            return url

        except S3Error as exc:
            logger.error("预签名 URL 生成失败 | 桶=%s | 键=%s | 异常=%s", bucket, object_key, exc)
            raise MinIOException(
                message="生成文件访问链接失败",
                detail=f"bucket={bucket}, key={object_key}, error={exc}",
            ) from exc

    # ------------------------------------------------------------------
    # 健康检查
    # ------------------------------------------------------------------
    async def health_check(self) -> bool:
        """
        检查 MinIO 连通性

        返回：
            True 表示连通，False 表示不可达
        """
        try:
            client = self._require_client()
            await asyncio.to_thread(client.bucket_exists, settings.MINIO_BUCKET_CONTRACT)
            return True
        except Exception as exc:  # noqa: BLE001 - 健康检查需吞掉全部异常
            logger.error("MinIO 健康检查失败 | 异常=%s", exc)
            return False


# 全局单例
minio_client = MinioClient()
