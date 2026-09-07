"""数据库连接与 Base 声明（SQLite 起步，最简依赖）。"""
from __future__ import annotations

import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

# 数据库文件放在项目根 data 目录，避免权限问题
DB_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
os.makedirs(DB_DIR, exist_ok=True)
DB_PATH = os.path.join(DB_DIR, "hermes_platform.db")
DB_URL = f"sqlite:///{DB_PATH}"

engine = create_engine(
    DB_URL,
    connect_args={"check_same_thread": False},
    echo=False,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


def get_db():
    """FastAPI 依赖：每个请求一个会话。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """建表 + 种子管理员账号 + 轻量迁移。延迟 import 以避免循环依赖。"""
    from .models import Admin  # noqa: WPS433

    Base.metadata.create_all(bind=engine)
    _migrate()
    db = SessionLocal()
    try:
        if not db.query(Admin).first():
            db.add(Admin(username="admin", password_hash=_hash("admin123")))
            db.commit()
    finally:
        db.close()


def _migrate() -> None:
    """轻量迁移：为已存在的表补充新列（SQLite ALTER TABLE ADD COLUMN）。"""
    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(expert)").fetchall()}
        if "feishu_app_id" not in cols:
            conn.execute("ALTER TABLE expert ADD COLUMN feishu_app_id TEXT")
        if "feishu_app_secret" not in cols:
            conn.execute("ALTER TABLE expert ADD COLUMN feishu_app_secret TEXT")
        conn.commit()
    except Exception:
        pass  # 表可能还不存在（首次建库），create_all 会处理
    finally:
        conn.close()


def _hash(pw: str) -> str:
    import hashlib

    return hashlib.sha256(f"hermes::{pw}".encode()).hexdigest()


def verify_password(pw: str, pw_hash: str) -> bool:
    return _hash(pw) == pw_hash
