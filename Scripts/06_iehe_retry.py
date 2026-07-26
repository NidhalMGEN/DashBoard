# -*- coding: utf-8 -*-
"""
06_iehe_retry.py — Re-vérifie en base les personnes non trouvées dans IEHE.

Ce script ne lit et n'écrit plus aucun CSV. L'état vit dans la table
`rptpsc.suivi_iehe` (base supervision) : `date_found IS NULL` remplace
l'ancien `statut_retry == "KO"` des fichiers `Output/{prefix}_IEHE_KO.csv`.

Déroulé (entièrement automatique, aucune intervention manuelle)
---------------------------------------------------------------
  1. Lit dans `suivi_iehe` toutes les personnes encore en attente, TOUS FLUX
     CONFONDUS (`fetch_pending`).
  2. Interroge `iehe.refkpep` par lots (une requête par lot, pas par personne).
  3. Solde les personnes trouvées : `date_found`, `mail_iehe`, `kpep_iehe`.
  4. Horodate `date_derniere_verif` sur celles toujours absentes.

Pourquoi le retry n'ouvre jamais un fichier d'un flux antérieur
----------------------------------------------------------------
Les CSV `{prefix}_NS_IEHE.csv` et `{prefix}_IEHE_KO.csv` sont des PHOTOS du flux
du jour J — ils décrivent ce qui était vrai ce jour-là et n'ont pas à changer.
Le fait « trouvée le J+3 » est un fait POSTÉRIEUR : il appartient à la table, et
la colonne `date_found` y répond déjà. Réécrire les CSV détruirait la photo.

Une personne trouvée est soldée sur TOUS ses flux d'un coup : elle existe ou non
dans IEHE, la réponse ne dépend pas du flux qui l'a signalée. Même choix que
`08_ged_retry.py` côté GED.

Usage
-----
    python 06_iehe_retry.py                  # tous les flux en attente
    python 06_iehe_retry.py --flux 17022026  # limite le scan à un flux
    python 06_iehe_retry.py --dry-run        # interroge IEHE sans rien écrire
"""

import argparse
import os
import sys
from datetime import date

try:
    import psycopg
except ImportError:
    print("[ERREUR] Module 'psycopg' non installé. Installez-le avec : pip install psycopg[binary]")
    sys.exit(1)

import suivi_iehe_db

# --- CONFIGURATION ---
# Base IEHE (lecture seule) — section [postgresql_iehe] de config/credentials.ini.
PG_USER = os.environ.get("PG_USER", "u_lpillon")
PG_PASSWORD = os.environ.get("PG_PASSWORD", "T_Run_Asc_2025#")
IEHE_SCHEMA = "iehe"
IEHE_TABLE = "refkpep"
IEHE_COL_ID = "refperboccn"

BATCH_SIZE = 5000


# --- CONNEXION IEHE ---

def connect_pg(host, port, db):
    try:
        return psycopg.connect(
            host=host, port=port, dbname=db,
            user=PG_USER, password=PG_PASSWORD,
            connect_timeout=5,
        )
    except Exception:
        return None


def connect_iehe_auto():
    """Connexion IEHE avec fallback (même logique que 01_generation_donnees.py)."""
    hosts = ["bdd-X0ED0550.alias", "100.54.41.6"]
    ports = [5559, 5432]
    dbs = ["choregie_db", "postgres"]

    for h in hosts:
        for p in ports:
            for d in dbs:
                conn = connect_pg(h, p, d)
                if conn:
                    print(f"[OK] Connecté à IEHE {h}:{p}/{d}")
                    return conn
    print("[ERREUR] Impossible de se connecter à la BDD IEHE.")
    return None


# --- REQUETE IEHE ---

def query_iehe_details(conn, num_personnes):
    """Interroge IEHE par num_personne, par lots.

    Retourne `{ num_personne: {"mail": ..., "kpep": ...} }` pour les seules
    personnes trouvées : une clé absente du dict = toujours absente d'IEHE.
    """
    if not num_personnes:
        return {}

    result = {}
    nb_lots = (len(num_personnes) + BATCH_SIZE - 1) // BATCH_SIZE
    for i in range(0, len(num_personnes), BATCH_SIZE):
        batch = num_personnes[i:i + BATCH_SIZE]
        sql = f"""
        WITH input_ids AS (
            SELECT unnest(%(vals)s::text[]) AS v
        ),
        matched AS (
            SELECT r1.{IEHE_COL_ID} AS num_personne,
                   r1.idrpp,
                   r1.adrmailctc AS mail
            FROM   {IEHE_SCHEMA}.{IEHE_TABLE} r1
            JOIN   input_ids ON input_ids.v = r1.{IEHE_COL_ID}
        ),
        kpep_map AS (
            SELECT DISTINCT ON (idrpp)
                   idrpp,
                   {IEHE_COL_ID} AS kpep
            FROM   {IEHE_SCHEMA}.{IEHE_TABLE}
            WHERE  {IEHE_COL_ID} LIKE 'KPEP%%'
            ORDER  BY idrpp, {IEHE_COL_ID}
        )
        SELECT DISTINCT ON (m.num_personne)
               m.num_personne,
               m.mail,
               k.kpep
        FROM   matched m
        LEFT JOIN kpep_map k ON k.idrpp = m.idrpp
        ORDER  BY m.num_personne
        """
        with conn.cursor() as cur:
            cur.execute(sql, {"vals": batch})
            for row in cur.fetchall():
                result[row[0]] = {"mail": row[1] or "", "kpep": row[2] or ""}
        print(f"   [QUERY] lot {i // BATCH_SIZE + 1}/{nb_lots} "
              f"({len(batch)} personnes) → {len(result)} trouvée(s) cumulées")

    return result


# --- MAIN ---

def main():
    parser = argparse.ArgumentParser(
        description="Retry IEHE piloté par la table rptpsc.suivi_iehe"
    )
    parser.add_argument(
        "--flux", default=None,
        help="Préfixe DDMMYYYY : limite le SCAN à ce flux. Une personne trouvée "
             "reste soldée sur tous ses flux.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Interroge IEHE et affiche le bilan sans écrire en base.",
    )
    args = parser.parse_args()

    print(f"\n{'='*60}")
    print(f"  06_iehe_retry.py — Retry IEHE (source : {suivi_iehe_db.FQTN})")
    print(f"{'='*60}")

    # 1. Base de suivi
    conn_suivi = suivi_iehe_db.connect_supervision()
    if conn_suivi is None:
        print("\n[ERREUR] Base de suivi injoignable — retry impossible.")
        return 1
    if not suivi_iehe_db.ensure_table(conn_suivi):
        print("\n[ERREUR] Table de suivi indisponible — retry impossible.")
        conn_suivi.close()
        return 1

    try:
        # 2. Personnes en attente
        par_flux = suivi_iehe_db.fetch_pending_by_flux(conn_suivi)
        pending = suivi_iehe_db.fetch_pending(conn_suivi, flux_id=args.flux)

        if not pending:
            print("\n[INFO] Aucune personne en attente : rien à re-vérifier.")
            return 0

        print(f"\n[INFO] {len(pending)} personne(s) unique(s) en attente"
              + (f" (scan limité au flux {args.flux})" if args.flux else ""))
        if par_flux:
            print("       Répartition des lignes en attente par flux :")
            for flux_id, nb in sorted(par_flux.items()):
                print(f"         - flux {flux_id} : {nb} ligne(s)")

        # 3. Interrogation IEHE
        conn_iehe = connect_iehe_auto()
        if conn_iehe is None:
            return 1
        try:
            found = query_iehe_details(conn_iehe, pending)
        finally:
            conn_iehe.close()

        nb_found = len(found)
        nb_still = len(pending) - nb_found

        if args.dry_run:
            print(f"\n[DRY-RUN] {nb_found} personne(s) seraient soldées, "
                  f"{nb_still} resteraient en attente. Aucune écriture.")
            return 0

        # 4. Mise à jour de l'état
        today = date.today()
        nb_soldees = suivi_iehe_db.mark_found(conn_suivi, found, today)
        # Après mark_found, les lignes encore NULL sont exactement les non trouvées.
        nb_horodatees = suivi_iehe_db.mark_checked(conn_suivi, today)

        print(f"\n{'='*60}")
        print(f"  RÉSUMÉ")
        print(f"{'='*60}")
        print(f"  Personnes re-vérifiées : {len(pending)}")
        print(f"  Trouvées dans IEHE     : {nb_found}")
        print(f"  Lignes soldées en base : {nb_soldees}  (une personne peut "
              f"solder plusieurs flux)")
        print(f"  Toujours absentes      : {nb_still} "
              f"({nb_horodatees} ligne(s) horodatée(s))")
        print(f"  Date de vérification   : {today.strftime('%d/%m/%Y')}")
        print()
        return 0

    finally:
        try:
            conn_suivi.close()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    sys.exit(main())
