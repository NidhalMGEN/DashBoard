"""Historique persistant des exécutions (pipeline ETL + requêtes SQL).

Stockage volontairement simple et portable : un fichier JSON-lines sous
`Output/run_history.jsonl` (1 ligne = 1 run). Aucune dépendance externe,
fonctionne même si la base est injoignable, et survit aux redémarrages.

Chaque enregistrement :
    {
      "id": "...", "kind": "pipeline"|"sql", "ts_start": ISO, "ts_end": ISO,
      "duration_s": float, "success": bool, "user": str,
      "summary": str, "details": {...}
    }
"""

from __future__ import annotations

import json
import uuid
import threading
import datetime
from pathlib import Path

_LOCK = threading.Lock()
_FILE_NAME = "run_history.jsonl"
_MAX_KEEP = 500  # on borne le fichier pour éviter une croissance illimitée


def _history_path(base_dir: Path) -> Path:
    out = Path(base_dir) / "Output"
    out.mkdir(parents=True, exist_ok=True)
    return out / _FILE_NAME


def record(base_dir: Path, entry: dict) -> dict:
    """Ajoute un enregistrement (complète id + horodatage si absents)."""
    entry = dict(entry)
    entry.setdefault("id", uuid.uuid4().hex[:12])
    entry.setdefault("ts_end", datetime.datetime.now().isoformat(timespec="seconds"))
    path = _history_path(base_dir)
    with _LOCK:
        try:
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            _truncate(path)
        except Exception:
            # L'historique ne doit jamais casser un run : échec silencieux.
            pass
    return entry


def _truncate(path: Path) -> None:
    """Conserve uniquement les _MAX_KEEP dernières lignes."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        if len(lines) > _MAX_KEEP:
            path.write_text("\n".join(lines[-_MAX_KEEP:]) + "\n", encoding="utf-8")
    except Exception:
        pass


def get(base_dir: Path, run_id: str) -> dict | None:
    """Retourne l'enregistrement complet d'un run (avec ses logs) par id."""
    path = _history_path(base_dir)
    if not path.exists():
        return None
    # Lecture sous verrou : record() écrit/tronque le même fichier sous _LOCK.
    with _LOCK:
        try:
            content = path.read_text(encoding="utf-8")
        except Exception:
            return None
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("id") == run_id:
            return rec
    return None


def recent(base_dir: Path, n: int = 50, kind: str | None = None) -> list[dict]:
    """Retourne les n derniers runs (le plus récent en premier)."""
    path = _history_path(base_dir)
    if not path.exists():
        return []
    out = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if kind and rec.get("kind") != kind:
                continue
            # On allège la liste : les logs (volumineux) sont récupérés à la
            # demande via get(id). On expose juste leur disponibilité — sans
            # re-parser la ligne (rec est déjà désérialisé).
            has_logs = bool(rec.get("logs"))
            rec = {k: v for k, v in rec.items() if k != "logs"}
            rec["has_logs"] = has_logs
            out.append(rec)
    except Exception:
        return []
    out.reverse()
    return out[:n]
