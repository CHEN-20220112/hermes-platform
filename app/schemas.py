"""Pydantic 请求/响应模型。"""
from __future__ import annotations

import datetime as dt
from typing import Optional, List
from pydantic import BaseModel, Field, ConfigDict


# ---------- Auth ----------
class LoginIn(BaseModel):
    username: str
    password: str


class LoginOut(BaseModel):
    token: str
    username: str


# ---------- Skill ----------
class SkillBase(BaseModel):
    name: str
    version: str = "1.0.0"
    category: str = "general"
    description: str = ""
    tags: str = ""
    content: str = ""


class SkillCreate(SkillBase):
    pass


class SkillOut(SkillBase):
    id: int
    model_config = ConfigDict(from_attributes=True)


# ---------- MCP ----------
class MCPServerBase(BaseModel):
    name: str
    transport: str = "http"
    config_template: str = "{}"
    tools_filter: str = ""


class MCPServerCreate(MCPServerBase):
    pass


class MCPServerOut(MCPServerBase):
    id: int
    model_config = ConfigDict(from_attributes=True)


# ---------- Expert ----------
class ExpertBase(BaseModel):
    name: str
    description: str = ""
    system_prompt: str = ""
    model: str = "deepseek/deepseek-chat"
    profile_name: str = Field(..., description="Hermes Profile 名，唯一")
    status: str = "active"
    feishu_visible: bool = True
    feishu_app_id: str = ""
    feishu_app_secret: str = ""  # 写入用；输出时脱敏


class ExpertCreate(ExpertBase):
    skill_ids: List[int] = Field(default_factory=list)
    mcp_ids: List[int] = Field(default_factory=list)
    platform_tool_ids: List[int] = Field(default_factory=list)


class ExpertUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    system_prompt: Optional[str] = None
    model: Optional[str] = None
    status: Optional[str] = None
    feishu_visible: Optional[bool] = None
    feishu_app_id: Optional[str] = None
    feishu_app_secret: Optional[str] = None
    skill_ids: Optional[List[int]] = None
    mcp_ids: Optional[List[int]] = None
    platform_tool_ids: Optional[List[int]] = None


class ExpertOut(ExpertBase):
    id: int
    skill_ids: List[int] = Field(default_factory=list)
    mcp_ids: List[int] = Field(default_factory=list)
    platform_tool_ids: List[int] = Field(default_factory=list)
    rendered: bool = False
    feishu_app_secret: str = ""          # 脱敏：仅末 4 位
    feishu_app_secret_set: bool = False  # 是否已配置
    created_at: Optional[dt.datetime] = None
    model_config = ConfigDict(from_attributes=True)


# ---------- Feishu 模拟器 ----------
class FeishuSelectOut(BaseModel):
    """专家选择卡片数据。"""
    experts: List[ExpertOut]


class FeishuRunIn(BaseModel):
    user_open_id: str = "sim_user_001"
    expert_id: int
    message: str
    channel: str = "feishu"


class ToolCallTrace(BaseModel):
    name: str
    args: dict
    result: dict


class FeishuRunOut(BaseModel):
    expert_id: int
    expert_name: str
    user_open_id: str
    message: str
    response: str
    skill_used: str
    mcp_used: str
    tokens: int
    latency_ms: int
    # 演示用：回显 Hermes Profile 的实际渲染产物路径
    profile_dir: str
    # 扩展：真实执行
    provider: str = "mock"  # mock / deepseek / deepseek-error
    tool_calls: List[ToolCallTrace] = Field(default_factory=list)
    model: Optional[str] = None
    iterations: Optional[int] = None


# ---------- Platform Tools ----------
class PlatformToolOut(BaseModel):
    id: int
    name: str
    description: str = ""
    parameters_json: str = "{}"
    category: str = "builtin"
    enabled: bool = True
    model_config = ConfigDict(from_attributes=True)


# ---------- Settings ----------
class SettingItem(BaseModel):
    key: str
    value: str = ""


class SettingsUpdate(BaseModel):
    deepseek_api_key: Optional[str] = None
    deepseek_base_url: Optional[str] = None
    deepseek_default_model: Optional[str] = None
    feishu_app_id: Optional[str] = None
    feishu_app_secret: Optional[str] = None
    feishu_allowed_users: Optional[str] = None
    feishu_enabled: Optional[bool] = None


class SettingsOut(BaseModel):
    deepseek_api_key: str = ""        # 脱敏：仅末 4 位
    deepseek_api_key_set: bool = False
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    deepseek_default_model: str = "deepseek-chat"
    feishu_app_id: str = ""
    feishu_app_secret: str = ""        # 脱敏
    feishu_app_secret_set: bool = False
    feishu_allowed_users: str = ""
    feishu_enabled: bool = False


class ExpertConnStatus(BaseModel):
    """单个专家的飞书 WS 连接状态。"""
    expert_id: int
    expert_name: str = ""
    app_id: str = ""                 # 脱敏
    configured: bool = False         # app_id + app_secret 是否齐全
    running: bool = False            # WS 连接是否在运行
    error: str = ""
    last_event_at: Optional[str] = None


class FeishuStatusOut(BaseModel):
    enabled: bool = False             # 全局开关
    running: bool = False             # 是否有至少一个专家连接在运行
    connections: List[ExpertConnStatus] = Field(default_factory=list)
    error: str = ""
    last_event_at: Optional[str] = None
