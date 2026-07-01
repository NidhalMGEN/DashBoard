"""Routes du Module 07 — Analyse Tickets S@M."""
from __future__ import annotations

import io
import threading
from pathlib import Path

import pandas as pd
from flask import (Blueprint, current_app, jsonify, render_template,
                   request, send_file)

from .anonymizer import TicketAnonymizer
from .kpi_engine import build_ai_prompt, compute_kpis

bp = Blueprint(
    "ticket_sam", __name__,
    url_prefix="/ticket-sam",
    template_folder="templates",
)

# État global mono-utilisateur (même pattern que le module pipeline)
_state: dict = {}
_lock = threading.Lock()


@bp.route("/")
def index():
    return render_template("ticket_sam/index.html", active_module="ticket_sam")


@bp.route("/api/upload", methods=["POST"])
def api_upload():
    """Reçoit et charge le fichier Excel en mémoire."""
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "Aucun fichier reçu."}), 400

    f = request.files["file"]
    if not f.filename.lower().endswith(".xlsx"):
        return jsonify({"ok": False, "error": "Seuls les fichiers .xlsx sont acceptés."}), 400

    try:
        raw = f.read()
        sheets: dict[str, pd.DataFrame] = pd.read_excel(
            io.BytesIO(raw), sheet_name=None, engine="openpyxl"
        )
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Lecture impossible : {exc}"}), 400

    with _lock:
        _state.clear()
        _state["sheets"]    = sheets
        _state["filename"]  = f.filename
        _state["anon_done"] = False

    sheet_info = {name: {"rows": len(df), "cols": len(df.columns)}
                  for name, df in sheets.items()}
    return jsonify({"ok": True, "sheets": sheet_info, "filename": f.filename})


@bp.route("/api/process", methods=["POST"])
def api_process():
    """Anonymise le fichier et calcule les KPIs."""
    with _lock:
        if "sheets" not in _state:
            return jsonify({"ok": False, "error": "Aucun fichier chargé."}), 400
        sheets = _state["sheets"]

    try:
        anon = TicketAnonymizer()
        anon_sheets = anon.anonymize_dataframes(sheets)
        kpis = compute_kpis(anon_sheets)

        with _lock:
            _state["anon_sheets"] = anon_sheets
            _state["kpis"]        = kpis
            _state["anon_stats"]  = anon.stats
            _state["anon_done"]   = True

        return jsonify({"ok": True, "kpis": kpis, "anon_stats": anon.stats})

    except Exception as exc:
        current_app.logger.error("Erreur traitement ticket_sam", exc_info=True)
        return jsonify({
            "ok": False,
            "error": "Une erreur interne est survenue lors du traitement du fichier.",
        }), 500


@bp.route("/api/download-anon")
def api_download_anon():
    """Télécharge le fichier anonymisé."""
    with _lock:
        if not _state.get("anon_done"):
            return jsonify({"error": "Aucun fichier anonymisé disponible."}), 400
        anon_sheets = _state["anon_sheets"]
        filename    = _state.get("filename", "tickets.xlsx")

    try:
        buf = TicketAnonymizer().to_excel(anon_sheets)
        stem = Path(filename).stem
        return send_file(
            buf,
            as_attachment=True,
            download_name=f"{stem}_anonymise.xlsx",
            mimetype=(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ),
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@bp.route("/api/prompt")
def api_prompt():
    """Retourne le prompt IA structuré."""
    with _lock:
        if not _state.get("anon_done"):
            return jsonify({"ok": False, "error": "Aucune analyse disponible."}), 400
        kpis     = _state["kpis"]
        filename = _state.get("filename", "")

    prompt = build_ai_prompt(kpis, filename)
    return jsonify({"ok": True, "prompt": prompt})


@bp.route("/api/status")
def api_status():
    """État courant de l'analyse."""
    with _lock:
        return jsonify({
            "loaded":     "sheets" in _state,
            "anon_done":  _state.get("anon_done", False),
            "filename":   _state.get("filename", ""),
            "anon_stats": _state.get("anon_stats", {}),
        })
