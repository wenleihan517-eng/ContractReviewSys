# D:/ContractReviewSys-合同审批智能审查项目/backend/app/engines/llm_client.py
"""
本地大模型客户端 — OpenAI 兼容协议适配层

【架构红线】
    本客户端只提供"辅助能力"调用入口：
        1. 条款语义解析
        2. 疑似风险识别（输出候选，不定级）
        3. 修改建议初稿生成
        4. 合规条文解读
    【严禁】通过本客户端直接产出：
        风险等级定级、违规判定、任务阻塞判定、最终审查结论。
    上述判定必须由规则引擎完成，本模块输出的任何结果均需经过规则引擎复核。

设计要点：
    - 采用 OpenAI 兼容协议（/v1/chat/completions），更换底层模型只需改 base_url
    - 使用 httpx 异步客户端，连接池复用，避免每次请求重建连接
    - 统一返回 LLMResponse 结构，屏蔽不同后端的响应差异
    - 失败自动重试（指数退避），超时与错误分类抛出业务异常

返回值说明：
    - LLMResponse.content：模型生成的文本内容
    - LLMResponse.usage：token 消耗统计
    - chat()：返回 LLMResponse
    - chat_json()：返回解析后的字典（要求模型输出 JSON）

使用示例：
    resp = await llm_client.chat(system_prompt="你是合同审查助手", user_prompt="解析条款")
    data = await llm_client.chat_json(system_prompt="...", user_prompt="...")
"""

import asyncio
import json
import re
import time
from typing import Any, Dict, List, Optional

import httpx
from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.exceptions import LLMException
from app.core.logging_config import get_logger

logger = get_logger(__name__)


class LLMResponse(BaseModel):
    """
    大模型返回结果封装

    字段：
        content: 模型生成的正文内容
        model: 实际响应的模型名称
        prompt_tokens: 输入 token 数
        completion_tokens: 输出 token 数
        total_tokens: 总 token 数
        elapsed_ms: 本次调用耗时（毫秒）
        finish_reason: 结束原因（stop / length 等）
    """

    content: str = Field(default="", description="模型生成正文")
    model: str = Field(default="", description="实际响应的模型名称")
    prompt_tokens: int = Field(default=0, description="输入 token 数")
    completion_tokens: int = Field(default=0, description="输出 token 数")
    total_tokens: int = Field(default=0, description="总 token 数")
    elapsed_ms: float = Field(default=0.0, description="调用耗时（毫秒）")
    finish_reason: str = Field(default="", description="结束原因")


class LLMClient:
    """
    本地大模型客户端（OpenAI 兼容协议）

    属性：
        _async_client: httpx 异步客户端，懒加载
    """

    def __init__(self) -> None:
        self._async_client: Optional[httpx.AsyncClient] = None

    # ------------------------------------------------------------------
    # 客户端生命周期
    # ------------------------------------------------------------------
    async def init_client(self) -> None:
        """
        初始化 httpx 异步客户端

        说明：
            在 FastAPI lifespan 启动阶段调用，建立连接池。
            超时时间取自配置中心，覆盖长文本解析场景。

        返回：
            无返回值
        """
        if self._async_client is not None:
            return

        self._async_client = httpx.AsyncClient(
            base_url=settings.LLM_BASE_URL.rstrip("/"),
            timeout=httpx.Timeout(
                timeout=settings.LLM_TIMEOUT_SECONDS,
                connect=10.0,
            ),
            headers={
                "Authorization": f"Bearer {settings.LLM_API_KEY}",
                "Content-Type": "application/json",
            },
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
        logger.info(
            "大模型客户端初始化完成 | 地址=%s | 模型=%s",
            settings.LLM_BASE_URL,
            settings.LLM_MODEL_NAME,
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
            logger.info("大模型客户端已关闭")

    def _require_client(self) -> httpx.AsyncClient:
        """
        获取客户端并校验已初始化

        返回：
            httpx.AsyncClient 实例

        异常：
            LLMException：客户端未初始化时抛出
        """
        if self._async_client is None:
            raise LLMException(message="大模型客户端尚未初始化")
        return self._async_client

    # ------------------------------------------------------------------
    # 核心对话接口
    # ------------------------------------------------------------------
    async def chat(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        json_mode: bool = False,
    ) -> LLMResponse:
        """
        调用大模型对话补全接口（OpenAI 兼容）

        参数：
            system_prompt: 系统提示词，定义模型角色与输出约束
            user_prompt: 用户提示词，承载待处理的合同条款内容
            temperature: 采样温度，缺省取配置值（默认 0.1，保证输出稳定）
            max_tokens: 最大生成 token 数，缺省取配置值
            json_mode: 是否要求模型以 JSON 格式输出。
                       为 True 时设置 response_format，提高 JSON 解析成功率

        返回：
            LLMResponse 实例

        异常：
            LLMException：请求失败、超时或响应结构异常时抛出
        """
        client = self._require_client()

        # 组装请求体，参数缺省时回落到配置中心的值
        payload: Dict[str, Any] = {
            "model": settings.LLM_MODEL_NAME,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": (
                settings.LLM_TEMPERATURE if temperature is None else temperature
            ),
            "max_tokens": settings.LLM_MAX_TOKENS if max_tokens is None else max_tokens,
            "stream": False,
        }

        # 需要结构化输出时启用 JSON 模式
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        start_ts = time.perf_counter()
        last_exception: Optional[Exception] = None

        # 重试循环：指数退避，最多 LLM_MAX_RETRIES 次
        for attempt in range(1, settings.LLM_MAX_RETRIES + 1):
            try:
                response = await client.post("/chat/completions", json=payload)
                response.raise_for_status()
                body = response.json()

                elapsed_ms = (time.perf_counter() - start_ts) * 1000.0
                result = self._parse_response(body, elapsed_ms)

                logger.info(
                    "大模型调用成功 | 模型=%s | 输入token=%d | 输出token=%d | 耗时=%.2fms | 尝试=%d",
                    result.model,
                    result.prompt_tokens,
                    result.completion_tokens,
                    result.elapsed_ms,
                    attempt,
                )
                return result

            except httpx.TimeoutException as exc:
                last_exception = exc
                logger.warning(
                    "大模型调用超时 | 尝试=%d/%d | 超时=%ss",
                    attempt,
                    settings.LLM_MAX_RETRIES,
                    settings.LLM_TIMEOUT_SECONDS,
                )

            except httpx.HTTPStatusError as exc:
                last_exception = exc
                logger.warning(
                    "大模型返回异常状态 | 状态码=%s | 尝试=%d/%d | 响应=%s",
                    exc.response.status_code,
                    attempt,
                    settings.LLM_MAX_RETRIES,
                    exc.response.text[:500],
                )

            except (httpx.RequestError, ValueError, KeyError) as exc:
                last_exception = exc
                logger.warning(
                    "大模型调用失败 | 异常类型=%s | 尝试=%d/%d",
                    type(exc).__name__,
                    attempt,
                    settings.LLM_MAX_RETRIES,
                )

            # 非最后一次尝试则退避等待后重试
            if attempt < settings.LLM_MAX_RETRIES:
                backoff = min(2.0 ** (attempt - 1), 8.0)
                await asyncio.sleep(backoff)

        # 全部尝试失败，抛出业务异常
        logger.error(
            "大模型调用最终失败 | 地址=%s | 模型=%s | 异常=%s",
            settings.LLM_BASE_URL,
            settings.LLM_MODEL_NAME,
            last_exception,
        )
        raise LLMException(
            message="本地大模型调用失败，请检查模型服务状态",
            detail=str(last_exception),
        ) from last_exception

    async def chat_json(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        调用大模型并解析 JSON 结果

        说明：
            强制启用 JSON 模式，并对返回内容做容错解析
            （兼容模型输出被 ```json 代码块包裹的情况）。

        参数：
            system_prompt: 系统提示词，须明确要求输出 JSON
            user_prompt: 用户提示词
            temperature: 采样温度
            max_tokens: 最大生成 token 数

        返回：
            解析后的字典

        异常：
            LLMException：调用失败或 JSON 解析失败时抛出
        """
        response = await self.chat(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=True,
        )

        content = response.content.strip()

        # 容错一：剥离 ```json ... ``` 代码块包裹
        if content.startswith("```"):
            content = re.sub(r"^```(?:json)?\s*", "", content)
            content = re.sub(r"\s*```$", "", content)
            content = content.strip()

        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            # 容错二：尝试截取首个 { 到最后一个 } 之间的内容
            start = content.find("{")
            end = content.rfind("}")
            if start != -1 and end != -1 and end > start:
                try:
                    parsed = json.loads(content[start : end + 1])
                except json.JSONDecodeError as exc:
                    logger.error("大模型 JSON 解析失败 | 原始内容=%s", content[:1000])
                    raise LLMException(
                        message="大模型输出不符合 JSON 格式，无法解析",
                        detail=content[:1000],
                    ) from exc
            else:
                logger.error("大模型 JSON 解析失败 | 原始内容=%s", content[:1000])
                raise LLMException(
                    message="大模型输出不符合 JSON 格式，无法解析",
                    detail=content[:1000],
                )

        if not isinstance(parsed, dict):
            raise LLMException(
                message="大模型输出 JSON 顶层结构必须为对象",
                detail=f"实际类型={type(parsed).__name__}",
            )

        return parsed

    # ------------------------------------------------------------------
    # 响应解析
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_response(body: Dict[str, Any], elapsed_ms: float) -> LLMResponse:
        """
        解析 OpenAI 兼容格式的响应体

        参数：
            body: 响应 JSON 字典
            elapsed_ms: 调用耗时（毫秒）

        返回：
            LLMResponse 实例

        异常：
            LLMException：响应结构缺少必要字段时抛出
        """
        try:
            choices: List[Dict[str, Any]] = body["choices"]
            if not choices:
                raise KeyError("choices 为空")

            message = choices[0].get("message", {})
            content = message.get("content", "") or ""
            finish_reason = choices[0].get("finish_reason", "") or ""

            usage = body.get("usage", {}) or {}

            return LLMResponse(
                content=content,
                model=body.get("model", settings.LLM_MODEL_NAME),
                prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
                completion_tokens=int(usage.get("completion_tokens", 0) or 0),
                total_tokens=int(usage.get("total_tokens", 0) or 0),
                elapsed_ms=elapsed_ms,
                finish_reason=finish_reason,
            )

        except (KeyError, IndexError, TypeError) as exc:
            logger.error("大模型响应结构异常 | 响应体=%s", str(body)[:1000])
            raise LLMException(
                message="大模型响应结构异常，无法解析",
                detail=f"error={exc}, body={str(body)[:500]}",
            ) from exc

    # ------------------------------------------------------------------
    # 健康检查
    # ------------------------------------------------------------------
    async def health_check(self) -> bool:
        """
        检查本地大模型服务连通性

        说明：请求 /models 接口（OpenAI 兼容协议的模型列表接口）。

        返回：
            True 表示连通，False 表示不可达
        """
        try:
            client = self._require_client()
            response = await client.get("/models")
            return response.status_code == 200
        except Exception as exc:  # noqa: BLE001 - 健康检查需吞掉全部异常
            logger.error("大模型服务健康检查失败 | 地址=%s | 异常=%s", settings.LLM_BASE_URL, exc)
            return False


# 全局单例，业务代码通过依赖注入或直接 import 使用
llm_client = LLMClient()
