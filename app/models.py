"""SQLAlchemy 数据模型 —— 对应技术方案 §9 关键数据模型。"""
from __future__ import annotations

import datetime as dt
from sqlalchemy import (
    Column,
    Integer,
    String,
    Text,
    Boolean,
    DateTime,
    ForeignKey,
    Table,
)
from sqlalchemy.orm import relationship

from .database import Base

# 专家 ↔ Skill 多对多
expert_skill = Table(
    "expert_skill",
    Base.metadata,
    Column("expert_id", Integer, ForeignKey("expert.id", ondelete="CASCADE"), primary_key=True),
    Column("skill_id", Integer, ForeignKey("skill.id", ondelete="CASCADE"), primary_key=True),
)

# 专家 ↔ MCP 多对多（带 per-expert 过滤策略）
expert_mcp = Table(
    "expert_mcp",
    Base.metadata,
    Column("expert_id", Integer, ForeignKey("expert.id", ondelete="CASCADE"), primary_key=True),
    Column("mcp_server_id", Integer, ForeignKey("mcp_server.id", ondelete="CASCADE"), primary_key=True),
)

# 专家 ↔ 平台可执行工具 多对多
expert_platform_tool = Table(
    "expert_platform_tool",
    Base.metadata,
    Column("expert_id", Integer, ForeignKey("expert.id", ondelete="CASCADE"), primary_key=True),
    Column("platform_tool_id", Integer, ForeignKey("platform_tool.id", ondelete="CASCADE"), primary_key=True),
)


class Admin(Base):
    __tablename__ = "admin"
    id = Column(Integer, primary_key=True)
    username = Column(String(64), unique=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    mfa = Column(String(64), nullable=True)  # 演示用占位


class Expert(Base):
    __tablename__ = "expert"
    id = Column(Integer, primary_key=True)
    name = Column(String(128), nullable=False)
    description = Column(Text, default="")
    system_prompt = Column(Text, default="")
    model = Column(String(128), default="anthropic/claude-sonnet-4")
    profile_name = Column(String(128), unique=True, nullable=False)  # Hermes Profile
    status = Column(String(16), default="active")  # active / disabled
    feishu_visible = Column(Boolean, default=True)
    # 模式 B：每个专家独立的飞书 App（在通讯录中作为独立联系人）
    feishu_app_id = Column(String(128), nullable=True)
    feishu_app_secret = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=dt.datetime.utcnow)

    skills = relationship("Skill", secondary=expert_skill, back_populates="experts")
    mcp_servers = relationship("MCPServer", secondary=expert_mcp, back_populates="experts")
    platform_tools = relationship("PlatformTool", secondary=expert_platform_tool, back_populates="experts")


class Skill(Base):
    __tablename__ = "skill"
    id = Column(Integer, primary_key=True)
    name = Column(String(128), unique=True, nullable=False)
    version = Column(String(32), default="1.0.0")
    category = Column(String(64), default="general")
    description = Column(Text, default="")
    tags = Column(String(255), default="")  # 逗号分隔
    content = Column(Text, default="")  # SKILL.md 正文
    created_at = Column(DateTime, default=dt.datetime.utcnow)

    experts = relationship("Expert", secondary=expert_skill, back_populates="skills")


class MCPServer(Base):
    __tablename__ = "mcp_server"
    id = Column(Integer, primary_key=True)
    name = Column(String(128), unique=True, nullable=False)
    transport = Column(String(16), default="http")  # http / stdio
    config_template = Column(Text, default="{}")  # JSON: url/headers 或 command/args/env
    tools_filter = Column(Text, default="")  # 逗号分隔的工具白名单
    created_at = Column(DateTime, default=dt.datetime.utcnow)

    experts = relationship("Expert", secondary=expert_mcp, back_populates="mcp_servers")


class FeishuApp(Base):
    __tablename__ = "feishu_app"
    id = Column(Integer, primary_key=True)
    app_id = Column(String(128), nullable=False)
    app_name = Column(String(128), default="Hermes 专家机器人")
    mode = Column(String(16), default="A")  # A: 单机器人+路由 / B: 一专家一机器人
    enabled = Column(Boolean, default=True)


class PlatformTool(Base):
    """平台内置可执行工具（替代外部 MCP 的本地执行能力，供真实 Agent 循环调用）。"""
    __tablename__ = "platform_tool"
    id = Column(Integer, primary_key=True)
    name = Column(String(128), unique=True, nullable=False)
    description = Column(Text, default="")
    parameters_json = Column(Text, default="{}")  # OpenAI function parameters JSON schema
    category = Column(String(64), default="builtin")
    enabled = Column(Boolean, default=True)

    experts = relationship("Expert", secondary=expert_platform_tool, back_populates="platform_tools")


class Setting(Base):
    """平台键值配置（DeepSeek API Key / Base URL / 默认模型 等）。"""
    __tablename__ = "setting"
    key = Column(String(64), primary_key=True)
    value = Column(Text, default="")
    updated_at = Column(DateTime, default=dt.datetime.utcnow, onupdate=dt.datetime.utcnow)


class CallLog(Base):
    __tablename__ = "call_log"
    id = Column(Integer, primary_key=True)
    expert_id = Column(Integer, ForeignKey("expert.id"), nullable=True)
    user_open_id = Column(String(128), nullable=False)
    channel = Column(String(32), default="feishu")
    message = Column(Text, default="")
    response = Column(Text, default="")
    skill_used = Column(String(255), default="")
    mcp_used = Column(String(255), default="")
    tokens = Column(Integer, default=0)
    latency_ms = Column(Integer, default=0)
    status = Column(String(16), default="ok")
    created_at = Column(DateTime, default=dt.datetime.utcnow)
