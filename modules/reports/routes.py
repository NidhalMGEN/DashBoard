"""Module 03 — Rapports CODIR.

Génère à la demande le dashboard HTML/PDF (réutilise `report_generator.py`),
archive les rapports produits sous `Output/` et permet de les consulter /
télécharger. Aucun envoi par mail (hors périmètre).
"""

from __future__ import annotations

import sys
import datetime
from pathlib import Path

from flask import (Blueprint, render_template, jsonify, current_app,
                   send_file, abort, request)

from core import run_history

bp = Blueprint(
    "reports", __name__,
    url_prefix="/reports",
    template_folder="templates",
)


def _base_dir() -> Path:
    return current_app.config["BASE_DIR"]


def _output_dir() -> Path:
    return _base_dir() / "Output"


def _list_reports() -> list[dict]:
    """Rapports archivés (rapport_codir_*.html), le plus récent en premier."""
    out = _output_dir()
    items = []
    if out.exists():
        for p in sorted(out.glob("rapport_codir_*.html"),
                        key=lambda x: x.stat().st_mtime, reverse=True):
            pdf = p.with_suffix(".pdf")
            items.append({
                "name": p.name,
                "flux": p.stem.replace("rapport_codir_", ""),
                "mtime": datetime.datetime.fromtimestamp(p.stat().st_mtime)
                          .strftime("%d/%m/%Y %H:%M"),
                "size_kb": round(p.stat().st_size / 1024),
                "has_pdf": pdf.exists(),
            })
    return items


@bp.route("/")
def index():
    return render_template("reports/index.html", active_module="reports")


def _latest_json():
    """Dernier *_Modele_clean.json (le plus récent), ou None."""
    out = _output_dir()
    files = sorted(out.glob("*_Modele_clean.json"),
                   key=lambda p: p.stat().st_mtime, reverse=True) if out.exists() else []
    return files[0] if files else None


def _flux_of(json_path: Path) -> str:
    stem = json_path.stem
    return stem.split("_")[0] if "_" in stem else stem


@bp.route("/api/list")
def api_list():
    """Liste des rapports archivés + flux source le plus récent.

    `latest_flux`        : identifiant du dernier flux disponible
    `latest_report_ready`: True si le rapport de CE flux est déjà archivé
    """
    latest = _latest_json()
    latest_flux = _flux_of(latest) if latest else None
    reports = _list_reports()
    ready = any(r["flux"] == latest_flux for r in reports) if latest_flux else False
    return jsonify({
        "reports": reports,
        "source_available": latest is not None,
        "latest_flux": latest_flux,
        "latest_report_ready": ready,
    })


@bp.route("/api/generate", methods=["POST"])
def api_generate():
    """Génère le rapport depuis le dernier *_Modele_clean.json présent."""
    out = _output_dir()
    json_files = sorted(out.glob("*_Modele_clean.json"),
                        key=lambda p: p.stat().st_mtime, reverse=True) if out.exists() else []
    if not json_files:
        return jsonify({"error": "Aucun flux *_Modele_clean.json dans Output/."}), 404

    # Import paresseux : report_generator est à la racine du projet.
    sys.path.insert(0, str(_base_dir()))
    from report_generator import generate

    ts_start = datetime.datetime.now()
    try:
        html_path = generate(json_files[0], out, _base_dir() / "assets")
    except Exception as exc:
        return jsonify({"error": f"Échec de génération : {exc}"}), 500

    ts_end = datetime.datetime.now()
    run_history.record(_base_dir(), {
        "kind": "report",
        "ts_start": ts_start.isoformat(timespec="seconds"),
        "ts_end": ts_end.isoformat(timespec="seconds"),
        "duration_s": round((ts_end - ts_start).total_seconds(), 1),
        "success": True,
        "user": request.remote_addr or "",
        "summary": f"Rapport CODIR généré : {html_path.name}",
        "details": {"file": html_path.name, "source": json_files[0].name},
    })
    return jsonify({"ok": True, "name": html_path.name})


@bp.route("/view/<path:name>")
def view(name):
    """Affiche un rapport HTML archivé (inline)."""
    p = _output_dir() / name
    if not p.exists() or p.suffix != ".html" or not p.name.startswith("rapport_codir_"):
        abort(404)
    return send_file(str(p), mimetype="text/html")


@bp.route("/download/<path:name>")
def download(name):
    """Télécharge un rapport (html ou pdf) archivé."""
    p = _output_dir() / name
    if (not p.exists() or p.suffix not in (".html", ".pdf")
            or not p.name.startswith("rapport_codir_")):
        abort(404)
    return send_file(str(p), as_attachment=True, download_name=p.name)
