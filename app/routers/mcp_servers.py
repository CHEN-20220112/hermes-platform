"""MCP Server 注册 CRUD。"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..auth import require_admin
from ..models import MCPServer
from ..schemas import MCPServerCreate, MCPServerOut

router = APIRouter(prefix="/api/mcp-servers", tags=["mcp"])


@router.get("", response_model=list[MCPServerOut])
def list_mcp(db: Session = Depends(get_db)):
    return db.query(MCPServer).order_by(MCPServer.id.desc()).all()


@router.post("", response_model=MCPServerOut, dependencies=[Depends(require_admin)])
def create_mcp(body: MCPServerCreate, db: Session = Depends(get_db)):
    if db.query(MCPServer).filter_by(name=body.name).first():
        raise HTTPException(400, f"MCP Server '{body.name}' 已存在")
    srv = MCPServer(**body.model_dump())
    db.add(srv)
    db.commit()
    db.refresh(srv)
    return srv


@router.put("/{mcp_id}", response_model=MCPServerOut, dependencies=[Depends(require_admin)])
def update_mcp(mcp_id: int, body: MCPServerCreate, db: Session = Depends(get_db)):
    srv = db.get(MCPServer, mcp_id)
    if not srv:
        raise HTTPException(404, "MCP Server 不存在")
    dup = db.query(MCPServer).filter(MCPServer.name == body.name, MCPServer.id != mcp_id).first()
    if dup:
        raise HTTPException(400, f"MCP Server '{body.name}' 已存在")
    for k, v in body.model_dump().items():
        setattr(srv, k, v)
    db.commit()
    db.refresh(srv)
    # MCP 配置变更后，重渲染绑定了该 MCP 的专家 Profile
    _rerender_bound_experts(db, srv)
    return srv


def _rerender_bound_experts(db: Session, srv: MCPServer):
    from ..services import ProfileRenderer
    for e in srv.experts:
        ProfileRenderer.render(e)


@router.delete("/{mcp_id}", dependencies=[Depends(require_admin)])
def delete_mcp(mcp_id: int, db: Session = Depends(get_db)):
    srv = db.get(MCPServer, mcp_id)
    if not srv:
        raise HTTPException(404, "MCP Server 不存在")
    db.delete(srv)
    db.commit()
    return {"ok": True}
