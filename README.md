# Hermes 专家配置与飞书服务平台

管理员在后台配置「专家」（系统提示词 + Skill + MCP + 平台工具 + 模型），平台下发 Hermes Profile，飞书用户在对话中选择专家并发送任务，平台路由到对应专家执行（DeepSeek function calling），回传结果并审计。

## 快速开始

### 1. 安装依赖

```bash
cd d:\agentplatform
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install lark-oapi
```

### 2. 启动服务

```bash
.venv\Scripts\uvicorn app.main:app --port 8000
```

服务启动后：
- 管理后台：浏览器打开 `http://127.0.0.1:8000`
- 默认账号：`admin` / `admin123`
- API 文档：`http://127.0.0.1:8000/docs`

### 3. 配置 DeepSeek

管理后台 → 「平台设置」→ 填入 DeepSeek API Key → 保存。

配置后，专家执行任务时将真实调用 DeepSeek function calling；未配置则回退到模拟回显。

### 4. 配置飞书

#### 4.1 创建飞书自建应用

1. 打开 [open.feishu.cn/app](https://open.feishu.cn/app) → 创建企业自建应用
2. 记录 **App ID** 和 **App Secret**

#### 4.2 开通权限

「权限管理」→ 开通以下权限：

| 权限 | 说明 |
|------|------|
| `im:message` | 接收用户消息 |
| `im:message:send_as_bot` | 以机器人身份发送消息 |
| `im:chat:readonly` | 获取群聊信息 |

#### 4.3 配置事件订阅

「事件与回调」页面：

**事件订阅**：
- 请求方式 = **「使用长连接接收回调」**
- 添加事件 `im.message.receive_v1`

**回调**：
- 请求方式 = **「使用长连接接收回调」**

> **重要**：必须先启动平台服务（WS 连接已建立），才能在飞书开放平台保存「长连接」订阅方式。

#### 4.4 启用机器人并发布

1. 「机器人」→ 启用机器人，设置名称和头像
2. 「版本管理与发布」→ 创建版本 → 发布

#### 4.5 平台填入凭证

管理后台 → 「平台设置」→ 「飞书接入」卡片：
- 填入 App ID / App Secret
- 选择「启用」
- 保存 → 点「启动连接」
- 「刷新状态」确认 WS 连接 = 运行中

### 5. 在飞书中使用

找到机器人（搜索机器人名称或从工作台进入）：

| 操作 | 效果 |
|------|------|
| 发送「专家」 | 收到专家列表 + 选择卡片 |
| 回复数字（如 `1`） | 按编号选择专家 |
| 点击卡片按钮 | 选择对应专家 |
| 选好后直接发任务 | 调用该专家执行 |
| `用 数据分析专家：本周销量排行` | 指令式一步到位 |

## 内置专家

服务启动时自动种入两个示例专家：

| 专家 | Profile | 绑定工具 | 用途 |
|------|---------|----------|------|
| 数据分析专家 | data-analyst | list_datasets, run_sql_query, calculator, get_current_time | 报表与指标分析 |
| 法务专家 | legal-counsel | web_search, get_current_time | 合同审查与风险标注 |

## 内置平台工具

| 工具 | 功能 |
|------|------|
| `calculator` | 安全的算术表达式计算 |
| `list_datasets` | 列出可用数据集（演示用模拟数据） |
| `run_sql_query` | 对演示数据集执行只读 SQL 查询 |
| `web_search` | 模拟网络搜索（演示用固定结果） |
| `get_current_time` | 获取当前时间 |

## 管理后台功能

| 页面 | 功能 |
|------|------|
| 专家配置 | 创建/编辑专家，绑定 Skill/MCP/平台工具，下发 Hermes Profile |
| Skill 仓库 | 管理可复用的 SKILL.md |
| MCP 管理 | 注册外部 MCP Server（stdio/http） |
| 平台工具 | 查看内置可执行工具 |
| 飞书模拟器 | 在管理后台直接模拟飞书对话，测试专家执行 |
| 平台设置 | DeepSeek 配置 + 飞书 App 凭证 + 连接控制 |

## 常见问题

### 飞书发消息无响应

1. 检查平台「平台设置」中飞书状态 = 运行中
2. 确认飞书开放平台「事件订阅」请求方式 = 长连接
3. 确认权限 `im:message` 已开通
4. 确认应用已发布
5. 群聊需 @机器人

### 执行任务报错

1. 确认 DeepSeek API Key 已配置
2. 确认模型为 `deepseek-chat`（支持 function calling）
3. 查看终端日志中的 `[Lark]` 输出

### 端口被占用

```bash
# 换端口
.venv\Scripts\uvicorn app.main:app --port 8080
```
