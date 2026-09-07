"""飞书接入与专家选择（模式 A · 演示版模拟器）。
对应技术方案 §7：用户选专家 → 平台路由 → Hermes 执行 → 回传 + 审计。
"""
import datetime as dt
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..auth import require_admin
from ..models import Expert, CallLog, FeishuApp
from ..schemas import FeishuRunIn, FeishuRunOut, FeishuSelectOut, ExpertOut, ToolCallTrace, FeishuStatusOut, ExpertConnStatus
from ..services import HermesExecutor
from .. import settings_store
from ..feishu_adapter import get_adapter
from .experts import _to_out

router = APIRouter(prefix="/api/feishu", tags=["feishu"])


@router.get("/experts", response_model=FeishuSelectOut)
def visible_experts(db: Session = Depends(get_db)):
    """专家选择卡片：仅返回飞书可见 + 启用中的专家。"""
    qs = (
        db.query(Expert)
        .filter(Expert.feishu_visible.is_(True), Expert.status == "active")
        .order_by(Expert.id.desc())
        .all()
    )
    return FeishuSelectOut(experts=[_to_out(e) for e in qs])


@router.post("/run", response_model=FeishuRunOut)
def run_task(body: FeishuRunIn, db: Session = Depends(get_db)):
    """飞书用户选择专家 → 路由到该专家 Hermes → 执行 → 回传 + 审计。
    配置了 DeepSeek Key 时真实调用 DeepSeek function calling；否则模拟回显。"""
    e = db.get(Expert, body.expert_id)
    if not e:
        raise HTTPException(404, "专家不存在")
    if not e.feishu_visible or e.status != "active":
        raise HTTPException(403, f"专家 {e.name} 已停用或对飞书不可见")

    settings = settings_store.load(db)

    # 路由 → Hermes（执行面）
    result = HermesExecutor.run(e, body.message, settings=settings)

    # 审计入库
    log = CallLog(
        expert_id=e.id,
        user_open_id=body.user_open_id,
        channel=body.channel,
        message=body.message,
        response=result["response"],
        skill_used=result["skill_used"],
        mcp_used=result["mcp_used"],
        tokens=result["tokens"],
        latency_ms=result["latency_ms"],
        status=result["provider"],
    )
    db.add(log)
    db.commit()

    return FeishuRunOut(
        expert_id=e.id,
        expert_name=e.name,
        user_open_id=body.user_open_id,
        message=body.message,
        response=result["response"],
        skill_used=result["skill_used"],
        mcp_used=result["mcp_used"],
        tokens=result["tokens"],
        latency_ms=result["latency_ms"],
        profile_dir=result["profile_dir"],
        provider=result["provider"],
        tool_calls=[ToolCallTrace(**tc) for tc in result.get("tool_calls", [])],
        model=result.get("model"),
        iterations=result.get("iterations"),
    )


@router.get("/apps")
def list_apps(db: Session = Depends(get_db)):
    return db.query(FeishuApp).all()


@router.post("/apps", dependencies=[])
def seed_app(db: Session = Depends(get_db)):
    """演示用：确保有一个飞书 App 记录（模式 A）。"""
    if not db.query(FeishuApp).first():
        db.add(FeishuApp(app_id="cli_demo", app_name="Hermes 专家机器人", mode="A", enabled=True))
        db.commit()
    return db.query(FeishuApp).all()


# ---------- 真实飞书连接控制（模式 B：每专家独立连接） ----------
@router.get("/status", response_model=FeishuStatusOut, dependencies=[Depends(require_admin)])
def feishu_status():
    s = get_adapter().status()
    return FeishuStatusOut(
        enabled=s["enabled"],
        running=s["running"],
        connections=[ExpertConnStatus(**c) for c in s["connections"]],
        error=s["error"],
        last_event_at=s["last_event_at"],
    )


@router.post("/start", dependencies=[Depends(require_admin)])
def feishu_start():
    msg = get_adapter().start()
    s = get_adapter().status()
    return {"ok": True, "message": msg, "status": s}


@router.post("/stop", dependencies=[Depends(require_admin)])
def feishu_stop():
    msg = get_adapter().stop()
    s = get_adapter().status()
    return {"ok": True, "message": msg, "status": s}


@router.post("/restart", dependencies=[Depends(require_admin)])
def feishu_restart():
    msg = get_adapter().restart()
    s = get_adapter().status()
    return {"ok": True, "message": msg, "status": s}
