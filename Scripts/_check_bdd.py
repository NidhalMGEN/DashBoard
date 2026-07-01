# -*- coding: utf-8 -*-
"""
_check_bdd.py — Validation des credentials BDD avant le démarrage du pipeline.
Appelé par ETL_vf.bat après la saisie utilisateur.
Exit 0 = OK | Exit 1 = échec connexion | Exit 2 = psycopg non disponible
"""
import os
import sys

PG_HOST = "bdd-X0ED0550.alias"
PG_PORT = 5559
PG_DB   = "choregie_db"
HOSTS_FALLBACK = [("bdd-X0ED0550.alias", 5559, "choregie_db"),
                  ("100.54.41.6",         5432, "postgres")]

user     = os.environ.get("PG_USER", "").strip()
password = os.environ.get("PG_PASSWORD", "").strip()

if not user:
    print("[ERREUR] PG_USER non renseigne.", flush=True)
    sys.exit(1)

try:
    import psycopg
except ImportError:
    print("[AVERT]  psycopg non installe — validation ignoree.", flush=True)
    sys.exit(2)

last_error = None
for host, port, db in HOSTS_FALLBACK:
    try:
        conn = psycopg.connect(
            host=host, port=port, dbname=db,
            user=user, password=password,
            connect_timeout=5,
        )
        conn.close()
        print(f"[OK]     Connexion BDD validee ({host}:{port}/{db})", flush=True)
        sys.exit(0)
    except Exception as e:
        last_error = e

print(f"[ERREUR] Connexion BDD impossible : {last_error}", flush=True)
sys.exit(1)
