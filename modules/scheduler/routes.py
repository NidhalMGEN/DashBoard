"""Module 04 — Planification : gestion des tâches programmées."""

from __future__ import annotations

from pathlib import Path

from flask import Blueprint, render_template, jsonify, request, current_app

from core import scheduler
from modules.pipeline.runner import PipelineRunner

bp = Blueprint(
    "scheduler", __name__,
    url_prefix="/scheduler",
    template_folder="templates",
)


def _base_dir() -> Path:
    return current_app.config["BASE_DIR"]


@bp.route("/")
def index():
    return render_template("scheduler/index.html", active_module="scheduler")


@bp.route("/api/schedules")
def api_list():
    """Liste des planifications + catalogue scripts pipeline (pour le form)."""
    return jsonify({
        "schedules": scheduler.load(_base_dir()),
        "scripts": PipelineRunner.scripts_catalog(),
    })


@bp.route("/api/schedules", methods=["POST"])
def api_add():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    kind = data.get("kind")
    time_ = data.get("time") or ""
    if not name or kind not in ("pipeline", "sql"):
        return jsonify({"error": "Nom et type requis"}), 400
    if not _valid_time(time_):
        return jsonify({"error": "Heure invalide (HH:MM)"}), 400
    freq = data.get("frequency", "daily")
    days = data.get("days") or []
    if freq == "weekly":
        if not days or not all(isinstance(d, int) and 0 <= d <= 6 for d in days):
            return jsonify({"error": "Sélectionnez au moins un jour valide (0-6)"}), 400
    try:
        max_retries = max(0, min(int(data.get("max_retries", 0)), 5))
    except (TypeError, ValueError):
        max_retries = 0
    sched = scheduler.add(_base_dir(), {
        "name": name, "kind": kind, "frequency": freq,
        "days": days, "time": time_, "max_retries": max_retries,
        "config": data.get("config") or {},
    })
    return jsonify({"ok": True, "schedule": sched})


@bp.route("/api/schedules/<sched_id>", methods=["DELETE"])
def api_delete(sched_id):
    return jsonify({"ok": scheduler.delete(_base_dir(), sched_id)})


@bp.route("/api/schedules/<sched_id>/toggle", methods=["POST"])
def api_toggle(sched_id):
    data = request.get_json(silent=True) or {}
    return jsonify({"ok": scheduler.update(_base_dir(), sched_id,
                                           {"enabled": bool(data.get("enabled"))})})


@bp.route("/api/schedules/<sched_id>/run", methods=["POST"])
def api_run_now(sched_id):
    return jsonify({"ok": scheduler.run_now(_base_dir(), sched_id)})


def _valid_time(t: str) -> bool:
    parts = t.split(":")
    if len(parts) != 2:
        return False
    try:
        h, m = int(parts[0]), int(parts[1])
        return 0 <= h < 24 and 0 <= m < 60
    except ValueError:
        return False
