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
from datetime import date
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

_DDL = f"""
CREATE TABLE IF NOT EXISTS {FQTN} (
    flux_id             TEXT NOT NULL,
    num_personne        TEXT NOT NULL,
    date_found          DATE,
    date_derniere_verif DATE,
    type_assure         TEXT,
    offre               TEXT,
    code_soc            TEXT,
    eligibilite_tp      TEXT,
    kpep_iehe           TEXT,
    mail_iehe           TEXT,
    motif               TEXT,
    PRIMARY KEY (flux_id, num_personne)
)
"""

# La PK (flux_id, num_personne) est inutilisable pour les requêtes par personne
# sans flux_id — or c'est exactement ce que fait le retry (une personne trouvée
# solde ses lignes de TOUS les flux). D'où l'index dédié.
_DDL_IDX = (
    f"CREATE INDEX IF NOT EXISTS idx_{SUIVI_TABLE}_personne "
    f"ON {FQTN} (num_personne)"
)
# Index partiel : le scan des KO en attente ne lit que cette fraction de table.
_DDL_IDX_PENDING = (
    f"CREATE INDEX IF NOT EXISTS idx_{SUIVI_TABLE}_pending "
    f"ON {FQTN} (num_personne) WHERE date_found IS NULL"
)


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
    """Crée table + index s'ils n'existent pas. Retourne `False` si échec."""
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
    """
    elig = (eligibilite_o_n or "").strip().upper()
    valeur = (valeur_carte_tp or "").strip().upper()
    if elig != "O":
        return ELIG_HORS
    return ELIG_FUTURE if valeur.startswith("FUTUR") else ELIG_ELIGIBLE


def upsert_ko_rows(conn, flux_id: str, rows: Sequence[Dict[str, Any]]) -> int:
    """Insère les personnes absentes d'IEHE pour ce flux.

    `rows` : dicts portant `num_personne` (obligatoire) et, optionnellement,
    `type_assure`, `offre`, `code_soc`, `eligibilite_tp`, `motif`.

    ⚠️ En cas de conflit (re-run du script 03 sur le même préfixe), on rafraîchit
    UNIQUEMENT les colonnes d'enrichissement. `date_found`, `date_derniere_verif`,
    `kpep_iehe` et `mail_iehe` ne sont jamais réécrites : sinon un rejeu du 03
    effacerait le résultat des retries déjà effectués.

    Retourne le nombre de lignes envoyées (0 si indisponible).
    """
    if conn is None or not rows or not flux_id:
        return 0

    payload = []
    seen = set()
    for r in rows:
        pid = str(r.get("num_personne", "") or "").strip()
        if not pid or pid in seen:
            continue
        seen.add(pid)
        payload.append((
            str(flux_id).strip(),
            pid,
            (str(r.get("type_assure", "") or "").strip() or None),
            (str(r.get("offre", "") or "").strip() or None),
            (str(r.get("code_soc", "") or "").strip() or None),
            (str(r.get("eligibilite_tp", "") or "").strip() or None),
            (str(r.get("motif", "") or "").strip() or None),
        ))

    if not payload:
        return 0

    sql = f"""
        INSERT INTO {FQTN}
            (flux_id, num_personne, type_assure, offre, code_soc,
             eligibilite_tp, motif, date_found, date_derniere_verif)
        VALUES (%s, %s, %s, %s, %s, %s, %s, NULL, NULL)
        ON CONFLICT (flux_id, num_personne) DO UPDATE SET
            type_assure    = EXCLUDED.type_assure,
            offre          = EXCLUDED.offre,
            code_soc       = EXCLUDED.code_soc,
            eligibilite_tp = EXCLUDED.eligibilite_tp,
            motif          = EXCLUDED.motif
    """
    try:
        with conn.cursor() as cur:
            cur.executemany(sql, payload)
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
    sql = (f"SELECT DISTINCT num_personne FROM {FQTN} "
           f"WHERE date_found IS NULL AND num_personne <> ''")
    params: tuple = ()
    if flux_id:
        sql += " AND flux_id = %s"
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
                f"SELECT flux_id, COUNT(*) FROM {FQTN} "
                f"WHERE date_found IS NULL GROUP BY flux_id ORDER BY flux_id"
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
           SET date_found          = %s,
               date_derniere_verif = %s,
               mail_iehe           = COALESCE(NULLIF(%s, ''), mail_iehe),
               kpep_iehe           = COALESCE(NULLIF(%s, ''), kpep_iehe)
         WHERE num_personne = %s
           AND date_found IS NULL
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
                f"UPDATE {FQTN} SET date_derniere_verif = %s WHERE date_found IS NULL",
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
                SELECT flux_id,
                       COUNT(*),
                       COUNT(*) FILTER (WHERE date_found IS NOT NULL),
                       MAX(date_derniere_verif)
                  FROM {FQTN}
                 GROUP BY flux_id
            """)
            rows_flux = cur.fetchall()

            cur.execute(f"""
                SELECT COALESCE(NULLIF(TRIM(type_assure), ''), 'INCONNU'),
                       COUNT(*),
                       COUNT(*) FILTER (WHERE date_found IS NOT NULL)
                  FROM {FQTN}
                 GROUP BY 1
            """)
            rows_type = cur.fetchall()

            cur.execute(f"""
                SELECT COALESCE(NULLIF(TRIM(eligibilite_tp), ''), '{ELIG_HORS}'),
                       COUNT(*),
                       COUNT(*) FILTER (WHERE date_found IS NOT NULL)
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
