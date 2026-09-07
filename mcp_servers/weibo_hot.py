"""微博热搜 MCP Server（mcp 2.x 兼容版）。

用 mcp 2.x 的 MCPServer API 重写，解决 china-trending-mcp 包与 mcp 2.x
不兼容的问题（旧版用 @server.list_tools() 装饰器，2.x 已移除）。

工具：
  - get_weibo_hot: 获取微博热搜榜 Top50
  - get_zhihu_hot: 获取知乎热榜 Top50

数据源与格式化逻辑复用 china-trending-mcp 的实现。
"""
import json
import logging
import sys

import requests
from mcp.server.mcpserver import MCPServer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stderr,
)
logger = logging.getLogger("weibo-hot-mcp")

# ── 常量 ──────────────────────────────────────────────
WEIBO_API_URL = "https://weibo.com/ajax/side/hotSearch"
WEIBO_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/17.0 Mobile/15E148 Safari/604.1"
    ),
    "Referer": "https://weibo.com/",
    "Accept": "application/json, text/plain, */*",
}

ZHIHU_API_URL = "https://www.zhihu.com/api/v4/search/top_search"
ZHIHU_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/17.0 Mobile/15E148 Safari/604.1"
    ),
    "Accept": "application/json, text/plain, */*",
}

mcp = MCPServer("weibo-hot")


def _safe_get(url: str, headers: dict) -> tuple[bool, object]:
    """安全 GET 请求，返回 (成功, data|error_msg)。"""
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        return True, resp.json()
    except requests.exceptions.Timeout:
        return False, "请求超时，请稍后重试"
    except requests.exceptions.ConnectionError:
        return False, "网络连接失败，请检查网络"
    except requests.exceptions.HTTPError as e:
        return False, f"HTTP 错误：{e.response.status_code}"
    except Exception as e:
        return False, f"请求异常：{e}"


def _format_weibo(items: list[dict]) -> str:
    if not items:
        return "微博热搜榜暂无数据"
    lines = ["微博热搜榜 Top50\n"]
    for item in items[:50]:
        rank = item.get("rank", "?")
        word = item.get("word", "").strip()
        num = item.get("num", 0)
        note = item.get("note", "").strip()
        line = f"{rank:>2}. {word}"
        if num:
            if num >= 10000:
                line += f"  热度{num / 10000:.1f}万"
            else:
                line += f"  热度{num}"
        if note:
            line += f"\n    摘要: {note}"
        lines.append(line)
    lines.append(f"\n共 {len(items)} 条热搜")
    return "\n".join(lines)


def _format_zhihu(items: list[dict]) -> str:
    if not items:
        return "知乎热榜暂无数据"
    lines = ["知乎热榜 Top50\n"]
    for idx, item in enumerate(items[:50], start=1):
        query = item.get("query", "").strip()
        if query:
            lines.append(f"{idx:>2}. {query}")
    lines.append(f"\n共 {len(items)} 条热榜")
    return "\n".join(lines)


@mcp.add_tool
def get_weibo_hot() -> str:
    """获取微博热搜榜 Top50。返回实时热搜列表，包含排名、标题、热度和话题摘要。"""
    logger.info("获取微博热搜...")
    ok, data = _safe_get(WEIBO_API_URL, WEIBO_HEADERS)
    if not ok:
        return f"微博热搜获取失败：{data}"
    realtime = data.get("data", {}).get("realtime", []) if isinstance(data, dict) else []
    if not realtime:
        return "微博热搜榜暂时无数据，请稍后再试"
    logger.info(f"微博热搜获取成功: {len(realtime)} 条")
    return _format_weibo(realtime)


@mcp.add_tool
def get_zhihu_hot() -> str:
    """获取知乎热榜 Top50。返回知乎当前热门话题列表，包含排名和问题标题。"""
    logger.info("获取知乎热榜...")
    ok, data = _safe_get(ZHIHU_API_URL, ZHIHU_HEADERS)
    if not ok:
        return f"知乎热榜获取失败：{data}"
    top_search = data.get("top_search", {}) if isinstance(data, dict) else {}
    words = top_search.get("words", []) if isinstance(top_search, dict) else []
    if not words:
        return "知乎热榜暂时无数据，请稍后再试"
    logger.info(f"知乎热榜获取成功: {len(words)} 条")
    return _format_zhihu(words)


if __name__ == "__main__":
    mcp.run()
