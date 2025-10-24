# services/informes_helpers.py
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

__all__ = [
    "parse_hhmm", "task_local_now", "window_bounds_with_tol",
    "confirm_window_checker"
]

def parse_hhmm(s: str, fallback="08:00"):
    try:
        h, m = map(int, (s or fallback).split(":"))
        return h, m
    except Exception:
        return 8, 0

def task_local_now(tarea) -> datetime:
    return datetime.now(ZoneInfo(getattr(tarea, "tz_name", None) or "UTC"))

def window_bounds_with_tol(tarea, base_local: datetime, ini_hm: tuple[int,int], fin_hm: tuple[int,int]):
    tol_min = int(getattr(tarea, "tolerancia_min", 0) or 0)
    ini_loc = base_local.replace(hour=ini_hm[0], minute=ini_hm[1], second=0, microsecond=0) - timedelta(minutes=tol_min)
    fin_loc = base_local.replace(hour=fin_hm[0], minute=fin_hm[1], second=0, microsecond=0) + timedelta(minutes=tol_min)
    return ini_loc, fin_loc

def confirm_window_checker(mode: str, start_local: datetime, hours: int, now_local: datetime) -> bool:
    """
    mode='opens_3h_before'  -> ok si now >= start - H
    mode='closes_3h_before' -> ok si now <= start - H
    """
    if hours < 0: hours = 0
    pivot = start_local - timedelta(hours=hours)
    if (mode or "").lower() == "closes_3h_before":
        return now_local <= pivot
    # default abre desde H horas antes
    return now_local >= pivot
