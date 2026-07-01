import pandas as pd
import os
import sys
import csv
import re
from pathlib import Path
from datetime import datetime
import numpy as np
import warnings

# Pour la connexion BDD (Optionnel selon environnement)
try:
    import psycopg
except ImportError:
    pass

# --- CONFIGURATION ---
SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = SCRIPT_DIR if (SCRIPT_DIR / "Input_Data").exists() else SCRIPT_DIR.parent
INPUT_DIR = BASE_DIR / "Input_Data"
OUTPUT_DIR = BASE_DIR / "Output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Config BDD (IEHE)
PG_HOST = "bdd-X0ED0550.alias"
PG_PORT = 5559
PG_DB = "choregie_db"
PG_USER     = os.environ.get("PG_USER", "")
PG_PASSWORD = os.environ.get("PG_PASSWORD", "")
IEHE_SCHEMA = "iehe"
IEHE_TABLE  = "refkpep"
IEHE_COL_ID_TABLE = "refperboccn"

# SEUIL DE DECOUPAGE DES REQUETES
BATCH_SIZE = 20000        # Email / KPEP
BATCH_SIZE_COMPLEX = 800  # Last Name / Middle Name

# --- UTILITAIRES ---
def connect_pg(host, port, db):
    try:
        return psycopg.connect(host=host, port=port, dbname=db, user=PG_USER, password=PG_PASSWORD, connect_timeout=3)
    except:
        return None

def connect_iehe_auto():
    hosts = ["bdd-X0ED0550.alias", "100.54.41.6"]
    ports = [5559, 5432]
    dbs = ["choregie_db", "postgres"]
    
    for h in hosts:
        for p in ports:
            for d in dbs:
                try: 
                    conn = connect_pg(h, p, d)
                    if conn: return conn
                except: continue
    return None

def clean_col_name(col):
    """Nettoyage uniquement des noms de colonnes (Headers)"""
    if not isinstance(col, str): return str(col)
    col = col.replace('\ufeff', '').replace('\u200b', '') # Suppression BOM
    col = col.lower().strip()
    return col

def load_csv_robust(path):
    if not path.exists(): return None
    read_params = {'sep': None, 'engine': 'python', 'dtype': str}
    attempts = [
        {'encoding': 'utf-8-sig', 'skiprows': 0}, {'encoding': 'utf-8', 'skiprows': 0},
        {'encoding': 'latin1', 'skiprows': 0}, {'encoding': 'utf-8-sig', 'skiprows': 2},
        {'encoding': 'latin1', 'skiprows': 2}, {'encoding': 'cp1252', 'skiprows': 0}
    ]
    for params in attempts:
        current_params = read_params.copy()
        current_params.update(params)
        try:
            # keep_default_na=False pour éviter de convertir "NA" ou "null" en NaN pandas automatiquement
            df = pd.read_csv(path, keep_default_na=False, **current_params)
            df.columns = [clean_col_name(c) for c in df.columns]
            cols = df.columns
            keywords = ['adhesion', 'assure', 'email', 'personne', 'kpep', 'realm', 'date', 'nom']
            if any(k in c for c in cols for k in keywords):
                return df
        except: continue
    return None

def get_col_name(df, candidates):
    if df is None: return None
    for col in candidates:
        if col in df.columns: return col
    return None

def find_latest_new_s(directory):
    if not directory.exists():
        return None, None
    candidates = list(directory.glob("*_New_S.csv"))
    if not candidates:
        return None, None

    best = None
    best_dt = None
    for f in candidates:
        prefix = f.name.split("_")[0]
        if re.fullmatch(r"\d{8}", prefix):
            try:
                dt = datetime.strptime(prefix, "%d%m%Y")
            except Exception:
                dt = None
        else:
            dt = None
        if dt is None:
            dt = datetime.fromtimestamp(f.stat().st_mtime)
        if best_dt is None or dt > best_dt:
            best_dt = dt
            best = f

    if best is None:
        return None, None
    prefix = best.name.split("_")[0] if "_" in best.name else "UNKNOWN"
    return best, prefix

# --- ETAPE 1 : GENERATION IEHE ---

def run_iehe_step(df_ns, output_iehe_path):
    """
    Étape IEHE : récupère depuis iehe.refkpep les enregistrements correspondant
    aux AI IDs présents dans New_S, en enrichissant chaque ligne avec la valeur
    KPEP associée (colonne kpep_iehe).

    Requête enrichie (vs version précédente) :
    - Un CTE kpep_iehe_map extrait le KPEP (refperboccn LIKE 'KPEP%') associé
      à chaque idrpp via DISTINCT ON → 1 KPEP représentant par idrpp.
    - LEFT JOIN sur r1 : les AI IDs sans KPEP associé restent dans l'export
      (kpep_iehe = NULL), garantissant l'exhaustivité de la population IEHE.
    - Le fichier CSV exporté contient toutes les colonnes r1.* PLUS kpep_iehe,
      ce qui permet au Script 02 (indicateur E2) de comparer directement
      KPEP NS = KPEP CIAM = KPEP IEHE sans requête supplémentaire.
    """
    print(f"   🔨 Génération IEHE ({output_iehe_path.name})...")
    if output_iehe_path.exists():
        print(f"      ✅ Fichier déjà présent.")
        return
    if 'psycopg' not in sys.modules:
        print("      ⚠️  Module 'psycopg' manquant. Saut de l'étape IEHE.")
        return
    col_target = get_col_name(df_ns, ['num_personne', 'numpersonne', 'num_pers', 'id_personne', 'idkpep'])
    if not col_target: return

    ids = df_ns[col_target].unique().tolist()
    ids = [str(i).strip() for i in ids if i and str(i).strip() != '']
    if not ids: return

    conn = connect_iehe_auto()
    if not conn: return

    safe_ids = [i.replace("'", "") for i in ids]

    # Requête enrichie :
    #   - kpep_iehe_map : 1 KPEP représentant par idrpp (DISTINCT ON)
    #   - LEFT JOIN     : conserve les AI IDs sans KPEP (kpep_iehe = NULL)
    #   - LIKE 'KPEP%%' : %% obligatoire (f-string + paramètre psycopg)
    sql = f"""
    WITH input_ids AS (
        SELECT unnest(%(vals)s::text[]) AS v
    ),
    kpep_iehe_map AS (
        -- 1 KPEP représentant par idrpp (alphabétique si multiples)
        SELECT DISTINCT ON (idrpp)
               idrpp,
               refperboccn AS kpep_iehe
        FROM   {IEHE_SCHEMA}.{IEHE_TABLE}
        WHERE  refperboccn LIKE 'KPEP%%'
        ORDER  BY idrpp, refperboccn
    )
    SELECT DISTINCT r1.*, km.kpep_iehe
    FROM   {IEHE_SCHEMA}.{IEHE_TABLE} r1
    JOIN   {IEHE_SCHEMA}.{IEHE_TABLE} r2 ON r2.idrpp = r1.idrpp
    JOIN   input_ids                       ON input_ids.v = r1.{IEHE_COL_ID_TABLE}
    LEFT JOIN kpep_iehe_map km             ON km.idrpp = r1.idrpp
    """
    try:
        with conn.cursor() as cur:
            cur.execute(sql, {"vals": safe_ids})
            rows = cur.fetchall()
            cols = [d.name for d in cur.description]
        conn.close()
        n_avec_kpep = sum(1 for r in rows if r[-1] is not None)
        print(f"      ✅ IEHE exporté : {len(rows)} lignes | {n_avec_kpep} avec kpep_iehe renseigné.")
        with open(output_iehe_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(cols)
            writer.writerows(rows)
    except Exception as e:
        print(f"      ❌ Erreur SQL : {e}")

# --- ETAPE 2 : GENERATION SQL (AVEC BATCHING) ---

def load_concat_for_filter(input_dir, pattern):
    """Charge et fusionne les résultats existants (CM/CK) pour filtrage."""
    files = sorted(input_dir.glob(pattern))
    data_set = set()
    for f in files:
        try:
            d = pd.read_csv(f, engine='python', dtype=str)
            c_mail = get_col_name(d, ['email', 'cm_email'])
            if c_mail:
                vals = d[c_mail].str.replace('"', '', regex=False).str.lower().str.strip().dropna()
                data_set.update(vals[vals != ''].tolist())
            c_kpep = get_col_name(d, ['idkpep', 'ck_kpep', 'kpep'])
            if c_kpep:
                vals = d[c_kpep].str.replace('"', '', regex=False).str.strip().dropna()
                data_set.update(vals[vals != ''].tolist())
        except: pass
    return data_set


def write_sql_batches(tasks, input_dir, output_dir, prefix):
    """Écrit les fichiers SQL par batch pour une liste de tâches."""
    today_str = datetime.now().strftime("%Y-%m-%d")

    for tpl_name, output_suffix, data_list, data_type in tasks:
        tpl_path = input_dir / tpl_name
        if not data_list:
            print(f"      ⚠️  Liste vide pour '{output_suffix}' — aucun fichier SQL généré.")
            continue
        if not tpl_path.exists():
            print(f"      ⚠️  Template absent : {tpl_name}")
            continue

        try:
            with open(tpl_path, 'r', encoding='utf-8') as f: base_sql_template = f.read()
        except Exception as e:
            print(f"      ❌ Erreur lecture template {tpl_name} : {e}")
            continue

        total_items = len(data_list)
        chunk_size = BATCH_SIZE_COMPLEX if "complex" in data_type else BATCH_SIZE
        for i in range(0, total_items, chunk_size):
            chunk = data_list[i : i + chunk_size]
            batch_index = (i // chunk_size) + 1
            out_name = f"{prefix}_REQ_{output_suffix}_Part{batch_index}.sql"
            print(f"      write -> {out_name} ({len(chunk)} items)")

            try:
                base_sql = base_sql_template

                if "complex" in data_type:
                    target_field_distinct = "last_name" if "lastname" in data_type else "middleName"
                    if "DISTINCT ON (birthDate)" in base_sql:
                        base_sql = base_sql.replace("DISTINCT ON (birthDate)", f"DISTINCT ON (first_name, {target_field_distinct}, birthDate)")
                    if "ORDER BY birthDate" in base_sql:
                        base_sql = base_sql.replace("ORDER BY birthDate", f"ORDER BY first_name, {target_field_distinct}, birthDate")

                if "simple_email" == data_type:
                    base_sql = base_sql.replace("usr.email IN", "LOWER(usr.email) IN").replace("usr.email =", "LOWER(usr.email) =")

                values_str = ""
                if "simple" in data_type:
                    values_str = "'" + "','".join(chunk) + "'"
                elif "complex" in data_type:
                    is_last = "lastname" in data_type
                    target_col = "usr.last_name" if is_last else "attmiddle.value"
                    conditions = []
                    if data_type.endswith("_date"):
                        for name_val, dt in chunk:
                            conditions.append(f"({target_col} ILIKE '{name_val}' AND att2.value = '{dt}')")
                    elif data_type.endswith("_firstname"):
                        for name_val, first_val in chunk:
                            conditions.append(f"({target_col} ILIKE '{name_val}' AND usr.first_name ILIKE '{first_val}')")
                    values_str = "\n      OR ".join(conditions)

                if "__LISTE_IDS__" in base_sql:
                    final_sql = base_sql.replace("__LISTE_IDS__", values_str)
                    final_sql = final_sql.replace("2025-11-30", today_str)
                    header = f"/* BATCH {batch_index} | GENERATED {datetime.now()} | SOURCE: {prefix} | NB: {len(chunk)} */\n"
                    with open(output_dir / out_name, 'w', encoding='utf-8') as f_out:
                        f_out.write(header + final_sql)
                else:
                    print(f"      ❌ Erreur Template {tpl_name} : Balise __LISTE_IDS__ introuvable.")
            except Exception as e:
                print(f"      ❌ Erreur génération lot {batch_index} pour {output_suffix} : {e}")


def run_sql_step(df, input_dir, output_dir, prefix):
    """Génération SQL en 2 phases avec une seule pause interactive.

    Phase 1 : CM (email) + CK (KPEP) générés depuis l'ensemble du parc New_S → pause unique
    Phase 2 : reliquat df_ciam non présent en CM ni en CK → requêtes Last/Middle
    """
    print(f"   🔨 Génération Requêtes SQL (Batching max {BATCH_SIZE} lignes)...")

    # --- Préparation commune ---
    # df_ciam = périmètre CIAM (hors conjoints), utilisé UNIQUEMENT pour le reliquat Last/Middle
    df_ciam = df.copy()
    col_type = get_col_name(df_ciam, ['type_assure', 'typeassure', 'code_role_personne', 'role', 'type'])
    if col_type:
        col_series = df_ciam[col_type].astype(str).str.upper().str.strip()
        mask_conjoint = col_series.str.contains("CONJ", na=False)
        df_ciam = df_ciam[~mask_conjoint]

    # Identification des colonnes (noms identiques dans df et df_ciam)
    col_mail = get_col_name(df_ciam, ['mailciam', 'mail_ciam', 'mail ciam', 'email_ciam', 'email', 'mail'])
    col_val  = get_col_name(df_ciam, ['valeur_coordonnee', 'valeur coordonnee', 'valeur_coordonnées', 'mail', 'email'])
    col_kpep = get_col_name(df_ciam, ['idkpep', 'kpep', 'id_kpep', 'id kpep', 'code_kpep'])
    col_nom  = get_col_name(df_ciam, ['nom_long', 'nom', 'lastname', 'last_name', 'nom_assure', 'nom_famille'])
    col_dnaiss = get_col_name(df_ciam, ['date_naissance', 'datenaissance', 'birthdate', 'birth_date', 'ddn'])
    col_prenom = get_col_name(df_ciam, ['prenom', 'prénom', 'first_name', 'firstname', 'given_name'])
    col_middle = get_col_name(df_ciam, ['middlename', 'middle_name', 'middle name', 'second_prenom'])
    col_middle_effective = col_middle if col_middle else col_nom

    def safe_sql_str(series):
        return series.fillna('').astype(str).str.strip()

    print(f"\n      📊 Population CIAM (hors conjoints) : {len(df_ciam)} / {len(df)} total New_S")

    # =========================================================
    # PHASE 1 : Génération des requêtes CM (EMAIL) + CK (KPEP)
    #           Source : ensemble du parc New_S (df complet)
    # =========================================================
    print("\n   ========================================================")
    print("   📧🔑 PHASE 1 : Génération des requêtes CM (Email) + CK (KPEP)")
    print("            Source : New_S hors conjoints")
    print("   ========================================================")

    # CM — emails depuis New_S hors conjoints
    email_list = []
    if col_mail:
        email_list.extend(df_ciam[col_mail].dropna().tolist())
    else:
        print(f"      ⚠️  Colonne email (mailciam) introuvable dans New_S.")
    if col_val:
        email_list.extend(df_ciam[col_val].dropna().tolist())
    email_list = list(set([str(e).lower().strip() for e in email_list if '@' in str(e)]))
    print(f"      🔹 Emails uniques à rechercher : {len(email_list)}")

    cm_tasks = [("00-Export_CIAM_EMAIL_With_Distinct.sql", "00-Export_CIAM_EMAIL_Global", email_list, "simple_email")]
    write_sql_batches(cm_tasks, input_dir, output_dir, prefix)
    if email_list:
        print(f"      ✅ Requêtes CM générées.")

    # CK — KPEP depuis New_S hors conjoints
    kpep_list = []
    if col_kpep:
        kpep_series = df_ciam[col_kpep].fillna("").astype(str).str.strip()
        kpep_list = kpep_series[kpep_series != ""].unique().tolist()
    else:
        print(f"      ⚠️  Colonne KPEP (idkpep) introuvable dans New_S.")
    print(f"      🔹 KPEP uniques à rechercher : {len(kpep_list)}")

    ck_tasks = [("00-Export_CIAM_KPEP_With_Distinct.sql", "00-Export_CIAM_KPEP_Global", kpep_list, "simple")]
    write_sql_batches(ck_tasks, input_dir, output_dir, prefix)
    if kpep_list:
        print(f"      ✅ Requêtes CK générées.")

    # --- PAUSE UNIQUE : Attente des résultats CM + CK ---
    print("\n   ========================================================")
    print("   ⏸️  ACTION MANUELLE REQUISE : CM (Email) + CK (KPEP)")
    print("   ========================================================")
    print(f"   1. Récupérez les requêtes CM et CK dans '{output_dir}'")
    print(f"   2. Exécutez-les sur la BDD CIAM")
    print(f"   3. Enregistrez les résultats dans '{input_dir}' :")
    print(f"        {prefix}_CM.csv")
    print(f"        {prefix}_CK.csv")
    input("\n   Appuyez sur Entrée une fois les fichiers CM et CK déposés...")

    # =========================================================
    # PHASE 2 : Reliquat (non présents en CM ni en CK) → Last/Middle
    #           Périmètre : df_ciam (hors conjoints)
    # =========================================================
    print("\n   ========================================================")
    print("   📝 PHASE 2 : Calcul du reliquat → Requêtes Last/Middle")
    print("   ========================================================")

    # Chargement des valeurs trouvées dans CM et CK
    already_found_cm = load_concat_for_filter(input_dir, f"{prefix}*CM*.csv")
    already_found_ck = load_concat_for_filter(input_dir, f"{prefix}*CK*.csv")
    print(f"      🔹 Valeurs chargées depuis CM : {len(already_found_cm)}")
    print(f"      🔹 Valeurs chargées depuis CK : {len(already_found_ck)}")

    # Masque combiné sur df_ciam : présent en CM (par email) OU en CK (par KPEP)
    mask_found = pd.Series(False, index=df_ciam.index)
    if col_mail:
        key_mail = df_ciam[col_mail].astype(str).str.replace('"', '', regex=False).str.lower().str.strip()
        mask_found |= key_mail.isin(already_found_cm)
    if col_val:
        key_val = df_ciam[col_val].astype(str).str.replace('"', '', regex=False).str.lower().str.strip()
        mask_found |= key_val.isin(already_found_cm)
    if col_kpep:
        key_kpep = df_ciam[col_kpep].astype(str).str.replace('"', '', regex=False).str.strip()
        mask_found |= key_kpep.isin(already_found_ck)

    df_reliquat = df_ciam[~mask_found].copy()

    print(f"      🔹 Présents en CM ou CK : {mask_found.sum()} / {len(df_ciam)}")
    print(f"      🔹 Reliquat Last/Middle  : {len(df_reliquat)} lignes")

    # Préparation des colonnes formatées pour les requêtes complexes
    df_reliquat['last_fmt']   = safe_sql_str(df_reliquat[col_nom])              if col_nom              else ""
    df_reliquat['first_fmt']  = safe_sql_str(df_reliquat[col_prenom])           if col_prenom           else ""
    df_reliquat['middle_fmt'] = safe_sql_str(df_reliquat[col_middle_effective]) if col_middle_effective else ""

    if col_dnaiss:
        raw_dates = safe_sql_str(df_reliquat[col_dnaiss])
        dt_parsed = pd.to_datetime(raw_dates, format='%Y-%m-%d', errors='coerce')
        mask_fail = dt_parsed.isna() & (raw_dates != "")
        if mask_fail.any():
            dt_parsed.loc[mask_fail] = pd.to_datetime(raw_dates.loc[mask_fail], dayfirst=True, errors='coerce')
        df_reliquat['dt_fmt'] = raw_dates
        mask_ok = dt_parsed.notna()
        df_reliquat.loc[mask_ok, 'dt_fmt'] = dt_parsed.loc[mask_ok].dt.strftime('%Y-%m-%d')
    else:
        df_reliquat['dt_fmt'] = ""

    # Échappement des apostrophes pour l'insertion SQL
    for col in ['last_fmt', 'first_fmt', 'middle_fmt', 'dt_fmt']:
        df_reliquat[col] = df_reliquat[col].str.replace("'", "''", regex=False)

    def build_list(df_src, col_a, col_b):
        """Construit une liste dédupliquée de tuples (val_a, val_b), en excluant les tuples avec valeur vide."""
        tuples = [(a, b) for a, b in zip(df_src[col_a], df_src[col_b]) if a and b]
        return sorted(list(set(tuples)))

    last_date_list   = build_list(df_reliquat, 'last_fmt',   'dt_fmt')
    last_first_list  = build_list(df_reliquat, 'last_fmt',   'first_fmt')
    middle_date_list = build_list(df_reliquat, 'middle_fmt', 'dt_fmt')
    middle_first_list = build_list(df_reliquat, 'middle_fmt', 'first_fmt')

    print(f"      🔹 Volumétrie SQL (Last/Middle) :")
    print(f"         - Last + Date     : {len(last_date_list)}")
    print(f"         - Last + Prénom   : {len(last_first_list)}")
    print(f"         - Middle + Date   : {len(middle_date_list)}")
    print(f"         - Middle + Prénom : {len(middle_first_list)}")

    complex_tasks = [
        ("00-Export_CIAM_LAST_NAME_With_Distinct.sql",   "01-Last_Name_Date",    last_date_list,    "complex_lastname_date"),
        ("00-Export_CIAM_LAST_NAME_With_Distinct.sql",   "01-Last_Name_Prenom",  last_first_list,   "complex_lastname_firstname"),
        ("00-Export_CIAM_MiddleName_With_Distinct.sql",  "01-Middle_Name_Date",  middle_date_list,  "complex_middlename_date"),
        ("00-Export_CIAM_MiddleName_With_Distinct.sql",  "01-Middle_Name_Prenom",middle_first_list, "complex_middlename_firstname"),
    ]
    write_sql_batches(complex_tasks, input_dir, output_dir, prefix)
    print(f"      ✅ Requêtes Last/Middle générées.")

    print(f"\n      ✅ Génération SQL terminée (Vérifiez le dossier Output).")

# --- MAIN ---

def main():
    print("=" * 56)
    print("   SCRIPT 01 — GENERATION DES REQUETES SQL CIAM")
    print("=" * 56)
    print(f"   Dossier de travail : {BASE_DIR}")
    print(f"   Input_Data         : {INPUT_DIR}")
    print(f"   Output             : {OUTPUT_DIR}")
    print()

    if not INPUT_DIR.exists():
        print(f"❌ Dossier Input_Data introuvable : {INPUT_DIR}")
        print("   Créez le dossier et déposez-y le fichier *_New_S.csv")
        input("\nAppuyez sur Entrée pour quitter...")
        return

    ns_path, prefix = find_latest_new_s(INPUT_DIR)
    if not ns_path:
        print(f"❌ Aucun fichier *_New_S.csv trouvé dans : {INPUT_DIR}")
        print("   Vérifiez que le fichier est présent et nommé DDMMYYYY_New_S.csv")
        input("\nAppuyez sur Entrée pour quitter...")
        return

    print(f"📂 Fichier New_S détecté : {ns_path.name}  (préfixe : {prefix})")
    df_ns = load_csv_robust(ns_path)
    if df_ns is None:
        print(f"❌ Impossible de lire le fichier : {ns_path.name}")
        print("   Vérifiez l'encodage, le séparateur et les en-têtes du CSV.")
        input("\nAppuyez sur Entrée pour quitter...")
        return

    print(f"   ✅ New_S chargé : {len(df_ns)} lignes | colonnes : {list(df_ns.columns)}")
    print()

    # 1. IEHE
    run_iehe_step(df_ns, INPUT_DIR / f"{prefix}_IEHE.csv")

    # 2. SQL : CM+CK → pause → reliquat → Last/Middle
    run_sql_step(df_ns, INPUT_DIR, OUTPUT_DIR, prefix)

    print("\n🏁 Etape 1 terminée.")
    input("\nAppuyez sur Entrée pour quitter...")

if __name__ == "__main__":
    main() 