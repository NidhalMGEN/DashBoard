"""Routes du Module 02 — Exécution requêtes SQL (wizard 3 étapes).

Sécurité : les mots de passe sont conservés UNIQUEMENT en session Flask
côté serveur (jamais en localStorage ni en cookie exposé), effacés à la
déconnexion. Les runs sont identifiés par un `run_id` ; chaque run a sa
propre file SSE.
"""

from __future__ import annotations

import json
import time
import uuid
import queue
import threading
from datetime import datetime
from pathlib import Path

from flask import (Blueprint, render_template, request, jsonify, session,
                   Response, current_app, send_file, abort)

from . import env_loader, query_loader, executor

bp = Blueprint(
    "sql_runner", __name__,
    url_prefix="/sql",
    template_folder="templates",
)

# Runs actifs : run_id -> {"queue": Queue, "files": [Path], "done": bool}
_RUNS: dict[str, dict] = {}


# ── Helpers contexte ──────────────────────────────────────────────────────
def _base_dir() -> Path:
    return current_app.config["BASE_DIR"]


def _load_env():
    sql_dir = env_loader.default_sql_dir(_base_dir())
    envs, valid_dbs = env_loader.load_environments(sql_dir / "environments.ini")
    return sql_dir, envs, valid_dbs


def _creds() -> dict | None:
    return session.get("sql_creds")


# ── Vues / API ────────────────────────────────────────────────────────────
@bp.route("/")
def index():
    return render_template("sql_runner/index.html", active_module="sql_runner")


@bp.route("/api/environments")
def api_environments():
    _, envs, _ = _load_env()
    return jsonify({"environments": sorted(envs.keys())})


@bp.route("/api/prefill")
def api_prefill():
    """Identifiants pré-remplis depuis credentials.ini pour l'environnement
    demandé. Ne renvoie JAMAIS les mots de passe : seulement un booléen
    indiquant qu'ils sont connus côté serveur."""
    env_name = request.args.get("environment", "")
    try:
        from config.credentials_loader import CREDENTIALS
        return jsonify(CREDENTIALS.sql_prefill(env_name))
    except Exception:
        return jsonify({"ora_user": "", "ora_pass_set": False,
                        "iehe_user": "", "iehe_pass_set": False})


def _ini_creds_available(env_names) -> bool:
    """True si credentials.ini fournit des identifiants Oracle (mdg) pour au
    moins un environnement — auquel cas l'écran de connexion est superflu."""
    try:
        from config.credentials_loader import CREDENTIALS
    except Exception:
        return False
    for env in env_names:
        u, p = CREDENTIALS.sql_secret_for(env, "mdg")
        if u and p:
            return True
    return False


@bp.route("/api/queries")
def api_queries():
    sql_dir, envs, valid_dbs = _load_env()
    queries, erreurs = query_loader.load_queries(sql_dir, valid_dbs)
    creds = _creds()
    env_names = sorted(envs.keys())
    ini_creds = _ini_creds_available(env_names)
    return jsonify({
        "queries": query_loader.queries_to_catalog(queries),
        "errors": erreurs,
        "connected": creds is not None,
        # Si les identifiants viennent du .ini, l'écran de connexion est inutile.
        "ini_creds_available": ini_creds,
        "environments": env_names,
        "default_env": (creds["environment"] if creds else (env_names[0] if env_names else None)),
    })


@bp.route("/api/connect", methods=["POST"])
def api_connect():
    """Teste les connexions et stocke les credentials en session."""
    data = request.get_json(silent=True) or {}
    ora_user = (data.get("ora_user") or "").strip()
    ora_pass = data.get("ora_pass") or ""
    iehe_user = (data.get("iehe_user") or "").strip()
    iehe_pass = data.get("iehe_pass") or ""

    sql_dir, envs, _ = _load_env()
    env_name = data.get("environment") or (sorted(envs.keys())[0] if envs else None)
    if env_name not in envs:
        return jsonify({"error": "Environnement inconnu"}), 400
    env = envs[env_name]

    # Complément depuis credentials.ini : l'utilisateur peut ne renseigner que
    # ce qui manque (mot de passe laissé vide = on prend celui du fichier).
    try:
        from config.credentials_loader import CREDENTIALS
        cu, cp = CREDENTIALS.sql_secret_for(env_name, "mdg")
        ora_user = ora_user or (cu or "")
        ora_pass = ora_pass or (cp or "")
        iu, ip = CREDENTIALS.sql_secret_for(env_name, "iehe")
        iehe_user = iehe_user or (iu or "")
        iehe_pass = iehe_pass or (ip or "")
    except Exception:
        pass

    # IEHE par défaut = creds Oracle si non fournis.
    iehe_user = iehe_user or ora_user
    iehe_pass = iehe_pass or ora_pass

    if not ora_user or not ora_pass:
        return jsonify({"error": "Identifiants Oracle requis (formulaire ou credentials.ini)"}), 400

    results = {}
    for db_key, info in env.items():
        conn_obj = executor.make_conn_params(db_key, info)
        # PostgreSQL -> creds IEHE ; Oracle (et autres) -> creds Oracle.
        if conn_obj["type"] == "postgresql":
            ok, msg = executor.test_connection(conn_obj, iehe_user, iehe_pass)
        else:
            ok, msg = executor.test_connection(conn_obj, ora_user, ora_pass)
        results[db_key] = {"ok": ok, "msg": msg}

    # On conserve les identifiants en session dès qu'on a des creds Oracle
    # (fallback pour les bases dont la section .ini est vide). Les bases KO
    # ne bloquent pas la sélection : l'utilisateur voit leur statut et choisit.
    all_ok = all(r["ok"] for r in results.values())
    session["sql_creds"] = {
        "ora_user": ora_user, "ora_pass": ora_pass,
        "iehe_user": iehe_user, "iehe_pass": iehe_pass,
        "environment": env_name,
    }
    session.permanent = False
    return jsonify({"ok": True, "all_ok": all_ok, "results": results,
                    "environment": env_name})


@bp.route("/api/session", methods=["DELETE"])
def api_disconnect():
    session.pop("sql_creds", None)
    return jsonify({"ok": True})


@bp.route("/api/execute", methods=["POST"])
def api_execute():
    """Lance l'exécution d'une sélection de requêtes. Retourne un run_id.

    Les identifiants proviennent de credentials.ini (par env/base) ; la session
    de connexion n'est qu'un fallback optionnel. Aucune connexion préalable
    n'est requise si le .ini est renseigné."""
    creds = _creds()  # peut être None : on s'appuie alors sur credentials.ini

    data = request.get_json(silent=True) or {}
    names = data.get("queries") or []
    dates = data.get("dates") or {}
    param_values = data.get("params") or {}
    timeout = int(data.get("timeout") or 300)
    # Environnement choisi PAR REQUÊTE (QUALIF/PROD) — permet de lancer des
    # requêtes qualif et prod dans le même run.
    query_envs_in = data.get("query_envs") or {}

    sql_dir, envs, valid_dbs = _load_env()
    if not envs:
        return jsonify({"error": "Aucun environnement configuré"}), 400
    default_env = (creds["environment"] if creds else None) or sorted(envs.keys())[0]

    all_queries, _ = query_loader.load_queries(sql_dir, valid_dbs)
    selected = {n: all_queries[n] for n in names if n in all_queries}
    if not selected:
        return jsonify({"error": "Aucune requête valide sélectionnée"}), 400

    # Résolution de l'environnement de chaque requête (validation incluse).
    query_envs = {}
    for n in selected:
        e = query_envs_in.get(n) or default_env
        if e not in envs:
            return jsonify({"error": f"Environnement '{e}' inconnu pour {n}"}), 400
        query_envs[n] = e

    # Garde : requête IEHE sans driver psycopg.
    if any(e["db"] == "iehe" for e in selected.values()):
        psycopg, _v = executor._load_psycopg()
        if psycopg is None:
            return jsonify({"error": "Requête IEHE sélectionnée mais 'psycopg' non installé."}), 400

    # Résolution des identifiants par (env, base) : credentials.ini d'abord,
    # puis fallback sur les creds saisis en session (env de connexion).
    try:
        from config.credentials_loader import CREDENTIALS
    except Exception:
        CREDENTIALS = None

    def creds_resolver(env_name, db_key):
        info = envs.get(env_name, {}).get(db_key, {})
        if CREDENTIALS is not None:
            # Section credentials.ini explicite (bases génériques) si fournie.
            cred_section = info.get("cred_section")
            if cred_section:
                u, p = CREDENTIALS.credentials_for(cred_section)
                if u and p:
                    return u, p
            u, p = CREDENTIALS.sql_secret_for(env_name, db_key)
            if u and p:
                return u, p
        if not creds:
            return None, None  # ni .ini ni session -> erreur gérée par execute_query
        if info.get("type") == "postgresql":
            return creds.get("iehe_user"), creds.get("iehe_pass")
        return creds.get("ora_user"), creds.get("ora_pass")

    date_prefix = (dates.get("quotidien") or dates.get("hebdo") or "").replace("/", "")
    output_dir = env_loader.default_sql_dir(_base_dir()).parent.parent / "output" / "SQL" / date_prefix

    run_id = uuid.uuid4().hex[:12]
    q: queue.Queue = queue.Queue()
    _RUNS[run_id] = {"queue": q, "files": [], "done": False}

    _logs: list[str] = []

    def emit(ev):
        # Conserve une trace texte pour l'historique (consultation a posteriori).
        if ev.get("type") == "log" and len(_logs) < 2000:
            _logs.append(f"[{ev.get('ts', '')}] {ev.get('text', '')}")
        elif ev.get("type") == "query" and len(_logs) < 2000:
            _logs.append(f"[{ev.get('status', '')}] {ev.get('name', '')}"
                         + (f" ({ev['env']})" if ev.get("env") else "")
                         + (f" — {ev['msg']}" if ev.get("msg") else ""))
        q.put(ev)

    base_dir = _base_dir()
    user = (creds or {}).get("ora_user", "") or "(credentials.ini)"
    ts_start = datetime.now()
    env_breakdown = {}
    for e in query_envs.values():
        env_breakdown[e] = env_breakdown.get(e, 0) + 1

    def _record(success, files):
        ts_end = datetime.now()
        from core import run_history
        run_history.record(base_dir, {
            "kind": "sql",
            "ts_start": ts_start.isoformat(timespec="seconds"),
            "ts_end": ts_end.isoformat(timespec="seconds"),
            "duration_s": round((ts_end - ts_start).total_seconds(), 1),
            "success": success,
            "user": user,
            "summary": f"{len(selected)} requête(s) — "
                       + " · ".join(f"{n} {e}" for e, n in env_breakdown.items()),
            "details": {"queries": list(selected.keys()),
                        "query_envs": query_envs,
                        "files": [f.name for f in files]},
            "logs": list(_logs),
        })

    def worker():
        try:
            files = executor.run_queries(
                selected=selected, environments=envs, query_envs=query_envs,
                creds_resolver=creds_resolver, dates=dates,
                param_values=param_values, output_dir=output_dir,
                timeout=timeout, emit=emit,
            )
            _RUNS[run_id]["files"] = files
            emit({"type": "done", "success": True,
                  "files": [f.name for f in files]})
            _record(True, files)
        except Exception as exc:
            emit({"type": "log", "level": "error",
                  "text": f"Erreur d'exécution : {exc}",
                  "ts": datetime.now().strftime("%H:%M:%S")})
            emit({"type": "done", "success": False, "files": []})
            _record(False, [])
        finally:
            _RUNS[run_id]["done"] = True

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"ok": True, "run_id": run_id})


@bp.route("/api/stream/<run_id>")
def api_stream(run_id):
    run = _RUNS.get(run_id)
    if not run:
        abort(404)

    def gen():
        q = run["queue"]
        last = time.time()
        while True:
            try:
                msg = q.get(timeout=1)
                yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
                if msg.get("type") == "done":
                    break
            except queue.Empty:
                if time.time() - last >= 15:
                    yield ": keep-alive\n\n"
                    last = time.time()

    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@bp.route("/api/results/<run_id>")
def api_results(run_id):
    run = _RUNS.get(run_id)
    if not run:
        abort(404)
    return jsonify({"files": [{"name": f.name} for f in run["files"]]})


@bp.route("/api/download/<run_id>/<path:filename>")
def api_download(run_id, filename):
    run = _RUNS.get(run_id)
    if not run:
        abort(404)
    for f in run["files"]:
        if f.name == filename and f.exists():
            return send_file(str(f), as_attachment=True, download_name=f.name)
    abort(404)
