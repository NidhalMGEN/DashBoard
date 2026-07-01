"""Accès données PostgreSQL pour le Dashboard supervision.

Toutes les requêtes PG sont isolées ici (jamais inline dans les routes).
Source : table `rptpsc.output_kpi_json` (base `supervisionpsc_db`).

Principe de robustesse : aucune fonction ne lève vers la route. En cas de
base inaccessible, on retourne un état dégradé explicite
(`{"available": False, "error": "..."}`) que l'IHM affiche proprement.
"""

from __future__ import annotations

import json
import datetime
from typing import Any

# Connexion historisation. Les identifiants proviennent en priorité de
# config/credentials.ini (section [postgresql_supervision]) ; à défaut on
# retombe sur les valeurs historiques (compat ascendante).
PG_HOST = "bdd-T0XX0052.alias"
PG_PORT = "5577"
PG_DB   = "supervisionpsc_db"
PG_USER = "rptpsc"
PG_PWD  = "rptpsc_xx"

_FALLBACK_DSN = f"postgresql+psycopg://{PG_USER}:{PG_PWD}@{PG_HOST}:{PG_PORT}/{PG_DB}"


def _dsn() -> str:
    try:
        from config.credentials_loader import CREDENTIALS
        dsn = CREDENTIALS.supervision_dsn()
        if dsn:
            return dsn
    except Exception:
        pass
    return _FALLBACK_DSN

# Seuils d'alerte par KPI (valeur -> couleur). Centralisés ici pour qu'un
# futur module Admin puisse les éditer sans toucher au reste du code.
THRESHOLDS = {
    "taux_matching_ciam":  {"green": 99.0, "orange": 97.0, "higher_is_better": True},
    "taux_presence_iehe":  {"green": 99.9, "orange": 99.0, "higher_is_better": True},
    "score_qualite":       {"green": 99.0, "orange": 97.0, "higher_is_better": True},
    "taux_eligibilite_tp": {"green": 50.0, "orange": 20.0, "higher_is_better": True},
    "taux_ged":            {"green": 50.0, "orange": 10.0, "higher_is_better": True},
}

# Chemins d'extraction dans le payload JSON (alignés sur report_generator).
_KPI_PATHS = {
    "volume_flux":         ["2_Volumetrie_Brute", "Resultats", "Total_Lignes"],
    "personnes_uniques":   ["3_Population_Unique", "Global_Personnes"],
    "cible_ciam":          ["3_Population_Unique", "Cible_CIAM_Assures"],
    "taux_matching_ciam":  ["5_CIAM", "Matching_Global", "Global", "Taux_Couverture"],
    "non_rapproches":      ["5_CIAM", "Matching_Global", "Global", "Non_Rapproches"],
    "score_qualite":       ["5_CIAM", "Score_Qualite_Donnees", "DATA_QUALITY_OK", "Pct"],
    "taux_presence_iehe":  ["6_IEHE", "Presence_Globale", "Presents_IEHE", "Taux"],
    "manquants_iehe":      ["6_IEHE", "Presence_Globale", "Manquants_IEHE", "Nombre"],
    "taux_eligibilite_tp": ["7_Carte_TP", "Eligibilite_Globale", "Population_Eligible", "Taux"],
    "eligibles_tp":        ["7_Carte_TP", "Eligibilite_Globale", "Population_Eligible", "Nombre"],
    "taux_ged":            ["7_Carte_TP", "Controle_GED_Quotidien", "Trouves_GED", "Taux"],
    "ged_recus":           ["7_Carte_TP", "Controle_GED_Quotidien", "Total_Cartes", "Nombre"],
    "ged_trouves":         ["7_Carte_TP", "Controle_GED_Quotidien", "Trouves_GED", "Nombre"],
    "ged_ko":              ["7_Carte_TP", "Controle_GED_Quotidien", "Non_Trouves_GED", "Nombre"],
}

# KPIs disposant d'un drill-down (ventilation détaillée + export Excel).
# Défini ici, en tête, car référencé par build_dashboard (lisibilité / tests).
DRILLABLE_KPIS = {"non_rapproches", "taux_matching_ciam", "score_qualite",
                  "manquants_iehe", "taux_eligibilite_tp", "eligibles_tp",
                  "taux_ged", "ged_recus"}


# Cache des moteurs par DSN : préserve le pool de connexions SQLAlchemy et
# gère le rechargement à chaud (un DSN différent crée un nouveau moteur).
_ENGINES: dict = {}


def _engine():
    from sqlalchemy import create_engine
    dsn = _dsn()
    if dsn not in _ENGINES:
        _ENGINES[dsn] = create_engine(dsn, pool_pre_ping=True)
    return _ENGINES[dsn]


def _parse_payload(p: Any) -> dict:
    if isinstance(p, dict):
        return p
    if isinstance(p, str):
        try:
            return json.loads(p)
        except Exception:
            return {}
    return {}


def _nav(payload: dict, path: list):
    cur = payload
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur[k]
    return cur if isinstance(cur, (int, float)) else None


def _navx(payload: dict, *path, default=None):
    """Comme _nav mais retourne n'importe quel type (dict, list, str...)."""
    cur = payload
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def _extract(payload: dict) -> dict:
    return {key: _nav(payload, path) for key, path in _KPI_PATHS.items()}


def fetch_runs(limit: int = 30) -> dict:
    """Récupère les N derniers flux (le plus récent en premier).

    Retourne un état toujours exploitable par l'IHM :
        {"available": bool, "error": str|None, "runs": [...]}
    Chaque run : {flux_id, date_import (ISO), kpi: {...}}.
    """
    try:
        from sqlalchemy import text
        with _engine().connect() as conn:
            rows = conn.execute(text(
                "SELECT flux_id, date_import, payload "
                "FROM rptpsc.output_kpi_json "
                "ORDER BY date_import DESC "
                "LIMIT :lim"
            ), {"lim": limit}).fetchall()
    except Exception as exc:
        return {"available": False, "error": str(exc), "runs": []}

    runs = []
    for r in rows:
        d = r[1]
        runs.append({
            "flux_id": r[0],
            "date_import": d.isoformat() if hasattr(d, "isoformat") else str(d),
            "kpi": _extract(_parse_payload(r[2])),
        })
    return {"available": True, "error": None, "runs": runs}


# Bases testées par le bloc « Test de connexion » du dashboard.
# (label affiché, section credentials.ini, type de driver)
_CONN_TARGETS = [
    ("Supervision PSC (PostgreSQL)", "postgresql_supervision", "pg"),
    ("IEHE (PostgreSQL)",            "postgresql_iehe",        "pg"),
    ("Oracle MDG — QUALIF",          "oracle_mdg_qualif",      "oracle"),
    ("Oracle MDG — PROD",            "oracle_mdg_prod",        "oracle"),
    ("Oracle DWH — QUALIF",          "oracle_dwh_qualif",      "oracle"),
    ("Oracle DWH — PROD",            "oracle_dwh_prod",        "oracle"),
]


def _ping_pg(sec: dict, user: str, pwd: str) -> tuple[bool, str]:
    try:
        import psycopg
    except ImportError:
        return False, "psycopg non installé"
    try:
        conn = psycopg.connect(
            host=sec.get("host"), port=int(sec.get("port", 5432)),
            dbname=sec.get("dbname"), user=user, password=pwd,
            connect_timeout=4,
        )
        conn.close()
        return True, "OK"
    except Exception as exc:
        return False, str(exc).splitlines()[0][:160]


def _ping_oracle(sec: dict, user: str, pwd: str) -> tuple[bool, str]:
    try:
        import oracledb
    except ImportError:
        return False, "oracledb non installé"
    try:
        dsn = oracledb.makedsn(sec.get("host"), sec.get("port", 1521), sid=sec.get("sid"))
        conn = oracledb.connect(user=user, password=pwd, dsn=dsn)
        conn.close()
        return True, "OK"
    except Exception as exc:
        return False, str(exc).splitlines()[0][:160]


def test_connections() -> dict:
    """Teste un ping de connexion sur chaque base configurée dans
    credentials.ini. Retourne {"results": [{label, section, ok, msg, configured}]}.
    Aucune exception remontée : une base non configurée ou injoignable est
    simplement marquée KO avec un message explicite."""
    try:
        from config.credentials_loader import CREDENTIALS
    except Exception:
        CREDENTIALS = None

    def _test_one(target):
        label, section, kind = target
        sec = CREDENTIALS.section(section) if CREDENTIALS else {}
        user, pwd = (CREDENTIALS.credentials_for(section) if CREDENTIALS else (None, None))
        # Section supervision : fallback historique rptpsc si non renseigné.
        if section == "postgresql_supervision" and (not user or not pwd):
            user, pwd = PG_USER, PG_PWD
            sec = sec or {"host": PG_HOST, "port": PG_PORT, "dbname": PG_DB}
        if not (user and pwd and sec.get("host")):
            return {"label": label, "section": section, "ok": False,
                    "configured": False, "msg": "non configuré dans credentials.ini"}
        ok, msg = (_ping_pg if kind == "pg" else _ping_oracle)(sec, user, pwd)
        return {"label": label, "section": section, "ok": ok,
                "configured": True, "msg": msg}

    # Pings en parallèle : le bloc reste réactif même si plusieurs bases sont KO.
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=len(_CONN_TARGETS)) as pool:
        results = list(pool.map(_test_one, _CONN_TARGETS))
    return {"results": results}


def build_alerts(base_dir=None) -> dict:
    """Centre d'alertes in-app : calcule les alertes actives à partir des
    seuils KPI du dernier flux, de la fraîcheur des données et de l'état des
    derniers runs. Toujours exploitable (jamais d'exception remontée).

    Retour : {"available": bool, "count": int, "alerts": [{level,title,detail}]}
    """
    alerts = []
    data = fetch_runs(limit=2)
    available = data["available"]
    if available and data["runs"]:
        lk = data["runs"][0]["kpi"]
        rules = [
            ("taux_matching_ciam", "Matching CIAM",       "%", True),
            ("taux_presence_iehe", "Présence IEHE",       "%", True),
            ("score_qualite",      "Qualité données",     "%", True),
            ("taux_eligibilite_tp","Éligibilité TP",      "%", True),
        ]
        for key, label, unit, hib in rules:
            v = lk.get(key)
            if v is None:
                continue
            badge = _badge(v, key)
            if badge == "red":
                alerts.append({"level": "err", "title": f"{label} critique",
                               "detail": f"{v}{unit} sous le seuil bas"})
            elif badge == "orange":
                alerts.append({"level": "warn", "title": f"{label} à surveiller",
                               "detail": f"{v}{unit} sous le seuil cible"})
        nr = lk.get("non_rapproches")
        if nr:
            alerts.append({"level": "warn" if nr <= 20 else "err",
                           "title": "Non-rapprochés CIAM",
                           "detail": f"{nr} personne(s) non rapprochée(s)"})

    # Fraîcheur des données (dernier import).
    if available and data["runs"]:
        try:
            d = datetime.datetime.fromisoformat(data["runs"][0]["date_import"])
            if d.tzinfo is not None:
                d = d.replace(tzinfo=None)
            hours = (datetime.datetime.now() - d).total_seconds() / 3600
            if hours > 48:
                alerts.append({"level": "warn", "title": "Données obsolètes",
                               "detail": f"Dernier run ETL il y a {hours:.0f} h"})
        except Exception:
            pass

    # Dernier run en échec (depuis l'historique local).
    if base_dir is not None:
        try:
            from core import run_history
            runs = run_history.recent(base_dir, n=5)
            if runs and not runs[0].get("success", True):
                alerts.append({"level": "err", "title": "Dernier run en échec",
                               "detail": runs[0].get("summary", "")})
        except Exception:
            pass

    if not available:
        alerts.append({"level": "warn", "title": "Base inaccessible",
                       "detail": "Indicateurs de supervision indisponibles"})

    return {"available": available, "count": len(alerts), "alerts": alerts}


def get_thresholds() -> dict:
    """Seuils effectifs : défauts du code + surcharges du module Admin."""
    try:
        from config.admin_store import get_thresholds as _gt
        return _gt(THRESHOLDS)
    except Exception:
        return THRESHOLDS


def _badge(value, key: str) -> str:
    """Retourne 'green' / 'orange' / 'red' / 'na' selon les seuils."""
    if value is None:
        return "na"
    th = get_thresholds().get(key)
    if not th:
        return "neutral"
    if th["higher_is_better"]:
        if value >= th["green"]:
            return "green"
        if value >= th["orange"]:
            return "orange"
        return "red"
    if value <= th["green"]:
        return "green"
    if value <= th["orange"]:
        return "orange"
    return "red"


def build_dashboard(limit: int = 30) -> dict:
    """Construit le modèle complet consommé par l'IHM dashboard.

    Structure retournée :
        {
          "available": bool, "error": str|None,
          "freshness_hours": float|None, "last_run": {...}|None,
          "nb_runs": int,
          "kpis": [ {key,label,value,unit,badge,delta} ... ],
          "trends": { "labels": [...], "series": { key: [...] } },
          "flags": [ {level, text, doc} ]
        }
    """
    data = fetch_runs(limit)
    if not data["available"]:
        return {
            "available": False, "error": data["error"],
            "freshness_hours": None, "last_run": None, "nb_runs": 0,
            "kpis": [], "trends": {"labels": [], "series": {}}, "flags": [],
        }

    runs = data["runs"]
    if not runs:
        return {
            "available": True, "error": None,
            "freshness_hours": None, "last_run": None, "nb_runs": 0,
            "kpis": [], "trends": {"labels": [], "series": {}},
            "flags": [{"level": "warn", "text": "Aucun flux en base.", "doc": None}],
        }

    last = runs[0]
    prev = runs[1] if len(runs) > 1 else None

    # Fraîcheur : heures depuis le dernier import.
    freshness = None
    try:
        d = datetime.datetime.fromisoformat(last["date_import"])
        if d.tzinfo is not None:
            d = d.replace(tzinfo=None)
        freshness = round((datetime.datetime.now() - d).total_seconds() / 3600, 1)
    except Exception:
        pass

    kpi_meta = [
        ("volume_flux",         "Volume du flux",        "lignes"),
        ("personnes_uniques",   "Personnes uniques",     "pers."),
        ("taux_matching_ciam",  "Matching CIAM",         "%"),
        ("non_rapproches",      "Non-rapprochés CIAM",   "pers."),
        ("score_qualite",       "Qualité données",       "%"),
        ("taux_presence_iehe",  "Présence IEHE",         "%"),
        ("manquants_iehe",      "Manquants IEHE",        "pers."),
        ("taux_eligibilite_tp", "Éligibilité TP",        "%"),
        ("eligibles_tp",        "Éligibles TP",          "pers."),
        ("taux_ged",            "Rapprochement TP GED",  "%"),
    ]

    kpis = []
    for key, label, unit in kpi_meta:
        v = last["kpi"].get(key)
        p = prev["kpi"].get(key) if prev else None
        delta = None
        if isinstance(v, (int, float)) and isinstance(p, (int, float)):
            delta = round(v - p, 2)
        kpis.append({
            "key": key, "label": label, "unit": unit,
            "value": v, "badge": _badge(v, key), "delta": delta,
            "drillable": key in DRILLABLE_KPIS,
        })

    # Tendances (ordre chronologique croissant pour les sparklines).
    chrono = list(reversed(runs))
    labels = []
    for r in chrono:
        try:
            labels.append(datetime.datetime.fromisoformat(r["date_import"]).strftime("%d/%m"))
        except Exception:
            labels.append(str(r["date_import"])[:10])
    series = {key: [r["kpi"].get(key) for r in chrono] for key in _KPI_PATHS}

    # Donut TP GED (état du dernier flux) + radar (jour vs moyenne période).
    lk = last["kpi"]
    ged_found = lk.get("ged_trouves")
    ged_ko = lk.get("ged_ko")
    if ged_found is None and lk.get("ged_recus") is not None and lk.get("taux_ged") is not None:
        ged_found = round(lk["ged_recus"] * lk["taux_ged"] / 100)
        ged_ko = lk["ged_recus"] - ged_found
    donut_ged = {"found": ged_found, "ko": ged_ko}

    def _avg(key):
        vals = [v for v in series.get(key, []) if isinstance(v, (int, float))]
        return round(sum(vals) / len(vals), 2) if vals else None

    radar = {
        "axes": ["Matching CIAM", "Présence IEHE", "Éligibilité TP",
                 "Qualité données", "TP GED"],
        "keys": ["taux_matching_ciam", "taux_presence_iehe", "taux_eligibilite_tp",
                 "score_qualite", "taux_ged"],
        "today": [lk.get(k) for k in ["taux_matching_ciam", "taux_presence_iehe",
                  "taux_eligibilite_tp", "score_qualite", "taux_ged"]],
        "avg": [_avg(k) for k in ["taux_matching_ciam", "taux_presence_iehe",
                "taux_eligibilite_tp", "score_qualite", "taux_ged"]],
    }

    # Flags métier.
    flags = []
    ged = last["kpi"].get("taux_ged")
    if ged is not None and ged <= 5:
        flags.append({
            "level": "warn",
            "text": f"TP GED à {ged}% — taux très bas, à valider métier "
                    "(règle de rapprochement GED en cours de qualification).",
            "doc": "docs/PROMPT_CLAUDE_CODE_FINAL.md",
        })
    if freshness is not None and freshness > 48:
        flags.append({
            "level": "warn",
            "text": f"Dernier run ETL il y a {freshness:.0f}h — données potentiellement obsolètes.",
            "doc": None,
        })

    return {
        "available": True, "error": None,
        "freshness_hours": freshness,
        "last_run": {"flux_id": last["flux_id"], "date_import": last["date_import"]},
        "nb_runs": len(runs),
        "kpis": kpis,
        "trends": {"labels": labels, "series": series},
        "donut_ged": donut_ged,
        "radar": radar,
        "flags": flags,
    }


# ---------------------------------------------------------------------------
# Enrichissement (#5) + Drill-down par KPI (#6)
# ---------------------------------------------------------------------------
def fetch_latest_payload() -> dict | None:
    """Payload JSON complet du flux le plus récent (ou None si BDD KO/vide)."""
    try:
        from sqlalchemy import text
        with _engine().connect() as conn:
            row = conn.execute(text(
                "SELECT payload FROM rptpsc.output_kpi_json "
                "ORDER BY date_import DESC LIMIT 1"
            )).fetchone()
    except Exception:
        return None
    return _parse_payload(row[0]) if row else None


def build_details(payload: dict | None = None) -> dict:
    """Graphiques d'enrichissement issus du dernier flux (sections annexes
    du JSON déjà historisé). Tout est défensif : sections absentes -> vides."""
    if payload is None:
        payload = fetch_latest_payload()
    if not payload:
        return {"available": False}

    # Matching par méthode (doughnut). On n'aligne que les paires (label, valeur)
    # dont la valeur est numérique pour éviter tout décalage dans Chart.js.
    methodes = _navx(payload, "5_CIAM", "Annexe", "Detail_Matching", "Par_Methode", default={})
    if not isinstance(methodes, dict):
        methodes = {}
    by_method = {
        "labels": [k for k, v in methodes.items() if isinstance(v, (int, float))],
        "data":   [v for v in methodes.values() if isinstance(v, (int, float))],
    }

    # Matching par société (barres groupées rapprochés / non-rapprochés).
    seg = _navx(payload, "5_CIAM", "Annexe", "Matching_Par_Segment",
                "B2_Par_Societe", "Resultats", default={})
    if not isinstance(seg, dict):
        seg = {}
    by_societe = {"labels": [], "rapproches": [], "non_rap": []}
    for soc, v in seg.items():
        if not isinstance(v, dict):
            continue
        by_societe["labels"].append(soc)
        by_societe["rapproches"].append(v.get("Rapproches"))
        by_societe["non_rap"].append(v.get("Non_Rapproches"))

    # Éligibles TP par mois (barres) — labels/valeurs alignés sur le numérique.
    mois = _navx(payload, "7_Carte_TP", "Eligibilite_Globale", "Eligible_Par_Mois", default={})
    if not isinstance(mois, dict):
        mois = {}
    tp_by_month = {
        "labels": [k for k, v in mois.items() if isinstance(v, (int, float))],
        "data":   [v for v in mois.values() if isinstance(v, (int, float))],
    }

    # Décomposition de la qualité KO (barres horizontales).
    deco = _navx(payload, "5_CIAM", "Score_Qualite_Donnees", "Decomposition_KO", default={})
    if not isinstance(deco, dict):
        deco = {}
    quality_ko = {"labels": [], "data": []}
    for label, sub in deco.items():
        nb = sub.get("Nombre") if isinstance(sub, dict) else None
        if isinstance(nb, (int, float)):
            quality_ko["labels"].append(label.replace("_", " "))
            quality_ko["data"].append(nb)

    return {"available": True, "by_method": by_method, "by_societe": by_societe,
            "tp_by_month": tp_by_month, "quality_ko": quality_ko}


# Drill-down : pour chaque KPI, où trouver le détail ventilé dans le JSON.
def kpi_detail(key: str, payload: dict | None = None) -> dict:
    """Retourne {title, columns, rows} pour le détail d'un KPI, ou
    {available:False} si pas de ventilation disponible."""
    if payload is None:
        payload = fetch_latest_payload()
    if not payload:
        return {"available": False, "reason": "Base inaccessible ou flux absent."}

    def table(title, columns, rows):
        return {"available": True, "title": title, "columns": columns, "rows": rows}

    if key in ("non_rapproches", "taux_matching_ciam"):
        seg = _navx(payload, "5_CIAM", "Annexe", "Matching_Par_Segment",
                    "B2_Par_Societe", "Resultats", default={})
        if not isinstance(seg, dict):
            seg = {}
        rows = [[soc, v.get("Total"), v.get("Rapproches"), v.get("Non_Rapproches"),
                 v.get("Taux")] for soc, v in seg.items() if isinstance(v, dict)]
        return table("Matching CIAM par société",
                     ["Société", "Total", "Rapprochés", "Non-rapprochés", "Taux %"], rows)

    if key == "score_qualite":
        deco = _navx(payload, "5_CIAM", "Score_Qualite_Donnees", "Decomposition_KO", default={})
        if not isinstance(deco, dict):
            deco = {}
        rows = [[label.replace("_", " "), sub.get("Nombre"), sub.get("Pct")]
                for label, sub in deco.items() if isinstance(sub, dict)]
        return table("Décomposition de la qualité KO",
                     ["Catégorie", "Nombre", "%"], rows)

    if key == "manquants_iehe":
        glob = _navx(payload, "6_IEHE", "Presence_Globale", "Manquants_IEHE", default={})
        if not isinstance(glob, dict):
            glob = {}
        rows = [["Assurés manquants", glob.get("dont_Assures")],
                ["Conjoints manquants", glob.get("dont_Conjoints")],
                ["Total manquants", glob.get("Nombre")]]
        return table("Manquants IEHE par type", ["Type", "Nombre"], rows)

    if key in ("taux_eligibilite_tp", "eligibles_tp"):
        mois = _navx(payload, "7_Carte_TP", "Eligibilite_Globale", "Eligible_Par_Mois", default={})
        if not isinstance(mois, dict):
            mois = {}
        rows = [[m, n] for m, n in mois.items()]
        return table("Éligibles TP par mois d'adhésion", ["Mois", "Éligibles"], rows)

    if key in ("taux_ged", "ged_recus"):
        soc = _navx(payload, "7_Carte_TP", "Controle_GED_Quotidien", "Par_Societe", default={})
        if not isinstance(soc, dict):
            soc = {}
        rows = [[s, v.get("Population_Eligible"), v.get("Trouves_GED"), v.get("Taux_GED")]
                for s, v in soc.items() if isinstance(v, dict)]
        return table("Contrôle GED par société",
                     ["Société", "Éligibles", "Trouvés GED", "Taux GED %"], rows)

    return {"available": False, "reason": "Pas de ventilation détaillée pour ce KPI."}


# ---------------------------------------------------------------------------
# Comparaison de flux (#1)
# ---------------------------------------------------------------------------
def flux_list(limit: int = 60) -> dict:
    """Liste des flux disponibles (id + date) pour les sélecteurs de comparaison."""
    data = fetch_runs(limit)
    if not data["available"]:
        return {"available": False, "flux": []}
    flux = [{"flux_id": r["flux_id"], "date_import": r["date_import"]} for r in data["runs"]]
    return {"available": True, "flux": flux}


def fetch_payload_by_flux(flux_id) -> dict | None:
    try:
        from sqlalchemy import text
        with _engine().connect() as conn:
            row = conn.execute(text(
                "SELECT payload FROM rptpsc.output_kpi_json "
                "WHERE flux_id = :fid ORDER BY date_import DESC LIMIT 1"
            ), {"fid": flux_id}).fetchone()
    except Exception:
        return None
    return _parse_payload(row[0]) if row else None


def compare_flux(flux_a, flux_b) -> dict:
    """Compare deux flux : aplatit les deux payloads et aligne par chemin.
    Retour : {available, rows:[{path, a, b, delta}]}."""
    pa = fetch_payload_by_flux(flux_a)
    pb = fetch_payload_by_flux(flux_b)
    if pa is None or pb is None:
        return {"available": False, "reason": "Flux introuvable(s) ou base inaccessible."}

    def _flat(payload, prefix=""):
        flat = {}
        for k, v in (payload or {}).items():
            key = f"{prefix} › {k}" if prefix else str(k)
            if isinstance(v, dict):
                flat.update(_flat(v, key))
            elif not isinstance(v, list):
                flat[key] = v
        return flat

    fa, fb = _flat(pa), _flat(pb)
    keys = list(dict.fromkeys(list(fa.keys()) + list(fb.keys())))
    rows = []
    for k in keys:
        va, vb = fa.get(k), fb.get(k)
        delta = None
        # bool hérite de int : on l'exclut pour ne pas calculer de delta sur True/False.
        if (isinstance(va, (int, float)) and not isinstance(va, bool)
                and isinstance(vb, (int, float)) and not isinstance(vb, bool)):
            delta = round(vb - va, 4)
        rows.append({"path": k, "a": va, "b": vb, "delta": delta,
                     "changed": va != vb})
    return {"available": True, "rows": rows}


# ---------------------------------------------------------------------------
# Export global Excel (#5) + Qualité des données (#4)
# ---------------------------------------------------------------------------
def _flatten_rows(obj, prefix=""):
    """Aplatit un dict/list imbriqué en lignes (chemin, valeur)."""
    rows = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = f"{prefix} › {k}" if prefix else str(k)
            if isinstance(v, (dict, list)):
                rows.extend(_flatten_rows(v, key))
            else:
                rows.append((key, v))
    elif isinstance(obj, list):
        if all(not isinstance(x, (dict, list)) for x in obj):
            rows.append((prefix, ", ".join("" if x is None else str(x) for x in obj)))
        else:
            for i, x in enumerate(obj):
                rows.extend(_flatten_rows(x, f"{prefix} [{i}]"))
    else:
        rows.append((prefix, obj))
    return rows


def _safe_sheet(name: str) -> str:
    s = "".join(c for c in str(name) if c.isalnum() or c in " _-")[:31]
    return s or "Donnees"


def export_all_workbook():
    """Construit un classeur Excel multi-onglets (1 onglet/section) avec TOUS
    les indicateurs du dernier flux. Retourne (BytesIO, nom) ou (None, raison)."""
    payload = fetch_latest_payload()
    if not payload:
        return None, "Base inaccessible ou aucun flux."
    try:
        # Aplatit d'abord : on n'ouvre pas un classeur vide (erreur openpyxl).
        sections_data = []
        for section, content in payload.items():
            rows = _flatten_rows(content)
            if rows:
                sections_data.append((section, rows))
        if not sections_data:
            return None, "Aucune donnée à exporter."

        import io
        import pandas as pd
        buf = io.BytesIO()
        used = set()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            for section, rows in sections_data:
                df = pd.DataFrame(rows, columns=["Indicateur", "Valeur"])
                name = _safe_sheet(section)
                base = name
                i = 1
                while name in used:
                    # Troncature dynamique : le nom final reste <= 31 caractères.
                    suffix = f"_{i}"
                    name = f"{base[:31 - len(suffix)]}{suffix}"
                    i += 1
                used.add(name)
                df.to_excel(writer, sheet_name=name, index=False)
        buf.seek(0)
        return buf, "tous_les_kpi.xlsx"
    except Exception as exc:
        return None, f"Export impossible : {exc}"


# Indicateurs d'anomalie suivis par le module Qualité (#4).
# (label, chemin Nombre, chemin Pct optionnel, sévérité de référence)
_QUALITY_DEFS = [
    ("Comptes sans email",          ["5_CIAM","Annexe","Qualite_Comptes_CIAM","F1a_Sans_Email","Nombre"],
                                     ["5_CIAM","Annexe","Qualite_Comptes_CIAM","F1a_Sans_Email","Pct"]),
    ("Comptes sans KPEP",           ["5_CIAM","Annexe","Qualite_Comptes_CIAM","F1b_Sans_KPEP","Nombre"],
                                     ["5_CIAM","Annexe","Qualite_Comptes_CIAM","F1b_Sans_KPEP","Pct"]),
    ("Doublons email CIAM",         ["5_CIAM","Annexe","Qualite_Comptes_CIAM","F1d_Doublons_Email","Nb_Emails_Dupliques"], None),
    ("Emails à risque",             ["5_CIAM","Score_Qualite_Donnees","Decomposition_KO","Email_CIAM_risque","Nombre"], None),
    ("KPEP NS ≠ CIAM",              ["5_CIAM","Score_Qualite_Donnees","Decomposition_KO","KPEP_NS_diff_CIAM","Nombre"], None),
    ("Prospects CIAM",              ["5_CIAM","Score_Qualite_Donnees","Decomposition_KO","Prospect_CIAM","Nombre"], None),
    ("DDN différente (NS↔CIAM)",    ["5_CIAM","Annexe","Incoherences_NS_CIAM","G1a_DDN_Differente","DDN_Differente","Nombre"], None),
    ("Nom/Prénom divergent",        ["5_CIAM","Annexe","Incoherences_NS_CIAM","G1b_Nom_Prenom_Divergent","Nom_Divergent","Nombre"], None),
    ("Manquants IEHE",              ["6_IEHE","Presence_Globale","Manquants_IEHE","Nombre"], None),
]


def build_quality(payload: dict | None = None) -> dict:
    """Synthèse qualité des données : score global + anomalies du dernier flux."""
    if payload is None:
        payload = fetch_latest_payload()
    if not payload:
        return {"available": False}
    score_ok = _navx(payload, "5_CIAM","Score_Qualite_Donnees","DATA_QUALITY_OK","Pct")
    score_ko = _navx(payload, "5_CIAM","Score_Qualite_Donnees","DATA_QUALITY_KO","Pct")
    anomalies = []
    for label, p_nb, p_pct in _QUALITY_DEFS:
        nb = _navx(payload, *p_nb)
        pct = _navx(payload, *p_pct) if p_pct else None
        if nb is None:
            continue
        # Sévérité simple selon le volume (à défaut de seuil métier dédié).
        level = "ok" if not nb else ("warn" if isinstance(nb, (int, float)) and nb <= 20 else "err")
        anomalies.append({"label": label, "nombre": nb, "pct": pct, "level": level})
    return {"available": True, "score_ok": score_ok, "score_ko": score_ko,
            "anomalies": anomalies}
# -----------------------------------------------------------------------
# Sélection par date (date picker)
# -----------------------------------------------------------------------

def fetch_available_dates(base_dir=None) -> dict:
    """Dates ayant des données : DB d'abord, sinon scan Output/."""
    try:
        from sqlalchemy import text
        with _engine().connect() as conn:
            rows = conn.execute(text(
                "SELECT DISTINCT CAST(date_import AS date) AS d "
                "FROM rptpsc.output_kpi_json ORDER BY d DESC LIMIT 365"
            )).fetchall()
        return {"available": True, "dates": [str(r[0]) for r in rows]}
    except Exception:
        pass
    # Fallback local : cherche Output/DDMMYYYY_Modele_clean.json
    if base_dir is None:
        return {"available": False, "dates": []}
    import re
    from pathlib import Path
    pat = re.compile(r'^(\d{2})(\d{2})(\d{4})_Modele_clean\.json$')
    dates = []
    for f in sorted((Path(base_dir) / "Output").iterdir(), reverse=True):
        m = pat.match(f.name)
        if m:
            dates.append(f"{m.group(3)}-{m.group(2)}-{m.group(1)}")  # YYYY-MM-DD
    return {"available": True, "dates": dates}


# -----------------------------------------------------------------------
# Sélection par date — réutilise exactement la logique de build_dashboard()
# -----------------------------------------------------------------------

def _build_dashboard_from_runs(runs: list, check_freshness: bool = True) -> dict:
    """Corps de build_dashboard(), extrait pour être partagé entre le mode
    'derniers runs' et le mode 'ancré sur une date'. runs[0] = le run
    affiché, runs[1] = celui contre lequel le delta est calculé."""
    last = runs[0]
    prev = runs[1] if len(runs) > 1 else None

    freshness = None
    try:
        d = datetime.datetime.fromisoformat(last["date_import"])
        if d.tzinfo is not None:
            d = d.replace(tzinfo=None)
        freshness = round((datetime.datetime.now() - d).total_seconds() / 3600, 1)
    except Exception:
        pass

    kpi_meta = [
        ("volume_flux",         "Volume du flux",        "lignes"),
        ("personnes_uniques",   "Personnes uniques",     "pers."),
        ("taux_matching_ciam",  "Matching CIAM",         "%"),
        ("non_rapproches",      "Non-rapprochés CIAM",   "pers."),
        ("score_qualite",       "Qualité données",       "%"),
        ("taux_presence_iehe",  "Présence IEHE",         "%"),
        ("manquants_iehe",      "Manquants IEHE",        "pers."),
        ("taux_eligibilite_tp", "Éligibilité TP",        "%"),
        ("eligibles_tp",        "Éligibles TP",          "pers."),
        ("taux_ged",            "Rapprochement TP GED",  "%"),
    ]

    kpis = []
    for key, label, unit in kpi_meta:
        v = last["kpi"].get(key)
        p = prev["kpi"].get(key) if prev else None
        delta = None
        if isinstance(v, (int, float)) and isinstance(p, (int, float)):
            delta = round(v - p, 2)
        kpis.append({
            "key": key, "label": label, "unit": unit,
            "value": v, "badge": _badge(v, key), "delta": delta,
            "drillable": key in DRILLABLE_KPIS,
        })

    chrono = list(reversed(runs))
    labels = []
    for r in chrono:
        try:
            labels.append(datetime.datetime.fromisoformat(r["date_import"]).strftime("%d/%m"))
        except Exception:
            labels.append(str(r["date_import"])[:10])
    series = {key: [r["kpi"].get(key) for r in chrono] for key in _KPI_PATHS}

    lk = last["kpi"]
    ged_found = lk.get("ged_trouves")
    ged_ko = lk.get("ged_ko")
    if ged_found is None and lk.get("ged_recus") is not None and lk.get("taux_ged") is not None:
        ged_found = round(lk["ged_recus"] * lk["taux_ged"] / 100)
        ged_ko = lk["ged_recus"] - ged_found
    donut_ged = {"found": ged_found, "ko": ged_ko}

    def _avg(key):
        vals = [v for v in series.get(key, []) if isinstance(v, (int, float))]
        return round(sum(vals) / len(vals), 2) if vals else None

    radar = {
        "axes": ["Matching CIAM", "Présence IEHE", "Éligibilité TP",
                 "Qualité données", "TP GED"],
        "keys": ["taux_matching_ciam", "taux_presence_iehe", "taux_eligibilite_tp",
                 "score_qualite", "taux_ged"],
        "today": [lk.get(k) for k in ["taux_matching_ciam", "taux_presence_iehe",
                  "taux_eligibilite_tp", "score_qualite", "taux_ged"]],
        "avg": [_avg(k) for k in ["taux_matching_ciam", "taux_presence_iehe",
                "taux_eligibilite_tp", "score_qualite", "taux_ged"]],
    }

    flags = []
    ged = last["kpi"].get("taux_ged")
    if ged is not None and ged <= 5:
        flags.append({
            "level": "warn",
            "text": f"TP GED à {ged}% — taux très bas, à valider métier.",
            "doc": "docs/PROMPT_CLAUDE_CODE_FINAL.md",
        })
    if check_freshness and freshness is not None and freshness > 48:
        flags.append({
            "level": "warn",
            "text": f"Dernier run ETL il y a {freshness:.0f}h — données potentiellement obsolètes.",
            "doc": None,
        })

    return {
        "available": True, "error": None,
        "freshness_hours": freshness,
        "last_run": {"flux_id": last["flux_id"], "date_import": last["date_import"]},
        "nb_runs": len(runs),
        "kpis": kpis,
        "trends": {"labels": labels, "series": series},
        "donut_ged": donut_ged,
        "radar": radar,
        "flags": flags,
    }


def fetch_runs_up_to(date_str: str, limit: int = 30, base_dir=None) -> dict:
    """Fenêtre de `limit` runs dont le flux_id (date logique DDMMYYYY) est
    <= date_str. runs[0] = le flux exact de cette date si présent. Tri sur
    flux_id, pas date_import — date_import est l'horodatage de chargement,
    pas la date logique du flux (cf. correction précédente)."""
    try:
        dt = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return {"available": False, "error": "Format invalide, attendu YYYY-MM-DD", "runs": [], "exact_fid": None}
    fid = dt.strftime("%d%m%Y")

    try:
        from sqlalchemy import text
        with _engine().connect() as conn:
            rows = conn.execute(text(
                "SELECT flux_id, date_import, payload "
                "FROM rptpsc.output_kpi_json "
                "WHERE TO_DATE(flux_id, 'DDMMYYYY') <= TO_DATE(:fid, 'DDMMYYYY') "
                "ORDER BY TO_DATE(flux_id, 'DDMMYYYY') DESC "
                "LIMIT :lim"
            ), {"fid": fid, "lim": limit}).fetchall()
        runs = [{
            "flux_id": r[0],
            "date_import": r[1].isoformat() if hasattr(r[1], "isoformat") else str(r[1]),
            "kpi": _extract(_parse_payload(r[2])),
        } for r in rows]
        return {"available": True, "error": None, "runs": runs, "exact_fid": fid}
    except Exception:
        pass

    if base_dir is not None:
        import re
        from pathlib import Path
        pat = re.compile(r'^(\d{2})(\d{2})(\d{4})_Modele_clean\.json$')
        out_dir = Path(base_dir) / "Output"
        candidates = []
        if out_dir.is_dir():
            for f in out_dir.iterdir():
                m = pat.match(f.name)
                if m:
                    fdt = datetime.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
                    if fdt <= dt:
                        candidates.append((fdt, f))
        candidates.sort(key=lambda x: x[0], reverse=True)
        runs = []
        for fdt, f in candidates[:limit]:
            try:
                payload = json.loads(f.read_text(encoding="utf-8"))
                runs.append({"flux_id": fdt.strftime("%d%m%Y"),
                             "date_import": fdt.isoformat(), "kpi": _extract(payload)})
            except Exception:
                continue
        return {"available": True, "error": None, "runs": runs, "exact_fid": fid}

    return {"available": False, "error": "Base inaccessible et aucun fichier local.", "runs": [], "exact_fid": fid}


def build_dashboard_for_date(date_str: str, limit: int = 30, base_dir=None) -> dict:
    data = fetch_runs_up_to(date_str, limit=limit, base_dir=base_dir)
    if not data["available"]:
        return {"available": False, "error": data["error"], "freshness_hours": None,
                "last_run": None, "nb_runs": 0, "kpis": [],
                "trends": {"labels": [], "series": {}}, "flags": []}
    runs = data["runs"]
    if not runs or runs[0]["flux_id"] != data["exact_fid"]:
        return {"available": True, "error": None, "freshness_hours": None,
                "last_run": None, "nb_runs": 0, "kpis": [],
                "trends": {"labels": [], "series": {}},
                "flags": [{"level": "warn", "text": f"Aucun flux pour le {date_str}.", "doc": None}]}
    return _build_dashboard_from_runs(runs, check_freshness=False)




def build_dashboard(limit: int = 30) -> dict:
    """Construit le modèle complet consommé par l'IHM dashboard.

    Structure retournée :
        {
          "available": bool, "error": str|None,
          "freshness_hours": float|None, "last_run": {...}|None,
          "nb_runs": int,
          "kpis": [ {key,label,value,unit,badge,delta} ... ],
          "trends": { "labels": [...], "series": { key: [...] } },
          "flags": [ {level, text, doc} ]
        }
    """
    data = fetch_runs(limit)
    if not data["available"]:
        return {
            "available": False, "error": data["error"],
            "freshness_hours": None, "last_run": None, "nb_runs": 0,
            "kpis": [], "trends": {"labels": [], "series": {}}, "flags": [],
        }
    

    runs = data["runs"]
    if not runs:
        return {
            "available": True, "error": None,
            "freshness_hours": None, "last_run": None, "nb_runs": 0,
            "kpis": [], "trends": {"labels": [], "series": {}},
            "flags": [{"level": "warn", "text": "Aucun flux en base.", "doc": None}],
        }

    last = runs[0]
    prev = runs[1] if len(runs) > 1 else None

    # Fraîcheur : heures depuis le dernier import.
    freshness = None
    try:
        d = datetime.datetime.fromisoformat(last["date_import"])
        if d.tzinfo is not None:
            d = d.replace(tzinfo=None)
        freshness = round((datetime.datetime.now() - d).total_seconds() / 3600, 1)
    except Exception:
        pass

    kpi_meta = [
        ("volume_flux",         "Volume du flux",        "lignes"),
        ("personnes_uniques",   "Personnes uniques",     "pers."),
        ("taux_matching_ciam",  "Matching CIAM",         "%"),
        ("non_rapproches",      "Non-rapprochés CIAM",   "pers."),
        ("score_qualite",       "Qualité données",       "%"),
        ("taux_presence_iehe",  "Présence IEHE",         "%"),
        ("manquants_iehe",      "Manquants IEHE",        "pers."),
        ("taux_eligibilite_tp", "Éligibilité TP",        "%"),
        ("eligibles_tp",        "Éligibles TP",          "pers."),
        ("taux_ged",            "Rapprochement TP GED",  "%"),
    ]

    kpis = []
    for key, label, unit in kpi_meta:
        v = last["kpi"].get(key)
        p = prev["kpi"].get(key) if prev else None
        delta = None
        if isinstance(v, (int, float)) and isinstance(p, (int, float)):
            delta = round(v - p, 2)
        kpis.append({
            "key": key, "label": label, "unit": unit,
            "value": v, "badge": _badge(v, key), "delta": delta,
            "drillable": key in DRILLABLE_KPIS,
        })

    # Tendances (ordre chronologique croissant pour les sparklines).
    chrono = list(reversed(runs))
    labels = []
    for r in chrono:
        try:
            labels.append(datetime.datetime.fromisoformat(r["date_import"]).strftime("%d/%m"))
        except Exception:
            labels.append(str(r["date_import"])[:10])
    series = {key: [r["kpi"].get(key) for r in chrono] for key in _KPI_PATHS}

    # Donut TP GED (état du dernier flux) + radar (jour vs moyenne période).
    lk = last["kpi"]
    ged_found = lk.get("ged_trouves")
    ged_ko = lk.get("ged_ko")
    if ged_found is None and lk.get("ged_recus") is not None and lk.get("taux_ged") is not None:
        ged_found = round(lk["ged_recus"] * lk["taux_ged"] / 100)
        ged_ko = lk["ged_recus"] - ged_found
    donut_ged = {"found": ged_found, "ko": ged_ko}

    def _avg(key):
        vals = [v for v in series.get(key, []) if isinstance(v, (int, float))]
        return round(sum(vals) / len(vals), 2) if vals else None

    radar = {
        "axes": ["Matching CIAM", "Présence IEHE", "Éligibilité TP",
                 "Qualité données", "TP GED"],
        "keys": ["taux_matching_ciam", "taux_presence_iehe", "taux_eligibilite_tp",
                 "score_qualite", "taux_ged"],
        "today": [lk.get(k) for k in ["taux_matching_ciam", "taux_presence_iehe",
                  "taux_eligibilite_tp", "score_qualite", "taux_ged"]],
        "avg": [_avg(k) for k in ["taux_matching_ciam", "taux_presence_iehe",
                "taux_eligibilite_tp", "score_qualite", "taux_ged"]],
    }

    # Flags métier.
    flags = []
    ged = last["kpi"].get("taux_ged")
    if ged is not None and ged <= 5:
        flags.append({
            "level": "warn",
            "text": f"TP GED à {ged}% — taux très bas, à valider métier "
                    "(règle de rapprochement GED en cours de qualification).",
            "doc": "docs/PROMPT_CLAUDE_CODE_FINAL.md",
        })
    if freshness is not None and freshness > 48:
        flags.append({
            "level": "warn",
            "text": f"Dernier run ETL il y a {freshness:.0f}h — données potentiellement obsolètes.",
            "doc": None,
        })

    return {
        "available": True, "error": None,
        "freshness_hours": freshness,
        "last_run": {"flux_id": last["flux_id"], "date_import": last["date_import"]},
        "nb_runs": len(runs),
        "kpis": kpis,
        "trends": {"labels": labels, "series": series},
        "donut_ged": donut_ged,
        "radar": radar,
        "flags": flags,
    }
