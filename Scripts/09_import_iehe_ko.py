# -*- coding: utf-8 -*-
"""
09_import_iehe_ko.py — Importe des fichiers `*_IEHE_KO.csv` dans `rptpsc.suivi_iehe`.

Sert à reprendre l'historique : les flux traités avant la migration en base
n'existent que sous forme de CSV, souvent posés dans OneDrive. Ce script les
retrouve, les lit et les injecte dans la table de suivi.

Ce n'est PAS une étape du pipeline (`pipeline_runner.py` ne le référence pas) :
c'est un outil de reprise, à lancer à la demande.

Ce qu'il fait
-------------
  1. Cherche dans OneDrive (par défaut) tous les fichiers dont le nom contient
     `IEHE_KO` et se termine par `.csv`.
  2. Déduit le `flux_id` du préfixe du nom de fichier (`16042026_IEHE_KO.csv`).
  3. Lit les entêtes sans se soucier de la casse ni des accents : les fichiers
     anciens et récents n'ont pas exactement les mêmes noms de colonnes.
  4. Envoie les lignes dans `suivi_iehe` via `suivi_iehe_db.upsert_ko_rows()`.

Le cas `statut_retry`
---------------------
Les CSV portent `statut_retry` ("KO"/"OK"), la table porte `date_found`
(NULL = pas encore trouvée). La conversion :

  - `statut_retry` = "OK" → la personne avait été retrouvée. La date exacte de
    découverte n'est pas dans le fichier : on retient `date_derniere_verif`,
    qui en est la meilleure approximation disponible.
  - tout le reste       → `date_found` reste NULL, la personne repart en attente
    et le prochain `06_iehe_retry.py` la reprendra.

Ce que l'import ne casse jamais
-------------------------------
`upsert_ko_rows()` ne réécrit pas les colonnes de retry d'une ligne existante.
Réimporter un fichier déjà chargé rafraîchit donc les colonnes d'identité, sans
effacer un `date_found` acquis depuis. Les personnes marquées "OK" dans le CSV
mais encore NULL en base sont soldées ensuite par `mark_found()`, qui préserve
lui aussi une date de découverte déjà présente.

Usage
-----
    python 09_import_iehe_ko.py --dry-run       # liste et compte, n'écrit rien
    python 09_import_iehe_ko.py                 # importe pour de bon
    python 09_import_iehe_ko.py --dir "D:/archives"
    python 09_import_iehe_ko.py --flux 16042026 # un seul flux
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

import iehe_ko_lib
import suivi_iehe_db

# Lancé à la main, ce script n'hérite pas du PYTHONIOENCODING=utf-8 que
# `pipeline_runner.py` impose à ses sous-processus. Sans ça, la console Windows
# (cp1252) fait planter le premier print contenant un emoji ou un accent.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

# `_clean_date` est réutilisé volontairement : l'import doit interpréter les
# dates EXACTEMENT comme la table les enregistre (notamment "07/05/2026" en
# jour/mois et "16042026" en DDMMYYYY). Reparser autrement ici créerait deux
# vérités pour la même chaîne.
_parse_date = suivi_iehe_db._clean_date

# Motif de nom de fichier. `IEHE_KO` n'importe où dans le nom, extension .csv.
FILE_PATTERN = re.compile(r"iehe[_\s-]*ko", re.IGNORECASE)
# Préfixe de flux : 8 chiffres en tête de nom (JJMMAAAA).
FLUX_PATTERN = re.compile(r"^(\d{8})")

# Alias acceptés par colonne de la table. `iehe_ko_lib.get_ci` gère déjà la
# casse et les accents : on ne liste ici que les vraies variantes de nommage
# (avec ou sans préfixe NS_, anciens noms courts).
COLUMN_ALIASES: Dict[str, Tuple[str, ...]] = {
    "NS_num_personne":        ("NS_num_personne", "num_personne", "numpersonne", "id_personne"),
    "NS_nom_long":            ("NS_nom_long", "nom_long", "nom"),
    "NS_prenom":              ("NS_prenom", "prenom"),
    "NS_type_assure":         ("NS_type_assure", "type_assure", "typeassure"),
    "NS_date_naissance":      ("NS_date_naissance", "date_naissance"),
    "NS_valeur_coordonnee":   ("NS_valeur_coordonnee", "valeur_coordonnee", "mail", "email"),
    "NS_idkpep":              ("NS_idkpep", "idkpep", "kpep"),
    "NS_offre":               ("NS_offre", "offre"),
    "NS_date_adhesion":       ("NS_date_adhesion", "date_adhesion"),
    "NS_date_effet_adhesion": ("NS_date_effet_adhesion", "date_effet_adhesion", "date_effet"),
    "NS_code_soc_appart":     ("NS_code_soc_appart", "code_soc_appart", "code_soc", "societe"),
    "Eligibilité TP":         ("Eligibilité TP", "Eligibilite TP", "eligibilite_tp"),
    "Date éligibilité TP":    ("Date éligibilité TP", "Date eligibilite TP", "date_eligibilite_tp"),
    "Valeur carte TP":        ("Valeur carte TP", "valeur_carte_tp"),
    "Raison non Eligibilité": ("Raison non Eligibilité", "Raison non Eligibilite",
                               "raison_non_eligibilite", "motif"),
    "date_found":             ("date_found",),
    "date_derniere_verif":    ("date_derniere_verif", "date_derniere_verification"),
    "mail_IEHE":              ("mail_IEHE", "mail_iehe"),
    "KPEP_IEHE":              ("KPEP_IEHE", "kpep_iehe"),
}


# =============================================================================
# RECHERCHE DES FICHIERS
# =============================================================================

def default_scan_dir() -> Optional[Path]:
    """Racine OneDrive du poste, via les variables d'environnement Windows."""
    for var in ("ONEDRIVECOMMERCIAL", "ONEDRIVE", "ONEDRIVECONSUMER"):
        value = os.environ.get(var)
        if value and Path(value).is_dir():
            return Path(value)
    return None


def find_ko_files(root: Path, recursive: bool = True) -> List[Path]:
    """Fichiers `*IEHE_KO*.csv` sous `root`, triés par flux puis par nom."""
    it = root.rglob("*.csv") if recursive else root.glob("*.csv")
    files = []
    for path in it:
        if not FILE_PATTERN.search(path.stem):
            continue
        # Fichiers temporaires Office : même nom, contenu inutilisable.
        if path.name.startswith("~$"):
            continue
        files.append(path)
    return sorted(files, key=lambda p: (p.name, str(p)))


def flux_id_from_name(path: Path) -> Optional[str]:
    """`16042026_IEHE_KO.csv` → "16042026". None si le préfixe est absent/invalide.

    Le flux_id fait partie de la clé primaire : mieux vaut refuser un fichier
    que d'inventer un identifiant qui polluerait la table définitivement.
    """
    match = FLUX_PATTERN.match(path.stem)
    if not match:
        return None
    candidate = match.group(1)
    try:
        datetime.strptime(candidate, "%d%m%Y")
    except ValueError:
        return None
    return candidate


# =============================================================================
# LECTURE
# =============================================================================

def read_csv_any(path: Path) -> Optional[pd.DataFrame]:
    """Lit un CSV sans connaître son séparateur ni son encodage.

    Tout est lu en texte (`dtype=str`) : la conversion des dates est faite plus
    tard par `suivi_iehe_db`, une seule fois, pour toute la chaîne.
    """
    for encoding in ("utf-8-sig", "latin-1"):
        try:
            df = pd.read_csv(path, sep=None, engine="python", dtype=str,
                             encoding=encoding, keep_default_na=False)
        except UnicodeDecodeError:
            continue
        except Exception as exc:  # noqa: BLE001
            print(f"      [WARN] lecture impossible : {exc}")
            return None

        # sep=None se trompe parfois : une seule colonne dont l'entête contient
        # encore un séparateur trahit l'échec.
        if len(df.columns) == 1:
            header = str(df.columns[0])
            for sep in (";", ",", "\t"):
                if sep in header:
                    try:
                        df = pd.read_csv(path, sep=sep, engine="python", dtype=str,
                                         encoding=encoding, keep_default_na=False)
                    except Exception:  # noqa: BLE001
                        pass
                    break

        # Les BOM successifs et les espaces parasites empêchent la reconnaissance
        # des entêtes.
        df.columns = [str(c).replace("\ufeff", "").strip() for c in df.columns]
        return df
    print("      [WARN] encodage non reconnu (ni utf-8 ni latin-1).")
    return None


def build_records(df: pd.DataFrame, flux_id: str) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Convertit les lignes du CSV en enregistrements pour `upsert_ko_rows`.

    Retourne aussi un petit compteur pour le rapport de fin.
    """
    stats = {"lignes": len(df), "sans_id": 0, "resolues": 0, "en_attente": 0}
    records: List[Dict[str, Any]] = []

    for row in df.to_dict("records"):
        rec: Dict[str, Any] = {}
        for column, aliases in COLUMN_ALIASES.items():
            rec[column] = iehe_ko_lib.get_ci(row, *aliases)

        if not str(rec.get("NS_num_personne") or "").strip():
            stats["sans_id"] += 1
            continue

        # statut_retry n'existe pas dans la table : il se traduit en date_found.
        statut = str(iehe_ko_lib.get_ci(row, "statut_retry") or "").strip().upper()
        date_found = _parse_date(rec.get("date_found"))
        if date_found is None and statut == "OK":
            # Approximation assumée : le CSV ne date pas la découverte, seule la
            # dernière vérification est connue.
            date_found = _parse_date(rec.get("date_derniere_verif"))
        rec["date_found"] = date_found

        if date_found is not None:
            stats["resolues"] += 1
        else:
            stats["en_attente"] += 1

        records.append(rec)

    return records, stats


def resolved_by_date(records: List[Dict[str, Any]]) -> Dict[date, Dict[str, Dict[str, str]]]:
    """Regroupe les personnes déjà retrouvées par date de découverte.

    `mark_found()` applique UNE date à tout un lot : on groupe donc par date
    plutôt que d'appeler la fonction ligne par ligne.
    """
    grouped: Dict[date, Dict[str, Dict[str, str]]] = {}
    for rec in records:
        day = rec.get("date_found")
        if not isinstance(day, date):
            continue
        pid = str(rec.get("NS_num_personne") or "").strip()
        if not pid:
            continue
        grouped.setdefault(day, {})[pid] = {
            "mail": str(rec.get("mail_IEHE") or "").strip(),
            "kpep": str(rec.get("KPEP_IEHE") or "").strip(),
        }
    return grouped


# =============================================================================
# MAIN
# =============================================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Importe les fichiers *_IEHE_KO.csv de OneDrive dans rptpsc.suivi_iehe.")
    parser.add_argument("--dir", dest="scan_dir", default=None,
                        help="Dossier à scanner (défaut : racine OneDrive du poste).")
    parser.add_argument("--flux", default=None,
                        help="N'importer que ce flux (ex. 16042026).")
    parser.add_argument("--no-recursive", action="store_true",
                        help="Ne pas descendre dans les sous-dossiers.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Analyse les fichiers et affiche le bilan sans rien écrire.")
    args = parser.parse_args()

    print("=" * 78)
    print("  09_import_iehe_ko.py — Import des CSV IEHE_KO dans la table de suivi")
    print("=" * 78)

    # --- Dossier à scanner ---
    if args.scan_dir:
        root = Path(args.scan_dir)
    else:
        root = default_scan_dir()
        if root is None:
            print("❌ OneDrive introuvable (variables ONEDRIVE / ONEDRIVECOMMERCIAL "
                  "absentes). Précisez --dir.")
            return 1
    if not root.is_dir():
        print(f"❌ Dossier introuvable : {root}")
        return 1

    print(f"   📂 Dossier          : {root}")
    print(f"   🔎 Récursif         : {'non' if args.no_recursive else 'oui'}")
    print(f"   🧪 Mode             : {'DRY-RUN (aucune écriture)' if args.dry_run else 'ÉCRITURE'}")

    # --- Fichiers ---
    files = find_ko_files(root, recursive=not args.no_recursive)
    if not files:
        print("\n⚠️  Aucun fichier *IEHE_KO*.csv trouvé.")
        return 0

    retenus: List[Tuple[Path, str]] = []
    for path in files:
        flux_id = flux_id_from_name(path)
        if flux_id is None:
            print(f"   ⏭️  Ignoré (pas de préfixe JJMMAAAA) : {path.name}")
            continue
        if args.flux and flux_id != str(args.flux).strip():
            continue
        retenus.append((path, flux_id))

    print(f"\n   {len(files)} fichier(s) trouvé(s), {len(retenus)} retenu(s).\n")
    if not retenus:
        return 0

    # --- Connexion (inutile en dry-run) ---
    conn = None
    if not args.dry_run:
        conn = suivi_iehe_db.connect_supervision()
        if conn is None:
            print("❌ Base supervision injoignable — rien n'a été importé.")
            return 1
        if not suivi_iehe_db.ensure_table(conn):
            print(f"❌ Table {suivi_iehe_db.FQTN} indisponible — rien n'a été importé.")
            conn.close()
            return 1

    total = {"fichiers": 0, "lignes": 0, "envoyees": 0, "resolues": 0,
             "en_attente": 0, "sans_id": 0, "soldees": 0}

    try:
        for path, flux_id in retenus:
            print(f"   ── {path.name}  (flux {flux_id})")
            try:
                rel = path.relative_to(root)
                print(f"      chemin : {rel}")
            except ValueError:
                print(f"      chemin : {path}")

            df = read_csv_any(path)
            if df is None or df.empty:
                print("      ⚠️  fichier vide ou illisible — ignoré.\n")
                continue

            records, stats = build_records(df, flux_id)
            print(f"      lignes : {stats['lignes']} | exploitables : {len(records)}"
                  f" | sans identifiant : {stats['sans_id']}")
            print(f"      déjà retrouvées : {stats['resolues']}"
                  f" | encore en attente : {stats['en_attente']}")

            total["fichiers"] += 1
            for key in ("lignes", "resolues", "en_attente", "sans_id"):
                total[key] += stats[key]

            if not records:
                print()
                continue

            if args.dry_run:
                print(f"      (dry-run) {len(records)} ligne(s) seraient envoyées.\n")
                continue

            envoyees = suivi_iehe_db.upsert_ko_rows(conn, flux_id, records)
            total["envoyees"] += envoyees
            print(f"      ✅ {envoyees} ligne(s) envoyée(s) dans {suivi_iehe_db.FQTN}")

            # Les lignes déjà présentes en base ne sont pas réécrites par l'upsert :
            # on solde ici celles que le CSV déclare retrouvées et qui sont encore
            # en attente. mark_found() préserve un date_found déjà renseigné.
            soldees = 0
            for day, found in resolved_by_date(records).items():
                soldees += suivi_iehe_db.mark_found(conn, found, day)
            if soldees:
                total["soldees"] += soldees
                print(f"      ✅ {soldees} personne(s) marquée(s) comme retrouvée(s)")
            print()
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    print("=" * 78)
    print(f"  Fichiers traités        : {total['fichiers']}")
    print(f"  Lignes lues             : {total['lignes']}")
    print(f"  Sans identifiant        : {total['sans_id']}")
    print(f"  Déjà retrouvées (CSV)   : {total['resolues']}")
    print(f"  Encore en attente       : {total['en_attente']}")
    if args.dry_run:
        print("  DRY-RUN — aucune écriture effectuée.")
    else:
        print(f"  Lignes envoyées en base : {total['envoyees']}")
        print(f"  Personnes soldées       : {total['soldees']}")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
