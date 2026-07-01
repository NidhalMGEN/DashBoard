"""Calcul des KPIs et génération du prompt IA — Module Analyse Ticket S@M."""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

MAIN_SHEET   = "Cop-Col inputs Stock"
LEGACY_SHEET = "oct - Historique clôturé Legacy"

# Seuils SLA par défaut en jours (modifiables)
SLA_JOURS: dict[int, float] = {1: 0.5, 2: 1, 3: 3, 4: 5, 5: 10}


def _safe_pct(num: int, den: int) -> float:
    return round(num / den * 100, 1) if den else 0.0


def _top_n(series: pd.Series, n: int = 12) -> list[dict]:
    counts = series.dropna().value_counts().head(n)
    return [{"label": str(k), "count": int(v)} for k, v in counts.items()]


def _conv_dates(col: pd.Series) -> pd.Series:
    """Convertit une colonne de dates (floats Excel serial ou strings) en datetime."""
    if col.dtype.kind == "f":
        return pd.to_datetime(col, unit="D", origin="1899-12-30", errors="coerce")
    converted = pd.to_datetime(col, errors="coerce")
    if col.dtype == object and converted.isna().all() and len(col) > 0:
        numeric = pd.to_numeric(col, errors="coerce")
        if numeric.notna().any():
            return pd.to_datetime(numeric, unit="D", origin="1899-12-30", errors="coerce")
    return converted


# ── Stock actif ────────────────────────────────────────────────────────────

def _kpis_stock(df: pd.DataFrame) -> dict:
    df = df.copy()
    n_total = len(df)
    if n_total == 0:
        return {"total": 0}

    # Dates
    df["_ouv"] = pd.to_datetime(df.get("Date d'émission"), errors="coerce")
    df["_clo"] = pd.to_datetime(df.get("Date de clôture"), errors="coerce")

    clos_mask = df["_clo"].notna()
    n_clos    = int(clos_mask.sum())
    n_ouverts = n_total - n_clos

    # Durée réelle en jours (pour tickets clôturés)
    df["_duree_j"] = (df["_clo"] - df["_ouv"]).dt.total_seconds() / 86400
    df.loc[df["_duree_j"] < 0, "_duree_j"] = None

    resolved = df.loc[clos_mask, "_duree_j"].dropna()

    duree_stats: dict[str, Any] = {}
    if len(resolved) > 0:
        duree_stats = {
            "moyenne_jours": round(float(resolved.mean()), 1),
            "mediane_jours": round(float(resolved.median()), 1),
            "p90_jours":     round(float(np.percentile(resolved, 90)), 1),
            "min_jours":     round(float(resolved.min()), 1),
            "max_jours":     round(float(resolved.max()), 1),
            "n_mesures":     int(len(resolved)),
        }

    # Priorité
    df["_prio"] = pd.to_numeric(df.get("Priorité", pd.Series(dtype=object)), errors="coerce")

    duree_par_prio: list[dict] = []
    for prio in sorted(df["_prio"].dropna().unique()):
        mask = (df["_prio"] == prio) & clos_mask & df["_duree_j"].notna()
        sub = df.loc[mask, "_duree_j"]
        if len(sub) == 0:
            continue
        sla_j = SLA_JOURS.get(int(prio), None)
        duree_par_prio.append({
            "priorite":     int(prio),
            "count":        int(len(sub)),
            "moyenne_jours": round(float(sub.mean()), 1),
            "mediane_jours": round(float(sub.median()), 1),
            "sla_jours":    sla_j,
            "pct_dans_sla": _safe_pct(int((sub <= sla_j).sum()), len(sub)) if sla_j else None,
        })

    # Retard SLA
    retard_col = df.get("Temps de retard")
    n_en_retard = 0
    if retard_col is not None:
        bad = {"", "None", "00:00:00", "0", "nan"}
        en_retard = retard_col.notna() & (~retard_col.astype(str).str.strip().isin(bad))
        n_en_retard = int(en_retard.sum())

    # SLA global sur tickets clôturés avec durée connue (vectorisé)
    if len(resolved) > 0 and df["_prio"].notna().any():
        sub_df = df[clos_mask & df["_duree_j"].notna() & df["_prio"].notna()].copy()
        sla_limit = sub_df["_prio"].map(SLA_JOURS)
        valid_sla = sla_limit.notna()
        total_sla = int(valid_sla.sum())
        ok_sla = int((sub_df.loc[valid_sla, "_duree_j"] <= sla_limit[valid_sla]).sum())
        taux_sla = _safe_pct(ok_sla, total_sla) if total_sla else None
    else:
        taux_sla = None

    # Evolution mensuelle
    def _evo(dates: pd.Series) -> list[dict]:
        valid = dates.dropna()
        if valid.empty:
            return []
        grp = valid.dt.to_period("M").value_counts().sort_index()
        return [{"mois": str(k), "count": int(v)} for k, v in grp.items()]

    par_statut = {str(k): int(v) for k, v in
                  df.get("Statut du ticket", pd.Series()).fillna("Inconnu").value_counts().items()}

    par_priorite = {}
    for k, v in df["_prio"].fillna(0).astype(int).value_counts().sort_index().items():
        par_priorite[str(k)] = int(v)

    return {
        "total":           n_total,
        "clos":            n_clos,
        "ouverts":         n_ouverts,
        "taux_cloture":    _safe_pct(n_clos, n_total),
        "n_en_retard":     n_en_retard,
        "taux_sla":        taux_sla,
        "par_statut":      par_statut,
        "par_priorite":    par_priorite,
        "par_theme":       _top_n(df.get("Thème", pd.Series())),
        "par_region":      _top_n(df.get("Région", pd.Series())),
        "par_marque":      _top_n(df.get("Marque et offre", pd.Series())),
        "par_origine":     _top_n(df.get("Origine", pd.Series())),
        "par_service":     _top_n(df.get("Service", pd.Series())),
        "par_intervenant": _top_n(df.get("Résolu par (intervenant)", pd.Series())),
        "par_groupe":      _top_n(df.get("Résolu par (groupe)", pd.Series())),
        "duree_resolution":  duree_stats,
        "duree_par_priorite": duree_par_prio,
        "evolution_ouvertures": _evo(df["_ouv"]),
        "evolution_clotures":   _evo(df["_clo"]),
    }


# ── Historique Legacy ──────────────────────────────────────────────────────

def _kpis_legacy(df: pd.DataFrame) -> dict:
    df = df.copy()
    n_total = len(df)
    if n_total == 0:
        return {"total": 0}

    # Durée de traitement (colonne en jours entiers)
    delai_raw = pd.to_numeric(df.get("Délais de traitement", pd.Series(dtype=object)), errors="coerce")
    delais = delai_raw[delai_raw > 0].dropna()

    duree_stats: dict[str, Any] = {}
    if len(delais) > 0:
        duree_stats = {
            "moyenne_jours": round(float(delais.mean()), 1),
            "mediane_jours": round(float(delais.median()), 1),
            "p90_jours":     round(float(np.percentile(delais, 90)), 1),
            "n_mesures":     int(len(delais)),
        }

    df["_ouv"] = _conv_dates(df.get("Date d'émission", pd.Series(dtype=object)))
    df["_clo"] = _conv_dates(df.get("Date clôture",    pd.Series(dtype=object)))

    def _evo(dates: pd.Series) -> list[dict]:
        valid = dates.dropna()
        if valid.empty:
            return []
        grp = valid.dt.to_period("M").value_counts().sort_index()
        return [{"mois": str(k), "count": int(v)} for k, v in grp.items()][-24:]

    prio = pd.to_numeric(df.get("Priorité", pd.Series()), errors="coerce")
    par_priorite = {
        str(int(k)): int(v)
        for k, v in prio.dropna().value_counts().sort_index().items()
    }

    return {
        "total":              n_total,
        "par_statut":         {str(k): int(v) for k, v in
                               df.get("Statut du ticket", pd.Series()).fillna("Inconnu")
                               .value_counts().items()},
        "par_priorite":       par_priorite,
        "par_theme":          _top_n(df.get("Thématique fonctionnelle", pd.Series())),
        "par_sujet":          _top_n(df.get("Sujet", pd.Series())),
        "duree_resolution":   duree_stats,
        "evolution_ouvertures": _evo(df["_ouv"]),
        "evolution_clotures":   _evo(df["_clo"]),
    }


# ── Entrée publique ────────────────────────────────────────────────────────

def compute_kpis(sheets: dict[str, pd.DataFrame]) -> dict:
    result: dict[str, Any] = {}
    if MAIN_SHEET   in sheets:
        result["stock"]  = _kpis_stock(sheets[MAIN_SHEET])
    if LEGACY_SHEET in sheets:
        result["legacy"] = _kpis_legacy(sheets[LEGACY_SHEET])
    return result


# ── Prompt IA ──────────────────────────────────────────────────────────────

def build_ai_prompt(kpis: dict, filename: str = "") -> str:
    lines: list[str] = []
    a = lines.append

    a("# Analyse du support informatique MGEN — Données anonymisées RGPD")
    a("")
    a("## Contexte")
    a(
        "Tu es un expert en analyse de la performance des équipes de support IT. "
        "Tu vas analyser les indicateurs clés du support MGEN extraits d'un fichier "
        "de suivi des tickets. Toutes les données ont été anonymisées (noms, emails, "
        "identifiants) conformément au RGPD. Aucune donnée personnelle n'est présente."
    )
    a("")

    stock = kpis.get("stock", {})
    if stock:
        a("## 1. Stock de tickets actifs")
        a("")
        a(f"- **Total tickets** : {stock.get('total', 'N/A')}")
        a(f"- **Clôturés** : {stock.get('clos', 0)} ({stock.get('taux_cloture', 0)} %)")
        a(f"- **Encore ouverts** : {stock.get('ouverts', 0)}")
        a(f"- **En retard SLA** : {stock.get('n_en_retard', 0)}")
        if stock.get("taux_sla") is not None:
            a(f"- **Taux de respect SLA** : {stock['taux_sla']} %")
        a("")

        if stock.get("par_statut"):
            a("### Répartition par statut")
            for statut, cnt in stock["par_statut"].items():
                a(f"- {statut} : {cnt}")
            a("")

        if stock.get("par_priorite"):
            a("### Répartition par priorité (1 = critique → 5 = faible)")
            for p, cnt in sorted(stock["par_priorite"].items()):
                a(f"- Priorité {p} : {cnt} tickets")
            a("")

        d = stock.get("duree_resolution", {})
        if d:
            a("### Temps de résolution (tickets clôturés)")
            a(f"- Moyenne : **{d.get('moyenne_jours', '?')} jours**")
            a(f"- Médiane : {d.get('mediane_jours', '?')} jours")
            a(f"- 90e percentile : {d.get('p90_jours', '?')} jours")
            a(f"- Max observé : {d.get('max_jours', '?')} jours")
            a(f"- Basé sur {d.get('n_mesures', 0)} tickets avec date de clôture")
            a("")

        if stock.get("duree_par_priorite"):
            a("### Délais de résolution par priorité")
            for item in stock["duree_par_priorite"]:
                sla_info = ""
                if item.get("sla_jours"):
                    sla_info = f" | SLA cible : {item['sla_jours']}j"
                    if item.get("pct_dans_sla") is not None:
                        sla_info += f" | Dans SLA : {item['pct_dans_sla']} %"
                a(f"- P{item['priorite']} : moy {item['moyenne_jours']}j, "
                  f"med {item['mediane_jours']}j ({item['count']} tickets){sla_info}")
            a("")

        if stock.get("par_theme"):
            a("### Top thèmes fonctionnels")
            for item in stock["par_theme"][:12]:
                a(f"- {item['label']} : {item['count']}")
            a("")

        if stock.get("par_region"):
            a("### Répartition géographique (top régions)")
            for item in stock["par_region"][:8]:
                a(f"- {item['label']} : {item['count']}")
            a("")

        if stock.get("par_marque"):
            a("### Répartition par marque / offre")
            for item in stock["par_marque"][:6]:
                a(f"- {item['label']} : {item['count']}")
            a("")

        if stock.get("par_intervenant"):
            a("### Charge par intervenant (noms anonymisés)")
            for item in stock["par_intervenant"][:8]:
                a(f"- {item['label']} : {item['count']} tickets")
            a("")

        if stock.get("evolution_ouvertures"):
            a("### Évolution mensuelle des ouvertures")
            for item in stock["evolution_ouvertures"]:
                a(f"- {item['mois']} : {item['count']}")
            a("")

    legacy = kpis.get("legacy", {})
    if legacy:
        a("## 2. Historique des tickets clôturés (Legacy)")
        a("")
        a(f"- **Total tickets historiques** : {legacy.get('total', 'N/A')}")
        dl = legacy.get("duree_resolution", {})
        if dl:
            a(f"- Temps moyen de traitement : **{dl.get('moyenne_jours', '?')} jours**")
            a(f"- Médiane : {dl.get('mediane_jours', '?')} jours")
            a(f"- 90e percentile : {dl.get('p90_jours', '?')} jours")
        a("")
        if legacy.get("par_theme"):
            a("### Top thèmes (historique)")
            for item in legacy["par_theme"][:10]:
                a(f"- {item['label']} : {item['count']}")
            a("")
        if legacy.get("evolution_ouvertures"):
            a("### Évolution mensuelle des ouvertures (24 derniers mois)")
            for item in legacy["evolution_ouvertures"]:
                a(f"- {item['mois']} : {item['count']}")
            a("")

    a("---")
    a("")
    a("## Questions d'analyse")
    a("")
    a("Sur la base **exclusive** des données ci-dessus, réponds en français de façon "
      "structurée et chiffrée aux questions suivantes :")
    a("")
    a("1. **Santé globale** : Quel est l'état de santé général du support ? "
      "Identifie les principaux signaux d'alerte.")
    a("2. **Performance SLA** : Le respect des délais est-il satisfaisant par priorité ? "
      "Quels niveaux de priorité posent problème ?")
    a("3. **Points chauds thématiques** : Quels domaines concentrent le plus de tickets ? "
      "Y a-t-il une surreprésentation anormale ?")
    a("4. **Répartition de la charge** : La charge est-elle équilibrée entre intervenants "
      "et entre régions ? Y a-t-il des déséquilibres ?")
    a("5. **Tendances** : Le volume évolue-t-il à la hausse ou à la baisse ? "
      "Y a-t-il des pics saisonniers ?")
    a("6. **Comparaison stock vs historique** : La performance actuelle est-elle meilleure "
      "ou moins bonne que l'historique Legacy ?")
    a("7. **Recommandations** : Propose 3 à 5 actions prioritaires concrètes et mesurables "
      "pour améliorer la qualité et la rapidité du support.")

    return "\n".join(lines)
