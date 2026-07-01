"""Persistance de la configuration Admin (#9).

Stocke les surcharges éditables depuis le module Admin (seuils d'alerte, etc.)
dans `config/admin_config.json`. Robuste : jamais d'exception remontée ; en
l'absence de fichier on retombe sur les valeurs par défaut du code.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

_PATH = Path(__file__).resolve().parent / "admin_config.json"
_LOCK = threading.Lock()


def load() -> dict:
    if not _PATH.is_file():
        return {}
    try:
        with _LOCK:
            data = json.loads(_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save(cfg: dict) -> bool:
    try:
        with _LOCK:
            _PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                             encoding="utf-8")
        return True
    except Exception:
        return False


def get_thresholds(defaults: dict) -> dict:
    """Fusionne les seuils par défaut (code) avec les surcharges Admin."""
    merged = {k: dict(v) for k, v in defaults.items()}
    overrides = load().get("thresholds") or {}
    for key, vals in overrides.items():
        if key in merged and isinstance(vals, dict):
            for f in ("green", "orange"):
                if isinstance(vals.get(f), (int, float)):
                    merged[key][f] = vals[f]
    return merged


def save_thresholds(thresholds: dict) -> bool:
    cfg = load()
    cfg["thresholds"] = thresholds
    return save(cfg)
