"""Runner du Module 01 — réutilise l'orchestrateur éprouvé `pipeline_runner`.

La logique métier (subprocess, pauses CM/CK et RELIQUAT, interception stdin,
décodage UTF-8 incrémental, étapes conditionnelles) reste dans le module
racine `pipeline_runner.py` pour ne pas dupliquer du code testé en production.
Ce fichier ne fait qu'exposer `PipelineRunner`, `StepStatus` et le catalogue
de scripts sous le namespace du blueprint.

`SCRIPTS` est dérivé du catalogue du runner : source de vérité unique exposée
via `GET /pipeline/api/scripts` et consommée par le JS (aucune duplication
dans les templates).
"""

from __future__ import annotations

import pipeline_runner as _core

PipelineRunner = _core.PipelineRunner
StepStatus = _core.StepStatus


def scripts_catalog() -> list[dict]:
    """Catalogue ordonné des étapes (id, label, durée estimée, deps...)."""
    return PipelineRunner.scripts_catalog()
