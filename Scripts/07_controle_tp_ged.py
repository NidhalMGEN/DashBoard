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


# -----------------------------
# I/O utilitaires (alignés 03_generation_fichiers_detail.py)
# -----------------------------
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
# CHARGEMENT FICHIER GED
# -----------------------------
def load_ged_kpep_set(path: Path) -> Tuple[set[str], int]:
    """
    Charge le CSV GED et retourne l'ensemble des KPEP présents (uppercase strip)
    ainsi que le nombre de lignes lues. Colonne acceptée : idepsp / idkpep / kpep.

    Le fichier GED est très simple (1 colonne). Le sniffer de séparateur de
    pandas peut s'égarer sur ce type de fichier : on tente d'abord un parsing
    explicite par séparateurs courants (',', ';', '\\t'), puis, en dernier
    recours, une lecture ligne-à-ligne si le fichier n'a qu'une seule colonne.
    """
    if not path.exists():
        return set(), 0

    def _try_read(sep: Optional[str]) -> Optional[pd.DataFrame]:
        for enc in ("utf-8-sig", "utf-8", "latin-1", "cp1252"):
            try:
                return pd.read_csv(path, sep=sep, engine="python", dtype=str, encoding=enc)
            except Exception:
                continue
        return None

    df: Optional[pd.DataFrame] = None
    for sep in (",", ";", "\t", None):
        tmp = _try_read(sep)
        if tmp is None or tmp.empty:
            continue
        tmp = normalize_cols(tmp)
        if get_col(tmp, ["idepsp", "idkpep", "kpep", "kpep_ged"]) is not None:
            df = tmp
            break
        # Colonne unique non reconnue → on la prend quand même
        if df is None and len(tmp.columns) == 1:
            df = tmp

    if df is None or df.empty:
        return set(), 0

    col = get_col(df, ["idepsp", "idkpep", "kpep", "kpep_ged"]) or df.columns[0]
    n = len(df)
    values = df[col].dropna().astype(str).str.strip().str.upper()
    values = values[values != ""]
    return set(values.tolist()), n


# -----------------------------
# CONSTRUCTION DU DÉTAIL
# -----------------------------
def build_detail(
    df_ns: pd.DataFrame,
    kpep_ged: set[str],
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
    col_nom = get_col(df, ["nom_long", "nom"])
    col_prenom = get_col(df, ["prenom", "firstname"])
    col_ctr = get_col(df, ["num_ctr_indiv", "contrat"])

    required = {"num_personne": col_pers, "type_assure": col_type, "offre": col_offre}
    missing = [k for k, v in required.items() if v is None]
    if missing:
        print(f"❌ New_S — colonnes manquantes : {missing}. Traitement impossible.")
        return pd.DataFrame()

    rows: List[Dict[str, Any]] = []

    date_flux_str = date_flux.strftime("%Y-%m-%d")

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

        present_ged = "OUI" if (kpep_ref_u and kpep_ref_u in kpep_ged) else "NON"

        # Si pas de KPEP IEHE, on ne peut pas statuer : NON_RAPPROCHE avec motif
        if not kpep_ref_u:
            statut = "NON_RAPPROCHE"
            motif = "KPEP IEHE absent"
        else:
            statut = "RAPPROCHE" if present_ged == "OUI" else "NON_RAPPROCHE"
            motif = ""
            if motif_kpep == "fallback_autre_societe":
                motif = "KPEP IEHE société différente (fallback)"

        # Application correction manuelle
        statut_final, commentaire = apply_correction(
            kpep_ref_u, code_soc, offre, statut, corrections
        )
        correction_appliquee = "OUI" if statut_final != statut else "NON"

        rows.append(
            {
                "date_flux": prefix,                         # DDMMYYYY
                "date_flux_iso": date_flux_str,              # YYYY-MM-DD
                "num_personne": num_pers,
                "nom_long": str(r.get(col_nom, "") or "").strip() if col_nom else "",
                "prenom": str(r.get(col_prenom, "") or "").strip() if col_prenom else "",
                "type_assure": type_assure,
                "code_societe": code_soc,
                "offre": offre,
                "num_contrat": str(r.get(col_ctr, "") or "").strip() if col_ctr else "",
                "kpep_reference": kpep_ref_u,
                "motif_kpep": motif_kpep,
                "present_ged": present_ged,
                "statut_rapprochement": statut,              # état brut calculé
                "correction_manuelle": correction_appliquee, # OUI / NON
                "statut_final": statut_final,                # après correction
                "motif": motif,
                "commentaire": commentaire,
            }
        )

    return pd.DataFrame(rows)


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


# -----------------------------
# MAIN
# -----------------------------
def main() -> int:
    parser = argparse.ArgumentParser(
        description="Contrôle journalier des cartes TP en GED"
    )
    parser.add_argument(
        "--prefix",
        default=None,
        help="Préfixe DDMMYYYY. Si absent, détection automatique (dernier New_S).",
    )
    parser.add_argument(
        "--sep",
        default=None,
        help="Séparateur CSV de sortie (défaut : OUTPUT_SEP env ou ';').",
    )
    parser.add_argument(
        "--skip-historique",
        action="store_true",
        help="Ne met pas à jour Output/Historique_TP_GED.csv (utile pour rejeux).",
    )
    args = parser.parse_args()

    output_sep = args.sep if args.sep else os.getenv("OUTPUT_SEP", ";")
    if not isinstance(output_sep, str) or len(output_sep) != 1:
        print(f"⚠️ Séparateur invalide '{output_sep}', fallback ';'.")
        output_sep = ";"

    print("--- 07_controle_tp_ged : démarrage ---")
    print(f"   Input_Data : {INPUT_DIR}")
    print(f"   Output     : {OUTPUT_DIR}")

    if not INPUT_DIR.exists():
        print(f"❌ Dossier Input_Data introuvable : {INPUT_DIR}")
        return 1

    # --- Détection préfixe ---
    prefix = args.prefix or find_latest_prefix(INPUT_DIR)
    if not prefix:
        print("❌ Aucun préfixe DDMMYYYY détecté (pas de {PREFIX}_New_S.csv).")
        return 1
    try:
        date_flux = datetime.strptime(prefix, "%d%m%Y").date()
    except ValueError:
        date_flux = date.today()
        print(f"⚠️ Préfixe '{prefix}' non parseable, date du jour utilisée.")
    print(f"📂 Préfixe retenu : {prefix}  (date de flux = {date_flux.strftime('%d/%m/%Y')})")

    # --- Chargement des fichiers ---
    path_ns = INPUT_DIR / f"{prefix}_New_S.csv"
    path_iehe = INPUT_DIR / f"{prefix}_IEHE.csv"
    path_ged = INPUT_DIR / f"{prefix}_TP_GED.csv"
    path_corr = INPUT_DIR / CORRECTIONS_FILE

    df_ns = load_csv(path_ns, sep=None)
    if df_ns is None or df_ns.empty:
        print(f"❌ Fichier New_S manquant ou vide : {path_ns.name}")
        return 1
    df_ns = normalize_cols(df_ns)
    print(f"   ✅ New_S chargé : {len(df_ns)} lignes")

    df_iehe = load_csv(path_iehe, sep=None)
    if df_iehe is None or df_iehe.empty:
        print(f"⚠️ IEHE absent ou vide ({path_iehe.name}) : KPEP de référence non résolus.")
    else:
        print(f"   ✅ IEHE chargé : {len(df_iehe)} lignes")

    if not path_ged.exists():
        print(f"⚠️ Extraction GED absente : {path_ged.name}")
        print("   → Le détail sera produit avec 100% NON_RAPPROCHE (population éligible pure).")
    kpep_ged, n_ged_rows = load_ged_kpep_set(path_ged)
    print(f"   ✅ GED chargé : {n_ged_rows} ligne(s), {len(kpep_ged)} KPEP distinct(s)")

    corrections = load_corrections(path_corr)

    # --- Index KPEP IEHE par (personne, société) ---
    exact_idx, fallback_idx = build_iehe_kpep_index(df_iehe)
    print(
        f"   ✅ Index IEHE : {len(exact_idx)} clé(s) exacte(s), "
        f"{len(fallback_idx)} personne(s) indexée(s)"
    )

    # --- Construction du détail ---
    df_detail = build_detail(
        df_ns=df_ns,
        kpep_ged=kpep_ged,
        exact_idx=exact_idx,
        fallback_idx=fallback_idx,
        corrections=corrections,
        date_flux=date_flux,
        prefix=prefix,
    )
    if df_detail.empty:
        print("⚠️ Aucune carte TP éligible trouvée dans New_S. Détail vide produit.")
    else:
        n_total = len(df_detail)
        n_rapp = (df_detail["statut_final"] == "RAPPROCHE").sum()
        n_nonrapp = (df_detail["statut_final"] == "NON_RAPPROCHE").sum()
        n_corr = (df_detail["correction_manuelle"] == "OUI").sum()
        taux = (n_rapp / n_total * 100.0) if n_total else 0.0
        print(
            f"📊 Résultat : {n_total} éligibles | "
            f"{n_rapp} RAPPROCHE | {n_nonrapp} NON_RAPPROCHE | "
            f"{n_corr} corrections manuelles | taux = {taux:.2f}%"
        )

    # --- Export Détail ---
    f_out = OUTPUT_DIR / f"{prefix}_TP_GED_Detail.csv"
    df_detail.to_csv(f_out, index=False, sep=output_sep, encoding="utf-8-sig")
    print(f"   ✅ Détail écrit : {f_out.name} ({len(df_detail)} lignes)")

    # --- Export KO (NON_RAPPROCHE avant ou après correction) ---
    df_ko = df_detail[df_detail["statut_final"] == "NON_RAPPROCHE"].copy() if not df_detail.empty else df_detail
    f_ko = OUTPUT_DIR / f"{prefix}_TP_GED_KO.csv"
    df_ko.to_csv(f_ko, index=False, sep=output_sep, encoding="utf-8-sig")
    print(f"   ✅ KO écrit    : {f_ko.name} ({len(df_ko)} lignes)")

    # --- Historique append-only ---
    if args.skip_historique:
        print("   ⏭️ Historique non mis à jour (--skip-historique).")
    else:
        hist_path = OUTPUT_DIR / HISTORIQUE_FILE
        append_historique(df_detail, hist_path, sep=output_sep)
        size = hist_path.stat().st_size if hist_path.exists() else 0
        print(f"   ✅ Historique : {hist_path.name} (+{len(df_detail)} lignes, taille={size} octets)")

    print("🏁 07_controle_tp_ged : terminé.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
    