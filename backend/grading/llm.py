"""LLM provider adapter. Supports OpenAI-compatible APIs and Anthropic Messages."""

import os, json, httpx
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

PROVIDER_CONFIG = {
    "dashscope": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "default_model": "qwen-plus",
        "api": "openai",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o-mini",
        "api": "openai",
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "default_model": "deepseek-chat",
        "api": "openai",
    },
    "anthropic": {
        "base_url": "https://api.anthropic.com/v1",
        "default_model": "claude-3-5-sonnet-20241022",
        "api": "anthropic",
    },
    "custom": {
        "base_url": "",
        "default_model": "",
        "api": "openai",
    },
}


class LLMClient:
    """Thin wrapper around OpenAI-compatible chat completion APIs."""

    def __init__(
        self,
        provider: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
    ):
        self.provider = (provider or os.getenv("LLM_PROVIDER") or "dashscope").lower().strip()
        cfg = PROVIDER_CONFIG.get(self.provider, PROVIDER_CONFIG["custom"])
        self.api_style = cfg.get("api", "openai")

        self.api_key = api_key or os.getenv("LLM_API_KEY", "")
        self.model = model or os.getenv("LLM_MODEL") or cfg.get("default_model", "")
        self.base_url = (
            (base_url or os.getenv("LLM_BASE_URL") or "").strip().rstrip("/")
            or cfg.get("base_url", "")
        )

    async def chat(
        self,
        messages: list[dict],
        temperature: float = 0.3,
        max_tokens: int = 2000,
        timeout: float = 120.0,
        json_mode: bool = False,
    ) -> str:
        if self.api_style == "anthropic":
            return await self._chat_anthropic(messages, temperature, max_tokens, timeout)
        return await self._chat_openai(
            messages, temperature, max_tokens, timeout, json_mode=json_mode
        )

    async def _chat_openai(
        self,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
        timeout: float,
        json_mode: bool = False,
    ) -> str:
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        # JSON mode forces OpenAI-compatible providers to emit a valid JSON
        # object instead of prose, which is exactly what chat_json needs.
        if json_mode and self.provider in {"openai", "deepseek", "dashscope"}:
            payload["response_format"] = {"type": "json_object"}
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"] or ""

    async def _chat_anthropic(
        self,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
        timeout: float,
    ) -> str:
        system = "\n\n".join(
            str(m.get("content") or "") for m in messages if m.get("role") == "system"
        )
        msgs = [
            {"role": m["role"], "content": m["content"]}
            for m in messages
            if m.get("role") in ("user", "assistant")
        ]
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{self.base_url}/messages",
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": self.model,
                    "max_tokens": max_tokens,
                    "system": system,
                    "messages": msgs,
                    "temperature": temperature,
                },
            )
            resp.raise_for_status()
            content = resp.json().get("content") or []
            return "".join(
                str(block.get("text") or "") for block in content if block.get("type") == "text"
            ).strip()

    @staticmethod
    def _extract_json(raw: str) -> str:
        """Extract the JSON object from an LLM reply (handles code fences and prose)."""
        text = str(raw or "").strip()
        # 去掉 Markdown 代码围栏（```json / ```）
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
        # 截取第一个 { 到最后一个 } 之间的内容，忽略前后说明文字
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            text = text[start : end + 1]
        return text

    async def chat_json(self, messages: list[dict], **kwargs) -> dict:
        """Call LLM and parse the response as JSON, with one strict-JSON retry."""
        json_mode = bool(kwargs.pop("json_mode", False))
        raw = await self.chat(messages, json_mode=json_mode, **kwargs)
        try:
            return json.loads(self._extract_json(raw))
        except (ValueError, TypeError) as exc:
            # 首次输出不是合法 JSON（空内容 / 散文 / max_tokens 截断）。
            # 重试时显式开启 JSON 模式，从接口层面约束模型只输出 JSON。
            retry_messages = [
                *messages,
                {
                    "role": "user",
                    "content": (
                        f"你上一次的输出不是合法 JSON（{exc}）。"
                        "请只输出一个合法 JSON 对象，不要 Markdown 代码块，"
                        "字符串内的换行请用 \\n 转义，不要输出字面换行。"
                    ),
                },
            ]
            raw2 = await self.chat(retry_messages, json_mode=True, **kwargs)
            return json.loads(self._extract_json(raw2))
