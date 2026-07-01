# -*- coding: utf-8 -*-
"""
prestations_par_offre_lib.py — Helpers partagés T1 (MDG_Prestations_par_offre).

Responsabilités :
  - Charger `config/mapping_offres.yml` (source unique de vérité).
  - Enrichir un DataFrame de prestations avec les colonnes `_nature`,
    `_societe` (lookup case-insensitive + strip).
  - Construire le classeur Excel multi-onglets aligné sur la maquette
    Laurence : TCD1, Prestations par offre, Prévoyance, Ste 010, Ste 044,
    Ste 052, TCD2, <NATURE>_ORIGINE.
  - Lire ce classeur pour alimenter la section JSON `8_Autres_Indicateurs.
    Prestations_Par_Offre` produite par `02_calcul_kpi.py`.

Utilisé par :
  - `launch_SQL_query_V2.py` (post-processor + workbook builder)
  - `02_calcul_kpi.py` (reader JSON)
"""
from __future__ import annotations

import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import yaml


REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_MAPPING_PATH = REPO_ROOT / "config" / "mapping_offres.yml"

# Caractères interdits dans un nom de feuille Excel (cf. openpyxl) :
# `: \ / ? * [ ]` + apostrophe en début/fin. On valide aussi les natures
# afin qu'elles soient utilisables dans `f"{nature}_ORIGINE"`.
_VALID_NATURE_RE = re.compile(r"^[A-Z0-9_]+$")
_VALID_SOCIETE_RE = re.compile(r"^[A-Z0-9_?]+$")  # "?" tolere pour fallback
_VALID_PREFIX_DDMMYYYY = re.compile(r"^\d{8}$")

# Cache + verrou pour `load_mapping` (thread-safety du `_warned_unknown`).
_MAPPING_CACHE: Dict[str, Any] = {}
_MAPPING_CACHE_LOCK = threading.Lock()

# Colonnes du SQL Laurence (sortie quotidienne).
COL_DATE = "Date_arrivee"
COL_OFFRE = "Offre"
COL_ORIGINE = "Origine"
COL_STATUT = "Etat"
COL_NB = "Nb_dossiers"


# ---------------------------------------------------------------------------
# Chargement du mapping
# ---------------------------------------------------------------------------

def load_mapping(path: Optional[Path] = None,
                  use_cache: bool = True) -> Dict[str, Any]:
    """Charge `config/mapping_offres.yml` et retourne un dict normalisé.

    - Cache module-level (thread-safe via `_MAPPING_CACHE_LOCK`) : évite de
      relire le YAML à chaque appel post-processor.
    - Validation regex des `nature` et `societe` (caractères interdits Excel).
    - Le set `_warned_unknown` vit dans le dict mappé et est aussi protégé
      par le lock à `lookup_offre`.

    Format de retour :
        {
            "offres": {OFFRE_STRIPPED_UPPER: {"nature": ..., "societe": ...}},
            "a_confirmer": {...}, "origines": [...],
            "natures_avec_onglet_origine": [...], "statuts": [...],
            "_warned_unknown": set(),  # interne, ne pas muter directement
        }
    """
    path = Path(path) if path else DEFAULT_MAPPING_PATH
    cache_key = str(path.resolve())
    if use_cache:
        with _MAPPING_CACHE_LOCK:
            if cache_key in _MAPPING_CACHE:
                return _MAPPING_CACHE[cache_key]

    if not path.exists():
        raise FileNotFoundError(f"Mapping introuvable : {path}")
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    offres_raw = raw.get("offres", {}) or {}
    offres: Dict[str, Dict[str, str]] = {}
    invalid: List[str] = []
    for code, info in offres_raw.items():
        key = str(code).strip().upper()
        if not key:
            continue
        nature = str((info or {}).get("nature", "")).strip()
        societe = str((info or {}).get("societe", "")).strip()
        if nature and not _VALID_NATURE_RE.match(nature):
            invalid.append(f"offres.{code}.nature='{nature}'")
            continue
        if societe and not _VALID_SOCIETE_RE.match(societe):
            invalid.append(f"offres.{code}.societe='{societe}'")
            continue
        offres[key] = {"nature": nature, "societe": societe}
    if invalid:
        raise ValueError(
            "mapping_offres.yml : valeurs invalides (caractères interdits "
            f"pour openpyxl/feuille Excel) : {invalid}"
        )

    a_confirmer = {
        str(k).strip().upper(): v
        for k, v in (raw.get("a_confirmer") or {}).items()
    }

    mapping = {
        "offres": offres,
        "a_confirmer": a_confirmer,
        "origines": [str(o).strip().upper() for o in (raw.get("origines") or [])],
        "natures_avec_onglet_origine": [
            str(n).strip().upper()
            for n in (raw.get("natures_avec_onglet_origine") or [])
        ],
        "statuts": [str(s).strip().upper() for s in (raw.get("statuts") or [])],
        "_warned_unknown": set(),
        "_warn_lock": threading.Lock(),
    }
    if use_cache:
        with _MAPPING_CACHE_LOCK:
            _MAPPING_CACHE[cache_key] = mapping
    return mapping


def clear_mapping_cache() -> None:
    """Vide le cache (utile pour les tests qui modifient le YAML à la volée)."""
    with _MAPPING_CACHE_LOCK:
        _MAPPING_CACHE.clear()


def lookup_offre(mapping: Dict[str, Any], code: Any) -> Tuple[str, str]:
    """Retourne `(nature, societe)` pour un code offre.

    - Strip + upper systématique avant lookup.
    - Si non trouvé : retourne `(code_brut, "?")` et déclenche un warning
      la première fois pour ce code (thread-safe via `_warn_lock`).
    """
    if code is None:
        return ("", "?")
    key = str(code).strip().upper()
    if not key:
        return ("", "?")
    if key in mapping["offres"]:
        info = mapping["offres"][key]
        return (info["nature"], info["societe"])
    # Non trouvé : warn une fois, protégé par lock pour le partage entre threads.
    lock = mapping.get("_warn_lock")
    seen = mapping.setdefault("_warned_unknown", set())
    if lock is not None:
        with lock:
            _maybe_warn_unknown(mapping, key, seen)
    else:
        _maybe_warn_unknown(mapping, key, seen)
    return (key, "?")  # Fallback : nature = code brut, société = "?"


def _maybe_warn_unknown(mapping: Dict[str, Any], key: str, seen: set) -> None:
    if key in seen:
        return
    seen.add(key)
    if key in mapping["a_confirmer"]:
        raison = mapping["a_confirmer"][key].get("raison", "")
        print(f"   [WARN] Offre '{key}' en attente de confirmation : {raison}")
    else:
        print(f"   [WARN] Offre '{key}' absente du mapping_offres.yml.")


# ---------------------------------------------------------------------------
# Enrichissement du DataFrame
# ---------------------------------------------------------------------------

def enrich_dataframe(df: pd.DataFrame, mapping: Dict[str, Any]) -> pd.DataFrame:
    """Ajoute les colonnes `_nature` et `_societe` au DataFrame brut.

    - Strip + upper sur la colonne `Offre` (les codes Oracle peuvent porter
      des espaces de fin, ex. `MESMEN001 `, `MESLAD46  `).
    - Coerce `Nb_dossiers` en int (NaN → 0).
    - Lookup vectorisé via `Series.map(dict)` (~100× plus rapide que la
      boucle Python sur 100k lignes). Les codes inconnus sont collectés
      avant le warning afin d'éviter N appels au lock.
    """
    if df is None or df.empty:
        return df
    out = df.copy()
    if COL_OFFRE in out.columns:
        out[COL_OFFRE] = out[COL_OFFRE].astype(str).str.strip().str.upper()
    else:
        out[COL_OFFRE] = ""

    offres_map = mapping["offres"]
    nature_lookup = {k: v["nature"] for k, v in offres_map.items()}
    societe_lookup = {k: v["societe"] for k, v in offres_map.items()}

    # Lookup vectorisé : codes connus -> nature/societe ; inconnus -> NaN
    mapped_nature = out[COL_OFFRE].map(nature_lookup)
    mapped_societe = out[COL_OFFRE].map(societe_lookup)

    # Codes inconnus : fallback nature = code brut, societe = "?", + warning
    unknown_mask = mapped_nature.isna()
    if unknown_mask.any():
        unknown_codes = sorted(set(out.loc[unknown_mask, COL_OFFRE]))
        # Filtre les codes vides (col absente du source SQL → "")
        unknown_codes = [c for c in unknown_codes if c]
        for code in unknown_codes:
            # Réutilise lookup_offre pour le warning thread-safe (et anti-spam)
            lookup_offre(mapping, code)
        mapped_nature = mapped_nature.where(~unknown_mask, out[COL_OFFRE])
        mapped_societe = mapped_societe.where(~unknown_mask, "?")

    out["_nature"] = mapped_nature.astype(str)
    out["_societe"] = mapped_societe.astype(str)

    if COL_NB in out.columns:
        out[COL_NB] = pd.to_numeric(out[COL_NB], errors="coerce").fillna(0).astype(int)

    return out


# ---------------------------------------------------------------------------
# Builder Excel multi-onglets
# ---------------------------------------------------------------------------

# Natures regroupées hors PREV (pour onglet "Prestations par offre").
NATURES_HORS_PREV = ["MSP", "EFS", "MAEE", "CULTURE", "JA", "MEAE", "MEN",
                      "NUANCE", "MSOL"]

# Natures de la Prévoyance — source unique de vérité (cf. mapping_offres.yml).
# Ordre conservé pour l'onglet "Prévoyance".
NATURES_PREV = ["PREV_INDIV", "PREV_MEAE", "PREV_CULTURE", "PREV_MEN"]

# Mapping label "humain" pour onglet Prévoyance (aligné avec maquette Laurence).
PREV_LABEL = {
    "PREV_INDIV": "Prev Indiv",
    "PREV_MEAE": "PREV MEAE",
    "PREV_CULTURE": "Prev CULTURE",
    "PREV_MEN": "Prev MEN",
}


def _safe_date(s: Any) -> Optional[datetime]:
    try:
        return datetime.strptime(str(s).strip(), "%Y%m%d")
    except (ValueError, TypeError):
        return None


def build_pivot_workbook(
    df_enriched: pd.DataFrame,
    output_path: Path,
    mapping: Dict[str, Any],
) -> Path:
    """Construit le classeur multi-onglets aligné sur la maquette Laurence."""
    statuts = mapping.get("statuts") or ["IN", "PA", "RJ", "VA"]

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        _sheet_tcd1(df_enriched, statuts, writer)
        _sheet_prestations_par_offre(df_enriched, writer)
        _sheet_prevoyance(df_enriched, writer)
        _sheet_societe(df_enriched, "010", statuts,
                        NATURES_HORS_PREV, writer, "Ste 010")
        _sheet_societe(df_enriched, "044", statuts,
                        ["MAEE", "MEAE"], writer, "Ste 044")
        _sheet_societe_msol(df_enriched, statuts, writer)
        _sheet_tcd2(df_enriched, statuts, writer)
        for nature in mapping.get("natures_avec_onglet_origine", []):
            _sheet_nature_origine(df_enriched, nature,
                                    mapping["origines"], writer)
    return output_path


def _sheet_tcd1(df: pd.DataFrame, statuts: List[str], writer) -> None:
    """Pivot par code offre brut × statut, avec colonne Total et Nature."""
    if df is None or df.empty:
        pd.DataFrame({"Statut": ["Aucune donnée"]}).to_excel(writer, sheet_name="TCD1", index=False)
        return
    pivot = df.pivot_table(index=COL_OFFRE, columns=COL_STATUT,
                            values=COL_NB, aggfunc="sum", fill_value=0)
    # Assure que tous les statuts attendus sont présents
    for s in statuts:
        if s not in pivot.columns:
            pivot[s] = 0
    pivot = pivot[statuts]
    pivot["Total général"] = pivot.sum(axis=1)
    # Nature regroupée (lookup mémoizé pour éviter le scan O(N) par offre)
    nature_by_code = (
        df.drop_duplicates(subset=[COL_OFFRE])
          .set_index(COL_OFFRE)["_nature"]
          .to_dict()
    )
    pivot["Nature"] = [nature_by_code.get(code, "") for code in pivot.index]
    pivot.reset_index(inplace=True)
    pivot.to_excel(writer, sheet_name="TCD1", index=False)


def _sheet_prestations_par_offre(df: pd.DataFrame, writer) -> None:
    """Pivot dates × natures (hors PREV)."""
    if df is None or df.empty:
        pd.DataFrame({"Statut": ["Aucune donnée"]}).to_excel(writer, sheet_name="Prestations par offre",
                                 index=False)
        return
    hors_prev = df[~df["_nature"].isin(NATURES_PREV)].copy()
    pivot = hors_prev.pivot_table(index=COL_DATE, columns="_nature",
                                    values=COL_NB, aggfunc="sum", fill_value=0)
    for n in NATURES_HORS_PREV:
        if n not in pivot.columns:
            pivot[n] = 0
    pivot = pivot[NATURES_HORS_PREV]
    pivot["Total"] = pivot.sum(axis=1)
    pivot.reset_index(inplace=True)
    pivot.to_excel(writer, sheet_name="Prestations par offre", index=False)


def _sheet_prevoyance(df: pd.DataFrame, writer) -> None:
    """Pivot dates × natures PREV."""
    if df is None or df.empty:
        pd.DataFrame({"Statut": ["Aucune donnée"]}).to_excel(writer, sheet_name="Prévoyance", index=False)
        return
    only_prev = df[df["_nature"].isin(NATURES_PREV)].copy()
    if only_prev.empty:
        pd.DataFrame({"Statut": ["Aucune donnée pour ce filtre"]}).to_excel(writer, sheet_name="Prévoyance",
                                                index=False)
        return
    pivot = only_prev.pivot_table(index=COL_DATE, columns="_nature",
                                    values=COL_NB, aggfunc="sum", fill_value=0)
    cols_to_keep = [c for c in ["PREV_INDIV", "PREV_MEAE", "PREV_CULTURE",
                                 "PREV_MEN"] if c in pivot.columns]
    pivot = pivot[cols_to_keep]
    pivot.rename(columns=PREV_LABEL, inplace=True)
    pivot["Total"] = pivot.sum(axis=1)
    pivot.reset_index(inplace=True)
    pivot.to_excel(writer, sheet_name="Prévoyance", index=False)


def _sheet_societe(df: pd.DataFrame, societe: str, statuts: List[str],
                    natures_attendues: List[str], writer, sheet_name: str) -> None:
    """Pivot dates × (nature-statut) restreint à une société."""
    if df is None or df.empty:
        pd.DataFrame({"Statut": ["Aucune donnée"]}).to_excel(writer, sheet_name=sheet_name, index=False)
        return
    sub = df[df["_societe"] == societe].copy()
    if sub.empty:
        pd.DataFrame({"Statut": ["Aucune donnée pour ce filtre"]}).to_excel(writer, sheet_name=sheet_name,
                                                index=False)
        return
    sub["_nature_statut"] = sub["_nature"] + "-" + sub[COL_STATUT]
    pivot = sub.pivot_table(index=COL_DATE, columns="_nature_statut",
                              values=COL_NB, aggfunc="sum", fill_value=0)
    # Colonnes attendues : nature × statut, dans l'ordre du mapping
    expected = [f"{n}-{s}" for n in natures_attendues for s in statuts]
    for c in expected:
        if c not in pivot.columns:
            pivot[c] = 0
    cols = [c for c in expected if c in pivot.columns]
    pivot = pivot[cols]
    pivot[f"total {societe}"] = pivot.sum(axis=1)
    pivot.reset_index(inplace=True)
    pivot.to_excel(writer, sheet_name=sheet_name, index=False)


def _sheet_societe_msol(df: pd.DataFrame, statuts: List[str], writer) -> None:
    """Société 052 : cumul de TOUTES les offres MSOL par statut."""
    if df is None or df.empty:
        pd.DataFrame({"Statut": ["Aucune donnée"]}).to_excel(writer, sheet_name="Ste 052", index=False)
        return
    sub = df[df["_nature"] == "MSOL"].copy()
    if sub.empty:
        pd.DataFrame({"Statut": ["Aucune donnée pour ce filtre"]}).to_excel(writer, sheet_name="Ste 052",
                                                index=False)
        return
    pivot = sub.pivot_table(index=COL_DATE, columns=COL_STATUT,
                              values=COL_NB, aggfunc="sum", fill_value=0)
    for s in statuts:
        if s not in pivot.columns:
            pivot[s] = 0
    pivot = pivot[statuts]
    pivot.columns = [f"Msol-{s}" for s in statuts]
    pivot.reset_index(inplace=True)
    pivot.to_excel(writer, sheet_name="Ste 052", index=False)


def _sheet_tcd2(df: pd.DataFrame, statuts: List[str], writer) -> None:
    """Pivot origine × offre × statut, avec sous-totaux."""
    if df is None or df.empty:
        pd.DataFrame({"Statut": ["Aucune donnée"]}).to_excel(writer, sheet_name="TCD2", index=False)
        return
    pivot = df.pivot_table(index=[COL_ORIGINE, COL_OFFRE],
                              columns=COL_STATUT, values=COL_NB,
                              aggfunc="sum", fill_value=0)
    for s in statuts:
        if s not in pivot.columns:
            pivot[s] = 0
    pivot = pivot[statuts]
    pivot["Total général"] = pivot.sum(axis=1)
    pivot.reset_index(inplace=True)
    pivot.to_excel(writer, sheet_name="TCD2", index=False)


def _sheet_nature_origine(df: pd.DataFrame, nature: str,
                            origines: List[str], writer) -> None:
    """Onglet `<NATURE>_ORIGINE` : dates × origines, restreint à une nature."""
    raw_name = f"{nature}_ORIGINE"
    sheet_name = raw_name[:31]
    if len(raw_name) > 31:
        # Excel limite à 31 chars : risque de collision si plusieurs natures
        # partagent les 31 premiers chars. Très peu probable mais on logue.
        print(f"   [WARN] Nom d'onglet tronqué : '{raw_name}' → '{sheet_name}'")
    if df is None or df.empty:
        pd.DataFrame({"Statut": ["Aucune donnée"]}).to_excel(writer, sheet_name=sheet_name, index=False)
        return
    sub = df[df["_nature"] == nature].copy()
    if sub.empty:
        pd.DataFrame({"Statut": ["Aucune donnée pour ce filtre"]}).to_excel(writer, sheet_name=sheet_name,
                                                index=False)
        return
    pivot = sub.pivot_table(index=COL_DATE, columns=COL_ORIGINE,
                              values=COL_NB, aggfunc="sum", fill_value=0)
    for o in origines:
        if o not in pivot.columns:
            pivot[o] = 0
    pivot = pivot[origines]
    pivot.reset_index(inplace=True)
    pivot.to_excel(writer, sheet_name=sheet_name, index=False)


# ---------------------------------------------------------------------------
# Reader JSON (utilisé par 02_calcul_kpi.py)
# ---------------------------------------------------------------------------

def _is_placeholder_sheet(df: pd.DataFrame) -> bool:
    """Détecte les feuilles placeholder écrites par `build_pivot_workbook`
    quand aucune donnée n'est disponible (`pd.DataFrame({"Statut": ["Aucune donnée"]})`)."""
    if df is None or df.empty:
        return True
    if list(df.columns) == ["Statut"] and len(df) == 1:
        val = str(df.iloc[0]["Statut"]).strip().lower()
        return val.startswith("aucune donn")
    return False


def read_pivot_for_json(xlsx_path: Path) -> Dict[str, Any]:
    """Lit le classeur pivot et synthétise les chiffres pour le JSON.

    Statuts possibles :
      - `Absent`  : fichier introuvable.
      - `Erreur`  : lecture KO (xlsx corrompu, openpyxl en échec…).
      - `Vide`    : fichier présent mais l'onglet "Prestations par offre"
                    est un placeholder (post-process appelé sur DF vide).
      - `OK`      : données présentes.
    """
    if not xlsx_path or not xlsx_path.exists():
        return {"Statut": "Absent", "Source": str(xlsx_path)}
    try:
        sheets = pd.read_excel(xlsx_path, sheet_name=None, engine="openpyxl")
    except Exception as exc:  # noqa: BLE001
        return {"Statut": "Erreur", "Motif": str(exc), "Source": str(xlsx_path)}

    sheet_prest = sheets.get("Prestations par offre")
    if sheet_prest is None or _is_placeholder_sheet(sheet_prest):
        return {
            "Statut": "Vide",
            "Source": str(xlsx_path),
            "Motif": ("Onglet 'Prestations par offre' absent ou placeholder "
                      "(post-process applique sur DataFrame vide)."),
        }

    quotidien: Dict[str, Any] = {}
    cols_nat = [c for c in sheet_prest.columns if c in NATURES_HORS_PREV]
    if COL_DATE in sheet_prest.columns and len(sheet_prest) > 0:
        last_row = sheet_prest.iloc[-1]
        quotidien["Date"] = str(last_row.get(COL_DATE, ""))
        quotidien["Par_Nature"] = {
            c: int(last_row.get(c, 0) or 0) for c in cols_nat
        }
        quotidien["Total_Dossiers"] = int(last_row.get("Total", 0) or 0)

    # Métrique diagnostique : dossiers sans offre (psdecodoff NULL côté SQL
    # ⇒ pseudo-offre "__SANS_OFFRE__" via NVL — cf. audit C2). Tant que les
    # branches UNION 2/3 ne sont pas livrées, cette valeur signale au métier
    # le volume perdu en V1.
    tcd1 = sheets.get("TCD1")
    if tcd1 is not None and not _is_placeholder_sheet(tcd1):
        col_offre_tcd1 = next((c for c in tcd1.columns
                                if str(c).strip() == COL_OFFRE), None)
        col_total_tcd1 = next((c for c in tcd1.columns
                                if str(c).strip() == "Total général"), None)
        if col_offre_tcd1 and col_total_tcd1:
            mask_sans_offre = (tcd1[col_offre_tcd1].astype(str).str.strip()
                                == "__SANS_OFFRE__")
            try:
                nb_sans_offre = int(pd.to_numeric(
                    tcd1.loc[mask_sans_offre, col_total_tcd1], errors="coerce"
                ).fillna(0).sum())
            except Exception:  # noqa: BLE001
                nb_sans_offre = 0
            if nb_sans_offre > 0:
                quotidien["Dossiers_Sans_Offre"] = {
                    "Nombre": nb_sans_offre,
                    "Diagnostic": (
                        "Branches UNION 2 (acofcodoff) et 3 (sans offre) du SQL "
                        "non livrées : ces dossiers sont remontés via NVL pour "
                        "visibilité, mais leur ventilation par offre/société "
                        "est inconnue. À corriger avec le SQL complet Laurence."
                    ),
                }

    # Par_Societe : à partir des onglets Ste 010, Ste 044, Ste 052.
    # Si un onglet est un placeholder ou ne porte pas de colonne "total",
    # on l'omet du dict au lieu d'écrire 0 (faux positif "OK avec 0 dossier").
    par_societe: Dict[str, int] = {}
    for soc, sheet_name in [("010", "Ste 010"), ("044", "Ste 044"),
                             ("052", "Ste 052")]:
        sh = sheets.get(sheet_name)
        if sh is None or _is_placeholder_sheet(sh):
            continue
        total_col = next((c for c in sh.columns
                          if str(c).lower().startswith("total")), None)
        if total_col is None:
            continue
        last_row = sh.iloc[-1]
        try:
            par_societe[soc] = int(last_row.get(total_col, 0) or 0)
        except (ValueError, TypeError):
            continue
    if par_societe:
        quotidien["Par_Societe"] = par_societe

    return {
        "Statut": "OK",
        "Source": str(xlsx_path),
        "Quotidien": quotidien,
        "Stock_Hebdo": {
            "Statut": "Non_Disponible",
            "Motif": "S_Prestations_stock_par_offre.sql en attente de Laurence.",
        },
    }


def find_latest_pivot(output_sql_dir: Path,
                       prefix_ddmmyyyy: Optional[str] = None,
                       allow_cross_date_fallback: bool = False
                       ) -> Optional[Path]:
    """Localise le dernier classeur pivot produit.

    Stratégie :
      - Si `prefix_ddmmyyyy` (DDMMYYYY validé regex) fourni → cherche
        UNIQUEMENT dans `output_sql_dir/DDMMYYYY/`. Si aucun fichier ne
        s'y trouve, retourne `None` par défaut (fail-loud).
      - Si `allow_cross_date_fallback=True`, retombe sur un scan récursif
        en émettant un WARN explicite mentionnant la date du fichier
        retourné (évite la confusion "j'ai cru lire le 14/05 alors que
        c'était le 13/05").
      - Sans préfixe : scan récursif, retourne le plus récent par mtime.

    Sécurité : `prefix_ddmmyyyy` doit matcher `\\d{8}` strict (défense en
    profondeur contre un path traversal si l'API est appelée par un consumer
    non-validé). Lève `ValueError` sinon.
    """
    if prefix_ddmmyyyy is not None and not _VALID_PREFIX_DDMMYYYY.match(
            str(prefix_ddmmyyyy)):
        raise ValueError(
            f"prefix_ddmmyyyy doit etre une chaine DDMMYYYY (8 chiffres), "
            f"reçu: {prefix_ddmmyyyy!r}"
        )
    if not output_sql_dir.exists():
        return None

    if prefix_ddmmyyyy:
        sub = output_sql_dir / prefix_ddmmyyyy
        if sub.exists():
            in_sub = list(sub.glob("*MDG_Prestations_par_offre*Pivot*.xlsx"))
            if in_sub:
                return max(in_sub, key=lambda p: p.stat().st_mtime)
        if not allow_cross_date_fallback:
            return None
        # Fallback avec WARN explicite
        all_pivots = list(output_sql_dir.rglob(
            "*MDG_Prestations_par_offre*Pivot*.xlsx"))
        if not all_pivots:
            return None
        chosen = max(all_pivots, key=lambda p: p.stat().st_mtime)
        chosen_date = chosen.parent.name if chosen.parent.parent == output_sql_dir else "?"
        print(f"   [WARN] Aucun pivot pour {prefix_ddmmyyyy}, fallback sur "
              f"{chosen.name} (date dossier: {chosen_date}).")
        return chosen

    # Sans préfixe : scan global
    candidates = list(output_sql_dir.rglob(
        "*MDG_Prestations_par_offre*Pivot*.xlsx"))
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)
