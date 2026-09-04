"""DeepSeek API 客户端（OpenAI 兼容，仅用 stdlib urllib，无新依赖）。

DeepSeek API 文档：https://api-docs.deepseek.com
- Base URL: https://api.deepseek.com/v1  （或 https://api.deepseek.com）
- 模型：deepseek-chat（支持 function calling）、deepseek-reasoner（推理，不支持 tools）
- 认证：Authorization: Bearer <api_key>
"""
from __future__ import annotations

import json
import urllib.request
import urllib.error


class DeepSeekError(RuntimeError):
    """DeepSeek 调用异常，message 含 API 返回的提示。"""


def chat_completions(
    *,
    api_key: str,
    base_url: str,
    model: str,
    messages: list[dict],
    tools: list[dict] | None = None,
    temperature: float = 0.3,
    max_tokens: int = 2048,
    timeout: float = 90.0,
) -> dict:
    """同步调用 DeepSeek chat completions，返回原始 JSON dict。"""
    url = base_url.rstrip("/") + "/chat/completions"
    body: dict = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"

    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        msg = e.read().decode("utf-8", errors="replace")
        try:
            err = json.loads(msg)
            detail = err.get("error", {}).get("message") or msg
        except json.JSONDecodeError:
            detail = msg
        raise DeepSeekError(f"DeepSeek HTTP {e.code}: {detail}") from None
    except urllib.error.URLError as e:
        raise DeepSeekError(f"DeepSeek 网络错误: {e.reason}") from None

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise DeepSeekError("DeepSeek 返回非 JSON 响应") from None
