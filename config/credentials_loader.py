"""Chargement centralisé des identifiants — `config/credentials.ini`.

Source de vérité unique pour tous les identifiants de connexion. Conçu pour
être édité par un utilisateur non technique (pas de code à toucher).

Robustesse : si le fichier est absent ou une section incomplète, on ne lève
jamais — les modules concernés affichent un état dégradé explicite. Le
singleton est rechargeable à chaud via `reload()` (endpoint /admin/reload-credentials).
"""

from __future__ import annotations

import configparser
import threading
from pathlib import Path

_CONFIG_DIR = Path(__file__).resolve().parent
_INI_PATH = _CONFIG_DIR / "credentials.ini"
_EXAMPLE_PATH = _CONFIG_DIR / "credentials.ini.example"

# Valeur placeholder du .example : traitée comme « non renseigné ».
_PLACEHOLDER = "CHANGER_ICI"

# Mapping section credentials.ini -> environnement/base du SQL runner.
# Permet de pré-remplir le formulaire SQL depuis le fichier central.
_SQL_SECTION_MAP = {
    ("QUALIF", "mdg"): "oracle_mdg_qualif",
    ("PROD",   "mdg"): "oracle_mdg_prod",
    ("QUALIF", "dwh"): "oracle_dwh_qualif",
    ("PROD",   "dwh"): "oracle_dwh_prod",
    ("QUALIF", "iehe"): "postgresql_iehe",
    ("PROD",   "iehe"): "postgresql_iehe",
}


class _Credentials:
    def __init__(self):
        self._lock = threading.Lock()
        self._sections: dict[str, dict[str, str]] = {}
        self._loaded_path: Path | None = None
        self.load()

    # ── Chargement ────────────────────────────────────────────────────
    def load(self) -> None:
        """(Re)lit credentials.ini. Fallback silencieux sur .example si le
        fichier réel est absent (valeurs placeholder = non renseigné)."""
        with self._lock:
            path = _INI_PATH if _INI_PATH.is_file() else _EXAMPLE_PATH
            if not path.is_file():
                self._sections = {}
                self._loaded_path = None
                return
            parser = configparser.ConfigParser()
            try:
                parser.read(path, encoding="utf-8")
            except Exception:
                # Rechargement défaillant (ex. .ini mal édité) : on conserve
                # l'état en mémoire plutôt que de casser l'application.
                return
            new_sections = {}
            for sec in parser.sections():
                new_sections[sec] = {
                    k: v.strip() for k, v in parser.items(sec)
                }
            self._sections = new_sections
            self._loaded_path = path

    def reload(self) -> dict:
        self.load()
        return self.status()

    # ── État ──────────────────────────────────────────────────────────
    def is_real_file(self) -> bool:
        return _INI_PATH.is_file()

    @staticmethod
    def _is_set(v: str | None) -> bool:
        return bool(v) and v != _PLACEHOLDER

    def status(self) -> dict:
        """Synthèse pour le bandeau topbar."""
        complete = []
        for name, sec in self._sections.items():
            if self._is_set(sec.get("user")) and self._is_set(sec.get("password")):
                complete.append(name)
        return {
            "file_present": self.is_real_file(),
            "loaded": self._loaded_path is not None,
            "sections_total": len(self._sections),
            "sections_complete": len(complete),
            "complete": complete,
        }

    # ── Accès ─────────────────────────────────────────────────────────
    def section(self, name: str) -> dict:
        """Retourne une copie de la section (dict vide si absente)."""
        with self._lock:
            return dict(self._sections.get(name, {}))

    def get(self, name: str, key: str, default: str | None = None) -> str | None:
        val = self.section(name).get(key)
        return val if self._is_set(val) else default

    def credentials_for(self, name: str) -> tuple[str | None, str | None]:
        """(user, password) d'une section, ou (None, None) si non renseigné."""
        sec = self.section(name)
        u, p = sec.get("user"), sec.get("password")
        if self._is_set(u) and self._is_set(p):
            return u, p
        return None, None

    def supervision_dsn(self) -> str | None:
        """DSN SQLAlchemy pour la base supervision (dashboard), ou None."""
        sec = self.section("postgresql_supervision")
        u, p = self.credentials_for("postgresql_supervision")
        if not u or not p or not sec.get("host"):
            return None
        from urllib.parse import quote_plus
        return (f"postgresql+psycopg://{quote_plus(u)}:{quote_plus(p)}@{sec['host']}:"
                f"{sec.get('port','5432')}/{sec.get('dbname','')}")

    def sql_prefill(self, env_name: str) -> dict:
        """Identifiants pré-remplis pour le formulaire SQL runner (sans mot
        de passe renvoyé en clair : on indique seulement s'il est connu)."""
        out = {"ora_user": "", "ora_pass_set": False,
               "iehe_user": "", "iehe_pass_set": False}
        env_name = (env_name or "").upper()
        mdg = self.section(_SQL_SECTION_MAP.get((env_name, "mdg"), ""))
        if self._is_set(mdg.get("user")):
            out["ora_user"] = mdg["user"]
            out["ora_pass_set"] = self._is_set(mdg.get("password"))
        iehe = self.section(_SQL_SECTION_MAP.get((env_name, "iehe"), ""))
        if self._is_set(iehe.get("user")):
            out["iehe_user"] = iehe["user"]
            out["iehe_pass_set"] = self._is_set(iehe.get("password"))
        return out

    def sql_secret_for(self, env_name: str, db_key: str) -> tuple[str | None, str | None]:
        """(user, password) réels pour une base SQL donnée — usage serveur
        uniquement (ne jamais renvoyer le mot de passe au navigateur)."""
        return self.credentials_for(_SQL_SECTION_MAP.get(((env_name or "").upper(), db_key), ""))


# Singleton applicatif.
CREDENTIALS = _Credentials()
