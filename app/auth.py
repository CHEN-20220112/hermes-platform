"""管理员鉴权依赖：演示版 token 直接存内存。"""
from __future__ import annotations

import secrets

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from .database import SessionLocal, verify_password
from .models import Admin

# 演示期：单进程内存 token 池。生产应放 Redis。
_TOKENS: dict[str, str] = {}  # token -> username
bearer = HTTPBearer()


def issue_token(db, username: str, password: str) -> str | None:
    admin = db.query(Admin).filter_by(username=username).first()
    if not admin or not verify_password(password, admin.password_hash):
        return None
    token = secrets.token_urlsafe(24)
    _TOKENS[token] = username
    return token


def require_admin(creds: HTTPAuthorizationCredentials = Depends(bearer)) -> str:
    """依赖：校验 token，返回 username。"""
    username = _TOKENS.get(creds.credentials)
    if not username:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未登录或 token 失效")
    return username
