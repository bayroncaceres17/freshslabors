# services/telefono.py
import re

__all__ = ["normalize_e164", "only_digits"]

def only_digits(s: str) -> str:
    return re.sub(r"\D+", "", (s or ""))

def normalize_e164(raw: str) -> str:
    """
    Normaliza un teléfono a formato E.164 suave (no estrictísimo):
    - Quita espacios, (), ., -, etc.
    - 00xxxx -> +xxxx
    - Si queda sin '+' y tiene 8–15 dígitos, antepone '+'.
    """
    s = (raw or '').strip()
    s = re.sub(r'[\s().-]', '', s)
    if s.startswith('00') and s[2:].isdigit():
        s = '+' + s[2:]
    if not s.startswith('+') and s.isdigit() and 8 <= len(s) <= 15:
        s = '+' + s
    return s
