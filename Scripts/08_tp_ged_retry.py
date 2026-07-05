# -*- coding: utf-8 -*-
"""
08_tp_ged_retry.py — Re-vérifie les cartes TP encore NON_RAPPROCHE (TP_GED_KO)
                       à J+1, J+2, J+7, pour détecter les rapprochements tardifs.

Usage :
    python 08_tp_ged_retry.py              # traite TOUS les *_TP_GED_KO.csv
    python 08_tp_ged_retry.py 17022026     # préfixe explicite (1 seul fichier)

Le script, pour chaque ligne encore en KO (statut_retry == "KO") :
  1. Si le KPEP de référence est absent (motif "KPEP IEHE absent"), re-interroge
     IEHE (BDD live) par num_personne pour tenter de le résoudre — même logique
     de résolution (match société puis fallback) que 07_controle_tp_ged.py.
  2. Pour toute ligne disposant désormais d'un KPEP de référence, revérifie sa
     présence dans l'extraction GED la plus récente (Input_Data/*_TP_GED.csv).
  3. Réapplique les corrections manuelles (Input_Data/TP_GED_Corrections.csv)
     et met à jour present_ged / statut_rapprochement / statut_final / motif.
  4. Réécrit chaque {prefix}_TP_GED_KO.csv en place : statut_retry passe à
     "OK" pour les lignes désormais RAPPROCHE (les lignes sont conservées,
     pas supprimées, pour garder la piste d'audit — même principe que
     06_iehe_retry.py pour les IEHE_KO).
"""

from __future__ import annotations

import sys
import os
import re
import importlib.util
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
INPUT_DIR = BASE_DIR / "Input_Data"
OUTPUT_DIR = BASE_DIR / "Output"

# Config BDD (IEHE) — identique à 01_generation_donnees.py / 06_iehe_retry.py
PG_USER = os.environ.get("PG_USER", "u_lpillon")
PG_PASSWORD = os.environ.get("PG_PASSWORD", "T_Run_Asc_2025#")
IEHE_SCHEMA = "iehe"
IEHE_TABLE = "refkpep"
IEHE_COL_ID = "refperboccn"

BATCH_SIZE = 5000

# --- Réutilisation de la logique de 07_controle_tp_ged.py (get_col, load_csv,
#     load_ged_kpep_set, load_corrections, apply_correction, resolve_kpep_ref).
#     Chargement par chemin de fichier car le nom du module commence par un
#     chiffre (non importable via `import 07_...`).
_spec = importlib.util.spec_from_file_location(
    "controle_tp_ged", SCRIPT_DIR / "07_controle_tp_ged.py"
)
ctrl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ctrl)


# --- CONNEXION (identique à 06_iehe_retry.py) ---

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


# --- REQUÊTE IEHE LIVE (num_personne -> KPEP par société) ---

def build_live_iehe_index(conn, num_personnes):
    """
    Interroge IEHE en direct par num_personne et retourne les mêmes structures
    que ctrl.build_iehe_kpep_index (exact_idx, fallback_idx), pour pouvoir
    réutiliser ctrl.resolve_kpep_ref telle quelle (même règle de précédence :
    match société exacte, puis fallback première société disponible).
    """
    exact_idx: dict = {}
    fallback_idx: dict = {}
    if not num_personnes:
        return exact_idx, fallback_idx

    for i in range(0, len(num_personnes), BATCH_SIZE):
        batch = num_personnes[i:i + BATCH_SIZE]
        sql = f"""
        WITH input_ids AS (
            SELECT unnest(%(vals)s::text[]) AS v
        ),
        matched AS (
            SELECT r1.{IEHE_COL_ID} AS num_personne,
                   r1.idrpp,
                   r1.socappr
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
        SELECT m.num_personne, m.socappr, k.kpep
        FROM   matched m
        LEFT JOIN kpep_map k ON k.idrpp = m.idrpp
        """
        with conn.cursor() as cur:
            cur.execute(sql, {"vals": batch})
            for num_pers, soc, kpep in cur.fetchall():
                pid = (num_pers or "").strip()
                kpep_s = (kpep or "").strip()
                if not pid or not kpep_s:
                    continue
                soc_s = (soc or "").strip()
                exact_idx.setdefault((pid, soc_s), kpep_s)
                fallback_idx.setdefault(pid, [])
                fallback_idx[pid].append((soc_s, kpep_s))

    return exact_idx, fallback_idx


# --- DÉTECTION DES FICHIERS ---

def find_all_tp_ged_ko(output_dir: Path):
    """Retourne tous les *_TP_GED_KO.csv dans Output/, triés par date de préfixe."""
    candidates = list(output_dir.glob("*_TP_GED_KO.csv"))
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


def find_latest_ged_extract(input_dir: Path):
    """Retourne le {PREFIX}_TP_GED.csv le plus récent d'Input_Data (extraction
    GED de référence pour re-vérifier la présence), ou None si aucun."""
    candidates = list(input_dir.glob("*_TP_GED.csv"))
    best, best_dt = None, None
    for f in candidates:
        prefix = f.name.split("_")[0]
        if re.fullmatch(r"\d{8}", prefix):
            try:
                dt = datetime.strptime(prefix, "%d%m%Y")
            except ValueError:
                continue
            if best_dt is None or dt > best_dt:
                best_dt, best = dt, f
    return best


def detect_separator(filepath: Path) -> str:
    """Détecte le séparateur CSV d'un fichier."""
    with open(filepath, "r", encoding="utf-8-sig") as f:
        first_line = f.readline()
    if "\t" in first_line:
        return "\t"
    if ";" in first_line:
        return ";"
    return ","


# --- TRAITEMENT D'UN FICHIER ---

def process_one_file(ko_path: Path, conn, kpep_ged: set, ged_label: str, corrections):
    """Re-vérifie un fichier TP_GED_KO : résout les KPEP IEHE encore absents,
    recontrôle la présence GED, réapplique les corrections manuelles, et
    réécrit le fichier en place.

    Toute exception est capturée et loguée : un fichier défaillant n'arrête
    pas le traitement des fichiers suivants.
    """
    print(f"\n{'─'*50}")
    print(f"  Fichier : {ko_path.name}")
    print(f"{'─'*50}")

    try:
        df = pd.read_csv(ko_path, sep=None, engine="python", dtype=str, keep_default_na=False)
        df.columns = [str(c).strip().lower() for c in df.columns]

        col_pers = ctrl.get_col(df, ["num_personne", "numpersonne"])
        col_soc = ctrl.get_col(df, ["code_soc_appart", "code_societe", "code_soc"])
        col_offre = ctrl.get_col(df, ["offre", "code_offre"])
        col_kpep = ctrl.get_col(df, ["kpep_reference"])
        col_motif_kpep = ctrl.get_col(df, ["motif_kpep"])
        col_present = ctrl.get_col(df, ["present_ged"])
        col_statut = ctrl.get_col(df, ["statut_rapprochement"])
        col_statut_final = ctrl.get_col(df, ["statut_final"])
        col_motif = ctrl.get_col(df, ["motif"])
        col_comment = ctrl.get_col(df, ["commentaire"])
        col_corr = ctrl.get_col(df, ["correction_manuelle"])

        required = {"num_personne": col_pers, "kpep_reference": col_kpep,
                    "present_ged": col_present, "statut_final": col_statut_final}
        missing = [k for k, v in required.items() if v is None]
        if missing:
            print(f"  [ERREUR] Colonnes manquantes {missing}. Ignoré.")
            return 0, 0, 0

        # Colonnes de suivi retry — initialisées si absentes (premier passage,
        # ou fichier généré avant l'ajout du suivi dans 07_controle_tp_ged.py).
        if "statut_retry" not in df.columns:
            df["statut_retry"] = "KO"
        if "date_derniere_verif" not in df.columns:
            df["date_derniere_verif"] = ""

        mask_ko = df["statut_retry"].astype(str).str.upper() == "KO"
        nb_ko = int(mask_ko.sum())

        if nb_ko == 0:
            nb_ok = int((df["statut_retry"].astype(str).str.upper() == "OK").sum())
            print(f"  [INFO] Tous les {nb_ok} enregistrements sont déjà OK.")
            return 0, 0, nb_ok

        print(f"  [INFO] Encore en KO : {nb_ko} / {len(df)}")

        # --- Étape 1 : résoudre via IEHE (live) les KPEP encore absents ---
        mask_no_kpep = mask_ko & (df[col_kpep].astype(str).str.strip() == "")
        nums_to_resolve = df.loc[mask_no_kpep, col_pers].astype(str).str.strip()
        nums_to_resolve = sorted({n for n in nums_to_resolve if n})
        print(f"  [INFO] KPEP IEHE à re-résoudre : {len(nums_to_resolve)}")

        exact_idx, fallback_idx = build_live_iehe_index(conn, nums_to_resolve)
        print(f"  [QUERY IEHE] → {len(fallback_idx)}/{len(nums_to_resolve)} personne(s) désormais connue(s)")

        # --- Étape 2 : recontrôle GED + réapplication des corrections ---
        today_str = datetime.now().strftime("%d%m%Y")
        nb_resolved_iehe = 0
        nb_resolved_final = 0

        for idx in df.index[mask_ko]:
            num_pers = str(df.at[idx, col_pers] or "").strip()
            code_soc = str(df.at[idx, col_soc] or "").strip() if col_soc else ""
            offre = str(df.at[idx, col_offre] or "").strip().upper() if col_offre else ""
            kpep_ref = str(df.at[idx, col_kpep] or "").strip().upper()

            df.at[idx, "date_derniere_verif"] = today_str

            if not kpep_ref:
                kpep_new, motif_kpep_new = ctrl.resolve_kpep_ref(
                    num_pers, code_soc, exact_idx, fallback_idx
                )
                if kpep_new:
                    kpep_ref = kpep_new.strip().upper()
                    df.at[idx, col_kpep] = kpep_ref
                    if col_motif_kpep:
                        df.at[idx, col_motif_kpep] = motif_kpep_new
                    nb_resolved_iehe += 1

            if not kpep_ref:
                # Toujours absent d'IEHE : reste KO, motif "KPEP IEHE absent" inchangé.
                continue

            present = "OUI" if kpep_ref in kpep_ged else "NON"
            if col_present:
                df.at[idx, col_present] = present

            statut = "RAPPROCHE" if present == "OUI" else "NON_RAPPROCHE"
            if col_statut:
                df.at[idx, col_statut] = statut

            # Le motif métier suit l'état du KPEP, pas seulement la présence
            # GED : un KPEP nouvellement résolu mais toujours absent de GED
            # ne doit plus afficher "KPEP IEHE absent" (motif stale), sinon
            # on recalcule exactement comme 07_controle_tp_ged.build_detail.
            if col_motif:
                motif_kpep_val = str(df.at[idx, col_motif_kpep] or "") if col_motif_kpep else ""
                df.at[idx, col_motif] = (
                    "KPEP IEHE société différente (fallback)"
                    if motif_kpep_val == "fallback_autre_societe" else ""
                )

            statut_final, commentaire = ctrl.apply_correction(
                kpep_ref, code_soc, offre, statut, corrections
            )
            if col_statut_final:
                df.at[idx, col_statut_final] = statut_final
            if col_corr:
                df.at[idx, col_corr] = "OUI" if statut_final != statut else "NON"
            if col_comment and commentaire:
                df.at[idx, col_comment] = commentaire

            if statut_final == "RAPPROCHE":
                df.at[idx, "statut_retry"] = "OK"
                nb_resolved_final += 1

        # Réécriture en place
        sep = detect_separator(ko_path)
        df.to_csv(ko_path, index=False, sep=sep, encoding="utf-8-sig")

        nb_still_ko = int((df["statut_retry"].astype(str).str.upper() == "KO").sum())
        nb_total_ok = int((df["statut_retry"].astype(str).str.upper() == "OK").sum())

        print(f"  [BILAN] {ko_path.name} : {nb_resolved_iehe} KPEP résolu(s) IEHE, "
              f"{nb_resolved_final} rapproché(s) ce retry / {nb_total_ok} OK cumulés / "
              f"{nb_still_ko} encore KO (GED de référence : {ged_label})")

        return nb_resolved_final, nb_still_ko, nb_total_ok

    except Exception as e:
        print(f"  [ERREUR] Échec traitement {ko_path.name} : {type(e).__name__}: {e}")
        return 0, 0, 0


# --- MAIN ---

def main():
    print(f"\n{'='*60}")
    print(f"  08_tp_ged_retry.py — Retry TP GED KO (J+1 / J+2 / J+7)")
    print(f"{'='*60}")

    # 1. Déterminer les fichiers à traiter
    if len(sys.argv) > 1:
        prefix = sys.argv[1]
        ko_path = OUTPUT_DIR / f"{prefix}_TP_GED_KO.csv"
        if not ko_path.exists():
            print(f"\n[ERREUR] Fichier introuvable : {ko_path}")
            sys.exit(1)
        files = [ko_path]
    else:
        files = find_all_tp_ged_ko(OUTPUT_DIR)

    if not files:
        print(f"\n[INFO] Aucun fichier *_TP_GED_KO.csv trouvé dans {OUTPUT_DIR}.")
        print(f"       → Cas normal si l'étape 07_controle_tp_ged.py n'a pas encore été exécutée.")
        sys.exit(0)

    print(f"\n[INFO] {len(files)} fichier(s) TP_GED_KO détecté(s)")

    # 2. Extraction GED de référence : la plus récente disponible dans Input_Data
    ged_file = find_latest_ged_extract(INPUT_DIR)
    if ged_file is None:
        print(f"\n[ERREUR] Aucun {{PREFIX}}_TP_GED.csv trouvé dans {INPUT_DIR}. "
              f"Impossible de re-vérifier la présence GED.")
        sys.exit(1)
    kpep_ged, n_ged_rows = ctrl.load_ged_kpep_set(ged_file)
    print(f"[INFO] Extraction GED de référence : {ged_file.name} "
          f"({n_ged_rows} ligne(s), {len(kpep_ged)} KPEP distinct(s))")

    # 3. Corrections manuelles (même fichier que 07_controle_tp_ged.py)
    corrections = ctrl.load_corrections(ctrl.INPUT_DIR / ctrl.CORRECTIONS_FILE)

    # 4. Connexion IEHE unique (réutilisée pour tous les fichiers)
    conn = connect_iehe_auto()

    # 5. Traitement de chaque fichier
    total_resolved = 0
    total_still_ko = 0
    total_ok = 0

    for ko_path in files:
        resolved, still_ko, ok = process_one_file(ko_path, conn, kpep_ged, ged_file.name, corrections)
        total_resolved += resolved
        total_still_ko += still_ko
        total_ok += ok

    conn.close()

    # 6. Résumé global
    print(f"\n{'='*60}")
    print(f"  RÉSUMÉ GLOBAL")
    print(f"{'='*60}")
    print(f"  Fichiers traités       : {len(files)}")
    print(f"  Rapprochés ce retry    : {total_resolved}")
    print(f"  Total RAPPROCHE (OK)   : {total_ok}")
    print(f"  Encore NON_RAPPROCHE   : {total_still_ko}")
    print(f"  GED de référence       : {ged_file.name}")
    print(f"  Date vérification      : {datetime.now().strftime('%d%m%Y')}")
    print()


if __name__ == "__main__":
    main()
