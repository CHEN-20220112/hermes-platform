"""管理员登录路由。"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..database import get_db
from ..auth import issue_token, require_admin
from ..schemas import LoginIn, LoginOut

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login", response_model=LoginOut)
def login(body: LoginIn, db: Session = Depends(get_db)):
    token = issue_token(db, body.username, body.password)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")
    return LoginOut(token=token, username=body.username)


@router.get("/me")
def me(username: str = Depends(require_admin)):
    return {"username": username}
