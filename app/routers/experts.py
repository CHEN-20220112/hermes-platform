"""专家配置 CRUD + 下发渲染 —— 核心路由。"""
import os
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..database import get_db
from ..auth import require_admin
from ..models import Expert, Skill, MCPServer, PlatformTool
from ..schemas import ExpertCreate, ExpertUpdate, ExpertOut
from ..services import ProfileRenderer, _profile_dir

router = APIRouter(prefix="/api/experts", tags=["experts"])


def _to_out(expert: Expert) -> ExpertOut:
    return ExpertOut(
        id=expert.id,
        name=expert.name,
        description=expert.description,
        system_prompt=expert.system_prompt,
        model=expert.model,
        profile_name=expert.profile_name,
        status=expert.status,
        feishu_visible=expert.feishu_visible,
        skill_ids=[s.id for s in expert.skills],
        mcp_ids=[m.id for m in expert.mcp_servers],
        platform_tool_ids=[t.id for t in expert.platform_tools],
        rendered=os.path.exists(os.path.join(_profile_dir(expert.profile_name), "SOUL.md")),
        created_at=expert.created_at,
    )


@router.get("", response_model=list[ExpertOut])
def list_experts(db: Session = Depends(get_db), visible_only: bool = Query(False)):
    q = db.query(Expert)
    if visible_only:
        q = q.filter(Expert.feishu_visible.is_(True), Expert.status == "active")
    return [_to_out(e) for e in q.order_by(Expert.id.desc()).all()]


@router.get("/{expert_id}", response_model=ExpertOut)
def get_expert(expert_id: int, db: Session = Depends(get_db)):
    e = db.get(Expert, expert_id)
    if not e:
        raise HTTPException(404, "专家不存在")
    return _to_out(e)


@router.post("", response_model=ExpertOut, dependencies=[Depends(require_admin)])
def create_expert(body: ExpertCreate, db: Session = Depends(get_db)):
    if db.query(Expert).filter_by(profile_name=body.profile_name).first():
        raise HTTPException(400, f"Profile 名 '{body.profile_name}' 已存在")
    e = Expert(
        name=body.name,
        description=body.description,
        system_prompt=body.system_prompt,
        model=body.model,
        profile_name=body.profile_name,
        status=body.status,
        feishu_visible=body.feishu_visible,
    )
    if body.skill_ids:
        e.skills = db.query(Skill).filter(Skill.id.in_(body.skill_ids)).all()
    if body.mcp_ids:
        e.mcp_servers = db.query(MCPServer).filter(MCPServer.id.in_(body.mcp_ids)).all()
    if body.platform_tool_ids:
        e.platform_tools = db.query(PlatformTool).filter(PlatformTool.id.in_(body.platform_tool_ids)).all()
    db.add(e)
    db.commit()
    db.refresh(e)
    # 配置即下发
    ProfileRenderer.render(e)
    return _to_out(e)


@router.put("/{expert_id}", response_model=ExpertOut, dependencies=[Depends(require_admin)])
def update_expert(expert_id: int, body: ExpertUpdate, db: Session = Depends(get_db)):
    e = db.get(Expert, expert_id)
    if not e:
        raise HTTPException(404, "专家不存在")
    data = body.model_dump(exclude_unset=True)
    skill_ids = data.pop("skill_ids", None)
    mcp_ids = data.pop("mcp_ids", None)
    tool_ids = data.pop("platform_tool_ids", None)
    for k, v in data.items():
        setattr(e, k, v)
    if skill_ids is not None:
        e.skills = db.query(Skill).filter(Skill.id.in_(skill_ids)).all()
    if mcp_ids is not None:
        e.mcp_servers = db.query(MCPServer).filter(MCPServer.id.in_(mcp_ids)).all()
    if tool_ids is not None:
        e.platform_tools = db.query(PlatformTool).filter(PlatformTool.id.in_(tool_ids)).all()
    db.commit()
    db.refresh(e)
    # 变更即下发
    ProfileRenderer.render(e)
    return _to_out(e)


@router.delete("/{expert_id}", dependencies=[Depends(require_admin)])
def delete_expert(expert_id: int, db: Session = Depends(get_db)):
    e = db.get(Expert, expert_id)
    if not e:
        raise HTTPException(404, "专家不存在")
    profile = e.profile_name
    db.delete(e)
    db.commit()
    # 清理 Profile 目录
    import shutil

    pdir = _profile_dir(profile)
    if os.path.exists(pdir):
        shutil.rmtree(pdir, ignore_errors=True)
    return {"ok": True}


@router.post("/{expert_id}/reload", dependencies=[Depends(require_admin)])
def reload_expert(expert_id: int, db: Session = Depends(get_db)):
    """手动触发重渲染（模拟热加载）。"""
    e = db.get(Expert, expert_id)
    if not e:
        raise HTTPException(404, "专家不存在")
    pdir = ProfileRenderer.render(e)
    return {"ok": True, "profile_dir": pdir}


@router.get("/{expert_id}/profile-files")
def get_profile_files(expert_id: int, db: Session = Depends(get_db)):
    """回读 Hermes Profile 已下发产物（前端展示）。"""
    e = db.get(Expert, expert_id)
    if not e:
        raise HTTPException(404, "专家不存在")
    return ProfileRenderer.rendered_files(e.profile_name)
