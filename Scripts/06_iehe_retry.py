# -*- coding: utf-8 -*-
"""
06_iehe_retry.py — Re-vérifie les personnes non trouvées en IEHE (IEHE_KO)
                    à J+1, J+2, J+7 pour s'assurer de leur création.

Usage :
    python 06_iehe_retry.py              # traite TOUS les *_IEHE_KO.csv
    python 06_iehe_retry.py 17022026     # préfixe explicite (1 seul fichier)

Le script :
  1. Détecte tous les {prefix}_IEHE_KO.csv dans Output/
  2. Pour chaque fichier, re-interroge IEHE pour les lignes encore "KO"
  3. Met à jour le statut en "OK" si trouvé (+ mail_IEHE, KPEP_IEHE)
  4. Réécrit chaque fichier en place
"""

import sys
import os
import re
from pathlib import Path
from datetime import datetime

import pandas as pd

try:
    import psycopg
except ImportError:
    print("[ERREUR] Module 'psycopg' non installé. Installez-le avec : pip install psycopg[binary]")
    sys.exit(1)

# --- CONFIGURATION ---
SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = SCRIPT_DIR if (SCRIPT_DIR / "Output").exists() else SCRIPT_DIR.parent
OUTPUT_DIR = BASE_DIR / "Output"

# Config BDD (IEHE) — identique à 01_generation_donnees.py
PG_USER = os.environ.get("PG_USER", "u_lpillon")
PG_PASSWORD = os.environ.get("PG_PASSWORD", "T_Run_Asc_2025#")
IEHE_SCHEMA = "iehe"
IEHE_TABLE = "refkpep"
IEHE_COL_ID = "refperboccn"

BATCH_SIZE = 5000


# --- CONNEXION ---

def connect_pg(host, port, db):
    try:
        return psycopg.connect(
            host=host, port=port, dbname=db,
            user=PG_USER, password=PG_PASSWORD,
            connect_timeout=5,
        )
    except Exception:
        return None


def connect_iehe_auto():
    """Tente la connexion IEHE avec fallback (même logique que 01_generation_donnees.py)."""
    hosts = ["bdd-X0ED0550.alias", "100.54.41.6"]
    ports = [5559, 5432]
    dbs = ["choregie_db", "postgres"]

    for h in hosts:
        for p in ports:
            for d in dbs:
                conn = connect_pg(h, p, d)
                if conn:
                    print(f"[OK] Connecté à {h}:{p}/{d}")
                    return conn
    print("[ERREUR] Impossible de se connecter à la BDD IEHE.")
    sys.exit(1)


# --- REQUETE IEHE ---

def query_iehe_details(conn, num_personnes):
    """
    Interroge IEHE par num_personne.
    Retourne un dict : { num_personne: { "mail": ..., "kpep": ... } }
    """
    if not num_personnes:
        return {}

    result = {}
    for i in range(0, len(num_personnes), BATCH_SIZE):
        batch = num_personnes[i:i + BATCH_SIZE]
        sql = f"""
        WITH input_ids AS (
            SELECT unnest(%(vals)s::text[]) AS v
        ),
        matched AS (
            SELECT r1.{IEHE_COL_ID} AS num_personne,
                   r1.idrpp,
                   r1.adrmailctc AS mail
            FROM   {IEHE_SCHEMA}.{IEHE_TABLE} r1
            JOIN   input_ids ON input_ids.v = r1.{IEHE_COL_ID}
        ),
        kpep_map AS (
            SELECT DISTINCT ON (idrpp)
                   idrpp,
                   {IEHE_COL_ID} AS kpep
            FROM   {IEHE_SCHEMA}.{IEHE_TABLE}
            WHERE  {IEHE_COL_ID} LIKE 'KPEP%%'
            ORDER  BY idrpp, {IEHE_COL_ID}
        )
        SELECT DISTINCT ON (m.num_personne)
               m.num_personne,
               m.mail,
               k.kpep
        FROM   matched m
        LEFT JOIN kpep_map k ON k.idrpp = m.idrpp
        ORDER  BY m.num_personne
        """
        with conn.cursor() as cur:
            cur.execute(sql, {"vals": batch})
            for row in cur.fetchall():
                result[row[0]] = {"mail": row[1] or "", "kpep": row[2] or ""}

    return result


# --- DETECTION DES FICHIERS ---

def find_all_iehe_ko(output_dir):
    """Retourne tous les *_IEHE_KO.csv dans Output/, triés par date de préfixe."""
    candidates = list(output_dir.glob("*_IEHE_KO.csv"))
    if not candidates:
        return []

    def sort_key(f):
        prefix = f.name.split("_")[0]
        if re.fullmatch(r"\d{8}", prefix):
            try:
                return datetime.strptime(prefix, "%d%m%Y")
            except ValueError:
                pass
        return datetime.fromtimestamp(f.stat().st_mtime)

    return sorted(candidates, key=sort_key)


def detect_separator(filepath):
    """Détecte le séparateur CSV d'un fichier."""
    with open(filepath, "r", encoding="utf-8-sig") as f:
        first_line = f.readline()
    if "\t" in first_line:
        return "\t"
    if ";" in first_line:
        return ";"
    return ","


# --- TRAITEMENT D'UN FICHIER ---

def process_one_file(ko_path, conn):
    """Traite un fichier IEHE_KO : re-vérifie les KO, met à jour en place.

    Toute exception est capturée et loguée : un fichier défaillant n'arrête
    pas le traitement des fichiers suivants.
    """
    print(f"\n{'─'*50}")
    print(f"  Fichier : {ko_path.name}")
    print(f"{'─'*50}")

    try:
        df = pd.read_csv(ko_path, sep=None, engine="python", dtype=str, keep_default_na=False)

        # Identifier la colonne num_personne
        col_num = None
        for c in df.columns:
            if "num_personne" in c.lower():
                col_num = c
                break
        if not col_num:
            print(f"  [ERREUR] Colonne num_personne introuvable. Ignoré.")
            return 0, 0, 0

        # Filtrer les KO restants
        mask_ko = df["statut_retry"] == "KO"
        nb_ko = mask_ko.sum()

        if nb_ko == 0:
            nb_ok = (df["statut_retry"] == "OK").sum()
            print(f"  [INFO] Tous les {nb_ok} enregistrements sont déjà OK.")
            return 0, 0, nb_ok

        print(f"  [INFO] Encore en KO : {nb_ko} / {len(df)}")

        # Requête IEHE
        nums_to_check = df.loc[mask_ko, col_num].unique().tolist()
        nums_to_check = [n.strip() for n in nums_to_check if n.strip()]

        iehe_data = query_iehe_details(conn, nums_to_check)
        print(f"  [QUERY] → {len(iehe_data)}/{len(nums_to_check)} trouvés")

        # Mise à jour
        today_str = datetime.now().strftime("%d%m%Y")
        nb_resolved = 0

        for idx in df.index:
            if df.at[idx, "statut_retry"] != "KO":
                continue
            np_val = df.at[idx, col_num].strip()
            df.at[idx, "date_derniere_verif"] = today_str

            if np_val in iehe_data:
                df.at[idx, "statut_retry"] = "OK"
                df.at[idx, "mail_IEHE"] = iehe_data[np_val].get("mail", "")
                df.at[idx, "KPEP_IEHE"] = iehe_data[np_val].get("kpep", "")
                nb_resolved += 1

        # Réécriture en place
        sep = detect_separator(ko_path)
        df.to_csv(ko_path, index=False, sep=sep, encoding="utf-8-sig")

        nb_still_ko = (df["statut_retry"] == "KO").sum()
        nb_total_ok = (df["statut_retry"] == "OK").sum()

        print(f"  [BILAN] {ko_path.name} : {nb_resolved} résolus / {nb_total_ok} OK cumulés / {nb_still_ko} encore KO")

        return nb_resolved, nb_still_ko, nb_total_ok

    except Exception as e:
        print(f"  [ERREUR] Échec traitement {ko_path.name} : {type(e).__name__}: {e}")
        return 0, 0, 0


# --- MAIN ---

def main():
    print(f"\n{'='*60}")
    print(f"  06_iehe_retry.py — Retry IEHE (J+1 / J+2 / J+7)")
    print(f"{'='*60}")

    # 1. Déterminer les fichiers à traiter
    if len(sys.argv) > 1:
        prefix = sys.argv[1]
        ko_path = OUTPUT_DIR / f"{prefix}_IEHE_KO.csv"
        if not ko_path.exists():
            print(f"\n[ERREUR] Fichier introuvable : {ko_path}")
            sys.exit(1)
        files = [ko_path]
    else:
        files = find_all_iehe_ko(OUTPUT_DIR)

    if not files:
        print(f"\n[INFO] Aucun fichier *_IEHE_KO.csv trouvé dans {OUTPUT_DIR}.")
        print(f"       → Cas normal si l'étape 3 n'a pas encore été exécutée.")
        print(f"       → Depuis la mise à jour, l'étape 3 (03_generation_fichiers_detail.py)")
        print(f"         produit toujours un fichier IEHE_KO (même vide). Vérifiez sa sortie")
        print(f"         si ce message est inattendu.")
        sys.exit(0)

    print(f"\n[INFO] {len(files)} fichier(s) IEHE_KO détecté(s)")

    # 2. Connexion unique (réutilisée pour tous les fichiers)
    conn = connect_iehe_auto()

    # 3. Traitement de chaque fichier
    total_resolved = 0
    total_still_ko = 0
    total_ok = 0

    for ko_path in files:
        resolved, still_ko, ok = process_one_file(ko_path, conn)
        total_resolved += resolved
        total_still_ko += still_ko
        total_ok += ok

    conn.close()

    # 4. Résumé global
    print(f"\n{'='*60}")
    print(f"  RÉSUMÉ GLOBAL")
    print(f"{'='*60}")
    print(f"  Fichiers traités    : {len(files)}")
    print(f"  Résolus ce retry    : {total_resolved}")
    print(f"  Total OK (cumulé)   : {total_ok}")
    print(f"  Encore KO           : {total_still_ko}")
    print(f"  Date vérification   : {datetime.now().strftime('%d%m%Y')}")
    print()


if __name__ == "__main__":
    main()