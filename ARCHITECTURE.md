# 项目架构说明

## 整体架构

```
┌─────────────────────────────────────────────────────────────┐
│                        飞书客户端                            │
│              （私聊 / 群聊 @机器人）                           │
└──────────────────────────┬──────────────────────────────────┘
                           │ WebSocket 长连接
                           │ (lark-oapi SDK)
┌──────────────────────────▼──────────────────────────────────┐
│                    FeishuAdapter (feishu_adapter.py)          │
│  · 接收 im.message.receive_v1 事件                            │
│  · 专家选择卡片 / 文本编号选择                                │
│  · 会话记忆（open_id → expert_id）                           │
│  · 线程池异步执行（避免 3 秒 ACK 超时）                       │
│  · 回传结果 + 审计入库                                        │
└──────────────────────────┬──────────────────────────────────┘
                           │ HermesExecutor.run(expert, task)
┌──────────────────────────▼──────────────────────────────────┐
│                    HermesExecutor (services.py)              │
│  · 有 DeepSeek Key → 真实 Agent 循环                         │
│  · 无 Key → 模拟回显（验证配置链路）                         │
└──────────┬───────────────────────────────┬──────────────────┘
           │                               │
           ▼                               ▼
┌─────────────────────┐     ┌─────────────────────────────────┐
│  agent_loop.py       │     │  ProfileRenderer (services.py)   │
│  · DeepSeek API 调用 │     │  · 渲染 SOUL.md                  │
│  · function calling  │     │  · 渲染 skills/ 目录             │
│  · 工具执行循环       │     │  · 渲染 config.yaml             │
│  · 最多 8 次迭代      │     └─────────────────────────────────┘
└──────────┬──────────┘
           │
           ▼
┌─────────────────────────────────────────────────────────────┐
│              platform_tools.py（内置可执行工具）              │
│  calculator · list_datasets · run_sql_query · web_search     │
│  · get_current_time                                          │
└─────────────────────────────────────────────────────────────┘
```

## 目录结构

```
agentplatform/
├── app/
│   ├── __init__.py
│   ├── main.py                 # FastAPI 入口，路由挂载，启动钩子
│   ├── database.py             # SQLAlchemy 引擎 + Session 工厂
│   ├── models.py               # ORM 模型（7 张表 + 3 张关联表）
│   ├── auth.py                 # 管理员认证（内存 token 池）
│   ├── schemas.py              # Pydantic 请求/响应模型
│   ├── settings_store.py       # 平台设置读写（键值对）
│   ├── services.py             # ProfileRenderer + HermesExecutor
│   ├── agent_loop.py           # DeepSeek function calling Agent 循环
│   ├── deepseek_client.py      # DeepSeek API 客户端（OpenAI 兼容）
│   ├── platform_tools.py       # 内置工具定义 + 执行处理器
│   ├── feishu_adapter.py       # 飞书 WS 长连接适配器
│   └── routers/
│       ├── auth.py             # /api/auth/* 登录、鉴权
│       ├── experts.py           # /api/experts/* 专家 CRUD + Profile 下发
│       ├── skills.py            # /api/skills/* Skill CRUD
│       ├── mcp_servers.py      # /api/mcp-servers/* MCP CRUD
│       ├── platform_tools.py   # /api/platform-tools/* 工具列表
│       ├── settings.py         # /api/settings DeepSeek + 飞书配置
│       └── feishu.py           # /api/feishu/* 飞书模拟器 + 连接控制
├── frontend/
│   └── index.html              # 单页管理后台（原生 JS）
├── data/
│   └── profiles/               # Profile 渲染产物（SOUL.md/config.yaml/skills/）
├── requirements.txt
└── README.md
```

## 核心模块说明

### 1. 数据层（models.py + database.py）

SQLite 数据库，7 张实体表 + 3 张多对多关联表：

| 表 | 说明 |
|----|------|
| `admin` | 管理员账号（用户名 + 密码哈希） |
| `expert` | 专家配置（提示词/模型/Profile 名/状态/飞书可见） |
| `skill` | Skill 中央仓库（SKILL.md 规范） |
| `mcp_server` | MCP Server 注册（stdio/http，含配置模板） |
| `platform_tool` | 平台内置可执行工具（含参数 JSON schema） |
| `setting` | 平台键值配置（DeepSeek Key / 飞书凭证等） |
| `call_log` | 调用审计日志 |
| `expert_skill` | 专家 ↔ Skill 多对多 |
| `expert_mcp` | 专家 ↔ MCP 多对多 |
| `expert_platform_tool` | 专家 ↔ 平台工具 多对多 |

### 2. 管理面（routers/）

所有管理 API 均需 `require_admin` 认证（Bearer Token）。

- **专家配置**：创建/编辑专家时绑定 Skill、MCP、平台工具，保存即调用 `ProfileRenderer.render()` 下发 Profile
- **Skill 仓库**：CRUD SKILL.md 内容
- **MCP 管理**：注册外部 MCP Server，配置传输方式和凭据模板
- **平台设置**：DeepSeek API Key / Base URL / 模型 + 飞书 App ID / Secret / 启用开关

### 3. 配置下发（ProfileRenderer）

专家保存后，`ProfileRenderer.render()` 将配置渲染为 Hermes Profile 文件：

```
data/profiles/<profile_name>/
├── SOUL.md              # 系统提示词（人格/指令）
├── config.yaml          # 模型 + MCP 配置 + 平台工具
└── skills/
    └── <category>/
        └── <skill_name>/
            └── SKILL.md  # Skill 正文（含 frontmatter）
```

### 4. 执行面（HermesExecutor + agent_loop.py）

**HermesExecutor** 是执行分发器：
- 有 DeepSeek Key → 调用 `agent_loop.run_task()` 走真实 Agent 循环
- 无 Key → 模拟回显（返回配置摘要，不真实推理）

**agent_loop.py** 实现 DeepSeek function calling 循环：

```
组装系统提示词 + Skill 指引
    ↓
暴露专家绑定的平台工具（OpenAI function schema）
    ↓
调用 DeepSeek chat completions
    ↓
┌─ 模型返回文本 → 最终答复，循环结束
└─ 模型返回 tool_calls → 执行平台工具 → 结果回灌 → 继续循环
    （最多 8 次迭代）
```

### 5. 飞书适配器（feishu_adapter.py）

通过 `lark-oapi` SDK 建立 WebSocket 长连接，无需公网域名。

**消息处理流程**：

```
飞书消息事件 → _on_message() → 线程池提交
    ↓
_handle_text()（后台线程）：
    · "专家" → 发送专家选择卡片 + 文本编号列表
    · 纯数字 → 按编号选择专家
    · "用 <专家名>:<任务>" → 指令式路由
    · 其他文本 → 凭会话记忆执行上次选的专家
    ↓
_run_and_reply()：
    · 重新加载 Expert（避免 detached session）
    · 回执 "已收到，正在处理…"
    · HermesExecutor.run(expert, task)
    · 审计入库（CallLog）
    · 回传结果（分段发送，支持超长文本）
```

**关键技术点**：
- daemon 线程中新建独立 asyncio 事件循环（解决与 FastAPI 主线程 loop 冲突）
- monkey-patch 修复 lark-oapi CARD 帧丢弃 bug（[Issue #126](https://github.com/larksuite/oapi-sdk-python/issues/126)）
- 线程池异步执行避免飞书 3 秒事件 ACK 超时
- 文本选专家兜底（不依赖卡片回调也能用）

### 6. 前端（frontend/index.html）

原生 JS 单页应用，无构建工具。功能页签：

| 页签 | 功能 |
|------|------|
| 专家配置 | 专家列表、新建/编辑弹窗、Profile 查看 |
| Skill 仓库 | Skill 列表、新建 |
| MCP 管理 | MCP Server 列表、注册 |
| 平台工具 | 内置工具列表 |
| 飞书模拟器 | 管理后台内模拟飞书对话，测试专家执行 |
| 平台设置 | DeepSeek + 飞书配置、连接状态与控制 |

## 配置流（管理员，低频）

```
管理员              平台后台              Hermes Profile
 ① 创建/编辑专家（提示词/Skill/MCP/模型）
   └──→ ② 生成渲染配置
        ③ 写 SOUL.md / skills / config.yaml
        └──────────────→ ④ 加载配置
        ←──────────────  ⑤ 生效确认
   ←── ⑥ 保存成功，专家上线
```

## 运行时流（飞书用户，高频）

```
飞书用户        飞书机器人/平台       Hermes（所选专家）      DeepSeek API
 ① 选专家（卡片/数字/指令）
   └──→ ② 路由到专家 Profile
 ③ 发送任务
   └──→ ④ 组装（系统提示词+Skill+工具）
        └──→ ⑤ DeepSeek function calling
             ⑥ Agent 循环（工具调用→结果回灌）
             └──→ ⑦ 模型推理
             ←──  返回
        ←── ⑧ 结果
   ←── ⑨ 渲染回复
        ⑩ 审计/用量入库
```

## 技术栈

| 层 | 技术 |
|----|------|
| Web 框架 | FastAPI 0.115 + Uvicorn |
| ORM | SQLAlchemy 2.0 |
| 数据库 | SQLite |
| 数据校验 | Pydantic 2.10 |
| 飞书 SDK | lark-oapi 1.7.3（WebSocket 长连接） |
| LLM | DeepSeek API（OpenAI 兼容，function calling） |
| 前端 | 原生 HTML + JS（单文件） |

## 扩展指南

### 新增平台工具

在 `platform_tools.py` 中：
1. 编写执行函数，加入 `HANDLERS` 字典
2. 编写工具定义（name/description/parameters_json），加入 `TOOL_DEFINITIONS`
3. 重启服务，工具自动种入数据库
4. 在专家配置中绑定即可

### 新增专家

管理后台「专家配置」→ 新建专家 → 填写系统提示词 → 绑定 Skill/MCP/工具 → 保存 → 自动下发 Profile → 飞书用户立即可选。

### 接入真实 Hermes

将 `HermesExecutor.run()` 中的 `agent_loop.run_task()` 调用替换为对 Hermes API Server 的 HTTP 调用，循环结构不变。
