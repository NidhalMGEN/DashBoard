# -*- coding: utf-8 -*-
"""
iehe_ko_lib.py — Helpers purs pour la classification des lignes IEHE_KO.

Centralise la logique utilisée par :
  - 02_calcul_kpi.py (calcul du bloc Retry_IEHE_KO)
  - scripts/migrate_iehe_ko_legacy.py (back-fill des CSV anciens)

Objectif : éviter la duplication de la logique TP/type_assure et permettre
des tests unitaires (le script principal ayant un nom débutant par un chiffre,
il n'est pas importable directement).
"""
from __future__ import annotations

import re
import unicodedata
import warnings
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

import pandas as pd


# --- Périmètre TP partagé avec 02_calcul_kpi / 03_generation_fichiers_detail ---
TYPES_TP_KO = frozenset({"ASSPRI", "MPACTI", "MPRETR", "MPVRET"})
OFFRES_PREV_NON_ELIGIBLES_KO = frozenset({"INPPREVIND"})
TYPES_ASSURES = frozenset({"ASSPRI", "MPACTI", "MPRETR", "MPVRET"})

# --- Alias de colonnes "num_personne" / "type_assure" ------------------------
# La détection de la personne est la clé des fallbacks A2/B/C : un alias manquant
# ⇒ pid vide ⇒ aucune source ne résout le type ⇒ tout bascule en INCONNU dans la
# ventilation Retry_IEHE_KO.Par_Type_Assure. Le script 03 détecte la colonne
# personne parmi {num_personne, numpersonne, num_pers, id_personne} puis la
# préfixe `NS_` dans le fichier KO ; il faut donc reconnaître TOUTES ces formes
# (get_ci/_detect_col gèrent déjà la casse et les accents, on ne liste que les
# bases distinctes). `refperboccn` est la clé de jointure IEHE = num_personne.
PERSON_COL_ALIASES = (
    "NS_num_personne", "NS_num_pers", "NS_numpersonne", "NS_id_personne",
    "num_personne", "numpersonne", "num_pers", "id_personne", "refperboccn",
)
TYPE_COL_ALIASES = (
    "NS_type_assure", "NS_typeassure", "type_assure", "typeassure",
)


def normalize_text(text: Any) -> str:
    """Normalisation alignée sur 02_calcul_kpi.normalize_text (upper + sans accent)."""
    if text is None:
        return ""
    try:
        if pd.isna(text) or text == "":
            return ""
    except (TypeError, ValueError):
        pass
    s = str(text).upper().strip()
    s = unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode("utf-8")
    return s


def get_ci(row: Any, *aliases: str) -> Any:
    """Lookup case-insensitive dans une `pd.Series` ou un dict.

    Les colonnes lues par `02_calcul_kpi.load_csv` sont lowercased ; les
    fichiers IEHE_KO sont écrits par `03_generation_fichiers_detail.py` avec
    des entêtes mixtes (`NS_type_assure`, `Eligibilité TP`...). Ce helper
    accepte plusieurs alias (différentes casses, sans/avec accents) et
    retourne la première valeur non vide trouvée.
    """
    if row is None:
        return None
    # Normalise les clés disponibles une seule fois
    if isinstance(row, pd.Series):
        keys = list(row.index)
    elif isinstance(row, dict):
        keys = list(row.keys())
    else:
        return None
    key_map: Dict[str, str] = {}
    for k in keys:
        if k is None:
            continue
        k_str = str(k)
        for variant in (k_str, k_str.lower(), k_str.upper(),
                        _strip_accents(k_str.lower()),
                        _strip_accents(k_str.upper())):
            key_map.setdefault(variant, k_str)

    for alias in aliases:
        if alias is None:
            continue
        for variant in (alias, alias.lower(), alias.upper(),
                        _strip_accents(alias.lower()),
                        _strip_accents(alias.upper())):
            if variant in key_map:
                val = row[key_map[variant]]
                if val is None:
                    continue
                try:
                    if pd.isna(val):
                        continue
                except (TypeError, ValueError):
                    pass
                return val
    return None


def _strip_accents(s: str) -> str:
    return unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode("utf-8")


def parse_date_any(s: Any) -> Optional[date]:
    """Parse une date sous formats variés. Retourne `None` si illisible."""
    if s is None:
        return None
    try:
        if pd.isna(s):
            return None
    except (TypeError, ValueError):
        pass
    s = str(s).strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    # Dernier recours : pandas dayfirst
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            dt = pd.to_datetime(s, dayfirst=True, errors="coerce")
        if pd.isna(dt):
            return None
        return dt.date()
    except Exception:
        return None


def tp_status(type_assure: str, offre: str, code_soc: str,
              d_adh: Optional[date], d_eff: Optional[date]) -> str:
    """Retourne le statut TP d'une ligne KO :
    - "Hors_Perimetre_TP" : type non assuré (CONJ/ENF…), offre PREV, ou société 073.
    - "Eligible_TP"       : assuré in-scope ET (date_effet - date_adh) <= 21j.
    - "Future_TP"         : assuré in-scope mais delta >= 22j (ou dates manquantes).
    """
    ta = (type_assure or "").strip().upper()
    of = (offre or "").strip().upper()
    cs = (code_soc or "").strip()
    if ta not in TYPES_TP_KO:
        return "Hors_Perimetre_TP"
    if of.startswith(("MEP", "IND")) or of in OFFRES_PREV_NON_ELIGIBLES_KO:
        return "Hors_Perimetre_TP"
    if cs == "073":
        return "Hors_Perimetre_TP"
    if d_adh is None or d_eff is None:
        return "Future_TP"
    return "Eligible_TP" if (d_eff - d_adh).days <= 21 else "Future_TP"


def classify_ko_row(
    row: Any,
    ns_current_lookup: Optional[Dict[str, Dict[str, Any]]] = None,
    ns_historical_lookup: Optional[Dict[str, Dict[str, Any]]] = None,
    ns_iehe_lookup: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Tuple[str, str]:
    """Retourne `(type_assure, eligibilite_tp_label)` à partir des sources disponibles.

    Source A  : colonnes du fichier IEHE_KO enrichi (`NS_type_assure`, `NS_offre`...).
    Source A2 : fallback via `num_personne` → lookup du `{prefix}_NS_IEHE.csv` du
                MÊME run que le fichier KO. C'est la source la plus fiable pour les
                fichiers KO legacy (générés avant l'ajout des colonnes d'enrichissement)
                car le NS_IEHE co-daté contient toujours `NS_type_assure`, l'offre,
                la société et les dates pour exactement ces personnes — y compris
                celles absentes des New_S (rotation) qui faisaient échouer B et C
                (symptôme : tout INCONNU / Hors_Perimetre_TP).
    Source B  : fallback via `num_personne` → lookup du New_S courant.
    Source C  : fallback via `num_personne` → lookup multi-NS historique.

    Précédence des flags pré-calculés :
      - Le flag `Eligibilité TP` écrit par `03_generation_fichiers_detail`
        ne prime que si la Source A porte aussi un `NS_type_assure`
        renseigné. Si `NS_type_assure` est vide (cas typique : 03 a écrit
        "N" avec raison "type vide"), on **recalcule** via `tp_status()`
        à partir des données fusionnées A>B>C — sinon une décision stale
        figerait le KO en Hors_Perimetre_TP même quand B/C ramène ASSPRI.

    Sémantique `Future_TP` alignée avec `compute_carte_tp_row` :
      - Si flag = "O" mais une des dates manque, on retourne `Future_TP`
        (et pas `Eligible_TP`) — cohérent avec l'écriture du 03.

    Lecture case-insensitive : `load_csv` lowercase les colonnes, or les
    CSV IEHE_KO sont écrits avec des entêtes mixtes (`NS_*`).
    """
    # --- Source A : colonnes du fichier (multi-casse) -----------------------
    ta_source_a = normalize_text(get_ci(row, *TYPE_COL_ALIASES))
    of_a = normalize_text(get_ci(row, "NS_offre", "ns_offre"))
    raw_cs = get_ci(row, "NS_code_soc_appart", "ns_code_soc_appart",
                    "NS_code_soc", "ns_code_soc")
    cs_a = "" if raw_cs is None else str(raw_cs).strip()
    d_adh_a = parse_date_any(get_ci(row, "NS_date_adhesion", "ns_date_adhesion",
                                     "NS_date_adh", "ns_date_adh"))
    d_eff_a = parse_date_any(get_ci(row, "NS_date_effet_adhesion",
                                     "ns_date_effet_adhesion",
                                     "NS_date_effet", "ns_date_effet"))

    # --- num_personne (clé des fallbacks A2/B/C) ----------------------------
    pid = normalize_text(get_ci(row, *PERSON_COL_ALIASES))

    # --- Source A2 : NS_IEHE co-daté (même run que le fichier KO) ------------
    ns_iehe = (ns_iehe_lookup or {}).get(pid, {}) if pid else {}

    # --- Source B : New_S courant -------------------------------------------
    ns_b = (ns_current_lookup or {}).get(pid, {}) if pid else {}

    # --- Source C : New_S historique ----------------------------------------
    ns_c = (ns_historical_lookup or {}).get(pid, {}) if pid else {}

    # Sélection : Source A prioritaire, puis A2 (NS_IEHE co-daté), puis B, puis C
    ta = (ta_source_a or ns_iehe.get("type_assure")
          or ns_b.get("type_assure") or ns_c.get("type_assure") or "")
    offre = (of_a or ns_iehe.get("offre")
             or ns_b.get("offre") or ns_c.get("offre") or "")
    code_soc = (cs_a or ns_iehe.get("code_soc")
                or ns_b.get("code_soc") or ns_c.get("code_soc") or "")
    d_adh = (d_adh_a or ns_iehe.get("date_adh")
             or ns_b.get("date_adh") or ns_c.get("date_adh"))
    d_eff = (d_eff_a or ns_iehe.get("date_eff")
             or ns_b.get("date_eff") or ns_c.get("date_eff"))

    # --- Flag pré-calculé éligibilité ---------------------------------------
    elig_flag = normalize_text(get_ci(row, "Eligibilité TP", "Eligibilite TP",
                                       "eligibilité tp", "eligibilite tp"))
    valeur_tp = normalize_text(get_ci(row, "Valeur carte TP", "valeur carte tp"))

    # Le flag ne fait foi que si la Source A portait un type_assure (cf. audit C4).
    flag_trusted = bool(ta_source_a) and elig_flag in ("O", "N")
    if flag_trusted and elig_flag == "N":
        label = "Hors_Perimetre_TP"
    elif flag_trusted and elig_flag == "O":
        if valeur_tp == "FUTUR":
            label = "Future_TP"
        elif d_adh is not None and d_eff is not None:
            label = "Eligible_TP" if (d_eff - d_adh).days <= 21 else "Future_TP"
        else:
            # Aligné avec compute_carte_tp_row : dates manquantes ⇒ Future_TP.
            label = "Future_TP"
    else:
        # Pas de flag fiable : reconstruit depuis le périmètre TP via A>B>C.
        label = tp_status(ta, offre, code_soc, d_adh, d_eff)

    return (ta or "INCONNU", label)


def build_ns_lookup_from_df(
    df_new_s: pd.DataFrame,
    col_pers: str,
    col_type: str,
) -> Dict[str, Dict[str, Any]]:
    """Construit un lookup `{num_personne -> {type_assure, offre, code_soc, date_adh, date_eff}}`
    depuis un DataFrame `New_S` *déjà* passé dans `preprocess_new_s` (colonnes
    `NS_offre`, `NS_code_soc`, `NS_date_adh`, `NS_date_effet` disponibles).

    Premier rencontré gagne (cohérent avec l'ancienne implémentation inline).
    """
    out: Dict[str, Dict[str, Any]] = {}
    if df_new_s is None or col_pers is None or col_type is None:
        return out
    needed = [col_pers, col_type, "NS_offre", "NS_code_soc",
              "NS_date_adh", "NS_date_effet"]
    missing = [c for c in needed if c not in df_new_s.columns]
    if missing:
        return out
    for tup in df_new_s[needed].itertuples(index=False, name=None):
        raw_pid, raw_type, raw_offre, raw_soc, raw_adh, raw_eff = tup
        pid = normalize_text(raw_pid)
        if not pid or pid in out:
            continue
        out[pid] = {
            "type_assure": normalize_text(raw_type),
            "offre": normalize_text(raw_offre),
            "code_soc": "" if pd.isna(raw_soc) else str(raw_soc).strip(),
            "date_adh": parse_date_any("" if pd.isna(raw_adh) else str(raw_adh)),
            "date_eff": parse_date_any("" if pd.isna(raw_eff) else str(raw_eff)),
        }
    return out


def build_ns_iehe_lookup(df_ns_iehe: pd.DataFrame) -> Dict[str, Dict[str, Any]]:
    """Construit `{num_personne -> {type_assure, offre, code_soc, date_adh, date_eff}}`
    depuis un DataFrame `{prefix}_NS_IEHE.csv` (sortie de `03_generation_fichiers_detail`).

    Sert de Source A2 dans `classify_ko_row` : le NS_IEHE co-daté contient
    `NS_type_assure`, `NS_offre`, `NS_code_soc_appart`, `NS_date_adhesion` et
    `NS_date_effet_adhesion` pour TOUTES les personnes du run (présentes ET
    absentes d'IEHE), ce qui permet de fiabiliser la ventilation des KPI Retry
    IEHE_KO même quand le fichier KO est legacy (sans colonnes d'enrichissement).

    Lecture case-insensitive (load_csv lowercase les entêtes). Premier
    num_personne rencontré gagne.
    """
    out: Dict[str, Dict[str, Any]] = {}
    if df_ns_iehe is None or df_ns_iehe.empty:
        return out
    col_pid = _detect_col(df_ns_iehe, PERSON_COL_ALIASES)
    col_type = _detect_col(df_ns_iehe, TYPE_COL_ALIASES)
    if not col_pid or not col_type:
        return out
    col_offre = _detect_col(df_ns_iehe, ["ns_offre", "offre"])
    col_soc = _detect_col(df_ns_iehe, ["ns_code_soc_appart", "ns_code_soc",
                                       "code_soc_appart", "code_soc"])
    col_adh = _detect_col(df_ns_iehe, ["ns_date_adhesion", "ns_date_adh",
                                       "date_adhesion", "date_adh"])
    col_eff = _detect_col(df_ns_iehe, ["ns_date_effet_adhesion", "ns_date_effet",
                                       "date_effet_adhesion", "date_effet"])

    cols = [col_pid, col_type, col_offre, col_soc, col_adh, col_eff]
    for tup in df_ns_iehe[[c for c in cols if c]].itertuples(index=False, name=None):
        vals = dict(zip([c for c in cols if c], tup))
        pid = normalize_text(vals.get(col_pid))
        if not pid or pid in out:
            continue
        raw_soc = vals.get(col_soc) if col_soc else None
        out[pid] = {
            "type_assure": normalize_text(vals.get(col_type)),
            "offre": normalize_text(vals.get(col_offre)) if col_offre else "",
            "code_soc": "" if (raw_soc is None or (isinstance(raw_soc, float) and pd.isna(raw_soc)))
                        else str(raw_soc).strip(),
            "date_adh": parse_date_any(vals.get(col_adh)) if col_adh else None,
            "date_eff": parse_date_any(vals.get(col_eff)) if col_eff else None,
        }
    return out


# Patterns connus pour les fichiers New_S historiques
_NEW_S_PATTERN = re.compile(r"^(?P<prefix>\d{8})_New_S(\..*)?\.csv$", re.IGNORECASE)


def list_historical_new_s(
    input_dir: Path,
    exclude_prefix: Optional[str] = None,
) -> list:
    """Liste les fichiers `<DDMMYYYY>_New_S*.csv` triés du plus récent au plus ancien.

    Le préfixe à exclure (`exclude_prefix`, typiquement le run courant) est filtré.
    """
    if not isinstance(input_dir, Path):
        input_dir = Path(input_dir)
    candidates = []
    for f in input_dir.glob("*New_S*.csv"):
        m = _NEW_S_PATTERN.match(f.name)
        if not m:
            continue
        prefix = m.group("prefix")
        if exclude_prefix and prefix == exclude_prefix:
            continue
        try:
            dt = datetime.strptime(prefix, "%d%m%Y")
        except ValueError:
            continue
        candidates.append((dt, f))
    candidates.sort(key=lambda t: t[0], reverse=True)
    return [f for _, f in candidates]


def build_historical_ns_lookup(
    input_dir: Path,
    exclude_prefix: Optional[str] = None,
    preprocess_fn=None,
    max_files: int = 30,
    max_entries: int = 5_000_000,
) -> Dict[str, Dict[str, Any]]:
    """Construit un lookup multi-NS en mergeant les `New_S` historiques.

    Stratégie : "le plus récent gagne" — on parcourt du plus récent au plus
    ancien, et on n'écrase pas une clé déjà présente.

    `preprocess_fn` (callable `df -> df`) est appelé sur chaque NS pour
    produire les colonnes `NS_offre`, `NS_code_soc`, `NS_date_adh`,
    `NS_date_effet`. S'il n'est pas fourni, on lit en mode dégradé.

    ⚠️ CONTRAT (cf. audit non-régression) :
    `preprocess_fn` est typiquement `02_calcul_kpi.preprocess_new_s`. Le
    contrat de cette fonction est qu'elle produit les 4 colonnes
    `NS_offre`/`NS_code_soc`/`NS_date_adh`/`NS_date_effet`. Si un futur
    changement renomme/supprime une de ces colonnes, ce lookup tombera
    silencieusement en mode dégradé (type_assure uniquement). La détection
    se fait via `has_ns_cols` : penser à mettre à jour la liste si le
    contrat évolue.

    Garde-fous (cf. audit M5 : risque mémoire potentiel sur gros volumes) :
      - `max_files` : nombre max de NS lus (les plus récents).
      - `max_entries` : plafond global sur la taille du lookup. Si atteint,
        on arrête la lecture et on logue un WARN explicite (taille typique
        ~200B/entrée Python ⇒ 5M entries ≈ 1 Go RAM).
      - Si `len(files) > max_files`, on logue les fichiers ignorés.
    """
    lookup: Dict[str, Dict[str, Any]] = {}
    all_files = list_historical_new_s(input_dir, exclude_prefix=exclude_prefix)
    if len(all_files) > max_files:
        ignored = [f.name for f in all_files[max_files:]]
        print(f"   [WARN] Lookup NS historique tronqué à {max_files} fichiers "
              f"({len(ignored)} ignoré(s), ex: {ignored[:3]}).")
    files = all_files[:max_files]
    truncated = False
    for f in files:
        if len(lookup) >= max_entries:
            truncated = True
            break
        df = _read_ns_csv_safe(f)
        if df is None or df.empty:
            continue
        if preprocess_fn is not None:
            try:
                df = preprocess_fn(df)
            except Exception as exc:  # noqa: BLE001
                print(f"   [WARN] preprocess_fn a échoué sur {f.name} : {exc}")
                continue

        col_pers = _detect_col(df, PERSON_COL_ALIASES)
        col_type = _detect_col(df, TYPE_COL_ALIASES)
        if not col_pers or not col_type:
            continue

        # Si preprocess_fn n'a pas tourné, les colonnes NS_* peuvent manquer :
        # on tente une lecture dégradée (type uniquement).
        has_ns_cols = all(c in df.columns for c in
                          ["NS_offre", "NS_code_soc", "NS_date_adh", "NS_date_effet"])

        for tup in df[[col_pers, col_type] + (
                ["NS_offre", "NS_code_soc", "NS_date_adh", "NS_date_effet"]
                if has_ns_cols else []
        )].itertuples(index=False, name=None):
            raw_pid, raw_type, *rest = tup
            pid = normalize_text(raw_pid)
            if not pid or pid in lookup:
                continue
            entry = {
                "type_assure": normalize_text(raw_type),
                "offre": "",
                "code_soc": "",
                "date_adh": None,
                "date_eff": None,
                "_source_file": f.name,
            }
            if has_ns_cols and rest:
                raw_offre, raw_soc, raw_adh, raw_eff = rest
                entry["offre"] = normalize_text(raw_offre)
                entry["code_soc"] = "" if pd.isna(raw_soc) else str(raw_soc).strip()
                entry["date_adh"] = parse_date_any(
                    "" if pd.isna(raw_adh) else str(raw_adh))
                entry["date_eff"] = parse_date_any(
                    "" if pd.isna(raw_eff) else str(raw_eff))
            lookup[pid] = entry
            if len(lookup) >= max_entries:
                truncated = True
                break
        if truncated:
            break
    if truncated:
        print(f"   [WARN] Lookup NS historique : plafond {max_entries} "
              f"entrees atteint, arret de la lecture. Augmentez max_entries "
              f"si besoin (attention RAM).")
    return lookup


def _detect_col(df: pd.DataFrame, aliases: Iterable[str]) -> Optional[str]:
    if df is None:
        return None
    cols_lower = {c.lower(): c for c in df.columns}
    for a in aliases:
        if a.lower() in cols_lower:
            return cols_lower[a.lower()]
    return None


def detect_separator(filepath: Path) -> str:
    """Auto-détecte le séparateur d'un CSV (\\t, ;, ,)."""
    with open(filepath, "r", encoding="utf-8-sig", errors="replace") as f:
        first_line = f.readline()
    if "\t" in first_line:
        return "\t"
    if ";" in first_line:
        return ";"
    return ","


def _read_ns_csv_safe(path: Path) -> Optional[pd.DataFrame]:
    """Lit un CSV New_S de manière défensive (encoding utf-8-sig → utf-8 → latin-1).

    Audit sécurité (encoding latin-1 silencieux peut masquer du mojibake) :
    on logue explicitement l'encodage retenu si on retombe sur latin-1.
    """
    if not path.exists():
        return None
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter(action="ignore", category=FutureWarning)
                df = pd.read_csv(path, sep=None, engine="python",
                                 encoding=encoding, dtype=str)
            if encoding == "latin-1":
                print(f"   [WARN] {path.name} lu en latin-1 (utf-8 a échoué). "
                      f"Risque de mojibake sur les accents — à investiguer.")
            return df
        except UnicodeDecodeError:
            continue
        except Exception as exc:  # noqa: BLE001
            print(f"   [WARN] Lecture {path.name} échouée ({encoding}) : {exc}")
            return None
    return None


# Colonnes obligatoires sur un IEHE_KO "enrichi" (sortie 03_generation_fichiers_detail).
# La présence/absence permet de décider si une migration legacy est nécessaire.
IEHE_KO_REQUIRED_COLS = (
    "NS_num_personne",
    "NS_type_assure",
    "NS_offre",
    "NS_code_soc_appart",
    "NS_date_adhesion",
    "NS_date_effet_adhesion",
    "Eligibilité TP",
    "Valeur carte TP",
)


def has_enriched_columns(columns: Iterable[str]) -> bool:
    """Vérifie qu'un IEHE_KO a déjà été enrichi (case-insensitive)."""
    lower = {str(c).lower() for c in columns}
    return all(c.lower() in lower for c in IEHE_KO_REQUIRED_COLS)


def compute_carte_tp_row(
    type_assure: str,
    offre: str,
    code_soc: str,
    date_effet_str: str,
    date_adh_str: str,
    eligibility_mode: str = "adh_plus_21",
) -> Tuple[str, str, str, str]:
    """Retourne `(eligibilite_O_N, date_eligibilite_str, valeur_carte_tp, raison)`.

    Réplique exacte de `03_generation_fichiers_detail.compute_carte_tp_row`
    pour permettre la réutilisation par `scripts/migrate_iehe_ko_legacy.py`
    (le script 03 ayant un nom non importable).

    Logique (alignée script 02) :
    - Périmètre : TYPES_TP_KO, hors offres PREV (préfixe MEP/IND) et hors société 073.
    - delta = date_effet - date_adh
    - ELIGIBLE si delta <= 21j (négatives comprises) → valeur = ""
    - FUTURE   si delta >= 22j                       → valeur = "Futur"
    """
    ta = (type_assure or "").strip().upper()
    of = (offre or "").strip().upper()
    cs = (code_soc or "").strip()
    if ta not in TYPES_TP_KO:
        return "N", "", "", f"Type assuré non éligible ({ta or 'vide'})"
    if of.startswith(("MEP", "IND")) or of in OFFRES_PREV_NON_ELIGIBLES_KO or cs == "073":
        return "N", "", "", "Offre PREV exclue"

    try:
        d_adh = datetime.strptime((date_adh_str or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        d_adh = None
    try:
        d_eff = datetime.strptime((date_effet_str or "").strip(), "%Y-%m-%d").date() \
            if (date_effet_str or "").strip() else None
    except ValueError:
        d_eff = None

    if eligibility_mode == "effet_minus_21":
        elig_date_str = (d_eff - timedelta(days=21)).strftime("%d/%m/%Y") if d_eff else ""
    else:
        elig_date_str = (d_adh + timedelta(days=21)).strftime("%d/%m/%Y") if d_adh else ""

    if not (date_effet_str or "").strip() or d_eff is None or d_adh is None:
        return "O", elig_date_str, "Futur", ""

    delta = (d_eff - d_adh).days
    if delta <= 21:
        return "O", elig_date_str, "", ""
    return "O", elig_date_str, "Futur", ""


def to_iso_date(date_raw: Any) -> str:
    """Convertit une date en format ISO `YYYY-MM-DD`. Retourne `""` si illisible."""
    d = parse_date_any(date_raw)
    if d is None:
        return ""
    return d.strftime("%Y-%m-%d")
