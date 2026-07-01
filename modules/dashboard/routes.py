"""Routes du Module 00 — Dashboard supervision MGEN."""

from __future__ import annotations

from flask import Blueprint, render_template, jsonify, request, current_app

from . import queries
from core import run_history

bp = Blueprint(
    "dashboard", __name__,
    url_prefix="/dashboard",
    template_folder="templates",
)


@bp.route("/")
def index():
    return render_template("dashboard/index.html", active_module="dashboard")


@bp.route("/api/kpis")
def api_kpis():
    """Modèle complet du dashboard (KPI + tendances + flags + fraîcheur).

    Toujours HTTP 200 : l'état dégradé (base inaccessible) est porté par
    le champ `available` du JSON, pas par un code d'erreur.

    Paramètre `?days=` (7/14/30/90) : fenêtre temporelle des graphiques.
    """
    try:
        days = int(request.args.get("days", 30))
    except (TypeError, ValueError):
        days = 30
    days = max(1, min(days, 365))
    return jsonify(queries.build_dashboard(limit=days))


@bp.route("/api/connections")
def api_connections():
    """Ping de connexion sur toutes les bases configurées (bloc Test de
    connexion). Toujours HTTP 200 — l'état par base est dans le JSON."""
    return jsonify(queries.test_connections())


@bp.route("/api/runs")
def api_runs():
    """Historique des derniers runs (pipeline + SQL)."""
    base_dir = current_app.config["BASE_DIR"]
    return jsonify({"runs": run_history.recent(base_dir, n=20)})


@bp.route("/api/run-logs")
def api_run_logs():
    """Logs complets d'un run passé (#2)."""
    base_dir = current_app.config["BASE_DIR"]
    rec = run_history.get(base_dir, request.args.get("id", ""))
    if not rec:
        return jsonify({"error": "Run introuvable"}), 404
    return jsonify({"summary": rec.get("summary", ""), "logs": rec.get("logs", [])})


@bp.route("/api/flux-list")
def api_flux_list():
    """Liste des flux pour la comparaison (#1)."""
    return jsonify(queries.flux_list())


@bp.route("/api/compare")
def api_compare():
    """Comparaison de deux flux (#1)."""
    a = request.args.get("a", "")
    b = request.args.get("b", "")
    if not a or not b:
        return jsonify({"available": False, "reason": "Deux flux requis."})
    return jsonify(queries.compare_flux(a, b))


@bp.route("/api/alerts")
def api_alerts():
    """Centre d'alertes in-app (badge topbar + carte dashboard)."""
    base_dir = current_app.config["BASE_DIR"]
    return jsonify(queries.build_alerts(base_dir))


@bp.route("/api/export-all")
def api_export_all():
    """Export Excel multi-onglets de tous les KPI du dernier flux (#5)."""
    from flask import send_file
    buf, name = queries.export_all_workbook()
    if buf is None:
        # 500 si erreur interne de génération, 404 si simplement aucune donnée.
        status_code = 500 if name.startswith("Export impossible") else 404
        return jsonify({"error": name}), status_code
    return send_file(buf, as_attachment=True, download_name=name,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@bp.route("/api/details")
def api_details():
    """Graphiques d'enrichissement du dernier flux (#5)."""
    return jsonify(queries.build_details())


@bp.route("/api/kpi-detail")
def api_kpi_detail():
    """Ventilation détaillée d'un KPI (#6) — table {title, columns, rows}."""
    key = request.args.get("key", "")
    return jsonify(queries.kpi_detail(key))


@bp.route("/api/kpi-detail/export")
def api_kpi_detail_export():
    """Export Excel de la ventilation d'un KPI (#6)."""
    import io
    key = request.args.get("key", "")
    detail = queries.kpi_detail(key)
    if not detail.get("available"):
        return jsonify({"error": detail.get("reason", "Aucun détail")}), 404
    try:
        import pandas as pd
        from flask import send_file
        df = pd.DataFrame(detail["rows"], columns=detail["columns"])
        buf = io.BytesIO()
        # Nom de feuille Excel sûr (≤31 car., pas de caractères interdits).
        sheet_name = "".join(c for c in key[:31] if c.isalnum() or c in " _-") or "Detail"
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            df.to_excel(writer, sheet_name=sheet_name, index=False)
        buf.seek(0)
        return send_file(buf, as_attachment=True,
                         download_name=f"detail_{key}.xlsx",
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    except Exception as exc:
        return jsonify({"error": f"Export impossible : {exc}"}), 500

@bp.route("/api/available-dates")
def api_available_dates():
    base_dir = current_app.config["BASE_DIR"]
    return jsonify(queries.fetch_available_dates(base_dir=base_dir))


@bp.route("/api/kpis-by-date")
def api_kpis_by_date():
    """?date=YYYY-MM-DD"""
    date_str = request.args.get("date", "")
    if not date_str:
        return jsonify({"available": False, "error": "Paramètre ?date=YYYY-MM-DD manquant"}), 400
    base_dir = current_app.config["BASE_DIR"]
    return jsonify(queries.build_dashboard_for_date(date_str, base_dir=base_dir))