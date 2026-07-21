"""Routes du module Pipeline GED — retry des cartes TP GED KO.

Même mécanique SSE + pause/résume que le module Pipeline ETL (un run à la
fois), appliquée au runner GED. Pas d'identifiants demandés : le script 08
utilise la BDD de suivi avec sa propre configuration.
"""

from __future__ import annotations

import json
import time
import queue
import threading
import datetime

from flask import Blueprint, render_template, request, Response, jsonify, current_app

from .runner import GedPipelineRunner
from core import run_history

bp = Blueprint(
    "pipeline_ged", __name__,
    url_prefix="/pipeline-ged",
    template_folder="templates",
)

# État partagé du module (un seul run actif à la fois).
_log_queue: queue.Queue = queue.Queue()
_runner: GedPipelineRunner | None = None
_thread: threading.Thread | None = None


@bp.route("/")
def index():
    return render_template("pipeline_ged/index.html", active_module="pipeline_ged")


@bp.route("/api/scripts")
def api_scripts():
    return jsonify({"scripts": GedPipelineRunner.scripts_catalog()})


@bp.route("/start", methods=["POST"])
def start():
    """Lance le pipeline GED.

    Body JSON :
        scripts : liste d'ids d'étapes (vide = toutes les étapes)
    """
    global _runner, _thread, _log_queue
    data = request.get_json(silent=True) or {}
    selected = data.get("scripts") or []

    if _runner and _runner.running:
        return jsonify({"error": "Pipeline GED déjà en cours"}), 409

    valid_ids = {s["id"] for s in GedPipelineRunner.scripts_catalog()}
    selected_ids = {s for s in selected if s in valid_ids}
    if not selected_ids:
        return jsonify({"error": "Aucun script sélectionné"}), 400

    # Vide la file de logs résiduels.
    while not _log_queue.empty():
        try:
            _log_queue.get_nowait()
        except queue.Empty:
            break

    _runner = GedPipelineRunner("", "", _log_queue, selected_ids=selected_ids)
    base_dir = current_app.config["BASE_DIR"]
    ts_start = datetime.datetime.now()

    def _run_and_record(runner):
        runner.run()
        steps = [{"id": s.id, "label": s.label, "status": s.status.value}
                 for s in runner.STEPS]
        ok = sum(1 for s in runner.STEPS if s.status.value == "ok")
        ts_end = datetime.datetime.now()
        run_history.record(base_dir, {
            "kind": "pipeline_ged",
            "ts_start": ts_start.isoformat(timespec="seconds"),
            "ts_end": ts_end.isoformat(timespec="seconds"),
            "duration_s": round((ts_end - ts_start).total_seconds(), 1),
            "success": runner.success,
            "user": "",
            "summary": f"Pipeline GED — {ok}/{len(steps)} étapes OK",
            "details": {"steps": steps},
            "logs": runner.log_lines,
        })

    _thread = threading.Thread(target=_run_and_record, args=(_runner,), daemon=True)
    _thread.start()
    return jsonify({"ok": True})


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
        return jsonify({"state": "idle", "progress": 0, "steps": [], "pause": None})
    steps = [{"id": s.id, "label": s.label, "status": s.status.value}
             for s in _runner.STEPS]
    return jsonify({
        "state": "running" if _runner.running else ("done" if _runner.success else "error"),
        "progress": _runner.progress,
        "steps": steps,
        "pause": _runner.pause_info,
    })
