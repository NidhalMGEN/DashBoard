"""Chargement des environnements SQL — extraction de launch_SQL_query_V2.py.

Lit `input_data/SQL/environments.ini`. Une section `<ENV>:<base>` par couple
(ex. `[QUALIF:mdg]`, `[PROD:iehe]`). Comportement métier conservé à
l'identique : validation des clés requises, fallback sur la configuration
historique en dur si le fichier est absent/vide.
"""

from __future__ import annotations

import configparser
from pathlib import Path

_ENV_FILE_NAME = "environments.ini"

# Fallback historique (compat : lancement isolé sans environments.ini).
_DEFAULT_ENVIRONMENTS = {
    "QUALIF": {
        "mdg": {"host": "bdd-S4PSYY00.alias", "port": "1521", "sid": "S4PSYY00", "type": "oracle"},
        "dwh": {"host": "bdd-X0PSYY02.alias", "port": "1521", "sid": "X0PSYY02", "type": "oracle"},
    },
    "PROD": {
        "mdg": {"host": "bdd-X0PSYY00.alias", "port": "1521", "sid": "X0PSYY00", "type": "oracle"},
        "dwh": {"host": "bdd-X0PSYY02.alias", "port": "1521", "sid": "X0PSYY02", "type": "oracle"},
    },
}

# Clés requises par TYPE de base (validation au chargement).
_REQUIRED_BY_TYPE: dict[str, tuple[str, ...]] = {
    "oracle":     ("host", "port", "sid"),
    "postgresql": ("host", "port", "dbname"),
}

# Type déduit pour les bases historiques (compat : pas de clé `type` requise).
_KNOWN_TYPE: dict[str, str] = {
    "mdg":  "oracle",
    "dwh":  "oracle",
    "iehe": "postgresql",
}


def _resolve_type(db_name: str, opts: dict) -> str | None:
    """Détermine le type d'une base : clé `type` explicite, sinon nom connu,
    sinon déduction (sid -> oracle, dbname -> postgresql)."""
    t = (opts.get("type") or "").strip().lower()
    if t in _REQUIRED_BY_TYPE:
        return t
    if db_name in _KNOWN_TYPE:
        return _KNOWN_TYPE[db_name]
    if opts.get("sid"):
        return "oracle"
    if opts.get("dbname"):
        return "postgresql"
    return None


def load_environments(ini_path: Path) -> tuple[dict, set[str]]:
    """Retourne (ENVIRONMENTS, VALID_DBS). Sections mal formées ignorées
    avec avertissement collecté (jamais d'exception remontée)."""
    if not ini_path.is_file():
        envs = {k: {b: dict(v) for b, v in bases.items()}
                for k, bases in _DEFAULT_ENVIRONMENTS.items()}
        return envs, {b for bases in envs.values() for b in bases}

    parser = configparser.ConfigParser()
    parser.read(ini_path, encoding="utf-8")

    envs: dict[str, dict[str, dict[str, str]]] = {}
    for section in parser.sections():
        if ":" not in section:
            continue
        env_name, db_name = section.split(":", 1)
        env_name = env_name.strip()
        db_name = db_name.strip().lower()
        opts = {k: v.strip() for k, v in parser.items(section) if v and v.strip()}
        db_type = _resolve_type(db_name, opts)
        required = _REQUIRED_BY_TYPE.get(db_type) if db_type else None
        if required is None:
            continue  # type inconnu / indéterminable -> section ignorée
        if [k for k in required if not opts.get(k)]:
            continue
        opts["type"] = db_type  # type résolu, exploité par l'executor
        envs.setdefault(env_name, {})[db_name] = opts

    if not envs:
        envs = {k: {b: dict(v) for b, v in bases.items()}
                for k, bases in _DEFAULT_ENVIRONMENTS.items()}

    valid_dbs = {b for bases in envs.values() for b in bases}
    return envs, valid_dbs


def default_sql_dir(base_dir: Path) -> Path:
    """Répertoire des requêtes SQL : `input_data/SQL` (casse réelle tolérée)."""
    for name in ("input_data", "Input_Data"):
        cand = base_dir / name / "SQL"
        if cand.is_dir():
            return cand
    return base_dir / "input_data" / "SQL"
