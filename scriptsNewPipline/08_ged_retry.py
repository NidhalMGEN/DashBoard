# -*- coding: utf-8 -*-
"""
07_controle_tp_ged.py (PROD GRADE) — CONTRÔLE JOURNALIER DES CARTES TP EN GED
=============================================================================

Objectif
--------
Rapprocher l'extraction GED quotidienne (fichier {PREFIX}_TP_GED.csv) avec la
population des cartes TP éligibles attendues (déduite de {PREFIX}_New_S.csv
enrichi par {PREFIX}_IEHE.csv), pour savoir :
  - quelles cartes TP sont TROUVÉES en GED (statut RAPPROCHE)
  - quelles cartes TP sont MANQUANTES (statut NON_RAPPROCHE)

Règles métier (population TP éligible)
--------------------------------------
- Types assurés retenus : ASSPRI, MPACTI, MPRETR, MPVRET
- Exclusions :
    * conjoints (type_assure contenant "CONJ")
    * offres commençant par "INP*"
    * offres commençant par "MEP*"
    * société "073"
- KPEP GED de référence = KPEP IEHE correspondant au code société de l'assuré
  (`kpep_iehe` de la ligne IEHE dont `socappr == code_soc_appart`).
  À défaut (pas de match société), on retient le `kpep_iehe` de la première
  ligne IEHE disponible pour la personne et on indique le motif.

Flux quotidien
--------------
  - {PREFIX}_TP_GED.csv : CSV très simple, une colonne contenant les KPEP
    présents en GED. Colonne attendue : `idepsp` (fallback : `idkpep`, `kpep`).

Correction manuelle (réconciliation métier)
-------------------------------------------
Fichier de référence : Input_Data/TP_GED_Corrections.csv (sans préfixe,
append-only manuel). Colonnes minimales :
  - kpep_ref           : KPEP de référence ciblé
  - code_societe       : code société concerné
  - offre              : offre concernée (facultatif, filtre additionnel)
  - decision_manuelle  : "FORCE_RAPPROCHE" ou "FORCE_NON_RAPPROCHE"
  - commentaire        : motif de la correction (ex. "MSP santé vs prévoyance")
  - date_correction    : DDMMYYYY ou YYYY-MM-DD (tracabilité)
  - auteur             : identifiant utilisateur (tracabilité)

La correction surcharge uniquement la colonne `statut_final` ; `statut_rapprochement`
reste la valeur calculée brute, pour garder la piste d'audit.

Historique
----------
Chaque exécution ajoute en append-only :
  - Output/Historique_TP_GED.csv (consolidé, clé naturelle = prefix + num_personne
    + code_societe + offre). La dédoublonnage n'est PAS fait ici : on conserve
    toutes les lignes pour pouvoir reconstituer l'état à une date donnée.

Entrées
-------
- Input_Data/{PREFIX}_New_S.csv          (obligatoire)
- Input_Data/{PREFIX}_IEHE.csv           (obligatoire — source des KPEP réf.)
- Input_Data/{PREFIX}_TP_GED.csv         (obligatoire — extraction GED du jour)
- Input_Data/TP_GED_Corrections.csv      (optionnel — surcharge manuelle)

Sorties
-------
- Output/{PREFIX}_TP_GED_Detail.csv      (détail exploitable métier)
- Output/{PREFIX}_TP_GED_KO.csv          (sous-ensemble NON_RAPPROCHE, prêt
                                          pour revue manuelle)
- Output/Historique_TP_GED.csv           (append-only cumulatif)
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import date, datetime
from io import StringIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import sys
from datetime import datetime

import psycopg


# -----------------------------
# CONFIG — aligné sur les autres scripts du pipeline
# -----------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = SCRIPT_DIR if (SCRIPT_DIR / "Input_Data").exists() else SCRIPT_DIR.parent
INPUT_DIR = BASE_DIR / "Input_Data"
OUTPUT_DIR = BASE_DIR / "Output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
INPUT_DIR.mkdir(parents=True, exist_ok=True)

# Types assurés éligibles carte TP (doit rester aligné avec 02_calcul_kpi / 03_generation_fichiers_detail)
TYPES_TP = ["ASSPRI", "MPACTI", "MPRETR", "MPVRET"]

# Préfixes d'offres exclus (demande métier flux GED : INP* et MEP*)
OFFRES_PREFIX_EXCLUS: Tuple[str, ...] = ("INP", "MEP")

# Sociétés exclues
SOCIETES_EXCLUES = {"073"}

# Nom du fichier historique cumulatif (append-only, pas de préfixe)
HISTORIQUE_FILE = "Historique_TP_GED.csv"

# Nom du fichier de corrections manuelles (persistant, pas de préfixe)
CORRECTIONS_FILE = "TP_GED_Corrections.csv"

BATCH_SIZE_SQL = 20000

# Template SQL pour interroger la GED (balise __LISTE_IDS__ à substituer)
GED_SQL_TEMPLATE = "00-Export_GED_KPEP_With_Distinct.sql"




# Config BDD (IEHE)
PG_HOST = os.getenv("PG_HOST", "bdd-T0XX0052.alias")
PG_PORT = os.getenv("PG_PORT", "5577")
PG_DB = os.getenv("PG_DB", "supervisionpsc_db")
PG_USER     = os.environ.get("PG_USER", "")
PG_PASSWORD = os.environ.get("PG_PASSWORD", "")
GED_SCHEMA = os.getenv("PG_SCHEMA", "rptpsc")
GED_TABLE  = "suivi_tp_ged"
# -----------------------------
# I/O utilitaires (alignés 03_generation_fichiers_detail.py)
# -----------------------------
# load the new_S file
def load_csv(path: Path, sep: Optional[str] = None) -> Optional[pd.DataFrame]:
    """
    Chargement tolérant (utf-8-sig → latin-1), détection de séparateur auto.
    Retourne None si le fichier est absent ou illisible (jamais d'exception).
    """
    if path is None or not path.exists():
        return None
    attempts = [
        {"encoding": "utf-8-sig"},
        {"encoding": "utf-8"},
        {"encoding": "latin-1"},
        {"encoding": "cp1252"},
    ]
    for params in attempts:
        try:
            df = pd.read_csv(path, sep=sep, engine="python", dtype=str, **params)
            return df
        except Exception:
            continue
    print(f"❌ Lecture impossible : {path.name}")
    return None

# remove majucule and spaces form words
def normalize_cols(df: Optional[pd.DataFrame]) -> Optional[pd.DataFrame]:
    """Met les colonnes en minuscules et strip.""" 
    if df is None:
        return None
    df.columns = [str(c).strip().lower() for c in df.columns]
    return df


def get_col(df: Optional[pd.DataFrame], candidates: List[str]) -> Optional[str]:
    """Retourne le premier nom de colonne trouvé dans le df (insensible à la casse)."""
    if df is None:
        return None
    cols = {c.lower() for c in df.columns}
    for cand in candidates:
        if cand.lower() in cols:
            return cand.lower()
    return None


def find_latest_prefix(folder: Path) -> Optional[str]:
    """
    Cherche le préfixe DDMMYYYY le plus récent dans `folder` à partir des
    fichiers {PREFIX}_New_S.csv (même convention que les autres scripts).
    """
    candidates = list(folder.glob("*_New_S.csv"))
    best_prefix: Optional[str] = None
    best_dt = None
    for f in candidates:
        prefix = f.name.split("_")[0]
        if re.fullmatch(r"\d{8}", prefix):
            try:
                dt = datetime.strptime(prefix, "%d%m%Y")
            except ValueError:
                continue
            if best_dt is None or dt > best_dt:
                best_dt = dt
                best_prefix = prefix
    return best_prefix


# -----------------------------
# ÉLIGIBILITÉ TP (règles du flux GED)
# -----------------------------
def est_conjoint(type_assure: str) -> bool:
    """True si le type d'assuré désigne un conjoint (CONJ, CONJOINT, etc.)."""
    return "CONJ" in (type_assure or "").upper()


def est_offre_exclue(offre: str) -> bool:
    """True si l'offre commence par un préfixe exclu (INP*, MEP*)."""
    o = (offre or "").upper().strip()
    return o.startswith(OFFRES_PREFIX_EXCLUS)


def est_societe_exclue(code_soc: str) -> bool:
    """True si la société est exclue (073)."""
    return (code_soc or "").strip() in SOCIETES_EXCLUES


def compute_eligibilite_tp_ged(
    type_assure: str,
    offre: str,
    code_soc: str,
) -> Tuple[bool, str]:
    """
    Retourne (eligible, raison_non_eligibilite).

    Règles (demande métier flux GED) :
      1. type_assure ∈ TYPES_TP   (pas de conjoint)
      2. offre ne commence pas par INP* / MEP*
      3. société != 073
    """
    ta = (type_assure or "").upper().strip()
    if est_conjoint(ta):
        return False, "Conjoint"
    if ta not in TYPES_TP:
        return False, f"Type assuré non éligible ({ta or 'vide'})"
    if est_offre_exclue(offre):
        return False, "Offre exclue (INP*/MEP*)"
    if est_societe_exclue(code_soc):
        return False, "Société exclue (073)"
    return True, ""


# -----------------------------
# INDEXATION IEHE (KPEP de référence par (num_personne, code_société))
# -----------------------------
def build_iehe_kpep_index(
    df_iehe: Optional[pd.DataFrame],
) -> Tuple[Dict[Tuple[str, str], str], Dict[str, List[Tuple[str, str]]]]:
    """
    Construit deux index utiles à partir de {PREFIX}_IEHE.csv :

    - exact_idx : {(num_personne, code_soc_iehe) -> kpep_iehe}
        * code_soc_iehe provient de la colonne `socappr`
        * on privilégie les lignes où `kpep_iehe` est renseigné

    - fallback_idx : {num_personne -> [(code_soc_iehe, kpep_iehe), ...]}
        * toutes les lignes IEHE avec un kpep_iehe non vide, triées par
          fréquence d'apparition (permet un fallback déterministe si la
          société demandée n'a pas de ligne exacte).

    `kpep_iehe` est la colonne explicitement au format "KPEP..." depuis les
    extractions récentes. Si elle manque, on retombe sur `idrpp` si et
    seulement si sa valeur commence par "KPEP".
    """
    exact_idx: Dict[Tuple[str, str], str] = {}
    fallback_idx: Dict[str, List[Tuple[str, str]]] = {}

    if df_iehe is None or df_iehe.empty:
        return exact_idx, fallback_idx

    df = normalize_cols(df_iehe.copy())

    col_pid = get_col(df, ["refperboccn", "num_personne", "numpersonne"])
    col_soc = get_col(df, ["socappr", "code_soc", "code_societe"])
    col_kpep_iehe = get_col(df, ["kpep_iehe"])
    col_idrpp = get_col(df, ["idrpp"])

    if col_pid is None:
        return exact_idx, fallback_idx

    def _pick_kpep(row: pd.Series) -> str:
        # Priorité à kpep_iehe
        if col_kpep_iehe:
            v = str(row.get(col_kpep_iehe, "") or "").strip()
            if v and v.lower() not in ("nan", "none", "null"):
                return v
        # Fallback sur idrpp uniquement si la valeur est déjà "KPEP..."
        if col_idrpp:
            v = str(row.get(col_idrpp, "") or "").strip()
            if v.upper().startswith("KPEP"):
                return v
        return ""

    for _, r in df.iterrows():
        pid = str(r.get(col_pid, "") or "").strip()
        if not pid:
            continue
        soc = str(r.get(col_soc, "") or "").strip() if col_soc else ""
        kpep = _pick_kpep(r)
        if not kpep:
            continue
        # Index exact (soc peut être vide → on garde quand même avec clé "")
        exact_idx.setdefault((pid, soc), kpep)
        fallback_idx.setdefault(pid, [])
        fallback_idx[pid].append((soc, kpep))

    return exact_idx, fallback_idx


def resolve_kpep_ref(
    num_personne: str,
    code_soc: str,
    exact_idx: Dict[Tuple[str, str], str],
    fallback_idx: Dict[str, List[Tuple[str, str]]],
) -> Tuple[str, str]:
    """
    Retourne (kpep_ref, motif).
    motif ∈ {"", "match_societe", "fallback_autre_societe", "absent_iehe"}.
    """
    # Tentative exacte
    v = exact_idx.get((num_personne, code_soc))
    if v:
        return v, "match_societe"

    # Fallback : première valeur IEHE disponible pour la personne
    candidates = fallback_idx.get(num_personne)
    if candidates:
        # On privilégie une société non vide
        for soc_c, kpep_c in candidates:
            if soc_c:
                return kpep_c, "fallback_autre_societe"
        return candidates[0][1], "fallback_autre_societe"

    return "", "absent_iehe"


# -----------------------------
# CORRECTIONS MANUELLES
# -----------------------------


def load_corrections(path: Path) -> Dict[Tuple[str, str, str], Tuple[str, str]]:
    """
    Retourne un dict {(kpep_ref, code_societe, offre) -> (decision, commentaire)}.

    - decision ∈ {"FORCE_RAPPROCHE", "FORCE_NON_RAPPROCHE"} (normalisée en MAJ).
    - offre vide dans le fichier = joker (s'applique à toutes les offres).
    - Les autres combinaisons (decision inconnue) sont ignorées avec un warning.

    Fichier attendu (séparateur auto-détecté) :
        kpep_ref;code_societe;offre;decision_manuelle;commentaire;date_correction;auteur
    """
    mapping: Dict[Tuple[str, str, str], Tuple[str, str]] = {}
    df = load_csv(path, sep=None)
    if df is None or df.empty:
        return mapping

    df = normalize_cols(df)
    col_kpep = get_col(df, ["kpep_ref", "kpep", "idepsp"])
    col_soc = get_col(df, ["code_societe", "code_soc", "code_soc_appart"])
    col_offre = get_col(df, ["offre", "code_offre"])
    col_dec = get_col(df, ["decision_manuelle", "decision", "statut_manuel"])
    col_com = get_col(df, ["commentaire", "motif"])

    if col_kpep is None or col_dec is None:
        print(
            f"⚠️ {path.name} ignoré : colonnes 'kpep_ref' et 'decision_manuelle' requises."
        )
        return mapping

    decisions_ok = {"FORCE_RAPPROCHE", "FORCE_NON_RAPPROCHE"}
    for _, r in df.iterrows():
        kpep = str(r.get(col_kpep, "") or "").strip().upper()
        if not kpep:
            continue
        soc = str(r.get(col_soc, "") or "").strip() if col_soc else ""
        offre = str(r.get(col_offre, "") or "").strip().upper() if col_offre else ""
        dec = str(r.get(col_dec, "") or "").strip().upper()
        if dec not in decisions_ok:
            continue
        com = str(r.get(col_com, "") or "").strip() if col_com else ""
        mapping[(kpep, soc, offre)] = (dec, com)

    if mapping:
        print(f"   ✅ {len(mapping)} correction(s) manuelle(s) chargée(s) depuis {path.name}")
    return mapping


def apply_correction(
    kpep_ref: str,
    code_soc: str,
    offre: str,
    statut_rapprochement: str,
    corrections: Dict[Tuple[str, str, str], Tuple[str, str]],
) -> Tuple[str, str]:
    """
    Retourne (statut_final, commentaire_final).

    Règle de recherche (ordre décroissant de spécificité) :
      1. (kpep, soc, offre) exacte
      2. (kpep, soc, "")    — joker offre
      3. (kpep, "",  "")    — joker soc+offre
    """
    kpep_u = (kpep_ref or "").strip().upper()
    soc_s = (code_soc or "").strip()
    offre_u = (offre or "").strip().upper()

    for key in ((kpep_u, soc_s, offre_u), (kpep_u, soc_s, ""), (kpep_u, "", "")):
        if key in corrections:
            dec, com = corrections[key]
            if dec == "FORCE_RAPPROCHE":
                return "RAPPROCHE", f"[FORCE_RAPPROCHE] {com}".strip()
            if dec == "FORCE_NON_RAPPROCHE":
                return "NON_RAPPROCHE", f"[FORCE_NON_RAPPROCHE] {com}".strip()
    return statut_rapprochement, ""




# -----------------------------
# CONSTRUCTION DU DÉTAIL
# -----------------------------
def build_detail(
    df_ns: pd.DataFrame,
    exact_idx: Dict[Tuple[str, str], str],
    fallback_idx: Dict[str, List[Tuple[str, str]]],
    corrections: Dict[Tuple[str, str, str], Tuple[str, str]],
    date_flux: date,
    prefix: str,
) -> pd.DataFrame:
    """
    Construit le dataframe de détail TP GED à partir du New_S + index IEHE.

    1 ligne = 1 contrat de la population TP éligible (après exclusions).
    """
    df = df_ns.copy()
    df.columns = [str(c).strip().lower() for c in df.columns]

    col_pers = get_col(df, ["num_personne", "numpersonne"])
    col_type = get_col(df, ["type_assure", "typeassure"])
    col_soc = get_col(df, ["code_soc_appart", "code_societe", "code_soc"])
    col_offre = get_col(df, ["offre", "code_offre"])

    required = {"num_personne": col_pers, "type_assure": col_type, "offre": col_offre}
    missing = [k for k, v in required.items() if v is None]
    if missing:
        print(f"❌ New_S — colonnes manquantes : {missing}. Traitement impossible.")
        return pd.DataFrame()

    rows: List[Dict[str, Any]] = []

    date_flux_str = date_flux.strftime("%Y-%m-%d")
    liste = []
    for _, r in df.iterrows():
        num_pers = str(r.get(col_pers, "") or "").strip()
        if not num_pers:
            continue
        type_assure = str(r.get(col_type, "") or "").strip().upper()
        offre = str(r.get(col_offre, "") or "").strip().upper()
        code_soc = str(r.get(col_soc, "") or "").strip() if col_soc else ""

        eligible, raison = compute_eligibilite_tp_ged(type_assure, offre, code_soc)
        if not eligible:
            continue  # on ne garde que la population éligible dans le détail

        kpep_ref, motif_kpep = resolve_kpep_ref(num_pers, code_soc, exact_idx, fallback_idx)
        kpep_ref_u = kpep_ref.upper().strip()


        # Si pas de KPEP IEHE, on ne peut pas statuer : NON_RAPPROCHE avec motif

        # manage this as non found and like store the reeson because  the nb of ko show also the reseaon 
        #cause its elegible
        if  kpep_ref_u:
            liste.append(kpep_ref_u)

    return set(liste)


# -----------------------------
# HISTORIQUE APPEND-ONLY
# -----------------------------
def append_historique(df_detail: pd.DataFrame, path: Path, sep: str = ";") -> None:
    """
    Ajoute le détail du jour au fichier historique cumulatif. Écrit l'entête
    uniquement si le fichier n'existe pas encore.
    """
    if df_detail is None or df_detail.empty:
        return
    write_header = not path.exists()
    df_detail.to_csv(
        path,
        index=False,
        sep=sep,
        encoding="utf-8-sig",
        mode="a" if not write_header else "w",
        header=write_header,
    )




def find_ged_sql_template() -> Optional[Path]:
    """
    Localise le template SQL GED ({GED_SQL_TEMPLATE}).
    Ordre de recherche : dossier du script, puis Input_Data.
    Retourne None si introuvable.
    """
    for folder in (SCRIPT_DIR, INPUT_DIR):
        candidate = folder / GED_SQL_TEMPLATE
        if candidate.exists():
            return candidate
    return None


def write_ged_sql_batches(kpep_list: List[str], output_dir: Path, prefix: str) -> int:
    """
    Génère les fichiers SQL par batch pour interroger la GED sur les KPEP de
    référence de la population TP éligible (même mécanique que
    write_sql_batches du script 01, type 'simple').

    Sorties : {PREFIX}_REQ_TP_GED_KPEP_Part{N}.sql dans `output_dir`.
    Retourne le nombre de fichiers écrits.
    """
    tpl_path = find_ged_sql_template()
    if tpl_path is None:
        print(f"      ⚠️  Template SQL absent : {GED_SQL_TEMPLATE} (ni {SCRIPT_DIR}, ni {INPUT_DIR})")
        return 0

    # Nettoyage : strip, suppression des apostrophes (injection), dédoublonnage
    kpeps = sorted({str(k).strip().replace("'", "") for k in kpep_list if str(k).strip()})
    if not kpeps:
        print("      ⚠️  Aucun KPEP éligible — aucun fichier SQL généré.")
        return 0

    try:
        base_sql_template = tpl_path.read_text(encoding="utf-8")
    except Exception as e:
        print(f"      ❌ Erreur lecture template {tpl_path.name} : {e}")
        return 0

    if "__LISTE_IDS__" not in base_sql_template:
        print(f"      ❌ Erreur Template {tpl_path.name} : Balise __LISTE_IDS__ introuvable.")
        return 0

    n_files = 0
    for i in range(0, len(kpeps), BATCH_SIZE_SQL):
        chunk = kpeps[i : i + BATCH_SIZE_SQL]
        batch_index = (i // BATCH_SIZE_SQL) + 1
        out_name = f"{prefix}_REQ_TP_GED_RETRY_KPEP_Part{batch_index}.sql"
        values_str = "'" + "','".join(chunk) + "'"
        final_sql = base_sql_template.replace("__LISTE_IDS__", values_str)
        header = f"/* BATCH {batch_index} | GENERATED {datetime.now()} | SOURCE: {prefix} | NB: {len(chunk)} */\n"
        try:
            with open(output_dir / out_name, "w", encoding="utf-8") as f_out:
                f_out.write(header + final_sql)
            print(f"      write -> {out_name} ({len(chunk)} KPEP)")
            n_files += 1
        except Exception as e:
            print(f"      ❌ Erreur génération lot {batch_index} : {e}")
    return n_files

# -----------------------------
# MAIN
# -----------------------------



def load_concat_csv_by_pattern(folder: Path, pattern: str, label: str = "") -> Tuple[Optional[pd.DataFrame], int]:
    files = sorted(folder.glob(pattern))
    if not files:
        return None, 0

    dfs: List[pd.DataFrame] = []
    for f in files:
        dfp = load_csv(f, sep=None)
        if dfp is None or dfp.empty:
            continue
        dfp = normalize_cols(dfp)
        dfs.append(dfp)

    if not dfs:
        return None, 0

    df = pd.concat(dfs, ignore_index=True)
    print(f"   ✅ {label} chargé via pattern: {len(files)} fichier(s), {len(df)} ligne(s)")
    return df, len(files)


def find_latest_new_s(folder: Path) -> Tuple[Optional[Path], Optional[str]]:
    candidates = list(folder.glob("*_New_S.csv"))
    if not candidates:
        return None, None

    best: Optional[Path] = None
    best_dt = None

    for f in candidates:
        prefix = f.name.split("_")[0]
        if re.fullmatch(r"\d{8}", prefix):
            try:
                dt = pd.to_datetime(prefix, format="%d%m%Y")
            except Exception:
                dt = None
            if dt is not None and (best_dt is None or dt > best_dt):
                best_dt = dt
                best = f

    if best is None:
        best = sorted(candidates)[0]
        prefix = best.name.split("_")[0] if "_" in best.name else "UNKNOWN"
        return best, prefix

    return best, best.name.split("_")[0]



def connect_pg(host, port, db):
    try:
        return psycopg.connect(host=host, port=port, dbname=db, user=PG_USER, password=PG_PASSWORD, connect_timeout=3)
    except:
        return None

#one connection
def connect_GED_auto():
    try:
        conn = connect_pg(PG_HOST, PG_PORT, PG_DB)
    except Exception as e:
        print(f"❌ GED connection failed: {PG_HOST}:{PG_PORT}/{PG_DB} — {e}")
        return None           
    return None
    


def getNotFound():
    conn = connect_GED_auto()
    if not conn:
        print("i cann't connect")
        sys.exit(1)
        return

    cur = conn.cursor()
    cur.execute( f"""
        SELECT kpep
        FROM {GED_SCHEMA}.{GED_TABLE}
        WHERE date_found IS NULL
        """
        )
    kpeps = [row[0] for row in cur.fetchall()]

    formatted = date.today().strftime("%d%m%Y")

    write_ged_sql_batches(kpeps,OUTPUT_DIR,formatted)
    conn.commit()
    cur.close()
    conn.close()
    return formatted

    
def SaveKpep(name, cur, formatted):
    cur.execute(
        f"""
        UPDATE {GED_SCHEMA}.{GED_TABLE} SET date_found = %s WHERE kpep = %s AND date_found IS NULL
        """,
        (formatted,name)
    )


def SaveToDB(prefix):
    conn = connect_GED_auto()
    if not conn:
        print("cann't connect")
        sys.exit(1)
    cur = conn.cursor()

    df_GED , _ = load_concat_csv_by_pattern(INPUT_DIR, f"{prefix}*TP_GED_RETRY*.csv", label="GED_KO")
    if df_GED is None:
        cur.close()
        conn.close()
        print("no file _TP_GED_RETRY found")
        return
    today = date.today()
    
    col_kpep = get_col(df_GED, ["idepsp", "idkpep", "kpep"])

    if col_kpep is None:
        print("❌ GED — no KPEP column found.")
        return

    for name in df_GED[col_kpep]:
        name = str(name).strip()
        if not name:
            continue
        SaveKpep(name, cur, today)

    conn.commit()
    cur.close()
    conn.close()


def main() -> int:
    prefix = getNotFound()
    input(f"\nAppuyez sur Entrée une fois le fichier est mis le fichier doit s'appelet {prefix}_TP_GED_RETRY.csv")
    SaveToDB(prefix)
    return 0


if __name__ == "__main__":
    sys.exit(main())
    