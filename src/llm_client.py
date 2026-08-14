"""通用 LLM 客户端：兼容所有 OpenAI 风格 API。

支持 OpenAI / DeepSeek / Kimi / 通义 / Ollama / vLLM / LM Studio 等
任何实现了 /chat/completions 接口的服务。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from typing import Any, AsyncIterator

import httpx


@dataclass
class LLMConfig:
    """单个角色的 LLM 配置"""
    api_key: str = ""
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4o-mini"
    temperature: float = 0.8
    max_tokens: int = 4096
    timeout: int = 300

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "LLMConfig":
        data = data or {}
        return cls(
            api_key=str(data.get("api_key", "")).strip(),
            base_url=str(data.get("base_url", "https://api.openai.com/v1")).strip().rstrip("/"),
            model=str(data.get("model", "gpt-4o-mini")).strip(),
            temperature=float(data.get("temperature", 0.8)),
            max_tokens=int(data.get("max_tokens", 4096)),
            timeout=int(data.get("timeout", 300)),
        )


class LLMError(Exception):
    """LLM 调用错误"""


class LLMClient:
    """异步 OpenAI 兼容客户端"""

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(600.0, connect=15.0),
                follow_redirects=True,
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    def _headers(self, config: LLMConfig) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if config.api_key:
            headers["Authorization"] = f"Bearer {config.api_key}"
        return headers

    def _endpoint(self, config: LLMConfig) -> str:
        base = config.base_url
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"

    async def chat(
        self,
        config: LLMConfig,
        messages: list[dict[str, str]],
        *,
        json_mode: bool = False,
    ) -> str:
        """一次性对话，返回完整文本。"""
        payload: dict[str, Any] = {
            "model": config.model,
            "messages": messages,
            "temperature": config.temperature,
            "max_tokens": config.max_tokens,
            "stream": False,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        client = await self._get_client()
        resp = await client.post(
            self._endpoint(config),
            headers=self._headers(config),
            json=payload,
        )
        if resp.status_code != 200:
            raise LLMError(f"LLM 请求失败 [{resp.status_code}]: {resp.text[:500]}")

        data = resp.json()
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError) as exc:
            raise LLMError(f"LLM 响应格式异常: {json.dumps(data, ensure_ascii=False)[:500]}") from exc

    async def chat_stream(
        self,
        config: LLMConfig,
        messages: list[dict[str, str]],
    ) -> AsyncIterator[str]:
        """流式对话，逐段返回增量文本。"""
        payload: dict[str, Any] = {
            "model": config.model,
            "messages": messages,
            "temperature": config.temperature,
            "max_tokens": config.max_tokens,
            "stream": True,
        }

        client = await self._get_client()
        async with client.stream(
            "POST",
            self._endpoint(config),
            headers=self._headers(config),
            json=payload,
        ) as resp:
            if resp.status_code != 200:
                body = (await resp.aread()).decode("utf-8", errors="ignore")
                raise LLMError(f"LLM 流式请求失败 [{resp.status_code}]: {body[:500]}")

            async for line in resp.aiter_lines():
                line = line.strip()
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                    delta = obj["choices"][0]["delta"].get("content")
                    if delta:
                        yield delta
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue
