"""真实 Agent 循环：用 DeepSeek function calling 完成任务。

流程（对应技术方案 §8.2 运行时流 ④~⑧）：
  组装系统提示词 + 已加载 Skill → 暴露专家绑定的平台工具 → 调 DeepSeek
  → 若模型要求工具则执行（平台内置工具）→ 把结果回灌 → 继续循环
  → 直至模型给出最终文本答复。

说明：真实架构里"执行"应交给 Hermes，平台只路由。本演示用平台内置工具替代
外部 MCP，让 Agent 闭环可真实跑通；接入真实 Hermes 时把 execute() 换成
对 Hermes API Server 的调用即可，循环结构不变。
"""
from __future__ import annotations

import json
import time
from typing import Any

from . import platform_tools
from .deepseek_client import chat_completions, DeepSeekError

MAX_ITER = 8


def _resolve_model(expert_model: str, default_model: str) -> str:
    """专家模型优先用 deepseek 系列；否则回退到平台默认模型。"""
    m = expert_model or ""
    # 兼容 "deepseek/deepseek-chat" / "deepseek-chat" / "deepseek-reasoner"
    if "/" in m:
        m = m.split("/", 1)[1]
    if m.startswith("deepseek"):
        return m
    # 非 deepseek 模型名（如 anthropic/...），回退到默认，保证可调通
    return default_model or "deepseek-chat"


def _build_system(expert, skills) -> str:
    parts: list[str] = []
    if expert.system_prompt:
        parts.append(expert.system_prompt)
    skill_text = "\n\n".join(
        f"### Skill: {s.name}（{s.category or 'general'} v{s.version}）\n{s.content}".strip()
        for s in skills
        if (s.content or "").strip()
    )
    if skill_text:
        parts.append("# 可用 Skill 规范（按需遵循其中的步骤）\n" + skill_text)
    parts.append(
        "# 执行约束\n"
        "- 需要数据/计算/检索时必须调用提供的工具，不要编造数据。\n"
        "- 工具返回的是演示数据，按其结果作答。\n"
        "- 最终用中文给出简洁结论。"
    )
    return "\n\n".join(parts)


def _assistant_msg_to_dict(msg: dict) -> dict:
    """把 DeepSeek 返回的 assistant message 转成可回灌的 dict。"""
    out: dict = {"role": "assistant", "content": msg.get("content") or ""}
    if msg.get("tool_calls"):
        out["tool_calls"] = [
            {
                "id": tc.get("id"),
                "type": "function",
                "function": {
                    "name": tc["function"]["name"],
                    "arguments": tc["function"].get("arguments") or "",
                },
            }
            for tc in msg["tool_calls"]
        ]
    return out


def run_task(
    *,
    expert,
    skills,
    message: str,
    settings: dict[str, str],
) -> dict:
    """完整 Agent 循环。"""
    t0 = time.time()
    api_key = settings.get("deepseek_api_key", "")
    if not api_key:
        raise DeepSeekError("未配置 DeepSeek API Key，请在「平台设置」中填写。")
    base_url = settings.get("deepseek_base_url", "https://api.deepseek.com/v1") or "https://api.deepseek.com/v1"
    default_model = settings.get("deepseek_default_model", "deepseek-chat") or "deepseek-chat"
    model = _resolve_model(expert.model, default_model)

    system = _build_system(expert, skills)
    messages: list[dict] = [
        {"role": "system", "content": system},
        {"role": "user", "content": message},
    ]

    # 暴露该专家绑定的平台工具
    tools = [platform_tools.to_openai_tool(t) for t in expert.platform_tools if t.enabled]
    tool_trace: list[dict] = []
    total_tokens = 0
    iterations = 0

    while iterations < MAX_ITER:
        iterations += 1
        resp = chat_completions(
            api_key=api_key,
            base_url=base_url,
            model=model,
            messages=messages,
            tools=tools or None,
        )
        total_tokens += int(resp.get("usage", {}).get("total_tokens") or 0)
        choice = (resp.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        finish = choice.get("finish_reason")

        # 把 assistant 消息回灌
        messages.append(_assistant_msg_to_dict(msg))
        tool_calls = msg.get("tool_calls") or []

        if not tool_calls:
            # 模型给出最终答复
            return {
                "response": (msg.get("content") or "").strip() or "（模型未返回文本）",
                "tool_calls": tool_trace,
                "tokens": total_tokens,
                "latency_ms": int((time.time() - t0) * 1000),
                "iterations": iterations,
                "model": model,
                "finish_reason": finish,
            }

        # 执行每个工具调用
        for tc in tool_calls:
            fn = tc.get("function") or {}
            name = fn.get("name", "")
            raw_args = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if raw_args else {}
            except json.JSONDecodeError:
                args = {"_raw": raw_args}
            result = platform_tools.execute(name, args)
            tool_trace.append({
                "name": name,
                "args": args,
                "result": result,
            })
            messages.append({
                "role": "tool",
                "tool_call_id": tc.get("id") or name,
                "content": json.dumps(result, ensure_ascii=False),
            })

    return {
        "response": "（已达最大循环轮次，未获得最终结论。请简化任务或增加工具能力。）",
        "tool_calls": tool_trace,
        "tokens": total_tokens,
        "latency_ms": int((time.time() - t0) * 1000),
        "iterations": iterations,
        "model": model,
        "finish_reason": "max_iterations",
    }
