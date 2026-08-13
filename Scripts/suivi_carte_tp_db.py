# -*- coding: utf-8 -*-
"""
suivi_carte_tp_db.py — Accès à la table d'état des cartes tiers payant attendues.

Problème résolu
---------------
Le contrôle GED partait du flux du jour (`{PREFIX}_New_S.csv`). Deux populations
en sortaient sans jamais y revenir :

  1. Les cartes ÉLIGIBLES PLUS TARD. Le `New_S` est un DIFFÉRENTIEL, pas une
     photo : vérifié sur les 10 flux disponibles, deux flux consécutifs n'ont
     AUCUNE personne en commun (54 personnes sur 47 721 apparaissent dans plus
     d'un flux, soit 0,1 %, et ce sont des seconds contrats). Une carte éligible
     au 15 août vue dans le flux du 25 juillet n'est donc jamais revue : si on ne
     la conserve pas, elle est perdue définitivement.
  2. Les personnes ABSENTES D'IEHE. Sans KPEP IEHE, aucune recherche GED n'est
     possible — la GED est indexée sur ce KPEP. Elles étaient abandonnées en
     silence, ce qui fausse le dénominateur (633 cartes attendues, 614 comptées).

`rptpsc.suivi_carte_tp` devient l'ÉTAT VIVANT : une ligne = une carte attendue,
conservée jusqu'à ce qu'elle soit trouvée en GED.

Deux axes d'état indépendants, chacun une date nullable
-------------------------------------------------------
Aucune colonne `statut`, aucun booléen. Même convention que `suivi_iehe_db` :
une date NULLE *est* le « pas encore trouvé », donc le fait et sa date ne
peuvent jamais se contredire.

  - `kpep_iehe` / `date_found_iehe` : la personne existe-t-elle dans IEHE ?
  - `date_found_ged`                : la carte est-elle sortie en GED ?

Les trois cas énoncés par le métier se lisent sur ces colonnes, sans en ajouter :

  | carte en GED             | date_found_ged IS NOT NULL                      |
  | absente d'IEHE           | kpep_iehe IS NULL                               |
  | éligible plus tard       | date_eligibilite > CURRENT_DATE                 |
  | éligible, carte manquante| kpep_iehe IS NOT NULL AND date_found_ged IS NULL
                               AND date_eligibilite <= CURRENT_DATE            |

Pourquoi PAS de colonne `carte_future`
--------------------------------------
Elle serait redondante ET fausse avec le temps. Mesuré sur les flux réels,
`delta(effet, adhésion) > 21j` et `date_eligibilite > date_flux` renvoient des
comptes IDENTIQUES (42 = 42 sur le flux du 16/04, 190 = 190 sur celui du 16/07) —
c'est une identité algébrique, `date_adhesion` valant la date du flux pour toutes
les lignes. Surtout, un booléen figé dirait encore « Futur » le 15 août pour une
carte devenue due ce matin-là. `date_eligibilite > CURRENT_DATE` reste juste.

Producteurs / consommateurs
---------------------------
  - 03_generation_fichiers_detail.py : `upsert_cartes()` — TOUTES les cartes
    `Eligibilité TP = O`, cartes futures COMPRISES
  - 06_iehe_retry.py                 : `sync_kpep_from_iehe()`
  - scriptsNewPipline/07             : `fetch_sans_kpep()` → `set_kpep_iehe()`
                                       → `fetch_ged_pending()` → `mark_ged_found()`
                                       → `apply_corrections()`
  - scriptsNewPipline/08             : `mark_ged_found()`
  - 02_calcul_kpi.py                 : `fetch_tp_stats()`, `fetch_controle_stats()`

Robustesse : aucune fonction ne lève. Base injoignable ⇒ log + valeur neutre,
le reste du pipeline CSV continue de tourner.

Note d'implémentation : contrairement à `suivi_iehe_db`, dont les colonnes
reproduisent les entêtes CSV accentuées et DOIVENT donc être quotées, toutes les
colonnes de cette table sont en minuscules ASCII sans espace. Aucun quoting n'est
nécessaire, ce qui rend le SQL lisible. Ne pas introduire de colonne accentuée
ici sans rétablir le quoting systématique.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

try:
    import psycopg
except ImportError:  # pragma: no cover - dépend de l'environnement d'exécution
    psycopg = None

# `_clean_text` / `_clean_date` sont RÉUTILISÉS tels quels : ils portent une
# garantie non triviale (jamais laisser PostgreSQL parser une date française —
# avec le DateStyle par défaut `ISO, MDY`, '07/05/2026' devient le 5 juillet).
# Les redéfinir ici ferait diverger les deux tables au premier correctif.
try:
    from suivi_iehe_db import (  # type: ignore
        _clean_date,
        _clean_text,
        connect_supervision,
        SUIVI_SCHEMA,
    )
except ImportError:  # pragma: no cover - exécution hors PYTHONPATH Scripts/
    _clean_date = None  # type: ignore
    _clean_text = None  # type: ignore
    connect_supervision = None  # type: ignore
    SUIVI_SCHEMA = os.getenv("PG_SUPERVISION_SCHEMA", "rptpsc")


SUIVI_TABLE = "suivi_carte_tp"
FQTN = f"{SUIVI_SCHEMA}.{SUIVI_TABLE}"

# Table source du KPEP IEHE pour `sync_kpep_from_iehe()`.
IEHE_FQTN = f"{SUIVI_SCHEMA}.suivi_iehe"

# Seuil contractuel : une carte doit être en GED dans les N jours. MGEN est
# pénalisée au-delà (appels d'offres). Exposé ici pour que le KPI et le seuil
# du dashboard ne puissent pas diverger.
DELAI_CONTRACTUEL_JOURS = 4

# Motifs — vocabulaire fermé, écrit par 06/07.
MOTIF_ABSENT_IEHE = "absent_iehe"
MOTIF_MATCH_SOCIETE = "match_societe"
MOTIF_FALLBACK = "fallback_autre_societe"
MOTIF_CORRECTION = "correction_manuelle"


def _fallback_clean_text(value: Any) -> Optional[str]:
    """Repli si `suivi_iehe_db` n'est pas importable. Cf. l'original."""
    if value is None:
        return None
    try:
        if value != value:  # NaN / NaT ne s'égalent pas à eux-mêmes
            return None
    except Exception:  # noqa: BLE001
        pass
    text = str(value).strip()
    if not text or text.lower() in ("nan", "nat", "none", "null"):
        return None
    return text


_FALLBACK_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d%m%Y")


def _fallback_clean_date(value: Any) -> Optional[date]:
    """Repli si `suivi_iehe_db` n'est pas importable. Cf. l'original."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = _fallback_clean_text(value)
    if text is None:
        return None
    text = text.split(" ")[0].split("T")[0]
    for fmt in _FALLBACK_DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


if _clean_text is None:  # pragma: no cover
    _clean_text = _fallback_clean_text
if _clean_date is None:  # pragma: no cover
    _clean_date = _fallback_clean_date


if connect_supervision is None:  # pragma: no cover
    # Repli utilisé uniquement si `suivi_iehe_db` n'est pas importable.
    #
    # Pas de mot de passe par défaut : il DOIT venir de l'environnement. Les
    # modules plus anciens du dépôt écrivent un identifiant de repli en dur
    # (`os.getenv(..., "rptpsc_xx")`) — ne pas propager ce motif. Sans la
    # variable, on échoue proprement et le pipeline CSV continue de tourner.
    _SUP_HOST = os.getenv("PG_SUPERVISION_HOST", "")
    _SUP_PORT = os.getenv("PG_SUPERVISION_PORT", "5577")
    _SUP_DB = os.getenv("PG_SUPERVISION_DB", "supervisionpsc_db")
    _SUP_USER = os.getenv("PG_SUPERVISION_USER", "")
    _SUP_PASSWORD = os.getenv("PG_SUPERVISION_PASSWORD", "")

    def connect_supervision():  # type: ignore[misc]
        """Connexion à la base supervision. Retourne `None` en cas d'échec."""
        if psycopg is None:
            print("   [WARN] suivi_carte_tp : module 'psycopg' absent.")
            return None
        if not (_SUP_HOST and _SUP_USER and _SUP_PASSWORD):
            print("   [WARN] suivi_carte_tp : identifiants supervision absents de "
                  "l'environnement (PG_SUPERVISION_HOST / _USER / _PASSWORD).")
            return None
        try:
            return psycopg.connect(
                host=_SUP_HOST, port=_SUP_PORT, dbname=_SUP_DB,
                user=_SUP_USER, password=_SUP_PASSWORD, connect_timeout=5,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"   [WARN] suivi_carte_tp : connexion supervision impossible : {exc}")
            return None


def _clean_key(value: Any) -> str:
    """Composant de clé primaire : jamais NULL.

    Les trois colonnes de la PK sont NOT NULL. `_clean_text` renvoie None sur une
    chaîne vide, ce qui ferait échouer l'INSERT — et donc, avec `executemany`, le
    lot ENTIER. Une société ou une offre vide est une donnée légitime : on la
    stocke telle quelle, chaîne vide.
    """
    text = _clean_text(value)
    return text if text is not None else ""


# =============================================================================
# SCHÉMA
# =============================================================================

# Schéma de la table : 15 colonnes, dans cet ordre. DDL et INSERT en sont TOUS
# deux dérivés — seule façon d'empêcher liste de colonnes, placeholders et ordre
# des valeurs de se désynchroniser silencieusement.
_COLUMNS: Sequence[Tuple[str, str]] = (
    # --- clé : une carte = une personne × une société × une offre ---
    ("num_personne",        "TEXT NOT NULL"),
    ("code_soc_appart",     "TEXT NOT NULL"),
    ("offre",               "TEXT NOT NULL"),
    # --- provenance ---
    ("flux_id",             "TEXT"),
    ("date_premiere_vue",   "DATE"),
    # --- attributs de la carte ---
    ("date_adhesion",       "DATE"),
    ("date_effet_adhesion", "DATE"),
    ("type_assure",         "TEXT"),
    ("date_eligibilite",    "DATE"),
    # --- axe 1 : présence IEHE ---
    ("kpep_iehe",           "TEXT"),
    ("date_found_iehe",     "DATE"),
    # --- axe 2 : présence GED ---
    ("date_found_ged",      "DATE"),
    ("date_dispo_ged",      "DATE"),
    # --- traçabilité ---
    ("date_derniere_verif", "DATE"),
    ("motif",               "TEXT"),
)

# `flux_id` est DÉLIBÉRÉMENT hors de la clé primaire. Les deux tables existantes
# (`suivi_iehe`, `suivi_tp_ged`) sont clés sur (flux_id, X) ; reproduire ce choix
# ici donnerait DEUX lignes à une personne vue dans deux flux et gonflerait le
# compte des « cartes attendues » — précisément le chiffre que le métier vérifie.
#
# Mesuré sur le flux du 16/04 : 11 415 lignes éligibles se réduisent à 11 365
# clés distinctes. Les 50 doublons sont deux contrats portant la MÊME carte ; les
# fondre est le comportement voulu.
_PK = ("num_personne", "code_soc_appart", "offre")

# `date_eligibilite` et `date_premiere_vue` sont NULLABLES à dessein, alors que
# le métier les veut toujours renseignées. Un NOT NULL ferait échouer l'INSERT —
# donc, avec `executemany`, le lot entier de 11 000 lignes — sur une seule date
# d'effet illisible. On préfère stocker la ligne avec la date à NULL : elle reste
# comptée, `fetch_ged_pending()` l'ignore (NULL <= CURRENT_DATE est faux) et le
# KPI la remonte dans `Sans_Date_Eligibilite` plutôt que de la perdre en silence.
_DDL = (
    f"CREATE TABLE IF NOT EXISTS {FQTN} (\n    "
    + ",\n    ".join(f"{name:<20} {sqltype}" for name, sqltype in _COLUMNS)
    + f",\n    PRIMARY KEY ({', '.join(_PK)})\n)"
)

_DDL_INDEXES = (
    # Scan quotidien des cartes à rechercher : ne lit que la fraction non soldée.
    f"CREATE INDEX IF NOT EXISTS idx_{SUIVI_TABLE}_ged_pending "
    f"ON {FQTN} (date_eligibilite) WHERE date_found_ged IS NULL",
    # `mark_ged_found` et `fetch_ged_pending` travaillent par KPEP, pas par PK.
    f"CREATE INDEX IF NOT EXISTS idx_{SUIVI_TABLE}_kpep "
    f"ON {FQTN} (kpep_iehe)",
    # Résolution du KPEP manquant (script 07) et relance IEHE.
    f"CREATE INDEX IF NOT EXISTS idx_{SUIVI_TABLE}_sans_kpep "
    f"ON {FQTN} (num_personne) WHERE kpep_iehe IS NULL",
    # Regroupement mensuel du KPI.
    f"CREATE INDEX IF NOT EXISTS idx_{SUIVI_TABLE}_eligibilite "
    f"ON {FQTN} (date_eligibilite)",
)


# Script de migration à lancer quand la table déployée porte un schéma antérieur.
MIGRATION_SQL = "Input_Data/SQL/migration_suivi_carte_tp.sql"


def _colonnes_manquantes(conn) -> Optional[List[str]]:
    """Colonnes de `_COLUMNS` absentes de la table déployée.

    Retourne `[]` si le schéma est conforme, `None` si la table n'existe pas
    encore (cas normal : le DDL va la créer) ou si l'introspection échoue.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = %s",
                (SUIVI_SCHEMA, SUIVI_TABLE),
            )
            presentes = {str(r[0]) for r in cur.fetchall()}
    except Exception:  # noqa: BLE001
        _rollback(conn)
        return None
    if not presentes:
        return None
    return [c for c, _ in _COLUMNS if c not in presentes]


def ensure_table(conn) -> bool:
    """Crée table + index s'ils n'existent pas. Retourne `False` si échec.

    ⚠️ `CREATE TABLE IF NOT EXISTS` ne migre RIEN : si une table `suivi_carte_tp`
    subsiste avec un schéma antérieur, le DDL est un no-op silencieux et tous les
    INSERT échoueront ensuite sur des colonnes absentes. Un changement de
    `_COLUMNS` impose donc un `DROP TABLE {FQTN}` manuel préalable, suivi d'un
    rejeu du script 03 sur les flux à recharger.

    Ce cas est DÉTECTÉ ici plutôt que laissé au premier INSERT venu. Sans ce
    contrôle, l'échec remontait sous la forme « création de rptpsc.suivi_carte_tp
    impossible : column "kpep_iehe" does not exist » — un message trompeur, car
    la table existe et la création n'a rien à voir : c'est la création d'un INDEX
    sur une colonne d'un autre schéma qui échoue. On nomme donc les colonnes
    manquantes et le script de migration à lancer.
    """
    if conn is None:
        return False

    manquantes = _colonnes_manquantes(conn)
    if manquantes:
        print(f"   [ERREUR] {FQTN} existe avec un SCHÉMA ANTÉRIEUR : "
              f"{len(manquantes)} colonne(s) manquante(s) — {', '.join(manquantes)}.")
        print(f"            La table n'est pas migrée automatiquement (des données "
              f"y sont déjà). Lancez : {MIGRATION_SQL}")
        return False

    try:
        with conn.cursor() as cur:
            cur.execute(_DDL)
            for ddl in _DDL_INDEXES:
                cur.execute(ddl)
        conn.commit()
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"   [WARN] suivi_carte_tp : création de {FQTN} impossible : {exc}")
        _rollback(conn)
        return False


def _rollback(conn) -> None:
    try:
        conn.rollback()
    except Exception:  # noqa: BLE001
        pass


# =============================================================================
# ÉCRITURE — script 03
# =============================================================================

# Spécification UNIQUE de l'upsert : (colonne, coercion, rafraîchie sur conflit).
#
# `refresh=False` sur tout ce qui porte une DÉCOUVERTE : un rejeu du script 03
# sur un flux déjà chargé ne doit effacer ni ce que les retries ont trouvé
# (`kpep_iehe`, `date_found_*`, `motif`), ni la provenance d'origine (`flux_id`,
# `date_premiere_vue`). `date_premiere_vue` en particulier sert d'ancre au délai
# contractuel : la réécrire décalerait le point de départ du chrono à chaque
# rejeu et rendrait le KPI non reproductible.
_UPSERT_SPEC: Sequence[Tuple[str, Any, bool]] = (
    ("num_personne",        _clean_key,  False),
    ("code_soc_appart",     _clean_key,  False),
    ("offre",               _clean_key,  False),
    ("flux_id",             _clean_text, False),
    ("date_premiere_vue",   _clean_date, False),
    ("date_adhesion",       _clean_date, True),
    ("date_effet_adhesion", _clean_date, True),
    ("type_assure",         _clean_text, True),
    ("date_eligibilite",    _clean_date, True),
    ("kpep_iehe",           _clean_text, False),
    ("date_found_iehe",     _clean_date, False),
    ("date_found_ged",      _clean_date, False),
    ("date_dispo_ged",      _clean_date, False),
    ("date_derniere_verif", _clean_date, True),
    ("motif",               _clean_text, False),
)

_UPSERT_SQL = f"""
    INSERT INTO {FQTN} ({", ".join(c for c, _, _ in _UPSERT_SPEC)})
    VALUES ({", ".join(["%s"] * len(_UPSERT_SPEC))})
    ON CONFLICT ({", ".join(_PK)}) DO UPDATE SET
        {", ".join(f"{c} = EXCLUDED.{c}" for c, _, upd in _UPSERT_SPEC if upd)}
"""


def upsert_cartes(
    conn,
    flux_id: str,
    flux_date: Optional[date],
    rows: Sequence[Dict[str, Any]],
) -> int:
    """Insère les cartes attendues révélées par ce flux.

    `rows` : dicts portant `num_personne` (obligatoire) et, optionnellement, les
    autres clés de `_UPSERT_SPEC`. L'appelant doit avoir DÉJÀ filtré sur
    `Eligibilité TP = O` — et surtout AVOIR CONSERVÉ LES CARTES FUTURES, qui sont
    la raison d'être de la table.

    `flux_id` / `flux_date` renseignent la provenance ; ils ne sont posés qu'à
    la première insertion (cf. `refresh=False`).

    Retourne le nombre de lignes envoyées (0 si indisponible).
    """
    if conn is None or not rows or not flux_id:
        return 0

    flux = str(flux_id).strip()
    fdate = _clean_date(flux_date) or _clean_date(flux) or date.today()

    payload: List[tuple] = []
    seen: set = set()
    sans_date = 0

    for r in rows:
        pid = _clean_text(r.get("num_personne"))
        if pid is None:
            continue
        key = (pid, _clean_key(r.get("code_soc_appart")), _clean_key(r.get("offre")))
        if key in seen:
            # Deux contrats pour la même carte : la PK les fondrait de toute
            # façon, on évite juste un aller-retour inutile.
            continue
        seen.add(key)

        enriched = {
            **r,
            "flux_id": flux,
            "date_premiere_vue": fdate,
            "date_derniere_verif": fdate,
        }
        if _clean_date(r.get("date_eligibilite")) is None:
            sans_date += 1
        payload.append(tuple(coerce(enriched.get(col)) for col, coerce, _ in _UPSERT_SPEC))

    if not payload:
        # Silence = piège : sans ce message, un mapping de colonnes erroné côté
        # script 03 ressemble à un flux sans aucune carte éligible.
        print(f"   [WARN] suivi_carte_tp : flux {flux} — {len(rows)} ligne(s) reçue(s) "
              f"mais aucune clé 'num_personne' exploitable, rien n'est inséré.")
        return 0

    try:
        with conn.cursor() as cur:
            cur.executemany(_UPSERT_SQL, payload)
        conn.commit()
        if sans_date:
            print(f"   [WARN] suivi_carte_tp : {sans_date} carte(s) sans date "
                  f"d'éligibilité calculable (date d'effet absente ou illisible). "
                  f"Conservées, comptées dans 'Sans_Date_Eligibilite', hors recherche GED.")
        return len(payload)
    except Exception as exc:  # noqa: BLE001
        print(f"   [WARN] suivi_carte_tp : insertion des cartes du flux {flux} "
              f"échouée : {exc}")
        _rollback(conn)
        return 0


# =============================================================================
# AXE 1 — PRÉSENCE IEHE (scripts 06 et 07)
# =============================================================================

def sync_kpep_from_iehe(conn) -> int:
    """Recopie le KPEP trouvé par le retry IEHE dans la table des cartes.

    C'est l'automatisation du geste manuel décrit par le métier : « récupérer le
    KPEP qui vient d'être créé pour relancer ». Le script 06 tournant AVANT le 07
    dans le pipeline, une personne créée dans IEHE ce matin devient recherchable
    en GED le jour même, et non à J+1.

    Le garde `kpep_iehe IS NULL` préserve un KPEP déjà résolu par le script 07
    (résolution par société, plus précise que celle du retry).

    Retourne le nombre de cartes mises à jour.
    """
    if conn is None:
        return 0
    sql = f"""
        UPDATE {FQTN} AS c
           SET kpep_iehe       = s.kpep,
               date_found_iehe = s.date_found,
               motif           = CASE WHEN c.motif = %s THEN NULL ELSE c.motif END
          FROM (
                SELECT "NS_num_personne"           AS num_personne,
                       MAX(NULLIF(TRIM("KPEP_IEHE"), '')) AS kpep,
                       MIN("date_found")           AS date_found
                  FROM {IEHE_FQTN}
                 WHERE "date_found" IS NOT NULL
                   AND NULLIF(TRIM("KPEP_IEHE"), '') IS NOT NULL
                 GROUP BY "NS_num_personne"
               ) AS s
         WHERE c.num_personne = s.num_personne
           AND c.kpep_iehe IS NULL
    """
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (MOTIF_ABSENT_IEHE,))
            updated = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        conn.commit()
        return int(updated)
    except Exception as exc:  # noqa: BLE001
        print(f"   [WARN] suivi_carte_tp : synchronisation des KPEP IEHE échouée : {exc}")
        _rollback(conn)
        return 0


def fetch_sans_kpep(conn) -> List[Tuple[str, str, str]]:
    """Cartes dont le KPEP IEHE reste à résoudre.

    Sans cette étape, `fetch_ged_pending()` (qui exige `kpep_iehe IS NOT NULL`)
    ne renverrait JAMAIS rien : le script 03 insère les cartes sans KPEP, il
    n'a pas l'index IEHE par société. Le script 07 résout ces lignes avec
    `build_iehe_kpep_index` / `resolve_kpep_ref` puis appelle `set_kpep_iehe()`.

    Retourne une liste de (num_personne, code_soc_appart, offre).
    """
    if conn is None:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT num_personne, code_soc_appart, offre FROM {FQTN} "
                f"WHERE kpep_iehe IS NULL AND date_found_ged IS NULL"
            )
            return [(str(a or ""), str(b or ""), str(c or "")) for a, b, c in cur.fetchall()]
    except Exception as exc:  # noqa: BLE001
        print(f"   [WARN] suivi_carte_tp : lecture des cartes sans KPEP échouée : {exc}")
        return []


def set_kpep_iehe(conn, rows: Iterable[Tuple[str, str, str, str, str]], day: Optional[date] = None) -> int:
    """Pose le KPEP IEHE résolu et son motif.

    `rows` : (num_personne, code_soc_appart, offre, kpep, motif). Un `kpep` vide
    ne pose QUE le motif — c'est le cas `absent_iehe`, où l'on doit garder la
    trace du « pourquoi » sans inventer de KPEP.

    Le garde `kpep_iehe IS NULL` empêche d'écraser une résolution antérieure.
    """
    if conn is None:
        return 0
    day = day or date.today()

    payload = []
    for num_pers, soc, offre, kpep, motif in rows:
        pid = _clean_text(num_pers)
        if pid is None:
            continue
        payload.append((
            _clean_text(kpep),
            _clean_text(motif),
            day,
            pid, _clean_key(soc), _clean_key(offre),
        ))
    if not payload:
        return 0

    sql = f"""
        UPDATE {FQTN}
           SET kpep_iehe           = COALESCE(%s, kpep_iehe),
               motif               = COALESCE(%s, motif),
               date_derniere_verif = %s
         WHERE num_personne    = %s
           AND code_soc_appart = %s
           AND offre           = %s
           AND kpep_iehe IS NULL
    """
    try:
        with conn.cursor() as cur:
            cur.executemany(sql, payload)
            updated = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        conn.commit()
        return int(updated)
    except Exception as exc:  # noqa: BLE001
        print(f"   [WARN] suivi_carte_tp : pose des KPEP IEHE échouée : {exc}")
        _rollback(conn)
        return 0


# =============================================================================
# AXE 2 — PRÉSENCE GED (scripts 07 et 08)
# =============================================================================

def fetch_ged_pending(conn) -> List[str]:
    """KPEP à interroger en GED aujourd'hui — LE remplaçant du flux du jour.

    Trois conditions, pas deux :

      1. `date_eligibilite <= CURRENT_DATE` — la carte est due. Les cartes
         futures restent en base et se présenteront d'elles-mêmes le jour venu.
      2. `date_found_ged IS NULL` — pas encore trouvée. C'est ce qui apporte
         « les affiliations du jour PLUS l'antériorité » : aucune union à écrire,
         le script 03 a déjà inséré les cartes du jour avant que 07 ne tourne.
      3. `kpep_iehe IS NOT NULL` — la GED est indexée sur le KPEP IEHE, pas sur
         la personne ni sur le KPEP d'affiliation. Une carte sans KPEP n'est PAS
         un problème GED : elle relève du retry IEHE, qui la résoudra.

    Retourne une liste dédoublonnée de KPEP.
    """
    if conn is None:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT DISTINCT kpep_iehe FROM {FQTN} "
                f"WHERE date_eligibilite <= CURRENT_DATE "
                f"  AND date_found_ged IS NULL "
                f"  AND kpep_iehe IS NOT NULL "
                f"  AND kpep_iehe <> ''"
            )
            return [str(r[0]).strip() for r in cur.fetchall() if r[0]]
    except Exception as exc:  # noqa: BLE001
        print(f"   [WARN] suivi_carte_tp : lecture des cartes à rechercher échouée : {exc}")
        return []


def fetch_min_eligibilite_pending(conn) -> Optional[date]:
    """Plus ancienne `date_eligibilite` encore non soldée — borne basse de la
    fenêtre `tmstinj` des requêtes GED.

    Mêmes TROIS conditions que `fetch_ged_pending()` : c'est exactement la
    population que la requête doit couvrir. Une fenêtre plus courte ferait sortir
    du champ les cartes rétroactives, qui ne seraient alors PLUS JAMAIS retrouvées.

    Extraite ici parce que les scripts 07 et 08 en ont tous deux besoin : le 08
    l'avait en SQL inline, le 07 calculait une fenêtre glissante différente. Deux
    copies divergeraient au premier correctif.

    Retourne `None` si plus aucune carte n'est en attente (l'appelant retombe
    alors sur la journée courante).
    """
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT MIN(date_eligibilite) FROM {FQTN} "
                f"WHERE date_eligibilite <= CURRENT_DATE "
                f"  AND date_found_ged IS NULL "
                f"  AND kpep_iehe IS NOT NULL AND kpep_iehe <> ''"
            )
            row = cur.fetchone()
            return _clean_date(row[0]) if row else None
    except Exception as exc:  # noqa: BLE001
        print(f"   [WARN] suivi_carte_tp : lecture de la plus ancienne éligibilité "
              f"échouée : {exc}")
        # Rollback OBLIGATOIRE même en lecture : une requête en erreur laisse la
        # transaction PostgreSQL en état « aborted », et TOUTE requête suivante sur
        # la même connexion échoue avec « current transaction is aborted » — donc
        # avec un message qui masque la vraie cause. Sans lui, un simple SELECT raté
        # ici fait échouer tout le calcul KPI derrière.
        _rollback(conn)
        return None


def fetch_ged_pending_detail(conn) -> List[Dict[str, Any]]:
    """Même population que `fetch_ged_pending`, avec le contexte de chaque carte.

    Utile au script 07 pour écrire le CSV de détail et ventiler par société sans
    relire le `New_S` — le backlog vient de flux antérieurs dont le CSV n'est
    plus forcément là.
    """
    if conn is None:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT num_personne, code_soc_appart, offre, kpep_iehe, "
                f"       date_eligibilite, date_premiere_vue, flux_id, type_assure, motif "
                f"  FROM {FQTN} "
                f" WHERE date_eligibilite <= CURRENT_DATE "
                f"   AND date_found_ged IS NULL "
                f"   AND kpep_iehe IS NOT NULL AND kpep_iehe <> ''"
            )
            cols = ("num_personne", "code_soc_appart", "offre", "kpep_iehe",
                    "date_eligibilite", "date_premiere_vue", "flux_id",
                    "type_assure", "motif")
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as exc:  # noqa: BLE001
        print(f"   [WARN] suivi_carte_tp : lecture détaillée des cartes échouée : {exc}")
        return []


def mark_ged_found(
    conn,
    trouvees: Iterable[Any],
    day: Optional[date] = None,
) -> int:
    """Solde les cartes désormais présentes en GED.

    `trouvees` : soit des KPEP nus, soit des couples `(kpep, date_dispo_ged)`.
    `date_dispo_ged` est le `tmstinj` rendu par l'export GED — la date à laquelle
    le document a réellement été injecté. Elle répond à la question métier
    « date de génération ou date de mise à disposition ? » : `date_found_ged`
    reste NOTRE date de détection, `date_dispo_ged` est celle de la GED.

    Volontairement SANS filtre de flux : une carte trouvée est trouvée, quel que
    soit le flux qui l'a révélée. Même choix que `08_ged_retry.py`.

    Le garde `date_found_ged IS NULL` préserve la date de PREMIÈRE découverte —
    c'est aussi ce qui rendra possible, plus tard, un historique « trouvée puis
    reperdue » sans rien redéfaire.
    """
    if conn is None:
        return 0
    day = day or date.today()

    payload = []
    for item in trouvees:
        if isinstance(item, (tuple, list)):
            kpep, dispo = (list(item) + [None])[:2]
        else:
            kpep, dispo = item, None
        k = _clean_text(kpep)
        if k is None:
            continue
        payload.append((day, _clean_date(dispo), day, k))
    if not payload:
        return 0

    sql = f"""
        UPDATE {FQTN}
           SET date_found_ged      = %s,
               date_dispo_ged      = COALESCE(%s, date_dispo_ged),
               date_derniere_verif = %s
         WHERE kpep_iehe = %s
           AND date_found_ged IS NULL
    """
    try:
        with conn.cursor() as cur:
            cur.executemany(sql, payload)
            updated = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        conn.commit()
        return int(updated)
    except Exception as exc:  # noqa: BLE001
        print(f"   [WARN] suivi_carte_tp : solde des cartes trouvées échoué : {exc}")
        _rollback(conn)
        return 0


def mark_checked(conn, day: Optional[date] = None) -> int:
    """Horodate la tentative sur les cartes restées introuvables.

    Sans ça on ne distingue pas « jamais recherchée » de « recherchée, toujours
    absente ».
    """
    if conn is None:
        return 0
    day = day or date.today()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE {FQTN} SET date_derniere_verif = %s "
                f"WHERE date_found_ged IS NULL AND date_eligibilite <= CURRENT_DATE",
                (day,),
            )
            updated = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        conn.commit()
        return int(updated)
    except Exception as exc:  # noqa: BLE001
        print(f"   [WARN] suivi_carte_tp : horodatage des cartes restantes échoué : {exc}")
        _rollback(conn)
        return 0


# =============================================================================
# CORRECTIONS MANUELLES — script 07
# =============================================================================

def apply_corrections(
    conn,
    corrections: Dict[Tuple[str, str, str], Tuple[str, str]],
    day: Optional[date] = None,
) -> Dict[str, int]:
    """Applique les décisions manuelles saisies dans `TP_GED_Corrections.csv`.

    Le métier retrouve des cartes à la main et veut que l'indicateur soit juste
    le soir même : ces décisions doivent donc modifier l'ÉTAT, pas seulement une
    colonne d'affichage d'un CSV.

      - `FORCE_RAPPROCHE`     → pose `date_found_ged` (si elle est encore vide)
      - `FORCE_NON_RAPPROCHE` → efface `date_found_ged` et `date_dispo_ged`

    `corrections` : clé (kpep, code_societe, offre) — société et/ou offre vides
    valent « toutes ». Même précédence que `apply_correction()` du script 07 :
    la clé la plus spécifique gagne, ce qui est obtenu ici en appliquant les
    corrections de la moins spécifique à la plus spécifique.

    Retourne {"rapproches": n, "non_rapproches": n}.
    """
    if conn is None or not corrections:
        return {"rapproches": 0, "non_rapproches": 0}
    day = day or date.today()

    # Du plus général au plus spécifique : la dernière écriture l'emporte, ce qui
    # reproduit la précédence de clé sans avoir à trier les cartes une à une.
    def _specificite(item) -> int:
        (_, soc, offre), _ = item
        return bool(soc) + bool(offre)

    nb_ok = nb_ko = 0
    try:
        with conn.cursor() as cur:
            for (kpep, soc, offre), (decision, _commentaire) in sorted(
                corrections.items(), key=_specificite
            ):
                k = _clean_text(kpep)
                if k is None:
                    continue
                filtres = ["kpep_iehe = %s"]
                params: List[Any] = []
                if decision == "FORCE_RAPPROCHE":
                    sql_set = ("date_found_ged = COALESCE(date_found_ged, %s), "
                               "motif = %s, date_derniere_verif = %s")
                    params += [day, MOTIF_CORRECTION, day]
                elif decision == "FORCE_NON_RAPPROCHE":
                    sql_set = ("date_found_ged = NULL, date_dispo_ged = NULL, "
                               "motif = %s, date_derniere_verif = %s")
                    params += [MOTIF_CORRECTION, day]
                else:
                    continue
                params.append(k)
                if soc:
                    filtres.append("code_soc_appart = %s")
                    params.append(str(soc).strip())
                if offre:
                    filtres.append("offre = %s")
                    params.append(str(offre).strip().upper())

                cur.execute(
                    f"UPDATE {FQTN} SET {sql_set} WHERE {' AND '.join(filtres)}",
                    tuple(params),
                )
                touched = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
                if decision == "FORCE_RAPPROCHE":
                    nb_ok += touched
                else:
                    nb_ko += touched
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        print(f"   [WARN] suivi_carte_tp : application des corrections échouée : {exc}")
        _rollback(conn)
        return {"rapproches": 0, "non_rapproches": 0}

    return {"rapproches": nb_ok, "non_rapproches": nb_ko}


# =============================================================================
# STATISTIQUES — script 02
# =============================================================================

# Date de mise à disposition retenue pour le calcul du délai.
#
# `date_dispo_ged` porte le `tmstinj` remonté par l'export GED : c'est la date
# d'injection RÉELLE du document. `date_found_ged` n'est que le jour où NOUS avons
# constaté la présence — un pipeline lancé avec trois jours de retard ajoutait donc
# trois jours de délai à TOUTES les cartes, ce qui mesurait notre cadence
# d'exécution et non la performance GED.
#
# Le repli sur `date_found_ged` n'est pas cosmétique : les cartes soldées avant que
# `tmstinj` ne soit remonté par le template SQL ont `date_dispo_ged IS NULL`. Sans
# COALESCE elles sortiraient entièrement des statistiques de délai — la base
# rétrécirait sans que rien ne le signale.
#
# `::date` sur les DEUX termes, ce n'est pas de la ceinture-bretelles. `tmstinj`
# est un HORODATAGE en GED ; selon l'ancienneté de la table, `date_dispo_ged` est
# déclarée DATE (schéma de `_COLUMNS`) ou TIMESTAMP (schéma déployé plus ancien).
# Sans le cast, COALESCE remonte le type le plus large, `timestamp - date` rend un
# INTERVAL, et `GREATEST(interval, 0)` échoue — « GREATEST types interval and
# integer cannot be matched », qui fait retomber tout le bloc KPI à vide. Le cast
# rend l'expression indépendante du type déclaré, et le délai se compte en jours
# pleins, ce qui est exactement la définition métier.
_DATE_GED = "COALESCE(date_dispo_ged::date, date_found_ged::date)"

# Départ du chrono : la date d'éligibilité SEULE (règle métier).
#
# Plancher à 0 : un délai négatif n'a pas de sens métier et fausserait moyenne et
# médiane. Il survient quand la carte est déjà en GED avant d'être due (carte
# produite en avance), ou lors d'un rejeu qui redate une découverte. « Trouvée avant
# l'échéance » se lit donc 0 jour, ce qui est aussi la définition de `Le_Jour_Meme`.
_DELAI = f"GREATEST({_DATE_GED} - date_eligibilite, 0)"

# Variante ancrée au flux, conservée pour ARBITRAGE — pas pour le contractuel.
#
# `date_eligibilite` répond à « cette carte fait-elle partie de la population ? » :
# c'est une condition d'ENTRÉE, pas une échéance. Prise seule, elle rend en retard
# tout ce qui est rétroactif : au jour où le flux du 16/04 arrive, 43 452 cartes sur
# 43 519 sont DÉJÀ en retard de 5 à 30 jours, simplement parce que leur date d'effet
# est passée ; sur le flux du 16/07 le retard maximal atteint 3 870 jours.
#
# `Dans_les_4_jours_Depuis_Flux` conserve donc la mesure qui démarre au plus tard des
# deux dates — on ne peut pas être en retard sur une carte qu'on ne connaissait pas.
# C'est elle qui permet d'expliquer l'écart quand le taux principal s'effondre.
_DELAI_DEPUIS_FLUX = (
    f"GREATEST({_DATE_GED} - GREATEST(date_eligibilite, date_premiere_vue), 0)"
)


def _taux(nombre: int, base: int) -> Dict[str, Any]:
    return {
        "Nombre": int(nombre),
        "Taux": round(nombre / base * 100, 2) if base > 0 else 0.0,
    }


def fetch_dernier_flux(conn) -> Optional[Tuple[str, date]]:
    """`(flux_id, date du flux)` du dernier flux chargé. `None` si table vide.

    Lu EN BASE et non par glob sur `Input_Data/` : le dashboard n'a aucune garantie
    d'accès au dossier des flux (déploiement séparé), et deux sources finiraient par
    donner deux dates différentes pour « le flux exécuté ».

    Le tri porte sur `date_premiere_vue`, pas sur `flux_id` : ce dernier est du texte
    `DDMMYYYY`, dont l'ordre lexicographique n'est pas l'ordre chronologique
    ('16072026' < '16042027' est faux dans les deux sens qui comptent).
    """
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT flux_id, MAX(date_premiere_vue) AS d FROM {FQTN} "
                f"WHERE flux_id IS NOT NULL AND date_premiere_vue IS NOT NULL "
                f"GROUP BY flux_id ORDER BY d DESC LIMIT 1"
            )
            row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001
        print(f"   [WARN] suivi_carte_tp : lecture du dernier flux échouée : {exc}")
        # Cf. `fetch_min_eligibilite_pending` : sans rollback, la transaction reste
        # « aborted » et le SELECT principal de `fetch_tp_stats`, qui suit, échoue
        # sur un message trompeur au lieu de sa propre erreur.
        _rollback(conn)
        return None

    if not row or not row[0]:
        return None
    d = _clean_date(row[1])
    return (str(row[0]).strip(), d) if d is not None else None


def _bornes_cohorte(
    debut: Optional[Any] = None,
    fin: Optional[Any] = None,
) -> Tuple[date, date, str]:
    """Bornes INCLUSIVES de la cohorte étudiée, sur `date_eligibilite`.

    La cohorte est toujours un intervalle : une seule journée s'obtient avec
    `debut == fin`. Il n'y a donc qu'un seul chemin de calcul, quelle que soit
    la largeur de la période — une vue journalière et une vue mensuelle ne
    peuvent pas se contredire, elles exécutent le même SQL.

      - les deux bornes    ⇒ [debut, fin]           (ex. 17/08 → 09/09)
      - `debut` seul       ⇒ [debut, debut]         (une journée)
      - `fin` seule        ⇒ [fin, fin]
      - aucune             ⇒ le mois courant

    Bornes inversées : on les remet dans l'ordre plutôt que de renvoyer une
    cohorte vide, qu'on prendrait pour « aucune carte » et non pour une saisie
    à l'envers.

    Le libellé renvoyé s'adapte : "2026-08" pour un mois calendaire complet,
    "2026-08-17" pour une journée, "2026-08-17 -> 2026-09-09" sinon.

    Séparateur en ASCII pur, PAS une flèche Unicode : ce libellé est imprimé en
    console par le script 02, et une console cp1252 (le cas par défaut sous
    Windows hors runner) fait échouer le `print` sur un caractère non encodable.
    L'IHM affiche une vraie flèche à partir de `Date_Debut` / `Date_Fin`.
    """
    today = date.today()
    d = _clean_date(debut)
    f = _clean_date(fin)

    if d is None and f is None:
        d = today.replace(day=1)
        f = _dernier_jour_du_mois(d)
    elif d is None:
        d = f
    elif f is None:
        f = d
    if d > f:
        d, f = f, d

    if d == f:
        libelle = d.strftime("%Y-%m-%d")
    elif d.day == 1 and f == _dernier_jour_du_mois(d):
        libelle = d.strftime("%Y-%m")
    else:
        libelle = f"{d:%Y-%m-%d} -> {f:%Y-%m-%d}"
    return d, f, libelle


def _dernier_jour_du_mois(d: date) -> date:
    premier_suivant = (d.replace(year=d.year + 1, month=1, day=1)
                       if d.month == 12 else d.replace(month=d.month + 1, day=1))
    return premier_suivant - timedelta(days=1)


def fetch_tp_stats(
    conn,
    debut: Optional[Any] = None,
    fin: Optional[Any] = None,
) -> Optional[Dict[str, Any]]:
    """Bloc `7_Carte_TP.Delais_GED`, calculé en SQL.

    La cohorte est une PÉRIODE DE DATES D'ÉLIGIBILITÉ, jamais un préfixe de
    flux : un flux du 16 juillet contient des cartes dues en juillet ET des
    cartes dues en septembre. Regrouper par flux mélangerait deux engagements
    différents, et le chiffre bougerait encore après la clôture du mois. Une
    période d'éligibilité passée, elle, ne bouge plus.

    `debut` / `fin` : bornes INCLUSIVES ("YYYY-MM-DD" ou `date`). Une seule
    journée = `debut == fin`. Sans arguments : LA JOURNÉE DU DERNIER FLUX EXÉCUTÉ
    (son préfixe), et le mois courant seulement si la table ne porte aucun flux.

    Retourne `None` si la base est injoignable ou la table vide.
    """
    if conn is None:
        return None

    # Le dernier flux sert à deux choses : la période par défaut, et la ventilation
    # « issues du flux exécuté » / « issues des flux antérieurs ».
    dernier = fetch_dernier_flux(conn)
    flux_id = dernier[0] if dernier else None

    # Défaut = le jour du flux. Résolu ICI et non dans `_bornes_cohorte`, qui reste
    # pure (pas de `conn`) : son repli mois courant demeure le bon comportement quand
    # aucun flux n'a encore été chargé.
    if debut is None and fin is None and dernier is not None:
        debut = fin = dernier[1]

    debut, fin, libelle = _bornes_cohorte(debut, fin)
    seuil = DELAI_CONTRACTUEL_JOURS

    try:
        with conn.cursor() as cur:
            # --- cohorte du mois (ou du jour) d'éligibilité ---
            #
            # `DUE` = carte sur laquelle l'engagement est mesurable. Deux cas :
            # elle est échue, OU elle est déjà en GED.
            #
            # Le second terme n'est pas un détail : une carte produite en avance
            # (trouvée avant sa date d'éligibilité) est un SUCCÈS. Sans lui, elle
            # tombait dans `Cartes_Futures` et disparaissait de `Trouvees_GED` —
            # on masquait de la bonne performance. Vérifié : sur une période
            # 17/08→09/09 lue le 06/08, deux cartes trouvées étaient comptées 0.
            #
            # Reste hors dénominateur ce qui n'est ni échu ni trouvé : ces
            # cartes-là ne peuvent pas encore être en retard.
            DUE = "(date_eligibilite <= CURRENT_DATE OR date_found_ged IS NOT NULL)"
            FUTURE = "date_eligibilite > CURRENT_DATE AND date_found_ged IS NULL"
            # Ventilation par flux d'origine : les deux compteurs se complètent
            # EXACTEMENT à `Population_Attendue`, sans dédoublonnage à écrire. La PK
            # est (num_personne, code_soc_appart, offre) et `flux_id` est posé avec
            # `refresh=False` : une carte porte donc un et un seul `flux_id`, celui
            # du flux qui l'a révélée la première fois. Contre-intuitif mais garanti
            # par le schéma — ne pas y ajouter de DISTINCT « au cas où », il
            # masquerait une régression de la clé.
            #
            # `IS DISTINCT FROM` et non `<>` : les lignes à `flux_id` NULL (chargées
            # avant l'ajout de la colonne) doivent compter comme antérieures, alors
            # que `flux_id <> 'X'` vaut NULL — donc faux — et les perdrait.
            cur.execute(f"""
                SELECT COUNT(*),
                       COUNT(*) FILTER (WHERE {FUTURE}),
                       COUNT(*) FILTER (WHERE {DUE}),
                       COUNT(*) FILTER (WHERE {DUE} AND date_found_ged IS NOT NULL),
                       COUNT(*) FILTER (WHERE {DUE} AND date_found_ged IS NOT NULL
                                          AND {_DELAI} <= {seuil}),
                       COUNT(*) FILTER (WHERE {DUE} AND date_found_ged IS NOT NULL
                                          AND {_DELAI_DEPUIS_FLUX} <= {seuil}),
                       COUNT(*) FILTER (WHERE {DUE} AND date_found_ged IS NOT NULL
                                          AND {_DELAI} = 0),
                       COUNT(*) FILTER (WHERE {DUE} AND date_found_ged IS NULL
                                          AND kpep_iehe IS NULL),
                       COUNT(*) FILTER (WHERE {DUE} AND date_found_ged IS NULL
                                          AND kpep_iehe IS NOT NULL),
                       COUNT(*) FILTER (WHERE flux_id = %s),
                       COUNT(*) FILTER (WHERE flux_id IS DISTINCT FROM %s),
                       -- `AND date_found_ged IS NOT NULL` n'est PAS redondant.
                       -- `GREATEST` ignore les NULL en PostgreSQL :
                       -- GREATEST(NULL::int - 5, 0) vaut 0, pas NULL. Une carte
                       -- jamais trouvée traverse donc _DELAI en « 0 jour » et
                       -- entre dans les agrégats comme une livraison parfaite.
                       -- Sans le filtre, plus il manque de cartes, meilleure
                       -- paraît la moyenne — et une période sans aucune carte
                       -- trouvée affiche 0/0/0 au lieu de « pas de mesure ».
                       -- Mesuré sur le jeu d'essai (04/08→30/08) : 0,25 au lieu
                       -- de 0,50 sur deux cartes trouvées à 0 et 1 jour.
                       MIN({_DELAI}) FILTER (WHERE {DUE} AND date_found_ged IS NOT NULL),
                       MAX({_DELAI}) FILTER (WHERE {DUE} AND date_found_ged IS NOT NULL),
                       AVG({_DELAI}) FILTER (WHERE {DUE} AND date_found_ged IS NOT NULL),
                       PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY {_DELAI})
                           FILTER (WHERE {DUE} AND date_found_ged IS NOT NULL)
                  FROM {FQTN}
                 WHERE date_eligibilite BETWEEN %s AND %s
            """, (flux_id, flux_id, debut, fin))
            row = cur.fetchone()

            # --- stock de KO antérieur ---
            #
            # Tout ce qui reste introuvable, révélé par un AUTRE flux que celui qui
            # vient d'être exécuté, et dû AVANT la borne haute de la période.
            #
            # Borne STRICTE, sur `fin` (le « au ») : c'est la règle métier — « depuis
            # le début jusqu'au ». Conséquence à connaître : ce stock RECOUVRE
            # partiellement `Non_Trouvees_GED` de la période (tout ce qui est dû entre
            # `debut` et `fin` exclu s'y retrouve). Les deux ne s'additionnent donc
            # pas ; c'est un cumul, pas un compteur disjoint.
            cur.execute(f"""
                SELECT COUNT(*) FROM {FQTN}
                 WHERE date_found_ged IS NULL
                   AND date_eligibilite < %s
                   AND flux_id IS DISTINCT FROM %s
            """, (fin, flux_id))
            stock_ko = cur.fetchone()[0]

            # --- photo du flux exécuté, indépendante de la période ---
            #
            # Relatif à CURRENT_DATE et non à la période : la question métier est
            # « ce flux, que vient-il de m'apporter, et qu'est-ce qui est dû
            # aujourd'hui ? ». Un flux du 16/07 porte des cartes dues en juillet ET
            # en septembre ; les deux compteurs les séparent.
            if flux_id is None:
                flux_row = (0, 0, 0)
            else:
                cur.execute(f"""
                    SELECT COUNT(*) FILTER (WHERE date_eligibilite <= CURRENT_DATE),
                           COUNT(*) FILTER (WHERE date_eligibilite >  CURRENT_DATE),
                           COUNT(*) FILTER (WHERE date_eligibilite IS NULL)
                      FROM {FQTN} WHERE flux_id = %s
                """, (flux_id,))
                flux_row = cur.fetchone()

            # --- état à date, toutes cohortes confondues (tuiles du dashboard) ---
            cur.execute(f"""
                SELECT COUNT(*) FILTER (WHERE date_eligibilite <= CURRENT_DATE),
                       COUNT(*) FILTER (WHERE date_eligibilite <= CURRENT_DATE
                                          AND date_found_ged IS NOT NULL),
                       COUNT(*) FILTER (WHERE date_eligibilite <= CURRENT_DATE
                                          AND kpep_iehe IS NULL),
                       COUNT(*) FILTER (WHERE date_eligibilite <= CURRENT_DATE
                                          AND kpep_iehe IS NOT NULL
                                          AND date_found_ged IS NULL),
                       COUNT(*) FILTER (WHERE date_eligibilite > CURRENT_DATE),
                       COUNT(*) FILTER (WHERE date_eligibilite IS NULL),
                       COUNT(*)
                  FROM {FQTN}
            """)
            etat = cur.fetchone()

            # --- ventilation par mois d'éligibilité ---
            # Mêmes règles que la cohorte ci-dessus : dénominateur = cartes
            # échues, sinon le mois en cours paraîtrait mauvais uniquement parce
            # qu'il contient des cartes pas encore dues.
            cur.execute(f"""
                SELECT TO_CHAR(date_eligibilite, 'YYYY-MM'),
                       COUNT(*),
                       COUNT(*) FILTER (WHERE {FUTURE}),
                       COUNT(*) FILTER (WHERE {DUE}),
                       COUNT(*) FILTER (WHERE {DUE} AND date_found_ged IS NOT NULL),
                       COUNT(*) FILTER (WHERE {DUE} AND date_found_ged IS NOT NULL
                                          AND {_DELAI} <= {seuil}),
                       PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY {_DELAI})
                           FILTER (WHERE {DUE} AND date_found_ged IS NOT NULL)
                  FROM {FQTN}
                 WHERE date_eligibilite IS NOT NULL
                 GROUP BY 1
                 ORDER BY 1
            """)
            par_mois = cur.fetchall()
    except Exception as exc:  # noqa: BLE001
        print(f"   [WARN] suivi_carte_tp : lecture des statistiques échouée : {exc}")
        return None

    if row is None or etat is None or int(etat[6] or 0) == 0:
        return None

    (attendues, futures, due, trouvees, dans_4j, dans_4j_flux, jour_meme,
     absents, en_attente, issues_courant, issues_anterieurs,
     d_min, d_max, d_moy, d_med) = row

    attendues = int(attendues or 0)
    futures = int(futures or 0)
    due = int(due or 0)
    trouvees = int(trouvees or 0)
    dans_4j = int(dans_4j or 0)
    absents = int(absents or 0)
    en_attente = int(en_attente or 0)

    flux_eligibles = int(flux_row[0] or 0)
    flux_futures = int(flux_row[1] or 0)
    flux_sans_date = int(flux_row[2] or 0)

    # `Plus_de_4_jours` ne compte QUE des cartes trouvées, en retard. Les cartes
    # jamais trouvées ne sont pas mélangées ici : elles ont leur propre compteur
    # (`Non_Trouvees_GED`, ventilé en `Absents_IEHE` / `En_Attente_GED`). Un
    # retard constaté et une carte absente appellent deux actions différentes ;
    # les additionner masquerait laquelle des deux domine.
    plus_4j = trouvees - dans_4j
    non_trouvees = due - trouvees

    return {
        "Source": f"{FQTN} (base supervision)",
        "Definition": (
            "Population = cartes dont la date d'éligibilité tombe dans la période, "
            "antériorité comprise. Les taux sont rapportés à Population_Due "
            "(= Population_Attendue - Cartes_Futures) : une carte pas encore échue "
            "ne peut pas être en retard. Le délai court à partir de la DATE "
            "D'ÉLIGIBILITÉ, et se termine au tmstinj de la GED (date d'injection "
            "réelle), avec repli sur notre date de constat quand le tmstinj n'a pas "
            "été remonté."
        ),
        # `Cohorte` : "2026-08" (mois complet), "2026-08-15" (une journée) ou
        # "2026-08-17 → 2026-09-09" (période libre).
        # `Mois_Reference` garde son nom historique — le dashboard et le rapport
        # le lisent — et vaut désormais le même libellé.
        "Cohorte": libelle,
        "Mois_Reference": libelle,
        "Date_Debut": debut.strftime("%Y-%m-%d"),
        "Date_Fin": fin.strftime("%Y-%m-%d"),
        "Nb_Jours_Periode": (fin - debut).days + 1,
        "Seuil_Contractuel_Jours": seuil,

        # --- population ---
        "Population_Attendue": attendues,
        # Part de la cohorte pas encore échue. Vaut 0 pour un mois clos ; n'est
        # non nul que pour le mois EN COURS (ou un mois futur). C'est la réponse
        # à « pourquoi des cartes futures dans un mois donné ? » : le 6 août, une
        # carte due le 15 août est bien d'août, mais elle n'est pas encore due.
        "Cartes_Futures": {"Nombre": futures},
        # Dénominateur contractuel.
        "Population_Due": due,

        # --- d'où viennent les cartes de la période ---
        # Somme EXACTE de Population_Attendue : une carte porte un seul `flux_id`
        # (cf. commentaire de la requête). `Issues_Flux_Courant` = révélées par le
        # flux qui vient d'être exécuté ; `Issues_Flux_Anterieurs` = antériorité,
        # que le flux du jour ne remontre jamais (le New_S est un différentiel).
        "Flux_Reference": flux_id,
        "Issues_Flux_Courant": int(issues_courant or 0),
        "Issues_Flux_Anterieurs": int(issues_anterieurs or 0),

        # --- respect de l'engagement, sur les cartes TROUVÉES ---
        # Trois compteurs disjoints qui se complètent à Population_Due :
        #   Dans_les_4_jours + Plus_de_4_jours + Non_Trouvees_GED = Population_Due
        # Tous rapportés au même dénominateur, donc directement comparables.
        # Le délai part de la date d'éligibilité : J=0 / J<=4 / J>4.
        "Dans_les_4_jours": _taux(dans_4j, due),
        "Plus_de_4_jours": _taux(plus_4j, due),
        "Le_Jour_Meme": _taux(int(jour_meme or 0), due),
        # Même seuil, chrono ancré à la découverte par le flux. Sert UNIQUEMENT
        # d'arbitrage : c'est l'écart entre les deux qui mesure la part de retard
        # imputable à la rétroactivité des dates d'effet, et non à la GED.
        "Dans_les_4_jours_Depuis_Flux": _taux(int(dans_4j_flux or 0), due),

        # --- où en sont les cartes ---
        # Trouvees_GED = Dans_les_4_jours + Plus_de_4_jours
        "Trouvees_GED": _taux(trouvees, due),
        "Non_Trouvees_GED": _taux(non_trouvees, due),
        # Les deux SEULES raisons d'une carte manquante ; leur somme vaut
        # exactement Non_Trouvees_GED. Elles appellent deux actions différentes :
        #   - Absents_IEHE  : pas de KPEP, on ne PEUT PAS interroger la GED
        #                     → relève du retry IEHE (script 06)
        #   - En_Attente_GED: KPEP connu, GED interrogée, carte pas encore sortie
        #                     → relève du retry GED (script 08)
        "Absents_IEHE": {"Nombre": absents},
        "En_Attente_GED": {"Nombre": en_attente},

        # Cumul des KO hérités : introuvables, révélés par un flux ANTÉRIEUR, dus
        # avant `Date_Limite` (le « au » de la période, borne exclue). C'est la
        # dette, pas un compteur de la période — il recouvre partiellement
        # Non_Trouvees_GED et ne s'y additionne pas.
        "Stock_KO_Anterieur": {
            "Nombre": int(stock_ko or 0),
            "Date_Limite": fin.strftime("%Y-%m-%d"),
        },

        # --- photo du flux exécuté (hors période, relatif à aujourd'hui) ---
        # Total = la SOMME des deux, pas un COUNT(*) : les cartes sans date
        # d'éligibilité ne tombent dans aucun des deux filtres et feraient un total
        # qui ne se réconcilie pas. Elles sont remontées à part.
        "Flux_Courant": {
            "Flux_Id": flux_id,
            "Date_Flux": dernier[1].strftime("%Y-%m-%d") if dernier else None,
            "Cartes_Eligibles": flux_eligibles,
            "Cartes_Futures": flux_futures,
            "Total_Cartes": flux_eligibles + flux_futures,
            "Sans_Date_Eligibilite": flux_sans_date,
        },

        "Distribution_Delai": {
            "Min": int(d_min) if d_min is not None else None,
            "Median": round(float(d_med), 1) if d_med is not None else None,
            "Moyenne": round(float(d_moy), 1) if d_moy is not None else None,
            "Max": int(d_max) if d_max is not None else None,
            "Base": trouvees,
        },
        "Etat_A_Date": {
            "Cartes_Attendues_A_Date": int(etat[0] or 0),
            "Trouvees_GED": int(etat[1] or 0),
            "Absents_IEHE": int(etat[2] or 0),
            "En_Attente_GED": int(etat[3] or 0),
            "Cartes_Futures": int(etat[4] or 0),
            "Sans_Date_Eligibilite": int(etat[5] or 0),
            "Total_Suivi": int(etat[6] or 0),
        },
        "Par_Mois_Eligibilite": {
            str(m): {
                "Population_Attendue": int(n),
                "Cartes_Futures": {"Nombre": int(nb_fut)},
                "Population_Due": int(nb_due),
                "Dans_les_4_jours": _taux(int(nb_4j), int(nb_due)),
                "Plus_de_4_jours": _taux(int(nb_ok) - int(nb_4j), int(nb_due)),
                "Trouvees_GED": _taux(int(nb_ok), int(nb_due)),
                "Non_Trouvees_GED": _taux(int(nb_due) - int(nb_ok), int(nb_due)),
                "Delai_Median": round(float(med), 1) if med is not None else None,
            }
            for m, n, nb_fut, nb_due, nb_ok, nb_4j, med in par_mois
        },
    }


def fetch_periodes(conn) -> Dict[str, Any]:
    """Amplitude des dates d'éligibilité présentes en base.

    Sert à borner et pré-remplir un sélecteur de période : on propose l'étendue
    réellement couverte plutôt qu'un calendrier libre, pour qu'une période vide
    — qu'on prendrait pour un calcul cassé — soit difficile à saisir.

    `flux` porte le dernier flux exécuté ; c'est la période PAR DÉFAUT du sélecteur
    (une journée), et elle doit venir d'ici pour que l'IHM et `fetch_tp_stats`
    s'accordent sur la même date sans la recalculer chacun de son côté.

    Retourne {"min": "YYYY-MM-DD", "max": ..., "mois": [...], "total": n,
              "flux": {"flux_id": ..., "date": ...} | None}.
    """
    vide: Dict[str, Any] = {"min": None, "max": None, "mois": [], "total": 0,
                            "flux": None}
    if conn is None:
        return vide
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT MIN(date_eligibilite), MAX(date_eligibilite), COUNT(*)
                  FROM {FQTN} WHERE date_eligibilite IS NOT NULL
            """)
            dmin, dmax, total = cur.fetchone()
            cur.execute(f"""
                SELECT DISTINCT TO_CHAR(date_eligibilite, 'YYYY-MM')
                  FROM {FQTN} WHERE date_eligibilite IS NOT NULL
                 ORDER BY 1 DESC
            """)
            mois = [str(r[0]) for r in cur.fetchall() if r[0]]
        dernier = fetch_dernier_flux(conn)
        return {
            "min": dmin.strftime("%Y-%m-%d") if dmin else None,
            "max": dmax.strftime("%Y-%m-%d") if dmax else None,
            "mois": mois,
            "total": int(total or 0),
            "flux": ({"flux_id": dernier[0], "date": dernier[1].strftime("%Y-%m-%d")}
                     if dernier else None),
        }
    except Exception as exc:  # noqa: BLE001
        print(f"   [WARN] suivi_carte_tp : lecture des périodes échouée : {exc}")
        return vide


def fetch_controle_stats(conn) -> Optional[Dict[str, Any]]:
    """Bloc `7_Carte_TP.Controle_GED_Quotidien`, calculé en SQL.

    Remplace la lecture de `{PREFIX}_TP_GED_Detail.csv`, que le script 07 en
    production (`scriptsNewPipline/`) n'écrit plus : le KPI retombait donc sur
    `status="Absent"` et les tuiles `taux_ged`, `ged_trouves`, `ged_ko` et
    `ged_recus` du dashboard étaient toutes vides.

    Les clés sont celles attendues par `report_generator.py` et
    `modules/dashboard/queries.py` — ne pas les renommer. `Total_Cartes` est
    ajoutée : `_KPI_PATHS["ged_recus"]` la cherchait sans qu'elle ait jamais été
    produite.
    """
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT COUNT(*) FILTER (WHERE date_eligibilite <= CURRENT_DATE),
                       COUNT(*) FILTER (WHERE date_eligibilite <= CURRENT_DATE
                                          AND date_found_ged IS NOT NULL),
                       COUNT(*) FILTER (WHERE date_eligibilite <= CURRENT_DATE
                                          AND date_found_ged IS NULL),
                       COUNT(*) FILTER (WHERE motif = %s),
                       COUNT(*)
                  FROM {FQTN}
            """, (MOTIF_CORRECTION,))
            total_due, trouves, non_trouves, corrections, total = cur.fetchone()

            cur.execute(f"""
                SELECT COALESCE(NULLIF(TRIM(code_soc_appart), ''), 'INCONNUE'),
                       COUNT(*) FILTER (WHERE date_eligibilite <= CURRENT_DATE),
                       COUNT(*) FILTER (WHERE date_eligibilite <= CURRENT_DATE
                                          AND date_found_ged IS NOT NULL)
                  FROM {FQTN}
                 GROUP BY 1
                 ORDER BY 1
            """)
            par_soc = cur.fetchall()
    except Exception as exc:  # noqa: BLE001
        print(f"   [WARN] suivi_carte_tp : lecture du contrôle quotidien échouée : {exc}")
        return None

    if int(total or 0) == 0:
        return None

    total_due = int(total_due or 0)
    trouves = int(trouves or 0)
    non_trouves = int(non_trouves or 0)

    return {
        "status": "Calculé",
        "definition": (
            "Contrôle des cartes TP en GED, sur la population attendue à date "
            "(affiliations du jour + antériorité non soldée)."
        ),
        "Source": f"{FQTN} (base supervision)",
        "Total_Cartes": {"Nombre": int(total or 0)},
        "Population_Eligible": total_due,
        "Trouves_GED": _taux(trouves, total_due),
        "Non_Trouves_GED": _taux(non_trouves, total_due),
        "Corrections_Manuelles": _taux(int(corrections or 0), total_due),
        "Statut_Final": {
            "Rapproche": trouves,
            "Non_Rapproche": non_trouves,
            "Taux_Rapprochement_Final": (
                round(trouves / total_due * 100, 2) if total_due > 0 else 0.0
            ),
        },
        "Par_Societe": {
            str(soc): _taux(int(ok), int(n))
            for soc, n, ok in par_soc
        },
    }
