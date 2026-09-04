"""Skill 中央仓库 CRUD。"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..auth import require_admin
from ..models import Skill
from ..schemas import SkillCreate, SkillOut

router = APIRouter(prefix="/api/skills", tags=["skills"])


@router.get("", response_model=list[SkillOut])
def list_skills(db: Session = Depends(get_db)):
    return db.query(Skill).order_by(Skill.id.desc()).all()


@router.post("", response_model=SkillOut, dependencies=[Depends(require_admin)])
def create_skill(body: SkillCreate, db: Session = Depends(get_db)):
    if db.query(Skill).filter_by(name=body.name).first():
        raise HTTPException(400, f"Skill '{body.name}' 已存在")
    sk = Skill(**body.model_dump())
    db.add(sk)
    db.commit()
    db.refresh(sk)
    return sk


@router.delete("/{skill_id}", dependencies=[Depends(require_admin)])
def delete_skill(skill_id: int, db: Session = Depends(get_db)):
    sk = db.get(Skill, skill_id)
    if not sk:
        raise HTTPException(404, "Skill 不存在")
    db.delete(sk)
    db.commit()
    return {"ok": True}
