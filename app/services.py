"""核心服务：
1) ProfileRenderer —— 把专家配置渲染下发为 Hermes Profile（SOUL.md / skills/ / config.yaml）。
2) HermesExecutor —— 演示版 Hermes 执行引擎：加载已下发 Profile，模拟 Agent 循环产出结果。
对应技术方案 §4.3「配置下发动作」与 §8.2「运行时流」。
"""
from __future__ import annotations

import json
import os
import shutil
import time
import datetime as dt
from typing import TYPE_CHECKING, Any, Callable, Optional

if TYPE_CHECKING:
    from .models import Expert, Skill, MCPServer

# 渲染产物根目录：项目根 data/profiles
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "profiles")


def _profile_dir(profile_name: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in profile_name)
    return os.path.join(DATA_DIR, safe)


# ============================================================
# Profile 渲染下发（管理面 → 执行面）
# ============================================================
class ProfileRenderer:
    """管理员保存专家后调用：写 SOUL.md / skills/ / config.yaml，模拟热加载回读校验。"""

    @staticmethod
    def render(expert: "Expert") -> str:
        pdir = _profile_dir(expert.profile_name)
        # 清理旧目录（避免删除残留旧 Skill）
        if os.path.exists(pdir):
            shutil.rmtree(pdir)
        os.makedirs(pdir, exist_ok=True)
        skills_dir = os.path.join(pdir, "skills")
        os.makedirs(skills_dir, exist_ok=True)

        # 1) SOUL.md —— 系统提示词（人格/系统指令）
        soul = (
            f"# {expert.name}\n\n"
            f"> {expert.description or '（未填写描述）'}\n\n"
            "## 系统提示词\n\n"
            f"{expert.system_prompt or '（未配置系统提示词）'}\n"
        )
        with open(os.path.join(pdir, "SOUL.md"), "w", encoding="utf-8") as f:
            f.write(soul)

        # 2) skills/<category>/<name>/SKILL.md —— 绑定的 Skill
        for sk in expert.skills:
            cat = sk.category or "general"
            sdir = os.path.join(skills_dir, cat, sk.name)
            os.makedirs(sdir, exist_ok=True)
            frontmatter = (
                "---\n"
                f"name: {sk.name}\n"
                f"description: {sk.description or sk.name}\n"
                f"version: {sk.version}\n"
                "metadata:\n"
                f"  tags: [{sk.tags}]\n"
                f"  category: {cat}\n"
                "---\n\n"
            )
            with open(os.path.join(sdir, "SKILL.md"), "w", encoding="utf-8") as f:
                f.write(frontmatter + (sk.content or ""))

        # 3) config.yaml —— 模型 + mcp_servers
        mcp_block = ""
        for mcp in expert.mcp_servers:
            cfg = {}
            try:
                cfg = json.loads(mcp.config_template or "{}")
            except json.JSONDecodeError:
                cfg = {}
            if mcp.transport == "http":
                mcp_block += f"  {mcp.name}:\n"
                mcp_block += f'    url: "{cfg.get("url", "")}"\n'
                headers = cfg.get("headers", {})
                if headers:
                    mcp_block += "    headers:\n"
                    for k, v in headers.items():
                        mcp_block += f'      {k}: "Bearer ***"\n' if "auth" in k.lower() else f'      {k}: "{v}"\n'
                if mcp.tools_filter:
                    mcp_block += f"    tools:\n      include: [{mcp.tools_filter}]\n"
                mcp_block += "    enabled: true\n"
            else:  # stdio
                mcp_block += f"  {mcp.name}:\n"
                mcp_block += f'    command: "{cfg.get("command", "npx")}"\n'
                args = cfg.get("args", [])
                if args:
                    mcp_block += f"    args: {json.dumps(args)}\n"
                env = cfg.get("env", {})
                if env:
                    mcp_block += "    env:\n"
                    for k in env:
                        mcp_block += f'      {k}: "***"\n'
                if mcp.tools_filter:
                    mcp_block += f"    tools:\n      exclude: [{mcp.tools_filter}]\n"

        # 平台内置可执行工具
        tool_block = ""
        for t in expert.platform_tools:
            tool_block += f"  {t.name}:\n    description: \"{(t.description or '')[:60]}\"\n    enabled: {str(t.enabled).lower()}\n"

        yaml_text = (
            "# Hermes Profile config —— 由平台下发\n"
            f"# 专家: {expert.name}  生成时间: {dt.datetime.now().isoformat(timespec='seconds')}\n\n"
            "model:\n"
            f"  default: \"{expert.model}\"\n\n"
            "mcp_servers:\n"
            f"{mcp_block if mcp_block else '  {}  # 未绑定 MCP'}\n\n"
            "platform_tools:\n"
            f"{tool_block if tool_block else '  []  # 未绑定平台工具'}\n"
        )
        with open(os.path.join(pdir, "config.yaml"), "w", encoding="utf-8") as f:
            f.write(yaml_text)

        # 4) 模拟热加载回读校验
        ok = os.path.exists(os.path.join(pdir, "SOUL.md")) and os.path.exists(
            os.path.join(pdir, "config.yaml")
        )
        if not ok:
            raise RuntimeError(f"Profile 下发校验失败: {pdir}")
        return pdir

    @staticmethod
    def rendered_files(profile_name: str) -> dict:
        """回读已下发文件清单（前端展示用）。"""
        pdir = _profile_dir(profile_name)
        if not os.path.exists(pdir):
            return {"dir": pdir, "files": {}}
        out: dict[str, str] = {}
        for root, _dirs, files in os.walk(pdir):
            for fn in files:
                fp = os.path.join(root, fn)
                rel = os.path.relpath(fp, pdir).replace("\\", "/")
                try:
                    with open(fp, "r", encoding="utf-8") as f:
                        out[rel] = f.read()
                except UnicodeDecodeError:
                    out[rel] = "(binary)"
        return {"dir": pdir, "files": out}


# ============================================================
# Hermes 执行引擎（演示版：模拟 Agent 循环）
# ============================================================
class HermesExecutor:
    """执行引擎分发器：
    - 配置了 DeepSeek API Key → 走真实 Agent 循环（DeepSeek function calling + 平台工具）。
    - 未配置 → 回退到模拟回显（验证配置链路，不真实推理）。
    平台只负责路由；真实架构里执行应交给 Hermes，本演示把 Hermes 等效实现放在 agent_loop.py。"""

    @staticmethod
    def run(
        expert: "Expert",
        message: str,
        settings: dict[str, str] | None = None,
        on_progress: Optional[Callable[[dict[str, Any]], None]] = None,
    ) -> dict:
        pdir = _profile_dir(expert.profile_name)
        # 若 Profile 未下发，先渲染（演示兜底）
        if not os.path.exists(os.path.join(pdir, "SOUL.md")):
            ProfileRenderer.render(expert)

        settings = settings or {}
        skill_names = [s.name for s in expert.skills]
        mcp_names = [m.name for m in expert.mcp_servers]
        tool_names = [t.name for t in expert.platform_tools]

        # ---- 真实执行：DeepSeek + function calling ----
        if settings.get("deepseek_api_key"):
            from .agent_loop import run_task, DeepSeekError  # noqa: WPS433
            try:
                res = run_task(
                    expert=expert,
                    skills=expert.skills,
                    message=message,
                    settings=settings,
                    on_progress=on_progress,
                )
                return {
                    "provider": "deepseek",
                    "response": res["response"],
                    "tool_calls": res.get("tool_calls", []),
                    "skill_used": ", ".join(skill_names),
                    "mcp_used": ", ".join(mcp_names),
                    "tokens": res["tokens"],
                    "latency_ms": res["latency_ms"],
                    "profile_dir": pdir,
                    "model": res.get("model"),
                    "iterations": res.get("iterations"),
                }
            except DeepSeekError as e:
                # 友好回退：把错误内联返回，不抛 500
                if on_progress:
                    try:
                        on_progress({"type": "final", "response": str(e), "tool_calls": [], "tokens": 0, "latency_ms": 0})
                    except Exception:
                        pass
                return {
                    "provider": "deepseek-error",
                    "response": f"【DeepSeek 调用失败】{e}\n\n请检查「平台设置」中的 API Key / Base URL / 模型是否正确。",
                    "tool_calls": [],
                    "skill_used": ", ".join(skill_names),
                    "mcp_used": ", ".join(mcp_names),
                    "tokens": 0,
                    "latency_ms": 0,
                    "profile_dir": pdir,
                }

        # ---- 模拟回退（未配置 Key）----
        t0 = time.time()
        time.sleep(0.15)
        response = (
            f"【模拟执行回执（未配置 DeepSeek Key）】\n"
            f"专家: {expert.name}\n"
            f"模型: {expert.model}\n"
            f"已加载 Skill: {', '.join(skill_names) if skill_names else '(无)'}\n"
            f"已加载 MCP: {', '.join(mcp_names) if mcp_names else '(无)'}\n"
            f"可执行平台工具: {', '.join(tool_names) if tool_names else '(无)'}\n"
            f"Profile 目录: {pdir}\n"
            f"──────────────────────\n"
            f"任务: {message}\n"
            f"提示: 在「平台设置」填入 DeepSeek API Key 后，本专家将真实调用 DeepSeek 完成任务。"
        )
        if on_progress:
            try:
                on_progress({"type": "final", "response": response, "tool_calls": [], "tokens": 0, "latency_ms": 0})
            except Exception:
                pass
        return {
            "provider": "mock",
            "response": response,
            "tool_calls": [],
            "skill_used": ", ".join(skill_names),
            "mcp_used": ", ".join(mcp_names),
            "tokens": 80 + len(message) // 3,
            "latency_ms": int((time.time() - t0) * 1000),
            "profile_dir": pdir,
        }
