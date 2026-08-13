# -*- coding: utf-8 -*-
"""
seed_test_carte_tp.py — Jeu d'essai pour `rptpsc.suivi_carte_tp`.

But
---
Vérifier, sur des données FABRIQUÉES et maîtrisées, que les deux mécanismes
centraux du suivi carte TP font bien ce qu'on croit :

  1. LE FILTRE DE PÉRIODE — `date_eligibilite <= CURRENT_DATE`
     (`fetch_ged_pending`) : une carte future ne doit PAS être demandée à la GED
     et ne doit PAS compter comme manquante.

  2. LE KPI DE DÉLAI — `fetch_tp_stats()` : ventilation J=0 / J<=4 / J>4 /
     Non_Trouvees, et l'écart entre le délai CONTRACTUEL (depuis
     `date_eligibilite` seule) et la variante ancrée au flux (depuis
     `GREATEST(date_eligibilite, date_premiere_vue)`), gardée pour arbitrage.

  3. LA VENTILATION PAR FLUX — `Issues_Flux_Courant` / `Issues_Flux_Anterieurs`,
     le bloc `Flux_Courant` et le `Stock_KO_Anterieur`.

20 lignes couvrent les cas limites : échéance à J exactement, échéance à J+1,
carte trouvée AVANT son échéance, carte découverte longtemps après son
échéance, carte sans KPEP IEHE, carte sans date d'éligibilité, et trois flux
différents dont les cartes tombent le même jour.

Toutes les valeurs attendues sont recalculées en Python, indépendamment du SQL :
si les deux divergent, c'est que l'un des deux a tort — et le script le dit.

Sécurité
--------
Les lignes de test sont reconnaissables sans ambiguïté :
`num_personne` commence par TSTCARTE et `flux_id` par TEST_SEED. `--clean` ne
supprime QUE ces lignes-là. Aucune donnée réelle n'est lue, modifiée ou
supprimée. À lancer sur une base de recette, pas en production.

Usage
-----
    python Scripts/seed_test_carte_tp.py --apply     # insere + verifie
    python Scripts/seed_test_carte_tp.py --check     # verifie seulement
    python Scripts/seed_test_carte_tp.py --clean     # supprime le jeu d'essai
    python Scripts/seed_test_carte_tp.py             # dry-run : montre le plan

Sorties en ASCII pur : ce script tourne en console Windows cp1252, où une
fleche Unicode ferait echouer le print (meme raison que `_bornes_cohorte`).
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_ROOT / "Scripts") not in sys.path:
    sys.path.insert(0, str(_ROOT / "Scripts"))


# =============================================================================
# CONNEXION
# =============================================================================

# `suivi_iehe_db` lit ses identifiants dans l'environnement AU MOMENT DE
# L'IMPORT, avec un repli codé en dur qui ne pointe sur rien d'utilisable ici.
# On peuple donc l'environnement depuis config/credentials.ini AVANT d'importer
# quoi que ce soit du suivi — l'ordre n'est pas cosmetique.
def _load_credentials_into_env() -> bool:
    """Renseigne PG_SUPERVISION_* depuis credentials.ini. False si absent."""
    if os.getenv("PG_SUPERVISION_HOST") and os.getenv("PG_SUPERVISION_PASSWORD"):
        return True  # environnement deja fourni (runner, CI) : on n'ecrase pas
    try:
        from config.credentials_loader import CREDENTIALS
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] credentials_loader indisponible : {exc}")
        return False
    sec = CREDENTIALS.section("postgresql_supervision")
    user, password = CREDENTIALS.credentials_for("postgresql_supervision")
    if not (sec.get("host") and user and password):
        print("[WARN] section [postgresql_supervision] incomplete dans credentials.ini")
        return False
    os.environ["PG_SUPERVISION_HOST"] = sec["host"]
    os.environ["PG_SUPERVISION_PORT"] = sec.get("port", "5432")
    os.environ["PG_SUPERVISION_DB"] = sec.get("dbname", "supervisionpsc_db")
    os.environ["PG_SUPERVISION_USER"] = user
    os.environ["PG_SUPERVISION_PASSWORD"] = password
    return True


_load_credentials_into_env()

import suivi_carte_tp_db as db  # noqa: E402  (import apres peuplement de l'env)


# =============================================================================
# MARQUEURS DU JEU D'ESSAI
# =============================================================================

PERSONNE_PREFIX = "TSTCARTE"
FLUX_PREFIX = "TEST_SEED"
KPEP_PREFIX = "KPEPTEST"

# Predicat unique du jeu d'essai. Utilise a l'identique par --clean et par les
# comptages : impossible que la suppression et la verification ne portent pas
# exactement sur les memes lignes.
_WHERE_TEST = (
    f"num_personne LIKE '{PERSONNE_PREFIX}%%' AND flux_id LIKE '{FLUX_PREFIX}%%'"
)


# =============================================================================
# LES 20 CAS
# =============================================================================

# Chaque cas : offsets en JOURS par rapport a CURRENT_DATE (T) de la base.
#   elig  : date_eligibilite       (None = colonne NULL)
#   vue   : date_premiere_vue      (le jour ou le flux a revele la carte)
#   found : date_found_ged         (None = pas encore trouvee)
#   kpep  : la personne est-elle dans IEHE ?
#
# `attendu` n'est PAS lu par le code : c'est la phrase que le cas est cense
# demontrer, imprimee a cote du resultat pour rendre un echec lisible.
_CAS: List[Dict[str, Any]] = [
    # --- KPI : ventilation du delai, cas nominaux -------------------------
    dict(n=1,  elig=-30, vue=-30, found=-30, kpep=True,
         attendu="delai 0 j  -> Le_Jour_Meme + Dans_les_4_jours"),
    dict(n=2,  elig=-30, vue=-30, found=-28, kpep=True,
         attendu="delai 2 j  -> Dans_les_4_jours"),
    dict(n=3,  elig=-30, vue=-30, found=-26, kpep=True,
         attendu="delai 4 j  -> Dans_les_4_jours (borne INCLUSE)"),
    dict(n=4,  elig=-30, vue=-30, found=-25, kpep=True,
         attendu="delai 5 j  -> Plus_de_4_jours (juste au-dela)"),
    dict(n=5,  elig=-30, vue=-30, found=-10, kpep=True,
         attendu="delai 20 j -> Plus_de_4_jours"),

    # --- FILTRE DE PERIODE : les bornes ----------------------------------
    dict(n=6,  elig=-20, vue=-20, found=None, kpep=True,
         attendu="echue, pas trouvee -> En_Attente_GED + DANS la liste GED"),
    dict(n=7,  elig=-20, vue=-20, found=None, kpep=False,
         attendu="echue mais sans KPEP -> Absents_IEHE, HORS liste GED"),
    dict(n=8,  elig=0,   vue=-5,  found=None, kpep=True,
         attendu="echue AUJOURD'HUI (<= est inclusif) -> DANS la liste GED"),
    dict(n=9,  elig=+1,  vue=-5,  found=None, kpep=True,
         attendu="echue DEMAIN -> Cartes_Futures, HORS liste GED"),
    dict(n=10, elig=+15, vue=-5,  found=None, kpep=True,
         attendu="echue dans 15 j -> Cartes_Futures, HORS liste GED"),

    # --- les deux pieges du calcul ---------------------------------------
    dict(n=11, elig=+10, vue=-5,  found=-1,  kpep=True,
         attendu="TROUVEE EN AVANCE -> comptee DUE et reussie, delai plancher 0"),
    dict(n=12, elig=None, vue=-5, found=None, kpep=True,
         attendu="sans date d'eligibilite -> Sans_Date_Eligibilite, HORS liste GED"),
    dict(n=13, elig=-40, vue=-3,  found=-1,  kpep=True,
         attendu="carte revelee tardivement -> borne 2 j (OK), BRUT 39 j (KO)"),

    # --- cartes echues recemment -----------------------------------------
    dict(n=14, elig=-2,  vue=-2,  found=None, kpep=True,
         attendu="echue avant-hier -> DANS la liste GED"),
    dict(n=15, elig=-1,  vue=-1,  found=0,   kpep=True,
         attendu="delai 1 j -> Dans_les_4_jours"),

    # --- MEME echeance, TROIS flux differents ----------------------------
    # Le point des notes : le controle du jour ne se lit pas dans le flux du
    # jour. Ces trois cartes sont dues le meme jour et viennent de trois flux
    # distincts ; seule la table peut les rassembler.
    dict(n=16, elig=-6,  vue=-20, found=None, kpep=True, flux="TEST_SEED_A",
         attendu="flux A (J-20), due J-6 -> DANS la liste GED"),
    dict(n=17, elig=-6,  vue=-6,  found=None, kpep=True, flux="TEST_SEED_B",
         attendu="flux B (J-6), due J-6 -> DANS la liste GED"),
    dict(n=18, elig=-6,  vue=-13, found=-4,  kpep=True, flux="TEST_SEED_C",
         attendu="flux C (J-13), due J-6, trouvee J-4 -> delai 2 j"),

    # --- anteriorite lointaine (cohorte d'un autre mois) -----------------
    dict(n=19, elig=-60, vue=-60, found=None, kpep=True,
         attendu="backlog ancien -> toujours DANS la liste GED"),
    dict(n=20, elig=-60, vue=-60, found=-59, kpep=True,
         attendu="delai 1 j, cohorte d'un mois anterieur"),
]


def _d(today: date, offset: Optional[int]) -> Optional[date]:
    return None if offset is None else today + timedelta(days=offset)


def _row(cas: Dict[str, Any], today: date) -> Dict[str, Any]:
    """Traduit un cas en ligne de table."""
    n = cas["n"]
    return {
        "num_personne": f"{PERSONNE_PREFIX}{n:03d}",
        "code_soc_appart": "999",          # societe fictive, jamais reelle
        "offre": "TESTTP001",
        "flux_id": cas.get("flux", FLUX_PREFIX),
        "date_premiere_vue": _d(today, cas["vue"]),
        "date_adhesion": _d(today, cas["vue"]),
        "date_effet_adhesion": (
            _d(today, cas["elig"] + 21) if cas["elig"] is not None else None
        ),
        "type_assure": "ASSPRI",
        "date_eligibilite": _d(today, cas["elig"]),
        "kpep_iehe": f"{KPEP_PREFIX}{n:03d}" if cas["kpep"] else None,
        "date_found_iehe": _d(today, cas["vue"]) if cas["kpep"] else None,
        "date_found_ged": _d(today, cas["found"]),
        "date_dispo_ged": _d(today, cas["found"]),
        "date_derniere_verif": today,
        "motif": "jeu_essai",
    }


# =============================================================================
# VALEURS ATTENDUES — recalculees en Python, sans SQL
# =============================================================================

def _delai(elig: Optional[int], vue: Optional[int], found: Optional[int]) -> Optional[int]:
    """Replique de `_DELAI` : GREATEST(found - elig, 0).

    `vue` n'entre PLUS dans le calcul contractuel (regle metier) ; le parametre
    est conserve pour ne pas avoir a toucher les appelants, et parce que
    `_delai_depuis_flux` s'en sert.

    La date GED est `COALESCE(date_dispo_ged, date_found_ged)` cote SQL ; le jeu
    d'essai pose les deux a la meme valeur, la replique reste donc exacte.
    """
    if found is None or elig is None:
        return None
    return max(found - elig, 0)


def _delai_depuis_flux(elig: Optional[int], vue: Optional[int],
                       found: Optional[int]) -> Optional[int]:
    """Replique de `_DELAI_DEPUIS_FLUX` : GREATEST(found - GREATEST(elig, vue), 0)."""
    if found is None or elig is None or vue is None:
        return None
    return max(found - max(elig, vue), 0)


def attendu_pending() -> List[str]:
    """KPEP que `fetch_ged_pending` DOIT renvoyer : echue, non trouvee, avec KPEP."""
    return sorted(
        f"{KPEP_PREFIX}{c['n']:03d}"
        for c in _CAS
        if c["elig"] is not None and c["elig"] <= 0
        and c["found"] is None and c["kpep"]
    )


def attendu_etat_a_date() -> Dict[str, int]:
    """Replique du second SELECT de `fetch_tp_stats` (bloc Etat_A_Date)."""
    echue = [c for c in _CAS if c["elig"] is not None and c["elig"] <= 0]
    return {
        "Cartes_Attendues_A_Date": len(echue),
        "Trouvees_GED": sum(1 for c in echue if c["found"] is not None),
        "Absents_IEHE": sum(1 for c in echue if not c["kpep"]),
        "En_Attente_GED": sum(1 for c in echue if c["kpep"] and c["found"] is None),
        "Cartes_Futures": sum(1 for c in _CAS
                              if c["elig"] is not None and c["elig"] > 0),
        "Sans_Date_Eligibilite": sum(1 for c in _CAS if c["elig"] is None),
        "Total_Suivi": len(_CAS),
    }


def attendu_cohorte(seuil: int) -> Dict[str, Any]:
    """Replique du premier SELECT de `fetch_tp_stats` sur une periode couvrant tout.

    Rappel des deux regles qui se lisent mal en SQL :
      DUE    = echue OU deja trouvee  (une carte trouvee en avance est un succes)
      FUTURE = pas echue ET pas trouvee
    Les lignes sans date d'eligibilite sortent par le BETWEEN.
    """
    coh = [c for c in _CAS if c["elig"] is not None]
    due = [c for c in coh if c["elig"] <= 0 or c["found"] is not None]
    futures = [c for c in coh if c["elig"] > 0 and c["found"] is None]
    trouvees = [c for c in due if c["found"] is not None]

    delais = [_delai(c["elig"], c["vue"], c["found"]) for c in trouvees]
    dans_4j = sum(1 for d in delais if d is not None and d <= seuil)
    depuis_flux = [_delai_depuis_flux(c["elig"], c["vue"], c["found"]) for c in trouvees]
    dans_4j_flux = sum(1 for d in depuis_flux if d is not None and d <= seuil)
    # `Le_Jour_Meme` (J = 0) : found <= elig, soit exactement delai == 0 grace au
    # plancher — une carte trouvee EN AVANCE compte donc ici.
    jour_meme = sum(1 for d in delais if d == 0)

    # Les cartes NON TROUVEES sont hors de la distribution : leur delai n'est
    # pas 0, il n'existe pas. C'est precisement ce que le SQL oubliait — en
    # PostgreSQL `GREATEST(NULL::int - 5, 0)` vaut 0, donc une carte manquante
    # entrait dans MIN/MAX/AVG comme une livraison le jour meme.
    ordre = sorted(d for d in delais if d is not None)
    milieu = len(ordre) // 2
    median = (float(ordre[milieu]) if len(ordre) % 2
              else (ordre[milieu - 1] + ordre[milieu]) / 2.0) if ordre else None
    # `round` reproduit `fetch_tp_stats`, arrondi bancaire compris (0,25 -> 0,2).
    moyenne = round(sum(ordre) / len(ordre), 1) if ordre else None

    return {
        "Population_Attendue": len(coh),
        "Cartes_Futures": len(futures),
        "Population_Due": len(due),
        "Trouvees_GED": len(trouvees),
        "Dans_les_4_jours": dans_4j,
        "Plus_de_4_jours": len(trouvees) - dans_4j,
        "Dans_les_4_jours_Depuis_Flux": dans_4j_flux,
        "Le_Jour_Meme": jour_meme,
        # Ventilation par flux d'origine. `FLUX_PREFIX` est le flux COURANT du
        # jeu d'essai : c'est lui qui porte la `date_premiere_vue` la plus
        # recente (cas n=15, J-1), donc celui que `fetch_dernier_flux` retient.
        "Issues_Flux_Courant": sum(
            1 for c in coh if c.get("flux", FLUX_PREFIX) == FLUX_PREFIX),
        "Issues_Flux_Anterieurs": sum(
            1 for c in coh if c.get("flux", FLUX_PREFIX) != FLUX_PREFIX),
        "Non_Trouvees_GED": len(due) - len(trouvees),
        "Absents_IEHE": sum(1 for c in due if c["found"] is None and not c["kpep"]),
        "En_Attente_GED": sum(1 for c in due if c["found"] is None and c["kpep"]),
        "Delai_Min": min(ordre) if ordre else None,
        "Delai_Max": max(ordre) if ordre else None,
        "Delai_Median": median,
        "Delai_Moyenne": moyenne,
        "Delai_Base": len(ordre),
    }


def attendu_stock_ko_anterieur(fin_offset: int) -> int:
    """Replique du compteur `Stock_KO_Anterieur`.

    Introuvables, issues d'un flux AUTRE que le flux courant, dues STRICTEMENT
    avant la borne haute. Les lignes sans date d'eligibilite sortent : en SQL
    `NULL < date` ne vaut pas vrai.
    """
    return sum(
        1 for c in _CAS
        if c["found"] is None
        and c["elig"] is not None and c["elig"] < fin_offset
        and c.get("flux", FLUX_PREFIX) != FLUX_PREFIX
    )


def attendu_flux_courant() -> Dict[str, int]:
    """Replique du bloc `Flux_Courant`, relatif a AUJOURD'HUI (hors periode).

    `Total_Cartes` est la SOMME des deux compteurs, pas le nombre de lignes du
    flux : une carte sans date d'eligibilite n'est ni echue ni future.
    """
    duflux = [c for c in _CAS if c.get("flux", FLUX_PREFIX) == FLUX_PREFIX]
    eligibles = sum(1 for c in duflux if c["elig"] is not None and c["elig"] <= 0)
    futures = sum(1 for c in duflux if c["elig"] is not None and c["elig"] > 0)
    return {
        "Cartes_Eligibles": eligibles,
        "Cartes_Futures": futures,
        "Total_Cartes": eligibles + futures,
        "Sans_Date_Eligibilite": sum(1 for c in duflux if c["elig"] is None),
    }


# =============================================================================
# ACTIONS
# =============================================================================

def _today(conn) -> date:
    """CURRENT_DATE **de la base**, pas de la machine.

    Le filtre teste est evalue par PostgreSQL. Prendre `date.today()` ici
    ferait echouer le jeu d'essai a tort si le serveur n'est pas sur le meme
    jour (fuseau, execution a cheval sur minuit)."""
    with conn.cursor() as cur:
        cur.execute("SELECT CURRENT_DATE")
        return cur.fetchone()[0]


def seed(conn, today: date) -> int:
    """Insere les 20 lignes. Remplace le jeu d'essai precedent s'il existe."""
    clean(conn, verbose=False)
    cols = [name for name, _ in db._COLUMNS]
    sql = (f"INSERT INTO {db.FQTN} ({', '.join(cols)}) "
           f"VALUES ({', '.join(['%s'] * len(cols))})")
    payload = [tuple(_row(c, today)[col] for col in cols) for c in _CAS]
    with conn.cursor() as cur:
        cur.executemany(sql, payload)
        inserted = cur.rowcount
    conn.commit()
    return int(inserted if inserted and inserted > 0 else len(payload))


def clean(conn, verbose: bool = True) -> int:
    """Supprime UNIQUEMENT les lignes du jeu d'essai."""
    with conn.cursor() as cur:
        cur.execute(f"DELETE FROM {db.FQTN} WHERE {_WHERE_TEST}")
        deleted = cur.rowcount or 0
    conn.commit()
    if verbose:
        print(f"Supprime : {deleted} ligne(s) de test.")
    return int(deleted)


def count_reelles(conn) -> int:
    """Lignes NON marquees test. Doit rester constant : garde-fou anti-degat."""
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM {db.FQTN} WHERE NOT ({_WHERE_TEST})")
        return int(cur.fetchone()[0])


# =============================================================================
# VERIFICATION
# =============================================================================

class _Rapport:
    def __init__(self) -> None:
        self.ok = 0
        self.ko: List[str] = []

    def check(self, libelle: str, obtenu: Any, attendu: Any) -> None:
        if obtenu == attendu:
            self.ok += 1
            print(f"  [OK]   {libelle:<34} = {obtenu}")
        else:
            self.ko.append(libelle)
            print(f"  [ECHEC]{libelle:<34} = {obtenu}  (attendu {attendu})")


def verifier(conn, today: date) -> bool:
    rap = _Rapport()
    seuil = db.DELAI_CONTRACTUEL_JOURS

    print(f"\nCURRENT_DATE de la base : {today}")
    print(f"Seuil contractuel       : {seuil} jours")

    print("\n--- Les 20 cas ---")
    for c in _CAS:
        r = _row(c, today)
        d = _delai(c["elig"], c["vue"], c["found"])
        print(f"  #{c['n']:02d} elig={str(r['date_eligibilite'] or '-'):<10} "
              f"vue={str(r['date_premiere_vue']):<10} "
              f"ged={str(r['date_found_ged'] or '-'):<10} "
              f"kpep={'oui' if c['kpep'] else 'NON':<3} "
              f"delai={str(d if d is not None else '-'):>3}  {c['attendu']}")

    # --- 1. LE FILTRE DE PERIODE ---
    print("\n--- 1. Filtre de periode : fetch_ged_pending() ---")
    pending = sorted(k for k in db.fetch_ged_pending(conn) if k.startswith(KPEP_PREFIX))
    attendu = attendu_pending()
    rap.check("KPEP a interroger", pending, attendu)
    rap.check("carte echue AUJOURD'HUI incluse",
              f"{KPEP_PREFIX}008" in pending, True)
    rap.check("carte echue DEMAIN exclue",
              f"{KPEP_PREFIX}009" in pending, False)
    rap.check("carte sans KPEP exclue",
              f"{KPEP_PREFIX}007" in pending, False)
    rap.check("carte sans date elig. exclue",
              f"{KPEP_PREFIX}012" in pending, False)
    rap.check("3 flux differents, meme echeance",
              sorted(k for k in pending if k[-3:] in ("016", "017")),
              [f"{KPEP_PREFIX}016", f"{KPEP_PREFIX}017"])

    # --- 2. LE KPI ---
    stats = db.fetch_tp_stats(conn, debut=today - timedelta(days=70),
                              fin=today + timedelta(days=30))
    if stats is None:
        print("\n[ECHEC] fetch_tp_stats a renvoye None.")
        return False

    print("\n--- 2. KPI sur la cohorte [J-70, J+30] ---")
    att = attendu_cohorte(seuil)
    rap.check("Population_Attendue", stats["Population_Attendue"], att["Population_Attendue"])
    rap.check("Cartes_Futures", stats["Cartes_Futures"]["Nombre"], att["Cartes_Futures"])
    rap.check("Population_Due", stats["Population_Due"], att["Population_Due"])
    rap.check("Trouvees_GED", stats["Trouvees_GED"]["Nombre"], att["Trouvees_GED"])
    rap.check("Dans_les_4_jours", stats["Dans_les_4_jours"]["Nombre"], att["Dans_les_4_jours"])
    rap.check("Plus_de_4_jours", stats["Plus_de_4_jours"]["Nombre"], att["Plus_de_4_jours"])
    rap.check("Dans_les_4_jours_Depuis_Flux",
              stats["Dans_les_4_jours_Depuis_Flux"]["Nombre"],
              att["Dans_les_4_jours_Depuis_Flux"])
    rap.check("Le_Jour_Meme", stats["Le_Jour_Meme"]["Nombre"], att["Le_Jour_Meme"])
    rap.check("Non_Trouvees_GED", stats["Non_Trouvees_GED"]["Nombre"], att["Non_Trouvees_GED"])
    rap.check("Absents_IEHE", stats["Absents_IEHE"]["Nombre"], att["Absents_IEHE"])
    rap.check("En_Attente_GED", stats["En_Attente_GED"]["Nombre"], att["En_Attente_GED"])

    # --- 2 bis. Ventilation par flux d'origine ---
    print("\n--- 2 bis. Flux d'origine ---")
    rap.check("Flux_Reference = flux le plus recent", stats["Flux_Reference"], FLUX_PREFIX)
    rap.check("Issues_Flux_Courant", stats["Issues_Flux_Courant"],
              att["Issues_Flux_Courant"])
    rap.check("Issues_Flux_Anterieurs", stats["Issues_Flux_Anterieurs"],
              att["Issues_Flux_Anterieurs"])
    rap.check("Stock_KO_Anterieur", stats["Stock_KO_Anterieur"]["Nombre"],
              attendu_stock_ko_anterieur(30))

    fc, att_fc = stats["Flux_Courant"], attendu_flux_courant()
    for cle in ("Cartes_Eligibles", "Cartes_Futures", "Total_Cartes",
                "Sans_Date_Eligibilite"):
        rap.check(f"Flux_Courant.{cle}", fc[cle], att_fc[cle])

    dist = stats["Distribution_Delai"]
    rap.check("Delai Min", dist["Min"], att["Delai_Min"])
    rap.check("Delai Max", dist["Max"], att["Delai_Max"])
    rap.check("Delai Median", dist["Median"], att["Delai_Median"])
    rap.check("Delai Moyenne", dist["Moyenne"], att["Delai_Moyenne"])
    rap.check("Delai Base = cartes trouvees", dist["Base"], att["Delai_Base"])

    # --- 3. Les invariants annonces par la docstring de fetch_tp_stats ---
    print("\n--- 3. Invariants ---")
    rap.check("dans4j + plus4j + non_trouvees = due",
              stats["Dans_les_4_jours"]["Nombre"] + stats["Plus_de_4_jours"]["Nombre"]
              + stats["Non_Trouvees_GED"]["Nombre"],
              stats["Population_Due"])
    rap.check("absents + en_attente = non_trouvees",
              stats["Absents_IEHE"]["Nombre"] + stats["En_Attente_GED"]["Nombre"],
              stats["Non_Trouvees_GED"]["Nombre"])
    rap.check("attendue - futures = due",
              stats["Population_Attendue"] - stats["Cartes_Futures"]["Nombre"],
              stats["Population_Due"])
    # Une carte porte UN SEUL flux_id (PK sans flux_id, `refresh=False`) : la
    # somme est donc exacte, sans dedoublonnage. Si ce test casse, c'est que la
    # cle de la table a change et que les cartes sont dupliquees par flux.
    rap.check("issues courant + anterieurs = attendue",
              stats["Issues_Flux_Courant"] + stats["Issues_Flux_Anterieurs"],
              stats["Population_Attendue"])
    rap.check("Le_Jour_Meme inclus dans Dans_les_4_jours",
              stats["Le_Jour_Meme"]["Nombre"] <= stats["Dans_les_4_jours"]["Nombre"],
              True)
    rap.check("dans4j + plus4j = trouvees",
              stats["Dans_les_4_jours"]["Nombre"] + stats["Plus_de_4_jours"]["Nombre"],
              stats["Trouvees_GED"]["Nombre"])
    # Le delai contractuel part de l'eligibilite SEULE : il est donc toujours
    # >= au delai ancre au flux, et le compteur "dans les 4 jours" toujours <=.
    rap.check("dans4j <= dans4j depuis flux",
              stats["Dans_les_4_jours"]["Nombre"]
              <= stats["Dans_les_4_jours_Depuis_Flux"]["Nombre"],
              True)
    rap.check("En_Attente_GED = taille liste GED",
              stats["En_Attente_GED"]["Nombre"], len(pending))

    # --- 4. Etat a date (tuiles du dashboard) ---
    print("\n--- 4. Etat_A_Date (toutes cohortes) ---")
    etat, att_etat = stats["Etat_A_Date"], attendu_etat_a_date()
    for cle in ("Cartes_Attendues_A_Date", "Trouvees_GED", "Absents_IEHE",
                "En_Attente_GED", "Cartes_Futures", "Sans_Date_Eligibilite",
                "Total_Suivi"):
        rap.check(cle, etat[cle], att_etat[cle])

    # --- 5. Une periode etroite ne doit voir QUE sa cohorte ---
    # Verifie que le KPI est bien borne par date_eligibilite et pas par le flux.
    print("\n--- 5. Periode etroite : la seule journee J-6 ---")
    j6 = today - timedelta(days=6)
    stats_j6 = db.fetch_tp_stats(conn, debut=j6, fin=j6)
    if stats_j6 is None:
        rap.ko.append("fetch_tp_stats(J-6) a renvoye None")
        print("  [ECHEC] fetch_tp_stats(J-6) a renvoye None")
    else:
        rap.check("Cohorte", stats_j6["Cohorte"], j6.strftime("%Y-%m-%d"))
        # Cas 16, 17, 18 : trois flux, une seule echeance.
        rap.check("Population_Attendue (3 flux)", stats_j6["Population_Attendue"], 3)
        rap.check("Trouvees_GED", stats_j6["Trouvees_GED"]["Nombre"], 1)
        rap.check("En_Attente_GED", stats_j6["En_Attente_GED"]["Nombre"], 2)
        # Une seule carte trouvee, a 2 jours. Les deux manquantes ne doivent pas
        # tirer la moyenne vers 0 : avec le bug, AVG valait (2+0+0)/3 = 0,7.
        rap.check("Delai Moyenne (2 manquantes)",
                  stats_j6["Distribution_Delai"]["Moyenne"], 2.0)

    # --- 6. NON-REGRESSION : une carte manquante n'est pas un delai de 0 ---
    #
    # `GREATEST` ignore les NULL en PostgreSQL, donc `_DELAI` rendait 0 pour une
    # carte jamais trouvee. Les agregats MIN/MAX/AVG l'avalaient comme une
    # livraison parfaite : plus il manquait de cartes, meilleur paraissait le
    # delai. Les deux periodes ci-dessous echouaient avant le correctif.
    print("\n--- 6. Non-regression : delai des cartes NON trouvees ---")

    # (a) La periode de la copie d'ecran metier : 2 trouvees (0 et 1 jour),
    #     2 manquantes. Attendu 0,5 ; le bug affichait 0,2.
    stats_ecran = db.fetch_tp_stats(conn, debut=today - timedelta(days=2),
                                    fin=today + timedelta(days=24))
    if stats_ecran is None:
        rap.ko.append("fetch_tp_stats(periode ecran) a renvoye None")
        print("  [ECHEC] fetch_tp_stats(periode ecran) a renvoye None")
    else:
        d = stats_ecran["Distribution_Delai"]
        rap.check("2 trouvees / 2 manquantes : base", d["Base"], 2)
        rap.check("2 trouvees / 2 manquantes : moy", d["Moyenne"], 0.5)
        rap.check("moyenne = mediane ici", d["Moyenne"], d["Median"])

    # (b) Cas extreme : une periode ou AUCUNE carte n'est trouvee (cas 6 et 7).
    #     Sans mesure, les quatre statistiques doivent etre None. Le bug
    #     affichait 0/0/0 — lisible comme une performance parfaite.
    j20 = today - timedelta(days=20)
    stats_vide = db.fetch_tp_stats(conn, debut=j20, fin=j20)
    if stats_vide is None:
        rap.ko.append("fetch_tp_stats(J-20) a renvoye None")
        print("  [ECHEC] fetch_tp_stats(J-20) a renvoye None")
    else:
        rap.check("aucune trouvee : Population_Due", stats_vide["Population_Due"], 2)
        rap.check("aucune trouvee : Trouvees_GED",
                  stats_vide["Trouvees_GED"]["Nombre"], 0)
        d = stats_vide["Distribution_Delai"]
        rap.check("aucune trouvee : Min/Max/Med/Moy",
                  [d["Min"], d["Max"], d["Median"], d["Moyenne"]],
                  [None, None, None, None])

    total = rap.ok + len(rap.ko)
    print(f"\n=== {rap.ok}/{total} verifications OK ===")
    if rap.ko:
        print("Echecs : " + ", ".join(rap.ko))
    return not rap.ko


# =============================================================================
# MAIN
# =============================================================================

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    p.add_argument("--apply", action="store_true", help="insere le jeu d'essai puis verifie")
    p.add_argument("--check", action="store_true", help="verifie sans reinserer")
    p.add_argument("--clean", action="store_true", help="supprime le jeu d'essai")
    args = p.parse_args()

    if not (args.apply or args.check or args.clean):
        print(__doc__)
        print(f"\nDry-run : {len(_CAS)} lignes seraient inserees dans {db.FQTN}.")
        print("Relancer avec --apply pour ecrire, --clean pour supprimer.")
        for c in _CAS:
            print(f"  #{c['n']:02d} {c['attendu']}")
        return 0

    conn = db.connect_supervision()
    if conn is None:
        print("[ECHEC] connexion a la base supervision impossible.")
        return 2

    try:
        if not db.ensure_table(conn):
            print(f"[ECHEC] {db.FQTN} indisponible.")
            return 2

        avant = count_reelles(conn)
        print(f"Lignes REELLES en table (hors jeu d'essai) : {avant}")

        if args.clean:
            clean(conn)
            return 0

        today = _today(conn)
        if args.apply:
            n = seed(conn, today)
            print(f"Insere : {n} ligne(s) de test dans {db.FQTN}.")

        succes = verifier(conn, today)

        apres = count_reelles(conn)
        if apres != avant:
            print(f"\n[ALERTE] les lignes reelles ont change : {avant} -> {apres}")
            return 3
        print(f"Lignes reelles inchangees ({apres}). "
              f"Nettoyage : python Scripts/seed_test_carte_tp.py --clean")
        return 0 if succes else 1
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    raise SystemExit(main())
