# D:/ContractReviewSys-合同审批智能审查项目/backend/app/engines/embedding_client.py
"""
BGE-M3 向量化客户端 — 本地服务，OpenAI 兼容协议

本模块职责：
    1. 封装 BGE-M3 向量化服务调用（单条 / 批量）
    2. 输出维度强校验，保证与 Milvus 集合 schema 一致
    3. 提供向量 L2 归一化工具（COSINE 度量下提升精度稳定性）
    4. 失败重试与超时控制

设计要点：
    - 本地内网服务，禁止走公网，base_url 由配置中心提供
    - 优先使用 /embeddings 批量接口，减少网络往返
    - 返回维度与配置不一致时立即报错，避免脏数据写入向量库

返回值说明：
    - embed_text()：返回单条文本的向量（List[float]）
    - embed_texts()：返回批量向量（List[List[float]]）
    - health_check()：返回 bool

使用示例：
    vec = await embedding_client.embed_text("本合同自双方签字之日起生效")
    vecs = await embedding_client.embed_texts(["条款一", "条款二"])
"""

import asyncio
import math
from typing import Any, Dict, List, Optional

import httpx

from app.core.config import settings
from app.core.exceptions import EmbeddingException
from app.core.logging_config import get_logger

logger = get_logger(__name__)


class EmbeddingClient:
    """
    BGE-M3 向量化客户端

    属性：
        _async_client: httpx 异步客户端，懒加载
    """

    def __init__(self) -> None:
        self._async_client: Optional[httpx.AsyncClient] = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def init_client(self) -> None:
        """
        初始化 httpx 异步客户端

        返回：
            无返回值
        """
        if self._async_client is not None:
            return

        self._async_client = httpx.AsyncClient(
            base_url=settings.EMBEDDING_BASE_URL.rstrip("/"),
            timeout=httpx.Timeout(
                timeout=settings.EMBEDDING_TIMEOUT_SECONDS,
                connect=5.0,
            ),
            headers={"Content-Type": "application/json"},
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            # 【关键】内网/本机向量化服务不走系统代理：
            # httpx 默认 trust_env=True 会读取 HTTP_PROXY 等环境变量，
            # 本机若存在 HTTP 代理，会劫持 127.0.0.1 请求导致
            # "向量化服务调用失败"（实测代理拦截后重试 3 次、每次数十秒）。
            trust_env=False,
        )
        logger.info(
            "向量化客户端初始化完成 | 地址=%s | 模型=%s | 维度=%d",
            settings.EMBEDDING_BASE_URL,
            settings.EMBEDDING_MODEL_NAME,
            settings.EMBEDDING_DIM,
        )

    async def close(self) -> None:
        """
        关闭 httpx 客户端

        返回：
            无返回值
        """
        if self._async_client is not None:
            await self._async_client.aclose()
            self._async_client = None
            logger.info("向量化客户端已关闭")

    def _require_client(self) -> httpx.AsyncClient:
        """
        获取客户端并校验已初始化

        返回：
            httpx.AsyncClient 实例

        异常：
            EmbeddingException：客户端未初始化时抛出
        """
        if self._async_client is None:
            raise EmbeddingException(message="向量化客户端尚未初始化")
        return self._async_client

    # ------------------------------------------------------------------
    # 核心向量化接口
    # ------------------------------------------------------------------
    async def embed_texts(self, texts: List[str]) -> List[List[float]]:
        """
        批量文本向量化

        参数：
            texts: 待向量化的文本列表。为空时直接返回空列表

        返回：
            向量列表，顺序与入参严格一一对应

        异常：
            EmbeddingException：调用失败、返回数量不匹配或维度异常时抛出
        """
        # 空列表直接返回，避免无意义的网络请求
        if not texts:
            return []

        client = self._require_client()
        payload: Dict[str, Any] = {
            "model": settings.EMBEDDING_MODEL_NAME,
            "input": texts,
            "encoding_format": "float",
        }

        last_exception: Optional[Exception] = None

        # 重试循环
        for attempt in range(1, settings.EMBEDDING_MAX_RETRIES + 1):
            try:
                response = await client.post("/embeddings", json=payload)
                response.raise_for_status()
                body = response.json()

                vectors = self._parse_embeddings(body, expected_count=len(texts))

                logger.info(
                    "向量化成功 | 条数=%d | 维度=%d | 尝试=%d",
                    len(vectors),
                    len(vectors[0]) if vectors else 0,
                    attempt,
                )
                return vectors

            except httpx.TimeoutException as exc:
                last_exception = exc
                logger.warning(
                    "向量化调用超时 | 条数=%d | 尝试=%d/%d",
                    len(texts),
                    attempt,
                    settings.EMBEDDING_MAX_RETRIES,
                )

            except httpx.HTTPStatusError as exc:
                last_exception = exc
                logger.warning(
                    "向量化服务返回异常状态 | 状态码=%s | 尝试=%d/%d | 响应=%s",
                    exc.response.status_code,
                    attempt,
                    settings.EMBEDDING_MAX_RETRIES,
                    exc.response.text[:500],
                )

            except (httpx.RequestError, ValueError, KeyError) as exc:
                last_exception = exc
                logger.warning(
                    "向量化调用失败 | 异常类型=%s | 尝试=%d/%d",
                    type(exc).__name__,
                    attempt,
                    settings.EMBEDDING_MAX_RETRIES,
                )

            if attempt < settings.EMBEDDING_MAX_RETRIES:
                backoff = min(2.0 ** (attempt - 1), 6.0)
                await asyncio.sleep(backoff)

        logger.error("向量化调用最终失败 | 地址=%s | 异常=%s", settings.EMBEDDING_BASE_URL, last_exception)
        raise EmbeddingException(
            message="向量化服务调用失败，请检查 BGE-M3 服务状态",
            detail=str(last_exception),
        ) from last_exception

    async def embed_text(self, text: str) -> List[float]:
        """
        单条文本向量化

        参数：
            text: 待向量化的文本

        返回：
            向量（长度为 EMBEDDING_DIM 的浮点列表）

        异常：
            EmbeddingException：调用失败时抛出
        """
        vectors = await self.embed_texts([text])
        if not vectors:
            raise EmbeddingException(message="向量化返回结果为空")
        return vectors[0]

    async def embed_in_batches(self, texts: List[str]) -> List[List[float]]:
        """
        超长文本列表分批向量化

        说明：
            按 EMBEDDING_BATCH_SIZE 切分，逐批调用后合并结果，
            保证顺序与入参一致。适用于整份合同条款批量入库场景。

        参数：
            texts: 待向量化的全部文本列表

        返回：
            向量列表，顺序与入参严格一致

        异常：
            EmbeddingException：任一批次失败时抛出（整体失败）
        """
        if not texts:
            return []

        batch_size = max(1, settings.EMBEDDING_BATCH_SIZE)
        all_vectors: List[List[float]] = []

        for start in range(0, len(texts), batch_size):
            batch = texts[start : start + batch_size]
            batch_vectors = await self.embed_texts(batch)
            all_vectors.extend(batch_vectors)

        logger.info(
            "分批向量化完成 | 总条数=%d | 批大小=%d | 批次数=%d",
            len(texts),
            batch_size,
            math.ceil(len(texts) / batch_size),
        )
        return all_vectors

    # ------------------------------------------------------------------
    # 响应解析与校验
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_embeddings(body: Dict[str, Any], expected_count: int) -> List[List[float]]:
        """
        解析 OpenAI 兼容格式的向量化响应

        参数：
            body: 响应 JSON 字典
            expected_count: 期望返回的向量条数

        返回：
            向量列表

        异常：
            EmbeddingException：结构异常、数量不匹配或维度不符时抛出
        """
        try:
            data: List[Dict[str, Any]] = body["data"]
        except (KeyError, TypeError) as exc:
            raise EmbeddingException(
                message="向量化响应结构异常，缺少 data 字段",
                detail=str(body)[:500],
            ) from exc

        # 数量校验：必须与入参条数一致，否则无法保证顺序对应关系
        if len(data) != expected_count:
            raise EmbeddingException(
                message="向量化返回条数与请求条数不一致",
                detail=f"期望={expected_count}, 实际={len(data)}",
            )

        # 按 index 排序，防御后端乱序返回
        try:
            data_sorted = sorted(data, key=lambda item: int(item.get("index", 0)))
        except (TypeError, ValueError):
            data_sorted = data

        vectors: List[List[float]] = []
        for item in data_sorted:
            embedding = item.get("embedding")
            if not isinstance(embedding, list) or not embedding:
                raise EmbeddingException(
                    message="向量化结果中存在非法的 embedding 字段",
                    detail=str(item)[:500],
                )
            # 维度强校验：与 Milvus 集合 schema 必须严格一致
            if len(embedding) != settings.EMBEDDING_DIM:
                raise EmbeddingException(
                    message="向量维度与配置不一致，可能存在模型配置错误",
                    detail=f"期望维度={settings.EMBEDDING_DIM}, 实际维度={len(embedding)}",
                )
            vectors.append([float(value) for value in embedding])

        return vectors

    # ------------------------------------------------------------------
    # 向量工具方法
    # ------------------------------------------------------------------
    @staticmethod
    def normalize_vector(vector: List[float]) -> List[float]:
        """
        L2 归一化向量

        说明：
            在 COSINE 度量下，归一化可提升数值稳定性；
            BGE-M3 输出已近似归一化，此处做一次保险处理。

        参数：
            vector: 原始向量

        返回：
            归一化后的向量。零向量原样返回，避免除零
        """
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0.0:
            return vector
        return [value / norm for value in vector]

    @staticmethod
    def cosine_similarity(vec_a: List[float], vec_b: List[float]) -> float:
        """
        计算两个向量的余弦相似度

        参数：
            vec_a: 向量 A
            vec_b: 向量 B

        返回：
            余弦相似度，范围 [-1, 1]

        异常：
            EmbeddingException：两向量维度不一致时抛出
        """
        if len(vec_a) != len(vec_b):
            raise EmbeddingException(
                message="计算相似度的两个向量维度不一致",
                detail=f"维度A={len(vec_a)}, 维度B={len(vec_b)}",
            )

        dot = sum(a * b for a, b in zip(vec_a, vec_b))
        norm_a = math.sqrt(sum(a * a for a in vec_a))
        norm_b = math.sqrt(sum(b * b for b in vec_b))

        if norm_a == 0.0 or norm_b == 0.0:
            return 0.0

        return dot / (norm_a * norm_b)

    # ------------------------------------------------------------------
    # 健康检查
    # ------------------------------------------------------------------
    async def health_check(self) -> bool:
        """
        检查向量化服务连通性

        说明：
            用一次最小 embeddings 请求探测（而非 /models）。
            部分推理服务实现（如本环境的 Infinity 部署）未提供
            /models 端点（GET 返回 404），但 /embeddings 工作正常；
            以业务真正依赖的端点做探活才不会误报。

        返回：
            True 表示连通，False 表示不可达
        """
        try:
            client = self._require_client()
            response = await client.post(
                "/embeddings",
                json={
                    "model": settings.EMBEDDING_MODEL_NAME,
                    "input": "healthcheck",
                },
            )
            return response.status_code == 200
        except Exception as exc:  # noqa: BLE001 - 健康检查需吞掉全部异常
            logger.error(
                "向量化服务健康检查失败 | 地址=%s | 异常=%s",
                settings.EMBEDDING_BASE_URL,
                exc,
            )
            return False


# 全局单例
embedding_client = EmbeddingClient()
