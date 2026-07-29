# -*- coding: utf-8 -*-
"""
suivi_iehe_db.py — Accès à la table de suivi des personnes absentes d'IEHE.

Remplace l'état porté jusqu'ici par les fichiers `Output/{prefix}_IEHE_KO.csv` :
la table `rptpsc.suivi_iehe` devient la SEULE source de vérité sur la question
« cette personne a-t-elle fini par apparaître dans IEHE, et quand ».

Séparation des rôles (même principe que le couple GED 07/08)
------------------------------------------------------------
  - `{prefix}_NS_IEHE.csv`  = photo figée du flux du jour J. Jamais réécrite.
  - `{prefix}_IEHE_KO.csv`  = photo figée des KO du jour J. Jamais réécrite.
  - `rptpsc.suivi_iehe`     = état vivant, seul objet mis à jour par les retries.

Conséquence directe : le retry n'a JAMAIS besoin de rouvrir un fichier d'un flux
antérieur. La colonne `date_found` répond déjà à « quand a-t-elle été trouvée ».
`date_found IS NULL` remplace l'ancien `statut_retry == "KO"`.

Producteurs / consommateurs
---------------------------
  - 03_generation_fichiers_detail.py : `upsert_ko_rows()` — insère les KO du flux
  - 06_iehe_retry.py                 : `fetch_pending()` → IEHE → `mark_found()`
  - 02_calcul_kpi.py                 : `fetch_stats()` pour le bloc Retry_IEHE_KO

Robustesse : aucune fonction ne lève. Base injoignable ⇒ log + valeur neutre,
le reste du pipeline CSV continue de tourner.
"""

from __future__ import annotations

import os
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Sequence

try:
    import psycopg
except ImportError:  # pragma: no cover - dépend de l'environnement d'exécution
    psycopg = None


# --- Base supervision (écriture) — valeurs alignées sur config/credentials.ini,
#     section [postgresql_supervision]. La base IEHE (choregie_db) reste en
#     lecture seule : on n'y crée aucun objet.
SUP_HOST = os.getenv("PG_SUPERVISION_HOST", "bdd-T0XX0052.alias")
SUP_PORT = os.getenv("PG_SUPERVISION_PORT", "5577")
SUP_DB = os.getenv("PG_SUPERVISION_DB", "supervisionpsc_db")
SUP_USER = os.getenv("PG_SUPERVISION_USER", "rptpsc")
SUP_PASSWORD = os.getenv("PG_SUPERVISION_PASSWORD", "rptpsc_xx")

SUIVI_SCHEMA = os.getenv("PG_SUPERVISION_SCHEMA", "rptpsc")
SUIVI_TABLE = "suivi_iehe"
FQTN = f"{SUIVI_SCHEMA}.{SUIVI_TABLE}"

# Libellés d'éligibilité TP — doivent rester identiques à ceux produits par
# `iehe_ko_lib.tp_status()`, sinon les ventilations du script 02 se scindent
# en doublons ("Eligible_TP" vs "ELIGIBLE_TP").
ELIG_ELIGIBLE = "Eligible_TP"
ELIG_FUTURE = "Future_TP"
ELIG_HORS = "Hors_Perimetre_TP"

def _q(identifier: str) -> str:
    """Quote un identifiant SQL.

    ⚠️ Obligatoire pour CHAQUE nom de colonne de ce module. PostgreSQL replie
    tout identifiant non quoté en minuscules et n'accepte ni espace ni accent :
    écrit tel quel, `NS_num_personne` désignerait `ns_num_personne` et
    `Eligibilité TP` serait une erreur de syntaxe. Les noms demandés n'existent
    donc que si toutes les requêtes les quotent — d'où la génération systématique
    du SQL à partir de `_COLUMNS` plutôt qu'à la main.
    """
    return '"' + identifier.replace('"', '""') + '"'


# Schéma de la table : 20 colonnes, dans cet ordre. DDL et INSERT en sont TOUS
# deux dérivés — c'est la seule façon d'empêcher la liste de colonnes, les
# placeholders et l'ordre des valeurs de se désynchroniser silencieusement.
# Les noms reproduisent exactement les entêtes du CSV `{prefix}_IEHE_KO.csv`.
_COLUMNS: Sequence[tuple] = (
    ("flux_id",                "TEXT NOT NULL"),
    ("NS_num_personne",        "TEXT NOT NULL"),
    ("NS_nom_long",            "TEXT"),
    ("NS_prenom",              "TEXT"),
    ("NS_type_assure",         "TEXT"),
    ("NS_date_naissance",      "DATE"),
    ("NS_valeur_coordonnee",   "TEXT"),
    ("NS_idkpep",              "TEXT"),
    ("NS_offre",               "TEXT"),
    ("NS_date_adhesion",       "DATE"),
    ("NS_date_effet_adhesion", "DATE"),
    ("NS_code_soc_appart",     "TEXT"),
    ("Eligibilité TP",         "TEXT"),
    ("Date éligibilité TP",    "DATE"),
    ("Valeur carte TP",        "TEXT"),
    ("Raison non Eligibilité", "TEXT"),
    ("date_found",             "DATE"),
    ("date_derniere_verif",    "DATE"),
    ("mail_IEHE",              "TEXT"),
    ("KPEP_IEHE",              "TEXT"),
)

_DDL = (
    f"CREATE TABLE IF NOT EXISTS {FQTN} (\n    "
    + ",\n    ".join(f"{_q(name):<26} {sqltype}" for name, sqltype in _COLUMNS)
    + f",\n    PRIMARY KEY ({_q('flux_id')}, {_q('NS_num_personne')})\n)"
)

# La PK (flux_id, NS_num_personne) est inutilisable pour les requêtes par personne
# sans flux_id — or c'est exactement ce que fait le retry (une personne trouvée
# solde ses lignes de TOUS les flux). D'où l'index dédié.
_DDL_IDX = (
    f"CREATE INDEX IF NOT EXISTS idx_{SUIVI_TABLE}_personne "
    f"ON {FQTN} ({_q('NS_num_personne')})"
)
# Index partiel : le scan des KO en attente ne lit que cette fraction de table.
_DDL_IDX_PENDING = (
    f"CREATE INDEX IF NOT EXISTS idx_{SUIVI_TABLE}_pending "
    f"ON {FQTN} ({_q('NS_num_personne')}) WHERE {_q('date_found')} IS NULL"
)

# Réplique SQL de `eligibilite_label()`. La table ne stocke que le couple brut
# ("O"/"N" + "Futur") demandé : le libellé attendu par le script 02 est dérivé à
# la lecture, sans colonne supplémentaire. LEFT() plutôt que LIKE 'FUTUR%' pour
# éviter un '%' dans une requête que psycopg pourrait un jour interpoler.
_ELIG_LABEL_SQL = f"""
        CASE
            WHEN UPPER(TRIM(COALESCE({_q('Eligibilité TP')}, ''))) <> 'O'
                THEN '{ELIG_HORS}'
            WHEN LEFT(UPPER(TRIM(COALESCE({_q('Valeur carte TP')}, ''))), 5) = 'FUTUR'
                THEN '{ELIG_FUTURE}'
            ELSE '{ELIG_ELIGIBLE}'
        END"""


# =============================================================================
# CONNEXION / SCHÉMA
# =============================================================================

def connect_supervision():
    """Connexion à la base supervision. Retourne `None` en cas d'échec."""
    if psycopg is None:
        print("   [WARN] suivi_iehe : module 'psycopg' absent — suivi BDD désactivé.")
        return None
    try:
        return psycopg.connect(
            host=SUP_HOST, port=SUP_PORT, dbname=SUP_DB,
            user=SUP_USER, password=SUP_PASSWORD, connect_timeout=5,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"   [WARN] suivi_iehe : connexion supervision impossible "
              f"({SUP_HOST}:{SUP_PORT}/{SUP_DB}) : {exc}")
        return None


def ensure_table(conn) -> bool:
    """Crée table + index s'ils n'existent pas. Retourne `False` si échec.

    ⚠️ `CREATE TABLE IF NOT EXISTS` ne migre RIEN : si une table `suivi_iehe`
    subsiste avec un schéma antérieur, le DDL est un no-op silencieux et tous les
    INSERT échoueront ensuite sur des colonnes absentes. Un changement de
    `_COLUMNS` impose donc un `DROP TABLE {FQTN}` manuel préalable.
    """
    if conn is None:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(_DDL)
            cur.execute(_DDL_IDX)
            cur.execute(_DDL_IDX_PENDING)
        conn.commit()
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"   [WARN] suivi_iehe : création de {FQTN} impossible : {exc}")
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        return False


# =============================================================================
# ÉCRITURE — script 03
# =============================================================================

def eligibilite_label(eligibilite_o_n: str, valeur_carte_tp: str) -> str:
    """Traduit le couple (`Eligibilité TP`, `Valeur carte TP`) en libellé unique.

    Correspondance exacte avec `iehe_ko_lib.tp_status()` :
      - "N"                  → Hors_Perimetre_TP
      - "O" + valeur "Futur" → Future_TP
      - "O" + valeur vide    → Eligible_TP

    Jumeau Python de `_ELIG_LABEL_SQL`, qui applique la même règle côté base
    pour `fetch_stats()` : toute modification ici doit être reportée là-bas.
    """
    elig = (eligibilite_o_n or "").strip().upper()
    valeur = (valeur_carte_tp or "").strip().upper()
    if elig != "O":
        return ELIG_HORS
    return ELIG_FUTURE if valeur.startswith("FUTUR") else ELIG_ELIGIBLE


def _clean_text(value: Any) -> Optional[str]:
    """Texte prêt pour la base. Vide / NaN / NaT → None.

    ⚠️ `value or ""` ne suffit pas : `bool(float('nan'))` vaut True, donc un
    trou laissé par une jointure pandas passerait la chaîne littérale 'nan'.
    Sur une colonne DATE, cela fait échouer le lot entier.
    """
    if value is None:
        return None
    try:
        if value != value:  # NaN et NaT sont les seuls à ne pas s'égaler
            return None
    except Exception:  # noqa: BLE001 - comparateur exotique : on retombe sur str()
        pass
    text = str(value).strip()
    if not text or text.lower() in ("nan", "nat", "none", "null"):
        return None
    return text


# Ordre significatif : le format ISO d'abord, puis le format français produit par
# `compute_carte_tp_row()` ("%d/%m/%Y"), puis le format des préfixes de flux.
_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d%m%Y")


def _clean_date(value: Any) -> Optional[date]:
    """Convertit en `datetime.date` réel. Valeur illisible → None.

    On ne laisse JAMAIS PostgreSQL parser la chaîne lui-même : avec le DateStyle
    par défaut (`ISO, MDY`), '07/05/2026' devient le 5 juillet au lieu du 7 mai,
    et '25/12/2026' lève `date/time field value out of range` qui fait échouer
    tout l'`executemany`. Passer un objet `date` supprime toute ambiguïté.
    """
    if isinstance(value, datetime):  # couvre pandas.Timestamp
        return value.date()
    if isinstance(value, date):
        return value
    text = _clean_text(value)
    if text is None:
        return None
    text = text.split(" ")[0].split("T")[0]  # '2026-04-16 00:00:00' → '2026-04-16'
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


# Spécification UNIQUE de l'upsert : (colonne, coercion, rafraîchie sur conflit).
# La colonne sert aussi de clé dans les dicts `rows` — les enregistrements produits
# par le script 03 portent déjà les entêtes du CSV. Le SQL et le payload sont
# générés depuis ce tuple, donc la N-ième valeur envoyée vise toujours la N-ième
# colonne nommée.
#
# `refresh=False` sur les colonnes de retry : un rejeu du script 03 sur un flux
# déjà chargé ne doit pas effacer ce que les retries ont trouvé (`date_found`,
# `mail_IEHE`, `KPEP_IEHE`, `date_derniere_verif`).
_UPSERT_SPEC: Sequence[tuple] = (
    ("NS_num_personne",        _clean_text, False),
    ("NS_nom_long",            _clean_text, True),
    ("NS_prenom",              _clean_text, True),
    ("NS_type_assure",         _clean_text, True),
    ("NS_date_naissance",      _clean_date, True),
    ("NS_valeur_coordonnee",   _clean_text, True),
    ("NS_idkpep",              _clean_text, True),
    ("NS_offre",               _clean_text, True),
    ("NS_date_adhesion",       _clean_date, True),
    ("NS_date_effet_adhesion", _clean_date, True),
    ("NS_code_soc_appart",     _clean_text, True),
    ("Eligibilité TP",         _clean_text, True),
    ("Date éligibilité TP",    _clean_date, True),
    ("Valeur carte TP",        _clean_text, True),
    ("Raison non Eligibilité", _clean_text, True),
    ("date_found",             _clean_date, False),
    ("date_derniere_verif",    _clean_date, False),
    ("mail_IEHE",              _clean_text, False),
    ("KPEP_IEHE",              _clean_text, False),
)

_UPSERT_SQL = f"""
    INSERT INTO {FQTN} ({_q('flux_id')}, {", ".join(_q(c) for c, _, _ in _UPSERT_SPEC)})
    VALUES ({", ".join(["%s"] * (1 + len(_UPSERT_SPEC)))})
    ON CONFLICT ({_q('flux_id')}, {_q('NS_num_personne')}) DO UPDATE SET
        {", ".join(f"{_q(c)} = EXCLUDED.{_q(c)}" for c, _, upd in _UPSERT_SPEC if upd)}
"""


def upsert_ko_rows(conn, flux_id: str, rows: Sequence[Dict[str, Any]]) -> int:
    """Insère les personnes absentes d'IEHE pour ce flux.

    `rows` : dicts portant `NS_num_personne` (obligatoire) et, optionnellement,
    les autres clés listées dans `_UPSERT_SPEC`.

    En cas de conflit (re-run du script 03 sur le même flux), on rafraîchit
    uniquement les colonnes d'enrichissement. Les colonnes liées aux retries
    et aux résultats IEHE ne sont pas écrasées.

    Retourne le nombre de lignes envoyées (0 si indisponible).
    """
    if conn is None or not rows or not flux_id:
        return 0

    payload: List[tuple] = []
    seen = set()
    flux = str(flux_id).strip()

    for r in rows:
        pid = _clean_text(r.get("NS_num_personne"))
        if pid is None or pid in seen:
            continue
        seen.add(pid)
        payload.append(
            (flux,) + tuple(coerce(r.get(col)) for col, coerce, _ in _UPSERT_SPEC)
        )

    if not payload:
        # Silence = piège : sans ce message, un mapping de colonnes erroné côté
        # script 03 ressemble à un flux sans aucun KO.
        print(f"   [WARN] suivi_iehe : flux {flux_id} — {len(rows)} ligne(s) reçue(s) "
              f"mais aucune clé 'NS_num_personne' exploitable, rien n'est inséré.")
        return 0

    try:
        with conn.cursor() as cur:
            cur.executemany(_UPSERT_SQL, payload)
        conn.commit()
        return len(payload)
    except Exception as exc:  # noqa: BLE001
        print(f"   [WARN] suivi_iehe : insertion des KO du flux {flux_id} "
              f"échouée : {exc}")
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        return 0

# =============================================================================
# LECTURE / MISE À JOUR — script 06 (retry)
# =============================================================================

def fetch_pending(conn, flux_id: Optional[str] = None) -> List[str]:
    """Liste dédoublonnée des `num_personne` encore absents d'IEHE.

    `flux_id` restreint le SCAN à un flux (utile pour rejouer un flux précis).
    Attention : cela ne change pas la portée de `mark_found()`, qui solde de
    toute façon la personne sur tous ses flux.
    """
    if conn is None:
        return []
    sql = (f"SELECT DISTINCT {_q('NS_num_personne')} FROM {FQTN} "
           f"WHERE {_q('date_found')} IS NULL AND {_q('NS_num_personne')} <> ''")
    params: tuple = ()
    if flux_id:
        sql += f" AND {_q('flux_id')} = %s"
        params = (str(flux_id).strip(),)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return [str(r[0]).strip() for r in cur.fetchall() if r[0]]
    except Exception as exc:  # noqa: BLE001
        print(f"   [WARN] suivi_iehe : lecture des KO en attente échouée : {exc}")
        return []


def fetch_pending_by_flux(conn) -> Dict[str, int]:
    """Nombre de KO encore en attente par flux (pour le log du retry)."""
    if conn is None:
        return {}
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {_q('flux_id')}, COUNT(*) FROM {FQTN} "
                f"WHERE {_q('date_found')} IS NULL "
                f"GROUP BY {_q('flux_id')} ORDER BY {_q('flux_id')}"
            )
            return {str(r[0]): int(r[1]) for r in cur.fetchall()}
    except Exception as exc:  # noqa: BLE001
        print(f"   [WARN] suivi_iehe : comptage par flux échoué : {exc}")
        return {}


def mark_found(conn, found: Dict[str, Dict[str, str]], day: Optional[date] = None) -> int:
    """Solde les personnes désormais présentes dans IEHE.

    ⚠️ Volontairement SANS filtre `flux_id` : une personne trouvée solde ses
    lignes en attente de TOUS les flux d'un coup — une personne existe ou non
    dans IEHE, la réponse ne dépend pas du flux qui l'a signalée. C'est le même
    choix que `08_ged_retry.py`.

    Le garde `AND date_found IS NULL` préserve la date de première découverte.

    Retourne le nombre de lignes soldées.
    """
    if conn is None or not found:
        return 0
    day = day or date.today()

    payload = []
    for pid, info in found.items():
        pid = str(pid).strip()
        if not pid:
            continue
        info = info or {}
        payload.append((
            day, day,
            str(info.get("mail", "") or "").strip(),
            str(info.get("kpep", "") or "").strip(),
            pid,
        ))
    if not payload:
        return 0

    sql = f"""
        UPDATE {FQTN}
           SET {_q('date_found')}          = %s,
               {_q('date_derniere_verif')} = %s,
               {_q('mail_IEHE')} = COALESCE(NULLIF(%s, ''), {_q('mail_IEHE')}),
               {_q('KPEP_IEHE')} = COALESCE(NULLIF(%s, ''), {_q('KPEP_IEHE')})
         WHERE {_q('NS_num_personne')} = %s
           AND {_q('date_found')} IS NULL
    """
    try:
        with conn.cursor() as cur:
            cur.executemany(sql, payload)
            updated = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        conn.commit()
        return int(updated)
    except Exception as exc:  # noqa: BLE001
        print(f"   [WARN] suivi_iehe : mise à jour des personnes trouvées "
              f"échouée : {exc}")
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        return 0


def mark_checked(conn, day: Optional[date] = None) -> int:
    """Horodate la tentative sur les lignes restées KO.

    Sans ça on ne saurait pas distinguer « retry jamais tenté » de « retry tenté,
    personne toujours absente ». Le script 02 expose cette date sous
    `Date_Derniere_Verification`.
    """
    if conn is None:
        return 0
    day = day or date.today()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE {FQTN} SET {_q('date_derniere_verif')} = %s "
                f"WHERE {_q('date_found')} IS NULL",
                (day,),
            )
            updated = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        conn.commit()
        return int(updated)
    except Exception as exc:  # noqa: BLE001
        print(f"   [WARN] suivi_iehe : horodatage des KO restants échoué : {exc}")
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        return 0


# =============================================================================
# STATISTIQUES — script 02
# =============================================================================

def _bucket(total: int, resolus: int) -> Dict[str, Any]:
    return {
        "Total_KO_Initial": int(total),
        "Resolus_Apres_Retry": int(resolus),
        "Encore_KO": int(total - resolus),
        "Taux_Resolution": round(resolus / total * 100, 2) if total > 0 else 0.0,
    }


def fetch_stats(conn) -> Optional[Dict[str, Any]]:
    """Bloc `Retry_IEHE_KO` calculé en SQL.

    Structure identique à celle produite auparavant depuis les CSV, pour ne rien
    casser côté `report_generator.py` : `Totaux`, `Par_Type_Assure`,
    `Par_Eligibilite_TP`, `Detail_Par_Fichier` (une entrée par flux).

    Retourne `None` si la base est injoignable ou la table vide — l'appelant
    retombe alors sur l'ancienne lecture des CSV.
    """
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT {_q('flux_id')},
                       COUNT(*),
                       COUNT(*) FILTER (WHERE {_q('date_found')} IS NOT NULL),
                       MAX({_q('date_derniere_verif')})
                  FROM {FQTN}
                 GROUP BY {_q('flux_id')}
            """)
            rows_flux = cur.fetchall()

            cur.execute(f"""
                SELECT COALESCE(NULLIF(TRIM({_q('NS_type_assure')}), ''), 'INCONNU'),
                       COUNT(*),
                       COUNT(*) FILTER (WHERE {_q('date_found')} IS NOT NULL)
                  FROM {FQTN}
                 GROUP BY 1
            """)
            rows_type = cur.fetchall()

            # Le libellé d'éligibilité n'est pas stocké : il est recalculé ici
            # depuis le couple brut, cf. `_ELIG_LABEL_SQL`.
            cur.execute(f"""
                SELECT {_ELIG_LABEL_SQL},
                       COUNT(*),
                       COUNT(*) FILTER (WHERE {_q('date_found')} IS NOT NULL)
                  FROM {FQTN}
                 GROUP BY 1
            """)
            rows_elig = cur.fetchall()
    except Exception as exc:  # noqa: BLE001
        print(f"   [WARN] suivi_iehe : lecture des statistiques échouée : {exc}")
        return None

    if not rows_flux:
        return None

    # `flux_id` est un TEXT DDMMYYYY : un tri SQL classerait sur le jour avant le
    # mois. On trie côté Python sur la date réelle (même correctif que 08_ged_retry).
    def _flux_key(row):
        from datetime import datetime
        try:
            return (0, datetime.strptime(str(row[0]).strip(), "%d%m%Y").date())
        except (ValueError, TypeError):
            return (1, str(row[0]))

    detail = []
    total = resolus = 0
    for flux_id, nb, nb_ok, last_check in sorted(rows_flux, key=_flux_key):
        total += int(nb)
        resolus += int(nb_ok)
        entry = _bucket(int(nb), int(nb_ok))
        entry["Flux"] = str(flux_id)
        entry["Date_Derniere_Verification"] = (
            last_check.strftime("%d%m%Y") if last_check else ""
        )
        detail.append(entry)

    totaux = _bucket(total, resolus)
    totaux["Fichiers_Traites"] = len(detail)

    return {
        "Source": f"{FQTN} (base supervision)",
        "Totaux": totaux,
        "Par_Type_Assure": {
            str(t): _bucket(int(n), int(k)) for t, n, k in sorted(rows_type)
        },
        "Par_Eligibilite_TP": {
            str(e): _bucket(int(n), int(k)) for e, n, k in sorted(rows_elig)
        },
        "Detail_Par_Fichier": detail,
    }
