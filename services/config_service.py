# services/config_service.py
from __future__ import annotations
import json, os
from typing import Any, Dict
from pathlib import Path
from flask import current_app

DEFAULTS: Dict[str, Any] = {
    "brand_name": "Fresh's Labors",
    "brand_logo": "",  # /static/img/logo.png
    "brand_footer": "© {{year}} Fresh's Labors. Todos los derechos reservados.",
    "wa_sender": "",   # +57xxxxxxxxxx
    "wa_prefix": "Hola, te comparto la información:",
    "mail_from": "",

    "otp_expiry_min": 10,
    "otp_resend_sec": 30,
    "otp_max_attempts": 5,
    "otp_enabled": True,

    "max_upload_mb": 6,
    "max_colilla_mb": 8,
    "dir_colillas": "uploads/colillas",
    "dir_pagos": "uploads/pagos",

    "table_page_size": 10,
    "theme_default": "auto"  # auto|light|dark
}

def _cfg_path() -> Path:
    root = Path(current_app.root_path)
    data_dir = root / "data"
    data_dir.mkdir(exist_ok=True)
    return data_dir / "app_config.json"

def read_config() -> Dict[str, Any]:
    p = _cfg_path()
    if not p.exists():
        return DEFAULTS.copy()
    try:
        with p.open("r", encoding="utf-8") as f:
            cfg = json.load(f)
        # merge con defaults por si faltan claves
        merged = DEFAULTS.copy()
        merged.update(cfg or {})
        return merged
    except Exception:
        return DEFAULTS.copy()

def write_config(new_cfg: Dict[str, Any]) -> None:
    # limpia tipos básicos
    cfg = read_config()
    cfg.update({
        "brand_name": (new_cfg.get("brand_name") or "").strip(),
        "brand_logo": (new_cfg.get("brand_logo") or "").strip(),
        "brand_footer": (new_cfg.get("brand_footer") or "").strip(),
        "wa_sender": (new_cfg.get("wa_sender") or "").strip(),
        "wa_prefix": (new_cfg.get("wa_prefix") or "").strip(),
        "mail_from": (new_cfg.get("mail_from") or "").strip(),

        "otp_expiry_min": int(new_cfg.get("otp_expiry_min") or 10),
        "otp_resend_sec": int(new_cfg.get("otp_resend_sec") or 30),
        "otp_max_attempts": int(new_cfg.get("otp_max_attempts") or 5),
        "otp_enabled": True if str(new_cfg.get("otp_enabled")).lower() in ("1","true","on","yes") else False,

        "max_upload_mb": int(new_cfg.get("max_upload_mb") or 6),
        "max_colilla_mb": int(new_cfg.get("max_colilla_mb") or 8),
        "dir_colillas": (new_cfg.get("dir_colillas") or "uploads/colillas").strip(),
        "dir_pagos": (new_cfg.get("dir_pagos") or "uploads/pagos").strip(),

        "table_page_size": int(new_cfg.get("table_page_size") or 10),
        "theme_default": (new_cfg.get("theme_default") or "auto"),
    })
    p = _cfg_path()
    with p.open("w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
