"""平台内置可执行工具注册表。

每个工具含：DB 元数据（name/description/parameters_json）+ Python 执行处理器。
Agent 循环把专家绑定的平台工具按 OpenAI function-calling schema 暴露给 DeepSeek，
模型决定调用时由本模块 `execute(name, args)` 实际执行。

工具设计：覆盖"取数 / 计算 / 检索 / 时间"四类常见能力，让真实 Agent 闭环可用。
"""
from __future__ import annotations

import ast
import datetime as dt
import json
import operator
import re
from typing import Any, Callable

# ---- 安全的算术求值（仅允许数字与 + - * / ** % () ） ----
_BIN_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.Pow: operator.pow, ast.Mod: operator.mod,
    ast.USub: operator.neg, ast.UAdd: operator.pos,
    ast.FloorDiv: operator.floordiv,
}


def _safe_eval(node):
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body)
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        return _BIN_OPS[type(node.op)](_safe_eval(node.left), _safe_eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _BIN_OPS:
        return _BIN_OPS[type(node.op)](_safe_eval(node.operand))
    if isinstance(node, ast.Num):  # py<3.8 兼容
        return node.n
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    raise ValueError("不支持的表达式")


def _calculator(args: dict) -> dict:
    expr = (args.get("expression") or "").strip()
    if not expr:
        return {"error": "缺少 expression 参数"}
    # 只允许数字与运算符
    if not re.fullmatch(r"[0-9eE.+\-*/%() ]+", expr):
        return {"error": "表达式含非法字符"}
    try:
        val = _safe_eval(ast.parse(expr, mode="eval"))
        return {"expression": expr, "result": float(val)}
    except Exception as e:
        return {"error": f"求值失败: {e}"}


# ---- 演示数据（模拟 internal-db 的 run_sql_query） ----
_DEMO_SALES = [
    {"category": "电子产品", "week": "本周", "sales": 128500, "orders": 320},
    {"category": "服装", "week": "本周", "sales": 86200, "orders": 410},
    {"category": "食品", "week": "本周", "sales": 54300, "orders": 680},
    {"category": "家居", "week": "本周", "sales": 41800, "orders": 195},
    {"category": "美妆", "week": "本周", "sales": 72600, "orders": 240},
]

_DEMO_DATASETS = [
    {"name": "sales", "description": "销售明细（品类/周/销售额/订单数）"},
    {"name": "inventory", "description": "库存（品类/在库数量）"},
    {"name": "users", "description": "用户画像（注册/活跃/等级）"},
]


def _list_datasets(args: dict) -> dict:
    return {"datasets": _DEMO_DATASETS}


def _run_sql_query(args: dict) -> dict:
    sql = (args.get("sql") or "").strip()
    if not sql:
        return {"error": "缺少 sql 参数"}
    # 演示：按 SQL 中出现的品类关键词过滤
    cats = [c for c in ["电子产品", "服装", "食品", "家居", "美妆"] if c in sql]
    rows = [r for r in _DEMO_SALES if not cats or r["category"] in cats]
    if not rows:
        rows = _DEMO_SALES  # 没匹配到就返回全部
    return {
        "sql": sql,
        "row_count": len(rows),
        "columns": ["category", "week", "sales", "orders"],
        "rows": rows,
        "note": "（演示数据，平台内置工具，非真实数据库）",
    }


def _web_search(args: dict) -> dict:
    q = (args.get("query") or "").strip()
    return {
        "query": q,
        "results": [
            {"title": f"关于「{q}」的搜索结果 1", "snippet": "演示用占位结果，真实环境接入搜索 MCP 后将返回真实网页摘要。", "url": "https://example.com/1"},
            {"title": f"关于「{q}」的搜索结果 2", "snippet": "演示用占位结果 2。", "url": "https://example.com/2"},
        ],
        "note": "（演示数据）",
    }


def _get_current_time(args: dict) -> dict:
    tz = args.get("timezone") or "Asia/Shanghai"
    try:
        import zoneinfo  # py3.9+
        now = dt.datetime.now(zoneinfo.ZoneInfo(tz))
    except Exception:
        now = dt.datetime.now()
    return {"iso": now.isoformat(timespec="seconds"), "timezone": tz, "weekday": now.strftime("%A")}


# ---- 工具注册表：name -> handler ----
HANDLERS: dict[str, Callable[[dict], dict]] = {
    "calculator": _calculator,
    "list_datasets": _list_datasets,
    "run_sql_query": _run_sql_query,
    "web_search": _web_search,
    "get_current_time": _get_current_time,
}


# ---- DB 种子定义：name/description/parameters_json/category ----
TOOL_DEFINITIONS: list[dict] = [
    {
        "name": "calculator",
        "description": "安全计算数学表达式（支持 + - * / ** % 与括号、小数）。用于数值核算。",
        "parameters_json": json.dumps({
            "type": "object",
            "properties": {"expression": {"type": "string", "description": "数学表达式，例如 (128500+86200)/2"}},
            "required": ["expression"],
        }, ensure_ascii=False),
        "category": "math",
    },
    {
        "name": "list_datasets",
        "description": "列出平台可查询的演示数据集（销售/库存/用户）。",
        "parameters_json": json.dumps({"type": "object", "properties": {}}, ensure_ascii=False),
        "category": "data",
    },
    {
        "name": "run_sql_query",
        "description": "对内部演示数仓执行只读 SQL 查询，返回销售明细（品类/周/销售额/订单数）。"
                       "支持按品类过滤（WHERE category IN (...)）。",
        "parameters_json": json.dumps({
            "type": "object",
            "properties": {"sql": {"type": "string", "description": "只读 SELECT 语句，例如 SELECT * FROM sales WHERE category='电子产品'"}},
            "required": ["sql"],
        }, ensure_ascii=False),
        "category": "data",
    },
    {
        "name": "web_search",
        "description": "联网搜索（演示版返回占位结果，用于法务/调研类专家检索参考）。",
        "parameters_json": json.dumps({
            "type": "object",
            "properties": {"query": {"type": "string", "description": "搜索关键词"}},
            "required": ["query"],
        }, ensure_ascii=False),
        "category": "search",
    },
    {
        "name": "get_current_time",
        "description": "获取当前时间与时区。",
        "parameters_json": json.dumps({
            "type": "object",
            "properties": {"timezone": {"type": "string", "description": "IANA 时区名，默认 Asia/Shanghai"}},
            "required": [],
        }, ensure_ascii=False),
        "category": "time",
    },
]


def execute(name: str, args: dict) -> dict:
    """执行某工具；handler 不存在或抛错时返回 error dict，永不抛异常给 agent 循环。"""
    h = HANDLERS.get(name)
    if h is None:
        return {"error": f"未知工具: {name}"}
    try:
        return h(args or {})
    except Exception as e:  # noqa: BLE001
        return {"error": f"工具执行异常: {e}"}


def to_openai_tool(tool) -> dict:
    """把 PlatformTool DB 行转成 OpenAI function-calling 的 tools 元素。"""
    try:
        params = json.loads(tool.parameters_json or "{}")
    except json.JSONDecodeError:
        params = {"type": "object", "properties": {}}
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description or tool.name,
            "parameters": params,
        },
    }
