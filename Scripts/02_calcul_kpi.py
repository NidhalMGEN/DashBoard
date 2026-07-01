# -*- coding: utf-8 -*-
"""
02_calcul_kpi.py (PROD GRADE) — KPI + MATCHING CIAM ALIGNÉ SCRIPT 03 (OPTION B SET-BASED)
========================================================================================

Changement majeur (Option B)
----------------------------
Le matching CIAM est désormais calculé au niveau "personne" (num_personne) en testant
toutes les valeurs candidates (emails CIAM, emails valeur_coordonnee, KPEP, identités, nom|ddn),
au lieu de tester une seule ligne représentante (drop_duplicates).

=> Corrige les NON_RAPPROCHE majoritairement dus aux doublons KPEP (multi-KPEP par personne).
=> Résultat déterministe et aligné avec le script 03.

Ordre strict inchangé (First Match Wins, sans écrasement)
--------------------------------------------------------
1) Mail_CIAM
2) Valeur_Coord
3) KPEP
4) Identité Full (Nom|Prenom|DDN)
5) Identité Inversée (Prenom|Nom|DDN)
6) Recherche_Large_Nom (Nom|DDN)
7) Recherche_Large_Middle (Nom|DDN)

Structure du JSON de sortie
----------------------------
1. Lectures fichiers input
2. Volumétrie brute
3. Volumétrie population unique
4. Qualité des données
   - Indicateurs (doublons, KPEP, etc.)
   - Qualité New_S [A1/A2/A3] : complétude clés, champs, radiation
   - Cohérence emails
5. Matching CIAM
   - Global (cible, rapprochés, non rapprochés, taux)
   - B1 : Par type d'assuré
   - B2 : Par société
   - B3 : Décomposition non-rapprochés
   - B4 : Cohérence KPEP New_S vs CIAM
6. IEHE
   - Population de référence, Présents, Manquants (volume + taux)
   - D1 : Complétude email IEHE
   - D2 : Concordance email IEHE vs CIAM
   - D3 : Concordance société IEHE vs New_S
7. Carte TP
   - Population éligible (volume + taux)
   - Population future (volume + taux)
   - Population PREV (volume)
   - Population éligible par mois
   - C1 : Éligibles TP non rapprochés CIAM
   - C2 : Délai moyen avant éligibilité FUTURE_TP
   - C3 : Répartition TP par société
8. Annexes
   - IEHE (détails par type, corrélations)
   - Carte TP (détail par type)
   - Matching CIAM (par méthode, par source)
   - Qualité contacts (CK, CM, Last, Middle)
"""

import json
import os
import re
import sys
import unicodedata
import warnings
from difflib import SequenceMatcher
from io import StringIO
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

# Helpers IEHE_KO partagés (classification multi-sources, lookup historique NS).
import iehe_ko_lib

# --- CONFIGURATION ---
SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = SCRIPT_DIR if (SCRIPT_DIR / "Input_Data").exists() else SCRIPT_DIR.parent
INPUT_DIR = BASE_DIR / "Input_Data"
OUTPUT_DIR = BASE_DIR / "Output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# --- CONSTANTES MÉTIER ---
# Types d'assuré entrant dans le périmètre CIAM / IEHE / Carte TP.
# Source unique de vérité — toute évolution doit n'impacter QUE cette ligne
# (cf. audit T4, M6 : ces 4 codes étaient dupliqués 5× dans le fichier).
TYPES_ASSURES_IEHE = ("ASSPRI", "MPACTI", "MPRETR", "MPVRET")

# =============================================================================
# OUTILS GÉNÉRIQUES
# =============================================================================

class NumpyEncoder(json.JSONEncoder):
    """Encodeur JSON pour gérer les types Numpy (int64, float64...) qui plantent json.dump."""
    def default(self, obj):
        if isinstance(obj, np.integer):
            return int(obj)
        if isinstance(obj, np.floating):
            return float(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        if isinstance(obj, set):
            return list(obj)
        return super().default(obj)


def _safe_get(d: Any, *path: Any) -> Any:
    cur = d
    for k in path:
        if isinstance(cur, dict):
            cur = cur.get(k)
        else:
            return None
        if cur is None:
            return None
    return cur


def _pick(src: Dict[str, Any], *keys: str) -> Dict[str, Any]:
    """Retourne un nouveau dict contenant uniquement les clés présentes dans src (ordre conservé)."""
    return {k: src[k] for k in keys if isinstance(src, dict) and k in src}


def _drop(src: Dict[str, Any], *keys: str) -> Dict[str, Any]:
    """Retourne une copie de src sans les clés listées."""
    if not isinstance(src, dict):
        return src
    return {k: v for k, v in src.items() if k not in set(keys)}


def reshape_to_modele_clean(report: Dict[str, Any]) -> Dict[str, Any]:
    """Transforme le rapport KPI brut en la structure cible Modele_clean.json :
    - sections 5/6/7 réordonnées avec sous-blocs `Annexe`
    - suppression des doublons (Doublons_Contrats_* dans Detail_Matching, Qualite_Sources_Extraites,
      Population_PREV) et du champ Population_Reference dans Presence_Globale
    - déplacement de Assures_Non_Crees_IEHE_Et_Eligibles_TP vers
      6_IEHE.Ecart_Lignes_vs_Personnes.Total_Flux.Par_Type_Assure.ASSPRI
    - split de blocs hybrides (Coherence_Emails, Qualite_NewS, Prospects_CIAM,
      Qualite_Emails_Enrichie) entre racine et Annexe.
    """
    src5 = report.get("5_CIAM", {}) or {}
    src6 = report.get("6_IEHE", {}) or {}
    src7 = report.get("7_Carte_TP", {}) or {}

    # ---------- 5_CIAM ----------
    coh = src5.get("Coherence_Emails", {}) or {}
    coh_root = _pick(coh, "Email_CIAM_Rapproche_Egal_Val_Coord", "KPI_Adresses_CIAM_Vides")
    coh_annexe = _drop(coh, "Email_CIAM_Rapproche_Egal_Val_Coord", "KPI_Adresses_CIAM_Vides", "definition")

    # T4 Bloc B — Imbriquer les detail-keys (Mails_CIAM_Egal_Val_Coord, Vrais_Emails_
    # Identiques, Mails_CIAM_Diff_Val_Coord, Detail_Diff...) SOUS la clef
    # Email_CIAM_Rapproche_Egal_Val_Coord pour regrouper la cohérence email
    # par sujet. La copie reste disponible dans Annexe.Coherence_Emails.
    if isinstance(coh_root.get("Email_CIAM_Rapproche_Egal_Val_Coord"), dict) and coh_annexe:
        coh_root["Email_CIAM_Rapproche_Egal_Val_Coord"]["Coherence_Emails"] = coh_annexe

    qns = src5.get("Qualite_NewS", {}) or {}
    qns_root_keys = _pick(qns, "A3_Radiation")
    qns_annexe = _drop(qns, "A3_Radiation", "definition")

    pros = src5.get("Prospects_CIAM", {}) or {}
    pros_root = _pick(pros, "E1c_Rapproches_Compte_CIAM_Prospect")
    pros_annexe = _drop(pros, "E1c_Rapproches_Compte_CIAM_Prospect", "definition")

    qee = src5.get("Qualite_Emails_Enrichie", {}) or {}
    qee_root = _pick(qee, "NS_Emails_MailCIAM")
    qee_annexe = _drop(qee, "NS_Emails_MailCIAM", "definition")

    detail_matching_clean = _drop(
        src5.get("Detail_Matching", {}) or {},
        "Doublons_Contrats_Diff_Offre",
        "Doublons_Contrats_Meme_Offre_Par_Type",
        "Doublons_Contrats_Diff_Offre_Par_Type",
    )

    # B1_Par_Type_Assure fait doublon avec Matching_Global (population 100% ASSPRI)
    matching_seg_clean = _drop(src5.get("Matching_Par_Segment", {}) or {}, "B1_Par_Type_Assure")

    # T4 Bloc A — Synthese_IEHE : 4 chiffres-clé issus de la section 6 IEHE,
    # remontés dans 5_CIAM pour lecture rapide (annotation Laurence l.115 du
    # JSON). Duplication intentionnelle : la section 6_IEHE reste intacte.
    src6_presence = (src6.get("Presence_Globale") or {})
    src6_detail = (src6.get("Detail_Par_Type_Assure") or {})
    _manquants_par_type = src6_detail.get("Manquants_IEHE_Par_Type_Assure") or {}
    _types_assures = set(TYPES_ASSURES_IEHE)
    synthese_iehe = {
        "definition": (
            "Synthèse de la présence IEHE remontée dans la section CIAM "
            "pour lecture rapide. Détail complet en 6_IEHE."
        ),
        "Personnes_Uniques_Presentes_IEHE": _safe_get(
            src6_presence, "Presents_IEHE", "Nombre"),
        "Personnes_Uniques_Absentes_IEHE": {
            "Nombre": _safe_get(src6_presence, "Manquants_IEHE", "Nombre"),
            "Taux": _safe_get(src6_presence, "Manquants_IEHE", "Taux"),
        },
        "Assures_Uniques_Absents_IEHE": int(sum(
            int(v or 0) for k, v in _manquants_par_type.items()
            if str(k).strip().upper() in _types_assures)),
        "Assures_Absents_IEHE_Et_Eligibles_Carte_TP":
            src6_detail.get("Assures_Non_Crees_IEHE_Et_Eligibles_TP"),
    }

    new_5 = {
        "definition": src5.get("definition"),
        "Matching_Global": src5.get("Matching_Global"),
        "Coherence_Emails": coh_root,
        "Synthese_IEHE": synthese_iehe,
        "Qualite_NewS": {**({"definition": qns.get("definition")} if qns.get("definition") else {}), **qns_root_keys},
        "Prospects_CIAM": {**({"definition": pros.get("definition")} if pros.get("definition") else {}), **pros_root},
        "Qualite_Emails_Enrichie": {**({"definition": qee.get("definition")} if qee.get("definition") else {}), **qee_root},
        "Annexe": {
            "Detail_Matching": detail_matching_clean,
            "Matching_Par_Segment": matching_seg_clean,
            "Coherence_Emails": coh_annexe,
            "Qualite_Comptes_CIAM": src5.get("Qualite_Comptes_CIAM"),
            "Incoherences_NS_CIAM": src5.get("Incoherences_NS_CIAM"),
            "Qualite_NewS": qns_annexe,
            "Prospects_CIAM": pros_annexe,
            "Coherence_KPEP_3_Sources": src5.get("Coherence_KPEP_3_Sources"),
            "Qualite_Emails_Enrichie": qee_annexe,
            "Score_Qualite_Donnees": src5.get("Score_Qualite_Donnees"),
        },
    }

    # ---------- 6_IEHE ----------
    presence = src6.get("Presence_Globale", {}) or {}
    ecart = presence.get("Ecart_Lignes_vs_Personnes", {}) or {}
    retry_ko = presence.get("Retry_IEHE_KO")

    # Move Assures_Non_Crees_IEHE_Et_Eligibles_TP into Ecart_Lignes_vs_Personnes.Total_Flux.Par_Type_Assure.ASSPRI
    detail_type = src6.get("Detail_Par_Type_Assure", {}) or {}
    nb_non_crees_tp = detail_type.get("Assures_Non_Crees_IEHE_Et_Eligibles_TP")

    ecart_root = {
        k: v for k, v in ecart.items()
        if k in ("definition", "Total_Flux", "Manquants_IEHE")
    }
    nb_eligibles_tp = _safe_get(src7, "Eligibilite_Globale", "Population_Eligible", "Nombre")
    if nb_non_crees_tp is not None or nb_eligibles_tp is not None:
        try:
            assr = (ecart_root.get("Total_Flux") or {}).get("Par_Type_Assure", {}).get("ASSPRI")
            if isinstance(assr, dict):
                if nb_non_crees_tp is not None:
                    assr["Assures_Non_Crees_IEHE_Et_Eligibles_TP"] = nb_non_crees_tp
                if nb_eligibles_tp is not None:
                    assr["Nb_Eligibles_Carte_TP"] = nb_eligibles_tp
        except Exception:
            pass

    presents_iehe_detail = ecart.get("Presents_IEHE")
    doublons_multi = ecart.get("Doublons_Personnes_Multi_Contrats")

    detail_type_clean = _drop(
        detail_type,
        "Assures_Non_Crees_IEHE_Et_Eligibles_TP",
        "Assures_Non_Crees_IEHE_Et_Eligibles_TP_Par_Type",
    )

    new_6 = {
        "definition": src6.get("definition"),
        "Presence_Globale": _drop(presence, "Population_Reference", "Ecart_Lignes_vs_Personnes", "Retry_IEHE_KO"),
        "Ecart_Lignes_vs_Personnes": ecart_root,
        "Annexe": {
            "Presents_IEHE": presents_iehe_detail,
            "Doublons_Personnes_Multi_Contrats": doublons_multi,
            "Retry_IEHE_KO": retry_ko,
            "Detail_Par_Type_Assure": detail_type_clean,
            "Qualite_Referentiel": src6.get("Qualite_Referentiel"),
        },
    }

    # ---------- 7_Carte_TP ----------
    elig = src7.get("Eligibilite_Globale", {}) or {}
    elig_clean = _drop(elig, "Population_PREV")

    tp_enrichi = src7.get("TP_Enrichi_Operationnel", {}) or {}
    annexe_tp_keys = _pick(tp_enrichi, "I1b_Future_TP_Delai_Restant", "I1c_Eligibles_TP_Sans_Email_CIAM")
    tp_enrichi_clean = _drop(tp_enrichi, "I1b_Future_TP_Delai_Restant", "I1c_Eligibles_TP_Sans_Email_CIAM")

    new_7 = {
        "definition": src7.get("definition"),
        "Eligibilite_Globale": elig_clean,
        "Detail_Par_Type": src7.get("Detail_Par_Type"),
        "TP_Enrichi_Operationnel": tp_enrichi_clean,
        "Controle_GED_Quotidien": src7.get("Controle_GED_Quotidien"),
        "Annexe": annexe_tp_keys,
    }

    # ---------- Réassemblage ordonné ----------
    out = {
        "Fichier_Principal": report.get("Fichier_Principal"),
        "Prefixe_Execution": report.get("Prefixe_Execution"),
        "Metadata": report.get("Metadata"),
        "1_Input_Files": report.get("1_Input_Files"),
        "2_Volumetrie_Brute": report.get("2_Volumetrie_Brute"),
        "3_Population_Unique": report.get("3_Population_Unique"),
        "4_Qualite_Donnees": report.get("4_Qualite_Donnees"),
        "5_CIAM": new_5,
        "6_IEHE": new_6,
        "7_Carte_TP": new_7,
        "8_Autres_Indicateurs": report.get("8_Autres_Indicateurs"),
    }
    return out


def export_thematic_jsons(final_report: Dict[str, Any], output_dir: Path, prefix: str) -> List[Path]:
    """Découpe le rapport KPI en 4 fichiers thématiques (CIAM, IEHE, Carte TP, Synthèse)."""
    header = {
        "Fichier_Principal": final_report.get("Fichier_Principal"),
        "Prefixe_Execution": final_report.get("Prefixe_Execution"),
        "Metadata": final_report.get("Metadata", {}),
    }

    highlights = {
        "definition": (
            "Indicateurs de tête de gondole agrégeant les 3 référentiels "
            "(CIAM, IEHE, Carte TP) et le score data-quality global."
        ),
        "CIAM_Taux_Couverture": _safe_get(final_report, "5_CIAM", "Matching_Global", "Global", "Taux_Couverture"),
        "CIAM_Rapproches": _safe_get(final_report, "5_CIAM", "Matching_Global", "Global", "Rapproches"),
        "CIAM_Non_Rapproches": _safe_get(final_report, "5_CIAM", "Matching_Global", "Global", "Non_Rapproches"),
        "IEHE_Taux_Presence": _safe_get(final_report, "6_IEHE", "Presence_Globale", "Presents_IEHE", "Taux"),
        "IEHE_Manquants": _safe_get(final_report, "6_IEHE", "Presence_Globale", "Manquants_IEHE", "Nombre"),
        "TP_Population_Base": _safe_get(final_report, "7_Carte_TP", "Eligibilite_Globale", "Population_Base"),
        "TP_Taux_Eligible": _safe_get(final_report, "7_Carte_TP", "Eligibilite_Globale", "Population_Eligible", "Taux"),
        "TP_Eligibles_Non_Rapproches_CIAM": _safe_get(
            final_report, "7_Carte_TP", "TP_Enrichi_Operationnel",
            "C1_Eligibles_TP_Non_Rapproches", "Eligibles_TP_Non_Rapproches", "Nombre",
        ),
        "Score_Data_Quality_OK_Pct": (
            _safe_get(final_report, "5_CIAM", "Annexe", "Score_Qualite_Donnees", "DATA_QUALITY_OK", "Pct")
            or _safe_get(final_report, "5_CIAM", "Score_Qualite_Donnees", "DATA_QUALITY_OK", "Pct")
        ),
        "Score_Data_Quality_KO_Nombre": (
            _safe_get(final_report, "5_CIAM", "Annexe", "Score_Qualite_Donnees", "DATA_QUALITY_KO", "Nombre")
            or _safe_get(final_report, "5_CIAM", "Score_Qualite_Donnees", "DATA_QUALITY_KO", "Nombre")
        ),
    }

    exports = {
        f"{prefix}_Modele_clean.json": final_report,
        f"{prefix}_KPI_CIAM.json": {**header, "5_CIAM": final_report.get("5_CIAM", {})},
        f"{prefix}_KPI_IEHE.json": {**header, "6_IEHE": final_report.get("6_IEHE", {})},
        f"{prefix}_KPI_Carte_TP.json": {**header, "7_Carte_TP": final_report.get("7_Carte_TP", {})},
        f"{prefix}_KPI_Synthese_Volumetrie_Qualite.json": {
            **header,
            "Highlights": highlights,
            "1_Input_Files": final_report.get("1_Input_Files", {}),
            "2_Volumetrie_Brute": final_report.get("2_Volumetrie_Brute", {}),
            "3_Population_Unique": final_report.get("3_Population_Unique", {}),
            "4_Qualite_Donnees": final_report.get("4_Qualite_Donnees", {}),
            "8_Autres_Indicateurs": final_report.get("8_Autres_Indicateurs", {}),
        },
    }

    written = []
    for filename, payload in exports.items():
        path = output_dir / filename
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=4, ensure_ascii=False, cls=NumpyEncoder)
        written.append(path)
    return written


def _load_wrapped_csv(path: Path, encoding: str = "utf-8") -> Optional[pd.DataFrame]:
    """
    Certains exports arrivent avec chaque ligne encapsulée dans des guillemets,
    ce qui fait charger tout le fichier en 1 seule colonne.
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

def load_csv(path: Path, sep=None, encoding="utf-8"):
    """Charge un CSV de manière robuste (détection sep automatique) avec debug."""
    if path is None or not path.exists():
        return None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter(action="ignore", category=FutureWarning)
            df = pd.read_csv(path, sep=sep, engine="python", encoding=encoding, dtype=str)
            if df is not None:
                if len(df.columns) == 1:
                    header = str(df.columns[0])
                    if ("," in header) and ('""' in header or '","' in header):
                        fixed = _load_wrapped_csv(path, encoding=encoding)
                        if fixed is not None:
                            df = fixed
                df.columns = [
                    str(c)
                    .lower()
                    .strip()
                    .replace('"', "")
                    .replace("'", "")
                    .replace("\ufeff", "")
                    for c in df.columns
                ]
                print(f"   [INFO] {path.name} chargé. {len(df)} lignes.")
            return df
    except UnicodeDecodeError:
        # fallback latin-1
        try:
            df = pd.read_csv(path, sep=sep, engine="python", encoding="latin-1", dtype=str)
            if df is not None:
                if len(df.columns) == 1:
                    header = str(df.columns[0])
                    if ("," in header) and ('""' in header or '","' in header):
                        fixed = _load_wrapped_csv(path, encoding="latin-1")
                        if fixed is not None:
                            df = fixed
                df.columns = [
                    str(c)
                    .lower()
                    .strip()
                    .replace('"', "")
                    .replace("'", "")
                    .replace("\ufeff", "")
                    for c in df.columns
                ]
                print(f"   [INFO] {path.name} chargé (latin-1). {len(df)} lignes.")
            return df
        except Exception as e:
            print(f"⚠️ Impossible de charger {path.name} (latin-1): {e}")
            return None
    except Exception as e:
        print(f"⚠️ Impossible de charger {path.name}: {e}")
        return None

def load_concat_by_pattern(directory: Path, pattern: str):
    """
    Charge et concatène tous les fichiers correspondant à un pattern (ex: *CK*.csv).
    Utilisé pour gérer les fichiers découpés (Part1, Part2...).
    """
    files = sorted(directory.glob(pattern))
    if not files:
        return None

    print(f"   [PATTERN] Chargement pattern '{pattern}' -> {len(files)} fichiers trouvés.")
    dfs = []
    for f in files:
        df = load_csv(f, sep=None)
        if df is not None and not df.empty:
            dfs.append(df)

    if not dfs:
        return None

    final_df = pd.concat(dfs, ignore_index=True)
    print(f"   [MERGE] {pattern} -> Total concaténé : {len(final_df)} lignes.")
    return final_df

def get_col_flexible(df, candidates):
    """Cherche une colonne parmi une liste de candidats (déjà en lowercase)."""
    if df is None:
        return None
    for col in candidates:
        if col in df.columns:
            return col
    return None

def normalize_text(text: Any) -> str:
    if pd.isna(text) or text == "":
        return ""
    text = str(text).upper().strip()
    text = unicodedata.normalize("NFD", text).encode("ascii", "ignore").decode("utf-8")
    return text

def normalize_email(email: Any) -> str:
    if pd.isna(email) or email == "":
        return ""
    s = str(email).lower().strip()
    if s in ("null", "nan", "none"):
        return ""
    return s

# Extraction robuste d'emails (support multi-valeurs)
EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)

# Validation de format email (plus stricte : ancrée début + fin)
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

def format_date_iso(date_str: Any) -> str:
    """
    Politique robuste et déterministe:
    - ISO YYYY-MM-DD => OK
    - DD/MM/YYYY ou DD-MM-YYYY => convert
    - sinon => tentative pandas (dayfirst), sinon => ""
    """
    if pd.isna(date_str) or date_str == "":
        return ""
    s = str(date_str).strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return s
    for fmt in ("%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
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

def get_file_info(df, name):
    if df is not None:
        return {"Fichier": name + " (Pattern/Concat)", "Lignes": len(df), "Statut": "Chargé"}
    return {"Fichier": name, "Lignes": 0, "Statut": "Non trouvé / Vide"}

# =============================================================================
# KPI QUALITÉ COORDONNÉES CONTACTS (NULL vs VIDE)
# =============================================================================

def analyse_qualite_contacts(df, colonnes):
    """
    Calcule NULL / VIDE / REMPLI pour une liste de colonnes sur un df.
    - NULL : valeur réellement null (NaN) + littéral "NULL"
    - VIDE : chaîne vide/whitespace (si pas NULL)
    """
    if df is None or df.empty:
        return {}

    stats = {}
    for col in colonnes:
        if col not in df.columns:
            continue

        s = df[col]
        s_str = s.astype(str)

        is_null = s.isna() | (s_str.str.strip().str.upper() == "NULL")
        is_empty = (~is_null) & (s_str.str.strip() == "")
        is_filled = ~(is_null | is_empty)

        stats[col] = {
            "NULL": int(is_null.sum()),
            "VIDE": int(is_empty.sum()),
            "REMPLI": int(is_filled.sum()),
        }

    return stats

# =============================================================================
# MATCHING CIAM — ENGINE + OPTION B (SET-BASED)
# =============================================================================

class MatchingEngine:
    def __init__(self, keycloak_data):
        self.kc = keycloak_data
        self.idx_kpep = self._build_index(self.kc["all"], "idkpep")
        self.idx_email = self._build_email_index(self.kc["all"])
        self.idx_identite_full = self._build_index(self.kc["all"], "cle_identite")
        self.idx_large_nom_ddn = self._build_index(self.kc["last"], "cle_nom_ddn")
        self.idx_middle = self._build_index(self.kc["middle"], "cle_nom_ddn_middle")

    def _build_index(self, df, col_name):
        if df is None or col_name not in df.columns or "id" not in df.columns:
            return {}
        tmp = df.copy()
        tmp[col_name] = tmp[col_name].fillna("").astype(str)
        tmp["id"] = tmp["id"].fillna("").astype(str)
        valid = tmp[(tmp[col_name] != "") & (tmp["id"] != "")]
        if valid.empty:
            return {}
        return valid.groupby(col_name)["id"].apply(list).to_dict()

    def _build_email_index(self, df):
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

    # --- Option B: set-based per person (row = ligne "personne" issue de build_person_view)
    def find_match_person(self, row):
        """
        Ordre strict métier :
        1. Mail CIAM
        2. Valeur Coord
        3. KPEP
        4. Identité Complète
        5. Identité Inversée
        6. Large Nom
        7. Large Middle
        """
        # 1) Mail_CIAM
        for mail in (row.get("emails_ciam") or []):
            res = self.idx_email.get(mail, [None])[0] if mail else None
            if res:
                return "RAPPROCHE", "Mail_CIAM", res, mail

        # 2) Valeur_Coord
        for mail in (row.get("emails_val") or []):
            res = self.idx_email.get(mail, [None])[0] if mail else None
            if res:
                return "RAPPROCHE", "Valeur_Coord", res, mail

        # 3) KPEP
        for kpep in (row.get("kpeps") or []):
            res = self.idx_kpep.get(kpep, [None])[0] if kpep else None
            if res:
                return "RAPPROCHE", "KPEP", res, kpep

        # 4) Identité Full (Nom|Prenom|DDN)
        for key in (row.get("ident_full") or []):
            res = self.idx_identite_full.get(key, [None])[0] if key else None
            if res:
                return "RAPPROCHE", "Nom_Prenom_DDN", res, key

        # 5) Identité inversée (Prenom|Nom|DDN)
        for key in (row.get("ident_inv") or []):
            res = self.idx_identite_full.get(key, [None])[0] if key else None
            if res:
                return "RAPPROCHE", "Identite_Inversee", res, key

        # 6) Large Nom (Nom|DDN)
        for key in (row.get("nom_ddn") or []):
            res = self.idx_large_nom_ddn.get(key, [None])[0] if key else None
            if res:
                return "RAPPROCHE", "Recherche_Large_Nom", res, key

        # 7) Large Middle (Nom|DDN)
        for key in (row.get("nom_ddn") or []):
            res = self.idx_middle.get(key, [None])[0] if key else None
            if res:
                return "RAPPROCHE", "Recherche_Large_Middle", res, key

        return "NON_RAPPROCHE", "", None, None


# =============================================================================
# PRÉPARATION
# =============================================================================

def preprocess_new_s(df):
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

    if not col_offre:
        print("\n⚠️ AVERTISSEMENT : Colonne 'offre' introuvable dans le fichier.")
        print("   -> Une valeur par défaut 'INCONNUE' sera utilisée.")

    df["NS_nom"] = df[col_nom].apply(normalize_text) if col_nom else ""
    df["NS_prenom"] = df[col_prenom].apply(normalize_text) if col_prenom else ""
    df["NS_ddn"] = df[col_ddn].apply(format_date_iso) if col_ddn else ""
    df["src_email_ciam"] = df[col_email].astype(str).str.strip() if col_email else ""
    df["NS_email_ciam"] = df[col_email].apply(normalize_email) if col_email else ""
    df["NS_kpep"] = df[col_kpep].apply(lambda x: str(x).strip() if pd.notna(x) else "") if col_kpep else ""

    if col_offre:
        df["NS_offre"] = df[col_offre].astype(str).str.strip().str.upper()
    else:
        df["NS_offre"] = "INCONNUE"

    df["NS_code_soc"] = df[col_soc].astype(str).str.strip() if col_soc else "UNK"
    df["NS_date_rad"] = df[col_rad].apply(format_date_iso) if col_rad else ""
    df["NS_date_adh"] = df[col_adh].apply(format_date_iso) if col_adh else ""
    df["NS_date_effet"] = df[col_eff].apply(format_date_iso) if col_eff else df["NS_date_adh"]

    def clean_val_coord(val):
        val = str(val).strip()
        if val.lower() in ("", "null", "nan", "none"):
            return ""
        return normalize_email(val) if "@" in val else ""

    df["src_email_val"] = df[col_val].astype(str).str.strip() if col_val else ""
    df["NS_email_val"] = df[col_val].apply(clean_val_coord) if col_val else ""
    df["NS_identite_full"] = df["NS_nom"] + "|" + df["NS_prenom"] + "|" + df["NS_ddn"]
    return df

def prepare_keycloak_data(df_ck, df_cm, df_last, df_last_prenom, df_middle, df_middle_prenom):
    data = {}

    def prep(df):
        if df is None:
            return pd.DataFrame()
        c_last = get_col_flexible(df, ["last_name", "nom"])
        c_first = get_col_flexible(df, ["first_name", "prenom"])
        c_birth = get_col_flexible(df, ["birthdate", "date_naissance"])

        df["nom_norm"] = df[c_last].apply(normalize_text) if c_last else ""
        df["prenom_norm"] = df[c_first].apply(normalize_text) if c_first else ""
        df["ddn_norm"] = df[c_birth].apply(format_date_iso) if c_birth else ""
        return df

    sources_all = []
    if df_ck is not None and not df_ck.empty:
        sources_all.append(df_ck.copy())
    if df_cm is not None and not df_cm.empty:
        sources_all.append(df_cm.copy())

    if sources_all:
        df_all = pd.concat(sources_all, ignore_index=True)
        if "id" in df_all.columns:
            dedupe_cols = ["id", "realm_id"] if "realm_id" in df_all.columns else ["id"]
            df_all = df_all.drop_duplicates(subset=dedupe_cols, keep="first")

        df_all["email"] = df_all["email"].apply(normalize_email) if "email" in df_all.columns else ""
        if "email_other" in df_all.columns:
            df_all["email_other"] = df_all["email_other"].apply(normalize_email)
        df_all["idkpep"] = (
            df_all["idkpep"].apply(lambda x: str(x).strip() if pd.notna(x) else "")
            if "idkpep" in df_all.columns
            else ""
        )
        df_all = prep(df_all)
        df_all["cle_identite"] = df_all["nom_norm"] + "|" + df_all["prenom_norm"] + "|" + df_all["ddn_norm"]
        df_all["cle_nom_prenom_kpep"] = df_all["nom_norm"] + "|" + df_all["prenom_norm"] + "|" + df_all["idkpep"]
        data["all"] = df_all
    else:
        data["all"] = pd.DataFrame(columns=["id", "email", "idkpep", "cle_identite", "cle_nom_prenom_kpep"])

    if df_last is not None:
        df_last = prep(df_last)
        df_last["cle_nom_ddn"] = df_last["nom_norm"] + "|" + df_last["ddn_norm"]
        data["last"] = df_last
    else:
        data["last"] = pd.DataFrame(columns=["id", "cle_nom_ddn"])

    if df_middle is not None:
        df_middle = prep(df_middle)
        df_middle["cle_nom_ddn_middle"] = df_middle["nom_norm"] + "|" + df_middle["ddn_norm"]
        data["middle"] = df_middle
    else:
        data["middle"] = pd.DataFrame(columns=["id", "cle_nom_ddn_middle"])

    data["last_prenom"] = df_last_prenom
    data["middle_prenom"] = df_middle_prenom
    return data

# =============================================================================
# OPTION B : VUE PERSONNE (set-based)
# =============================================================================

def build_person_view(df: pd.DataFrame, col_pers: str) -> pd.DataFrame:
    """
    1 ligne = 1 personne (col_pers), avec listes triées/déterministes:
    emails_ciam, emails_val, kpeps, ident_full, ident_inv, nom_ddn
    """
    required = ["NS_email_ciam", "NS_email_val", "NS_kpep", "NS_nom", "NS_prenom", "NS_ddn"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise RuntimeError(f"build_person_view: colonnes NS_* manquantes: {missing}")

    rows: List[Dict[str, Any]] = []

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

        kpeps = sorted({str(x).strip() for x in g["NS_kpep"].tolist() if pd.notna(x) and str(x).strip() not in ("", "NULL", "null")})

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
                "emails_ciam": sorted(emails_ciam),
                "emails_val": sorted(emails_val),
                "kpeps": kpeps,
                "ident_full": sorted(ident_full),
                "ident_inv": sorted(ident_inv),
                "nom_ddn": sorted(nom_ddn),
            }
        )

    return pd.DataFrame(rows)

# =============================================================================
# ANALYSES KPI
# =============================================================================

def analyse_doublons_contrats(df_new_s, types_assures_ai):
    col_type = get_col_flexible(df_new_s, ["type_assure", "typeassure"])
    col_pers = get_col_flexible(df_new_s, ["num_personne", "numpersonne"])
    col_ctr = get_col_flexible(df_new_s, ["num_ctr_indiv", "contrat"])

    if not (col_type and col_pers and col_ctr):
        return 0, 0, {}, {}

    mask_assure = df_new_s[col_type].isin(types_assures_ai)
    df_ai = df_new_s[mask_assure].copy()
    dup_personnes = df_ai[df_ai.duplicated(subset=[col_pers], keep=False)]

    meme_offre = 0
    diff_offre = 0
    meme_offre_par_type = {}
    diff_offre_par_type = {}
    for _, group in dup_personnes.groupby(col_pers):
        offres = group["NS_offre"].unique()
        contrats = group[col_ctr].unique()
        if len(contrats) > 1:
            types_in_group = group[col_type].unique()
            if len(offres) == 1:
                meme_offre += 1
                for t in types_in_group:
                    meme_offre_par_type[t] = meme_offre_par_type.get(t, 0) + 1
            else:
                diff_offre += 1
                for t in types_in_group:
                    diff_offre_par_type[t] = diff_offre_par_type.get(t, 0) + 1
    return meme_offre, diff_offre, meme_offre_par_type, diff_offre_par_type

def analyse_doublons_kpep_detail(df_new_s, types_assures_ai):
    col_pers = get_col_flexible(df_new_s, ["num_personne", "numpersonne"])
    col_type = get_col_flexible(df_new_s, ["type_assure", "typeassure"])

    if not col_pers or "NS_kpep" not in df_new_s.columns:
        return {}

    stats = {
        "global_multi_kpep": 0,
        "detail": {
            "2_kpep": {"total": 0, "dont_assures": 0},
            "3_kpep": {"total": 0, "dont_assures": 0},
            "plus_3_kpep": {"total": 0, "dont_assures": 0},
        },
    }

    for _, group in df_new_s.groupby(col_pers):
        kpeps = [k for k in group["NS_kpep"].dropna().unique() if str(k).strip() != ""]
        nb_kpeps = len(kpeps)

        if nb_kpeps > 1:
            stats["global_multi_kpep"] += 1
            is_assure = group[col_type].isin(types_assures_ai).any() if col_type else False

            if nb_kpeps == 2:
                stats["detail"]["2_kpep"]["total"] += 1
                if is_assure:
                    stats["detail"]["2_kpep"]["dont_assures"] += 1
            elif nb_kpeps == 3:
                stats["detail"]["3_kpep"]["total"] += 1
                if is_assure:
                    stats["detail"]["3_kpep"]["dont_assures"] += 1
            else:
                stats["detail"]["plus_3_kpep"]["total"] += 1
                if is_assure:
                    stats["detail"]["plus_3_kpep"]["dont_assures"] += 1

    return stats

def analyse_qualite_emails(df):
    """
    Cohérence entre Mail CIAM (colonne mailciam dans New_S)
    et Valeur Coordonnées (colonne valeur_coordonnee dans New_S).
    Base de calcul : toutes les lignes (assurés + conjoints).
    """
    if df is None:
        return {}

    total = len(df)
    if total == 0:
        return {}

    c_ciam = df["NS_email_ciam"]
    c_val = df["NS_email_val"]

    m_ciam_empty = (c_ciam == "")
    m_val_empty = (c_val == "")
    m_eq = (c_ciam == c_val)
    m_both_empty = (m_ciam_empty & m_val_empty)
    # [PATCH 1] Vrais emails identiques : exclut les cas "deux vides"
    m_both_nonempty_eq = (~m_ciam_empty) & (~m_val_empty) & m_eq
    # [PATCH 5] Décomposition des "différents"
    m_ciam_only = (~m_ciam_empty) & m_val_empty
    m_val_only = m_ciam_empty & (~m_val_empty)
    m_both_diff = (~m_ciam_empty) & (~m_val_empty) & (~m_eq)

    def fmt(count):
        return {"Nombre": int(count), "Pct": round(count / total * 100, 2)}

    return {
        "Mails_CIAM_Egal_Val_Coord": fmt(m_eq.sum()),
        "Vrais_Emails_Identiques": {
            **fmt(m_both_nonempty_eq.sum()),
            "definition": "Emails égaux ET non-vides (exclut les 'deux vides' comptés à tort comme égaux).",
        },
        "Mails_CIAM_Diff_Val_Coord": fmt((~m_eq).sum()),
        "Detail_Diff": {
            "CIAM_Present_Coord_Absente": fmt(m_ciam_only.sum()),
            "Coord_Presente_CIAM_Absent": fmt(m_val_only.sum()),
            "Deux_Emails_Distincts": fmt(m_both_diff.sum()),
        },
        "Mails_CIAM_Vides": fmt(m_ciam_empty.sum()),
        "Mails_Val_Coord_Vides": fmt(m_val_empty.sum()),
        "Mails_Deux_Sources_Vides": fmt(m_both_empty.sum()),
    }

# =============================================================================
# LOGIQUE CARTES TP
# =============================================================================

def calcul_kpi_cartes_tp(df_new_s):
    """
    Calcule les KPI Carte TP.

    Périmètre : types assurés ASSPRI, MPACTI, MPRETR, MPVRET
                Exclusions : offres PREV (préfixe MEP ou IND) et société 073

    Logique éligibilité basée sur le delta :
    - delta = date_effet_adhesion - date_adhesion (en jours)
    - "ELIGIBLE_TP" si delta < 22 (valeurs négatives comprises)
    - "FUTURE_TP"   si delta >= 22
    """
    TYPES_TP = list(TYPES_ASSURES_IEHE)

    col_type = get_col_flexible(df_new_s, ["type_assure", "typeassure"])
    col_pers = get_col_flexible(df_new_s, ["num_personne", "numpersonne"])
    if not col_type or not col_pers:
        return {}

    # Filtrage périmètre : types TP, hors PREV (MEP/IND/INPPREVIND) et hors société 073
    # Offres PREV explicitement non éligibles TP (demande métier)
    OFFRES_PREV_NON_ELIGIBLES = {"INPPREVIND"}
    mask_type = df_new_s[col_type].isin(TYPES_TP)
    mask_prev = (
        df_new_s["NS_offre"].str.startswith(("MEP", "IND"), na=False)
        | df_new_s["NS_offre"].isin(OFFRES_PREV_NON_ELIGIBLES)
    )
    mask_073  = df_new_s["NS_code_soc"].astype(str).str.strip() == "073"
    df_work = df_new_s[mask_type & ~mask_prev & ~mask_073].copy()

    def analyze_contract_tp(row):
        date_adh_str = str(row.get("NS_date_adh", "") or "").strip()
        date_eff_str = str(row.get("NS_date_effet", "") or "").strip()
        if not date_adh_str or not date_eff_str:
            return "FUTURE_TP", None

        try:
            d_adh = datetime.strptime(date_adh_str, "%Y-%m-%d").date()
            d_eff = datetime.strptime(date_eff_str, "%Y-%m-%d").date()
        except Exception:
            return "FUTURE_TP", None

        delta = (d_eff - d_adh).days
        if delta < 22:
            return "ELIGIBLE_TP", d_eff
        else:
            return "FUTURE_TP", d_eff

    tp_result = df_work.apply(analyze_contract_tp, axis=1)
    df_work["_tp_status"] = [r[0] for r in tp_result]
    df_work["_tp_elig_date"] = [r[1] for r in tp_result]

    # Statut par personne (priorité : ELIGIBLE > FUTURE)
    STATUS_PRIORITY = {"ELIGIBLE_TP": 2, "FUTURE_TP": 1}
    person_status_map = {}

    for num_pers, group in df_work.groupby(col_pers):
        best_row = None
        best_prio = 0
        for _, r in group.iterrows():
            prio = STATUS_PRIORITY.get(r["_tp_status"], 0)
            if prio > best_prio:
                best_prio = prio
                best_row = r
        if best_row is not None:
            person_status_map[num_pers] = {
                "status": best_row["_tp_status"],
                "code": str(best_row.get("NS_code_soc", "UNK")),
                "offre": str(best_row.get("NS_offre", "UNK")),
                "type": str(best_row.get(col_type, "UNK")),
                "elig_date": best_row["_tp_elig_date"],
            }

    count_eligible = sum(1 for d in person_status_map.values() if d["status"] == "ELIGIBLE_TP")
    count_future = sum(1 for d in person_status_map.values() if d["status"] == "FUTURE_TP")

    pop_base = count_eligible + count_future
    taux_eligible = round(count_eligible / pop_base * 100, 2) if pop_base > 0 else 0.0
    taux_future = round(count_future / pop_base * 100, 2) if pop_base > 0 else 0.0

    # Répartition par mois d'éligibilité carte TP (tous statuts : ELIGIBLE_TP + FUTURE_TP)
    eligible_by_month: Dict[str, int] = {}
    for pid, info in person_status_map.items():
        if info["status"] in ("ELIGIBLE_TP", "FUTURE_TP"):
            eff_date = info.get("elig_date")
            if eff_date is not None:
                try:
                    month_key = eff_date.strftime("%Y-%m")
                    eligible_by_month[month_key] = eligible_by_month.get(month_key, 0) + 1
                except Exception:
                    pass
    eligible_by_month = dict(sorted(eligible_by_month.items()))

    # Détail par (code | offre | type) pour Annexe
    breakdown_counts: Dict[str, Dict] = {}
    for pid, info in person_status_map.items():
        key = f"{info['code']} | {info['offre']} | {info['type']}"
        breakdown_counts.setdefault(key, {"eligible": 0, "future": 0})
        if info["status"] == "ELIGIBLE_TP":
            breakdown_counts[key]["eligible"] += 1
        elif info["status"] == "FUTURE_TP":
            breakdown_counts[key]["future"] += 1

    formatted_bd = {
        k: {"cartes_tp": v["eligible"], "future_tp": v["future"]}
        for k, v in sorted(breakdown_counts.items())
    }

    eligible_ids = {pid for pid, d in person_status_map.items() if d["status"] == "ELIGIBLE_TP"}

    # Vue par ligne-contrat (pour annexe Ecart_Lignes_vs_Personnes) :
    # chaque ligne du df_work TP (après filtrage périmètre TP) avec son statut.
    line_tp_records = [
        {
            "num_personne": str(r.get(col_pers, "")).strip(),
            "type_assure": str(r.get(col_type, "UNK")).strip(),
            "status": r["_tp_status"],
        }
        for _, r in df_work[[col_pers, col_type, "_tp_status"]].iterrows()
    ]

    return {
        "status": "Calculé",
        # --- KPI principaux (section 7) ---
        "population_base": pop_base,
        "population_eligible": count_eligible,
        "taux_eligible": taux_eligible,
        "population_future": count_future,
        "taux_future": taux_future,
        "population_prev": 0,
        "eligible_par_mois": eligible_by_month,
        # --- Annexe ---
        "_annexe_detail_par_type": formatted_bd,
        "_annexe_non_eligible": 0,
        # --- Debug (pour usage interne) ---
        "_debug_eligible_ids": eligible_ids,
        "_debug_non_eligible_ids": set(),
        "_debug_person_tp_map": {
            pid: {"status": d["status"], "type": d["type"], "elig_date": d["elig_date"], "code": d["code"]}
            for pid, d in person_status_map.items()
        },
        "_debug_line_tp_records": line_tp_records,
    }

# =============================================================================
# KPI — CARTE TP GED (flux quotidien produit par 07_controle_tp_ged.py)
# =============================================================================

def calcul_kpi_tp_ged(prefix: str) -> Dict[str, Any]:
    """
    Calcule les KPI du contrôle journalier Carte TP GED à partir du fichier
    détail produit par 07_controle_tp_ged.py : Output/{PREFIX}_TP_GED_Detail.csv.

    Retourne un dict "prêt à injecter" dans le JSON KPI (section 7bis_Carte_TP_GED).
    Si le fichier n'existe pas, retourne un dict avec status="Absent" et des
    compteurs à 0 (aucune régression sur le reste du pipeline).

    Population = 1 ligne par (num_personne x contrat TP éligible).
    Les taux sont exprimés en % (arrondi 2 décimales).
    """
    detail_path = OUTPUT_DIR / f"{prefix}_TP_GED_Detail.csv"
    if not detail_path.exists():
        return {
            "status": "Absent",
            "definition": (
                "Contrôle journalier des cartes TP en GED. "
                "Fichier détail non trouvé : l'étape 07_controle_tp_ged.py "
                "n'a probablement pas été lancée pour ce préfixe."
            ),
            "Fichier_Detail_Attendu": detail_path.name,
            "Population_Eligible": 0,
            "Trouves_GED": {"Nombre": 0, "Taux": 0.0},
            "Non_Trouves_GED": {"Nombre": 0, "Taux": 0.0},
            "Corrections_Manuelles": {"Nombre": 0, "Taux": 0.0},
            "Statut_Final": {"Rapproche": 0, "Non_Rapproche": 0, "Taux_Rapprochement_Final": 0.0},
            "Par_Societe": {},
        }

    df = load_csv(detail_path, sep=None)
    if df is None or df.empty:
        return {
            "status": "Vide",
            "definition": "Contrôle journalier des cartes TP en GED. Détail vide.",
            "Fichier_Detail": detail_path.name,
            "Population_Eligible": 0,
            "Trouves_GED": {"Nombre": 0, "Taux": 0.0},
            "Non_Trouves_GED": {"Nombre": 0, "Taux": 0.0},
            "Corrections_Manuelles": {"Nombre": 0, "Taux": 0.0},
            "Statut_Final": {"Rapproche": 0, "Non_Rapproche": 0, "Taux_Rapprochement_Final": 0.0},
            "Par_Societe": {},
        }

    # Normalisation colonnes (load_csv les met déjà en minuscules)
    df = df.copy()
    for col in ("statut_rapprochement", "statut_final", "correction_manuelle",
                "present_ged", "code_societe", "offre", "type_assure"):
        if col not in df.columns:
            df[col] = ""
        df[col] = df[col].fillna("").astype(str).str.strip()

    # Harmonisation : majuscule pour les statuts
    df["statut_rapprochement"] = df["statut_rapprochement"].str.upper()
    df["statut_final"] = df["statut_final"].str.upper()
    df["correction_manuelle"] = df["correction_manuelle"].str.upper()
    df["present_ged"] = df["present_ged"].str.upper()

    n_total = len(df)
    n_ged_oui = int((df["present_ged"] == "OUI").sum())
    n_ged_non = int((df["present_ged"] == "NON").sum())
    n_corr = int((df["correction_manuelle"] == "OUI").sum())
    n_rapp_final = int((df["statut_final"] == "RAPPROCHE").sum())
    n_nonrapp_final = int((df["statut_final"] == "NON_RAPPROCHE").sum())

    taux = lambda num, den: round(num / den * 100.0, 2) if den > 0 else 0.0

    # Ventilation par société
    par_soc: Dict[str, Dict[str, Any]] = {}
    for soc, grp in df.groupby("code_societe"):
        base = len(grp)
        trouves = int((grp["present_ged"] == "OUI").sum())
        rapp_final = int((grp["statut_final"] == "RAPPROCHE").sum())
        par_soc[str(soc) or "(vide)"] = {
            "Population_Eligible": base,
            "Trouves_GED": trouves,
            "Taux_GED": taux(trouves, base),
            "Rapproche_Final": rapp_final,
            "Taux_Rapprochement_Final": taux(rapp_final, base),
        }

    # Récupération date du flux (1ère valeur non vide)
    date_flux = ""
    if "date_flux" in df.columns and not df["date_flux"].empty:
        first = df["date_flux"].dropna().astype(str).str.strip()
        first = first[first != ""]
        if not first.empty:
            date_flux = first.iloc[0]

    return {
        "status": "Calculé",
        "definition": (
            "Contrôle journalier de la présence en GED des cartes TP éligibles. "
            "Population TP éligible = ASSPRI/MPACTI/MPRETR/MPVRET, hors conjoints, "
            "hors offres INP*/MEP*, hors société 073. "
            "KPEP de référence = KPEP IEHE correspondant au code société."
        ),
        "Fichier_Detail": detail_path.name,
        "Date_Flux": date_flux,
        "Population_Eligible": n_total,
        "Trouves_GED": {"Nombre": n_ged_oui, "Taux": taux(n_ged_oui, n_total)},
        "Non_Trouves_GED": {"Nombre": n_ged_non, "Taux": taux(n_ged_non, n_total)},
        "Corrections_Manuelles": {
            "Nombre": n_corr,
            "Taux": taux(n_corr, n_total),
            "definition": (
                "Lignes dont le statut_final a été forcé via "
                "Input_Data/TP_GED_Corrections.csv (faux KO réconciliés)."
            ),
        },
        "Statut_Final": {
            "Rapproche": n_rapp_final,
            "Non_Rapproche": n_nonrapp_final,
            "Taux_Rapprochement_Final": taux(n_rapp_final, n_total),
        },
        "Par_Societe": dict(sorted(par_soc.items())),
    }


def calcul_kpi5_rejets_anciennete(prefix: str) -> Dict[str, Any]:
    """
    Restitue, pour chaque code origine et chaque boite de traitement,
    l'anciennete (en jours) du rejet le plus vieux observe sur la semaine
    de reference. Alimente la section 8_Autres_Indicateurs.Prestations_Rejets_Plus_Anciens.

    Source : output/SQL/<DDMMYYYY>/<HHMMSS>_Resultats_SQL.xlsx produit par
    launch_SQL_query_V2.py a partir des requetes S_KPI5-*.sql (NMASS, LUMAS,
    OXA, TPCVM, TPHOS, TPVIA, Autre). Chaque feuille KPI5-<ORI> contient au
    minimum les colonnes PSASBOITRT, AGE_JOURS, PSRJCODREJ.

    Strategie de localisation :
      1) output/SQL/<prefix>/  (preference)
      2) sinon, le dossier output/SQL/* le plus recent

    Si rien n'est trouve, retourne un bloc avec Statut="Absent" et un
    squelette Par_Origine vide pour chaque origine connue.
    """
    origines = ["NMASS", "LUMAS", "OXA", "TPCVM", "TPHOS", "TPVIA", "Autre"]
    libelles = {
        "NMASS": "NMASS",
        "LUMAS": "LUMAS — Luminess",
        "OXA": "OXA",
        "TPCVM": "TPCVM",
        "TPHOS": "TPHOS",
        "TPVIA": "TPVIA",
        "Autre": "Autres origines (hors 6 listées)",
    }
    par_origine_vide = {
        ori: {"Libelle": libelles[ori], "Nb_Boites": 0,
              "Anciennete_Max_Globale_Jours": None, "Par_Boite": {}}
        for ori in origines
    }
    definition = (
        "Pour chaque code origine et chaque boite de traitement, anciennete "
        "(en jours) du rejet le plus vieux observe sur la semaine de reference. "
        "Issu des requetes hebdo KPI5 (S_KPI5-*.sql) exportees par "
        "launch_SQL_query_V2.py dans output/SQL/<DDMMYYYY>/<HHMMSS>_Resultats_SQL.xlsx."
    )

    sql_root = next(
        (p for p in (BASE_DIR / "output" / "SQL", BASE_DIR / "Output" / "SQL") if p.exists()),
        None,
    )
    if sql_root is None:
        return {
            "definition": definition,
            "Statut": "Absent",
            "Motif": "Repertoire output/SQL/ introuvable.",
            "Par_Origine": par_origine_vide,
        }

    dir_prefix = sql_root / prefix
    candidate_dirs: List[Path] = []
    if dir_prefix.is_dir():
        candidate_dirs.append(dir_prefix)
    candidate_dirs.extend(sorted(
        (p for p in sql_root.iterdir() if p.is_dir() and p != dir_prefix),
        key=lambda p: p.stat().st_mtime, reverse=True,
    ))

    # Priorite au xlsx dedie KPI5 (output split par categorie), fallback
    # sur les anciens *_Resultats_SQL.xlsx mono-fichier.
    xlsx_path: Optional[Path] = None
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
        return {
            "definition": definition,
            "Statut": "Absent",
            "Motif": f"Aucun xlsx KPI5*_Resultats_SQL.xlsx trouve sous {sql_root}.",
            "Par_Origine": par_origine_vide,
        }

    try:
        sheets = pd.read_excel(xlsx_path, sheet_name=None, engine="openpyxl")
    except Exception as exc:
        return {
            "definition": definition,
            "Statut": "Erreur",
            "Motif": f"Lecture xlsx impossible : {exc}",
            "Source": str(xlsx_path),
            "Par_Origine": par_origine_vide,
        }

    par_origine: Dict[str, Any] = {}
    for ori in origines:
        bloc_vide = par_origine_vide[ori]
        # Nom de feuille produit par launch_SQL_query_V2 : "KPI5-<ORI>" (tronque a 31).
        target = f"KPI5-{ori}"[:31]
        matches = [k for k in sheets if k == target or k.startswith(target)]
        if not matches:
            par_origine[ori] = bloc_vide
            continue
        df = sheets[matches[0]]
        if df is None or df.empty:
            par_origine[ori] = bloc_vide
            continue

        cols = {str(c).upper(): c for c in df.columns}
        col_boite = cols.get("PSASBOITRT")
        col_age = cols.get("AGE_JOURS")
        col_codrej = cols.get("PSRJCODREJ")
        if not (col_boite and col_age):
            par_origine[ori] = bloc_vide
            continue

        keep = [col_boite, col_age] + ([col_codrej] if col_codrej else [])
        sub = df[keep].copy()
        sub[col_age] = pd.to_numeric(sub[col_age], errors="coerce")
        sub = sub.dropna(subset=[col_boite, col_age])
        if sub.empty:
            par_origine[ori] = bloc_vide
            continue

        idx_max = sub.groupby(col_boite)[col_age].idxmax()
        sub_max = sub.loc[idx_max]

        par_boite: Dict[str, Any] = {}
        for _, row in sub_max.iterrows():
            boite = str(row[col_boite]).strip()
            entry: Dict[str, Any] = {"Anciennete_Max_Jours": int(row[col_age])}
            if col_codrej and pd.notna(row[col_codrej]):
                entry["Code_Rejet"] = str(row[col_codrej]).strip()
            par_boite[boite] = entry

        par_origine[ori] = {
            "Libelle": libelles[ori],
            "Nb_Boites": len(par_boite),
            "Anciennete_Max_Globale_Jours": int(sub[col_age].max()),
            "Par_Boite": dict(sorted(par_boite.items())),
        }

    # Date_Suivi deduit du nom de dossier parent (DDMMYYYY -> DD/MM/YYYY)
    parent_name = xlsx_path.parent.name
    if len(parent_name) == 8 and parent_name.isdigit():
        date_suivi = f"{parent_name[:2]}/{parent_name[2:4]}/{parent_name[4:]}"
    else:
        date_suivi = ""

    try:
        source_rel = str(xlsx_path.relative_to(BASE_DIR))
    except ValueError:
        source_rel = str(xlsx_path)

    return {
        "definition": definition,
        "Date_Suivi": date_suivi,
        "Source": source_rel,
        "Statut": "Charge",
        "Par_Origine": par_origine,
    }


def calcul_prestations_par_offre(prefix: str) -> Dict[str, Any]:
    """T1 — Synthèse des prestations par offre dans la section 8.

    Lit le classeur pivot produit par `launch_SQL_query_V2.py` + post-process
    `pivot_prestations_par_offre`. Stratégie de localisation identique à
    `calcul_kpi5_rejets_anciennete` :
      1) output/SQL/<prefix>/MDG_Prestations_par_offre*Pivot*.xlsx
      2) sinon, le plus récent toutes dates confondues.

    Si le fichier est absent : Statut="Absent" + squelette vide.
    Si la lecture échoue   : Statut="Erreur" + motif.
    """
    definition = (
        "Quotidien J-1 + stock hebdo par offre. Source : "
        "output/SQL/<DDMMYYYY>/MDG_Prestations_par_offre_<HHMMSS>_Pivot.xlsx "
        "produit par launch_SQL_query_V2.py (post_process pivot_prestations_par_offre)."
    )
    try:
        import prestations_par_offre_lib as ppo
    except ImportError as exc:
        return {"definition": definition, "Statut": "Erreur",
                "Motif": f"Import prestations_par_offre_lib KO : {exc}"}

    sql_root = next(
        (p for p in (BASE_DIR / "output" / "SQL", BASE_DIR / "Output" / "SQL")
         if p.exists()),
        None,
    )
    if sql_root is None:
        return {"definition": definition, "Statut": "Absent",
                "Motif": "Repertoire output/SQL/ introuvable."}

    pivot_path = ppo.find_latest_pivot(sql_root, prefix_ddmmyyyy=prefix)
    if pivot_path is None:
        return {"definition": definition, "Statut": "Absent",
                "Motif": ("Aucun classeur MDG_Prestations_par_offre*Pivot*.xlsx "
                          f"trouve sous {sql_root}.")}

    payload = ppo.read_pivot_for_json(pivot_path)
    payload["definition"] = definition
    return payload


# =============================================================================
# NOUVEAUX KPI — A : QUALITÉ NEW_S
# =============================================================================

def analyse_completude_new_s(df_new_s, types_assures_ai, col_type, col_pers):
    """
    A1 : Complétude des clés de rapprochement (par personne, sur assurés ciblés CIAM).
    A2 : Complétude par champ clé dans New_S (niveau ligne, assurés).
    A3 : Taux de radiation (dateradassure renseigné).
    """
    if df_new_s is None or df_new_s.empty or not col_type or not col_pers:
        return {}

    mask_assure = df_new_s[col_type].isin(types_assures_ai)
    df_ai = df_new_s[mask_assure].copy()
    n_total = len(df_new_s)
    n_ai = len(df_ai)

    # --- A3 : Radiation ---
    n_rad_all = int((df_new_s["NS_date_rad"].fillna("").str.strip() != "").sum())
    n_rad_ai = int((df_ai["NS_date_rad"].fillna("").str.strip() != "").sum())

    a3 = {
        "definition": "Lignes avec dateradassure renseigné (non-vide).",
        "Toute_Population": {
            "Nombre": n_rad_all,
            "Pct": round(n_rad_all / n_total * 100, 2) if n_total > 0 else 0.0,
            "Base": n_total,
        },
        "Assures_Uniquement": {
            "Nombre": n_rad_ai,
            "Pct": round(n_rad_ai / n_ai * 100, 2) if n_ai > 0 else 0.0,
            "Base": n_ai,
        },
    }

    # --- A2 : Complétude par champ clé (niveau ligne, assurés) ---
    def completude(col):
        if col not in df_ai.columns:
            return {"Rempli": 0, "Vide": n_ai, "Pct_Rempli": 0.0}
        rempli = int((df_ai[col].fillna("").str.strip() != "").sum())
        return {
            "Rempli": rempli,
            "Vide": n_ai - rempli,
            "Pct_Rempli": round(rempli / n_ai * 100, 2) if n_ai > 0 else 0.0,
        }

    a2 = {
        "definition": "Taux de remplissage des champs clés dans New_S (assurés, niveau ligne).",
        "Base_Lignes_Assures": n_ai,
        "Champs": {
            "mailciam": completude("NS_email_ciam"),
            "idkpep": completude("NS_kpep"),
            "date_naissance": completude("NS_ddn"),
            "nom": completude("NS_nom"),
            "prenom": completude("NS_prenom"),
        },
    }

    # --- A1 : Complétude des clés de rapprochement (niveau personne) ---
    n_with_email = 0
    n_with_kpep = 0
    n_with_identite = 0
    n_with_any = 0
    n_no_key = 0
    total_persons = 0

    for _, g in df_ai.groupby(col_pers, dropna=False):
        total_persons += 1
        has_email = any(
            e
            for v in list(g["NS_email_ciam"]) + list(g["NS_email_val"])
            for e in extract_emails(v)
        )
        has_kpep = any(
            str(k).strip() not in ("", "NULL", "null")
            for k in g["NS_kpep"].tolist()
            if pd.notna(k)
        )
        has_ident = any(
            str(nom).strip() and str(pre).strip() and str(ddn).strip()
            for nom, pre, ddn in zip(g["NS_nom"], g["NS_prenom"], g["NS_ddn"])
        )
        if has_email:
            n_with_email += 1
        if has_kpep:
            n_with_kpep += 1
        if has_ident:
            n_with_identite += 1
        if has_email or has_kpep or has_ident:
            n_with_any += 1
        else:
            n_no_key += 1

    def fmt(n):
        return {"Nombre": n, "Pct": round(n / total_persons * 100, 2) if total_persons > 0 else 0.0}

    a1 = {
        "definition": (
            "Nombre de personnes uniques (assurés ciblés CIAM) disposant d'au moins une clé "
            "de rapprochement : email (mailciam ou valeur_coordonnee), KPEP, ou identité complète."
        ),
        "Population_Assures_Uniques": total_persons,
        "Avec_Au_Moins_Une_Cle": fmt(n_with_any),
        "Sans_Aucune_Cle": fmt(n_no_key),
        "Avec_Email": fmt(n_with_email),
        "Avec_KPEP": fmt(n_with_kpep),
        "Avec_Identite_Complete": fmt(n_with_identite),
    }

    return {
        "A1_Completude_Cles_Rapprochement": a1,
        "A2_Completude_Champs_NewS": a2,
        "A3_Radiation": a3,
    }


# =============================================================================
# NOUVEAUX KPI — B : MATCHING CIAM ENRICHI
# =============================================================================

def analyse_rapprochement_par_segment(df_ciam_target, df_assures, col_pers, col_type, col_soc, kc_data):
    """
    B1 : Taux de rapprochement CIAM par type d'assuré.
    B2 : Taux de rapprochement CIAM par société.
    B3 : Décomposition des non-rapprochés (aucune clé vs clé présente non trouvée).
    B4 : Cohérence KPEP New_S vs KPEP du compte CIAM matché.
    """
    if df_ciam_target is None or df_ciam_target.empty or col_pers not in df_ciam_target.columns:
        return {}

    # Lookup dicts: person → type / société
    type_lookup: Dict[Any, str] = {}
    soc_lookup: Dict[Any, str] = {}
    if col_type and col_pers in df_assures.columns and col_type in df_assures.columns:
        type_lookup = df_assures.groupby(col_pers, dropna=False)[col_type].first().to_dict()
    if col_soc and col_soc in df_assures.columns:
        soc_lookup = df_assures.groupby(col_pers, dropna=False)[col_soc].first().to_dict()

    type_series = df_ciam_target[col_pers].map(type_lookup).fillna("UNK")
    soc_series = df_ciam_target[col_pers].map(soc_lookup).fillna("UNK")
    status_series = df_ciam_target["match_status"]

    def taux_par_groupe(group_series):
        result = {}
        for grp_val in group_series.unique():
            mask_grp = group_series == grp_val
            total = int(mask_grp.sum())
            rap = int((mask_grp & (status_series == "RAPPROCHE")).sum())
            result[str(grp_val)] = {
                "Total": total,
                "Rapproches": rap,
                "Non_Rapproches": total - rap,
                "Taux": round(rap / total * 100, 2) if total > 0 else 0.0,
            }
        return dict(sorted(result.items()))

    b1 = taux_par_groupe(type_series)
    b2 = taux_par_groupe(soc_series)

    # --- B3 : Décomposition des non-rapprochés ---
    mask_nr = status_series == "NON_RAPPROCHE"
    df_nr = df_ciam_target[mask_nr]
    n_nr = int(mask_nr.sum())

    if not df_nr.empty:
        has_email = (
            df_nr["emails_ciam"].apply(lambda x: bool(x))
            | df_nr["emails_val"].apply(lambda x: bool(x))
        )
        has_kpep = df_nr["kpeps"].apply(lambda x: bool(x))
        has_ident = df_nr["ident_full"].apply(lambda x: bool(x))
        has_any = has_email | has_kpep | has_ident
        n_key_present = int(has_any.sum())
        n_no_key = int((~has_any).sum())
    else:
        n_no_key = 0
        n_key_present = 0

    def fmtnr(n):
        return {"Nombre": n, "Pct": round(n / n_nr * 100, 2) if n_nr > 0 else 0.0}

    b3 = {
        "definition": (
            "Parmi les non-rapprochés : distingue l'absence totale de clé de rapprochement "
            "des cas où une clé est présente mais introuvable dans CIAM."
        ),
        "Total_Non_Rapproches": n_nr,
        "Aucune_Cle_Disponible": fmtnr(n_no_key),
        "Cle_Presente_Non_Trouvee": fmtnr(n_key_present),
    }

    # --- B4 : Cohérence KPEP New_S vs CIAM ---
    kpep_ciam_map: Dict[str, str] = {}
    if kc_data and kc_data.get("all") is not None and not kc_data["all"].empty:
        ref = kc_data["all"].drop_duplicates(subset=["id"]).set_index("id")
        if "idkpep" in ref.columns:
            kpep_ciam_map = ref["idkpep"].fillna("").astype(str).to_dict()

    mask_rap = status_series == "RAPPROCHE"
    df_rap = df_ciam_target[mask_rap]

    n_ns_has_kpep = 0
    n_kpep_match = 0
    n_kpep_diff = 0
    n_ciam_no_kpep = 0

    for _, row in df_rap.iterrows():
        ns_kpeps = row.get("kpeps") or []
        if not ns_kpeps:
            continue
        n_ns_has_kpep += 1
        ciam_kpep = kpep_ciam_map.get(str(row.get("matched_id", "")), "")
        if not ciam_kpep:
            n_ciam_no_kpep += 1
        elif ciam_kpep in ns_kpeps:
            n_kpep_match += 1
        else:
            n_kpep_diff += 1

    def fmtb4(n):
        return {"Nombre": n, "Pct": round(n / n_ns_has_kpep * 100, 2) if n_ns_has_kpep > 0 else 0.0}

    b4 = {
        "definition": (
            "Parmi les rapprochés ayant un KPEP dans New_S : "
            "cohérence entre ce KPEP et le KPEP du compte CIAM matché."
        ),
        "Base_Rapproches_Avec_KPEP_NewS": n_ns_has_kpep,
        "KPEP_Coherent": fmtb4(n_kpep_match),
        "KPEP_Different": fmtb4(n_kpep_diff),
        "CIAM_Sans_KPEP": fmtb4(n_ciam_no_kpep),
    }

    # --- H1b : Taux par méthode × société ---
    df_rap = df_ciam_target[mask_rap].copy()
    df_rap["_soc"] = df_rap[col_pers].map(soc_lookup).fillna("UNK")
    h1b: Dict[str, Dict[str, int]] = {}
    for _, row_h in df_rap.iterrows():
        m = str(row_h.get("match_method") or "")
        s = str(row_h.get("_soc") or "UNK")
        h1b.setdefault(m, {})[s] = h1b.get(m, {}).get(s, 0) + 1
    h1b = {k: dict(sorted(v.items())) for k, v in sorted(h1b.items())}

    # --- H1c / H1d : Non rapprochés avec clé NS valide absente de CIAM ---
    df_all_ck = kc_data.get("all") if kc_data else None
    ciam_kpep_set: set = set()
    ciam_email_set: set = set()
    if df_all_ck is not None and not df_all_ck.empty:
        if "idkpep" in df_all_ck.columns:
            _kpeps = df_all_ck["idkpep"].dropna().astype(str).str.strip().str.upper()
            ciam_kpep_set = set(_kpeps[_kpeps.str.startswith("KPEP")])
        for _em_col in ("email", "email_other"):
            if _em_col in df_all_ck.columns:
                _ems = df_all_ck[_em_col].dropna().astype(str).str.strip().str.lower()
                ciam_email_set.update(_ems[_ems != ""])

    n_hc_has_kpep = 0   # NS a un KPEP absent de CIAM
    n_hd_has_email = 0  # NS a un email absent de CIAM

    for _, row_nr in df_nr.iterrows():
        kpeps_ns = row_nr.get("kpeps") or []
        if kpeps_ns:
            kpeps_up = {k.strip().upper() for k in kpeps_ns if k}
            if kpeps_up and not kpeps_up.intersection(ciam_kpep_set):
                n_hc_has_kpep += 1

        emails_ns = list(row_nr.get("emails_ciam") or []) + list(row_nr.get("emails_val") or [])
        if emails_ns:
            emails_lo = {e.strip().lower() for e in emails_ns if e}
            if emails_lo and not emails_lo.intersection(ciam_email_set):
                n_hd_has_email += 1

    def fmth(n):
        return {"Nombre": n, "Pct": round(n / n_nr * 100, 2) if n_nr > 0 else 0.0}

    return {
        "B1_Par_Type_Assure": {
            "definition": "Taux de rapprochement CIAM par type d'assuré (ASSPRI, MPACTI, MPRETR, MPVRET).",
            "Resultats": b1,
        },
        "B2_Par_Societe": {
            "definition": "Taux de rapprochement CIAM par code société (code_soc_appart).",
            "Resultats": b2,
        },
        "B3_Decomposition_Non_Rapproches": b3,
        "B4_Coherence_KPEP": b4,
        "H1b_Methode_Par_Societe": {
            "definition": (
                "Répartition des méthodes de rapprochement par code société (rapprochés uniquement). "
                "Croisement méthode × code_soc_appart."
            ),
            "Resultats": h1b,
        },
        "H1c_NonRap_KPEP_NS_Absent_CIAM": {
            "definition": (
                "Parmi les non-rapprochés : personnes ayant un KPEP dans New_S "
                "mais dont ce KPEP est introuvable dans CIAM → compte potentiellement manquant."
            ),
            **fmth(n_hc_has_kpep),
        },
        "H1d_NonRap_Email_NS_Absent_CIAM": {
            "definition": (
                "Parmi les non-rapprochés : personnes ayant un email dans New_S (mailciam ou "
                "valeur_coordonnee) mais dont cet email est introuvable dans CIAM → "
                "email potentiellement à créer ou corriger dans CIAM."
            ),
            **fmth(n_hd_has_email),
        },
    }


# =============================================================================
# NOUVEAUX KPI — C : CARTE TP ENRICHIE
# =============================================================================

def analyse_tp_enrichi(person_tp_map, ids_assures, df_ciam_target, col_pers, date_ref_str,
                       matched_email_map=None):
    """
    C1 : Assurés éligibles TP non rapprochés dans CIAM.
    C2 : Délai moyen + distribution avant éligibilité (FUTURE_TP).
    C3 : Répartition des statuts TP par code société.
    """
    if not person_tp_map:
        return {}

    try:
        date_ref = datetime.strptime(date_ref_str, "%d/%m/%Y").date()
    except Exception:
        date_ref = date.today()

    # --- C1 ---
    ids_eligible_tp = {pid for pid, d in person_tp_map.items() if d["status"] == "ELIGIBLE_TP"}
    ids_rapproches: set = set()
    if df_ciam_target is not None and not df_ciam_target.empty and col_pers in df_ciam_target.columns:
        ids_rapproches = set(
            df_ciam_target.loc[df_ciam_target["match_status"] == "RAPPROCHE", col_pers].tolist()
        )
    ids_non_rap = ids_assures - ids_rapproches
    ids_elig_non_rap = ids_eligible_tp & ids_non_rap
    n_elig = len(ids_eligible_tp)
    n_elig_nr = len(ids_elig_non_rap)

    c1 = {
        "definition": (
            "Assurés classés ELIGIBLE_TP (carte TP déjà acquise) "
            "qui ne sont pas encore rapprochés dans CIAM."
        ),
        "Nb_Eligibles_TP": n_elig,
        "Nb_Non_Rapproches_CIAM": len(ids_non_rap),
        "Eligibles_TP_Non_Rapproches": {
            "Nombre": n_elig_nr,
            "Pct_Sur_Eligibles": round(n_elig_nr / n_elig * 100, 2) if n_elig > 0 else 0.0,
        },
    }

    # --- C2 : délai (en jours) entre date_adhesion et date_effet pour les FUTURE_TP ---
    # date_adhesion = date du flux (date_ref), elig_date = date_effet stockée
    delays = []
    for info in person_tp_map.values():
        if info["status"] == "FUTURE_TP" and info.get("elig_date") is not None:
            try:
                delta = (info["elig_date"] - date_ref).days
                delays.append(delta)
            except Exception:
                pass

    if delays:
        sorted_delays = sorted(delays)
        avg_delay = round(sum(delays) / len(delays), 1)
        med_delay = sorted_delays[len(delays) // 2]
        dist = {"0_30j": 0, "31_60j": 0, "61_90j": 0, "91_180j": 0, "plus_180j": 0}
        for d in delays:
            if d <= 30:
                dist["0_30j"] += 1
            elif d <= 60:
                dist["31_60j"] += 1
            elif d <= 90:
                dist["61_90j"] += 1
            elif d <= 180:
                dist["91_180j"] += 1
            else:
                dist["plus_180j"] += 1
    else:
        avg_delay = 0.0
        med_delay = 0
        dist = {}

    c2 = {
        "definition": (
            "Délai (en jours) entre date_adhesion et date_effet pour les FUTURE_TP "
            "(delta = date_effet - date_adhesion >= 22j)."
        ),
        "Population_Future_TP": len(delays),
        "Delai_Moyen_Jours": avg_delay,
        "Delai_Median_Jours": med_delay,
        "Delai_Min_Jours": min(delays) if delays else 0,
        "Delai_Max_Jours": max(delays) if delays else 0,
        "Distribution": dist,
    }

    # --- C3 : répartition TP par société ---
    c3: Dict[str, Dict[str, int]] = {}
    for info in person_tp_map.values():
        soc = info.get("code", "UNK")
        c3.setdefault(soc, {"ELIGIBLE_TP": 0, "FUTURE_TP": 0})
        c3[soc][info["status"]] = c3[soc].get(info["status"], 0) + 1

    # --- I1b : FUTURE_TP dont la date d'éligibilité est à plus de X jours d'aujourd'hui ---
    today = date.today()
    n_futur_gt30 = 0
    n_futur_gt60 = 0
    n_futur_gt90 = 0
    n_futur_total = 0
    for info in person_tp_map.values():
        if info["status"] == "FUTURE_TP" and info.get("elig_date") is not None:
            try:
                days_remaining = (info["elig_date"] - today).days
                n_futur_total += 1
                if days_remaining > 30:
                    n_futur_gt30 += 1
                if days_remaining > 60:
                    n_futur_gt60 += 1
                if days_remaining > 90:
                    n_futur_gt90 += 1
            except Exception:
                pass

    def fmti1b(n):
        return {"Nombre": n, "Pct": round(n / n_futur_total * 100, 2) if n_futur_total > 0 else 0.0}

    i1b = {
        "definition": (
            "Assurés FUTURE_TP dont la date d'éligibilité TP est à plus de X jours d'aujourd'hui "
            "(délai restant calculé depuis la date d'exécution du script)."
        ),
        "Base_Future_TP": n_futur_total,
        "Futur_GT_30j": fmti1b(n_futur_gt30),
        "Futur_GT_60j": fmti1b(n_futur_gt60),
        "Futur_GT_90j": fmti1b(n_futur_gt90),
    }

    # --- I1c : Éligibles TP sans email CIAM valide ---
    pid_to_mid: Dict[Any, Any] = {}
    if df_ciam_target is not None and not df_ciam_target.empty and col_pers in df_ciam_target.columns:
        rap_rows = df_ciam_target[df_ciam_target["match_status"] == "RAPPROCHE"]
        pid_to_mid = rap_rows.set_index(col_pers)["matched_id"].to_dict()

    em_map = matched_email_map if matched_email_map is not None else {}
    n_elig_sans_email = 0
    for pid in ids_eligible_tp:
        mid = pid_to_mid.get(pid)
        if mid is None:
            n_elig_sans_email += 1
        else:
            em = str(em_map.get(str(mid), "") or "").strip()
            if not em or not EMAIL_VALID_RE.match(em.upper()):
                n_elig_sans_email += 1

    n_elig = len(ids_eligible_tp)

    i1c = {
        "definition": (
            "Assurés ELIGIBLE_TP (carte TP déjà acquise) sans email CIAM valide : "
            "non rapprochés dans CIAM OU email du compte CIAM vide/invalide. "
            "Ces personnes ne peuvent pas être notifiées par email."
        ),
        "Base_Eligibles_TP": n_elig,
        "Sans_Email_CIAM_Valide": {
            "Nombre": n_elig_sans_email,
            "Pct": round(n_elig_sans_email / n_elig * 100, 2) if n_elig > 0 else 0.0,
        },
    }

    return {
        "C1_Eligibles_TP_Non_Rapproches": c1,
        "C2_Delai_Futur_TP": c2,
        "C3_Repartition_TP_Par_Societe": {
            "definition": (
                "Répartition des statuts TP (ELIGIBLE_TP / FUTURE_TP) "
                "par code société (hors PREV et société 073)."
            ),
            "Resultats": dict(sorted(c3.items())),
        },
        "I1b_Future_TP_Delai_Restant": i1b,
        "I1c_Eligibles_TP_Sans_Email_CIAM": i1c,
    }


# =============================================================================
# NOUVEAUX KPI — D : IEHE QUALITÉ ENRICHIE
# =============================================================================

def analyse_iehe_qualite(df_iehe, df_new_s, df_ciam_target, col_pers, matched_email_map):
    """
    D1 : Complétude de l'email de contact (adrmailctc) dans IEHE.
    D2 : Concordance email IEHE vs email du compte CIAM matché.
    D3 : Concordance socappr (IEHE) vs code_soc_appart (New_S).
    """
    if df_iehe is None or df_iehe.empty:
        return {}

    n_iehe = len(df_iehe)

    # --- D1 : Complétude email IEHE ---
    if "adrmailctc" in df_iehe.columns:
        n_rempli = int((df_iehe["adrmailctc"].fillna("").str.strip() != "").sum())
    else:
        n_rempli = 0
    n_vide = n_iehe - n_rempli

    d1 = {
        "definition": "Présence de l'email de contact (adrmailctc) dans le référentiel IEHE.",
        "Population_IEHE": n_iehe,
        "Email_Rempli": {
            "Nombre": n_rempli,
            "Pct": round(n_rempli / n_iehe * 100, 2) if n_iehe > 0 else 0.0,
        },
        "Email_Vide": {
            "Nombre": n_vide,
            "Pct": round(n_vide / n_iehe * 100, 2) if n_iehe > 0 else 0.0,
        },
    }

    # --- D2 : Concordance email IEHE vs email CIAM (rapprochés présents dans IEHE) ---
    d2: Dict[str, Any]
    if "refperboccn" not in df_iehe.columns or "adrmailctc" not in df_iehe.columns:
        d2 = {"status": "colonnes refperboccn ou adrmailctc absentes de IEHE"}
    elif df_ciam_target is None or df_ciam_target.empty or col_pers not in df_ciam_target.columns:
        d2 = {"status": "df_ciam_target non disponible"}
    else:
        iehe_email_map = (
            df_iehe[["refperboccn", "adrmailctc"]]
            .drop_duplicates(subset=["refperboccn"])
            .set_index("refperboccn")["adrmailctc"]
            .fillna("")
            .astype(str)
            .str.strip()
            .to_dict()
        )
        mask_rap = df_ciam_target["match_status"] == "RAPPROCHE"
        n_comm = 0
        n_eq = 0
        n_diff = 0
        n_iehe_vide = 0
        n_ciam_vide = 0
        n_deux_vides = 0

        for _, row in df_ciam_target[mask_rap].iterrows():
            pid = str(row[col_pers])
            if pid not in iehe_email_map:
                continue
            n_comm += 1
            iehe_em = normalize_email(iehe_email_map[pid])
            mid = row.get("matched_id", "")
            ciam_em = normalize_email(matched_email_map.get(mid, "") if mid else "")
            if not iehe_em and not ciam_em:
                n_deux_vides += 1
            elif not iehe_em:
                n_iehe_vide += 1
            elif not ciam_em:
                n_ciam_vide += 1
            elif iehe_em == ciam_em:
                n_eq += 1
            else:
                n_diff += 1

        def fmtd2(n):
            return {"Nombre": n, "Pct": round(n / n_comm * 100, 2) if n_comm > 0 else 0.0}

        d2 = {
            "definition": (
                "Concordance entre l'email IEHE (adrmailctc) et l'email du compte CIAM matché, "
                "pour les assurés rapprochés présents dans IEHE."
            ),
            "Base_Rapproches_Presents_IEHE": n_comm,
            "Emails_Identiques": fmtd2(n_eq),
            "Emails_Differents": fmtd2(n_diff),
            "IEHE_Vide_CIAM_Present": fmtd2(n_iehe_vide),
            "CIAM_Vide_IEHE_Present": fmtd2(n_ciam_vide),
            "Deux_Vides": fmtd2(n_deux_vides),
        }

    # --- D3 : Concordance socappr IEHE vs code_soc_appart New_S ---
    d3: Dict[str, Any]
    if "refperboccn" not in df_iehe.columns or "socappr" not in df_iehe.columns:
        d3 = {"status": "colonnes refperboccn ou socappr absentes de IEHE"}
    else:
        col_soc_ns = get_col_flexible(df_new_s, ["code_soc_appart", "code_societe", "code_soc"])
        if not col_soc_ns or col_pers not in df_new_s.columns:
            d3 = {"status": "colonnes requises absentes de New_S"}
        else:
            ns_soc_map = (
                df_new_s[[col_pers, col_soc_ns]]
                .drop_duplicates(subset=[col_pers])
                .set_index(col_pers)[col_soc_ns]
                .fillna("").astype(str).str.strip()
                .to_dict()
            )
            iehe_soc_map = (
                df_iehe[["refperboccn", "socappr"]]
                .drop_duplicates(subset=["refperboccn"])
                .set_index("refperboccn")["socappr"]
                .fillna("").astype(str).str.strip()
                .to_dict()
            )
            n_cmp = 0
            n_match = 0
            n_diff_soc = 0
            n_iehe_soc_vide = 0
            n_ns_soc_vide = 0

            for pid_iehe, iehe_soc in iehe_soc_map.items():
                ns_soc = ns_soc_map.get(str(pid_iehe), "")
                n_cmp += 1
                if not iehe_soc:
                    n_iehe_soc_vide += 1
                elif not ns_soc:
                    n_ns_soc_vide += 1
                elif iehe_soc == ns_soc:
                    n_match += 1
                else:
                    n_diff_soc += 1

            def fmtd3(n):
                return {"Nombre": n, "Pct": round(n / n_cmp * 100, 2) if n_cmp > 0 else 0.0}

            d3 = {
                "definition": (
                    "Concordance entre socappr (IEHE) et code_soc_appart (New_S), "
                    "par personne (jointure sur refperboccn = num_personne)."
                ),
                "Base_Personnes_IEHE": n_cmp,
                "Societes_Identiques": fmtd3(n_match),
                "Societes_Differentes": fmtd3(n_diff_soc),
                "IEHE_Soc_Vide": fmtd3(n_iehe_soc_vide),
                "NS_Soc_Vide": fmtd3(n_ns_soc_vide),
            }

    # --- J1c : Email IEHE invalide (format) ---
    if "adrmailctc" in df_iehe.columns:
        _ems_iehe = df_iehe["adrmailctc"].fillna("").astype(str).str.strip()
        _ems_non_vides = _ems_iehe[_ems_iehe != ""]
        n_j1c_remplis = len(_ems_non_vides)
        n_j1c_invalides = int(
            (~_ems_non_vides.str.upper().str.match(EMAIL_VALID_RE.pattern)).sum()
        )
        j1c = {
            "definition": (
                "Parmi les emails IEHE (adrmailctc) renseignés : "
                "emails au format invalide (ne respectant pas la syntaxe standard)."
            ),
            "Base_Emails_Remplis": n_j1c_remplis,
            "Emails_Invalides": {
                "Nombre": n_j1c_invalides,
                "Pct": round(n_j1c_invalides / n_j1c_remplis * 100, 2) if n_j1c_remplis > 0 else 0.0,
            },
        }
    else:
        j1c = {"status": "colonne adrmailctc absente de IEHE"}

    # --- J1b : Email IEHE présent mais absent/différent de CIAM ---
    # (Potentiel d'enrichissement CIAM depuis IEHE)
    # Agrège : CIAM_Vide_IEHE_Present + Emails_Differents
    j1b: Dict[str, Any]
    if isinstance(d2, dict) and "CIAM_Vide_IEHE_Present" in d2 and "Emails_Differents" in d2:
        n_ciam_vide = d2["CIAM_Vide_IEHE_Present"].get("Nombre", 0)
        n_em_diff = d2["Emails_Differents"].get("Nombre", 0)
        n_j1b_total = n_ciam_vide + n_em_diff
        base_j1b = d2.get("Base_Rapproches_Presents_IEHE", 0)
        j1b = {
            "definition": (
                "Parmi les rapprochés présents dans IEHE : email IEHE renseigné mais "
                "absent ou différent dans CIAM → potentiel d'enrichissement. "
                "= CIAM_Vide_IEHE_Present + Emails_Differents."
            ),
            "Base": base_j1b,
            "Potentiel_Enrichissement": {
                "Nombre": n_j1b_total,
                "Pct": round(n_j1b_total / base_j1b * 100, 2) if base_j1b > 0 else 0.0,
            },
            "Dont_CIAM_Vide": {"Nombre": n_ciam_vide},
            "Dont_Email_Different": {"Nombre": n_em_diff},
        }
    else:
        j1b = {"status": "données D2 insuffisantes pour calculer J1b"}

    return {
        "D1_Completude_Email_IEHE": d1,
        "D2_Concordance_Email_IEHE_CIAM": d2,
        "D3_Concordance_Societe_IEHE_NS": d3,
        "J1b_Potentiel_Enrichissement_CIAM": j1b,
        "J1c_Emails_IEHE_Invalides": j1c,
    }


# =============================================================================
# NOUVEAUX KPI — E : INDICATEURS QUALITÉ ENRICHIS
# =============================================================================

def analyse_prospects_ciam(kc_data, df_new_s, df_ciam_target, col_pers):
    """
    E1 : Comptes CIAM créés sur des prospects (birthdate = '1900-01-01').

    a) E1a — Comptes Keycloak avec birthdate = '1900-01-01' (données fictives dans CIAM).
    b) E1b — Lignes New_S (assurés) avec date_naissance = '1900-01-01' (source).
    c) E1c — Parmi les rapprochés : combien ont un compte CIAM avec birthdate = '1900-01-01'.
    """
    PROSPECT_DATE = "1900-01-01"
    result: Dict[str, Any] = {}

    def fmt_c(n, total):
        return {"Nombre": n, "Pct": round(n / total * 100, 2) if total > 0 else 0.0}

    # E1a — Comptes CIAM (Keycloak) avec birthdate prospect
    df_all_ciam = kc_data.get("all") if kc_data else None
    n_ciam_total = 0
    n_ciam_prospects = 0
    if df_all_ciam is not None and not df_all_ciam.empty:
        id_col = "id" if "id" in df_all_ciam.columns else None
        n_ciam_total = int(df_all_ciam[id_col].nunique()) if id_col else len(df_all_ciam)
        if "birthdate" in df_all_ciam.columns and id_col:
            tmp = df_all_ciam.drop_duplicates(subset=[id_col]).copy()
            tmp["_ddn"] = tmp["birthdate"].apply(format_date_iso)
            n_ciam_prospects = int((tmp["_ddn"] == PROSPECT_DATE).sum())

    result["E1a_Comptes_CIAM_Prospects"] = {
        "definition": (
            "Comptes Keycloak (CK/CM) où la date de naissance (birthdate) normalisée "
            "= '1900-01-01'. Indique un compte créé sans date de naissance réelle (prospect)."
        ),
        "Total_Comptes_CIAM_Uniques": n_ciam_total,
        "Comptes_Prospects": fmt_c(n_ciam_prospects, n_ciam_total),
    }

    # E1b — Assurés New_S avec date_naissance prospect
    n_ns_total = 0
    n_ns_prospects = 0
    col_type_ns = get_col_flexible(df_new_s, ["type_assure", "typeassure"]) if df_new_s is not None else None
    if df_new_s is not None and not df_new_s.empty and "NS_ddn" in df_new_s.columns:
        df_ai_ns = (
            df_new_s[df_new_s[col_type_ns].isin(TYPES_ASSURES_IEHE)]
            if col_type_ns else df_new_s
        )
        n_ns_total = len(df_ai_ns)
        n_ns_prospects = int((df_ai_ns["NS_ddn"] == PROSPECT_DATE).sum())

    result["E1b_Assures_NS_Prospects"] = {
        "definition": (
            "Lignes New_S (assurés AI) où date_naissance normalisée = '1900-01-01'. "
            "Indique une date de naissance manquante ou fictive dans la source."
        ),
        "Total_Lignes_Assures": n_ns_total,
        "Lignes_Prospect": fmt_c(n_ns_prospects, n_ns_total),
    }

    # E1c — Rapprochés dont le compte CIAM matché est un prospect
    n_rapproches = 0
    n_rap_prospect = 0
    if (
        df_ciam_target is not None
        and not df_ciam_target.empty
        and df_all_ciam is not None
        and "birthdate" in df_all_ciam.columns
        and "id" in df_all_ciam.columns
    ):
        tmp = df_all_ciam.drop_duplicates(subset=["id"]).copy()
        tmp["_ddn"] = tmp["birthdate"].apply(format_date_iso)
        ciam_ddn_map = tmp.set_index("id")["_ddn"].to_dict()
        mask_rap = df_ciam_target["match_status"] == "RAPPROCHE"
        n_rapproches = int(mask_rap.sum())
        for mid in df_ciam_target.loc[mask_rap, "matched_id"].tolist():
            if ciam_ddn_map.get(str(mid), "") == PROSPECT_DATE:
                n_rap_prospect += 1

    result["E1c_Rapproches_Compte_CIAM_Prospect"] = {
        "definition": (
            "Parmi les assurés rapprochés dans CIAM : combien ont été associés à un compte "
            "Keycloak dont la birthdate = '1900-01-01' (compte créé sur un prospect)."
        ),
        "Base_Rapproches": n_rapproches,
        "Rapproches_Compte_Prospect": fmt_c(n_rap_prospect, n_rapproches),
    }

    return result


def analyse_coherence_kpep_3_sources(df_ciam_target, kc_data, df_iehe, col_pers):
    """
    E2 : Cohérence stricte du KPEP entre 3 sources : NS / CIAM / IEHE.

    - KPEP NS   : idkpep de New_S (agrégé dans kpeps par personne)
    - KPEP CIAM : idkpep du compte Keycloak matché
    - KPEP IEHE : colonne KPEP-format dans le fichier IEHE (si détectable)

    Normalisation : strip + upper. Matching exact uniquement. Pas de fuzzy.
    Base : assurés rapprochés.
    """
    if df_ciam_target is None or df_ciam_target.empty:
        return {}

    # Build CIAM KPEP map: ciam_id -> idkpep (upper)
    ciam_kpep_map: Dict[str, str] = {}
    df_all_ciam = kc_data.get("all") if kc_data else None
    if df_all_ciam is not None and "id" in df_all_ciam.columns and "idkpep" in df_all_ciam.columns:
        tmp = df_all_ciam.drop_duplicates(subset=["id"])
        ciam_kpep_map = (
            tmp.set_index("id")["idkpep"]
            .fillna("").astype(str).str.strip().str.upper()
            .to_dict()
        )

    # Détecter une colonne KPEP-format dans IEHE (valeurs commençant par 'KPEP')
    iehe_kpep_map: Dict[str, str] = {}
    iehe_kpep_available = False
    if df_iehe is not None and not df_iehe.empty and "refperboccn" in df_iehe.columns:
        for col in df_iehe.columns:
            if col == "refperboccn":
                continue
            sample = df_iehe[col].dropna().astype(str).str.strip()
            if sample[sample.str.upper().str.startswith("KPEP")].shape[0] > 0:
                tmp_iehe = df_iehe[["refperboccn", col]].copy()
                tmp_iehe[col] = tmp_iehe[col].fillna("").astype(str).str.strip().str.upper()
                tmp_iehe = tmp_iehe[tmp_iehe[col].str.startswith("KPEP")]
                if not tmp_iehe.empty:
                    iehe_kpep_map = (
                        tmp_iehe.drop_duplicates(subset=["refperboccn"])
                        .set_index("refperboccn")[col]
                        .to_dict()
                    )
                    iehe_kpep_available = True
                break

    mask_rap = df_ciam_target["match_status"] == "RAPPROCHE"
    df_rap = df_ciam_target[mask_rap]
    n_base = int(mask_rap.sum())

    n_ns_kpep = 0
    n_ciam_kpep = 0
    n_iehe_kpep = 0
    n_ns_eq_ciam = 0
    n_ns_diff_ciam = 0
    n_iehe_eq_ciam = 0
    n_iehe_diff_ciam = 0
    n_all3_eq = 0

    for _, row in df_rap.iterrows():
        ns_kpeps = [k.upper().strip() for k in (row.get("kpeps") or []) if k]
        mid = str(row.get("matched_id", ""))
        ciam_kpep = ciam_kpep_map.get(mid, "").strip()
        pid = str(row.get(col_pers, ""))
        iehe_kpep = iehe_kpep_map.get(pid, "").strip() if iehe_kpep_available else ""

        if ns_kpeps:
            n_ns_kpep += 1
        if ciam_kpep:
            n_ciam_kpep += 1
        if iehe_kpep:
            n_iehe_kpep += 1

        # NS vs CIAM
        ns_eq_ciam = bool(ns_kpeps) and bool(ciam_kpep) and (ciam_kpep in ns_kpeps)
        if bool(ns_kpeps) and bool(ciam_kpep):
            if ns_eq_ciam:
                n_ns_eq_ciam += 1
            else:
                n_ns_diff_ciam += 1

        if iehe_kpep_available and bool(iehe_kpep) and bool(ciam_kpep):
            if iehe_kpep == ciam_kpep:
                n_iehe_eq_ciam += 1
            else:
                n_iehe_diff_ciam += 1

        # Cohérence stricte 3 sources
        if iehe_kpep_available and bool(ns_kpeps) and bool(ciam_kpep) and bool(iehe_kpep):
            if ns_eq_ciam and iehe_kpep == ciam_kpep:
                n_all3_eq += 1

    def fmt(n):
        return {"Nombre": n, "Pct": round(n / n_base * 100, 2) if n_base > 0 else 0.0}

    result: Dict[str, Any] = {
        "definition": (
            "Cohérence stricte du KPEP entre NS (idkpep New_S), CIAM (idkpep Keycloak matché) "
            "et IEHE (si une colonne au format KPEP* est détectée dans le fichier IEHE chargé). "
            "Normalisation : strip + upper. Matching exact uniquement."
        ),
        "Base_Rapproches": n_base,
        "Disponibilite_KPEP": {
            "NS_Avec_KPEP": fmt(n_ns_kpep),
            "CIAM_Avec_KPEP": fmt(n_ciam_kpep),
            "IEHE_Avec_KPEP": fmt(n_iehe_kpep) if iehe_kpep_available else "non_disponible",
        },
        "E2_NS_Egal_CIAM": {
            **fmt(n_ns_eq_ciam),
            "definition": "KPEP NS == KPEP CIAM (les deux non-vides). Cohérence parfaite NS/CIAM.",
        },
        "E2_NS_Different_CIAM": {
            **fmt(n_ns_diff_ciam),
            "definition": "KPEP NS ≠ KPEP CIAM (les deux non-vides). Potentiel mauvais rapprochement.",
        },
    }

    if iehe_kpep_available:
        result["E2_IEHE_Egal_CIAM"] = {
            **fmt(n_iehe_eq_ciam),
            "definition": "KPEP IEHE == KPEP CIAM (les deux non-vides).",
        }
        result["E2_IEHE_Different_CIAM"] = {
            **fmt(n_iehe_diff_ciam),
            "definition": "KPEP IEHE ≠ KPEP CIAM (les deux non-vides). Incohérence inter-référentiels.",
        }
        result["E2_Coherence_3_Sources_Stricte"] = {
            **fmt(n_all3_eq),
            "definition": (
                "KPEP NS = KPEP CIAM = KPEP IEHE (les 3 non-vides et identiques). "
                "Matching parfait sur les 3 référentiels."
            ),
        }
    else:
        result["E2_IEHE_KPEP_Status"] = (
            "IEHE KPEP non disponible dans le fichier chargé "
            "(aucune colonne avec valeurs au format KPEP* détectée)."
        )

    return result


def analyse_qualite_emails_enrichie(kc_data, df_new_s, top_n_domains: int = 10):
    """
    E3 : Qualité des emails — validation format + détection domaines à risque.

    Analysé sur :
    - Emails CIAM (email + email_other depuis CK/CM)
    - Emails NS mailciam (colonne mailciam dans New_S)
    - Emails NS valeur_coordonnee (colonne valeur_coordonnee dans New_S)

    Règles :
    - Invalide : ne respecte pas le pattern email standard ancré
    - À risque : domaine dans RISKY_EMAIL_DOMAINS (yopmail, gmx, jetable…)
    """

    def _analyse_email_list(emails: List[str], source_label: str) -> Dict[str, Any]:
        if not emails:
            return {"source": source_label, "Total_Emails": 0}
        n_total = len(emails)
        n_invalid = 0
        n_risky = 0
        domain_counter: Dict[str, int] = {}

        for em in emails:
            e = str(em).strip().lower()
            if not e or e in ("null", "nan", "none"):
                n_invalid += 1
                continue
            valid = bool(EMAIL_VALID_RE.match(e))
            if not valid:
                n_invalid += 1
            domain = e.split("@", 1)[1] if "@" in e else ""
            if domain:
                domain_counter[domain] = domain_counter.get(domain, 0) + 1
            if domain in RISKY_EMAIL_DOMAINS:
                n_risky += 1

        top_domains = dict(sorted(domain_counter.items(), key=lambda x: -x[1])[:top_n_domains])

        def fmt(n):
            return {"Nombre": n, "Pct": round(n / n_total * 100, 2)}

        return {
            "source": source_label,
            "Total_Emails": n_total,
            "Invalides": {
                **fmt(n_invalid),
                "definition": "Email ne respectant pas le format standard (absence '@', domaine invalide, etc.).",
            },
            "A_Risque": {
                **fmt(n_risky),
                "definition": (
                    f"Email dont le domaine figure dans la liste des domaines à risque "
                    f"({len(RISKY_EMAIL_DOMAINS)} domaines : yopmail, gmx, jetable, mailinator…)."
                ),
            },
            "Top_Domaines": top_domains,
        }

    # Emails CIAM (CK + CM combinés)
    ciam_emails: List[str] = []
    df_all_ciam = kc_data.get("all") if kc_data else None
    if df_all_ciam is not None and not df_all_ciam.empty:
        for col in ["email", "email_other"]:
            if col in df_all_ciam.columns:
                vals = df_all_ciam[col].dropna().astype(str).str.strip()
                ciam_emails.extend(
                    v for v in vals.tolist() if v.lower() not in ("", "null", "nan", "none")
                )

    # Emails NS mailciam + valeur_coordonnee
    ns_emails_ciam: List[str] = []
    ns_emails_val: List[str] = []
    if df_new_s is not None and not df_new_s.empty:
        if "NS_email_ciam" in df_new_s.columns:
            vals = df_new_s["NS_email_ciam"].dropna().astype(str).str.strip()
            ns_emails_ciam.extend(v for v in vals.tolist() if v)
        if "NS_email_val" in df_new_s.columns:
            vals = df_new_s["NS_email_val"].dropna().astype(str).str.strip()
            ns_emails_val.extend(v for v in vals.tolist() if v)

    return {
        "definition": (
            "Qualité des adresses email dans les 3 sources. "
            "Invalide = format non conforme (absence @, structure incorrecte). "
            "À risque = domaine dans la liste des domaines suspects (jetables, temporaires, gmx…)."
        ),
        "CIAM_Emails_Keycloak": _analyse_email_list(ciam_emails, "CIAM (email + email_other)"),
        "NS_Emails_MailCIAM": _analyse_email_list(ns_emails_ciam, "New_S (mailciam)"),
        "NS_Emails_ValCoord": _analyse_email_list(ns_emails_val, "New_S (valeur_coordonnee)"),
    }


def compute_data_quality_score(df_ciam_target, kc_data, col_pers):
    """
    Bonus : Agrégat du score de qualité de données par assuré rapproché.

    DATA_QUALITY_KO si au moins un des critères suivants est vrai :
    1. Compte CIAM prospect (birthdate = '1900-01-01')
    2. Incohérence KPEP : KPEP NS ≠ KPEP CIAM (les deux non-vides)
    3. Email CIAM invalide (format non conforme)
    4. Email CIAM à risque (domaine suspect)
    """
    if df_ciam_target is None or df_ciam_target.empty:
        return {}

    mask_rap = df_ciam_target["match_status"] == "RAPPROCHE"
    n_base = int(mask_rap.sum())
    df_rap = df_ciam_target[mask_rap]

    if df_rap.empty:
        return {"Base_Rapproches": 0}

    # Build CIAM maps (dédoublonnés par id)
    ciam_ddn_map: Dict[str, str] = {}
    ciam_kpep_map: Dict[str, str] = {}
    ciam_email_map: Dict[str, str] = {}

    df_all_ciam = kc_data.get("all") if kc_data else None
    if df_all_ciam is not None and "id" in df_all_ciam.columns:
        tmp = df_all_ciam.drop_duplicates(subset=["id"])
        if "birthdate" in tmp.columns:
            ciam_ddn_map = tmp.set_index("id")["birthdate"].apply(format_date_iso).to_dict()
        if "idkpep" in tmp.columns:
            ciam_kpep_map = (
                tmp.set_index("id")["idkpep"]
                .fillna("").astype(str).str.strip().str.upper()
                .to_dict()
            )
        if "email" in tmp.columns:
            ciam_email_map = (
                tmp.set_index("id")["email"]
                .fillna("").astype(str).str.strip().str.lower()
                .to_dict()
            )

    PROSPECT_DATE = "1900-01-01"
    n_ok = 0
    n_ko = 0
    raisons_count: Dict[str, int] = {}

    for _, row in df_rap.iterrows():
        mid = str(row.get("matched_id", ""))
        raisons: List[str] = []

        # 1. Compte CIAM prospect
        if ciam_ddn_map.get(mid, "") == PROSPECT_DATE:
            raisons.append("Prospect_CIAM")

        # 2. KPEP NS ≠ CIAM (les deux non-vides)
        ns_kpeps = [k.upper().strip() for k in (row.get("kpeps") or []) if k]
        ciam_kpep = ciam_kpep_map.get(mid, "")
        if ns_kpeps and ciam_kpep and ciam_kpep not in ns_kpeps:
            raisons.append("KPEP_NS_diff_CIAM")

        # 3. Email CIAM invalide ou à risque
        ciam_email = ciam_email_map.get(mid, "")
        if ciam_email:
            if not EMAIL_VALID_RE.match(ciam_email):
                raisons.append("Email_CIAM_invalide")
            elif "@" in ciam_email and ciam_email.split("@", 1)[1] in RISKY_EMAIL_DOMAINS:
                raisons.append("Email_CIAM_risque")

        if not raisons:
            n_ok += 1
        else:
            n_ko += 1
            for r in raisons:
                raisons_count[r] = raisons_count.get(r, 0) + 1

    def fmt(n):
        return {"Nombre": n, "Pct": round(n / n_base * 100, 2) if n_base > 0 else 0.0}

    def fmt_ko(n):
        return {"Nombre": n, "Pct": round(n / n_ko * 100, 2) if n_ko > 0 else 0.0}

    return {
        "definition": (
            "Score de qualité de données par assuré rapproché. "
            "DATA_QUALITY_OK = aucune anomalie parmi : prospect CIAM, "
            "incohérence KPEP NS/CIAM, email invalide, email à risque. "
            "DATA_QUALITY_KO = au moins 1 critère non conforme."
        ),
        "Base_Rapproches": n_base,
        "DATA_QUALITY_OK": fmt(n_ok),
        "DATA_QUALITY_KO": fmt(n_ko),
        "Decomposition_KO": {
            k: fmt_ko(v)
            for k, v in sorted(raisons_count.items(), key=lambda x: -x[1])
        },
    }


# =============================================================================
# NOUVEAUX KPI — F : QUALITÉ DES COMPTES CIAM
# =============================================================================

def analyse_qualite_ciam(kc_data) -> Dict[str, Any]:
    """
    F1a : Comptes CIAM sans email (ni email ni email_other).
    F1b : Comptes CIAM sans KPEP valide (idkpep vide ou sans préfixe KPEP).
    F1c : Comptes avec email_other mais pas email principal.
    F1d : Doublons email CIAM (même adresse liée à plusieurs id).
    """
    df_all = kc_data.get("all") if kc_data else None
    if df_all is None or df_all.empty or "id" not in df_all.columns:
        return {}

    df_ciam = df_all.drop_duplicates(subset=["id"]).copy()
    n_total = len(df_ciam)

    em_col = "email" if "email" in df_ciam.columns else None
    emo_col = "email_other" if "email_other" in df_ciam.columns else None

    df_ciam["_em"] = (
        df_ciam[em_col].fillna("").astype(str).str.strip().str.lower()
        if em_col else pd.Series("", index=df_ciam.index)
    )
    df_ciam["_emo"] = (
        df_ciam[emo_col].fillna("").astype(str).str.strip().str.lower()
        if emo_col else pd.Series("", index=df_ciam.index)
    )

    # F1a : sans email
    mask_no_em = (df_ciam["_em"] == "") & (df_ciam["_emo"] == "")
    n_f1a = int(mask_no_em.sum())

    # F1b : sans KPEP valide
    if "idkpep" in df_ciam.columns:
        kpep_clean = df_ciam["idkpep"].fillna("").astype(str).str.strip()
        mask_no_kpep = (kpep_clean == "") | (~kpep_clean.str.upper().str.startswith("KPEP"))
        n_f1b = int(mask_no_kpep.sum())
    else:
        n_f1b = n_total

    # F1c : email_other seulement (pas email principal)
    n_f1c = int(((df_ciam["_em"] == "") & (df_ciam["_emo"] != "")).sum())

    # F1d : doublons email (même email → plusieurs id)
    rows_em = df_ciam[df_ciam["_em"] != ""][["id", "_em"]].rename(columns={"_em": "_email"})
    rows_emo = df_ciam[df_ciam["_emo"] != ""][["id", "_emo"]].rename(columns={"_emo": "_email"})
    em_all = pd.concat([rows_em, rows_emo], ignore_index=True)

    if not em_all.empty:
        email_id_counts = em_all.groupby("_email")["id"].nunique()
        dup_emails = email_id_counts[email_id_counts > 1]
        n_f1d_emails = int(len(dup_emails))
        n_f1d_comptes = int(em_all[em_all["_email"].isin(dup_emails.index)]["id"].nunique())
    else:
        n_f1d_emails = 0
        n_f1d_comptes = 0

    def fmt(n):
        return {"Nombre": n, "Pct": round(n / n_total * 100, 2) if n_total > 0 else 0.0}

    return {
        "_Base_Comptes_CIAM_Uniques": n_total,
        "F1a_Sans_Email": {
            "definition": "Comptes CIAM sans email (ni email ni email_other renseignés).",
            **fmt(n_f1a),
        },
        "F1b_Sans_KPEP": {
            "definition": "Comptes CIAM sans KPEP valide (idkpep vide ou ne commençant pas par 'KPEP').",
            **fmt(n_f1b),
        },
        "F1c_Email_Other_Seulement": {
            "definition": "Comptes avec email_other renseigné mais email principal vide (migration incomplète potentielle).",
            **fmt(n_f1c),
        },
        "F1d_Doublons_Email": {
            "definition": "Emails rattachés à plusieurs id CIAM distincts (email + email_other confondus).",
            "Nb_Emails_Dupliques": n_f1d_emails,
            "Nb_Comptes_Concernes": n_f1d_comptes,
            "Pct_Comptes": round(n_f1d_comptes / n_total * 100, 2) if n_total > 0 else 0.0,
        },
    }


# =============================================================================
# NOUVEAUX KPI — G : INCOHÉRENCES NS ↔ CIAM
# =============================================================================

def analyse_incoherences_ns_ciam(df_ciam_target, df_new_s, kc_data, col_pers) -> Dict[str, Any]:
    """
    G1a : DDN différente NS vs CIAM (rapprochés, hors 1900-01-01).
    G1b : Nom/prénom divergent (similarité SequenceMatcher < 0.7).
    G1c : Email coordonnée NS ≠ email CIAM (rapprochés avec email valide côté NS).
    """
    if df_ciam_target is None or df_ciam_target.empty:
        return {}

    PROSPECT_DATE = "1900-01-01"

    df_all = kc_data.get("all") if kc_data else None
    if df_all is None or df_all.empty or "id" not in df_all.columns:
        return {"status": "données CIAM non disponibles"}

    ciam_ref = df_all.drop_duplicates(subset=["id"]).set_index("id")

    mask_rap = df_ciam_target["match_status"] == "RAPPROCHE"
    df_rap = df_ciam_target[mask_rap]
    n_rapproches = len(df_rap)
    if n_rapproches == 0:
        return {}

    # Lookup NS preprocessé par personne
    def _ns_map(col_ns):
        if col_ns in df_new_s.columns and col_pers in df_new_s.columns:
            return (
                df_new_s.drop_duplicates(subset=[col_pers])
                .set_index(col_pers)[col_ns]
                .fillna("").astype(str).to_dict()
            )
        return {}

    ns_ddn_map = _ns_map("NS_ddn")
    ns_nom_map = _ns_map("NS_nom")
    ns_pre_map = _ns_map("NS_prenom")
    ns_email_val_map = _ns_map("NS_email_val")

    n_g1a_base = n_g1a_diff = 0
    n_g1b_base = n_g1b_diff = 0
    n_g1c_base = n_g1c_diff = 0

    for _, row in df_rap.iterrows():
        pid = row[col_pers]
        mid = row.get("matched_id")
        if mid is None or str(mid) not in ciam_ref.index:
            continue
        ciam_row = ciam_ref.loc[str(mid)]

        # G1a — DDN
        ns_ddn = ns_ddn_map.get(pid, "")
        ciam_ddn = format_date_iso(str(ciam_row.get("birthdate", "") or ""))
        if ns_ddn and ciam_ddn and ns_ddn != PROSPECT_DATE and ciam_ddn != PROSPECT_DATE:
            n_g1a_base += 1
            if ns_ddn != ciam_ddn:
                n_g1a_diff += 1

        # G1b — Nom/prénom
        ns_nom = ns_nom_map.get(pid, "")
        ns_pre = ns_pre_map.get(pid, "")
        ciam_nom = normalize_text(str(ciam_row.get("last_name", "") or ""))
        ciam_pre = normalize_text(str(ciam_row.get("first_name", "") or ""))
        if ns_nom and ciam_nom:
            n_g1b_base += 1
            sim_nom = SequenceMatcher(None, ns_nom, ciam_nom).ratio()
            sim_pre = SequenceMatcher(None, ns_pre, ciam_pre).ratio() if ns_pre and ciam_pre else 1.0
            if (sim_nom + sim_pre) / 2.0 < 0.7:
                n_g1b_diff += 1

        # G1c — Email coordonnée NS vs CIAM
        ns_em_val = normalize_email(ns_email_val_map.get(pid, ""))
        ciam_em = normalize_email(str(ciam_row.get("email", "") or ""))
        if ns_em_val:
            n_g1c_base += 1
            if ciam_em != ns_em_val:
                n_g1c_diff += 1

    def fmtg(n, base):
        return {"Nombre": n, "Pct": round(n / base * 100, 2) if base > 0 else 0.0}

    return {
        "G1a_DDN_Differente": {
            "definition": (
                "Parmi les rapprochés avec DDN renseignée des deux côtés (hors 1900-01-01) : "
                "divergence entre date de naissance NS et compte CIAM matché. "
                "Indicateur de données périmées non propagées."
            ),
            "Base": n_g1a_base,
            "DDN_Differente": fmtg(n_g1a_diff, n_g1a_base),
            "DDN_Identique": fmtg(n_g1a_base - n_g1a_diff, n_g1a_base),
        },
        "G1b_Nom_Prenom_Divergent": {
            "definition": (
                "Parmi les rapprochés avec nom renseigné des deux côtés : "
                "similarité combinée nom+prénom (SequenceMatcher) < 0.70. "
                "Peut indiquer un mauvais rapprochement ou des données sources divergentes."
            ),
            "Base": n_g1b_base,
            "Seuil_Similarite": 0.7,
            "Nom_Divergent": fmtg(n_g1b_diff, n_g1b_base),
            "Nom_Coherent": fmtg(n_g1b_base - n_g1b_diff, n_g1b_base),
        },
        "G1c_Email_Coord_NS_Different_CIAM": {
            "definition": (
                "Parmi les rapprochés ayant un email valeur_coordonnee dans NS : "
                "cet email diffère de l'email principal du compte CIAM matché. "
                "Signale des données de contact potentiellement périmées dans l'un des référentiels."
            ),
            "Base": n_g1c_base,
            "Email_Different": fmtg(n_g1c_diff, n_g1c_base),
            "Email_Identique": fmtg(n_g1c_base - n_g1c_diff, n_g1c_base),
        },
    }


# =============================================================================
# MAIN
# =============================================================================

def main():
    print("--- Démarrage Calcul KPI Enrichis (Support Batching CSV) ---")

    new_s_files = list(INPUT_DIR.glob("*New_S.csv"))
    if not new_s_files:
        print("ERREUR: Aucun fichier New_S trouvé.")
        return

    # Sélection fichier New_S le plus récent (DDMMYYYY)
    target_file_new_s = None
    prefix = None
    max_date = None

    print(f"   📂 Fichiers candidats trouvés : {[f.name for f in new_s_files]}")

    for f in new_s_files:
        current_prefix = f.name.split("_")[0]
        if len(current_prefix) == 8 and current_prefix.isdigit():
            try:
                current_date = datetime.strptime(current_prefix, "%d%m%Y")
                if max_date is None or current_date > max_date:
                    max_date = current_date
                    target_file_new_s = f
                    prefix = current_prefix
            except ValueError:
                pass

    if target_file_new_s is None:
        target_file_new_s = new_s_files[0]
        prefix = target_file_new_s.name.split("_")[0] if "_" in target_file_new_s.name else "UNKNOWN"
        print(f"⚠️ Aucun préfixe de date valide détecté. Utilisation du premier fichier : {target_file_new_s.name}")
    else:
        print(f"✅ Sélection du fichier le plus récent : {target_file_new_s.name} (Date : {max_date.strftime('%d/%m/%Y')})")

    print(f"Préfixe retenu : {prefix}")

    dfs = {}
    input_stats = {}

    # -------------------------------------------------------------------------
    # 1. LECTURES DES FICHIERS INPUT
    # Méthode : chargement CSV standard + concaténation multi-fichiers par pattern
    # -------------------------------------------------------------------------
    dfs["New_S"] = load_csv(target_file_new_s, sep=None)
    dfs["IEHE"] = load_csv(INPUT_DIR / f"{prefix}_IEHE.csv", sep=None)

    input_stats["New_S"] = get_file_info(dfs["New_S"], target_file_new_s.name)
    input_stats["IEHE"] = get_file_info(dfs["IEHE"], f"{prefix}_IEHE.csv")

    print("   🔍 Chargement des fichiers sources (avec consolidation automatique)...")

    dfs["CK"] = load_concat_by_pattern(INPUT_DIR, f"{prefix}*CK*.csv")
    input_stats["CK"] = get_file_info(dfs["CK"], f"{prefix}_CK*.csv")

    dfs["CM"] = load_concat_by_pattern(INPUT_DIR, f"{prefix}*CM*.csv")
    input_stats["CM"] = get_file_info(dfs["CM"], f"{prefix}_CM*.csv")

    dfs["Last"] = load_concat_by_pattern(INPUT_DIR, f"{prefix}*Last*.csv")
    input_stats["Last"] = get_file_info(dfs["Last"], f"{prefix}_Last*.csv")

    dfs["Middle"] = load_concat_by_pattern(INPUT_DIR, f"{prefix}*Middle*.csv")
    input_stats["Middle"] = get_file_info(dfs["Middle"], f"{prefix}_Middle*.csv")

    dfs["Last_Prenom"] = load_concat_by_pattern(INPUT_DIR, f"{prefix}*Last_Prenom*.csv")
    dfs["Middle_Prenom"] = load_concat_by_pattern(INPUT_DIR, f"{prefix}*Middle_Prenom*.csv")

    # IEHE_KO (générés par script 03, dans Output/) — tous les fichiers disponibles
    iehe_ko_files = sorted(OUTPUT_DIR.glob("*_IEHE_KO.csv"))
    iehe_ko_dfs = []
    for f in iehe_ko_files:
        df_tmp = load_csv(f, sep=None)
        if df_tmp is not None and not df_tmp.empty:
            df_tmp["_source_file"] = f.name
            iehe_ko_dfs.append(df_tmp)
    dfs["IEHE_KO_list"] = iehe_ko_dfs

    df_new_s = dfs["New_S"]
    if df_new_s is None:
        return

    # Pré-traitement
    df_new_s = preprocess_new_s(df_new_s)

    col_type = get_col_flexible(df_new_s, ["type_assure", "typeassure"])
    col_pers = get_col_flexible(df_new_s, ["num_personne", "numpersonne"])
    col_ctr = get_col_flexible(df_new_s, ["num_ctr_indiv", "contrat"])

    if not col_type or not col_pers:
        print("❌ Colonnes requises manquantes dans New_S (type_assure / num_personne).")
        return

    types_assures_ai = list(TYPES_ASSURES_IEHE)

    mask_assure = df_new_s[col_type].isin(types_assures_ai)
    mask_conjoint = df_new_s[col_type].str.contains("CONJ", na=False)

    # -------------------------------------------------------------------------
    # 2. VOLUMÉTRIE BRUTE
    # Méthode : comptage direct des lignes par type
    # -------------------------------------------------------------------------
    volumetrie = {
        "Total_Lignes": int(len(df_new_s)),
        "Nbr_Assures_Brut": int(mask_assure.sum()),
        "Nbr_Conjoints_Brut": int(mask_conjoint.sum()),
    }

    # -------------------------------------------------------------------------
    # 3. VOLUMÉTRIE POPULATION UNIQUE
    # Méthode : dédoublonnage sur num_personne (une personne peut avoir plusieurs lignes/contrats)
    # -------------------------------------------------------------------------
    all_unique_ids = set(df_new_s[col_pers].dropna().unique())
    ids_assures = set(df_new_s[mask_assure][col_pers].dropna().unique())
    ids_conjoints = set(df_new_s[mask_conjoint][col_pers].dropna().unique())

    nbr_contrats_uniques = len(df_new_s[col_ctr].dropna().unique()) if col_ctr else 0

    identifiants = {
        "Nbr_Num_Personne_Unique_Global": len(all_unique_ids),
        "Population_Theorique_CIAM_Assures": len(ids_assures),
        "Nbr_Contrats_Uniques": nbr_contrats_uniques,
        "Nbr_Conjoints_Uniques": len(ids_conjoints),
    }

    ids_croises = ids_assures.intersection(ids_conjoints)
    counts_assure = df_new_s[mask_assure][col_pers].value_counts()
    counts_conjoint = df_new_s[mask_conjoint][col_pers].value_counts()

    # -------------------------------------------------------------------------
    # 4. QUALITÉ DES DONNÉES
    # Méthode : comptages sur les colonnes brutes et normalisées de New_S
    # -------------------------------------------------------------------------
    kpep_detail_stats = analyse_doublons_kpep_detail(df_new_s, types_assures_ai)
    nb_meme_offre, nb_diff_offre, meme_offre_par_type, diff_offre_par_type = analyse_doublons_contrats(df_new_s, types_assures_ai)
    email_quality_stats = analyse_qualite_emails(df_new_s)

    doublons_stats = {
        "Assure_ET_Conjoint_Meme_ID": len(ids_croises),
        "Assures_En_Double_Lignes": int((counts_assure > 1).sum()),
        "Conjoints_En_Double_Lignes": int((counts_conjoint > 1).sum()),
        "Personnes_Plusieurs_KPEP": kpep_detail_stats.get("global_multi_kpep", 0),
        "Detail_KPEP": kpep_detail_stats.get("detail", {}),
        "Doublons_Contrats_Meme_Offre": nb_meme_offre,
        "Doublons_Contrats_Meme_Offre_Par_Type": meme_offre_par_type,
        "Doublons_Contrats_Diff_Offre": nb_diff_offre,
        "Doublons_Contrats_Diff_Offre_Par_Type": diff_offre_par_type,
    }

    # Qualité contacts CK/CM/Last/Middle (pour Annexe)
    colonnes_contacts = ["idkpep", "email", "email_other", "first_name", "last_name", "middlename"]
    qualite_contacts = {
        "CK": analyse_qualite_contacts(dfs["CK"], colonnes_contacts),
        "CM": analyse_qualite_contacts(dfs["CM"], colonnes_contacts),
        "Last": analyse_qualite_contacts(dfs["Last"], colonnes_contacts),
        "Middle": analyse_qualite_contacts(dfs["Middle"], colonnes_contacts),
    }

    # -------------------------------------------------------------------------
    # 5. MATCHING CIAM (OPTION B SET-BASED)
    # Méthode : matching au niveau "personne" (num_personne)
    #   -> Agrégation de toutes les valeurs candidates (emails, kpeps, identités)
    #      sur l'ensemble des lignes d'un même num_personne
    #   -> Ordre strict : Mail_CIAM > Valeur_Coord > KPEP > Identite_Full
    #                     > Identite_Inversee > Recherche_Large_Nom > Recherche_Large_Middle
    # -------------------------------------------------------------------------
    if dfs["CK"] is None or dfs["CK"].empty:
        print("❌ CK vide/introuvable : matching CIAM impossible.")
        return

    kc_data = prepare_keycloak_data(
        dfs["CK"],
        dfs["CM"],
        dfs["Last"],
        dfs["Last_Prenom"],
        dfs["Middle"],
        dfs["Middle_Prenom"],
    )
    engine = MatchingEngine(kc_data)

    df_assures = df_new_s[mask_assure].copy()
    df_ciam_target = build_person_view(df_assures, col_pers)

    results = df_ciam_target.apply(engine.find_match_person, axis=1)
    df_ciam_target["match_status"] = [x[0] for x in results]
    df_ciam_target["match_method"] = [x[1] for x in results]
    df_ciam_target["matched_id"] = [x[2] for x in results]
    df_ciam_target["matched_key"] = [x[3] for x in results]

    debug_match_email = os.getenv("DEBUG_MATCH_EMAIL", "0") == "1"
    if debug_match_email:
        def _hits_email_idx(vals):
            if not isinstance(vals, list):
                return []
            return [v for v in vals if v in engine.idx_email]

        dbg = df_ciam_target[df_ciam_target["match_status"] == "NON_RAPPROCHE"].copy()
        dbg["emails_val_hits"] = dbg["emails_val"].apply(_hits_email_idx)
        dbg["emails_ciam_hits"] = dbg["emails_ciam"].apply(_hits_email_idx)
        suspicious = dbg[(dbg["emails_val_hits"].apply(len) > 0) | (dbg["emails_ciam_hits"].apply(len) > 0)]
        print(f"[DEBUG_MATCH_EMAIL] NON_RAPPROCHE avec emails présents en index: {len(suspicious)}")
        if not suspicious.empty:
            print(
                suspicious[
                    [col_pers, "emails_ciam", "emails_val", "emails_ciam_hits", "emails_val_hits"]
                ]
                .head(20)
                .to_string(index=False)
            )

    nb_rapproches = int((df_ciam_target["match_status"] == "RAPPROCHE").sum())
    cible = len(ids_assures)
    taux = (nb_rapproches / cible) * 100 if cible > 0 else 0.0

    method_counts = (
        df_ciam_target[df_ciam_target["match_status"] == "RAPPROCHE"]["match_method"]
        .value_counts()
        .to_dict()
    )

    sources_breakdown = {"Standard_CK_CM": 0, "Fichier_Last": 0, "Fichier_Middle": 0}
    for method, count in method_counts.items():
        if "Recherche_Large_Nom" in method:
            sources_breakdown["Fichier_Last"] += int(count)
        elif "Recherche_Large_Middle" in method:
            sources_breakdown["Fichier_Middle"] += int(count)
        else:
            sources_breakdown["Standard_CK_CM"] += int(count)

    # -------------------------------------------------------------------------
    # KPI Adresses CIAM vides après rapprochement
    # Méthode : parmi les comptes rapprochés ayant une valeur_coordonnée email,
    #           combien ont un email CIAM vide dans le compte Keycloak matché ?
    # Base = population rapprochée
    # -------------------------------------------------------------------------
    matched_email_map = pd.Series(dtype=str)
    if kc_data.get("all") is not None and not kc_data["all"].empty and "id" in kc_data["all"].columns:
        ref_ck = kc_data["all"].drop_duplicates(subset=["id"]).set_index("id")
        if "email" in ref_ck.columns:
            matched_email_map = ref_ck["email"].fillna("").astype(str)

    if isinstance(matched_email_map, pd.Series) and not matched_email_map.empty:
        df_ciam_target["matched_email_ciam"] = df_ciam_target["matched_id"].map(matched_email_map).fillna("")
    else:
        df_ciam_target["matched_email_ciam"] = ""

    mask_rapproche = df_ciam_target["match_status"] == "RAPPROCHE"
    mask_email_val = df_ciam_target["emails_val"].apply(lambda v: isinstance(v, list) and len(v) > 0)
    email_ciam = df_ciam_target["matched_email_ciam"].fillna("").astype(str).str.strip()
    email_ciam = email_ciam.mask(email_ciam.str.lower().isin(["nan", "none", "null"]), "")
    mask_ciam_empty = email_ciam == ""

    nb_adresses_ciam_vides = int((mask_rapproche & mask_email_val & mask_ciam_empty).sum())
    base_rapproches = int(mask_rapproche.sum())
    pct_adresses_ciam_vides = round((nb_adresses_ciam_vides / base_rapproches) * 100, 2) if base_rapproches > 0 else 0.0

    # -------------------------------------------------------------------------
    # KPI : Email CIAM rapproché = Email valeur coordonnées (New_S)
    # Méthode : parmi les comptes rapprochés, combien ont le même email
    #           dans le compte Keycloak matché ET dans valeur_coordonnee (New_S) ?
    # [PATCH 2] Comparaison sur TOUTE la liste emails_val (pas seulement v[0])
    # Base = population rapprochée
    # -------------------------------------------------------------------------
    _ec_list = email_ciam.tolist()
    _ev_list = df_ciam_target["emails_val"].tolist()
    mask_email_ciam_eq_val = mask_rapproche & pd.Series(
        [ec != "" and isinstance(ev, list) and ec in ev
         for ec, ev in zip(_ec_list, _ev_list)],
        index=df_ciam_target.index,
    )
    nb_email_ciam_eq_val = int(mask_email_ciam_eq_val.sum())
    pct_email_ciam_eq_val = round(nb_email_ciam_eq_val / base_rapproches * 100, 2) if base_rapproches > 0 else 0.0

    # -------------------------------------------------------------------------
    # 6. IEHE
    # Méthode : jointure entre num_personne (New_S) et refperboccn (IEHE)
    # Population de référence = toutes personnes uniques du flux (assurés + conjoints)
    # -------------------------------------------------------------------------
    ids_iehe = set()
    if dfs["IEHE"] is not None and "refperboccn" in dfs["IEHE"].columns:
        ids_iehe = set(dfs["IEHE"]["refperboccn"].dropna().astype(str).str.strip().unique())

    pop_global = identifiants["Nbr_Num_Personne_Unique_Global"]
    nb_presents_iehe = len(all_unique_ids.intersection(ids_iehe))
    nb_manquants_iehe = pop_global - nb_presents_iehe
    taux_presents_iehe = round(nb_presents_iehe / pop_global * 100, 2) if pop_global > 0 else 0.0
    taux_manquants_iehe = round(nb_manquants_iehe / pop_global * 100, 2) if pop_global > 0 else 0.0

    # Détails IEHE par type (pour Annexe)
    manquants_iehe_par_type = {}
    population_ref_par_type = {}
    presents_iehe_par_type = {}
    if col_type and col_pers:
        for type_val, grp in df_new_s.groupby(col_type):
            ids_type = set(grp[col_pers].dropna().unique())
            population_ref_par_type[type_val] = int(len(ids_type))
            presents_iehe_par_type[type_val] = int(len(ids_type.intersection(ids_iehe)))
            manquants_iehe_par_type[type_val] = int(len(ids_type - ids_iehe))

    # -------------------------------------------------------------------------
    # 6.bis ÉCART LIGNES vs PERSONNES UNIQUES (raison du delta)
    # Les KPI IEHE dédoublonnent par num_personne. Les comptages directs sur
    # NS_IEHE.csv (ligne = 1 contrat) peuvent différer si une personne apparaît
    # sur plusieurs contrats.
    # -------------------------------------------------------------------------
    ecart_lignes_personnes = {}
    if col_pers and col_type:
        ser_pers = df_new_s[col_pers].dropna().astype(str).str.strip()
        ser_type = df_new_s.loc[ser_pers.index, col_type].astype(str).str.strip()
        mask_in_iehe_ln = ser_pers.isin(ids_iehe)

        nb_lignes_total = int(len(ser_pers))
        nb_lignes_presents = int(mask_in_iehe_ln.sum())
        nb_lignes_manquants = int((~mask_in_iehe_ln).sum())

        # Comptages par type_assure (lignes + personnes uniques)
        lignes_par_type = ser_type.value_counts().to_dict()
        lignes_manquants_par_type = ser_type[~mask_in_iehe_ln].value_counts().to_dict()
        lignes_presents_par_type = ser_type[mask_in_iehe_ln].value_counts().to_dict()

        # Doublons : personnes apparaissant sur >1 contrat (= >1 ligne)
        counts_per_pers = ser_pers.value_counts()
        doublons_ids = set(counts_per_pers[counts_per_pers > 1].index)
        # Type d'assuré par personne (1 personne = 1 type normalement)
        df_pers = df_new_s.dropna(subset=[col_pers]).copy()
        df_pers["_pid_norm"] = df_pers[col_pers].astype(str).str.strip()
        df_pers_uniq = df_pers.drop_duplicates(subset=["_pid_norm"])
        type_by_pers = (
            df_pers_uniq.set_index("_pid_norm")[col_type].astype(str).str.strip().to_dict()
        )
        doublons_par_type: Dict[str, int] = {}
        for pid in doublons_ids:
            t = str(type_by_pers.get(pid, "INCONNU")).strip()
            doublons_par_type[t] = doublons_par_type.get(t, 0) + 1

        # Top 5 exemples de doublons (pour traçabilité)
        top_doublons = [
            {
                "num_personne": str(pid),
                "type_assure": str(type_by_pers.get(pid, "INCONNU")).strip(),
                "nb_contrats": int(counts_per_pers[pid]),
            }
            for pid in counts_per_pers[counts_per_pers > 1].head(5).index
        ]

        def _split(lignes_tot: int, pers_uniq: int, lignes_par_t: Dict[str, int],
                   pers_par_t: Dict[str, int]) -> Dict[str, Any]:
            types = sorted(set(lignes_par_t.keys()) | set(pers_par_t.keys()))
            return {
                "Lignes": int(lignes_tot),
                "Personnes_Uniques": int(pers_uniq),
                "Delta_Doublons": int(lignes_tot - pers_uniq),
                "Par_Type_Assure": {
                    t: {
                        "Lignes": int(lignes_par_t.get(t, 0)),
                        "Personnes_Uniques": int(pers_par_t.get(t, 0)),
                        "Delta_Doublons": int(lignes_par_t.get(t, 0) - pers_par_t.get(t, 0)),
                    }
                    for t in types
                },
            }

        ecart_lignes_personnes = {
            "definition": (
                "Raison du delta entre comptage par lignes-contrat (NS_IEHE.csv) "
                "et par personnes uniques (KPI). Une même personne peut apparaître "
                "sur plusieurs contrats : le KPI dédoublonne par num_personne, "
                "le comptage par lignes ne dédoublonne pas. "
                "Le delta = nombre de contrats supplémentaires liés aux personnes multi-contrats."
            ),
            "Total_Flux": _split(
                nb_lignes_total, pop_global,
                lignes_par_type, population_ref_par_type,
            ),
            "Manquants_IEHE": _split(
                nb_lignes_manquants, nb_manquants_iehe,
                lignes_manquants_par_type, manquants_iehe_par_type,
            ),
            "Presents_IEHE": _split(
                nb_lignes_presents, nb_presents_iehe,
                lignes_presents_par_type, presents_iehe_par_type,
            ),
            "Doublons_Personnes_Multi_Contrats": {
                "definition": (
                    "Personnes (num_personne) apparaissant sur plusieurs contrats "
                    "dans le flux. Chaque contrat supplémentaire augmente le "
                    "comptage-lignes sans augmenter le comptage-personnes."
                ),
                "Nombre_Personnes": int(len(doublons_ids)),
                "Par_Type_Assure": doublons_par_type,
                "Exemples_Top_5": top_doublons,
            },
        }

    # Métriques IEHE_KO (retry J+1/J+2/J+7) — tous les fichiers disponibles
    # Ventilations supplémentaires :
    #   - Par type d'assuré (NS_type_assure)
    #   - Par éligibilité Carte TP (Eligible_TP / Non_Eligible_TP)
    # Source A  : colonnes ajoutées dans le fichier IEHE_KO par 03_generation_fichiers_detail.py
    # Source A2 : fallback par jointure num_personne ↔ {prefix}_NS_IEHE.csv du MÊME run
    #             (porte type/offre/société/dates pour toutes les personnes, même
    #             absentes d'IEHE) — corrige le cas des fichiers KO legacy dont les
    #             personnes ont disparu des New_S (symptôme : tout INCONNU / 0 Carte TP).
    # Source B  : fallback par jointure num_personne ↔ New_S courant
    # Source C  : fallback multi-NS historiques (fichiers KO antérieurs au
    #             schéma enrichi, ou personnes résolues entre-temps et donc
    #             absentes du New_S courant — cf. bug INCONNU rapporté).
    # Logique pure centralisée dans iehe_ko_lib pour permettre les tests.
    ns_lookup_by_pers = iehe_ko_lib.build_ns_lookup_from_df(df_new_s, col_pers, col_type)

    # Lookup historique : on parcourt les *_New_S.csv plus anciens et on
    # complète sans écraser (Source C). N'importe quel échec de lecture
    # est loggué mais ne bloque pas la classification.
    try:
        ns_historical_lookup = iehe_ko_lib.build_historical_ns_lookup(
            INPUT_DIR, exclude_prefix=prefix, preprocess_fn=preprocess_new_s)
        if ns_historical_lookup:
            print(f"   📚 Lookup NS historique : {len(ns_historical_lookup)} "
                  f"personne(s) complétée(s) depuis les New_S antérieurs.")
    except Exception as exc:  # noqa: BLE001
        print(f"   [WARN] Lookup NS historique indisponible : {exc}")
        ns_historical_lookup = {}

    def _empty_bucket() -> Dict[str, int]:
        return {"Total_KO_Initial": 0, "Resolus_Apres_Retry": 0, "Encore_KO": 0}

    def _finalize_buckets(buckets: Dict[str, Dict[str, int]]) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        for k, v in sorted(buckets.items()):
            tot = v["Total_KO_Initial"]
            v["Taux_Resolution"] = round(v["Resolus_Apres_Retry"] / tot * 100, 2) if tot > 0 else 0.0
            out[k] = v
        return out

    # Cache des lookups NS_IEHE co-datés (Source A2), construits à la demande
    # par préfixe de fichier KO. Le NS_IEHE du même run porte toujours
    # NS_type_assure + offre + société + dates pour ces personnes, ce qui
    # fiabilise la ventilation des fichiers KO legacy (sans enrichissement).
    _ns_iehe_lookup_cache: Dict[str, Dict[str, Dict[str, Any]]] = {}

    def _get_ns_iehe_lookup(ko_source_file: str) -> Dict[str, Dict[str, Any]]:
        prefix_ko = str(ko_source_file).split("_IEHE_KO")[0] if ko_source_file else ""
        if not prefix_ko:
            return {}
        if prefix_ko not in _ns_iehe_lookup_cache:
            ns_iehe_path = OUTPUT_DIR / f"{prefix_ko}_NS_IEHE.csv"
            df_ns_iehe = load_csv(ns_iehe_path, sep=None)
            lookup = iehe_ko_lib.build_ns_iehe_lookup(df_ns_iehe) if df_ns_iehe is not None else {}
            if lookup:
                print(f"   🔗 IEHE_KO {prefix_ko} : {len(lookup)} personne(s) "
                      f"récupérée(s) depuis {ns_iehe_path.name} (Source A2).")
            _ns_iehe_lookup_cache[prefix_ko] = lookup
        return _ns_iehe_lookup_cache[prefix_ko]

    def _classify_ko_row(row: pd.Series, ns_iehe_lookup: Dict[str, Dict[str, Any]]) -> Tuple[str, str]:
        """Retourne `(type_assure, eligibilite_tp_label)`.

        Délégué à `iehe_ko_lib.classify_ko_row` : Source A (colonnes du
        fichier IEHE_KO enrichi), A2 (NS_IEHE co-daté), B (New_S courant),
        C (NS historiques). Le lookup case-insensitive corrige le bug "tout
        INCONNU" causé par le lowercase des entêtes lors de la lecture via
        load_csv ; la Source A2 corrige le cas des fichiers KO legacy dont
        les personnes ont disparu des New_S (rotation).
        """
        return iehe_ko_lib.classify_ko_row(
            row, ns_current_lookup=ns_lookup_by_pers,
            ns_historical_lookup=ns_historical_lookup,
            ns_iehe_lookup=ns_iehe_lookup)

    iehe_ko_details = []
    iehe_ko_totaux = _empty_bucket()
    iehe_ko_par_type_total: Dict[str, Dict[str, int]] = {}
    iehe_ko_par_elig_total: Dict[str, Dict[str, int]] = {
        "Eligible_TP": _empty_bucket(),
        "Future_TP": _empty_bucket(),
        "Hors_Perimetre_TP": _empty_bucket(),
    }

    for df_ko in dfs.get("IEHE_KO_list", []):
        if "statut_retry" not in df_ko.columns:
            continue
        ko_total = len(df_ko)
        ko_resolved = int((df_ko["statut_retry"] == "OK").sum())
        ko_still = ko_total - ko_resolved
        derniere_verif = ""
        if "date_derniere_verif" in df_ko.columns:
            derniere_verif = df_ko["date_derniere_verif"].dropna().astype(str).str.strip().max()
        source = df_ko["_source_file"].iloc[0] if "_source_file" in df_ko.columns else ""

        # Source A2 : NS_IEHE du même run que ce fichier KO (récupère type/dates
        # pour les fichiers legacy non enrichis ou aux personnes absentes des New_S).
        ns_iehe_lookup = _get_ns_iehe_lookup(source)

        # Ventilations par fichier
        par_type_file: Dict[str, Dict[str, int]] = {}
        par_elig_file: Dict[str, Dict[str, int]] = {
            "Eligible_TP": _empty_bucket(),
            "Future_TP": _empty_bucket(),
            "Hors_Perimetre_TP": _empty_bucket(),
        }
        for _, r in df_ko.iterrows():
            ta_label, elig_label = _classify_ko_row(r, ns_iehe_lookup)
            resolved = str(r.get("statut_retry", "")).strip().upper() == "OK"

            for buckets, key in ((par_type_file, ta_label),
                                  (par_elig_file, elig_label),
                                  (iehe_ko_par_type_total, ta_label),
                                  (iehe_ko_par_elig_total, elig_label)):
                bucket = buckets.setdefault(key, _empty_bucket())
                bucket["Total_KO_Initial"] += 1
                if resolved:
                    bucket["Resolus_Apres_Retry"] += 1
                else:
                    bucket["Encore_KO"] += 1

        detail = {
            "Fichier": source,
            "Total_KO_Initial": ko_total,
            "Resolus_Apres_Retry": ko_resolved,
            "Taux_Resolution": round(ko_resolved / ko_total * 100, 2) if ko_total > 0 else 0.0,
            "Encore_KO": ko_still,
            "Date_Derniere_Verification": derniere_verif,
            "Par_Type_Assure": _finalize_buckets(par_type_file),
            "Par_Eligibilite_TP": _finalize_buckets(par_elig_file),
        }
        iehe_ko_details.append(detail)
        iehe_ko_totaux["Total_KO_Initial"] += ko_total
        iehe_ko_totaux["Resolus_Apres_Retry"] += ko_resolved
        iehe_ko_totaux["Encore_KO"] += ko_still

    iehe_ko_stats = None
    if iehe_ko_details:
        t = iehe_ko_totaux
        t["Taux_Resolution"] = round(t["Resolus_Apres_Retry"] / t["Total_KO_Initial"] * 100, 2) if t["Total_KO_Initial"] > 0 else 0.0
        t["Fichiers_Traites"] = len(iehe_ko_details)
        iehe_ko_stats = {
            "Totaux": t,
            "Par_Type_Assure": _finalize_buckets(iehe_ko_par_type_total),
            "Par_Eligibilite_TP": _finalize_buckets(iehe_ko_par_elig_total),
            "Detail_Par_Fichier": iehe_ko_details,
        }
        print(f"   📊 IEHE_KO : {len(iehe_ko_details)} fichier(s) | "
              f"{t['Resolus_Apres_Retry']}/{t['Total_KO_Initial']} résolus ({t['Encore_KO']} encore KO)")

    # -------------------------------------------------------------------------
    # 7. CARTE TP
    # -------------------------------------------------------------------------
    tp_stats = calcul_kpi_cartes_tp(df_new_s)
    # --- KPI Carte TP GED (flux quotidien produit par 07_controle_tp_ged.py) ---
    # Lecture du fichier détail (tolère son absence : section 7bis = status "Absent").
    tp_ged_stats = calcul_kpi_tp_ged(prefix)
    # --- KPI5 Rejets Plus Anciens (flux hebdo produit par launch_SQL_query_V2.py) ---
    # Lecture du xlsx output/SQL/<prefix>/ ; tolère son absence (Statut="Absent").
    kpi5_rejets_anciennete = calcul_kpi5_rejets_anciennete(prefix)
    ids_tp_eligible = tp_stats.pop("_debug_eligible_ids", set())
    ids_tp_non_eligible = tp_stats.pop("_debug_non_eligible_ids", set())
    person_tp_map = tp_stats.pop("_debug_person_tp_map", {})
    tp_detail_par_type = tp_stats.pop("_annexe_detail_par_type", {})
    tp_annexe_non_eligible = tp_stats.pop("_annexe_non_eligible", 0)
    line_tp_records = tp_stats.pop("_debug_line_tp_records", [])

    # =========================================================================
    # NOUVEAUX KPI — appels
    # =========================================================================
    col_soc_ns = get_col_flexible(df_new_s, ["code_soc_appart", "code_societe", "code_soc"])

    kpi_qualite_ns = analyse_completude_new_s(df_new_s, types_assures_ai, col_type, col_pers)

    kpi_matching_seg = analyse_rapprochement_par_segment(
        df_ciam_target, df_assures, col_pers, col_type, col_soc_ns, kc_data
    )

    _matched_email_dict = matched_email_map.to_dict() if isinstance(matched_email_map, pd.Series) else {}

    kpi_tp_enrichi = analyse_tp_enrichi(
        person_tp_map, ids_assures, df_ciam_target, col_pers,
        datetime.strptime(prefix, "%d%m%Y").strftime("%d/%m/%Y") if len(prefix) == 8 else "",
        matched_email_map=_matched_email_dict,
    )

    kpi_iehe_qualite = analyse_iehe_qualite(
        dfs["IEHE"], df_new_s, df_ciam_target, col_pers, matched_email_map
    )

    # -------------------------------------------------------------------------
    # NOUVEAUX KPI — F : Qualité comptes CIAM
    # -------------------------------------------------------------------------
    kpi_qualite_ciam = analyse_qualite_ciam(kc_data)

    # -------------------------------------------------------------------------
    # NOUVEAUX KPI — G : Incohérences NS ↔ CIAM
    # -------------------------------------------------------------------------
    kpi_incoherences = analyse_incoherences_ns_ciam(df_ciam_target, df_new_s, kc_data, col_pers)

    # -------------------------------------------------------------------------
    # NOUVEAUX KPI — E : Indicateurs Qualité Enrichis
    # -------------------------------------------------------------------------
    kpi_prospects = analyse_prospects_ciam(kc_data, df_new_s, df_ciam_target, col_pers)
    kpi_kpep_3_sources = analyse_coherence_kpep_3_sources(df_ciam_target, kc_data, dfs.get("IEHE"), col_pers)
    kpi_email_qualite = analyse_qualite_emails_enrichie(kc_data, df_new_s)
    kpi_dq_score = compute_data_quality_score(df_ciam_target, kc_data, col_pers)

    # Alertes métier : assurés manquants dans IEHE ET éligibles TP
    ids_assures_manquants_iehe = ids_assures - ids_iehe
    ids_manquants_iehe_et_tp = ids_assures_manquants_iehe.intersection(ids_tp_eligible)
    nb_manquants_iehe_et_tp = len(ids_manquants_iehe_et_tp)

    # Corrélation TP / IEHE par type d'assuré (pour Annexe)
    correlation_tp_type = {}
    totaux_presents = {"TP_Valide": 0, "TP_Non_Eligible": 0, "TP_Futur": 0}
    totaux_manquants = {"TP_Valide": 0, "TP_Non_Eligible": 0, "TP_Futur": 0}
    non_crees_et_tp_par_type = {}

    for pid, info in person_tp_map.items():
        type_assure = info["type"]
        tp_status = info["status"]
        in_iehe = pid in ids_iehe

        if type_assure not in correlation_tp_type:
            correlation_tp_type[type_assure] = {
                "Presents_IEHE": {"TP_Valide": 0, "TP_Non_Eligible": 0, "TP_Futur": 0},
                "Manquants_IEHE": {"TP_Valide": 0, "TP_Non_Eligible": 0, "TP_Futur": 0},
            }

        categorie = "Presents_IEHE" if in_iehe else "Manquants_IEHE"
        totaux = totaux_presents if in_iehe else totaux_manquants

        if tp_status == "ELIGIBLE_TP":
            correlation_tp_type[type_assure][categorie]["TP_Valide"] += 1
            totaux["TP_Valide"] += 1
        elif tp_status == "FUTURE_TP":
            correlation_tp_type[type_assure][categorie]["TP_Futur"] += 1
            totaux["TP_Futur"] += 1
        else:
            correlation_tp_type[type_assure][categorie]["TP_Non_Eligible"] += 1
            totaux["TP_Non_Eligible"] += 1

    if col_type and col_pers:
        for type_val, grp in df_new_s[mask_assure].groupby(col_type):
            ids_type = set(grp[col_pers].dropna().unique())
            non_crees_et_tp_par_type[type_val] = int(len((ids_type - ids_iehe).intersection(ids_tp_eligible)))

    # -------------------------------------------------------------------------
    # ANNEXE — ÉCART LIGNES vs PERSONNES (pour Annexe_IEHE)
    # Complète le breakdown du 6_IEHE.Ecart_Lignes_vs_Personnes avec les KPI
    # spécifiques à l'annexe : Assures_Non_Crees_IEHE_Et_Eligibles_TP et
    # Correlation_TP_Par_Type_Assure (comptage par ligne-contrat).
    # -------------------------------------------------------------------------
    annexe_ecart = {}
    if col_type and col_pers:
        # --- Correlation_TP_Par_Type_Assure par LIGNE (hors périmètre excluant PREV/073) ---
        correlation_tp_lignes: Dict[str, Dict[str, Dict[str, int]]] = {}
        STATUS_TO_KEY = {"ELIGIBLE_TP": "TP_Valide", "FUTURE_TP": "TP_Futur"}
        totaux_presents_lignes = {"TP_Valide": 0, "TP_Non_Eligible": 0, "TP_Futur": 0}
        totaux_manquants_lignes = {"TP_Valide": 0, "TP_Non_Eligible": 0, "TP_Futur": 0}

        for rec in line_tp_records:
            type_val = rec["type_assure"]
            pid = rec["num_personne"]
            status = rec["status"]
            in_iehe = pid in ids_iehe
            categorie = "Presents_IEHE" if in_iehe else "Manquants_IEHE"

            correlation_tp_lignes.setdefault(type_val, {
                "Presents_IEHE": {"TP_Valide": 0, "TP_Non_Eligible": 0, "TP_Futur": 0},
                "Manquants_IEHE": {"TP_Valide": 0, "TP_Non_Eligible": 0, "TP_Futur": 0},
            })
            key = STATUS_TO_KEY.get(status, "TP_Non_Eligible")
            correlation_tp_lignes[type_val][categorie][key] += 1
            if in_iehe:
                totaux_presents_lignes[key] += 1
            else:
                totaux_manquants_lignes[key] += 1

        # --- Assures_Non_Crees_IEHE_Et_Eligibles_TP par LIGNE ---
        # = lignes où num_personne manquant IEHE ET statut_TP = ELIGIBLE_TP
        # (restriction aux types assurés AI comme le KPI unique équivalent)
        non_crees_et_tp_lignes = 0
        non_crees_et_tp_lignes_par_type: Dict[str, int] = {}
        for rec in line_tp_records:
            pid = rec["num_personne"]
            type_val = rec["type_assure"]
            status = rec["status"]
            if type_val not in types_assures_ai:
                continue
            if status != "ELIGIBLE_TP":
                continue
            if pid in ids_iehe:
                continue
            non_crees_et_tp_lignes += 1
            non_crees_et_tp_lignes_par_type[type_val] = non_crees_et_tp_lignes_par_type.get(type_val, 0) + 1

        # Helper pour fabriquer un bloc {Lignes, Personnes_Uniques, Delta_Doublons}
        def _triplet(lignes: int, pers: int) -> Dict[str, int]:
            return {
                "Lignes": int(lignes),
                "Personnes_Uniques": int(pers),
                "Delta_Doublons": int(lignes - pers),
            }

        # Fusion Correlation : pour chaque (type, categorie, tp_status) → triplet
        correlation_tp_ecart: Dict[str, Dict[str, Dict[str, Dict[str, int]]]] = {}
        types_vus = set(correlation_tp_type.keys()) | set(correlation_tp_lignes.keys())
        for t in sorted(types_vus):
            correlation_tp_ecart[t] = {}
            for cat in ("Presents_IEHE", "Manquants_IEHE"):
                correlation_tp_ecart[t][cat] = {}
                pers_block = correlation_tp_type.get(t, {}).get(cat, {})
                ln_block = correlation_tp_lignes.get(t, {}).get(cat, {})
                for key in ("TP_Valide", "TP_Non_Eligible", "TP_Futur"):
                    correlation_tp_ecart[t][cat][key] = _triplet(
                        ln_block.get(key, 0), pers_block.get(key, 0)
                    )

        # Totaux TP (présents/manquants) en lignes-vs-personnes
        totaux_presents_ecart = {
            k: _triplet(totaux_presents_lignes.get(k, 0), totaux_presents.get(k, 0))
            for k in ("TP_Valide", "TP_Non_Eligible", "TP_Futur")
        }
        totaux_manquants_ecart = {
            k: _triplet(totaux_manquants_lignes.get(k, 0), totaux_manquants.get(k, 0))
            for k in ("TP_Valide", "TP_Non_Eligible", "TP_Futur")
        }

        # Assures_Non_Crees_IEHE_Et_Eligibles_TP en lignes-vs-personnes
        non_crees_ecart_global = _triplet(non_crees_et_tp_lignes, nb_manquants_iehe_et_tp)
        types_non_crees_vus = set(non_crees_et_tp_lignes_par_type.keys()) | set(non_crees_et_tp_par_type.keys())
        non_crees_ecart_par_type = {
            t: _triplet(
                non_crees_et_tp_lignes_par_type.get(t, 0),
                non_crees_et_tp_par_type.get(t, 0),
            )
            for t in sorted(types_non_crees_vus)
        }

        annexe_ecart = {
            "definition": (
                "Raison du delta entre comptage par lignes-contrat (NS_IEHE.csv) "
                "et par personnes uniques pour les KPI de l'annexe IEHE. "
                "Mêmes principes que 6_IEHE.Ecart_Lignes_vs_Personnes : "
                "une personne peut avoir plusieurs contrats → +1 ligne par contrat supplémentaire."
            ),
            "Assures_Non_Crees_IEHE_Et_Eligibles_TP": non_crees_ecart_global,
            "Assures_Non_Crees_IEHE_Et_Eligibles_TP_Par_Type": non_crees_ecart_par_type,
            "Correlation_TP_Par_Type_Assure": correlation_tp_ecart,
            "Totaux_TP_Presents_IEHE": totaux_presents_ecart,
            "Totaux_TP_Manquants_IEHE": totaux_manquants_ecart,
        }

    # =========================================================================
    # CONSTRUCTION DU RAPPORT JSON FINAL
    # =========================================================================

    # -----------------------------------------------------------------
    # Bloc Coherence_Emails (déplacé en section CIAM, conservé identique
    # pour ne pas perdre les définitions et la structure des sous-clés).
    # -----------------------------------------------------------------
    coherence_emails_block = {
        **email_quality_stats,
        "Email_CIAM_Rapproche_Egal_Val_Coord": {
            "definition": (
                "Parmi les comptes rapprochés : "
                "l'email du compte Keycloak matché (CIAM) = l'email valeur_coordonnee (New_S). "
                "Base = population rapprochée."
            ),
            "Nombre": nb_email_ciam_eq_val,
            "Pct_Sur_Rapproches": pct_email_ciam_eq_val,
            "Base_Rapproches": base_rapproches,
        },
        "KPI_Adresses_CIAM_Vides": {
            "definition": (
                "Parmi les comptes rapprochés ayant une valeur_coordonnee email : "
                "combien ont un email vide dans le compte Keycloak matché ? "
                "Base = population rapprochée."
            ),
            "Nombre": nb_adresses_ciam_vides,
            "Pct_Sur_Rapproches": pct_adresses_ciam_vides,
            "Base_Rapproches": base_rapproches,
        },
    }

    final_report = {
        "Fichier_Principal": target_file_new_s.name,
        "Prefixe_Execution": prefix,
        "Metadata": {
            "Date_Execution": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "Script_Version": "5.1_KPI_Regroupes_Par_Nature_CIAM_IEHE_TP",
        },

        # =================================================================
        # SECTIONS 1 → 4.Indicateurs : structure conservée à l'identique
        # (cf. demande métier : ne rien modifier jusqu'au bloc
        #  Doublons_Contrats_Diff_Offre_Par_Type).
        # =================================================================

        # -----------------------------------------------------------------
        # 1. LECTURES FICHIERS INPUT
        # -----------------------------------------------------------------
        "1_Input_Files": {
            "definition": (
                "Audit des fichiers chargés en entrée. "
                "CK = résultats requêtes KPEP ; CM = résultats requêtes Email ; "
                "Last/Middle = résultats requêtes Nom+Date/Prénom (sur non-rapprochés uniquement)."
            ),
            "Details": input_stats,
        },

        # -----------------------------------------------------------------
        # 2. VOLUMÉTRIE BRUTE
        # -----------------------------------------------------------------
        "2_Volumetrie_Brute": {
            "definition": (
                "Comptage des lignes du fichier New_S. "
                "1 ligne = 1 contrat. Une même personne peut avoir plusieurs lignes (multi-contrats)."
            ),
            "Resultats": volumetrie,
        },

        # -----------------------------------------------------------------
        # 3. VOLUMÉTRIE POPULATION UNIQUE
        # -----------------------------------------------------------------
        "3_Population_Unique": {
            "definition": (
                "Dédoublonnage sur num_personne. "
                "Population_Theorique_CIAM_Assures = cible du matching CIAM."
            ),
            "Global_Personnes": identifiants["Nbr_Num_Personne_Unique_Global"],
            "Cible_CIAM_Assures": identifiants["Population_Theorique_CIAM_Assures"],
            "Nbr_Contrats_Uniques": identifiants["Nbr_Contrats_Uniques"],
            "Conjoints_Uniques": identifiants["Nbr_Conjoints_Uniques"],
        },

        # -----------------------------------------------------------------
        # 4. QUALITÉ DES DONNÉES (Indicateurs doublons / anomalies)
        # NB : Coherence_Emails est désormais regroupé dans 5_CIAM.
        # -----------------------------------------------------------------
        "4_Qualite_Donnees": {
            "definition": (
                "Analyse des doublons métier et anomalies de contrats dans New_S. "
                "(La cohérence des emails est désormais regroupée dans la section 5_CIAM.)"
            ),
            "Indicateurs": doublons_stats,
        },

        # =================================================================
        # 5. CIAM — Tous les indicateurs Keycloak / rapprochement
        # Ordre : (1) chiffre clé global → (2) détails du matching →
        # (3) qualité côté CIAM → (4) qualité côté NS → (5) cohérence inter-sources.
        # =================================================================
        "5_CIAM": {
            "definition": (
                "Indicateurs relatifs au référentiel CIAM (Keycloak) : "
                "rapprochement, segmentation, cohérence emails, qualité des comptes, "
                "incohérences NS↔CIAM, qualité des sources extraites et indicateurs enrichis."
            ),

            # ---- (1) CHIFFRE CLÉ : taux de couverture global ----
            "Matching_Global": {
                "definition": (
                    "Rapprochement des assurés uniques avec CIAM (Keycloak). "
                    "Méthode Option B (set-based) : toutes les valeurs candidates d'une personne "
                    "sont testées simultanément. "
                    "Ordre : Mail_CIAM > Valeur_Coord > KPEP > Identite_Full > Identite_Inversee "
                    "> Recherche_Large_Nom > Recherche_Large_Middle."
                ),
                "Global": {
                    "Cible": cible,
                    "Rapproches": nb_rapproches,
                    "Non_Rapproches": int(max(0, cible - nb_rapproches)),
                    "Taux_Couverture": round(taux, 2),
                },
            },

            # ---- (2) DÉTAILS DU MATCHING ----
            "Detail_Matching": {
                "definition": "Détail du rapprochement CIAM par méthode et par source.",
                "Par_Methode": method_counts,
                "Par_Source_Fichier": sources_breakdown,
                "Sources_Reference": {
                    "CK_Principal": input_stats["CK"],
                    "CM_Complementaire": input_stats["CM"],
                    "Last_Complementaire": input_stats["Last"],
                    "Middle_Complementaire": input_stats["Middle"],
                },
                "Doublons_Contrats_Diff_Offre": nb_diff_offre,
                "Doublons_Contrats_Meme_Offre_Par_Type": meme_offre_par_type,
                "Doublons_Contrats_Diff_Offre_Par_Type": diff_offre_par_type,
            },
            "Matching_Par_Segment": {
                "definition": (
                    "Analyse segmentée du rapprochement CIAM pour identifier les populations "
                    "moins bien couvertes et les anomalies de référentiel. "
                    "B1 — Par type d'assuré : taux de rapprochement distincts par type "
                    "(ASSPRI, MPACTI, MPRETR, MPVRET). "
                    "B2 — Par société : taux de rapprochement par code_soc_appart. "
                    "B3 — Décomposition des non-rapprochés (clé absente vs clé présente non trouvée). "
                    "B4 — Cohérence KPEP entre New_S et le compte Keycloak matché."
                ),
                **kpi_matching_seg,
            },

            # ---- (3) COHÉRENCE EMAILS (déplacé depuis 4_Qualite_Donnees) ----
            "Coherence_Emails": coherence_emails_block,

            # ---- (4) QUALITÉ DES COMPTES CIAM ET INCOHÉRENCES ----
            "Qualite_Comptes_CIAM": {
                "definition": (
                    "Indicateurs de complétude et de cohérence des comptes Keycloak (CIAM). "
                    "F1a — Sans email (ni email ni email_other). "
                    "F1b — Sans KPEP valide (préfixe KPEP absent). "
                    "F1c — Email_other seulement (migration incomplète). "
                    "F1d — Doublons email : même adresse liée à plusieurs comptes CIAM."
                ),
                **kpi_qualite_ciam,
            },
            "Incoherences_NS_CIAM": {
                "definition": (
                    "Détection des divergences entre New_S et les comptes CIAM rapprochés. "
                    "G1a — DDN différente. "
                    "G1b — Nom/prénom divergent (SequenceMatcher < 0.70). "
                    "G1c — Email valeur_coordonnee NS ≠ email CIAM."
                ),
                **kpi_incoherences,
            },

            # ---- (5) QUALITÉ DES SOURCES (extractions CK/CM/Last/Middle + New_S) ----
            "Qualite_Sources_Extraites": {
                "definition": (
                    "Qualité des données dans les fichiers CIAM extraits "
                    "(CK = KPEP, CM = Email, Last/Middle = Nom). "
                    "Permet de vérifier la complétude des champs clés."
                ),
                "CK": qualite_contacts["CK"],
                "CM": qualite_contacts["CM"],
                "Last": qualite_contacts["Last"],
                "Middle": qualite_contacts["Middle"],
            },
            "Qualite_NewS": {
                "definition": (
                    "Indicateurs de qualité des données source dans le fichier New_S. "
                    "A1 — Complétude des clés de rapprochement par personne unique. "
                    "A2 — Complétude par champ clé (mailciam, idkpep, date_naissance, nom, prenom). "
                    "A3 — Taux de radiation (lignes avec dateradassure renseigné)."
                ),
                **kpi_qualite_ns,
            },

            # ---- (6) INDICATEURS ENRICHIS (E + Score) ----
            "Prospects_CIAM": {
                "definition": (
                    "E1 — Comptes Keycloak créés avec birthdate = '1900-01-01' (prospects), "
                    "croisé avec NS et les rapprochés."
                ),
                **kpi_prospects,
            },
            "Coherence_KPEP_3_Sources": {
                "definition": (
                    "E2 — Cohérence stricte (trim + upper) du KPEP entre New_S, CIAM et IEHE."
                ),
                **kpi_kpep_3_sources,
            },
            "Qualite_Emails_Enrichie": {
                "definition": (
                    "E3 — Qualité des emails (invalides + à risque sur 25 domaines suspects) "
                    "sur les 3 sources CIAM, NS mailciam, NS valeur_coordonnee."
                ),
                **kpi_email_qualite,
            },
            "Score_Qualite_Donnees": {
                "definition": (
                    "Score global DATA_QUALITY_OK/KO par assuré rapproché "
                    "(prospect CIAM + KPEP incohérent + email invalide/à risque)."
                ),
                **kpi_dq_score,
            },
        },

        # =================================================================
        # 6. IEHE — Tous les indicateurs liés au référentiel IEHE
        # Ordre : (1) chiffre clé global → (2) détails par segment → (3) qualité.
        # =================================================================
        "6_IEHE": {
            "definition": (
                "Indicateurs relatifs au référentiel IEHE (iehe.refkpep) : "
                "présence, segmentation par type d'assuré et qualité du référentiel."
            ),

            # ---- (1) CHIFFRE CLÉ : présence globale ----
            "Presence_Globale": {
                "definition": (
                    "Présence dans le référentiel IEHE. "
                    "Population de référence = toutes personnes uniques du flux "
                    "(assurés + conjoints), jointure sur num_personne = refperboccn."
                ),
                "Population_Reference": pop_global,
                "Presents_IEHE": {
                    "Nombre": nb_presents_iehe,
                    "Taux": taux_presents_iehe,
                },
                "Manquants_IEHE": {
                    "Nombre": nb_manquants_iehe,
                    "Taux": taux_manquants_iehe,
                    # T2 — répartition Assurés / Conjoints demandée par Laurence.
                    # Source : manquants_iehe_par_type (déjà calculé section 6).
                    "dont_Assures": int(sum(
                        v for k, v in manquants_iehe_par_type.items()
                        if str(k).strip().upper() in types_assures_ai)),
                    "dont_Conjoints": int(sum(
                        v for k, v in manquants_iehe_par_type.items()
                        if "CONJ" in str(k).strip().upper())),
                },
                "Ecart_Lignes_vs_Personnes": ecart_lignes_personnes,
                "Retry_IEHE_KO": iehe_ko_stats if iehe_ko_stats else "Aucun fichier IEHE_KO disponible",
            },

            # ---- (2) DÉTAIL PAR TYPE D'ASSURÉ + CORRÉLATION TP ----
            "Detail_Par_Type_Assure": {
                "definition": "Détail IEHE par type d'assuré et corrélations avec l'éligibilité TP.",
                "Population_Ref_Par_Type_Assure": population_ref_par_type,
                "Presents_IEHE_Par_Type_Assure": presents_iehe_par_type,
                "Manquants_IEHE_Par_Type_Assure": manquants_iehe_par_type,
                "Assures_Non_Crees_IEHE_Et_Eligibles_TP": nb_manquants_iehe_et_tp,
                "Assures_Non_Crees_IEHE_Et_Eligibles_TP_Par_Type": non_crees_et_tp_par_type,
                "Correlation_TP_Par_Type_Assure": dict(sorted(correlation_tp_type.items())),
                "Totaux_TP_Presents_IEHE": totaux_presents,
                "Totaux_TP_Manquants_IEHE": totaux_manquants,
                "Ecart_Lignes_vs_Personnes": annexe_ecart,
            },

            # ---- (3) QUALITÉ DU RÉFÉRENTIEL IEHE ----
            "Qualite_Referentiel": {
                "definition": (
                    "Indicateurs de qualité du référentiel IEHE et de cohérence avec les autres sources. "
                    "D1 — Complétude email IEHE (adrmailctc). "
                    "D2 — Concordance email IEHE vs CIAM (rapprochés présents en IEHE). "
                    "D3 — Concordance société IEHE vs New_S (socappr vs code_soc_appart)."
                ),
                **kpi_iehe_qualite,
            },
        },

        # =================================================================
        # 7. CARTE TP — Tous les indicateurs Tiers Payant
        # Ordre : (1) éligibilité globale → (2) détail par offre/société →
        # (3) enrichissements opérationnels → (4) contrôle GED quotidien.
        # =================================================================
        "7_Carte_TP": {
            "definition": (
                "Indicateurs relatifs à la carte Tiers Payant : éligibilité globale, "
                "détail par société/offre/type, indicateurs opérationnels enrichis et "
                "contrôle GED quotidien."
            ),

            # ---- (1) CHIFFRE CLÉ : éligibilité globale ----
            "Eligibilite_Globale": {
                "definition": (
                    "Éligibilité carte Tiers Payant. "
                    "Périmètre : assurés ASSPRI/MPACTI/MPRETR/MPVRET, "
                    "hors offres PREV (préfixe MEP/IND) et hors société 073. "
                    "Règle : delta = date_effet_adhesion - date_adhesion. "
                    "ELIGIBLE si delta < 22j (valeurs négatives comprises). "
                    "FUTURE si delta >= 22j."
                ),
                "Population_Base": tp_stats.get("population_base", 0),
                "Population_Eligible": {
                    "Nombre": tp_stats.get("population_eligible", 0),
                    "Taux": tp_stats.get("taux_eligible", 0.0),
                },
                "Population_Future": {
                    "Nombre": tp_stats.get("population_future", 0),
                    "Taux": tp_stats.get("taux_future", 0.0),
                },
                "Population_PREV": {
                    "Nombre": tp_stats.get("population_prev", 0),
                },
                "Eligible_Par_Mois": tp_stats.get("eligible_par_mois", {}),
            },

            # ---- (2) DÉTAIL PAR SOCIÉTÉ / OFFRE / TYPE ----
            "Detail_Par_Type": {
                "definition": "Détail éligibilité TP par (code société | offre | type assuré).",
                "Non_Eligibles": tp_annexe_non_eligible,
                "Detail_Par_Type": tp_detail_par_type,
            },

            # ---- (3) INDICATEURS ENRICHIS (priorisation opérationnelle) ----
            "TP_Enrichi_Operationnel": {
                "definition": (
                    "Analyses complémentaires sur l'éligibilité TP. "
                    "C1 — Éligibles TP non rapprochés CIAM (impact opérationnel immédiat). "
                    "C2 — Délai avant éligibilité (FUTURE_TP) : distribution par tranches. "
                    "C3 — Répartition des statuts TP par société."
                ),
                **kpi_tp_enrichi,
            },

            # ---- (4) CONTRÔLE GED QUOTIDIEN ----
            "Controle_GED_Quotidien": tp_ged_stats,
        },

        # =================================================================
        # 8. AUTRES INDICATEURS — KPI transversaux ne relevant ni de
        # CIAM, ni d'IEHE, ni de la carte TP.
        # =================================================================
        "8_Autres_Indicateurs": {
            "definition": (
                "Indicateurs complémentaires ne relevant pas des catégories CIAM / IEHE / TP."
            ),
            "Prestations_Rejets_Plus_Anciens": kpi5_rejets_anciennete,
            "Prestations_Par_Offre": calcul_prestations_par_offre(prefix),
        },
    }

    final_report = reshape_to_modele_clean(final_report)

    thematic_paths = export_thematic_jsons(final_report, OUTPUT_DIR, prefix)

    # Debug export : 1 ligne par personne (sets + résultat)
    df_ciam_target.to_csv(OUTPUT_DIR / "detail_matching_ciam.csv", index=False, sep=";")

    print("\n=== RAPPORT KPI COMPLET GÉNÉRÉ ===")
    print(json.dumps(final_report, indent=4, cls=NumpyEncoder))
    for p in thematic_paths:
        print(f"Sauvegarde : {p}")

if __name__ == "__main__":
    main()