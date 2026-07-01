"""Exécution SQL — extraction de launch_SQL_query_V2.py.

Comportement métier conservé à l'identique : dispatch Oracle/PostgreSQL,
timeout par requête (Timer + cancel), substitution des paramètres,
post-processors, export Excel un fichier par catégorie.

Les drivers (oracledb, psycopg) et pandas sont importés paresseusement afin
que le module reste importable même si une dépendance manque (état dégradé
explicite au moment de l'exécution, jamais à l'import).
"""

from __future__ import annotations

import re
import time
import threading
from datetime import datetime, timedelta
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

_PLACEHOLDER_RE = re.compile(r"\{([A-Z][A-Z0-9_]*)\}")


# ── psycopg (optionnel, requis seulement pour IEHE) ───────────────────────
def _load_psycopg():
    try:
        import psycopg as p
        return p, 3
    except ImportError:
        try:
            import psycopg2 as p  # type: ignore
            return p, 2
        except ImportError:
            return None, 0


# ── Connexion (dispatch générique par TYPE : oracle | postgresql) ──────────
def _db_type(db_info: dict) -> str:
    """Type d'une base : clé `type` explicite (posée par env_loader), sinon
    déduction (sid -> oracle, dbname -> postgresql). Permet d'ajouter une
    nouvelle base via environments.ini sans modifier ce code."""
    t = (db_info.get("type") or "").strip().lower()
    if t in ("oracle", "postgresql"):
        return t
    if db_info.get("sid"):
        return "oracle"
    if db_info.get("dbname"):
        return "postgresql"
    raise ValueError(f"Type de base indéterminable : {db_info}")


def make_conn_params(db_key: str, db_info: dict[str, str]):
    """Retourne un objet de connexion {type, params} dispatché par type.
    `db_key` est conservé pour compat d'appel mais le type prime."""
    db_type = _db_type(db_info)
    if db_type == "oracle":
        import oracledb
        return {"type": "oracle",
                "params": oracledb.makedsn(db_info["host"], db_info["port"], sid=db_info["sid"])}
    if db_type == "postgresql":
        port = db_info["port"]
        return {"type": "postgresql",
                "params": {"host": db_info["host"],
                           "port": int(port) if str(port).isdigit() else port,
                           "dbname": db_info["dbname"]}}
    raise ValueError(f"Type de base non supporté : '{db_type}'")


def connect(conn_obj, user: str, password: str):
    if conn_obj["type"] == "oracle":
        import oracledb
        return oracledb.connect(user=user, password=password, dsn=conn_obj["params"])
    if conn_obj["type"] == "postgresql":
        psycopg, _ = _load_psycopg()
        if psycopg is None:
            raise RuntimeError("Connexion PostgreSQL impossible : 'psycopg' non installé.")
        return psycopg.connect(user=user, password=password, **conn_obj["params"])
    raise ValueError(f"Type de base non supporté : '{conn_obj['type']}'")


def cancel_query(conn, conn_obj, log=lambda m: None) -> None:
    try:
        if conn_obj["type"] == "postgresql":
            fn = getattr(conn, "cancel_safe", None) or getattr(conn, "cancel", None)
        else:
            fn = getattr(conn, "cancel", None)
        if fn is None:
            log(f"ATTENTION : aucune méthode cancel pour type='{conn_obj['type']}'.")
            return
        fn()
    except Exception as exc:
        log(f"ATTENTION : échec annulation ({conn_obj['type']}) : {exc}")


def _is_timeout_error(exc: Exception, conn_obj) -> bool:
    if conn_obj["type"] == "oracle":
        return "ORA-01013" in str(exc)
    if conn_obj["type"] == "postgresql":
        return exc.__class__.__name__ in ("QueryCanceled", "QueryCanceledError")
    return False


# ── Paramètres ────────────────────────────────────────────────────────────
def resolve_default_token(raw: str | None) -> str | None:
    if raw is None:
        return None
    s = raw.strip()
    if s == "TODAY":
        return datetime.now().strftime("%Y%m%d")
    if s == "YESTERDAY":
        return (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
    return s


def substitute_params(sql: str, values: dict[str, str]) -> str:
    if not values:
        return sql

    def repl(m: re.Match) -> str:
        name = m.group(1)
        if name in values:
            return values[name].replace("'", "''")
        return m.group(0)

    return _PLACEHOLDER_RE.sub(repl, sql)


# ── Post-processors (port fidèle) ─────────────────────────────────────────
def _col_to_numeric(df, col):
    import pandas as pd
    return pd.to_numeric(df[col], errors="coerce").fillna(0)


def postprocess_sum_total_doublons(df):
    import pandas as pd
    if df is None or df.empty:
        return df
    id_cols = list(df.columns[:2])
    num_cols = [c for c in df.columns if c not in id_cols]
    total_row = {id_cols[0]: "TOTAL_GENERAL"}
    if len(id_cols) > 1:
        total_row[id_cols[1]] = ""
    for c in num_cols:
        total_row[c] = int(_col_to_numeric(df, c).sum())
    return pd.concat([df, pd.DataFrame([total_row])], ignore_index=True)


def postprocess_sum_boite_prefixes_stock(df):
    import pandas as pd
    if df is None or df.empty:
        return df
    boite_col = df.columns[0]
    total_col = next((c for c in df.columns if str(c).strip().lower() == "total"), None)
    if total_col is None:
        return df
    total_num = _col_to_numeric(df, total_col)
    boites = df[boite_col].astype(str)
    rows = []
    for prefix in ("DSI", "GES", "VDC"):
        mask = boites.str.startswith(prefix)
        row = {c: "" for c in df.columns}
        row[boite_col] = f"TOTAL_{prefix}*"
        row[total_col] = int(total_num[mask].sum())
        rows.append(row)
    return pd.concat([df, pd.DataFrame(rows)], ignore_index=True)


POST_PROCESSORS = {
    "sum_total_doublons": postprocess_sum_total_doublons,
    "sum_boite_prefixes_stock": postprocess_sum_boite_prefixes_stock,
    # pivot_prestations_par_offre : dépend de prestations_par_offre_lib (Excel
    # template avancé) — non câblé dans le module web v1, ignoré proprement.
}


# ── Exécution d'une requête ───────────────────────────────────────────────
def execute_query(conn_obj, user, password, sheet_name, query,
                  timeout, log=lambda m: None):
    import pandas as pd
    t0 = time.time()
    log(f"DÉBUT   {sheet_name}")
    try:
        conn = connect(conn_obj, user, password)
        cursor = conn.cursor()
        timer = threading.Timer(timeout, lambda: cancel_query(conn, conn_obj, log))
        timer.start()
        try:
            cursor.execute(query)
            cols = [c[0] for c in cursor.description] if cursor.description else []
            rows = cursor.fetchall()
        finally:
            timer.cancel()
            cursor.close()
        conn.close()
        df = pd.DataFrame(rows, columns=cols)
        msg = f"OK      {sheet_name} -> {len(df)} lignes en {time.time()-t0:.1f}s"
        log(msg)
        return sheet_name, df, "ok", msg
    except Exception as exc:
        if _is_timeout_error(exc, conn_obj):
            msg = f"TIMEOUT {sheet_name} ({timeout}s) - requête annulée"
        else:
            msg = f"ERREUR  {sheet_name} : {exc}"
        log(msg)
        return sheet_name, None, "error", msg


def test_connection(conn_obj, user, password) -> tuple[bool, str]:
    """Teste une connexion. Retourne (ok, message)."""
    try:
        conn = connect(conn_obj, user, password)
        conn.close()
        return True, "OK"
    except Exception as exc:
        return False, str(exc)


def run_queries(selected: dict, environments: dict, query_envs: dict,
                creds_resolver, dates: dict, param_values: dict,
                output_dir: Path, timeout: int, emit) -> list[Path]:
    """Exécute les requêtes sélectionnées en parallèle (environnement choisi
    PAR REQUÊTE) et exporte un xlsx par catégorie. `emit` pousse les SSE.

    `selected`      : { sheet_name: entry } (entries de query_loader).
    `environments`  : { env_name: { db_key: info } } (toutes les configs).
    `query_envs`    : { sheet_name: env_name } — cible d'exécution par requête.
    `creds_resolver`: callable(env_name, db_key) -> (user, password).
    Retourne la liste des fichiers écrits.
    """
    import pandas as pd

    def log(m):
        emit({"type": "log", "level": "info", "text": m,
              "ts": datetime.now().strftime("%H:%M:%S")})

    # Cache des paramètres de connexion par (env, db_key).
    conn_params_cache: dict = {}

    def conn_params(env_name, db_key):
        key = (env_name, db_key)
        if key not in conn_params_cache:
            info = environments.get(env_name, {}).get(db_key)
            conn_params_cache[key] = make_conn_params(db_key, info) if info else None
        return conn_params_cache[key]

    output_dir.mkdir(parents=True, exist_ok=True)
    cats = sorted({e["category"] for e in selected.values()})
    heure = datetime.now().strftime("%H%M%S")
    output_files = {c: output_dir / f"{c}_{heure}_Resultats_SQL.xlsx" for c in cats}

    results: dict = {}
    total = len(selected)
    done = 0
    emit({"type": "progress", "pct": 0})

    with ThreadPoolExecutor(max_workers=min(total, 4)) as pool:
        futures = {}
        for sheet_name, entry in selected.items():
            date_req = dates.get(entry["freq"], "")
            q = entry["sql"].replace("{DATE_SUIVI}", date_req)
            q = substitute_params(q, param_values)
            db_key = entry["db"]
            env_name = query_envs.get(sheet_name) or entry.get("env")
            params = conn_params(env_name, db_key)
            if params is None:
                emit({"type": "query", "name": sheet_name, "status": "error",
                      "msg": f"Base '{db_key}' non déclarée pour l'environnement {env_name}"})
                done += 1
                emit({"type": "progress", "pct": int(done / total * 100)})
                continue
            u, pw = creds_resolver(env_name, db_key)
            emit({"type": "query", "name": sheet_name, "status": "running",
                  "env": env_name})
            fut = pool.submit(execute_query, params,
                              u, pw, sheet_name, q, timeout, log)
            futures[fut] = sheet_name

        for fut in as_completed(futures):
            sheet_name, df, status, msg = fut.result()
            if df is not None:
                pp_key = selected[sheet_name].get("post_process")
                pp_fn = POST_PROCESSORS.get(pp_key) if pp_key else None
                if pp_fn is not None:
                    try:
                        df = pp_fn(df)
                        log(f"POST-TRT {sheet_name} : {pp_key} appliqué")
                    except Exception as exc:
                        log(f"POST-TRT {sheet_name} : ÉCHEC ({exc})")
                results[sheet_name] = df
            done += 1
            emit({"type": "query", "name": sheet_name, "status": status, "msg": msg})
            emit({"type": "progress", "pct": int(done / total * 100)})

    written: list[Path] = []
    for cat in cats:
        sheets = [s for s in selected if selected[s]["category"] == cat and s in results]
        if not sheets:
            continue
        path = output_files[cat]
        with pd.ExcelWriter(path, engine="openpyxl") as writer:
            for s in sheets:
                results[s].to_excel(writer, sheet_name=s[:31], index=False)
        written.append(path)
        emit({"type": "log", "level": "ok",
              "text": f"Fichier écrit : {path.name}",
              "ts": datetime.now().strftime("%H:%M:%S")})

    return written
