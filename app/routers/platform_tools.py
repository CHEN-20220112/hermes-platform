"""平台内置可执行工具列表（只读，工具由 platform_tools.py 注册）。"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import get_db
from ..auth import require_admin
from ..models import PlatformTool
from ..schemas import PlatformToolOut

router = APIRouter(prefix="/api/platform-tools", tags=["platform-tools"])


@router.get("", response_model=list[PlatformToolOut])
def list_tools(db: Session = Depends(get_db)):
    return db.query(PlatformTool).order_by(PlatformTool.id.asc()).all()


@router.post("/reseed", dependencies=[Depends(require_admin)])
def reseed(db: Session = Depends(get_db)):
    """补齐内置工具（main.py 启动已种过，这里供手动补齐）。"""
    from .. import platform_tools as pt
    for d in pt.TOOL_DEFINITIONS:
        if not db.query(PlatformTool).filter_by(name=d["name"]).first():
            db.add(PlatformTool(**d))
    db.commit()
    return {"ok": True}
