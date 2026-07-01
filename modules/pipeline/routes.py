"""Routes du Module 01 — Pipeline ETL.

Conserve la mécanique SSE + pause/resume de l'application monolithique, en
ajoutant la sélection de scripts (exécution partielle) et le mode pipeline
complet. L'état du runner est porté par le module (un run à la fois).
"""

from __future__ import annotations

import json
import time
import queue
import threading
import datetime

from flask import Blueprint, render_template, request, Response, jsonify, current_app

from .runner import PipelineRunner
from core import run_history

bp = Blueprint(
    "pipeline", __name__,
    url_prefix="/pipeline",
    template_folder="templates",
)

# État partagé du module (un seul pipeline actif à la fois).
_log_queue: queue.Queue = queue.Queue()
_runner: PipelineRunner | None = None
_thread: threading.Thread | None = None


@bp.route("/")
def index():
    return render_template("pipeline/index.html", active_module="pipeline")


@bp.route("/api/scripts")
def api_scripts():
    """Catalogue des scripts (source de vérité unique pour le sélecteur)."""
    return jsonify({"scripts": PipelineRunner.scripts_catalog()})


def _iehe_creds_from_ini() -> tuple[str | None, str | None]:
    """Identifiants IEHE issus de config/credentials.ini, ou (None, None)."""
    try:
        from config.credentials_loader import CREDENTIALS
        return CREDENTIALS.credentials_for("postgresql_iehe")
    except Exception:
        return None, None


@bp.route("/api/credentials-status")
def api_credentials_status():
    """Indique si les identifiants IEHE sont disponibles dans credentials.ini.
    Ne renvoie jamais le mot de passe — juste un booléen et l'utilisateur."""
    u, p = _iehe_creds_from_ini()
    return jsonify({"iehe_available": bool(u and p), "user": u or ""})


@bp.route("/start", methods=["POST"])
def start():
    """Lance le pipeline.

    Body JSON :
        pg_user, pg_password : identifiants IEHE
        mode    : "full" (01→07) | "selection"
        scripts : liste d'ids d'étapes (mode selection uniquement)
    """
    global _runner, _thread, _log_queue
    data = request.get_json(silent=True) or {}
    pg_user = data.get("pg_user", "")
    pg_password = data.get("pg_password", "")
    mode = data.get("mode", "full")
    selected = data.get("scripts") or []

    # Priorité stricte aux identifiants de credentials.ini : dès que la section
    # [postgresql_iehe] est complète, elle l'emporte (jamais de mélange avec une
    # saisie IHM partielle). Le formulaire n'est qu'un fallback si le .ini est vide.
    cu, cp = _iehe_creds_from_ini()
    if cu and cp:
        pg_user = cu
        pg_password = cp

    if not pg_user or not pg_password:
        return jsonify({"error": "Identifiants IEHE manquants (formulaire ou credentials.ini)"}), 400

    if _runner and _runner.running:
        return jsonify({"error": "Pipeline déjà en cours"}), 409

    if mode == "selection":
        valid_ids = {s["id"] for s in PipelineRunner.scripts_catalog()}
        selected_ids = {s for s in selected if s in valid_ids}
        if not selected_ids:
            return jsonify({"error": "Aucun script sélectionné"}), 400
    else:
        selected_ids = None  # pipeline complet

    # Vide la file de logs résiduels.
    while not _log_queue.empty():
        try:
            _log_queue.get_nowait()
        except queue.Empty:
            break

    _runner = PipelineRunner(pg_user, pg_password, _log_queue, selected_ids=selected_ids)
    base_dir = current_app.config["BASE_DIR"]
    user = pg_user
    ts_start = datetime.datetime.now()

    def _run_and_record(runner):
        runner.run()
        # Enregistre le run dans l'historique (observabilité).
        steps = [{"id": s.id, "label": s.label, "status": s.status.value}
                 for s in runner.STEPS]
        ok = sum(1 for s in runner.STEPS if s.status.value == "ok")
        ts_end = datetime.datetime.now()
        run_history.record(base_dir, {
            "kind": "pipeline",
            "ts_start": ts_start.isoformat(timespec="seconds"),
            "ts_end": ts_end.isoformat(timespec="seconds"),
            "duration_s": round((ts_end - ts_start).total_seconds(), 1),
            "success": runner.success,
            "user": user,
            "summary": f"{'Pipeline complet' if mode != 'selection' else 'Sélection'} — "
                       f"{ok}/{len(steps)} étapes OK",
            "details": {"mode": mode, "steps": steps},
            "logs": runner.log_lines,
        })

    _thread = threading.Thread(target=_run_and_record, args=(_runner,), daemon=True)
    _thread.start()
    return jsonify({"ok": True, "mode": mode})


@bp.route("/error-decision", methods=["POST"])
def error_decision():
    """Décision sur une étape en échec : 'continue' (ignorer) ou 'abort'."""
    data = request.get_json(silent=True) or {}
    decision = data.get("decision", "abort")
    if _runner:
        _runner.error_decision(decision)
        return jsonify({"ok": True})
    return jsonify({"error": "Aucun pipeline actif"}), 400


@bp.route("/stream")
def stream():
    def gen():
        keepalive = 15
        last = time.time()
        while True:
            try:
                msg = _log_queue.get(timeout=1)
                yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
                if msg.get("type") == "done":
                    break
            except queue.Empty:
                if time.time() - last >= keepalive:
                    yield ": keep-alive\n\n"
                    last = time.time()

    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@bp.route("/resume", methods=["POST"])
def resume():
    data = request.get_json(silent=True) or {}
    pause_id = data.get("pause_id", "")
    if _runner:
        _runner.resume(pause_id)
        return jsonify({"ok": True})
    return jsonify({"error": "Aucun pipeline actif"}), 400


@bp.route("/status")
def status():
    if _runner is None:
        return jsonify({"state": "idle", "progress": 0, "steps": []})
    steps = [{"id": s.id, "label": s.label, "status": s.status.value}
             for s in _runner.STEPS]
    return jsonify({
        "state": "running" if _runner.running else ("done" if _runner.success else "error"),
        "progress": _runner.progress,
        "steps": steps,
    })
