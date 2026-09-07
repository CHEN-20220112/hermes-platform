"""MCP 客户端封装。

把 lark-oapi 风格的"同步调用方 + 后台事件循环"模式套用到 MCP：
- 后台跑一个独立的 asyncio loop，所有 MCP 异步操作都丢上去执行。
- 对外暴露同步接口 connect / list_tools / call_tool / close，供同步的
  agent_loop 直接调用。

支持两种传输：
- stdio：启动子进程（command + args + env）
- http（streamable-http）：连接远程 MCP Server URL（可选 headers 鉴权）
"""
from __future__ import annotations

import asyncio
import json
import threading
from typing import Any, Optional


# ====== 后台事件循环（单例） ======
_loop: Optional[asyncio.AbstractEventLoop] = None
_loop_lock = threading.Lock()
_loop_thread: Optional[threading.Thread] = None


def _get_loop() -> asyncio.AbstractEventLoop:
    """获取（必要时启动）后台事件循环。"""
    global _loop, _loop_thread
    with _loop_lock:
        if _loop is None or _loop.is_closed():
            _loop = asyncio.new_event_loop()
            _loop_thread = threading.Thread(
                target=_run_loop, args=(_loop,), daemon=True, name="mcp-loop")
            _loop_thread.start()
        return _loop


def _run_loop(loop: asyncio.AbstractEventLoop) -> None:
    asyncio.set_event_loop(loop)
    loop.run_forever()


def _run(coro) -> Any:
    """把协程丢到后台 loop 执行，同步等待结果。"""
    loop = _get_loop()
    fut = asyncio.run_coroutine_threadsafe(coro, loop)
    return fut.result()


# ====== 会话上下文（持有 async 资源，供 close 时释放） ======
class MCPSession:
    """一个 MCP Server 的连接句柄。

    实际的 async 资源（session / stdio 管道 / http 连接）保存在
    `_ctx` 里，由后台 loop 管理生命周期。
    """

    def __init__(self, name: str):
        self.name = name
        # 由 _connect 填充：{"session": ClientSession, "stack": AsyncExitStack}
        self._ctx: dict[str, Any] = {}
        # 已发现的工具缓存（OpenAI function schema 格式）
        self.tools: list[dict] = []

    @property
    def connected(self) -> bool:
        return bool(self._ctx.get("session"))


# ====== 连接 / 工具发现 / 调用 ======
def connect(server_name: str, transport: str, config: dict) -> MCPSession:
    """同步连接一个 MCP Server，返回 MCPSession。

    config 结构：
      - stdio: {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-xxx"], "env": {...}}
      - http:  {"url": "http://host:port/mcp", "headers": {"Authorization": "Bearer xxx"}}
    """
    session = MCPSession(server_name)
    _run(_connect_async(session, transport, config))
    return session


async def _connect_async(session: MCPSession, transport: str, config: dict) -> None:
    from contextlib import AsyncExitStack
    from mcp.client.session import ClientSession

    stack = AsyncExitStack()
    read_stream = write_stream = None

    if transport == "stdio":
        from mcp.client.stdio import stdio_client, StdioServerParameters
        params = StdioServerParameters(
            command=config["command"],
            args=config.get("args") or [],
            env=config.get("env"),
        )
        read_stream, write_stream = await stack.enter_async_context(stdio_client(params))
    elif transport in ("http", "streamable_http"):
        from mcp.client.streamable_http import streamable_http_client
        url = config["url"]
        headers = config.get("headers") or None
        read_stream, write_stream = await stack.enter_async_context(
            streamable_http_client(url, headers=headers)
        )
    else:
        raise ValueError(f"不支持的 MCP 传输类型: {transport}")

    mcp_session = await stack.enter_async_context(
        ClientSession(read_stream, write_stream)
    )
    await mcp_session.initialize()
    session._ctx = {"session": mcp_session, "stack": stack}


def list_tools(session: MCPSession, tools_filter: str = "") -> list[dict]:
    """列出该 Server 的工具，转成 OpenAI function-calling schema。

    tools_filter: 逗号分隔的工具名白名单，空则全部返回。
    返回元素结构：
      {"name": "{server}__{tool}", "description": ..., "input_schema": {...}}
    注意：返回的 name 已加 server 前缀，避免多 Server 间重名。
    """
    allow = {t.strip() for t in tools_filter.split(",") if t.strip()}
    tools = _run(_list_tools_async(session))
    out: list[dict] = []
    for t in tools:
        raw_name = t.name
        if allow and raw_name not in allow:
            continue
        prefixed = f"{session.name}__{raw_name}"
        out.append({
            "name": prefixed,
            "raw_name": raw_name,
            "description": t.description or "",
            "input_schema": t.input_schema or {"type": "object", "properties": {}},
        })
    session.tools = out
    return out


async def _list_tools_async(session: MCPSession):
    mcp_session = session._ctx["session"]
    result = await mcp_session.list_tools()
    return result.tools


def call_tool(session: MCPSession, prefixed_name: str, arguments: dict) -> dict:
    """调用一个 MCP 工具，返回结构化结果。

    prefixed_name 是 list_tools 返回的带前缀名称；内部会去掉前缀再调用。
    """
    raw_name = prefixed_name.split("__", 1)[1] if "__" in prefixed_name else prefixed_name
    return _run(_call_tool_async(session, raw_name, arguments))


async def _call_tool_async(session: MCPSession, name: str, arguments: dict) -> dict:
    mcp_session = session._ctx["session"]
    result = await mcp_session.call_tool(name, arguments)
    # MCP 返回的是 content 列表（text/image/...），拼成文本
    texts = []
    for c in result.content:
        if c.type == "text":
            texts.append(c.text)
        else:
            texts.append(f"[{c.type}]")
    text = "\n".join(texts)
    # 尝试解析为 JSON（很多 MCP server 返回 JSON 字符串）
    try:
        parsed = json.loads(text)
        return {"_raw": text, "data": parsed, "is_error": getattr(result, "is_error", False)}
    except (json.JSONDecodeError, TypeError):
        return {"_raw": text, "is_error": getattr(result, "is_error", False)}


def close(session: MCPSession) -> None:
    """关闭连接，释放子进程 / HTTP 连接。"""
    if not session.connected:
        return
    try:
        _run(_close_async(session))
    except Exception:
        pass
    session._ctx = {}
    session.tools = []


async def _close_async(session: MCPSession) -> None:
    stack = session._ctx.get("stack")
    if stack is not None:
        await stack.aclose()
