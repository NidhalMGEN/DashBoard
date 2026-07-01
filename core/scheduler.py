"""Planificateur léger (#4) — exécution automatique pipeline / requêtes SQL.

Conçu pour l'application portable mono-processus : un thread de fond vérifie
chaque minute les planifications dues et les déclenche en mode non-attendu
(les identifiants proviennent de credentials.ini, les pauses sont franchies
automatiquement). Les planifications sont persistées dans un fichier JSON.

Une planification :
    {
      "id": "...", "name": "...", "kind": "pipeline"|"sql",
      "frequency": "daily"|"weekly", "days": [0..6], "time": "HH:MM",
      "enabled": bool, "config": {...}, "last_fired": "YYYY-MM-DD HH:MM",
      "last_status": "ok"|"error"|null
    }
config pipeline : {"mode": "full"|"selection", "scripts": [...]}
config sql      : {"environment": "PROD", "queries": [...] | null}
"""

from __future__ import annotations

import json
import uuid
import queue
import threading
import datetime
from pathlib import Path

from core import run_history

# RLock : verrou réentrant — add/update/delete prennent le verrou et appellent
# load/save qui le reprennent, sans interblocage.
_LOCK = threading.RLock()
_STORE_NAME = "schedules.json"
_thread: threading.Thread | None = None
_stop = threading.Event()
_base_dir: Path | None = None
# Anti-rejeu en mémoire : bloque un second déclenchement dans la même minute
# même si l'écriture disque de last_fired échoue.
_last_fired_mem: dict[str, str] = {}


def _store_path(base_dir: Path) -> Path:
    out = Path(base_dir) / "Output"
    out.mkdir(parents=True, exist_ok=True)
    return out / _STORE_NAME


def load(base_dir: Path) -> list[dict]:
    with _LOCK:
        p = _store_path(base_dir)
        if not p.exists():
            return []
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception:
            return []


def save(base_dir: Path, schedules: list[dict]) -> None:
    with _LOCK:
        try:
            _store_path(base_dir).write_text(
                json.dumps(schedules, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass


def add(base_dir: Path, sched: dict) -> dict:
    with _LOCK:
        sched = dict(sched)
        sched.setdefault("id", uuid.uuid4().hex[:10])
        sched.setdefault("enabled", True)
        sched.setdefault("last_fired", None)
        sched.setdefault("last_status", None)
        schedules = load(base_dir)
        schedules.append(sched)
        save(base_dir, schedules)
        return sched


def update(base_dir: Path, sched_id: str, patch: dict) -> bool:
    with _LOCK:
        schedules = load(base_dir)
        found = False
        for s in schedules:
            if s.get("id") == sched_id:
                s.update(patch)
                found = True
        if found:
            save(base_dir, schedules)
        return found


def delete(base_dir: Path, sched_id: str) -> bool:
    with _LOCK:
        schedules = load(base_dir)
        new = [s for s in schedules if s.get("id") != sched_id]
        if len(new) != len(schedules):
            save(base_dir, new)
            return True
        return False


# ── Déclenchement (headless, non-attendu) ──────────────────────────────────
def _trigger_pipeline(base_dir: Path, cfg: dict) -> tuple[bool, str]:
    from config.credentials_loader import CREDENTIALS
    from pipeline_runner import PipelineRunner
    u, p = CREDENTIALS.credentials_for("postgresql_iehe")
    if not (u and p):
        return False, "Identifiants IEHE absents de credentials.ini"
    selected = None
    if cfg.get("mode") == "selection":
        valid = {s["id"] for s in PipelineRunner.scripts_catalog()}
        selected = {s for s in (cfg.get("scripts") or []) if s in valid}
        if not selected:
            return False, "Aucun script valide dans la planification"
    log_q = queue.Queue()
    runner = PipelineRunner(u, p, log_q, selected_ids=selected, unattended=True)
    runner.run()
    # Préserve les logs d'exécution (sinon perdus en mode planifié) : on les
    # imprime sur la sortie standard pour le débogage.
    while not log_q.empty():
        item = log_q.get()
        if item.get("type") == "log":
            print(f"[Scheduler Pipeline] {item.get('ts')} "
                  f"[{item.get('level')}] {item.get('text')}", flush=True)
    return runner.success, ("Pipeline OK" if runner.success else "Pipeline en échec")


def _trigger_sql(base_dir: Path, cfg: dict) -> tuple[bool, str]:
    from modules.sql_runner import env_loader, query_loader, executor
    from config.credentials_loader import CREDENTIALS
    sql_dir = env_loader.default_sql_dir(base_dir)
    envs, valid = env_loader.load_environments(sql_dir / "environments.ini")
    if not envs:
        return False, "Aucun environnement configuré"
    all_q, _ = query_loader.load_queries(sql_dir, valid)
    names = cfg.get("queries") or list(all_q.keys())
    selected = {n: all_q[n] for n in names if n in all_q}
    if not selected:
        return False, "Aucune requête planifiée disponible"
    env = cfg.get("environment") or sorted(envs.keys())[0]
    query_envs = {n: env for n in selected}

    def creds_resolver(e, db):
        return CREDENTIALS.sql_secret_for(e, db)

    y = (datetime.datetime.now() - datetime.timedelta(days=1)).strftime("%d/%m/%Y")
    dates = {"quotidien": y, "hebdo": y}
    out = base_dir / "Output" / "SQL" / y.replace("/", "")
    files = executor.run_queries(selected, envs, query_envs, creds_resolver,
                                 dates, {}, out, 300, lambda ev: None)
    return True, f"{len(files)} fichier(s) généré(s)"


def _run_once(base_dir: Path, sched: dict) -> tuple[bool, str]:
    kind = sched.get("kind")
    try:
        if kind == "pipeline":
            return _trigger_pipeline(base_dir, sched.get("config", {}))
        if kind == "sql":
            return _trigger_sql(base_dir, sched.get("config", {}))
        return False, f"Type inconnu : {kind}"
    except Exception as exc:
        return False, f"Erreur : {exc}"


def _run_schedule(base_dir: Path, sched: dict) -> None:
    ts_start = datetime.datetime.now()
    kind = sched.get("kind")
    # Réessais automatiques en cas d'échec (max_retries, délai entre essais).
    try:
        max_retries = max(0, int(sched.get("max_retries", 0)))
    except (TypeError, ValueError):
        max_retries = 0
    retry_delay = 30  # secondes entre deux tentatives
    attempt = 0
    while True:
        ok, msg = _run_once(base_dir, sched)
        attempt += 1
        if ok or attempt > max_retries:
            if attempt > 1:
                msg = f"{msg} (après {attempt} tentative(s))"
            break
        # échec -> on patiente puis on réessaie (sauf si arrêt demandé)
        if _stop.wait(retry_delay):
            break
    ts_end = datetime.datetime.now()
    update(base_dir, sched["id"], {"last_status": "ok" if ok else "error"})
    run_history.record(base_dir, {
        "kind": f"scheduled_{kind}",
        "ts_start": ts_start.isoformat(timespec="seconds"),
        "ts_end": ts_end.isoformat(timespec="seconds"),
        "duration_s": round((ts_end - ts_start).total_seconds(), 1),
        "success": ok,
        "user": "scheduler",
        "summary": f"Planification « {sched.get('name', '')} » — {msg}",
        "details": {"schedule_id": sched["id"], "kind": kind},
    })


def _is_due(sched: dict, now: datetime.datetime) -> bool:
    if not sched.get("enabled", True):
        return False
    if sched.get("time") != now.strftime("%H:%M"):
        return False
    if sched.get("frequency") == "weekly":
        if now.weekday() not in (sched.get("days") or []):
            return False
    # Anti-rejeu : vérification en mémoire puis sur disque (robuste même si
    # l'écriture de last_fired a échoué).
    minute_str = now.strftime("%Y-%m-%d %H:%M")
    if _last_fired_mem.get(sched.get("id")) == minute_str:
        return False
    return sched.get("last_fired") != minute_str


def _loop():
    while not _stop.is_set():
        try:
            now = datetime.datetime.now()
            for sched in load(_base_dir):
                if _is_due(sched, now):
                    minute_str = now.strftime("%Y-%m-%d %H:%M")
                    _last_fired_mem[sched["id"]] = minute_str  # bloque le rejeu immédiatement
                    update(_base_dir, sched["id"], {"last_fired": minute_str})
                    threading.Thread(target=_run_schedule,
                                     args=(_base_dir, sched), daemon=True).start()
        except Exception:
            pass
        _stop.wait(20)  # vérification toutes les 20 s


def start(base_dir: Path) -> None:
    """Démarre le thread de planification (idempotent)."""
    global _thread, _base_dir
    _base_dir = Path(base_dir)
    if _thread is not None and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, daemon=True)
    _thread.start()


def run_now(base_dir: Path, sched_id: str) -> bool:
    """Déclenche immédiatement une planification (bouton « Lancer maintenant »)."""
    for s in load(base_dir):
        if s.get("id") == sched_id:
            threading.Thread(target=_run_schedule, args=(Path(base_dir), s),
                             daemon=True).start()
            return True
    return False


def stop() -> None:
    """Arrête proprement le thread de planification (tests / rechargement)."""
    global _thread
    _stop.set()
    if _thread is not None:
        _thread.join(timeout=5)
        _thread = None
