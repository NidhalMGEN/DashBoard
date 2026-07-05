import psycopg, os, sys
PG_USER = os.environ.get("PG_USER", "")
PG_PASSWORD = os.environ.get("PG_PASSWORD", "")
print("PG_USER défini :", bool(PG_USER))
print("PG_PASSWORD défini :", bool(PG_PASSWORD))
try:
    conn = psycopg.connect(host="bdd-X0ED0550.alias", port=5559, dbname="choregie_db",
                            user=PG_USER, password=PG_PASSWORD, connect_timeout=5)
    print("CONNEXION OK")
except Exception as e:
    print("ERREUR REELLE :", type(e).__name__, "-", e)
