"""Registre central des modules de la plateforme.

Ajouter un module = ajouter une entrée `ModuleSpec` dans la liste `MODULES`.
Aucune modification de `app.py` n'est nécessaire : la factory parcourt cette
liste, importe chaque blueprint et l'enregistre. Un module `enabled=False`
reste déclaré mais n'est ni enregistré ni affiché dans la sidebar.

Chaque module est un Flask Blueprint autonome qui porte ses propres routes,
templates et logique métier. L'ordre de la liste détermine l'ordre d'affichage
dans la navigation latérale.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModuleSpec:
    key:         str   # identifiant court, unique (sert d'ancre)
    code:        str   # code affiché dans la sidebar (ex. "00")
    label:       str   # libellé lisible
    icon:        str   # emoji / pictogramme sidebar
    import_path: str   # chemin d'import du module exposant `bp`
    blueprint:   str   # nom de l'attribut Blueprint dans le module
    enabled:     bool = True
    default:     bool = False  # module servi sur la route racine "/"


# Source de vérité unique pour la navigation et l'enregistrement des blueprints.
MODULES: list[ModuleSpec] = [
    ModuleSpec(
        key="dashboard", code="00", label="Dashboard", icon="📊",
        import_path="modules.dashboard.routes", blueprint="bp",
        enabled=True, default=True,
    ),
    ModuleSpec(
        key="pipeline", code="01", label="Pipeline ETL", icon="⚙️",
        import_path="modules.pipeline.routes", blueprint="bp",
        enabled=True,
    ),
    ModuleSpec(
        key="sql_runner", code="02", label="Requêtes SQL", icon="🗄️",
        import_path="modules.sql_runner.routes", blueprint="bp",
        enabled=True,
    ),
    ModuleSpec(
        key="reports", code="03", label="Rapports CODIR", icon="📄",
        import_path="modules.reports.routes", blueprint="bp",
        enabled=True,
    ),
    ModuleSpec(
        key="scheduler", code="04", label="Planification", icon="⏰",
        import_path="modules.scheduler.routes", blueprint="bp",
        enabled=True,
    ),
    ModuleSpec(
        key="quality", code="05", label="Qualité données", icon="🔎",
        import_path="modules.quality.routes", blueprint="bp",
        enabled=True,
    ),
    ModuleSpec(
        key="admin", code="06", label="Administration", icon="🛠️",
        import_path="modules.admin.routes", blueprint="bp",
        enabled=True,
    ),
    ModuleSpec(
        key="ticket_sam", code="07", label="Analyse Tickets S@M", icon="🎫",
        import_path="modules.ticket_sam.routes", blueprint="bp",
        enabled=True,
    ),
]


def active_modules() -> list[ModuleSpec]:
    """Retourne les modules activés, dans l'ordre de déclaration."""
    return [m for m in MODULES if m.enabled]


def default_module() -> ModuleSpec | None:
    """Retourne le module servi sur la route racine, ou le premier activé."""
    for m in active_modules():
        if m.default:
            return m
    actives = active_modules()
    return actives[0] if actives else None
