"""平台设置路由：DeepSeek + 飞书 App 配置。"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import get_db
from ..auth import require_admin
from ..schemas import SettingsOut, SettingsUpdate
from .. import settings_store

router = APIRouter(prefix="/api/settings", tags=["settings"])

# 敏感字段：脱敏返回；写入时若为占位（**** 开头）则跳过
SECRET_KEYS = ("deepseek_api_key", "feishu_app_secret")


def _mask(key: str) -> str:
    return settings_store.mask_key(key)


def _to_out(db) -> SettingsOut:
    s = settings_store.load(db)
    return SettingsOut(
        deepseek_api_key=_mask(s["deepseek_api_key"]),
        deepseek_api_key_set=bool(s["deepseek_api_key"]),
        deepseek_base_url=s["deepseek_base_url"],
        deepseek_default_model=s["deepseek_default_model"],
        feishu_app_id=s["feishu_app_id"],
        feishu_app_secret=_mask(s["feishu_app_secret"]),
        feishu_app_secret_set=bool(s["feishu_app_secret"]),
        feishu_allowed_users=s["feishu_allowed_users"],
        feishu_enabled=str(s.get("feishu_enabled", "false")).lower() == "true",
    )


@router.get("", response_model=SettingsOut, dependencies=[Depends(require_admin)])
def get_settings(db: Session = Depends(get_db)):
    return _to_out(db)


@router.put("", response_model=SettingsOut, dependencies=[Depends(require_admin)])
def update_settings(body: SettingsUpdate, db: Session = Depends(get_db)):
    for k in (
        "deepseek_api_key", "deepseek_base_url", "deepseek_default_model",
        "feishu_app_id", "feishu_app_secret", "feishu_allowed_users",
    ):
        v = getattr(body, k)
        if v is not None:
            # 脱敏占位（**** 开头）不覆盖原值
            if k in SECRET_KEYS and v.startswith("*"):
                continue
            settings_store.upsert(db, k, v)
    if body.feishu_enabled is not None:
        settings_store.upsert(db, "feishu_enabled", "true" if body.feishu_enabled else "false")
    return _to_out(db)
