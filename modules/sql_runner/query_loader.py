"""Scan et parsing des requêtes SQL — extraction de launch_SQL_query_V2.py.

Conserve les conventions existantes :
  * sous-dossier = catégorie ; `DDL/` exclu ;
  * préfixe `J_` = quotidien, `S_` = hebdo, autres ignorés ;
  * métadonnées en en-tête : `-- db:`, `-- post_process:`, `-- param.X: prompt [defaut]` ;
  * placeholders `{NOM}` validés contre les déclarations (`DATE_SUIVI` toléré).
"""

from __future__ import annotations

import re
from pathlib import Path

PREFIX_TO_FREQ = {"J_": "quotidien", "S_": "hebdo"}

_PLACEHOLDER_RE = re.compile(r"\{([A-Z][A-Z0-9_]*)\}")
_META_RE = re.compile(r"^\s*--\s*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.+?)\s*$")
_PARAM_RE = re.compile(r"^\s*--\s*param\.([A-Z][A-Z0-9_]*)\s*:\s*(.+?)\s*$")
_DEFAULT_RE = re.compile(r"^(?P<prompt>.+?)\s*\[(?P<default>[^\]]+)\]\s*$")

_EXCLUDED_SUBDIRS = {"DDL"}


def _parse_sql_file(path: Path) -> dict:
    """Extrait meta / params / params_order / sql d'un fichier .sql."""
    text = path.read_text(encoding="utf-8-sig")
    lines = text.splitlines()

    meta: dict[str, str] = {}
    params: dict[str, dict[str, str | None]] = {}
    params_order: list[str] = []
    i = 0
    while i < len(lines):
        raw = lines[i]
        stripped = raw.strip()
        if stripped == "":
            i += 1
            continue
        m_param = _PARAM_RE.match(raw)
        if m_param is not None:
            name = m_param.group(1)
            rest = m_param.group(2).strip()
            md = _DEFAULT_RE.match(rest)
            if md:
                prompt = md.group("prompt").strip()
                default = md.group("default").strip()
            else:
                prompt, default = rest, None
            if name not in params:
                params[name] = {"prompt": prompt, "default": default}
                params_order.append(name)
            i += 1
            continue
        m = _META_RE.match(raw)
        if m is None:
            break
        meta[m.group(1).lower()] = m.group(2).strip()
        i += 1

    sql_body = "\n".join(lines[i:]).strip()
    while sql_body.endswith((";", "/")):
        sql_body = sql_body[:-1].rstrip()
    return {"meta": meta, "params": params,
            "params_order": params_order, "sql": sql_body}


def _detect_freq(filename: str) -> str | None:
    for prefix, freq in PREFIX_TO_FREQ.items():
        if filename.startswith(prefix):
            return freq
    return None


def load_queries(sql_dir: Path, valid_dbs: set[str]) -> tuple[dict, list[str]]:
    """Scanne `sql_dir`. Retourne (queries, erreurs).

    `queries` : { nom_feuille: {freq, db, sql, category, params, params_order, post_process?} }
    `erreurs` : liste de messages (métadonnées invalides) — non bloquant côté web,
                l'IHM affiche les requêtes valides et signale les erreurs.
    """
    queries: dict[str, dict] = {}
    erreurs: list[str] = []
    if not sql_dir.is_dir():
        return queries, [f"Dossier SQL introuvable : {sql_dir}"]

    files: list[tuple[Path, str]] = []
    for p in sorted(sql_dir.rglob("*.sql")):
        rel = p.relative_to(sql_dir).parts
        if rel[0] in _EXCLUDED_SUBDIRS and len(rel) > 1:
            continue
        freq = _detect_freq(p.name)
        if freq is None:
            continue
        files.append((p, freq))

    for path, freq in files:
        name = path.stem[2:]
        parsed = _parse_sql_file(path)
        meta, sql_body = parsed["meta"], parsed["sql"]
        rel_parts = path.relative_to(sql_dir).parts
        category = rel_parts[0] if len(rel_parts) > 1 else "Autre"

        if not sql_body:
            erreurs.append(f"{path.name} : corps SQL vide")
            continue
        db = meta.get("db", "").lower()
        if db not in valid_dbs:
            erreurs.append(f"{path.name} : db '{db}' invalide (attendu {sorted(valid_dbs)})")
            continue

        placeholders = set(_PLACEHOLDER_RE.findall(sql_body))
        declared = set(parsed["params"].keys())
        missing = sorted(placeholders - declared - {"DATE_SUIVI"})
        if missing:
            erreurs.append(f"{path.name} : placeholders non déclarés {missing}")
            continue

        entry = {
            "freq": freq, "db": db, "sql": sql_body, "category": category,
            "params": parsed["params"], "params_order": parsed["params_order"],
        }
        pp = meta.get("post_process")
        if pp:
            entry["post_process"] = pp
        queries[name] = entry

    return queries, erreurs


def queries_to_catalog(queries: dict) -> list[dict]:
    """Représentation JSON-safe pour l'IHM (sans le corps SQL volumineux)."""
    out = []
    for name, q in queries.items():
        out.append({
            "name": name, "category": q["category"], "freq": q["freq"],
            "db": q["db"],
            "params": [{"name": p, **q["params"][p]} for p in q["params_order"]
                       if p != "DATE_SUIVI"],
            "post_process": q.get("post_process"),
        })
    return out
