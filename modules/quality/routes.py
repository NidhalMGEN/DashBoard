"""Module 06 — Qualité des données : anomalies du dernier flux + export."""

from __future__ import annotations

from flask import Blueprint, render_template, jsonify

from modules.dashboard import queries as dq

bp = Blueprint(
    "quality", __name__,
    url_prefix="/quality",
    template_folder="templates",
)


@bp.route("/")
def index():
    return render_template("quality/index.html", active_module="quality")


@bp.route("/api/quality")
def api_quality():
    return jsonify(dq.build_quality())


@bp.route("/api/export")
def api_export():
    """Export Excel des anomalies qualité du dernier flux."""
    import io
    data = dq.build_quality()
    if not data.get("available"):
        return jsonify({"error": "Aucun flux exploitable"}), 404
    try:
        import pandas as pd
        from flask import send_file
        rows = [[a["label"], a["nombre"], a["pct"], a["level"]]
                for a in data["anomalies"]]
        if not rows:
            return jsonify({"error": "Aucune anomalie à exporter"}), 404
        df = pd.DataFrame(rows, columns=["Anomalie", "Nombre", "%", "Sévérité"])
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            df.to_excel(writer, sheet_name="Qualite", index=False)
        buf.seek(0)
        return send_file(buf, as_attachment=True, download_name="qualite_donnees.xlsx",
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    except Exception as exc:
        return jsonify({"error": f"Export impossible : {exc}"}), 500
