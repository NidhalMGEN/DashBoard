# -*- coding: utf-8 -*-
"""
03_generation_fichiers_detail.py (PROD GRADE) — MATCHING CIAM ALIGNÉ SCRIPT 02 + OPTION B (SET-BASED)
===================================================================================================

Objectif
--------
Générer NS_CIAM (CSV) en conservant la logique de labels/colonnes existante,
tout en appliquant la même logique de rapprochement CIAM que le script 02_calcul_kpi.py :

  Mail_CIAM -> Valeur_Coord -> KPEP -> Identite_Full -> Identite_Inverted -> Recherche_Large_Nom -> Recherche_Large_Middle
  - arrêt immédiat au 1er match (First Match Wins)
  - SANS écrasement

Option B (Set-based per person)
-------------------------------
Le matching est calculé au niveau "compte" (= num_personne), mais au lieu de ne tester qu'une
ligne représentante (drop_duplicates), on teste l'ensemble des valeurs candidates (emails, kpeps, identités)
disponibles sur toutes les lignes du même num_personne.

=> corrige les "NON_RAPPROCHE" causés par multi-KPEP / multi-emails selon les contrats/lignes.
=> résultat déterministe (ne dépend plus de l'ordre des lignes).

Colonnes du fichier NS_CIAM générées
-------------------------------------
1. Colonnes de base New_S (depuis code_soc_appart jusqu'à offre)
2. CIAM Last Nom, CIAM Middle Nom, CIAM Prénom, CIAM Date de naissance,
   CIAM KPEP, CIAM Email, CIAM Email Other, CIAM Téléphone
3. Rapproché / Non rapproché, Donnée rapprochée
4. Nombre de KPEP, Email CIAM = Email coordonnées
5. IEHE Présent, IEHE KPEP, IKPEP IEHE ≠ KPEP CIAM
6. Eligibilité TP, Date éligibilité TP, Valeur carte TP

Entrées (Input_Data)
--------------------
- {PREFIX}_New_S.csv  (PREFIX = DDMMYYYY)
- {PREFIX}_CK*.csv    (support batching: CK, CK1, CK2...)
- {PREFIX}_Last*.csv  (optionnel)
- {PREFIX}_Middle*.csv (optionnel)
- {PREFIX}_IEHE.csv   (optionnel)

Sorties (Output)
----------------
- {PREFIX}_NS_CIAM.csv
"""

from __future__ import annotations

import argparse
import os
import re
import unicodedata
import warnings
from datetime import date, datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd


# -----------------------------
# CONFIG
# -----------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = SCRIPT_DIR if (SCRIPT_DIR / "Input_Data").exists() else SCRIPT_DIR.parent
INPUT_DIR = BASE_DIR / "Input_Data"
OUTPUT_DIR = BASE_DIR / "Output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
INPUT_DIR.mkdir(parents=True, exist_ok=True)

# Types assurés inclus dans le périmètre CIAM (et TP)
TYPES_ASSURES_AI = ["ASSPRI", "MPRETR", "MPVRET", "MPACTI"]

# Types assurés éligibles carte TP
TYPES_TP = ["ASSPRI", "MPACTI", "MPRETR", "MPVRET"]

# Offres PREV explicitement non éligibles TP (en plus des préfixes MEP/IND)
OFFRES_PREV_NON_ELIGIBLES = {"INPPREVIND"}



# -----------------------------
# UTILITAIRES I/O
# -----------------------------
def _load_wrapped_csv(path: Path, encoding: str = "utf-8") -> Optional[pd.DataFrame]:
    """
    Certains exports ont une ligne entière encapsulée entre guillemets
    (CSV "double-quoté"), ce qui est lu en 1 seule colonne.
    """
    try:
        raw = path.read_text(encoding=encoding, errors="ignore").splitlines()
        cleaned = []
        for line in raw:
            s = line.rstrip("\r\n")
            if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
                s = s[1:-1].replace('""', '"')
            cleaned.append(s)
        return pd.read_csv(StringIO("\n".join(cleaned)), sep=",", engine="python", dtype=str)
    except Exception:
        return None


def load_csv(path: Path, sep: Optional[str] = None) -> Optional[pd.DataFrame]:
    if path is None or not path.exists():
        return None
    try:
        df = pd.read_csv(path, sep=sep, engine="python", dtype=str, encoding="utf-8-sig")
        if df is not None and len(df.columns) == 1:
            header = str(df.columns[0])
            if ("," in header) and ('""' in header or '","' in header):
                fixed = _load_wrapped_csv(path, encoding="utf-8-sig")
                if fixed is not None:
                    df = fixed
        return df
    except UnicodeDecodeError:
        df = pd.read_csv(path, sep=sep, engine="python", dtype=str, encoding="latin-1")
        if df is not None and len(df.columns) == 1:
            header = str(df.columns[0])
            if ("," in header) and ('""' in header or '","' in header):
                fixed = _load_wrapped_csv(path, encoding="latin-1")
                if fixed is not None:
                    df = fixed
        return df
    except Exception as e:
        print(f"❌ Erreur lecture CSV {path.name}: {e}")
        return None


def normalize_cols(df: Optional[pd.DataFrame]) -> Optional[pd.DataFrame]:
    if df is None:
        return None
    df.columns = [str(c).strip().lower() for c in df.columns]
    return df


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


# -----------------------------
# FLEX COLS
# -----------------------------
def get_col_flexible(df: pd.DataFrame, candidates: List[str]) -> Optional[str]:
    if df is None:
        return None
    cols = {c.lower() for c in df.columns}
    for cand in candidates:
        if cand.lower() in cols:
            return cand.lower()
    return None


# -----------------------------
# NORMALISATION (alignée script 02)
# -----------------------------
def normalize_text(text: Any) -> str:
    if pd.isna(text) or text == "":
        return ""
    s = str(text).upper().strip()
    s = unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode("utf-8")
    return s


def normalize_email(text: Any) -> str:
    if pd.isna(text) or text == "":
        return ""
    s = str(text).strip().lower()
    if s in ("null", "nan", "none"):
        return ""
    return s

# Extraction robuste d'emails (support multi-valeurs)
EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)

# Validation de format email (ancrée début + fin)
EMAIL_VALID_RE = re.compile(r"^[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}$", re.IGNORECASE)

# Domaines email à risque (jetables, temporaires, non professionnels)
RISKY_EMAIL_DOMAINS: frozenset = frozenset({
    "yopmail.com", "mailinator.com", "guerrillamail.com", "throwam.com",
    "sharklasers.com", "grr.la", "spam4.me", "trashmail.com",
    "dispostable.com", "fakeinbox.com", "mailnull.com", "maildrop.cc",
    "spamgourmet.com", "spamgourmet.net", "spamgourmet.org",
    "gmx.com", "gmx.fr", "gmx.de", "gmx.net",
    "jetable.fr.nf", "jetable.net", "jetable.org",
    "example.com", "test.com", "noemail.com",
})

def extract_emails(value: Any) -> List[str]:
    if pd.isna(value) or value == "":
        return []
    s = str(value).strip()
    if not s:
        return []
    found = EMAIL_RE.findall(s)
    if not found and "@" in s:
        found = [s]
    cleaned = []
    seen = set()
    for e in found:
        ne = normalize_email(e)
        if ne and ne not in seen:
            cleaned.append(ne)
            seen.add(ne)
    return cleaned


def format_date_iso(value: Any) -> str:
    if pd.isna(value) or value == "":
        return ""
    s = str(value).strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return s
    for fmt in ("%d/%m/%Y", "%d-%m-%Y"):
        try:
            return pd.to_datetime(s, format=fmt, errors="raise").strftime("%Y-%m-%d")
        except Exception:
            pass
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            dt = pd.to_datetime(s, dayfirst=True, errors="coerce")
        if pd.isna(dt):
            return ""
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return ""


# -----------------------------
# PREPROCESS New_S (NS_* identiques au script 02)
# -----------------------------
def preprocess_new_s(df: Optional[pd.DataFrame]) -> Optional[pd.DataFrame]:
    if df is None:
        return None

    col_nom = get_col_flexible(df, ["nom_long", "nom", "nom_assure", "lastname"])
    col_prenom = get_col_flexible(df, ["prenom", "firstname", "prenom_assure"])
    col_ddn = get_col_flexible(df, ["date_naissance", "birthdate", "ddn"])
    col_email = get_col_flexible(df, ["mailciam", "mail_ciam", "email", "mail"])
    col_kpep = get_col_flexible(df, ["idkpep", "kpep", "id_kpep"])
    col_offre = get_col_flexible(df, ["offre", "code_offre", "libelle_offre", "nom_offre"])
    col_soc = get_col_flexible(df, ["code_soc_appart", "code_societe", "societe", "code_soc"])
    col_rad = get_col_flexible(df, ["dateradassure", "date_radiation", "date_rad"])
    col_adh = get_col_flexible(df, ["date_adhesion", "date_entree", "date_adh"])
    col_eff = get_col_flexible(df, ["date_effet_adhesion", "date_effet", "date_eff"])
    col_val = get_col_flexible(df, ["valeur_coordonnee", "valeur_coordonnees", "valeur coordonnee"])

    # NS_offre (aligné script 02)
    if col_offre:
        df["NS_offre"] = df[col_offre].astype(str).str.upper().str.strip()
    else:
        df["NS_offre"] = ""

    df["NS_societe"] = df[col_soc].astype(str).str.strip() if col_soc else ""

    df["NS_nom"] = df[col_nom].apply(normalize_text) if col_nom else ""
    df["NS_prenom"] = df[col_prenom].apply(normalize_text) if col_prenom else ""
    df["NS_ddn"] = df[col_ddn].apply(format_date_iso) if col_ddn else ""

    df["src_email_ciam"] = df[col_email].astype(str).str.strip() if col_email else ""
    df["NS_email_ciam"] = df[col_email].apply(normalize_email) if col_email else ""
    df["NS_kpep"] = df[col_kpep].apply(lambda x: str(x).strip() if pd.notna(x) else "") if col_kpep else ""

    df["NS_date_rad"] = df[col_rad].apply(format_date_iso) if col_rad else ""
    df["NS_date_adh"] = df[col_adh].apply(format_date_iso) if col_adh else ""
    df["NS_date_effet"] = df[col_eff].apply(format_date_iso) if col_eff else df["NS_date_adh"]

    def clean_val_coord(val: Any) -> str:
        s = str(val).strip()
        if s.lower() in ("", "null", "nan", "none"):
            return ""
        return normalize_email(s) if "@" in s else ""

    df["src_email_val"] = df[col_val].astype(str).str.strip() if col_val else ""
    df["NS_email_val"] = df[col_val].apply(clean_val_coord) if col_val else ""
    df["NS_identite_full"] = df["NS_nom"] + "|" + df["NS_prenom"] + "|" + df["NS_ddn"]
    return df


# -----------------------------
# KEYCLOAK DATA (aligné script 02)
# -----------------------------
def prepare_keycloak_data(
    df_ck: Optional[pd.DataFrame],
    df_cm: Optional[pd.DataFrame],
    df_last: Optional[pd.DataFrame],
    df_last_prenom: Optional[pd.DataFrame],
    df_middle: Optional[pd.DataFrame],
    df_middle_prenom: Optional[pd.DataFrame],
) -> Dict[str, pd.DataFrame]:
    data: Dict[str, pd.DataFrame] = {}

    def prep(df: pd.DataFrame) -> pd.DataFrame:
        c_last = get_col_flexible(df, ["last_name", "nom"])
        c_first = get_col_flexible(df, ["first_name", "prenom"])
        c_birth = get_col_flexible(df, ["birthdate", "date_naissance"])

        df["nom_norm"] = df[c_last].apply(normalize_text) if c_last else ""
        df["prenom_norm"] = df[c_first].apply(normalize_text) if c_first else ""
        df["ddn_norm"] = df[c_birth].apply(format_date_iso) if c_birth else ""
        return df

    sources_all: List[pd.DataFrame] = []
    if df_ck is not None and not df_ck.empty:
        sources_all.append(df_ck.copy())
    if df_cm is not None and not df_cm.empty:
        sources_all.append(df_cm.copy())

    if sources_all:
        df_all = pd.concat(sources_all, ignore_index=True)
        if "id" in df_all.columns:
            dedupe_cols = ["id", "realm_id"] if "realm_id" in df_all.columns else ["id"]
            df_all = df_all.drop_duplicates(subset=dedupe_cols, keep="first")

        # Aligné script 02 : email/idkpep normalisés
        if "email" in df_all.columns:
            df_all["email"] = df_all["email"].apply(normalize_email)
        else:
            df_all["email"] = ""
        if "email_other" in df_all.columns:
            df_all["email_other"] = df_all["email_other"].apply(normalize_email)

        if "idkpep" in df_all.columns:
            df_all["idkpep"] = df_all["idkpep"].apply(lambda x: str(x).strip() if pd.notna(x) else "")
        else:
            df_all["idkpep"] = ""

        df_all = prep(df_all)
        df_all["cle_identite"] = df_all["nom_norm"] + "|" + df_all["prenom_norm"] + "|" + df_all["ddn_norm"]
        data["all"] = df_all
    else:
        data["all"] = pd.DataFrame(columns=["id", "email", "email_other", "idkpep", "cle_identite"])

    if df_last is not None and not df_last.empty:
        df_last = prep(df_last)
        df_last["cle_nom_ddn"] = df_last["nom_norm"] + "|" + df_last["ddn_norm"]
        data["last"] = df_last
    else:
        data["last"] = pd.DataFrame(columns=["id", "cle_nom_ddn"])

    if df_middle is not None and not df_middle.empty:
        df_middle = prep(df_middle)
        df_middle["cle_nom_ddn_middle"] = df_middle["nom_norm"] + "|" + df_middle["ddn_norm"]
        data["middle"] = df_middle
    else:
        data["middle"] = pd.DataFrame(columns=["id", "cle_nom_ddn_middle"])

    data["last_prenom"] = df_last_prenom if df_last_prenom is not None else pd.DataFrame()
    data["middle_prenom"] = df_middle_prenom if df_middle_prenom is not None else pd.DataFrame()
    return data


# -----------------------------
# OPTION B: VUE PERSONNE (set-based)
# -----------------------------
def _clean_email_for_match(v: Any) -> str:
    return normalize_email(v)


def build_person_view(df: pd.DataFrame, col_pers: str) -> pd.DataFrame:
    """
    1 ligne = 1 personne (col_pers) avec listes triées/déterministes des candidats.
    Requiert que preprocess_new_s ait créé les colonnes NS_*.
    """
    required = ["NS_email_ciam", "NS_email_val", "NS_kpep", "NS_nom", "NS_prenom", "NS_ddn"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise RuntimeError(f"build_person_view: colonnes NS_* manquantes: {missing}")

    rows: List[Dict[str, Any]] = []

    # groupby dropna=False pour éviter de perdre un id vide (au cas où)
    for pid, g in df.groupby(col_pers, dropna=False):
        emails_ciam_set: set[str] = set()
        for v in g["NS_email_ciam"].tolist():
            for e in extract_emails(v):
                emails_ciam_set.add(e)
        emails_val_set: set[str] = set()
        for v in g["NS_email_val"].tolist():
            for e in extract_emails(v):
                emails_val_set.add(e)
        emails_ciam = sorted(emails_ciam_set)
        emails_val = sorted(emails_val_set)

        kpeps = sorted({ str(x).strip() for x in g["NS_kpep"].tolist() if pd.notna(x) and str(x).strip() not in ("", "NULL", "null") })

        ident_full: set[str] = set()
        ident_inv: set[str] = set()
        nom_ddn: set[str] = set()

        for nom, prenom, ddn in zip(g["NS_nom"].tolist(), g["NS_prenom"].tolist(), g["NS_ddn"].tolist()):
            nom = str(nom or "").strip()
            prenom = str(prenom or "").strip()
            ddn = str(ddn or "").strip()

            if nom and prenom and ddn:
                ident_full.add(f"{nom}|{prenom}|{ddn}")
                ident_inv.add(f"{prenom}|{nom}|{ddn}")

            if nom and ddn:
                nom_ddn.add(f"{nom}|{ddn}")

        rows.append(
            {
                col_pers: pid,
                "emails_ciam": sorted(ident for ident in emails_ciam),
                "emails_val": sorted(ident for ident in emails_val),
                "kpeps": kpeps,
                "ident_full": sorted(ident_full),
                "ident_inv": sorted(ident_inv),
                "nom_ddn": sorted(nom_ddn),
            }
        )

    return pd.DataFrame(rows)


# -----------------------------
# MATCHING ENGINE (aligné script 02 + find_match_person Option B)
# -----------------------------
class MatchingEngine:
    def __init__(self, keycloak_data: Dict[str, pd.DataFrame]):
        self.kc = keycloak_data
        self.idx_kpep = self._build_index(self.kc["all"], "idkpep")
        self.idx_email = self._build_email_index(self.kc["all"])
        self.idx_identite_full = self._build_index(self.kc["all"], "cle_identite")
        self.idx_large_nom_ddn = self._build_index(self.kc["last"], "cle_nom_ddn")
        self.idx_middle = self._build_index(self.kc["middle"], "cle_nom_ddn_middle")

    def _build_index(self, df: pd.DataFrame, col_name: str) -> Dict[str, List[str]]:
        if df is None or col_name not in df.columns or "id" not in df.columns:
            return {}
        valid = df[df[col_name].fillna("").astype(str) != ""]
        if valid.empty:
            return {}
        return valid.groupby(col_name)["id"].apply(list).to_dict()

    def _build_email_index(self, df: pd.DataFrame) -> Dict[str, List[str]]:
        if df is None or "id" not in df.columns:
            return {}
        email_cols = [c for c in ["email", "email_other"] if c in df.columns]
        if not email_cols:
            return {}
        parts = []
        for c in email_cols:
            tmp = df[["id", c]].copy()
            tmp = tmp.rename(columns={c: "email_raw"})
            parts.append(tmp)
        tmp_all = pd.concat(parts, ignore_index=True)
        tmp_all["id"] = tmp_all["id"].fillna("").astype(str)
        tmp_all["email_raw"] = tmp_all["email_raw"].fillna("").astype(str)
        tmp_all = tmp_all[tmp_all["id"] != ""]
        tmp_all["email_norm"] = tmp_all["email_raw"].apply(extract_emails)
        tmp_all = tmp_all.explode("email_norm")
        tmp_all = tmp_all[tmp_all["email_norm"].notna() & (tmp_all["email_norm"] != "")]
        if tmp_all.empty:
            return {}
        return tmp_all.groupby("email_norm")["id"].apply(list).to_dict()

    def _pick_first(self, ids: List[str]) -> Optional[str]:
        return ids[0] if ids else None

    def _match_email(self, email_value: str) -> Optional[str]:
        if not email_value:
            return None
        return self._pick_first(self.idx_email.get(email_value, []))

    def _match_kpep(self, kpep: str) -> Optional[str]:
        if not kpep:
            return None
        return self._pick_first(self.idx_kpep.get(str(kpep), []))

    def _match_identite_stricte_key(self, identite_full: str) -> Optional[str]:
        # identite_full doit être complète (pas de segment vide)
        if not identite_full or "||" in identite_full:
            return None
        return self._pick_first(self.idx_identite_full.get(identite_full, []))

    def _match_recherche_large_nom_key(self, nom_ddn_key: str) -> Optional[str]:
        if not nom_ddn_key or "|" not in nom_ddn_key:
            return None
        return self._pick_first(self.idx_large_nom_ddn.get(nom_ddn_key, []))

    def _match_recherche_large_middle_key(self, nom_ddn_key: str) -> Optional[str]:
        if not nom_ddn_key or "|" not in nom_ddn_key:
            return None
        return self._pick_first(self.idx_middle.get(nom_ddn_key, []))

    # --- Option B: set-based per person
    def find_match_person(self, person_row: pd.Series) -> Tuple[str, str, Optional[str], Optional[str]]:
        """
        Matching set-based au niveau personne.
        Ordre strict :
          Mail_CIAM -> Valeur_Coord -> KPEP -> Identite_Full -> Identite_Inverted -> Recherche_Large_Nom -> Recherche_Large_Middle
        """
        # 1) Mail_CIAM (tous les emails)
        for mail in (person_row.get("emails_ciam") or []):
            res = self._match_email(mail)
            if res:
                return "RAPPROCHE", "Mail_CIAM", res, mail

        # 2) Valeur_Coord (tous les emails valeur_coordonnee)
        for mail in (person_row.get("emails_val") or []):
            res = self._match_email(mail)
            if res:
                return "RAPPROCHE", "Valeur_Coord", res, mail

        # 3) KPEP (tous les kpep)
        for kpep in (person_row.get("kpeps") or []):
            res = self._match_kpep(kpep)
            if res:
                return "RAPPROCHE", "KPEP", res, kpep

        # 4) Identité Full
        for key in (person_row.get("ident_full") or []):
            res = self._match_identite_stricte_key(key)
            if res:
                return "RAPPROCHE", "Identite_Full", res, key

        # 5) Identité inversée (on réutilise idx_identite_full)
        for key in (person_row.get("ident_inv") or []):
            res = self._match_identite_stricte_key(key)
            if res:
                return "RAPPROCHE", "Identite_Inverted", res, key

        # 6) Recherche Large Nom (Nom|DDN)
        for key in (person_row.get("nom_ddn") or []):
            res = self._match_recherche_large_nom_key(key)
            if res:
                return "RAPPROCHE", "Recherche_Large_Nom", res, key

        # 7) Recherche Large Middle (Nom|DDN)
        for key in (person_row.get("nom_ddn") or []):
            res = self._match_recherche_large_middle_key(key)
            if res:
                return "RAPPROCHE", "Recherche_Large_Middle", res, key

        return "NON_RAPPROCHE", "AUCUNE", None, None


# -----------------------------
# CARTE TP — calcul par ligne
# -----------------------------
def compute_carte_tp_row(
    type_assure: str,
    offre: str,
    code_soc: str,
    date_effet_str: str,
    date_adh_str: str,
    eligibility_mode: str = "adh_plus_21",
) -> Tuple[str, str, str, str]:
    """
    Retourne (eligibilite_O_N, date_eligibilite_str, valeur_carte_tp, raison_non_eligibilite).

    Logique (alignée script 02) :
    - Périmètre : TYPES_TP, hors offres PREV (préfixe MEP/IND) et hors société 073
    - delta = date_effet_adhesion - date_adhesion
    - ELIGIBLE si delta <= 21j (valeurs négatives comprises) → valeur = ""
    - FUTURE   si delta >= 22j                               → valeur = "Futur"

    eligibility_mode (date affichée dans la colonne "Date éligibilité TP") :
    - "adh_plus_21"   (défaut, fichiers NS_CIAM et IEHE_KO) : date_adhesion + 21 jours
    - "effet_minus_21" (fichier NS_IEHE)                    : date_effet_adhesion - 21 jours
    """
    if type_assure not in TYPES_TP:
        return "N", "", "", f"Type assuré non éligible ({type_assure})"

    # Exclusion PREV : offres MEP/IND/INPPREVIND ou société 073
    # Offres PREV explicitement non éligibles TP (demande métier)
    if (
        offre.startswith(("MEP", "IND"))
        or offre in OFFRES_PREV_NON_ELIGIBLES
        or code_soc == "073"
    ):
        return "N", "", "", "Offre PREV exclue"

    try:
        d_adh = datetime.strptime(date_adh_str, "%Y-%m-%d").date()
    except ValueError:
        d_adh = None

    try:
        d_eff = datetime.strptime(date_effet_str, "%Y-%m-%d").date() if date_effet_str else None
    except ValueError:
        d_eff = None

    if eligibility_mode == "effet_minus_21":
        elig_date_str = (d_eff - timedelta(days=21)).strftime("%d/%m/%Y") if d_eff else ""
    else:
        elig_date_str = (d_adh + timedelta(days=21)).strftime("%d/%m/%Y") if d_adh else ""

    if not date_effet_str:
        return "O", elig_date_str, "Futur", ""

    if d_eff is None:
        # date_effet illisible : aligné sur le cas "absente" → Futur (et non Eligible)
        return "O", elig_date_str, "Futur", ""

    if d_adh is None:
        # pas de date_adh → impossible de calculer le delta → considéré futur par défaut
        return "O", elig_date_str, "Futur", ""

    delta = (d_eff - d_adh).days
    if delta <= 21:
        return "O", elig_date_str, "", ""
    else:
        return "O", elig_date_str, "Futur", ""


# -----------------------------
# MAIN
# -----------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="Génération NS_CIAM (matching aligné script 02)")
    parser.add_argument(
        "--sep",
        default=None,
        help="Séparateur CSV de sortie (défaut: ';' ou OUTPUT_SEP).",
    )
    parser.add_argument(
        "--debug-email",
        action="store_true",
        help="Exporte un CSV d'audit des NON_RAPPROCHE avec emails présents en index CIAM.",
    )
    parser.add_argument(
        "--no-debug-email",
        action="store_true",
        help="Désactive l'audit email (si activé par défaut).",
    )
    args = parser.parse_args()

    output_sep = args.sep if args.sep is not None else os.getenv("OUTPUT_SEP", ";")
    if not isinstance(output_sep, str) or len(output_sep) != 1:
        print(f"⚠️ Séparateur invalide '{output_sep}'. Utilisation ';' par défaut.")
        output_sep = ";"

    if args.debug_email:
        debug_match_email = True
    elif args.no_debug_email:
        debug_match_email = False
    else:
        debug_match_email = os.getenv("DEBUG_MATCH_EMAIL", "1") == "1"

    if not INPUT_DIR.exists():
        print(f"❌ Dossier Input_Data introuvable: {INPUT_DIR}")
        return

    ns_path, prefix = find_latest_new_s(INPUT_DIR)
    if not ns_path or not prefix:
        print("❌ Aucun fichier New_S trouvé.")
        return

    # Date de référence TP = date du flux (aligné script 02)
    try:
        date_flux = datetime.strptime(prefix, "%d%m%Y").date()
    except ValueError:
        date_flux = date.today()
        print(f"⚠️ Préfixe '{prefix}' non parseable, utilisation date du jour pour TP.")

    print(f"📂 Génération NS_CIAM : {prefix} (date référence TP : {date_flux.strftime('%d/%m/%Y')})")

    # --- Chargement ---
    df_ns_raw = load_csv(ns_path, sep=None)
    if df_ns_raw is None or df_ns_raw.empty:
        print("❌ New_S vide ou illisible.")
        return
    df_ns_raw = normalize_cols(df_ns_raw)

    # Colonnes originales New_S — capturées AVANT le prétraitement NS_*
    original_ns_cols = list(df_ns_raw.columns)

    df_iehe = normalize_cols(load_csv(INPUT_DIR / f"{prefix}_IEHE.csv", sep=None))

    df_ck, _ = load_concat_csv_by_pattern(INPUT_DIR, f"{prefix}*CK*.csv", label="CK")
    df_cm, _ = load_concat_csv_by_pattern(INPUT_DIR, f"{prefix}*CM*.csv", label="CM")
    df_last, _ = load_concat_csv_by_pattern(INPUT_DIR, f"{prefix}*Last*.csv", label="Rech_Last")
    df_middle, _ = load_concat_csv_by_pattern(INPUT_DIR, f"{prefix}*Middle*.csv", label="Rech_Middle")

    if (df_ck is None or df_ck.empty) and (df_cm is None or df_cm.empty):
        print("❌ CK/CM vides ou introuvables : matching impossible.")
        return

    # --- Prétraitement ---
    df_ns = preprocess_new_s(df_ns_raw)

    col_type = get_col_flexible(df_ns, ["type_assure", "typeassure"])
    col_pers = get_col_flexible(df_ns, ["num_personne", "numpersonne", "num_pers", "id_personne"])
    col_ctr = get_col_flexible(df_ns, ["num_ctr_indiv", "contrat"])

    if not col_type or not col_pers:
        print("❌ Colonnes requises manquantes dans New_S (type_assure / num_personne).")
        return

    # Périmètre : assurés AI uniquement
    mask_assure = df_ns[col_type].isin(TYPES_ASSURES_AI)
    mask_conjoint = df_ns[col_type].str.contains("CONJ", na=False)

    df_work = df_ns[mask_assure].copy()

    if df_work.empty:
        print("❌ Aucun assuré AI trouvé après filtrage (TYPES_ASSURES_AI).")
        return

    # --- Matching CIAM (Option B set-based) ---
    kc_data = prepare_keycloak_data(df_ck, df_cm, df_last, None, df_middle, None)
    engine = MatchingEngine(kc_data)

    df_target = build_person_view(df_work, col_pers)
    if df_target.empty:
        print("❌ build_person_view a retourné 0 compte.")
        return

    results = df_target.apply(engine.find_match_person, axis=1)
    df_target["match_status"] = [x[0] for x in results]
    df_target["match_method"] = [x[1] for x in results]
    df_target["matched_id"] = [x[2] for x in results]
    df_target["matched_key"] = [x[3] for x in results]

    # Debug audit email (optionnel)
    if debug_match_email:
        def _hits_email_idx(vals):
            if not isinstance(vals, list):
                return []
            return [v for v in vals if v in engine.idx_email]

        dbg = df_target[df_target["match_status"] == "NON_RAPPROCHE"].copy()
        dbg["emails_val_hits"] = dbg["emails_val"].apply(_hits_email_idx)
        dbg["emails_ciam_hits"] = dbg["emails_ciam"].apply(_hits_email_idx)
        suspicious = dbg[(dbg["emails_val_hits"].apply(len) > 0) | (dbg["emails_ciam_hits"].apply(len) > 0)]
        audit_path = OUTPUT_DIR / f"{prefix}_AUDIT_DEBUG_EMAIL.csv"
        suspicious[
            [col_pers, "emails_ciam", "emails_val", "emails_ciam_hits", "emails_val_hits", "match_status"]
        ].to_csv(audit_path, index=False, sep=output_sep, encoding="utf-8-sig")
        print(f"[DEBUG_MATCH_EMAIL] NON_RAPPROCHE avec emails présents en index: {len(suspicious)}")
        print(f"[DEBUG_MATCH_EMAIL] Audit exporté : {audit_path.name}")

    # Propagation sur toutes les lignes assurés
    df_work = df_work.merge(
        df_target[[col_pers, "match_status", "match_method", "matched_id", "matched_key"]],
        on=col_pers,
        how="left",
    )

    # --- Flags complémentaires ---
    ids_assures = set(df_ns[mask_assure][col_pers].dropna().unique())
    ids_conjoints = set(df_ns[mask_conjoint][col_pers].dropna().unique())
    ids_assure_conjoint = ids_assures.intersection(ids_conjoints)
    df_work["Flag_Assure_ET_Conjoint_Meme_ID"] = df_work[col_pers].isin(ids_assure_conjoint).astype(int)

    kpep_clean = df_work["NS_kpep"].astype(str).str.strip().replace({"": np.nan, "NULL": np.nan, "null": np.nan})
    df_work["Nombre de KPEP"] = (
        kpep_clean.groupby(df_work[col_pers]).transform("nunique").fillna(0).astype(int)
    )

    ids_doublons_meme_offre = set()
    if col_ctr:
        df_ai = df_ns[mask_assure].copy()
        grp = df_ai.groupby(col_pers).agg(
            contrats_nunique=(col_ctr, "nunique"),
            offres_nunique=("NS_offre", "nunique"),
        )
        ids_doublons_meme_offre = set(grp[(grp["contrats_nunique"] > 1) & (grp["offres_nunique"] == 1)].index)
    df_work["Flag_Doublons_Contrats_Meme_Offre"] = df_work[col_pers].isin(ids_doublons_meme_offre).astype(int)

    # --- Enrichissement CIAM depuis les sources keycloak ---
    ref_sources: List[pd.DataFrame] = []
    for key in ("all", "last", "middle"):
        src = kc_data.get(key)
        if src is not None and not src.empty and "id" in src.columns:
            ref_sources.append(src)

    if ref_sources:
        ref = pd.concat(ref_sources, ignore_index=True)
        ref = ref.drop_duplicates(subset=["id"], keep="first").set_index("id")

        def pick(refcol: str) -> pd.Series:
            if refcol in ref.columns:
                return df_work["matched_id"].map(ref[refcol])
            return pd.Series([np.nan] * len(df_work), index=df_work.index)

        def clean_pick(refcol: str) -> pd.Series:
            s = pick(refcol).fillna("").astype(str).str.strip()
            return s.mask(s.str.lower().isin(["nan", "none", "null"]), "")

        df_work["CIAM Last Nom"] = clean_pick("last_name")
        df_work["CIAM Middle Nom"] = clean_pick("middlename")
        df_work["CIAM Prénom"] = clean_pick("first_name")
        df_work["CIAM Date de naissance"] = clean_pick("birthdate")
        df_work["CIAM KPEP"] = clean_pick("idkpep")
        df_work["CIAM Email"] = clean_pick("email")
        df_work["CIAM Email Other"] = clean_pick("email_other")
        df_work["CIAM Téléphone"] = clean_pick("phonenumber")
    else:
        for col_label in ["CIAM Last Nom", "CIAM Middle Nom", "CIAM Prénom", "CIAM Date de naissance",
                          "CIAM KPEP", "CIAM Email", "CIAM Email Other", "CIAM Téléphone"]:
            df_work[col_label] = ""

    # --- Colonnes de rapprochement ---
    df_work["Rapproché / Non rapproché"] = np.where(
        df_work["match_status"] == "RAPPROCHE", "Rapproché", "Non rapproché"
    )
    df_work["Donnée rapprochée"] = df_work["match_method"].fillna("AUCUNE")

    # --- Nombre de KPEP ---
    # (déjà dans Nombre de KPEP)

    # --- Email CIAM = Email coordonnées (uniquement quand trouvé) ---
    # Affiche l'email uniquement si le compte est rapproché ET que l'email CIAM matché
    # correspond à l'email de valeur_coordonnée (New_S)
    email_ciam_matched = df_work["CIAM Email"].fillna("").astype(str).str.lower().str.strip()
    email_val_ns = df_work["NS_email_val"].fillna("").astype(str).str.lower().str.strip()
    mask_email_match = (
        (df_work["match_status"] == "RAPPROCHE")
        & (email_ciam_matched != "")
        & (email_ciam_matched == email_val_ns)
    )
    df_work["Email CIAM = Email coordonnées"] = np.where(mask_email_match, email_ciam_matched, "")

    # --- Qualité Données (flag par ligne) ---
    # DATA_QUALITY_OK : aucune anomalie détectée
    # DATA_QUALITY_KO : au moins 1 des critères suivants est vrai :
    #   - Non rapproché
    #   - Date naissance prospect (NS ou CIAM = '1900-01-01')
    #   - KPEP NS ≠ KPEP CIAM (les deux non-vides)
    #   - Email CIAM invalide (format non conforme)
    #   - Email CIAM à risque (domaine suspect)
    def _dq_flag(row: pd.Series) -> str:
        raisons: List[str] = []
        if row.get("match_status") != "RAPPROCHE":
            raisons.append("Non rapproché")
        else:
            if str(row.get("NS_ddn", "")).strip() == "1900-01-01":
                raisons.append("DDN prospect (NS)")
            if str(row.get("CIAM Date de naissance", "")).strip() == "1900-01-01":
                raisons.append("DDN prospect (CIAM)")
            ns_kpep_val = str(row.get("NS_kpep", "")).strip().upper()
            ciam_kpep_val = str(row.get("CIAM KPEP", "")).strip().upper()
            if ns_kpep_val and ciam_kpep_val and ns_kpep_val != ciam_kpep_val:
                raisons.append("KPEP NS≠CIAM")
            ciam_em = str(row.get("CIAM Email", "")).strip().lower()
            if ciam_em and ciam_em not in ("nan", "none", "null"):
                if not EMAIL_VALID_RE.match(ciam_em):
                    raisons.append("Email CIAM invalide")
                elif "@" in ciam_em and ciam_em.split("@", 1)[1] in RISKY_EMAIL_DOMAINS:
                    raisons.append("Email CIAM risque")
        return "OK" if not raisons else "KO: " + "; ".join(raisons)

    df_work["Qualité Données"] = df_work.apply(_dq_flag, axis=1)

    # --- Carte TP ---
    def apply_tp(row: pd.Series) -> Tuple[str, str, str]:
        return compute_carte_tp_row(
            type_assure=str(row.get(col_type, "")).strip(),
            offre=str(row.get("NS_offre", "")).strip(),
            code_soc=str(row.get("NS_societe", "")).strip(),
            date_effet_str=str(row.get("NS_date_effet", "") or "").strip(),
            date_adh_str=str(row.get("NS_date_adh", "") or "").strip(),
        )

    tp_results = df_work.apply(apply_tp, axis=1)
    df_work["Eligibilité TP"] = [r[0] for r in tp_results]
    df_work["Date éligibilité TP"] = [r[1] for r in tp_results]
    df_work["Valeur carte TP"] = [r[2] for r in tp_results]

    # --- Sélection finale des colonnes ---
    # 1. Colonnes de base New_S (telles qu'elles existaient dans le CSV)
    ns_cols = [c for c in original_ns_cols if c in df_work.columns]

    # Colonnes d'enrichissement (hors colonnes NS)
    extra_cols = [
        # CIAM enrichissement
        "CIAM Last Nom",
        "CIAM Middle Nom",
        "CIAM Prénom",
        "CIAM Date de naissance",
        "CIAM KPEP",
        "CIAM Email",
        "CIAM Email Other",
        # Rapprochement
        "Rapproché / Non rapproché",
        "Donnée rapprochée",
        # Compléments
        "Nombre de KPEP",
        "Email CIAM = Email coordonnées",
        # Qualité données
        "Qualité Données",
    ]

    final_cols = ns_cols + [c for c in extra_cols if c in df_work.columns]
    df_out = df_work[final_cols].copy()

    # Préfixer les colonnes NS_ pour distinguer l'origine New_S
    rename_ns = {c: f"NS_{c}" for c in ns_cols}
    df_out = df_out.rename(columns=rename_ns)

    # Export NS_CIAM
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    f_out = OUTPUT_DIR / f"{prefix}_NS_CIAM.csv"
    df_out.to_csv(f_out, index=False, sep=output_sep, encoding="utf-8-sig")
    print(f"   ✅ Fichier NS_CIAM généré : {f_out.name} ({len(df_out)} lignes, {len(df_out.columns)} colonnes)")
    print(f"   📌 Comptes assurés AI uniques: {df_target.shape[0]}")
    print(f"   📌 Comptes CIAM non rapprochés: {(df_target['match_status'] != 'RAPPROCHE').sum()}")

    # Export NS_IEHE — toutes les personnes du flux (assurés + conjoints), enrichies :
    #   - colonnes IEHE brutes (idrpp, refperboccn, etc.)
    #   - IEHE Présent / IEHE KPEP / IKPEP IEHE ≠ KPEP CIAM
    #   - Carte TP (y compris valeur "Conjoint" pour les CONJO)
    if (
        df_iehe is not None
        and not df_iehe.empty
        and col_pers in df_ns.columns
        and "refperboccn" in df_iehe.columns
    ):
        # --- Jointure IEHE (1 ligne IEHE représentante par personne) ---
        df_iehe_ref = df_iehe.drop_duplicates(subset=["refperboccn"]).set_index("refperboccn")
        df_m = df_ns.merge(
            df_iehe_ref,
            left_on=col_pers,
            right_index=True,
            how="left",
            suffixes=("", "_iehe"),
        )

        # --- Index multi-valeurs IEHE KPEP (idrpp peut être multiple par personne) ---
        iehe_kpep_by_pers_iehe: Dict[str, List[str]] = {}
        if "idrpp" in df_iehe.columns:
            for _, r in df_iehe.iterrows():
                pid = str(r.get("refperboccn", "")).strip()
                kpep = str(r.get("idrpp", "")).strip()
                if pid and kpep and kpep.lower() not in ("", "nan", "null"):
                    iehe_kpep_by_pers_iehe.setdefault(pid, [])
                    if kpep not in iehe_kpep_by_pers_iehe[pid]:
                        iehe_kpep_by_pers_iehe[pid].append(kpep)

        ids_iehe_set_m = set(iehe_kpep_by_pers_iehe.keys()) | set(
            df_iehe["refperboccn"].dropna().astype(str).str.strip().unique()
        )

        # IEHE Présent
        df_m["IEHE Présent"] = df_m[col_pers].astype(str).str.strip().isin(ids_iehe_set_m).map(
            {True: "Présent dans IEHE", False: "Non présent dans IEHE"}
        )

        # --- Export IEHE_KO (personnes non trouvées en IEHE) ---
        # Création systématique (même vide) pour garantir une source de vérité
        # historisable et permettre à 06_iehe_retry.py de toujours trouver un fichier.
        # Inclut date_adhesion / date_effet_adhesion + éligibilité Carte TP pour
        # permettre la ventilation des KPI Retry IEHE par type d'assuré et par
        # éligibilité TP, sans dépendre du New_S courant.
        mask_ko = df_m["IEHE Présent"] == "Non présent dans IEHE"
        ko_src_cols = [col_pers]
        for c in ["nom_long", "prenom", col_type, "date_naissance",
                   "valeur_coordonnee", "idkpep", "offre",
                   "date_adhesion", "date_effet_adhesion", "code_soc_appart"]:
            if c and c in df_m.columns and c not in ko_src_cols:
                ko_src_cols.append(c)
        df_ko = df_m.loc[mask_ko, ko_src_cols + ["NS_date_adh", "NS_date_effet", "NS_societe", "NS_offre"]] \
                    .drop_duplicates(subset=[col_pers]).copy()

        # Calcul éligibilité TP par ligne KO (utilise les versions ISO des dates)
        def _ko_tp_row(row: pd.Series) -> Tuple[str, str, str, str]:
            return compute_carte_tp_row(
                type_assure=str(row.get(col_type, "")).strip() if col_type else "",
                offre=str(row.get("NS_offre", "") or "").strip(),
                code_soc=str(row.get("NS_societe", "") or "").strip(),
                date_effet_str=str(row.get("NS_date_effet", "") or "").strip(),
                date_adh_str=str(row.get("NS_date_adh", "") or "").strip(),
            )

        if len(df_ko) > 0:
            tp_ko = df_ko.apply(_ko_tp_row, axis=1)
            df_ko["Eligibilité TP"] = [r[0] for r in tp_ko]
            df_ko["Date éligibilité TP"] = [r[1] for r in tp_ko]
            df_ko["Valeur carte TP"] = [r[2] for r in tp_ko]
            df_ko["Raison non Eligibilité"] = [r[3] for r in tp_ko]
        else:
            df_ko["Eligibilité TP"] = []
            df_ko["Date éligibilité TP"] = []
            df_ko["Valeur carte TP"] = []
            df_ko["Raison non Eligibilité"] = []

        # Drop colonnes techniques NS_* utilisées uniquement pour le calcul ci-dessus
        df_ko = df_ko.drop(columns=["NS_date_adh", "NS_date_effet", "NS_societe", "NS_offre"], errors="ignore")
        df_ko = df_ko.rename(columns={c: f"NS_{c}" for c in ko_src_cols})
        df_ko["statut_retry"] = "KO"
        df_ko["date_derniere_verif"] = prefix
        df_ko["mail_IEHE"] = ""
        df_ko["KPEP_IEHE"] = ""
        ko_path = OUTPUT_DIR / f"{prefix}_IEHE_KO.csv"
        df_ko.to_csv(ko_path, index=False, sep=output_sep, encoding="utf-8-sig")
        if len(df_ko) == 0:
            print(f"   ✅ Fichier IEHE_KO généré : {ko_path.name} (0 personne — toutes présentes dans IEHE)")
        else:
            print(f"   ✅ Fichier IEHE_KO généré : {ko_path.name} ({len(df_ko)} personnes non présentes dans IEHE)")

        # [TEMPORAIRE — DÉSACTIVÉ] IEHE KPEP (règle code 044)
        # def get_iehe_kpep_m(pid_val: Any, soc_val: Any) -> str:
        #     pid = str(pid_val).strip()
        #     soc = str(soc_val).strip()
        #     kpeps = iehe_kpep_by_pers_iehe.get(pid, [])
        #     if not kpeps:
        #         return ""
        #     if soc == "044":
        #         filtered = [k for k in kpeps if k.startswith("044")]
        #         return ";".join(filtered) if filtered else ";".join(kpeps)
        #     return ";".join(kpeps)
        # df_m["IEHE KPEP"] = df_m.apply(
        #     lambda row: get_iehe_kpep_m(row[col_pers], row.get("NS_societe", "")), axis=1
        # )

        # [TEMPORAIRE — DÉSACTIVÉ] IKPEP IEHE ≠ KPEP CIAM
        # ciam_kpep_map: Dict[str, str] = {}
        # if "CIAM KPEP" in df_work.columns:
        #     tmp_kpep = (
        #         df_work[[col_pers, "CIAM KPEP"]]
        #         .dropna(subset=[col_pers])
        #         .drop_duplicates(subset=[col_pers])
        #     )
        #     ciam_kpep_map = tmp_kpep.set_index(col_pers)["CIAM KPEP"].to_dict()
        # def compare_kpep_m(pid_val: Any, iehe_kpep_str: str) -> str:
        #     pid = str(pid_val).strip()
        #     ciam_kpep = str(ciam_kpep_map.get(pid, "")).strip()
        #     if not iehe_kpep_str or not ciam_kpep:
        #         return ""
        #     iehe_set = {k.strip() for k in iehe_kpep_str.split(";") if k.strip()}
        #     return "N" if ciam_kpep in iehe_set else "O"
        # df_m["IKPEP IEHE ≠ KPEP CIAM"] = df_m.apply(
        #     lambda row: compare_kpep_m(row[col_pers], row["IEHE KPEP"]), axis=1
        # )

        # --- Carte TP (tous types, y compris CONJO) ---
        def apply_tp_iehe(row: pd.Series) -> Tuple[str, str, str, str]:
            type_assure = str(row.get(col_type, "")).strip()
            offre = str(row.get("NS_offre", "")).strip()
            code_soc = str(row.get("NS_societe", "")).strip()
            date_eff_str = str(row.get("NS_date_effet", "") or "").strip()
            date_adh_str = str(row.get("NS_date_adh", "") or "").strip()

            # Conjoint : éligible, valeur = "Conjoint"
            if "CONJ" in type_assure:
                if not date_eff_str:
                    return "O", "", "Conjoint", ""
                try:
                    d_eff = datetime.strptime(date_eff_str, "%Y-%m-%d").date()
                    return "O", d_eff.strftime("%d/%m/%Y"), "Conjoint", ""
                except Exception:
                    return "O", "", "Conjoint", ""

            # Délégation à la fonction commune (assurés).
            # Spécifique NS_IEHE : Date éligibilité TP = date_effet_adhesion - 21 jours.
            return compute_carte_tp_row(
                type_assure=type_assure,
                offre=offre,
                code_soc=code_soc,
                date_effet_str=date_eff_str,
                date_adh_str=date_adh_str,
                eligibility_mode="effet_minus_21",
            )

        tp_iehe = df_m.apply(apply_tp_iehe, axis=1)
        df_m["Eligibilité TP"] = [r[0] for r in tp_iehe]
        df_m["Date éligibilité TP"] = [r[1] for r in tp_iehe]
        df_m["Valeur carte TP"] = [r[2] for r in tp_iehe]
        df_m["Raison non Eligibilité"] = [r[3] for r in tp_iehe]

        # Mois éligibilité TP au format MM/YYYY (dérivé de Date éligibilité TP)
        def to_month_yyyy(date_str: str) -> str:
            if not date_str:
                return ""
            try:
                return datetime.strptime(date_str, "%d/%m/%Y").strftime("%m/%Y")
            except Exception:
                return ""

        df_m["Mois éligibilité TP"] = df_m["Date éligibilité TP"].apply(to_month_yyyy)

        # --- Sélection et ordonnancement des colonnes NS_IEHE ---
        # 1. Colonnes NS (originales New_S) avec préfixe NS_
        iehe_ns_cols = [c for c in original_ns_cols if c in df_m.columns]

        # 2. Colonnes IEHE brutes (hors clé de jointure, hors colonnes supprimées,
        #    hors doublons avec colonnes NS déjà présentes)
        # - refperboccn  : clé de jointure (= num_personne, déjà dans NS_num_personne)
        # - telmbictc    : supprimée (demande métier)
        # - socappr      : quasi-doublon de NS_code_soc_appart (valeur IEHE légèrement différente
        #                  dans ~0,4% des cas — conservée pour traçabilité)
        iehe_source_cols: List[str] = []
        for c in df_iehe.columns:
            if c in ("refperboccn", "telmbictc"):
                continue
            if c in df_m.columns:
                iehe_source_cols.append(c)
            elif f"{c}_iehe" in df_m.columns:
                iehe_source_cols.append(f"{c}_iehe")

        # 3. Colonnes calculées (IEHE, TP, etc.)
        # Note : "Type assuré" et "Offre" supprimés car 100% identiques à NS_type_assure / NS_offre
        iehe_extra_cols = [
            "IEHE Présent",
            # "IEHE KPEP",            # [TEMPORAIRE — DÉSACTIVÉ]
            # "IKPEP IEHE ≠ KPEP CIAM",  # [TEMPORAIRE — DÉSACTIVÉ]
            "Eligibilité TP",
            "Date éligibilité TP",
            "Valeur carte TP",
            "Raison non Eligibilité",
            "Mois éligibilité TP",
        ]

        iehe_final_cols = (
            iehe_ns_cols
            + iehe_source_cols
            + [c for c in iehe_extra_cols if c in df_m.columns]
        )
        iehe_rename_ns = {c: f"NS_{c}" for c in iehe_ns_cols}
        df_m_out = df_m[iehe_final_cols].rename(columns=iehe_rename_ns)

        df_m_out.to_csv(OUTPUT_DIR / f"{prefix}_NS_IEHE.csv", index=False, sep=output_sep, encoding="utf-8-sig")
        print(f"   ✅ Fichier NS_IEHE généré : {prefix}_NS_IEHE.csv ({len(df_m_out)} lignes, {len(df_m_out.columns)} colonnes)")


if __name__ == "__main__":
    main() 