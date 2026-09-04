"""FastAPI 入口：管理员后台 + 飞书路由网关（演示版）。

职责边界（对应技术方案 §2）：
- 管理面：专家/Skill/MCP 配置 CRUD
- 路由面：飞书请求 → 专家 → Hermes
- 执行面：交给 HermesExecutor（演示用模拟，真实环境调 Hermes API Server）
- 数据层：SQLite（演示起步）

启动： uvicorn app.main:app --reload --port 8000
"""
from __future__ import annotations

import os

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from .database import init_db
from .routers import auth, experts, skills, mcp_servers, feishu, settings as settings_router, platform_tools

FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend")

app = FastAPI(
    title="Hermes 专家配置与飞书服务平台",
    description="管理员配置专家（系统提示词+Skill+MCP+平台工具+模型）→ 下发 Hermes Profile → 飞书用户选专家干活。配 DeepSeek Key 后真实调用 DeepSeek function calling。",
    version="0.2.0-deepseek",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup() -> None:
    init_db()
    # 演示用：种入飞书 App 记录 + 示例 Skill/MCP/专家
    _seed_demo_data()
    # 若飞书已启用且凭证齐全，自动拉起 WS 长连接
    try:
        from .feishu_adapter import get_adapter
        msg = get_adapter().start()
        print(f"[feishu] {msg}")
    except Exception as e:  # noqa: BLE001
        print(f"[feishu] 自动启动失败：{e}")


def _seed_demo_data() -> None:
    from .database import SessionLocal
    from .models import Skill, MCPServer, Expert, FeishuApp, PlatformTool, Setting
    from . import platform_tools as pt_mod
    from . import settings_store

    db = SessionLocal()
    try:
        if not db.query(FeishuApp).first():
            db.add(FeishuApp(app_id="cli_demo", app_name="Hermes 专家机器人", mode="A", enabled=True))
            db.commit()

        # 平台设置默认值
        for k, v in settings_store.DEFAULTS.items():
            if not db.get(Setting, k):
                db.add(Setting(key=k, value=v))
        db.commit()

        # 平台内置工具
        for d in pt_mod.TOOL_DEFINITIONS:
            if not db.query(PlatformTool).filter_by(name=d["name"]).first():
                db.add(PlatformTool(**d))
        db.commit()

        if not db.query(Skill).first():
            db.add_all([
                Skill(
                    name="sql-query",
                    version="1.2.0",
                    category="data",
                    description="针对内部数仓执行只读 SQL 查询并格式化结果",
                    tags="sql,database",
                    content="## 用法\n当需要查询内部数据时调用本 Skill：\n1. 确认查询口径\n2. 构造只读 SELECT\n3. 调用 run_sql_query 工具\n4. 格式化结果回传",
                ),
                Skill(
                    name="report-render",
                    version="1.0.0",
                    category="data",
                    description="把查询结果渲染为表格/图表并下发",
                    tags="report,chart",
                    content="## 用法\n基于 sql-query 的结果生成可视化报表（Markdown 表格）。",
                ),
                Skill(
                    name="contract-review",
                    version="1.0.0",
                    category="legal",
                    description="审查合同条款、标注风险点",
                    tags="legal,contract",
                    content="## 用法\n通读合同 → 抽取关键条款 → 标注风险 → 给出修改建议。需要检索法条时调用 web_search 工具。",
                ),
            ])
            db.commit()
        if not db.query(MCPServer).first():
            db.add_all([
                MCPServer(
                    name="internal-db",
                    transport="http",
                    config_template='{"url":"https://mcp.internal.corp:8443/mcp","headers":{"Authorization":"Bearer demo-token"}}',
                    tools_filter="list_datasets,run_sql_query",
                ),
                MCPServer(
                    name="github",
                    transport="stdio",
                    config_template='{"command":"npx","args":["-y","@modelcontextprotocol/server-github"],"env":{"GITHUB_PERSONAL_ACCESS_TOKEN":"demo"}}',
                    tools_filter="delete_repo",
                ),
            ])
            db.commit()

        # 平台工具名 -> id 映射
        tool_by_name = {t.name: t for t in db.query(PlatformTool).all()}
        data_tools = [tool_by_name[n] for n in ("list_datasets", "run_sql_query", "calculator", "get_current_time") if n in tool_by_name]
        legal_tools = [tool_by_name[n] for n in ("web_search", "get_current_time") if n in tool_by_name]

        if not db.query(Expert).first():
            da = Expert(
                name="数据分析专家",
                description="负责报表与指标分析，先问清口径再取数",
                system_prompt="你是一名资深数据分析师。收到任务后：1) 先与用户确认统计口径（时间/维度/指标），口径不清时主动追问；2) 调用 run_sql_query 工具取数；3) 用 Markdown 表格呈现结果并给出结论。不要编造数据。",
                model="deepseek/deepseek-chat",
                profile_name="data-analyst",
                status="active",
                feishu_visible=True,
            )
            da.skills = db.query(Skill).filter(Skill.name.in_(["sql-query", "report-render"])).all()
            da.mcp_servers = db.query(MCPServer).filter(MCPServer.name == "internal-db").all()
            da.platform_tools = data_tools
            legal = Expert(
                name="法务专家",
                description="审查合同条款、标注风险点与修改建议",
                system_prompt="你是一名资深法务。通读合同全文，标注高风险条款（违约责任、知识产权、保密、终止条件），给出修改建议与法理依据。需要检索时调用 web_search 工具。",
                model="deepseek/deepseek-chat",
                profile_name="legal-counsel",
                status="active",
                feishu_visible=True,
            )
            legal.skills = db.query(Skill).filter(Skill.name == "contract-review").all()
            legal.platform_tools = legal_tools
            db.add_all([da, legal])
            db.commit()
            from .services import ProfileRenderer
            for e in [da, legal]:
                ProfileRenderer.render(e)
        else:
            # 兼容已有 DB：若专家未绑定平台工具，按 profile 名启发式补绑，并把模型迁到 deepseek
            for e in db.query(Expert).all():
                if e.model and e.model.startswith("anthropic"):
                    e.model = "deepseek/deepseek-chat"
                if not e.platform_tools:
                    if "data" in (e.profile_name or "") or "analyst" in (e.profile_name or ""):
                        e.platform_tools = list({t.id: t for t in data_tools}.values())
                    elif "legal" in (e.profile_name or "") or "counsel" in (e.profile_name or ""):
                        e.platform_tools = list({t.id: t for t in legal_tools}.values())
                    else:
                        merged = {t.id: t for t in data_tools + legal_tools}
                        e.platform_tools = list(merged.values())
            db.commit()
            from .services import ProfileRenderer
            for e in db.query(Expert).all():
                ProfileRenderer.render(e)
    finally:
        db.close()


# 路由挂载
app.include_router(auth.router)
app.include_router(experts.router)
app.include_router(skills.router)
app.include_router(mcp_servers.router)
app.include_router(settings_router.router)
app.include_router(platform_tools.router)
app.include_router(feishu.router)


@app.get("/api/health")
def health():
    return {"ok": True, "ts": __import__("datetime").datetime.now().isoformat(timespec="seconds")}


# 前端单页：根路径返回 index.html
@app.get("/", response_class=HTMLResponse)
def index():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))


# 静态资源（若需要）
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
