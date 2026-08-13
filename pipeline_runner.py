import os
import sys
import codecs
import queue
import threading
import subprocess
import datetime
from enum import Enum
from dataclasses import dataclass, field
from pathlib import Path


class StepStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    OK      = "ok"
    SKIP    = "skip"
    ERROR   = "error"


@dataclass
class Step:
    id:          str
    label:       str
    script:      str | None
    conditional: bool = False
    # Dossier du script, relatif à la racine du projet. None = SCRIPTS_DIR_NAME
    # du runner. Permet à une étape d'un pipeline de pointer vers un autre
    # dossier (les étapes TP GED vivent dans scriptsNewPipline/).
    scripts_dir: str | None = None
    status:      StepStatus = field(default=StepStatus.PENDING, init=False)


class PipelineRunner:
    # Séquence identique à ETL_vf.bat (référence production) :
    # TCD(05) → IEHE(01) → [PAUSE RELIQUAT] → Detail(03) → Retry(06) → GED(07) → KPI(02) → BDD(04)
    #
    # Étapes TP GED : elles pointent vers scriptsNewPipline/, la version qui
    # alimente la table de suivi rptpsc.suivi_tp_ged (lue par la courbe
    # « Résorption TP GED » du dashboard). Les anciens Scripts/07 et Scripts/08
    # restent sur disque mais ne sont plus appelés.
    #
    # Leurs ids sont volontairement tp_ged_* et non ged/ged_retry : _build_env
    # injecte les identifiants IEHE par id d'étape, or ces scripts attaquent la
    # base de supervision avec leur propre compte (cf. _build_env).
    STEPS = [
        Step("tcd",    "TCD Accolade",   "05_generation_tcd.py",             conditional=True),
        Step("iehe",   "IEHE + SQL",      "01_generation_donnees.py"),
        Step("detail", "Fichiers détail", "03_generation_fichiers_detail.py"),
        Step("retry",  "Retry IEHE KO",   "06_iehe_retry.py"),
        Step("tp_ged_controle", "Contrôle TP GED",  "07_controle_tp_ged.py",
             scripts_dir="scriptsNewPipline"),
        Step("tp_ged_retry",    "Retry TP GED KO",  "08_ged_retry.py",
             scripts_dir="scriptsNewPipline"),
        Step("kpi",    "Calcul KPI",      "02_calcul_kpi.py"),
        Step("bdd",    "Chargement BDD",  "04_chargement_bdd.py"),
        Step("noemie", "Taux de noémisation", "09_taux_noemisation.py",      conditional=True),
    ]

    # Triggers conditionnels — globs exacts repris de ETL_vf.bat
    #
    # Pas de trigger pour tp_ged_controle : contrairement à l'ancien Scripts/07,
    # le script génère d'abord les requêtes SQL puis se met EN PAUSE le temps
    # que l'on dépose le CSV GED. Exiger le CSV en amont ferait sauter l'étape
    # à presque tous les runs.
    TRIGGER_GLOBS = {
        "tcd": ["*_Accolade - KPI*.xlsx", "*Accolade*KPI*.xlsx"],
        # Un seul glob couvre Noemie/Noémie et le séparateur espace tolérés par
        # NOEMIE_FILENAME_RE (Scripts/09_taux_noemisation.py).
        "noemie": ["*Taux*No*mie*.xlsx"],
    }

    # Dossier des scripts exécutés par les étapes (surchargeable par sous-classe).
    SCRIPTS_DIR_NAME = "Scripts"

    # Étapes exclues des runs planifiés (unattended). Les scripts TP GED
    # s'arrêtent pour que l'on dépose un CSV ; sans opérateur, _trigger_pause
    # répond \n automatiquement et le script poursuivrait SANS le fichier,
    # écrivant des lignes fausses dans rptpsc.suivi_tp_ged. Mieux vaut ne pas
    # les lancer du tout et les jouer à la main.
    UNATTENDED_SKIP = {"tp_ged_controle", "tp_ged_retry"}

    # Prompt input() du script 01 (ligne 365) → vraie pause IHM, on attend l'utilisateur.
    # Marqueur discriminant placé en FIN de prompt (les deux prompts partagent le
    # préfixe « Appuyez sur Entrée », on ne déclenche que sur la partie distinctive).
    PAUSE_PROMPT_MARKER = "CM et CK"
    PAUSE_ID = "cm_ck"
    PAUSE_MESSAGE = "Déposez CM.csv et CK.csv dans Input_Data/ puis cliquez « Continuer »"

    # Pauses SUPPLÉMENTAIRES — (marqueur, pause_id, message).
    # Un pipeline qui enchaîne des scripts d'origines différentes rencontre
    # plusieurs prompts input() distincts : un marqueur unique ne suffit plus.
    # Le prompt non reconnu ne serait jamais détecté et le subprocess resterait
    # bloqué indéfiniment sur son input() (gel de l'étape).
    # Les sous-classes qui ne pilotent qu'un seul script gardent la voie simple
    # (surcharge de PAUSE_PROMPT_MARKER) — cf. modules/pipeline_ged/runner.py.
    EXTRA_PAUSES: list[tuple[str, str, str]] = [
        # Prompt input() des scripts scriptsNewPipline/07 et 08 : « Appuyez sur
        # Entrée une fois le fichier ... {PREFIX}_TP_GED[_RETRY].csv ».
        # Le marqueur s'arrête à la partie STABLE des deux prompts, dont les
        # formulations divergent (07 : « ... déposé — il doit s'appeler »,
        # 08 : « ... est mis le fichier doit s'appeler »). Il ne collisionne pas
        # avec le prompt du script 01, qui dit « une fois LES FICHIERS CM et CK ».
        # Message générique : le nom exact du fichier attendu figure dans les logs.
        ("Appuyez sur Entrée une fois le fichier", "ged_csv",
         "Exécutez les requêtes SQL générées dans Output/ sur la GED, déposez le "
         "CSV résultat dans Input_Data/ (nom exact affiché dans les logs) puis "
         "cliquez « Continuer »"),
    ]

    @classmethod
    def _pause_rules(cls) -> list[tuple[str, str, str]]:
        """Règles de pause, marqueur par défaut en tête."""
        return [(cls.PAUSE_PROMPT_MARKER, cls.PAUSE_ID, cls.PAUSE_MESSAGE),
                *cls.EXTRA_PAUSES]

    # Prompts terminaux « Appuyez sur Entrée pour quitter » (lignes 460/467/475/488
    # du script 01) → on répond automatiquement \n pour ne pas bloquer le subprocess.
    AUTOANSWER_MARKER = "pour quitter"

    # Filet de sécurité. Un prompt input() reformulé dans un script cesse d'être
    # reconnu par les marqueurs ci-dessus : l'étape gèle alors sans aucun
    # message dans l'IHM (le subprocess attend sur stdin, le runner attend sur
    # stdout). Plutôt que ce blocage muet, tout prompt contenant encore
    # « Appuyez sur Entrée » déclenche une pause générique : l'opérateur voit le
    # texte réel du prompt dans les logs et débloque avec « Continuer ».
    # Testé APRÈS AUTOANSWER_MARKER, sinon il capturerait les « pour quitter ».
    PAUSE_FALLBACK_MARKER = "Appuyez sur Entrée"
    PAUSE_FALLBACK_ID = "prompt_script"
    PAUSE_FALLBACK_MESSAGE = ("Le script attend une action de votre part "
                              "(voir le prompt ci-dessus dans les logs) puis "
                              "cliquez « Continuer »")

    # Métadonnées exposées à l'IHM (sélecteur de scripts du Module 01).
    # Durées estimées indicatives (secondes) + dépendances inter-étapes.
    # Source de vérité unique consommée via GET /pipeline/api/scripts.
    STEP_META = {
        "tcd":    {"duration_est": 60,  "deps": [],                 "desc": "Génération TCD Accolade (conditionnel)"},
        "iehe":   {"duration_est": 180, "deps": [],                 "desc": "Génération données + requêtes SQL IEHE"},
        "detail": {"duration_est": 120, "deps": ["iehe"],           "desc": "Fichiers détail par segment"},
        "retry":  {"duration_est": 90,  "deps": ["iehe"],           "desc": "Retry des IEHE en KO"},
        "tp_ged_controle": {"duration_est": 120, "deps": [],
                            "desc": "Contrôle journalier TP GED : génération des SQL, "
                                    "pause dépôt du CSV GED, enregistrement en BDD suivi"},
        "tp_ged_retry":    {"duration_est": 120, "deps": ["tp_ged_controle"],
                            "desc": "Relance les KPEP GED non trouvés (BDD suivi) : génération "
                                    "des SQL, pause dépôt du CSV résultat, mise à jour BDD"},
        "kpi":    {"duration_est": 120, "deps": ["iehe", "detail"], "desc": "Calcul des KPI (Modele_clean.json)"},
        "bdd":    {"duration_est": 300, "deps": ["kpi"],            "desc": "Chargement / historisation BDD"},
        "noemie": {"duration_est": 30,  "deps": [],
                   "desc": "Taux de noémisation par client/offre (fichier mensuel, conditionnel)"},
    }

    @classmethod
    def scripts_catalog(cls) -> list[dict]:
        """Catalogue des étapes pour l'IHM (id, label, durée, deps, conditionnel)."""
        out = []
        for s in cls.STEPS:
            meta = cls.STEP_META.get(s.id, {})
            out.append({
                "id": s.id, "label": s.label, "script": s.script,
                "conditional": s.conditional,
                "duration_est": meta.get("duration_est", 60),
                "deps": meta.get("deps", []),
                "desc": meta.get("desc", ""),
            })
        return out

    def __init__(self, pg_user: str, pg_password: str, log_queue: queue.Queue,
                 selected_ids: set[str] | None = None, unattended: bool = False):
        self.pg_user     = pg_user
        self.pg_password = pg_password
        self.log_queue   = log_queue
        self.base_dir    = Path(__file__).parent
        # selected_ids = None  -> pipeline complet (toutes les étapes)
        # selected_ids = {...}  -> exécute uniquement les étapes cochées
        self.selected_ids = selected_ids
        # unattended = True (runs planifiés) : aucune interaction possible.
        # Les pauses (CM/CK, RELIQUAT) sont franchies automatiquement et une
        # étape en échec abandonne le pipeline (pas d'attente de décision IHM).
        self.unattended = unattended
        self._pause_event: threading.Event | None = None
        self._current_pause_id: str | None = None
        self._pause_message: str | None = None
        self._error_event: threading.Event | None = None
        self._error_decision: str = "abort"
        self.log_lines: list[str] = []  # journal complet du run (historique)
        self.progress    = 0
        self.running     = False
        self.success     = False
        for s in self.STEPS:
            s.status = StepStatus.PENDING

    # ── Chemin Python portable ────────────────────────────────────────
    @property
    def python_exe(self) -> str:
        if getattr(sys, "frozen", False):
            return str(Path(sys.executable).parent / "python" / "python.exe")
        # Python embarqué livré à la racine du projet (dossier python/)
        embedded = self.base_dir / "python" / "python.exe"
        if embedded.exists():
            return str(embedded)
        candidate = self.base_dir / "WinPython" / "WPy64-313130" / "python" / "python.exe"
        return str(candidate) if candidate.exists() else sys.executable

    # ── Environnement par script ──────────────────────────────────────
    def _build_env(self, step: Step) -> dict:
        env = os.environ.copy()
        # Force UTF-8 côté enfant : les scripts impriment des emojis (✅ ⏸️ 🔹),
        # sinon UnicodeEncodeError sous Windows (stdout cp1252).
        env["PYTHONUTF8"]       = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        # Les scripts importent des modules locaux (iehe_ko_lib, etc.) depuis
        # le dossier Scripts/. L'embeddable Python ecrase sys.path via ._pth
        # donc Scripts/ n'est jamais ajoute automatiquement.
        scripts_dir = str(self.base_dir / "Scripts")
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = scripts_dir + os.pathsep + existing if existing else scripts_dir
        if step.id in ("iehe", "retry"):
            env["PG_USER"]     = self.pg_user
            env["PG_PASSWORD"] = self.pg_password
        # Script 04 (bdd) : NE PAS injecter PG_USER/PG_PASSWORD — credentials
        # d'historisation hardcodés dans le script (collision fatale sinon).
        #
        # Étapes tp_ged_* : même règle. Les scripts de scriptsNewPipline/ lisent
        # PG_USER/PG_PASSWORD mais visent la base de supervision et retombent sur
        # leur propre compte quand ces variables sont absentes. Leur injecter le
        # login IEHE les ferait échouer à l'authentification.
        return env

    # ── Émission SSE ──────────────────────────────────────────────────
    def emit_log(self, text: str, level: str = "info"):
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        # Conserve les lignes pour l'historique (consultation des logs d'un run
        # passé) en plus de la diffusion SSE. Borné pour ne pas gonfler.
        if len(self.log_lines) < 2000:
            self.log_lines.append(f"[{ts}] {text}")
        self.log_queue.put({"type": "log", "level": level, "text": text, "ts": ts})

    def emit_step(self, step_id: str, status: str):
        self.log_queue.put({"type": "step", "id": step_id, "status": status})

    def emit_pause(self, pause_id: str, message: str):
        self.log_queue.put({"type": "pause", "id": pause_id, "message": message})

    def emit_progress(self, pct: int):
        self.progress = pct
        self.log_queue.put({"type": "progress", "pct": pct})

    @staticmethod
    def _classify(line: str) -> str:
        low = line.lower()
        if any(w in low for w in ("erreur", "error", "exception", "traceback", "❌", "✗")):
            return "error"
        if any(w in low for w in ("warning", "warn", "attention", "⚠")):
            return "warn"
        if "✅" in line or "✓" in line or "succès" in low or "termine" in low:
            return "ok"
        return "info"

    # ── Pause IHM (déclenchée par un prompt input() d'un script) ──────
    def _trigger_pause(self, pause_id: str, message: str, proc):
        if self.unattended:
            # Run planifié : on ne bloque pas, on répond directement au input().
            self.emit_log(f"⏩ Pause « {pause_id} » franchie automatiquement "
                          "(mode planifié)", level="warn")
            try:
                proc.stdin.write(b"\n")
                proc.stdin.flush()
            except (OSError, ValueError):
                pass
            return
        self._pause_event      = threading.Event()
        self._current_pause_id = pause_id
        msg = message
        self._pause_message    = msg
        self.emit_pause(pause_id, msg)
        self.emit_log(f"⏸ PAUSE — {msg}", level="pause")
        self._pause_event.wait()
        self._current_pause_id = None
        self._pause_message    = None
        self._pause_event = None
        # Débloque le input() du script Python
        try:
            proc.stdin.write(b"\n")
            proc.stdin.flush()
        except (OSError, ValueError):
            pass

    def _autoanswer(self, proc):
        """Répond \\n à un prompt terminal (« Appuyez sur Entrée pour quitter »)."""
        try:
            proc.stdin.write(b"\n")
            proc.stdin.flush()
        except (OSError, ValueError):
            pass

    def resume(self, pause_id: str):
        if self._pause_event and self._current_pause_id == pause_id:
            self._pause_event.set()

    @property
    def pause_info(self) -> dict | None:
        """État de pause courant pour l'IHM : None si aucune pause active,
        sinon {"id": ..., "message": ...}. Permet de restaurer la bannière
        de pause après un rechargement de page (cf. /pipeline/status)."""
        if self._current_pause_id is None:
            return None
        return {"id": self._current_pause_id, "message": self._pause_message}

    # ── Décision sur erreur (Ignorer et continuer / Abandonner) ────────
    def _ask_error_decision(self, step: Step) -> str:
        """Bloque le run sur une étape en échec et attend la décision IHM.
        Retourne 'continue' (ignorer l'erreur et poursuivre) ou 'abort'."""
        if self.unattended:
            # Run planifié : pas d'interaction -> abandon sûr.
            return "abort"
        self._error_event = threading.Event()
        self._error_decision = "abort"  # défaut sûr si la file se ferme
        self.log_queue.put({
            "type": "error_decision", "id": step.id, "label": step.label,
            "message": f"L'étape « {step.label} » a échoué. "
                       "Ignorer et continuer, ou abandonner le pipeline ?",
        })
        self._error_event.wait()
        decision = self._error_decision
        self._error_event = None
        return decision

    def error_decision(self, decision: str):
        """Appelée par la route /pipeline/error-decision (continue|abort)."""
        if self._error_event is not None:
            self._error_decision = "continue" if decision == "continue" else "abort"
            self._error_event.set()

    # ── Exécution d'une étape ─────────────────────────────────────────
    def _run_step(self, step: Step) -> bool:
        script_dir  = step.scripts_dir or self.SCRIPTS_DIR_NAME
        script_path = self.base_dir / script_dir / step.script
        if not script_path.exists():
            self.emit_log(f"❌ Script introuvable : {script_path}", level="error")
            return False

        step.status = StepStatus.RUNNING
        self.emit_step(step.id, "running")
        self.emit_log(f"▶ Démarrage : {step.label}", level="info")

        creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

        # Mode BINAIRE + read1() : permet de lire les prompts input() qui n'ont
        # pas de \n final (impossible avec l'itération ligne par ligne). L'enfant
        # écrit sans tampon (PYTHONUNBUFFERED) donc les prompts arrivent aussitôt.
        proc = subprocess.Popen(
            [self.python_exe, str(script_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE,
            env=self._build_env(step),
            cwd=str(self.base_dir),
            creationflags=creationflags,
        )

        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        line_buf = ""

        while True:
            chunk = proc.stdout.read1(4096) if hasattr(proc.stdout, "read1") else proc.stdout.read(1)
            if not chunk:
                if proc.poll() is not None:
                    break
                continue

            line_buf += decoder.decode(chunk)

            # Émet toutes les lignes complètes terminées par \n
            while "\n" in line_buf:
                line, line_buf = line_buf.split("\n", 1)
                line = line.rstrip("\r")
                if line.strip():
                    self.emit_log(line, level=self._classify(line))

            # Détection des prompts input() (texte SANS \n final dans line_buf)
            if line_buf:
                rule = next((r for r in self._pause_rules() if r[0] in line_buf), None)
                if rule is not None:
                    _, pause_id, pause_msg = rule
                    if line_buf.strip():
                        self.emit_log(line_buf.strip(), level="pause")
                    line_buf = ""
                    self._trigger_pause(pause_id, pause_msg, proc)
                elif self.AUTOANSWER_MARKER in line_buf:
                    if line_buf.strip():
                        self.emit_log(line_buf.strip(), level="info")
                    line_buf = ""
                    self._autoanswer(proc)
                elif self.PAUSE_FALLBACK_MARKER and self.PAUSE_FALLBACK_MARKER in line_buf:
                    # Prompt non catalogué : pause générique plutôt que gel muet.
                    if line_buf.strip():
                        self.emit_log(line_buf.strip(), level="pause")
                    line_buf = ""
                    self._trigger_pause(self.PAUSE_FALLBACK_ID,
                                        self.PAUSE_FALLBACK_MESSAGE, proc)

        # Vide le reliquat éventuel du buffer
        tail = (line_buf + decoder.decode(b"", final=True)).strip()
        if tail:
            self.emit_log(tail, level=self._classify(tail))

        proc.wait()
        ok = proc.returncode == 0
        step.status = StepStatus.OK if ok else StepStatus.ERROR
        self.emit_step(step.id, "ok" if ok else "error")
        if ok:
            self.emit_log(f"✓ {step.label} terminé", level="ok")
        else:
            self.emit_log(f"✗ {step.label} échoué (code {proc.returncode})", level="error")
        return ok

    # ── Pause RELIQUAT (gérée par l'orchestrateur, comme dans ETL_vf.bat) ──
    def _pause_reliquat(self):
        if self.unattended:
            self.emit_log("⏩ Pause RELIQUAT franchie automatiquement (mode planifié)",
                          level="warn")
            return
        ev = threading.Event()
        self._pause_event      = ev
        self._current_pause_id = "reliquat"
        msg = ("Récupérez les requêtes SQL dans Output/, exécutez-les sur la BDD CIAM, "
               "déposez les résultats dans Input_Data/ puis cliquez « Continuer »")
        self._pause_message    = msg
        self.emit_pause("reliquat", msg)
        self.emit_log(f"⏸ PAUSE RELIQUAT — {msg}", level="pause")
        ev.wait()
        self._pause_event      = None
        self._current_pause_id = None
        self._pause_message    = None

    # ── Étape conditionnelle : présence du fichier déclencheur ────────
    def _trigger_present(self, step_id: str) -> bool:
        input_dir = self.base_dir / "Input_Data"
        if not input_dir.exists():
            return False
        for pattern in self.TRIGGER_GLOBS.get(step_id, []):
            if list(input_dir.glob(pattern)):
                return True
        return False

    # ── Boucle principale ─────────────────────────────────────────────
    def _is_selected(self, step_id: str) -> bool:
        """True si l'étape doit être exécutée (None = pipeline complet)."""
        return self.selected_ids is None or step_id in self.selected_ids

    def run(self):
        self.running = True
        self.success = False
        # Progression basée sur le nombre d'étapes RÉELLEMENT à exécuter.
        to_run = [s for s in self.STEPS if self._is_selected(s.id)]
        total    = max(len(to_run), 1)
        done     = 0
        ok_count = 0

        try:
            for step in self.STEPS:
                # Étape non cochée : marquée « ignoré » sans exécution.
                if not self._is_selected(step.id):
                    step.status = StepStatus.SKIP
                    self.emit_step(step.id, "skip")
                    self.emit_log(f"— {step.label} non sélectionné", level="info")
                    continue

                self.emit_progress(int(done / total * 100))

                # Étape à pause manuelle en run planifié : on ne la joue pas.
                if self.unattended and step.id in self.UNATTENDED_SKIP:
                    step.status = StepStatus.SKIP
                    self.emit_step(step.id, "skip")
                    self.emit_log(f"— {step.label} ignoré "
                                  "(pause manuelle impossible en mode planifié)",
                                  level="warn")
                    done += 1
                    ok_count += 1
                    continue

                # Pause RELIQUAT juste avant l'étape Détail (après IEHE)
                if step.id == "detail":
                    self._pause_reliquat()

                if step.conditional and not self._trigger_present(step.id):
                    step.status = StepStatus.SKIP
                    self.emit_step(step.id, "skip")
                    self.emit_log(f"— {step.label} ignoré (fichier déclencheur absent)", level="info")
                    done += 1
                    ok_count += 1
                    continue

                if self._run_step(step):
                    done += 1
                    ok_count += 1
                else:
                    # Étape en échec : on laisse l'utilisateur décider
                    # (Ignorer et continuer / Abandonner), comme ETL_vf.
                    decision = self._ask_error_decision(step)
                    if decision == "continue":
                        self.emit_log(f"⏭ {step.label} ignoré — poursuite du pipeline",
                                      level="warn")
                        done += 1  # comptée comme traitée mais pas ok_count
                        continue
                    self.emit_log(f"⛔ Pipeline abandonné sur l'échec de {step.label}",
                                  level="error")
                    break
        except Exception as exc:  # garde-fou : ne jamais laisser le thread mourir en silence
            self.emit_log(f"❌ Erreur orchestrateur : {exc}", level="error")

        all_ok = ok_count == len(to_run)
        self.emit_progress(100 if all_ok else self.progress)
        self.success = all_ok
        self.running = False
        self.log_queue.put({"type": "done", "success": all_ok})
