"""真实 Agent 循环：用 DeepSeek function calling 完成任务。

流程（对应技术方案 §8.2 运行时流 ④~⑧）：
  组装系统提示词 + 已加载 Skill → 暴露专家绑定的平台工具 → 调 DeepSeek
  → 若模型要求工具则执行（平台内置工具）→ 把结果回灌 → 继续循环
  → 直至模型给出最终文本答复。

说明：真实架构里"执行"应交给 Hermes，平台只路由。本演示用平台内置工具替代
外部 MCP，让 Agent 闭环可真实跑通；接入真实 Hermes 时把 execute() 换成
对 Hermes API Server 的调用即可，循环结构不变。

进度回调：run_task 接受可选的 on_progress(step: dict) 回调，在每一步
（迭代开始 / 工具调用 / 工具返回 / 最终答复）时触发，供前端/飞书实时展示。
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable, Optional

from . import platform_tools
from . import mcp_client
from .deepseek_client import chat_completions, DeepSeekError

MAX_ITER = 8


def _mcp_tool_to_openai(t: dict) -> dict:
    """把 MCP 工具（带前缀 name + input_schema）转成 OpenAI function schema。"""
    return {
        "type": "function",
        "function": {
            "name": t["name"],
            "description": t.get("description", ""),
            "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
        },
    }


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
    on_progress: Optional[Callable[[dict], None]] = None,
) -> dict:
    """完整 Agent 循环。

    on_progress(step) 在关键节点触发，step 结构：
      {"type": "iter_start", "iteration": int}
      {"type": "tool_call", "name": str, "args": dict}
      {"type": "tool_result", "name": str, "result": Any}
      {"type": "final", "response": str, "tool_calls": list, "tokens": int, "latency_ms": int}
    回调在调用线程同步执行，实现方需自行保证不阻塞或抛异常。
    """
    t0 = time.time()
    api_key = settings.get("deepseek_api_key", "")
    if not api_key:
        raise DeepSeekError("未配置 DeepSeek API Key，请在「平台设置」中填写。")
    base_url = settings.get("deepseek_base_url", "https://api.deepseek.com/v1") or "https://api.deepseek.com/v1"
    default_model = settings.get("deepseek_default_model", "deepseek-chat") or "deepseek-chat"
    model = _resolve_model(expert.model, default_model)

    def _emit(step: dict) -> None:
        if on_progress is None:
            return
        try:
            on_progress(step)
        except Exception:  # noqa: BLE001
            # 进度回调失败不影响主流程
            pass

    system = _build_system(expert, skills)
    messages: list[dict] = [
        {"role": "system", "content": system},
        {"role": "user", "content": message},
    ]

    # ---- 收集该专家绑定的工具：平台内置工具 + MCP Server 工具 ----
    tools: list[dict] = [platform_tools.to_openai_tool(t) for t in expert.platform_tools if t.enabled]
    # MCP 会话表：带前缀工具名 -> (session, raw_name)
    mcp_sessions: dict[str, tuple[mcp_client.MCPSession, str]] = {}
    try:
        for mcp_srv in getattr(expert, "mcp_servers", []) or []:
            try:
                cfg = json.loads(mcp_srv.config_template or "{}")
            except json.JSONDecodeError:
                cfg = {}
            try:
                sess = mcp_client.connect(mcp_srv.name, mcp_srv.transport, cfg)
            except Exception as e:  # noqa: BLE001
                # 某个 MCP Server 连不上不影响整体，记到 trace 里
                _emit({"type": "tool_call", "name": f"mcp:{mcp_srv.name}", "args": {"error": str(e)}})
                continue
            try:
                mcp_tools = mcp_client.list_tools(sess, mcp_srv.tools_filter or "")
            except Exception as e:  # noqa: BLE001
                mcp_client.close(sess)
                _emit({"type": "tool_call", "name": f"mcp:{mcp_srv.name}", "args": {"error": f"list_tools: {e}"}})
                continue
            for mt in mcp_tools:
                tools.append(_mcp_tool_to_openai(mt))
                mcp_sessions[mt["name"]] = (sess, mt["raw_name"])
    except Exception:  # noqa: BLE001
        pass

    tool_trace: list[dict] = []
    total_tokens = 0
    iterations = 0

    try:
        result = _run_loop(
            model=model, messages=messages, tools=tools, mcp_sessions=mcp_sessions,
            tool_trace=tool_trace, on_progress=_emit,
            api_key=api_key, base_url=base_url, t0=t0,
        )
        return result
    finally:
        # 关闭所有 MCP 会话（子进程 / HTTP 连接）
        closed = set()
        for sess, _ in mcp_sessions.values():
            if id(sess) in closed:
                continue
            closed.add(id(sess))
            try:
                mcp_client.close(sess)
            except Exception:  # noqa: BLE001
                pass


def _run_loop(
    *,
    model: str,
    messages: list[dict],
    tools: list[dict],
    mcp_sessions: dict,
    tool_trace: list[dict],
    on_progress: Callable[[dict], None],
    api_key: str,
    base_url: str,
    t0: float,
) -> dict:
    """Agent 主循环（抽出独立函数以便 run_task 用 try/finally 清理 MCP 会话）。"""
    total_tokens = 0
    iterations = 0

    while iterations < MAX_ITER:
        iterations += 1
        on_progress({"type": "iter_start", "iteration": iterations, "model": model})
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
            response = (msg.get("content") or "").strip() or "（模型未返回文本）"
            on_progress({
                "type": "final",
                "response": response,
                "tool_calls": tool_trace,
                "tokens": total_tokens,
                "latency_ms": int((time.time() - t0) * 1000),
                "iterations": iterations,
            })
            return {
                "response": response,
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
            on_progress({"type": "tool_call", "name": name, "args": args})

            # 分发：带前缀的是 MCP 工具，否则是平台内置工具
            if name in mcp_sessions:
                sess, raw_name = mcp_sessions[name]
                try:
                    result = mcp_client.call_tool(sess, name, args)
                except Exception as e:  # noqa: BLE001
                    result = {"error": f"MCP 工具调用失败: {e}"}
            else:
                result = platform_tools.execute(name, args)

            tool_trace.append({
                "name": name,
                "args": args,
                "result": result,
            })
            on_progress({"type": "tool_result", "name": name, "result": result})
            messages.append({
                "role": "tool",
                "tool_call_id": tc.get("id") or name,
                "content": json.dumps(result, ensure_ascii=False),
            })

    response = "（已达最大循环轮次，未获得最终结论。请简化任务或增加工具能力。）"
    on_progress({
        "type": "final",
        "response": response,
        "tool_calls": tool_trace,
        "tokens": total_tokens,
        "latency_ms": int((time.time() - t0) * 1000),
        "iterations": iterations,
    })
    return {
        "response": response,
        "tool_calls": tool_trace,
        "tokens": total_tokens,
        "latency_ms": int((time.time() - t0) * 1000),
        "iterations": iterations,
        "model": model,
        "finish_reason": "max_iterations",
    }
