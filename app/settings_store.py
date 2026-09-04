"""平台设置读写辅助：把 Setting 表读成扁平 dict / 写回。"""
from __future__ import annotations

from .models import Setting

DEFAULTS = {
    "deepseek_api_key": "",
    "deepseek_base_url": "https://api.deepseek.com/v1",
    "deepseek_default_model": "deepseek-chat",
    "feishu_app_id": "",
    "feishu_app_secret": "",
    "feishu_allowed_users": "",   # 逗号分隔 open_id；空 = 允许所有人（演示用）
    "feishu_enabled": "false",
}


def load(db) -> dict[str, str]:
    """读取所有设置，合并默认值，返回扁平 dict。"""
    rows = {s.key: s.value for s in db.query(Setting).all()}
    out = dict(DEFAULTS)
    out.update(rows)
    return out


def upsert(db, key: str, value: str) -> None:
    """写入或更新单个设置项。"""
    s = db.get(Setting, key)
    if s is None:
        db.add(Setting(key=key, value=value))
    else:
        s.value = value
    db.commit()


def mask_key(key: str) -> str:
    """API Key 脱敏：仅保留末 4 位。"""
    if not key:
        return ""
    if len(key) <= 4:
        return "*" * len(key)
    return "*" * (len(key) - 4) + key[-4:]
