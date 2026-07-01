import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import sqlalchemy
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.types import JSON, DateTime, Text
try:
    from sqlalchemy.dialects.postgresql import JSONB
except Exception:
    JSONB = JSON

# --- CONFIGURATION ---
SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = SCRIPT_DIR if (SCRIPT_DIR / "Input_Data").exists() else SCRIPT_DIR.parent
INPUT_DIR = BASE_DIR / "Input_Data"
OUTPUT_DIR = BASE_DIR / "Output"

PG_HOST = os.getenv("PG_HOST", "bdd-T0XX0052.alias")
PG_PORT = os.getenv("PG_PORT", "5577")
PG_DB = os.getenv("PG_DB", "supervisionpsc_db")
# Credentials hardcodes : NE PAS lire PG_USER/PG_PASSWORD depuis l'env.
# Le .bat ETL_vf2 exporte PG_USER/PG_PASSWORD pour la BDD IEHE (script 01),
# ces valeurs n'ont rien a voir avec la BDD d'historisation rptpsc et la
# collision provoquait des FATAL "password authentication failed".
PG_USER = "rptpsc"
PG_PASSWORD = "rptpsc_xx"
PG_SCHEMA = os.getenv("PG_SCHEMA", "rptpsc")
PG_INSERT_METHOD = os.getenv("PG_INSERT_METHOD", "executemany").lower()
PG_CHUNKSIZE = int(os.getenv("PG_CHUNKSIZE", "2000"))

# --- CONFORMITÉ SCHÉMA ---
# Colonnes ajoutées par les évolutions récentes des scripts 01/02/03.
# Structure : { "nom_table": [("nom_colonne", "TYPE_SQL"), ...] }
SCHEMA_CONFORMANCE: dict[str, list[tuple[str, str]]] = {
    "input_iehe": [
        ("kpep_iehe", "TEXT"),
    ],
    "output_new_s_ciam": [
        ("qualite_donnees", "TEXT"),
    ],
}

def get_engine():
    dsn = os.getenv("PG_DSN")
    if dsn:
        return create_engine(dsn, hide_parameters=True)
    url = f"postgresql+psycopg://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/{PG_DB}"
    return create_engine(url, hide_parameters=True)

def _quote_ident(name: str) -> str:
    safe = name.replace('"', '""')
    return f'"{safe}"'

def _sql_type_for_column(col_name: str, json_cols: set[str] | None = None):
    if col_name == "date_import":
        return "TIMESTAMP"
    if json_cols and col_name in json_cols:
        return "JSONB"
    return "TEXT"

def schema_exists(engine, schema: str) -> bool:
    q = text("SELECT 1 FROM information_schema.schemata WHERE schema_name = :schema LIMIT 1")
    try:
        with engine.connect() as conn:
            return conn.execute(q, {"schema": schema}).first() is not None
    except Exception:
        return False

def ensure_schema(engine, schema: str):
    """No-op : le schema est suppose pre-existant.

    Le schema cible (par defaut 'rptpsc') est cree manuellement par un
    DBA, une fois pour toutes par environnement, via le script
    Input_Data/SQL/DDL/01_create_schema_rptpsc_DBA.sql. Le compte BDD
    applicatif utilise par ce script n'a generalement PAS le droit
    CREATE SCHEMA - et n'en a pas besoin.

    Si le schema n'existe vraiment pas, les operations suivantes
    (CREATE TABLE / INSERT) leveront une erreur explicite a leur niveau.
    """
    return

def count_rows_for_flux(engine, schema: str, table_name: str, flux_id: str) -> int:
    sql = text(
        f"SELECT COUNT(*) FROM {_quote_ident(schema)}.{_quote_ident(table_name)} "
        "WHERE flux_id = :fid"
    )
    try:
        with engine.connect() as conn:
            val = conn.execute(sql, {"fid": flux_id}).scalar()
            return int(val or 0)
    except Exception:
        return -1

def compact_sqlalchemy_error(exc: Exception, limit: int = 700) -> str:
    """
    Evite d'afficher des requêtes SQL gigantesques dans le terminal.
    """
    base = str(exc).replace("\n", " ").strip()
    # Si SQLAlchemy injecte la requête complète, on tronque agressivement.
    if len(base) > limit:
        base = base[:limit] + " ...[truncated]"
    # Préfère le message du driver si disponible
    orig = getattr(exc, "orig", None)
    if orig:
        o = str(orig).replace("\n", " ").strip()
        if len(o) > limit:
            o = o[:limit] + " ...[truncated]"
        return f"{exc.__class__.__name__}: {o}"
    return f"{exc.__class__.__name__}: {base}"

def resolve_insert_strategy(insert_method: str) -> list[str]:
    mode = (insert_method or "executemany").lower()
    if mode == "multi":
        return ["multi", "executemany"]
    if mode == "auto":
        return ["multi", "executemany"]
    return ["executemany"]

def ensure_table_and_columns(df: pd.DataFrame, table_name: str, engine, schema: str, json_cols: set[str] | None = None):
    if df is None or df.empty:
        return
    ensure_schema(engine, schema)
    insp = inspect(engine)
    table_exists = insp.has_table(table_name, schema=schema)
    cols = list(df.columns)
    if not table_exists:
        col_defs = []
        for c in cols:
            col_defs.append(f"{_quote_ident(c)} {_sql_type_for_column(c, json_cols)}")
        ddl = f"CREATE TABLE IF NOT EXISTS {schema}.{_quote_ident(table_name)} ({', '.join(col_defs)})"
        with engine.connect() as conn:
            conn.execute(text(ddl))
            conn.commit()
        return

    existing = {c["name"] for c in insp.get_columns(table_name, schema=schema)}
    missing = [c for c in cols if c not in existing]
    if not missing:
        return
    with engine.connect() as conn:
        for c in missing:
            ddl = f"ALTER TABLE {schema}.{_quote_ident(table_name)} ADD COLUMN IF NOT EXISTS {_quote_ident(c)} {_sql_type_for_column(c, json_cols)}"
            conn.execute(text(ddl))
        conn.commit()

def check_and_fix_schema(engine, schema: str, dry_run: bool = False):
    """
    Vérifie et corrige la conformité du schéma BDD selon SCHEMA_CONFORMANCE.
    Pour chaque table/colonne manquante, exécute ALTER TABLE ADD COLUMN IF NOT EXISTS.
    En dry_run : affiche les DDL sans les exécuter.
    """
    print("\n🔍 Vérification conformité schéma BDD...")
    insp = inspect(engine)
    actions: list[str] = []

    for table_name, col_specs in SCHEMA_CONFORMANCE.items():
        if not insp.has_table(table_name, schema=schema):
            print(f"   ℹ️  Table '{table_name}' inexistante — sera créée au premier chargement.")
            continue
        existing = {c["name"] for c in insp.get_columns(table_name, schema=schema)}
        for col_name, col_type in col_specs:
            if col_name not in existing:
                ddl = (
                    f"ALTER TABLE {_quote_ident(schema)}.{_quote_ident(table_name)} "
                    f"ADD COLUMN IF NOT EXISTS {_quote_ident(col_name)} {col_type};"
                )
                actions.append((table_name, col_name, ddl))

    if not actions:
        print("   ✅ Schéma conforme — aucune modification nécessaire.")
        return

    for table_name, col_name, ddl in actions:
        if dry_run:
            print(f"   [DRY-RUN] DDL à exécuter : {ddl}")
        else:
            try:
                with engine.connect() as conn:
                    conn.execute(text(ddl))
                    conn.commit()
                print(f"   ✅ Colonne ajoutée : {table_name}.{col_name}")
            except Exception as exc:
                print(f"   ❌ Erreur DDL {table_name}.{col_name} : {compact_sqlalchemy_error(exc)}")


def clean_col_name(col):
    if not isinstance(col, str): return str(col)
    col = col.lower().strip().replace(" ", "_").replace("-", "_").replace(".", "")
    col = col.replace("é", "e").replace("è", "e").replace("'", "")
    return col

def _read_csv_with_sep(path: Path, sep: str | None):
    return pd.read_csv(path, sep=sep, engine="python", dtype=str, encoding="utf-8-sig")

def load_csv_robust(path):
    if not path.exists():
        return None
    try:
        df = _read_csv_with_sep(path, sep=None)
    except UnicodeDecodeError:
        try:
            df = pd.read_csv(path, sep=None, engine="python", dtype=str, encoding="latin-1")
        except Exception:
            return None
    except Exception:
        return None

    # Heuristique: si 1 seule colonne et présence de ';' dans l'en-tête, relire en ';'
    if df is not None and len(df.columns) == 1:
        header = df.columns[0]
        if isinstance(header, str) and ";" in header:
            try:
                df = _read_csv_with_sep(path, sep=";")
            except Exception:
                pass
        elif isinstance(header, str) and "," in header:
            try:
                df = _read_csv_with_sep(path, sep=",")
            except Exception:
                pass

    if df is None:
        return None
    df.columns = [clean_col_name(c) for c in df.columns]
    df = df.loc[:, ~df.columns.duplicated()]
    return df


def load_concat_csv_by_pattern(directory: Path, pattern: str):
    """Charge et concatène plusieurs CSV correspondant à un pattern (ex: '11122025_Rech_Nom*.csv')."""
    files = sorted(directory.glob(pattern))
    dfs = []
    source_rows = 0
    loaded_files = 0
    print(f"   [CHARGEMENT BDD] Scan pattern '{pattern}' -> {len(files)} fichiers.")
    for f in files:
        df = load_csv_robust(f)
        if df is None or df.empty:
            continue
        loaded_files += 1
        source_rows += len(df)
        print(f"      - {f.name}: {len(df)} lignes")
        dfs.append(df)
    if not dfs:
        print("      -> Aucun fichier exploitable.")
        return None, {"files_found": len(files), "files_loaded": 0, "source_rows": 0, "prepared_rows": 0}
    out = pd.concat(dfs, ignore_index=True)
    before_dedupe = len(out)
    out = out.loc[:, ~out.columns.duplicated()]
    # best-effort dedupe
    for key_cols in (['realm_id'], ['idkpep','realm_id'], ['email','realm_id']):
        if all(c in out.columns for c in key_cols):
            out = out.drop_duplicates(subset=key_cols, keep='first')
            break
    print(f"      -> Concaténé: {before_dedupe} lignes | Après dédoublonnage: {len(out)} lignes")
    return out, {
        "files_found": len(files),
        "files_loaded": loaded_files,
        "source_rows": source_rows,
        "prepared_rows": len(out),
    }

def upload_dataframe(
    df,
    table_name,
    engine,
    flux_id,
    schema,
    dry_run=False,
    source_rows=None,
    insert_method="executemany",
    chunksize=2000,
):
    metrics = {
        "table": table_name,
        "source_rows": int(source_rows or 0),
        "prepared_rows": 0,
        "deleted_rows": 0,
        "db_rows_flux": 0,
        "status": "SKIPPED",
        "error": "",
    }
    if df is None or df.empty:
        print(f"   ⚠️ {table_name} : aucune donnée à charger.")
        return metrics
    df = df.copy()
    if 'id' in df.columns: df.rename(columns={'id': 'id_csv'}, inplace=True)
    df['flux_id'] = flux_id
    df['date_import'] = datetime.now()
    metrics["prepared_rows"] = int(len(df))
    if metrics["source_rows"] == 0:
        metrics["source_rows"] = metrics["prepared_rows"]
    
    try:
        ensure_table_and_columns(df, table_name, engine, schema)
    except Exception as exc:
        metrics["status"] = "ERROR"
        metrics["error"] = compact_sqlalchemy_error(exc)
        print(f"   ❌ Erreur {table_name} (DDL) : {metrics['error']}")
        return metrics

    # Nettoyage préalable (Delete by flux_id)
    if dry_run:
        metrics["status"] = "DRY_RUN"
        print(
            f"   [DRY-RUN] {table_name} | Source={metrics['source_rows']} | "
            f"Préparées={metrics['prepared_rows']} | flux_id={flux_id}"
        )
        return metrics
    with engine.connect() as conn:
        try:
            res = conn.execute(
                text(
                    f"DELETE FROM {_quote_ident(schema)}.{_quote_ident(table_name)} "
                    "WHERE flux_id = :fid"
                ),
                {"fid": flux_id},
            )
            conn.commit()
            metrics["deleted_rows"] = int(res.rowcount or 0)
        except Exception:
            pass

    # Insertion robuste avec strategie configurable.
    strategies = resolve_insert_strategy(insert_method)
    last_error = ""
    inserted = False
    for idx, strategy in enumerate(strategies):
        method_arg = "multi" if strategy == "multi" else None
        strategy_chunksize = min(max(1, chunksize), 1000) if strategy == "multi" else max(1, chunksize)
        try:
            df.to_sql(
                table_name,
                engine,
                schema=schema,
                if_exists='append',
                index=False,
                chunksize=strategy_chunksize,
                method=method_arg,
            )
            db_rows = count_rows_for_flux(engine, schema, table_name, flux_id)
            metrics["db_rows_flux"] = db_rows
            metrics["status"] = "OK" if db_rows in (metrics["prepared_rows"], -1) else "WARN"
            ecart = "N/A" if db_rows < 0 else str(metrics["prepared_rows"] - db_rows)
            suffix = f" [{strategy}]" if len(strategies) > 1 else ""
            print(
                f"   ✅ {table_name}{suffix} | Source={metrics['source_rows']} | Préparées={metrics['prepared_rows']} | "
                f"Supprimées={metrics['deleted_rows']} | En base (flux)={db_rows if db_rows >= 0 else 'inconnu'} | "
                f"Ecart={ecart}"
            )
            inserted = True
            break
        except Exception as exc:
            last_error = compact_sqlalchemy_error(exc)
            if idx < len(strategies) - 1:
                next_strategy = strategies[idx + 1]
                print(
                    f"   ⚠️ {table_name} : échec mode {strategy}, "
                    f"fallback {next_strategy}. Détail: {last_error}"
                )
    if not inserted:
        metrics["status"] = "ERROR"
        metrics["error"] = last_error or "Erreur d'insertion inconnue"
        print(f"   ❌ Erreur {table_name} : {metrics['error']}")
    return metrics

def upload_kpi_json(json_path, table_name, engine, flux_id, schema, dry_run=False):
    metrics = {
        "table": table_name,
        "source_rows": 0,
        "prepared_rows": 0,
        "deleted_rows": 0,
        "db_rows_flux": 0,
        "status": "SKIPPED",
        "error": "",
    }
    if not json_path.exists():
        print(f"   ⚠️ {table_name} : fichier absent ({json_path.name}).")
        return metrics
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception:
        metrics["status"] = "ERROR"
        metrics["error"] = f"Lecture JSON impossible: {json_path.name}"
        print(f"   ❌ {table_name} : lecture JSON impossible ({json_path.name}).")
        return metrics
    df = pd.DataFrame([{"flux_id": flux_id, "date_import": datetime.now(), "payload": payload}])
    metrics["source_rows"] = 1
    metrics["prepared_rows"] = 1

    try:
        ensure_table_and_columns(df, table_name, engine, schema, json_cols={"payload"})
    except Exception as exc:
        metrics["status"] = "ERROR"
        metrics["error"] = compact_sqlalchemy_error(exc)
        print(f"   ❌ Erreur {table_name} (DDL) : {metrics['error']}")
        return metrics

    if dry_run:
        metrics["status"] = "DRY_RUN"
        print(f"   [DRY-RUN] {table_name} | Source=1 | Préparées=1 | flux_id={flux_id}")
        return metrics
    with engine.connect() as conn:
        try:
            res = conn.execute(
                text(
                    f"DELETE FROM {_quote_ident(schema)}.{_quote_ident(table_name)} "
                    "WHERE flux_id = :fid"
                ),
                {"fid": flux_id},
            )
            conn.commit()
            metrics["deleted_rows"] = int(res.rowcount or 0)
        except Exception:
            pass
    try:
        df.to_sql(
            table_name,
            engine,
            schema=schema,
            if_exists="append",
            index=False,
            dtype={"payload": JSONB},
        )
        db_rows = count_rows_for_flux(engine, schema, table_name, flux_id)
        metrics["db_rows_flux"] = db_rows
        metrics["status"] = "OK" if db_rows in (1, -1) else "WARN"
        print(
            f"   ✅ {table_name} | Source=1 | Préparées=1 | Supprimées={metrics['deleted_rows']} | "
            f"En base (flux)={db_rows if db_rows >= 0 else 'inconnu'}"
        )
    except Exception as e:
        metrics["status"] = "ERROR"
        metrics["error"] = compact_sqlalchemy_error(e)
        print(f"   ❌ Erreur {table_name} : {metrics['error']}")
    return metrics

def load_kpi5_rejets_anciennete_xlsx(prefix: str) -> pd.DataFrame | None:
    """
    Lit le xlsx hebdo des requetes S_KPI5-*.sql (produit par launch_SQL_query_V2.py)
    et construit un DataFrame plat utilisable par upload_dataframe :
    une ligne par (origine, boite de traitement) avec le rejet le plus vieux.

    Source : output/SQL/<prefix>/<HHMMSS>_Resultats_SQL.xlsx (preference) ou
    le dernier xlsx disponible sous output/SQL/. Renvoie None si rien trouve.
    """
    origines = ["NMASS", "LUMAS", "OXA", "TPCVM", "TPHOS", "TPVIA", "Autre"]

    sql_root = next(
        (p for p in (BASE_DIR / "output" / "SQL", BASE_DIR / "Output" / "SQL") if p.exists()),
        None,
    )
    if sql_root is None:
        return None

    dir_prefix = sql_root / prefix
    candidate_dirs: list[Path] = []
    if dir_prefix.is_dir():
        candidate_dirs.append(dir_prefix)
    candidate_dirs.extend(sorted(
        (p for p in sql_root.iterdir() if p.is_dir() and p != dir_prefix),
        key=lambda p: p.stat().st_mtime, reverse=True,
    ))

    # Priorite au xlsx KPI5 (output split par categorie), fallback sur
    # les anciens *_Resultats_SQL.xlsx mono-fichier.
    xlsx_path: Path | None = None
    for d in candidate_dirs:
        for pattern in ("KPI5*_Resultats_SQL.xlsx", "*_Resultats_SQL.xlsx"):
            xlsxs = sorted(d.glob(pattern),
                           key=lambda p: p.stat().st_mtime, reverse=True)
            if xlsxs:
                xlsx_path = xlsxs[0]
                break
        if xlsx_path is not None:
            break
    if xlsx_path is None:
        return None

    try:
        sheets = pd.read_excel(xlsx_path, sheet_name=None, engine="openpyxl")
    except Exception as exc:
        print(f"   ⚠️ KPI5 xlsx ({xlsx_path.name}) : lecture impossible ({exc}).")
        return None

    parent_name = xlsx_path.parent.name
    date_suivi = (
        f"{parent_name[:2]}/{parent_name[2:4]}/{parent_name[4:]}"
        if len(parent_name) == 8 and parent_name.isdigit() else ""
    )

    rows: list[dict] = []
    for ori in origines:
        target = f"KPI5-{ori}"[:31]
        matches = [k for k in sheets if k == target or k.startswith(target)]
        if not matches:
            continue
        df = sheets[matches[0]]
        if df is None or df.empty:
            continue
        cols = {str(c).upper(): c for c in df.columns}
        col_boite = cols.get("PSASBOITRT")
        col_age = cols.get("AGE_JOURS")
        col_codrej = cols.get("PSRJCODREJ")
        col_codori = cols.get("PSEPCODORI")
        if not (col_boite and col_age):
            continue
        sub = df[[col_boite, col_age] + ([col_codrej] if col_codrej else [])
                 + ([col_codori] if col_codori else [])].copy()
        sub[col_age] = pd.to_numeric(sub[col_age], errors="coerce")
        sub = sub.dropna(subset=[col_boite, col_age])
        if sub.empty:
            continue
        idx_max = sub.groupby(col_boite)[col_age].idxmax()
        for _, r in sub.loc[idx_max].iterrows():
            rows.append({
                "origine_groupe": ori,
                "psepcodori": str(r[col_codori]).strip() if col_codori and pd.notna(r[col_codori]) else ori,
                "psasboitrt": str(r[col_boite]).strip(),
                "age_jours_max": int(r[col_age]),
                "psrjcodrej": str(r[col_codrej]).strip() if col_codrej and pd.notna(r[col_codrej]) else "",
                "date_suivi": date_suivi,
                "source_xlsx": xlsx_path.name,
            })

    if not rows:
        return None
    return pd.DataFrame(rows)


def detect_latest_prefix(input_dir: Path, output_dir: Path):
    candidates = []
    for pattern in ["*_New_S.csv"]:
        for f in input_dir.glob(pattern):
            prefix = f.name.split("_")[0]
            if prefix.isdigit() and len(prefix) == 8:
                try:
                    dt = datetime.strptime(prefix, "%d%m%Y")
                except Exception:
                    dt = datetime.fromtimestamp(f.stat().st_mtime)
                candidates.append((dt, prefix))
    for pattern in ["*_Modele_clean.json", "*_NS_CIAM.csv", "*_NS_IEHE.csv"]:
        for f in output_dir.glob(pattern):
            prefix = f.name.split("_")[0]
            if prefix.isdigit() and len(prefix) == 8:
                try:
                    dt = datetime.strptime(prefix, "%d%m%Y")
                except Exception:
                    dt = datetime.fromtimestamp(f.stat().st_mtime)
                candidates.append((dt, prefix))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]

def main():
    parser = argparse.ArgumentParser(description="Chargement BDD des inputs/outputs CIAM")
    parser.add_argument("--prefix", help="Préfixe (DDMMYYYY). Si absent, détection auto.")
    parser.add_argument("--input-dir", default=str(INPUT_DIR))
    parser.add_argument("--output-dir", default=str(OUTPUT_DIR))
    parser.add_argument("--schema", default=PG_SCHEMA)
    parser.add_argument(
        "--insert-method",
        default=PG_INSERT_METHOD if PG_INSERT_METHOD in {"executemany", "multi", "auto"} else "executemany",
        choices=["executemany", "multi", "auto"],
        help="Mode d'insertion SQLAlchemy. executemany=recommande, multi=rapide, auto=multi puis fallback.",
    )
    parser.add_argument(
        "--chunksize",
        type=int,
        default=max(1, PG_CHUNKSIZE),
        help="Taille des batches pour l'insertion.",
    )
    parser.add_argument("--dry-run", action="store_true", help="N'écrit pas en base, affiche le plan.")
    parser.add_argument("--fix-schema", action="store_true", help="Vérifie et corrige la conformité du schéma BDD avant le chargement.")
    args = parser.parse_args()

    input_dir = Path(args.input_dir)
    output_dir = Path(args.output_dir)
    schema = args.schema
    prefix = args.prefix or detect_latest_prefix(input_dir, output_dir)

    if not prefix:
        print("❌ Préfixe introuvable.")
        return

    print(f"🚀 Chargement BDD pour : {prefix}")
    try:
        engine = get_engine()
    except Exception:
        return

    # Le schema est suppose pre-existant : aucune verification ni creation
    # n'est tentee ici (cf. ensure_schema = no-op). Si le schema est
    # genuinement absent, l'erreur remontera explicitement depuis
    # CREATE TABLE / INSERT ci-dessous.

    if args.fix_schema or args.dry_run:
        check_and_fix_schema(engine, schema, dry_run=args.dry_run)

    run_metrics = []

    # Inputs (Pas de découpage attendu pour New_S)
    df_new_s = load_csv_robust(input_dir / f"{prefix}_New_S.csv")
    run_metrics.append(
        upload_dataframe(
            df_new_s,
            "input_new_s",
            engine,
            prefix,
            schema,
            args.dry_run,
            source_rows=(0 if df_new_s is None else len(df_new_s)),
            insert_method=args.insert_method,
            chunksize=args.chunksize,
        )
    )
    
    df_iehe = load_csv_robust(input_dir / f"{prefix}_IEHE.csv")
    run_metrics.append(
        upload_dataframe(
            df_iehe,
            "input_iehe",
            engine,
            prefix,
            schema,
            args.dry_run,
            source_rows=(0 if df_iehe is None else len(df_iehe)),
            insert_method=args.insert_method,
            chunksize=args.chunksize,
        )
    )

    # MODIFIE : CM et CK supportent maintenant le batching (*CM*.csv, *CK*.csv)
    df_cm, m_cm = load_concat_csv_by_pattern(input_dir, f"{prefix}*CM*.csv")
    run_metrics.append(
        upload_dataframe(
            df_cm,
            "input_cm",
            engine,
            prefix,
            schema,
            args.dry_run,
            source_rows=m_cm["source_rows"],
            insert_method=args.insert_method,
            chunksize=args.chunksize,
        )
    )
    df_ck, m_ck = load_concat_csv_by_pattern(input_dir, f"{prefix}*CK*.csv")
    run_metrics.append(
        upload_dataframe(
            df_ck,
            "input_ck",
            engine,
            prefix,
            schema,
            args.dry_run,
            source_rows=m_ck["source_rows"],
            insert_method=args.insert_method,
            chunksize=args.chunksize,
        )
    )
    
    # Modification des patterns de recherche (Last / Middle)
    df_last, m_last = load_concat_csv_by_pattern(input_dir, f"{prefix}*Last*.csv")
    run_metrics.append(
        upload_dataframe(
            df_last,
            "input_rech_nom",
            engine,
            prefix,
            schema,
            args.dry_run,
            source_rows=m_last["source_rows"],
            insert_method=args.insert_method,
            chunksize=args.chunksize,
        )
    )
    df_middle, m_middle = load_concat_csv_by_pattern(input_dir, f"{prefix}*Middle*.csv")
    run_metrics.append(
        upload_dataframe(
            df_middle,
            "input_rech_middle",
            engine,
            prefix,
            schema,
            args.dry_run,
            source_rows=m_middle["source_rows"],
            insert_method=args.insert_method,
            chunksize=args.chunksize,
        )
    )
    
    # Outputs (Fichier final consolidé)
    df_ns_ciam = load_csv_robust(output_dir / f"{prefix}_NS_CIAM.csv")
    run_metrics.append(
        upload_dataframe(
            df_ns_ciam,
            "output_new_s_ciam",
            engine,
            prefix,
            schema,
            args.dry_run,
            source_rows=(0 if df_ns_ciam is None else len(df_ns_ciam)),
            insert_method=args.insert_method,
            chunksize=args.chunksize,
        )
    )
    df_ns_iehe = load_csv_robust(output_dir / f"{prefix}_NS_IEHE.csv")
    run_metrics.append(
        upload_dataframe(
            df_ns_iehe,
            "output_new_s_iehe",
            engine,
            prefix,
            schema,
            args.dry_run,
            source_rows=(0 if df_ns_iehe is None else len(df_ns_iehe)),
            insert_method=args.insert_method,
            chunksize=args.chunksize,
        )
    )
    df_detail = load_csv_robust(output_dir / "detail_matching_ciam.csv")
    run_metrics.append(
        upload_dataframe(
            df_detail,
            "output_detail_matching_ciam",
            engine,
            prefix,
            schema,
            args.dry_run,
            source_rows=(0 if df_detail is None else len(df_detail)),
            insert_method=args.insert_method,
            chunksize=args.chunksize,
        )
    )

    # IEHE_KO : personnes non trouvées en IEHE + statut des retries J+1/J+2/J+7
    # (écrit par 03 puis enrichi par 06_iehe_retry.py)
    df_iehe_ko = load_csv_robust(output_dir / f"{prefix}_IEHE_KO.csv")
    run_metrics.append(
        upload_dataframe(
            df_iehe_ko,
            "output_iehe_ko",
            engine,
            prefix,
            schema,
            args.dry_run,
            source_rows=(0 if df_iehe_ko is None else len(df_iehe_ko)),
            insert_method=args.insert_method,
            chunksize=args.chunksize,
        )
    )

    # Audit debug email : non-rapprochés CIAM dont l'email est pourtant
    # indexé côté CIAM (produit par 03 si DEBUG_MATCH_EMAIL=1)
    df_audit_email = load_csv_robust(output_dir / f"{prefix}_AUDIT_DEBUG_EMAIL.csv")
    run_metrics.append(
        upload_dataframe(
            df_audit_email,
            "output_audit_debug_email",
            engine,
            prefix,
            schema,
            args.dry_run,
            source_rows=(0 if df_audit_email is None else len(df_audit_email)),
            insert_method=args.insert_method,
            chunksize=args.chunksize,
        )
    )

    # --- Flux TP GED (script 07_controle_tp_ged.py) ---
    # Inputs (extraction brute GED + corrections manuelles de référence)
    df_tp_ged_in = load_csv_robust(input_dir / f"{prefix}_TP_GED.csv")
    run_metrics.append(
        upload_dataframe(
            df_tp_ged_in,
            "input_tp_ged",
            engine,
            prefix,
            schema,
            args.dry_run,
            source_rows=(0 if df_tp_ged_in is None else len(df_tp_ged_in)),
            insert_method=args.insert_method,
            chunksize=args.chunksize,
        )
    )
    # Le fichier de corrections est persistant (même flux_id=prefix pour traçabilité)
    df_tp_ged_corr = load_csv_robust(input_dir / "TP_GED_Corrections.csv")
    run_metrics.append(
        upload_dataframe(
            df_tp_ged_corr,
            "input_tp_ged_corrections",
            engine,
            prefix,
            schema,
            args.dry_run,
            source_rows=(0 if df_tp_ged_corr is None else len(df_tp_ged_corr)),
            insert_method=args.insert_method,
            chunksize=args.chunksize,
        )
    )

    # Output — détail journalier du flux TP GED (une ligne = un contrat éligible)
    df_tp_ged_detail = load_csv_robust(output_dir / f"{prefix}_TP_GED_Detail.csv")
    run_metrics.append(
        upload_dataframe(
            df_tp_ged_detail,
            "output_tp_ged_detail",
            engine,
            prefix,
            schema,
            args.dry_run,
            source_rows=(0 if df_tp_ged_detail is None else len(df_tp_ged_detail)),
            insert_method=args.insert_method,
            chunksize=args.chunksize,
        )
    )

    # Output — sous-ensemble NON_RAPPROCHE du detail TP GED
    df_tp_ged_ko = load_csv_robust(output_dir / f"{prefix}_TP_GED_KO.csv")
    run_metrics.append(
        upload_dataframe(
            df_tp_ged_ko,
            "output_tp_ged_ko",
            engine,
            prefix,
            schema,
            args.dry_run,
            source_rows=(0 if df_tp_ged_ko is None else len(df_tp_ged_ko)),
            insert_method=args.insert_method,
            chunksize=args.chunksize,
        )
    )

    # Output — synthèse hebdo KPI5 (un rejet le plus vieux par origine x boîte)
    df_kpi5_rejets = load_kpi5_rejets_anciennete_xlsx(prefix)
    run_metrics.append(
        upload_dataframe(
            df_kpi5_rejets,
            "output_kpi5_rejets_anciennete",
            engine,
            prefix,
            schema,
            args.dry_run,
            source_rows=(0 if df_kpi5_rejets is None else len(df_kpi5_rejets)),
            insert_method=args.insert_method,
            chunksize=args.chunksize,
        )
    )

    run_metrics.append(
        upload_kpi_json(output_dir / f"{prefix}_Modele_clean.json", "output_kpi_json", engine, prefix, schema, args.dry_run)
    )

    print("\n📊 Récapitulatif chargement")
    print("   Table                           | Source | Préparées | Supprimées | En base (flux) | Statut")
    print("   -----------------------------------------------------------------------------------------------")
    for m in run_metrics:
        print(
            f"   {m['table'][:30].ljust(30)} | "
            f"{str(m['source_rows']).rjust(6)} | "
            f"{str(m['prepared_rows']).rjust(9)} | "
            f"{str(m['deleted_rows']).rjust(9)} | "
            f"{str(m['db_rows_flux']).rjust(14)} | "
            f"{m['status']}"
        )
        if m.get("error"):
            print(f"      ↳ {m['error']}")
    
    print("🏁 Chargement terminé.")

if __name__ == "__main__":
    main()