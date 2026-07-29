# -*- coding: utf-8 -*-
"""
09_taux_noemisation.py — Taux de noémisation par client/offre
==============================================================

Entrée  : Input_Data/{PREFIX}_Taux_Noemie*.xlsx   (PREFIX = DDMMYYYY)
          extraction Power BI — rapport "Taux de Noémisation"
Sortie  : Output/{PREFIX}_Taux_Noemie*.xlsx
          (copie du fichier source, enrichie)

Deux phases :
  1. Complétion de la feuille source : colonnes Actifs2 / Non actifs2 /
     Non_noémisable2 / Taux / Offres / Individuel ou Collectif / ordre
     (le bloc « vert » saisi à la main aujourd'hui) + les 3 lignes de totaux.
  2. Feuille 'Taux_Noémisation' : récapitulatif à plat, une ligne par
     client/offre consolidé, trié par ordre d'affichage (principe Feuil7).

Taux = Actifs / (Actifs + non Actifs + Non_noémisable)

Les 3 indicateurs remontés au dashboard sont les totaux de la phase 2 :
  taux total · taux Collectif (I/C = C) · taux Individuel (I/C = I)

Usage :
  python 09_taux_noemisation.py
"""

from __future__ import annotations

import re
import shutil
import sys
import unicodedata
import warnings
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from openpyxl import load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

# ─── CONFIG ───────────────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR   = SCRIPT_DIR if (SCRIPT_DIR / "Input_Data").exists() else SCRIPT_DIR.parent
INPUT_DIR  = BASE_DIR / "Input_Data"
OUTPUT_DIR = BASE_DIR / "Output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
INPUT_DIR.mkdir(parents=True, exist_ok=True)

# Nommage convenu avec PILLON Laurence : jjmmaaaa_Taux_Noémie.xlsx
# (accent et séparateur libres — "Noemie", "Noémie", "Taux Noemie"…)
NOEMIE_FILENAME_RE = re.compile(r"^(\d{8})_Taux[ _]?No[eé]mie.*\.xlsx$", re.IGNORECASE)
RECAP_SHEET        = "Taux_Noémisation"

# Colonnes de l'extraction brute (Power BI) — repérées par libellé, jamais par
# lettre : la position dérive dès qu'une colonne est ajoutée en amont.
COL_LIBCRT = "libcrt"
COL_ACTIFS = "actifs"
COL_NONACT = "non actifs"
COL_NONNOE = "non noemisable"
COL_PCTACT = "% actifs"

# Colonnes ajoutées par ce script (bloc « vert »).
COL_ACTIFS2 = "actifs2"
COL_NONACT2 = "non actifs2"
COL_NONNOE2 = "non noemisable2"
COL_TAUX    = "taux"
COL_OFFRES  = "offres"
COL_INDCOL  = "individuel ou collectif"
COL_ORDRE   = "ordre"

OUTPUT_HEADERS = [
    ("Actifs2",           COL_ACTIFS2),
    ("Non actifs2",       COL_NONACT2),
    ("Non_noémisable2",   COL_NONNOE2),
    ("Taux",              COL_TAUX),
    ("Offres",            COL_OFFRES),
    ("Individuel ou Collectif", COL_INDCOL),
    ("ordre",             COL_ORDRE),
]

# ─── CONFIG REGROUPEMENTS (fallback) ──────────────────────────────────────────
# Utilisée UNIQUEMENT si l'extraction ne contient pas déjà les colonnes
# Offres / Individuel ou Collectif / ordre (cas d'un export Power BI brut).
# Format : "LIBCRT porteur" → (ordre, libellé du regroupement, "C" | "I")
# Renseignée automatiquement par le script au 1er passage sur un fichier déjà
# complété (cf. bloc [CONFIG] affiché en fin d'exécution) — copier-coller ici.
NOEMIE_GROUPES: Dict[str, Tuple[Optional[int], str, str]] = {}

# Rattachement Individuel/Collectif des libcrt membres d'un regroupement
# (les membres n'ont pas de ligne de synthèse propre mais comptent dans les
# totaux C/I). Renseignée en même temps que NOEMIE_GROUPES.
NOEMIE_MEMBRES: Dict[str, str] = {}

# Le libellé de la colonne "Offres" est DESCRIPTIF, pas une clé technique :
# il contient des coquilles (ex. "EFS SANTE" pour le libcrt "EFF SANTE") car
# Laurence calcule ses sommes par référence de cellule, pas par nom.
# Chaque membre non résolu est signalé en [WARN] ; on l'aligne ici.
ALIAS_MEMBRES: Dict[str, str] = {
    "EFS SANTE":      "EFF SANTE",
    "EFS SANTE BRED": "EFF SANTE BRED",
}

# ─── FORMATS / STYLES (alignés sur 05_generation_tcd.py) ──────────────────────

FMT_INT = "#,##0"
FMT_PCT = "0.00%"

_HEADER_FILL = PatternFill("solid", fgColor="1F497D")
_HEADER_FONT = Font(bold=True, color="FFFFFF", size=10)
_GROUP_FILL  = PatternFill("solid", fgColor="E2EFDA")   # vert clair (bloc Laurence)
_TOTAL_FILL  = PatternFill("solid", fgColor="DCE6F1")
_TOTAL_FONT  = Font(bold=True, size=10)
_GRAND_FILL  = PatternFill("solid", fgColor="FFF2CC")
_GRAND_FONT  = Font(bold=True, size=10, color="7F6000")
_DATA_FONT   = Font(size=10)
_CENTER      = Alignment(horizontal="center", vertical="center")
_LEFT        = Alignment(horizontal="left",   vertical="center")
_TAB_COLOR   = "70AD47"

# ─── HELPERS ──────────────────────────────────────────────────────────────────

def _norm(value) -> str:
    """Normalise un libellé pour comparaison : sans accent, minuscule, espaces
    simples. '_' est traité comme un espace ('Non_noémisable' → 'non noemisable')."""
    if value is None:
        return ""
    txt = unicodedata.normalize("NFKD", str(value))
    txt = "".join(c for c in txt if not unicodedata.combining(c))
    txt = txt.replace("_", " ").replace("\xa0", " ").lower()
    return re.sub(r"\s+", " ", txt).strip()


def _num(value) -> float:
    """Convertit une cellule Excel en float ; cellule vide/texte → 0.0."""
    if value is None or isinstance(value, str) and not value.strip():
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _taux(actifs: float, non_actifs: float, non_noem: float) -> Optional[float]:
    """Taux de noémisation. None si le dénominateur est nul (offre sans contrat)."""
    denom = actifs + non_actifs + non_noem
    return actifs / denom if denom else None


def _split_membres(label: str) -> List[str]:
    """Éclate un libellé de regroupement ('A + B+C') en libcrt membres."""
    return [m.strip() for m in str(label).split("+") if m.strip()]

# ─── I/O ──────────────────────────────────────────────────────────────────────

def find_input_file() -> Optional[Path]:
    """Localise l'extraction Taux_Noémie la plus récente. None si absente
    (cas fonctionnel accepté : l'étape est simplement ignorée)."""
    matches = [f for f in INPUT_DIR.iterdir()
               if f.is_file() and not f.name.startswith("~$")
               and NOEMIE_FILENAME_RE.match(f.name)]
    if not matches:
        return None
    if len(matches) > 1:
        matches.sort(key=lambda f: NOEMIE_FILENAME_RE.match(f.name).group(1), reverse=True)
        warnings.warn(
            f"[WARN] Plusieurs extractions Taux_Noémie détectées : "
            f"{[f.name for f in matches]}\n  → Utilisation de {matches[0].name}",
            stacklevel=2,
        )
    return matches[0]


def find_header_row(ws) -> int:
    """Ligne d'en-tête = 1ʳᵉ ligne contenant 'libcrt'.

    L'extraction Power BI place un rappel des filtres en ligne 1 et une ligne
    vide en ligne 2 ; l'en-tête est donc en ligne 3 — mais rien ne garantit
    que ce cadrage soit stable d'un mois sur l'autre.
    """
    for row in ws.iter_rows(min_row=1, max_row=min(ws.max_row, 30)):
        for cell in row:
            if _norm(cell.value) == COL_LIBCRT:
                return cell.row
    raise ValueError(
        f"En-tête introuvable dans '{ws.title}' : aucune cellule '{COL_LIBCRT}' "
        f"dans les 30 premières lignes.\n"
        f"  → Vérifier que l'extraction Power BI n'a pas changé de structure."
    )


def map_columns(ws, header_row: int) -> Dict[str, int]:
    """Libellé normalisé → index de colonne (1-based)."""
    mapping: Dict[str, int] = {}
    for cell in ws[header_row]:
        key = _norm(cell.value)
        if key and key not in mapping:
            mapping[key] = cell.column
    return mapping


def read_data_rows(ws, header_row: int, cols: Dict[str, int]) -> List[dict]:
    """Lit les lignes de données jusqu'aux totaux (exclus).

    Fin de bloc = 1ʳᵉ ligne dont le libcrt est vide ou commence par 'total'.
    Les lignes de totaux existantes sont réécrites par ce script.
    """
    c_lib = cols[COL_LIBCRT]
    rows: List[dict] = []
    for r in range(header_row + 1, ws.max_row + 1):
        libcrt = ws.cell(row=r, column=c_lib).value
        norm = _norm(libcrt)
        if not norm or norm.startswith("total"):
            break
        rows.append({
            "row":     r,
            "libcrt":  str(libcrt).strip(),
            "actifs":  _num(ws.cell(row=r, column=cols[COL_ACTIFS]).value),
            "non_act": _num(ws.cell(row=r, column=cols[COL_NONACT]).value),
            "non_noe": _num(ws.cell(row=r, column=cols[COL_NONNOE]).value),
            # Mapping éventuellement déjà saisi dans le fichier
            "offres":  ws.cell(row=r, column=cols[COL_OFFRES]).value if COL_OFFRES in cols else None,
            "indcol":  ws.cell(row=r, column=cols[COL_INDCOL]).value if COL_INDCOL in cols else None,
            "ordre":   ws.cell(row=r, column=cols[COL_ORDRE]).value  if COL_ORDRE  in cols else None,
        })
    return rows

# ─── REGROUPEMENTS ────────────────────────────────────────────────────────────

def build_groupes(rows: List[dict]) -> Tuple[Dict[str, dict], Dict[str, str]]:
    """Construit les regroupements client/offre.

    Source du mapping, par ordre de priorité :
      1. les colonnes Offres / Individuel ou Collectif / ordre du fichier
         (extraction déjà complétée par Laurence) ;
      2. la config NOEMIE_GROUPES / NOEMIE_MEMBRES (extraction brute A–E).

    Retourne (groupes, indcol_par_libcrt) où groupes est indexé par libcrt
    porteur : {ordre, label, indcol, membres:[libcrt…]}.
    """
    par_libcrt = {r["libcrt"]: r for r in rows}
    index_norm = {_norm(lib): lib for lib in par_libcrt}

    def resoudre(token: str) -> Optional[str]:
        """Token du libellé 'Offres' → libcrt réel (via alias si nécessaire)."""
        cible = ALIAS_MEMBRES.get(token.strip().upper(), token.strip())
        return index_norm.get(_norm(cible))

    du_fichier = any(r["offres"] for r in rows)
    groupes: Dict[str, dict] = {}
    indcol: Dict[str, str] = {}

    if du_fichier:
        print("  [MAP]     Mapping lu depuis le fichier (colonnes Offres / "
              "Individuel ou Collectif / ordre)")
        for r in rows:
            if r["indcol"]:
                indcol[r["libcrt"]] = str(r["indcol"]).strip().upper()[:1]
            if not r["offres"]:
                continue                      # ligne membre : pas de synthèse propre
            tokens  = _split_membres(r["offres"])
            membres, orphelins = [], []
            for tok in tokens:
                cible = resoudre(tok)
                (membres if cible else orphelins).append(cible or tok)
            if r["libcrt"] not in membres:
                # Porteuse absente de son propre libellé (coquille) → réintégrée
                membres.insert(0, r["libcrt"])
            if orphelins:
                print(f"  [WARN]    Regroupement '{r['offres']}' : membre(s) non "
                      f"résolu(s) {orphelins} — ignoré(s) dans la somme.\n"
                      f"            → Ajouter l'alias dans ALIAS_MEMBRES si c'est "
                      f"une coquille de libellé.")
            ordre = int(r["ordre"]) if isinstance(r["ordre"], (int, float)) else None
            groupes[r["libcrt"]] = {
                "ordre":   ordre,
                "label":   str(r["offres"]).strip(),
                "indcol":  str(r["indcol"]).strip().upper()[:1] if r["indcol"] else "",
                "membres": list(dict.fromkeys(membres)),
            }
    else:
        print("  [MAP]     Mapping lu depuis la config NOEMIE_GROUPES "
              "(extraction brute sans bloc vert)")
        if not NOEMIE_GROUPES:
            raise ValueError(
                "L'extraction ne contient ni colonne 'Offres' ni config "
                "NOEMIE_GROUPES renseignée.\n"
                "  → Lancer d'abord le script sur un fichier complété par "
                "Laurence, puis recopier le bloc [CONFIG] affiché en fin "
                "d'exécution dans NOEMIE_GROUPES / NOEMIE_MEMBRES."
            )
        indcol.update(NOEMIE_MEMBRES)
        for porteur, (ordre, label, ic) in NOEMIE_GROUPES.items():
            if porteur not in par_libcrt:
                print(f"  [WARN]    Offre porteuse '{porteur}' absente du flux "
                      f"— regroupement ignoré ce mois-ci.")
                continue
            membres = [c for c in (resoudre(t) for t in _split_membres(label)) if c]
            if porteur not in membres:
                membres.insert(0, porteur)
            indcol[porteur] = ic
            for m in membres:
                indcol.setdefault(m, ic)
            groupes[porteur] = {"ordre": ordre, "label": label, "indcol": ic,
                                "membres": list(dict.fromkeys(membres))}

    # Contrôle de partition : chaque libcrt doit appartenir à exactement un
    # groupe, sinon les totaux double-comptent (ou perdent) des contrats.
    vus: Dict[str, str] = {}
    for porteur, g in groupes.items():
        for m in g["membres"]:
            if m in vus:
                print(f"  [WARN]    '{m}' rattaché à la fois à '{vus[m]}' et "
                      f"'{porteur}' — double comptage dans les totaux.")
            vus[m] = porteur
    orphelins = [lib for lib in par_libcrt if lib not in vus]
    if orphelins:
        print(f"  [WARN]    {len(orphelins)} libcrt hors regroupement : "
              f"{orphelins}\n            → Absent(s) du récapitulatif mais "
              f"compté(s) dans les totaux généraux.")

    return groupes, indcol


def agreger(groupes: Dict[str, dict], rows: List[dict]) -> Dict[str, dict]:
    """Somme Actifs / non Actifs / Non_noémisable sur les membres de chaque groupe."""
    par_libcrt = {r["libcrt"]: r for r in rows}
    for porteur, g in groupes.items():
        g["actifs"]  = sum(par_libcrt[m]["actifs"]  for m in g["membres"] if m in par_libcrt)
        g["non_act"] = sum(par_libcrt[m]["non_act"] for m in g["membres"] if m in par_libcrt)
        g["non_noe"] = sum(par_libcrt[m]["non_noe"] for m in g["membres"] if m in par_libcrt)
        g["taux"]    = _taux(g["actifs"], g["non_act"], g["non_noe"])
    return groupes


def _tri_key(porteur: str, g: dict):
    """Tri par ordre croissant ; groupes sans ordre relégués en fin de bloc."""
    return (g["ordre"] is None, g["ordre"] or 0, porteur)


def totaux(rows: List[dict], indcol: Dict[str, str]) -> Dict[str, dict]:
    """Totaux général / Collectif / Individuel.

    Calculés sur les lignes BRUTES (une par libcrt), jamais sur les agrégats
    de groupe : chaque libcrt n'est compté qu'une fois, quel que soit son
    rattachement.
    """
    def bloc(sel: List[dict]) -> dict:
        a  = sum(r["actifs"]  for r in sel)
        na = sum(r["non_act"] for r in sel)
        nn = sum(r["non_noe"] for r in sel)
        return {"actifs": a, "non_act": na, "non_noe": nn, "taux": _taux(a, na, nn)}

    tot = {
        "Total":            bloc(rows),
        "Total Collectif":  bloc([r for r in rows if indcol.get(r["libcrt"]) == "C"]),
        "Total Individuel": bloc([r for r in rows if indcol.get(r["libcrt"]) == "I"]),
    }

    # Contrôle : Total doit valoir Collectif + Individuel. Un écart signale un
    # libcrt sans rattachement I/C — invisible sur le taux affiché à 2 décimales,
    # d'où l'alerte explicite.
    sans_rattachement = [r["libcrt"] for r in rows
                         if indcol.get(r["libcrt"]) not in ("C", "I")]
    if sans_rattachement:
        print(f"  [WARN]    {len(sans_rattachement)} libcrt sans 'Individuel ou "
              f"Collectif' : {sans_rattachement}\n"
              f"            → Comptés dans le Total mais ni en C ni en I.")
    for cle in ("actifs", "non_act", "non_noe"):
        ecart = tot["Total"][cle] - (tot["Total Collectif"][cle]
                                     + tot["Total Individuel"][cle])
        if abs(ecart) > 1e-6:
            print(f"  [WARN]    Incohérence '{cle}' : Total "
                  f"({tot['Total'][cle]:,.0f}) != Collectif + Individuel "
                  f"({tot['Total Collectif'][cle] + tot['Total Individuel'][cle]:,.0f}) "
                  f"— écart {ecart:+,.0f}".replace(",", " "))
    return tot

# ─── PHASE 1 : COMPLÉTION DE LA FEUILLE SOURCE ────────────────────────────────

def ensure_output_columns(ws, header_row: int, cols: Dict[str, int]) -> Dict[str, int]:
    """Garantit la présence des colonnes du bloc vert ; les crée à la suite
    de la dernière colonne renseignée si elles manquent."""
    next_col = max(cols.values()) + 1 if cols else 1
    for libelle, key in OUTPUT_HEADERS:
        if key in cols:
            continue
        cell = ws.cell(row=header_row, column=next_col, value=libelle)
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
        cell.alignment = _CENTER
        cols[key] = next_col
        next_col += 1
    return cols


def write_phase1(ws, header_row: int, cols: Dict[str, int], rows: List[dict],
                 groupes: Dict[str, dict], indcol: Dict[str, str],
                 tot: Dict[str, dict]) -> int:
    """Écrit le bloc vert sur chaque ligne + les 3 lignes de totaux.

    Convention reprise du fichier de Laurence : les colonnes agrégées ne sont
    renseignées que sur la ligne porteuse d'un regroupement multi-offres ; une
    offre isolée conserve son seul taux ligne à ligne.
    """
    def put(r: int, key: str, value, fmt: Optional[str] = None, center=True):
        cell = ws.cell(row=r, column=cols[key], value=value)
        cell.font = _DATA_FONT
        cell.fill = _GROUP_FILL
        cell.alignment = _CENTER if center else _LEFT
        if fmt:
            cell.number_format = fmt
        return cell

    for data in rows:
        r = data["row"]
        g = groupes.get(data["libcrt"])
        put(r, COL_INDCOL, indcol.get(data["libcrt"], ""))
        if g is None:
            continue                        # ligne membre : agrégée chez sa porteuse
        if len(g["membres"]) > 1:
            put(r, COL_ACTIFS2, g["actifs"],  FMT_INT)
            put(r, COL_NONACT2, g["non_act"], FMT_INT)
            put(r, COL_NONNOE2, g["non_noe"], FMT_INT)
        put(r, COL_TAUX,   g["taux"], FMT_PCT)
        put(r, COL_OFFRES, g["label"], center=False)
        if g["ordre"] is not None:
            put(r, COL_ORDRE, g["ordre"])

    # ── Lignes de totaux, juste sous le bloc de données ──────────────────────
    first_total = (rows[-1]["row"] + 1) if rows else header_row + 1
    for i, libelle in enumerate(["Total", "Total Collectif", "Total Individuel"]):
        r, t = first_total + i, tot[libelle]
        grand = (libelle == "Total")
        fill  = _GRAND_FILL if grand else _TOTAL_FILL
        font  = _GRAND_FONT if grand else _TOTAL_FONT
        valeurs = [(cols[COL_LIBCRT], libelle,      None),
                   (cols[COL_ACTIFS], t["actifs"],  FMT_INT),
                   (cols[COL_NONACT], t["non_act"], FMT_INT),
                   (cols[COL_NONNOE], t["non_noe"], FMT_INT)]
        if COL_PCTACT in cols:
            valeurs.append((cols[COL_PCTACT], t["taux"], FMT_PCT))
        for col, value, fmt in valeurs:
            cell = ws.cell(row=r, column=col, value=value)
            cell.font, cell.fill = font, fill
            cell.alignment = _LEFT if col == cols[COL_LIBCRT] else _CENTER
            if fmt:
                cell.number_format = fmt
    return first_total

# ─── PHASE 2 : FEUILLE RÉCAPITULATIVE ─────────────────────────────────────────

def write_recap(wb, groupes: Dict[str, dict], tot: Dict[str, dict]) -> None:
    """Feuille 'Taux_Noémisation' — une ligne par client/offre consolidé.

    Structure calquée sur Feuil7 du TCD : ordre d'affichage, libellé du
    regroupement, valeurs agrégées, taux, rattachement I/C.
    """
    if RECAP_SHEET in wb.sheetnames:
        del wb[RECAP_SHEET]
    ws = wb.create_sheet(RECAP_SHEET)
    ws.sheet_properties.tabColor = _TAB_COLOR

    entetes = ["ordre", "Client / Offre", "Actifs", "non Actifs",
               "Non_noémisable", "Taux de noémisation", "Individuel ou Collectif"]
    for i, libelle in enumerate(entetes, start=1):
        cell = ws.cell(row=1, column=i, value=libelle)
        cell.fill, cell.font, cell.alignment = _HEADER_FILL, _HEADER_FONT, _CENTER

    ordonnes = sorted(groupes.items(), key=lambda kv: _tri_key(*kv))
    for i, (_porteur, g) in enumerate(ordonnes, start=2):
        valeurs = [(1, g["ordre"], None), (2, g["label"], None),
                   (3, g["actifs"], FMT_INT), (4, g["non_act"], FMT_INT),
                   (5, g["non_noe"], FMT_INT), (6, g["taux"], FMT_PCT),
                   (7, g["indcol"], None)]
        for col, value, fmt in valeurs:
            cell = ws.cell(row=i, column=col, value=value)
            cell.font = _DATA_FONT
            cell.alignment = _LEFT if col == 2 else _CENTER
            if fmt:
                cell.number_format = fmt

    # Totaux : ce sont les 3 indicateurs remontés au dashboard, donc présents
    # ici même s'ils figurent déjà en bas de la feuille source.
    start = len(ordonnes) + 3
    for i, libelle in enumerate(["Total", "Total Collectif", "Total Individuel"]):
        r, t  = start + i, tot[libelle]
        grand = (libelle == "Total")
        fill  = _GRAND_FILL if grand else _TOTAL_FILL
        font  = _GRAND_FONT if grand else _TOTAL_FONT
        valeurs = [(2, libelle, None), (3, t["actifs"], FMT_INT),
                   (4, t["non_act"], FMT_INT), (5, t["non_noe"], FMT_INT),
                   (6, t["taux"], FMT_PCT)]
        for col, value, fmt in valeurs:
            cell = ws.cell(row=r, column=col, value=value)
            cell.font, cell.fill = font, fill
            cell.alignment = _LEFT if col == 2 else _CENTER
            if fmt:
                cell.number_format = fmt

    ws.freeze_panes = "A2"
    for i, _ in enumerate(entetes, start=1):
        largeur = max((len(str(ws.cell(row=r, column=i).value or ""))
                       for r in range(1, ws.max_row + 1)), default=10)
        ws.column_dimensions[get_column_letter(i)].width = min(max(largeur + 2, 10), 60)
    print(f"  [RECAP]   {len(ordonnes)} clients/offres consolidés")


def dump_config(groupes: Dict[str, dict], indcol: Dict[str, str]) -> None:
    """Affiche le mapping au format config, à recopier dans NOEMIE_GROUPES /
    NOEMIE_MEMBRES pour traiter ensuite des extractions brutes (colonnes A–E)."""
    # Console Windows en cp1252 : pas de caractère semi-graphique dans les print.
    print("\n" + "-" * 60)
    print("[CONFIG]  Mapping figé — à recopier dans ce script si les prochaines")
    print("          extractions arrivent sans le bloc vert :\n")
    print("NOEMIE_GROUPES = {")
    for porteur, g in sorted(groupes.items(), key=lambda kv: _tri_key(*kv)):
        print(f'    {porteur!r}: ({g["ordre"]}, {g["label"]!r}, {g["indcol"]!r}),')
    print("}\n")
    print("NOEMIE_MEMBRES = {")
    for lib, ic in sorted(indcol.items()):
        if lib not in groupes:
            print(f"    {lib!r}: {ic!r},")
    print("}")
    print("-" * 60)

# ─── MAIN ─────────────────────────────────────────────────────────────────────

def process(input_file: Path) -> Path:
    prefix      = NOEMIE_FILENAME_RE.match(input_file.name).group(1)
    output_file = OUTPUT_DIR / input_file.name

    print(f"\n[INPUT]   {input_file.name}  (préfixe: {prefix})")
    print(f"[OUTPUT]  {output_file}")

    shutil.copy2(input_file, output_file)
    print("\n[COPY]    Fichier dupliqué vers Output/")

    wb = load_workbook(output_file)
    ws = wb[wb.sheetnames[0]]
    header_row = find_header_row(ws)
    cols = map_columns(ws, header_row)
    print(f"[LOAD]    Feuille '{ws.title}' — en-tête ligne {header_row}")

    manquantes = [c for c in (COL_LIBCRT, COL_ACTIFS, COL_NONACT, COL_NONNOE)
                  if c not in cols]
    if manquantes:
        raise ValueError(
            f"Colonnes obligatoires absentes de l'extraction : {manquantes}\n"
            f"  Colonnes trouvées : {sorted(cols)}"
        )

    rows = read_data_rows(ws, header_row, cols)
    print(f"[PARSER]  {len(rows)} libcrt lus")

    print("\n[GROUPES] Construction des regroupements client/offre...")
    groupes, indcol = build_groupes(rows)
    groupes = agreger(groupes, rows)
    tot = totaux(rows, indcol)

    print("\n[PHASE 1] Complétion de la feuille source...")
    cols = ensure_output_columns(ws, header_row, cols)
    first_total = write_phase1(ws, header_row, cols, rows, groupes, indcol, tot)
    print(f"  [OK]      Bloc vert écrit + totaux lignes {first_total}–{first_total + 2}")

    print("\n[PHASE 2] Génération de la feuille récapitulative...")
    write_recap(wb, groupes, tot)

    wb.save(output_file)
    wb.close()

    print("\n[KPI]     Indicateurs dashboard :")
    for libelle in ("Total", "Total Collectif", "Total Individuel"):
        t = tot[libelle]
        pct = f"{t['taux']:.2%}" if t["taux"] is not None else "n/a"
        print(f"  {libelle:<18} {pct:>8}   "
              f"(actifs {int(t['actifs']):,} / non actifs {int(t['non_act']):,} "
              f"/ non noémisables {int(t['non_noe']):,})".replace(",", " "))

    dump_config(groupes, indcol)
    print(f"\n[DONE]    {output_file.name} sauvegardé.")
    return output_file


def main() -> None:
    print("=" * 60)
    print("09_taux_noemisation.py — Taux de noémisation par client/offre")
    print("=" * 60)

    input_file = find_input_file()
    if input_file is None:
        print(f"\n[INFO]    Aucun fichier '*_Taux_Noémie*.xlsx' trouvé dans {INPUT_DIR}.")
        print("          → Étape taux de noémisation ignorée.")
        print("=" * 60)
        return

    try:
        process(input_file)
    except Exception as exc:
        print(f"\n[ERREUR]  {exc}", file=sys.stderr)
        print("=" * 60)
        sys.exit(1)
    print("=" * 60)


if __name__ == "__main__":
    main()
