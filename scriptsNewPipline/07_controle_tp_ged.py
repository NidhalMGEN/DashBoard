# -*- coding: utf-8 -*-
"""
07_controle_tp_ged.py (PROD GRADE) — CONTRÔLE JOURNALIER DES CARTES TP EN GED
=============================================================================

Objectif
--------
Rapprocher l'extraction GED quotidienne (fichier {PREFIX}_TP_GED.csv) avec la
population des cartes TP attendues, pour savoir lesquelles sont sorties en GED
et lesquelles manquent encore.

D'où vient la population (CHANGEMENT MAJEUR)
--------------------------------------------
Plus du {PREFIX}_New_S.csv du jour, mais de `rptpsc.suivi_carte_tp`, alimentée
par le script 03. Le New_S est un DIFFÉRENTIEL : deux flux consécutifs n'ont
aucune personne en commun. Une carte éligible au 15 août révélée par le flux du
25 juillet n'est donc jamais revue — partir du flux du jour la perdait
définitivement, de même que toutes les cartes restées introuvables la veille.

La requête quotidienne (`fetch_ged_pending`) porte donc sur les affiliations du
jour PLUS l'antériorité non soldée, en trois conditions :
  1. `date_eligibilite <= CURRENT_DATE` — les cartes futures attendent leur date
  2. `date_found_ged IS NULL`           — pas encore trouvée
  3. `kpep_iehe IS NOT NULL`            — la GED est indexée sur le KPEP IEHE

Aucune union à écrire : le script 03 tourne avant, la table ne se vide jamais.

Règles métier (population TP éligible)
--------------------------------------
L'éligibilité (type assuré / offre / société) n'est PLUS décidée ici : elle est
calculée par le script 03 et matérialisée dans la table. Le périmètre partagé
vit dans `Scripts/iehe_ko_lib.py`.

- KPEP GED de référence = KPEP IEHE correspondant au code société de l'assuré
  (`kpep_iehe` de la ligne IEHE dont `socappr == code_soc_appart`).
  À défaut (pas de match société), on retient le `kpep_iehe` de la première
  ligne IEHE disponible pour la personne et on indique le motif.
  ⚠️ C'est bien le KPEP IEHE, JAMAIS celui du New_S : la carte est indexée en
  GED sur le KPEP d'IEHE, pas sur celui de l'affiliation.
- Une personne absente d'IEHE n'a pas de KPEP : elle est comptée au dénominateur
  avec le motif `absent_iehe` mais EXCLUE de la requête GED, où elle serait
  introuvable par construction. Le script 06 la résout, le lendemain elle entre
  d'elle-même dans la recherche.

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

La correction ne se contente plus de surcharger une colonne d'affichage : elle
modifie l'état en base (`date_found_ged`), pour que le recalcul du script 02
reprenne la décision le soir même.

Historique
----------
Plus de fichier historique cumulatif : l'état vivant est porté par
`rptpsc.suivi_carte_tp`, où chaque carte conserve sa date de PREMIÈRE
découverte (les mises à jour sont gardées par `... IS NULL`).

Entrées
-------
- Input_Data/{PREFIX}_IEHE.csv           (obligatoire — source des KPEP réf.)
- Input_Data/{PREFIX}_TP_GED.csv         (obligatoire — extraction GED du jour)
- Input_Data/TP_GED_Corrections.csv      (optionnel — surcharge manuelle)
- rptpsc.suivi_carte_tp                  (obligatoire — LA population)
Le {PREFIX}_New_S.csv n'est plus lu ; seul son nom sert à détecter le préfixe.

Sorties
-------
- Output/{PREFIX}_REQ_TP_GED_KPEP_Part{N}.sql   (requêtes à exécuter sur la GED)
- rptpsc.suivi_carte_tp   (date_found_ged / date_dispo_ged / kpep_iehe / motif)

`rptpsc.suivi_tp_ged` n'est PLUS alimentée : une seule table porte l'état des
cartes. La courbe « Résorption TP GED » du dashboard lit `suivi_carte_tp`.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import date, datetime, timedelta
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

# `pipeline_runner` ajoute déjà Scripts/ au PYTHONPATH pour TOUTES les étapes,
# y compris celles de scriptsNewPipline/. Ce complément couvre le lancement
# manuel du script hors IHM.
_SCRIPTS_DIR = str(BASE_DIR / "Scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

# État vivant des cartes TP attendues — remplace le New_S comme source de la
# population interrogée. Sans lui, le script ne peut plus rien faire : ce n'est
# pas un import tolérant.
import suivi_carte_tp_db

# NOTE — La règle d'éligibilité (type assuré / offre / société) ne vit PLUS ici.
# Elle était dupliquée dans ce fichier sous une forme légèrement différente de
# celle du script 03 (ici « INP* », là-bas « IND* » + « INPPREVIND ») : deux
# règles pour une même question, qui se seraient tôt ou tard contredites.
# Désormais le script 03 décide de l'éligibilité et alimente
# `rptpsc.suivi_carte_tp` ; ce script ne fait que rechercher en GED ce que la
# table lui présente. Périmètre partagé : `Scripts/iehe_ko_lib.py`.

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
PG_USER     = os.environ.get("PG_USER", "rptpsc")
PG_PASSWORD = os.environ.get("PG_PASSWORD", "rptpsc_xx")
GED_SCHEMA = os.getenv("PG_SCHEMA", "rptpsc")
# `GED_TABLE = "suivi_tp_ged"` retiré : ce script n'écrit plus dans l'ancienne
# table. Tout l'état des cartes vit dans `rptpsc.suivi_carte_tp`.
# -----------------------------
# I/O utilitaires (alignés 03_generation_fichiers_detail.py)
# -----------------------------
# load the new_S file
def _detect_sep(path: Path, encoding: str) -> str:
    with open(path, "r", encoding=encoding, errors="replace") as fh:
        first_line = fh.readline()
    counts = {s: first_line.count(s) for s in (";", ",", "\t", "|")}
    best = max(counts, key=counts.get)
    return best if counts[best] > 0 else ","

def load_csv(path: Path, sep: Optional[str] = None) -> Optional[pd.DataFrame]:
    if path is None or not path.exists():
        return None
    for params in ({"encoding": "utf-8-sig"}, {"encoding": "utf-8"},
                   {"encoding": "latin-1"}, {"encoding": "cp1252"}):
        try:
            use_sep = sep if sep is not None else _detect_sep(path, params["encoding"])
            df = pd.read_csv(path, sep=use_sep, engine="python", dtype=str, **params)
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


# `apply_correction()` a été retirée : elle décidait d'un statut ligne à ligne
# dans un CSV, alors que la correction doit désormais modifier l'ÉTAT en base
# pour que le KPI la reprenne au recalcul. Sa règle de précédence de clé
# (exacte > joker offre > joker société+offre) est reprise à l'identique par
# `suivi_carte_tp_db.apply_corrections()`, qui applique les corrections de la
# moins spécifique à la plus spécifique — la dernière écriture l'emporte.


# -----------------------------
# RÉSOLUTION DU KPEP IEHE
# -----------------------------
def resolve_kpeps_manquants(
    conn,
    exact_idx: Dict[Tuple[str, str], str],
    fallback_idx: Dict[str, List[Tuple[str, str]]],
) -> Dict[str, int]:
    """
    Renseigne `kpep_iehe` sur les cartes qui n'en ont pas encore.

    ÉTAPE INDISPENSABLE, et non un simple enrichissement : le script 03 insère
    les cartes SANS KPEP (il n'a pas l'index IEHE par société), alors que
    `fetch_ged_pending()` exige `kpep_iehe IS NOT NULL`. Sans cette passe, la
    requête quotidienne ne renverrait jamais rien.

    Le KPEP retenu est celui d'IEHE, résolu par société — PAS celui du New_S.
    C'est le point que le métier a signalé explicitement : « la carte est indexée
    sur le KPEP d'IEHE, pas forcément le KPEP de l'affiliation ». L'ancienne
    version injectait le KPEP New_S dans la requête GED, ce qui faisait chercher
    la mauvaise clé dès que les deux différaient.

    Les personnes absentes d'IEHE reçoivent le motif `absent_iehe` et AUCUN
    KPEP : elles restent comptées au dénominateur mais ne partent pas en GED,
    où elles seraient introuvables par construction. Le script 06 les résoudra.
    """
    stats = {"resolues": 0, "absentes_iehe": 0, "fallback": 0}
    if conn is None:
        return stats

    a_traiter = suivi_carte_tp_db.fetch_sans_kpep(conn)
    if not a_traiter:
        return stats

    updates: List[Tuple[str, str, str, str, str]] = []
    for num_pers, code_soc, offre in a_traiter:
        kpep_iehe, motif = resolve_kpep_ref(num_pers, code_soc, exact_idx, fallback_idx)
        kpep_u = (kpep_iehe or "").strip().upper()
        if kpep_u:
            stats["resolues"] += 1
            if motif == "fallback_autre_societe":
                stats["fallback"] += 1
        else:
            stats["absentes_iehe"] += 1
        updates.append((num_pers, code_soc, offre, kpep_u, motif))

    suivi_carte_tp_db.set_kpep_iehe(conn, updates)
    return stats


def load_ged_resultats(prefix: str) -> List[Tuple[str, Optional[str]]]:
    """
    Lit le CSV déposé après exécution de la requête GED.

    Retourne des couples (kpep, tmstinj). `tmstinj` est la date d'injection
    réelle du document en GED ; elle répond à la question métier « date de
    génération ou date de mise à disposition ? ». Elle est facultative : les
    exports antérieurs à l'ajout de la colonne ne renvoient que `idepsp`, et le
    script continue de fonctionner sans elle.

    `exclude="RETRY"` écarte {prefix}_TP_GED_RETRY.csv, produit par le script 08
    et daté du jour lui aussi.
    """
    df_ged, _ = load_concat_csv_by_pattern(
        INPUT_DIR, f"{prefix}_TP_GED*.csv", label="GED", exclude="RETRY"
    )
    if df_ged is None or df_ged.empty:
        return []

    col_kpep = get_col(df_ged, ["idepsp", "idkpep", "kpep"])
    if col_kpep is None:
        print(f"❌ GED — aucune colonne KPEP trouvée. Colonnes lues : {df_ged.columns.tolist()}")
        return []
    col_tms = get_col(df_ged, ["tmstinj", "date_injection", "date_dispo"])

    resultats: List[Tuple[str, Optional[str]]] = []
    for _, r in df_ged.iterrows():
        kpep = str(r.get(col_kpep, "") or "").strip()
        if not kpep:
            continue
        tms = str(r.get(col_tms, "") or "").strip() if col_tms else ""
        resultats.append((kpep, tms or None))
    return resultats


# `append_historique()` a été retirée : elle était définie mais jamais appelée,
# et l'historique qu'elle prétendait tenir est désormais porté par
# `rptpsc.suivi_carte_tp`, où chaque carte garde sa date de première découverte.


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


# -----------------------------
# FENÊTRE tmstinj DE LA REQUÊTE GED
# -----------------------------
def build_ged_window(debut_inclus: date, fin_inclus: date) -> Tuple[str, str]:
    """
    Traduit une fenêtre métier INCLUSIVE [debut_inclus, fin_inclus] en les deux
    bornes à écrire dans le template GED.

    Dans la GED, `tmstinj` a une granularité "jour" et les comparateurs du
    template sont stricts : `tmstinj > 'D'` exclut toute la journée D, et
    `tmstinj < 'D'` l'exclut également. Pour que les deux journées extrêmes
    soient bien couvertes, on décale donc chaque borne d'un jour vers
    l'extérieur.

    Retourne (date_debut, date_fin) au format YYYYMMDD.
    """
    return (
        (debut_inclus - timedelta(days=1)).strftime("%Y%m%d"),
        (fin_inclus + timedelta(days=1)).strftime("%Y%m%d"),
    )


def compute_window_controle(min_eligibilite: Optional[date]) -> Tuple[date, date]:
    """
    Fenêtre métier (INCLUSIVE) du contrôle : [plus ancienne éligibilité non
    soldée, aujourd'hui].

    Remplace la fenêtre glissante d'origine ([flux - 7j plafonné au 1er du mois,
    dernier jour du mois]), qui perdait définitivement les cartes rétroactives :
    une carte due en mai et révélée par le flux d'août tombait hors requête, donc
    ne pouvait plus JAMAIS être retrouvée en GED — alors qu'elle reste comptée au
    dénominateur du KPI.

    C'est exactement la règle du script 08 (`compute_window_retry`) : la population
    des deux scripts est la même (`fetch_ged_pending`), la fenêtre doit l'être aussi.

    Le « -1 jour » demandé par le métier sur la borne basse est appliqué par
    `build_ged_window` : les comparateurs du template étant stricts, la requête
    finale s'écrit `tmstinj > (min_eligibilite - 1j)`, soit `>= min_eligibilite`.

    Si plus aucune carte n'est en attente, on retombe sur la journée courante.
    """
    aujourd_hui = date.today()
    debut = min_eligibilite if min_eligibilite is not None else aujourd_hui
    return min(debut, aujourd_hui), aujourd_hui


def write_ged_sql_batches(
    kpep_list: List[str],
    output_dir: Path,
    prefix: str,
    debut_inclus: date,
    fin_inclus: date,
) -> int:
    """
    Génère les fichiers SQL par batch pour interroger la GED sur les KPEP
    IEHE des cartes attendues (même mécanique que write_sql_batches du
    script 01, type 'simple').

    `kpep_list` ne contient QUE des KPEP IEHE résolus : les personnes absentes
    d'IEHE sont écartées en amont par `fetch_ged_pending()`. Auparavant elles
    étaient injectées ici avec leur KPEP d'affiliation, introuvable en GED par
    construction.

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

    date_debut, date_fin = build_ged_window(debut_inclus, fin_inclus)
    print(
        f"      Fenetre tmstinj : {debut_inclus:%d/%m/%Y} -> {fin_inclus:%d/%m/%Y} "
        f"(bornes SQL : > '{date_debut}' et < '{date_fin}')"
    )

    # Garde-fou déploiement : si seul le .py a été recopié et pas le template,
    # la fenêtre resterait celle codée en dur dans l'ancien .sql, sans erreur
    # visible — d'où l'avertissement explicite.
    if "__DATE_DEBUT__" not in base_sql_template or "__DATE_FIN__" not in base_sql_template:
        # Message volontairement en ASCII pur : il se déclenche typiquement
        # lors d'un déploiement partiel, y compris hors runner (console cp1252
        # où un emoji ferait planter le print).
        print(
            f"      [ATTENTION] Template {tpl_path.name} obsolete : balises "
            f"__DATE_DEBUT__ / __DATE_FIN__ absentes."
        )
        print("         -> La fenetre tmstinj restera celle codee en dur dans le template.")
        print("         -> Redeployez Input_Data/00-Export_GED_KPEP_With_Distinct.sql.")
    n_files = 0
    for i in range(0, len(kpeps), BATCH_SIZE_SQL):
        chunk = kpeps[i : i + BATCH_SIZE_SQL]
        batch_index = (i // BATCH_SIZE_SQL) + 1
        out_name = f"{prefix}_REQ_TP_GED_KPEP_Part{batch_index}.sql"
        values_str = "'" + "','".join(chunk) + "'"
        final_sql = (
            base_sql_template.replace("__LISTE_IDS__", values_str)
            .replace("__DATE_DEBUT__", date_debut)
            .replace("__DATE_FIN__", date_fin)
        )
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



def load_concat_csv_by_pattern(
    folder: Path,
    pattern: str,
    label: str = "",
    exclude: Optional[str] = None,
) -> Tuple[Optional[pd.DataFrame], int]:
    """`exclude` : motif (insensible à la casse) écarté des noms de fichiers."""
    files = sorted(folder.glob(pattern))
    if exclude:
        files = [f for f in files if exclude.lower() not in f.name.lower()]
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
        return psycopg.connect(host=host, port=port, dbname=db, user=PG_USER, password=PG_PASSWORD, connect_timeout=3)
  

#one connection
def connect_GED_auto():
    try:
        return connect_pg(PG_HOST, PG_PORT, PG_DB)
    except Exception as e:
        print(f"❌ GED connection failed")
        print(f"Host: {PG_HOST}")
        print(f"Port: {PG_PORT}")
        print(f"DB:   {PG_DB}")
        print(e)
        return None


# `SaveKpepFisrt_Attempt()`, `SaveKpep()` et `save_suivi_tp_ged()` ont ete
# retirees. Les deux premieres n'etaient jamais appelees ; la troisieme
# alimentait `rptpsc.suivi_tp_ged`, l'ANCIENNE table, uniquement pour la
# courbe « Resorption TP GED » du dashboard. Cette courbe lit desormais
# `suivi_carte_tp` : une seule table porte l'etat des cartes, plus aucune
# ecriture a tenir en double.


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
    path_iehe = INPUT_DIR / f"{prefix}_IEHE.csv"
    path_corr = INPUT_DIR / CORRECTIONS_FILE

    # Le New_S n'est PLUS la source de la population — c'est tout l'objet de la
    # refonte. Il n'est plus lu ici : le script 03 a déjà inséré les cartes du
    # jour dans `suivi_carte_tp` avant que cette étape ne démarre (ordre du
    # pipeline : 03 → 06 → 07). Seul son nom sert encore, via la détection du
    # préfixe ci-dessus.
    df_iehe = load_csv(path_iehe, sep=None)
    if df_iehe is None or df_iehe.empty:
        print(f"⚠️ IEHE absent ou vide ({path_iehe.name}) : KPEP de référence non résolus.")
    else:
        print(f"   ✅ IEHE chargé : {len(df_iehe)} lignes")

    corrections = load_corrections(path_corr)

    # --- Index KPEP IEHE par (personne, société) ---
    exact_idx, fallback_idx = build_iehe_kpep_index(df_iehe)
    print(
        f"   ✅ Index IEHE : {len(exact_idx)} clé(s) exacte(s), "
        f"{len(fallback_idx)} personne(s) indexée(s)"
    )

    # --- Table d'état des cartes attendues ---
    conn_cartes = suivi_carte_tp_db.connect_supervision()
    if conn_cartes is None or not suivi_carte_tp_db.ensure_table(conn_cartes):
        print("❌ Table suivi_carte_tp indisponible : contrôle GED impossible.")
        return 1

    try:
        # --- 1. Résolution des KPEP IEHE manquants ---
        stats_kpep = resolve_kpeps_manquants(conn_cartes, exact_idx, fallback_idx)
        print(f"   ✅ KPEP IEHE : {stats_kpep['resolues']} résolu(s) "
              f"(dont {stats_kpep['fallback']} par fallback société), "
              f"{stats_kpep['absentes_iehe']} personne(s) encore absente(s) d'IEHE")

        # --- 2. Population à interroger : le jour + l'antériorité ---
        #
        # Une seule requête, pas d'union : les cartes du jour ont été insérées
        # par le script 03, celles des flux antérieurs n'ont jamais quitté la
        # table. Les cartes futures en sont exclues par `date_eligibilite <=
        # CURRENT_DATE` et reviendront d'elles-mêmes le jour de leur échéance.
        #
        # Les personnes sans KPEP IEHE sont exclues ici : la GED est indexée sur
        # ce KPEP, les chercher sans lui ne peut rien donner. Elles restent
        # comptées au dénominateur du KPI.
        kpeps_attendus = suivi_carte_tp_db.fetch_ged_pending(conn_cartes)
        if not kpeps_attendus:
            print("\n[INFO] Aucune carte à rechercher aujourd'hui.")
            return 0
        print(f"   📋 {len(kpeps_attendus)} carte(s) à rechercher en GED "
              f"(affiliations du jour + antériorité non soldée)")

        # --- 3. Génération des requêtes SQL GED ---
        #
        # La fenêtre `tmstinj` est dérivée des DONNÉES (plus ancienne éligibilité
        # encore en attente), pas de la date du flux : le backlog remonte à des flux
        # bien antérieurs, qu'une fenêtre calée sur le mois du flux excluait.
        debut_inclus, fin_inclus = compute_window_controle(
            suivi_carte_tp_db.fetch_min_eligibilite_pending(conn_cartes)
        )
        write_ged_sql_batches(
            kpeps_attendus, OUTPUT_DIR, prefix, debut_inclus, fin_inclus,
        )

        input(f"\nAppuyez sur Entrée une fois le fichier déposé — "
              f"il doit s'appeler {prefix}_TP_GED.csv")

        # --- 4. Dépouillement ---
        resultats = load_ged_resultats(prefix)
        if not resultats:
            print("⚠️ Aucun résultat GED exploitable : aucune carte n'est soldée.")
            return 1

        today = date.today()
        nb_soldees = suivi_carte_tp_db.mark_ged_found(conn_cartes, resultats, today)
        nb_horodatees = suivi_carte_tp_db.mark_checked(conn_cartes, today)
        print(f"   ✅ {len(resultats)} KPEP trouvé(s) en GED → "
              f"{nb_soldees} carte(s) soldée(s), {nb_horodatees} encore en attente")

        # --- 5. Corrections manuelles ---
        #
        # Elles étaient chargées puis IGNORÉES : `corrections` était un paramètre
        # de `build_detail` jamais lu dans son corps. Le métier retrouve des
        # cartes à la main et veut que l'indicateur soit juste le soir même :
        # elles doivent donc modifier l'ÉTAT, pas seulement l'affichage.
        if corrections:
            bilan = suivi_carte_tp_db.apply_corrections(conn_cartes, corrections, today)
            print(f"   ✅ Corrections manuelles appliquées : "
                  f"{bilan['rapproches']} forcée(s) rapprochée(s), "
                  f"{bilan['non_rapproches']} forcée(s) non rapprochée(s)")

        return 0
    finally:
        try:
            conn_cartes.close()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    sys.exit(main())
    