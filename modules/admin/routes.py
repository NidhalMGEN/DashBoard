"""Module 05 — Admin : seuils d'alerte, configuration, état système."""

from __future__ import annotations

import sys

from flask import Blueprint, render_template, jsonify, request

from modules._registry import active_modules
from modules.dashboard import queries as dq

bp = Blueprint(
    "admin", __name__,
    url_prefix="/admin",
    template_folder="templates",
)

# Libellés lisibles des KPI à seuils (édition Admin).
_KPI_LABELS = {
    "taux_matching_ciam":  "Matching CIAM",
    "taux_presence_iehe":  "Présence IEHE",
    "score_qualite":       "Qualité données",
    "taux_eligibilite_tp": "Éligibilité TP",
    "taux_ged":            "Taux GED",
}


@bp.route("/")
def index():
    return render_template("admin/index.html", active_module="admin")


@bp.route("/api/config")
def api_config():
    """Configuration courante : seuils (effectifs + défauts), modules, creds."""
    try:
        from config.credentials_loader import CREDENTIALS
        cred = CREDENTIALS.status()
    except Exception:
        cred = {}
    thresholds = dq.get_thresholds()
    rows = [{
        "key": k, "label": _KPI_LABELS.get(k, k),
        "green": thresholds[k]["green"], "orange": thresholds[k]["orange"],
        "default_green": dq.THRESHOLDS[k]["green"],
        "default_orange": dq.THRESHOLDS[k]["orange"],
        "higher_is_better": thresholds[k]["higher_is_better"],
    } for k in dq.THRESHOLDS]
    return jsonify({
        "thresholds": rows,
        "modules": [{"key": m.key, "code": m.code, "label": m.label,
                     "enabled": m.enabled} for m in active_modules()],
        "credentials": cred,
        "python": sys.version.split()[0],
    })


@bp.route("/api/thresholds", methods=["POST"])
def api_thresholds():
    """Enregistre les seuils édités (surcharges persistées)."""
    data = request.get_json(silent=True) or {}
    incoming = data.get("thresholds") or {}
    clean = {}
    for key, vals in incoming.items():
        if key not in dq.THRESHOLDS or not isinstance(vals, dict):
            continue
        entry = {}
        for f in ("green", "orange"):
            try:
                entry[f] = float(vals[f])
            except (KeyError, TypeError, ValueError):
                pass
        if entry:
            clean[key] = entry
    try:
        from config.admin_store import save_thresholds
        ok = save_thresholds(clean)
    except Exception:
        ok = False
    return jsonify({"ok": ok})
