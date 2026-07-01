"""Moteur d'anonymisation RGPD pour les tickets support S@M (MGEN).

Stratégie en 3 passes :
1. Colonnes structurées (Bénéficiaire, Résolu par) : substitution directe.
2. Colonne Description semi-structurée : regex sur Nom/Prénom/KPEP/Email/Tel.
3. Textes libres (Résolution, Titre…) : remplacement des noms connus.
"""
from __future__ import annotations

import io
import random
import re
import string
from typing import Any

import pandas as pd

# Colonnes avec noms directement en clair
NAME_COLS = ["Bénéficiaire", "Résolu par (intervenant)"]

# Colonnes de texte libre contenant des noms connus
FREE_TEXT_COLS = [
    "Résolution", "Résolution Immédiate", "Résolution technique",
    "Titre", "Diagnostic",
]

# Colonnes avec description semi-structurée (format NomXXXPrénomYYY…)
SEMI_STRUCT_COLS = ["Description"]

# ── Regex ──────────────────────────────────────────────────────────────────
# NOM : patronyme en MAJUSCULES — lookahead pour ne pas mordre sur le P de Prénom
_NOM_RE    = re.compile(
    r'(Nom)([A-ZÀÉÈÊËÏÎÔÙÛÜ]+)(?=Prénom|KPEP|Adresse|\s|$)'
)
# PRÉNOM : prénom en casse titre (ex: Blandine, Jean, Jean-Pierre)
_PRENOM_RE = re.compile(
    r'(Prénom)([A-ZÀÉÈÊËÏÎÔÙÛÜ][a-zàéèêëïîôùûü]{1,30}'
    r'(?:-[A-ZÀÉÈÊËÏÎÔÙÛÜ][a-zàéèêëïîôùûü]{1,30})*)'
)
_KPEP_RE   = re.compile(r'(KPEP)(\d{10,20})')
_EMAIL_RE  = re.compile(r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b')
_PHONE_RE  = re.compile(
    r'\b(?:\+33[\s.\-]?[1-9](?:[\s.\-]?\d{2}){4}'
    r'|0[1-9](?:[\s.\-]?\d{2}){4})\b'
)
_NIR_RE = re.compile(
    r'\b[12][\s.]?\d{2}[\s.]?\d{2}[\s.]?\d{2,3}[\s.]?\d{3}[\s.]?\d{3}[\s.]?\d{2}\b'
)


def _match_case(original: str, fake_val: str) -> str:
    """Adapte la casse du remplacement à celle de l'original."""
    if original.isupper():
        return fake_val.upper()
    if original.istitle():
        return fake_val.title()
    return fake_val


class TicketAnonymizer:
    """Anonymise les DataFrames de tickets en plusieurs passes cohérentes."""

    def __init__(self) -> None:
        self._name_map:  dict[str, str] = {}
        self._nom_map:   dict[str, str] = {}
        self._prenom_map: dict[str, str] = {}
        self._email_map: dict[str, str] = {}
        self._phone_map: dict[str, str] = {}
        self._kpep_map:  dict[str, str] = {}

        # Faker instancié par instance pour garantir l'isolation thread
        try:
            from faker import Faker as _Faker
            self._fake = _Faker("fr_FR")
            self._fake.seed_instance(42)
            self._has_faker = True
        except ImportError:
            self._fake = None  # type: ignore[assignment]
            self._has_faker = False

    # ── Générateurs cohérents ──────────────────────────────────────────────

    def _fake_name(self, original: str) -> str:
        """Remplace un nom complet (Nom + Prénom) par un équivalent fictif."""
        key = original.strip().lower()
        if key not in self._name_map:
            if self._has_faker:
                fake = f"{self._fake.last_name()} {self._fake.first_name()}"
            else:
                rnd = ''.join(random.choices(string.ascii_uppercase, k=6))
                fake = f"{rnd} Prénom"
            self._name_map[key] = fake
        return _match_case(original.strip(), self._name_map[key])

    def _fake_nom(self, original: str) -> str:
        """Remplace un patronyme seul (colonne Nom ou regex Nom dans Description)."""
        key = original.strip().lower()
        if key not in self._nom_map:
            if self._has_faker:
                self._nom_map[key] = self._fake.last_name()
            else:
                self._nom_map[key] = ''.join(random.choices(string.ascii_uppercase, k=6))
        return _match_case(original.strip(), self._nom_map[key])

    def _fake_prenom(self, original: str) -> str:
        """Remplace un prénom seul (regex Prénom dans Description)."""
        key = original.strip().lower()
        if key not in self._prenom_map:
            if self._has_faker:
                self._prenom_map[key] = self._fake.first_name()
            else:
                rnd = ''.join(random.choices(string.ascii_lowercase, k=5))
                self._prenom_map[key] = rnd.capitalize()
        return _match_case(original.strip(), self._prenom_map[key])

    def _fake_email(self, original: str) -> str:
        key = original.lower()
        if key not in self._email_map:
            if self._has_faker:
                self._email_map[key] = self._fake.email()
            else:
                rnd = ''.join(random.choices(string.ascii_lowercase, k=8))
                self._email_map[key] = f"{rnd}@example.fr"
        return self._email_map[key]

    def _fake_phone(self, original: str) -> str:
        key = re.sub(r'\D', '', original)
        if key not in self._phone_map:
            digits = ''.join(str(random.randint(0, 9)) for _ in range(8))
            prefix = random.choice(["06", "07"])
            self._phone_map[key] = f"{prefix} {digits[:2]} {digits[2:4]} {digits[4:6]} {digits[6:]}"
        return self._phone_map[key]

    def _fake_kpep(self, original: str) -> str:
        if original not in self._kpep_map:
            self._kpep_map[original] = ''.join(
                str(random.randint(0, 9)) for _ in range(len(original))
            )
        return self._kpep_map[original]

    # ── Passes d'anonymisation ─────────────────────────────────────────────

    def _anon_description(self, text: Any) -> Any:
        """Parse le format semi-structuré : NomXXX / PrénomYYY / KPEP / email / tel."""
        if not isinstance(text, str):
            return text
        text = _EMAIL_RE.sub(lambda m: self._fake_email(m.group()), text)
        text = _PHONE_RE.sub(lambda m: self._fake_phone(m.group()), text)
        text = _NIR_RE.sub("[NIR]", text)
        text = _NOM_RE.sub(lambda m: m.group(1) + self._fake_nom(m.group(2)), text)
        text = _PRENOM_RE.sub(lambda m: m.group(1) + self._fake_prenom(m.group(2)), text)
        text = _KPEP_RE.sub(lambda m: m.group(1) + self._fake_kpep(m.group(2)), text)
        return text

    def _anon_free_text(self, text: Any, pattern: re.Pattern | None,
                        name_map_lower: dict[str, str]) -> Any:
        """Remplace les noms connus dans du texte libre via une regex combinée."""
        if not isinstance(text, str):
            return text
        text = _EMAIL_RE.sub(lambda m: self._fake_email(m.group()), text)
        text = _PHONE_RE.sub(lambda m: self._fake_phone(m.group()), text)
        if pattern is not None:
            text = pattern.sub(lambda m: name_map_lower.get(m.group(0).lower(), m.group(0)), text)
        return text

    # ── Point d'entrée ─────────────────────────────────────────────────────

    def anonymize_dataframes(
        self, sheets: dict[str, pd.DataFrame]
    ) -> dict[str, pd.DataFrame]:
        """Anonymise toutes les feuilles d'un fichier."""
        result: dict[str, pd.DataFrame] = {}

        # Passe 1 : recenser tous les noms des colonnes structurées et pré-remplir le mapping
        all_names: list[str] = []
        for df in sheets.values():
            for col in NAME_COLS:
                if col in df.columns:
                    for val in df[col].dropna().unique():
                        s = str(val).strip()
                        if s and len(s) > 2:
                            all_names.append(s)
                            self._fake_name(s)  # pré-génère le mapping

        # Regex combinée pour tous les noms connus — une seule passe sur le texte libre
        known_pairs: list[tuple[str, str]] = sorted(
            [(n, self._fake_name(n)) for n in set(all_names)],
            key=lambda x: len(x[0]), reverse=True,
        )
        combined_pattern: re.Pattern | None = None
        name_map_lower: dict[str, str] = {}
        if known_pairs:
            name_map_lower = {real.lower(): fake for real, fake in known_pairs}
            escaped = [re.escape(real) for real, _ in known_pairs]
            combined_pattern = re.compile("|".join(escaped), re.IGNORECASE)

        # Passe 2 & 3 : traiter chaque feuille
        for sheet_name, df in sheets.items():
            df = df.copy()

            for col in NAME_COLS:
                if col in df.columns:
                    df[col] = df[col].apply(
                        lambda v: self._fake_name(str(v)) if pd.notna(v) and str(v).strip() else v
                    )

            for col in SEMI_STRUCT_COLS:
                if col in df.columns:
                    df[col] = df[col].apply(self._anon_description)

            for col in FREE_TEXT_COLS:
                if col in df.columns:
                    df[col] = df[col].apply(
                        lambda v: self._anon_free_text(v, combined_pattern, name_map_lower)
                    )

            result[sheet_name] = df

        return result

    def to_excel(self, anon_sheets: dict[str, pd.DataFrame]) -> io.BytesIO:
        """Sérialise les feuilles anonymisées dans un BytesIO Excel."""
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            for name, df in anon_sheets.items():
                df.to_excel(writer, sheet_name=name[:31], index=False)
        buf.seek(0)
        return buf

    @property
    def stats(self) -> dict[str, int]:
        return {
            "noms_uniques":       len(self._name_map),
            "emails_uniques":     len(self._email_map),
            "telephones_uniques": len(self._phone_map),
            "kpep_uniques":       len(self._kpep_map),
        }
