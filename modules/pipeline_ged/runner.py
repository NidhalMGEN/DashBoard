"""Runner du module Pipeline GED.

Sous-classe de `pipeline_runner.PipelineRunner` : toute la mécanique
(subprocess, SSE, pause sur prompt input(), décision sur erreur) est héritée.
Seuls changent le dossier des scripts (scriptsNewPipline/), le catalogue
d'étapes et le prompt de pause des scripts 07/08 (dépôt du CSV GED).

Note : les ids d'étapes sont volontairement différents de "ged"/"ged_retry"
(pipeline ETL) pour ne PAS déclencher l'injection des identifiants IEHE dans
`_build_env` — les scripts 07/08 utilisent la BDD de suivi avec leurs propres
identifiants.
"""

from __future__ import annotations

import pipeline_runner as _core

Step = _core.Step
StepStatus = _core.StepStatus


class GedPipelineRunner(_core.PipelineRunner):
    SCRIPTS_DIR_NAME = "scriptsNewPipline"

    STEPS = [
        Step("tp_ged_controle", "Contrôle TP GED", "07_controle_tp_ged.py"),
        Step("tp_ged_retry", "Retry TP GED KO", "08_ged_retry.py"),
    ]

    TRIGGER_GLOBS: dict = {}

    STEP_META = {
        "tp_ged_controle": {
            "duration_est": 120, "deps": [],
            "desc": "Contrôle journalier TP GED : génération des SQL, pause "
                    "dépôt du CSV GED, enregistrement en BDD suivi",
        },
        "tp_ged_retry": {
            "duration_est": 120, "deps": ["tp_ged_controle"],
            "desc": "Relance les KPEP GED non trouvés (BDD suivi) : génération "
                    "des SQL, pause dépôt du CSV résultat, mise à jour BDD",
        },
    }

    # Prompt input() des scripts 07/08 : « Appuyez sur Entrée une fois le
    # fichier est mis le fichier doit s'appelet {PREFIX}_TP_GED[_RETRY].csv ».
    # Message générique : le nom exact du fichier attendu est dans les logs.
    PAUSE_PROMPT_MARKER = "fichier est mis"
    PAUSE_ID = "ged_csv"
    PAUSE_MESSAGE = ("Exécutez les requêtes SQL générées dans Output/ sur la GED, "
                     "déposez le CSV résultat dans Input_Data/ (nom exact affiché "
                     "dans les logs) puis cliquez « Continuer »")
