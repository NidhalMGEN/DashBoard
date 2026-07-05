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
    status:      StepStatus = field(default=StepStatus.PENDING, init=False)


class PipelineRunner:
    # Séquence identique à ETL_vf.bat (référence production) :
    # TCD(05) → IEHE(01) → [PAUSE RELIQUAT] → Detail(03) → Retry(06) → GED(07) → KPI(02) → BDD(04)
    STEPS = [
        Step("tcd",    "TCD Accolade",   "05_generation_tcd.py",             conditional=True),
        Step("iehe",   "IEHE + SQL",      "01_generation_donnees.py"),
        Step("detail", "Fichiers détail", "03_generation_fichiers_detail.py"),
        Step("retry",  "Retry IEHE KO",   "06_iehe_retry.py"),
        Step("ged",    "Contrôle GED",    "07_controle_tp_ged.py",            conditional=True),
        Step("ged_retry", "Retry TP GED KO", "08_tp_ged_retry.py"),
        Step("kpi",    "Calcul KPI",      "02_calcul_kpi.py"),
        Step("bdd",    "Chargement BDD",  "04_chargement_bdd.py"),
    ]

    # Triggers conditionnels — globs exacts repris de ETL_vf.bat
    TRIGGER_GLOBS = {
        "tcd": ["*_Accolade - KPI*.xlsx", "*Accolade*KPI*.xlsx"],
        "ged": ["*_TP_GED.csv", "*TP_GED*.csv"],
    }

    # Prompt input() du script 01 (ligne 365) → vraie pause IHM, on attend l'utilisateur.
    # Marqueur discriminant placé en FIN de prompt (les deux prompts partagent le
    # préfixe « Appuyez sur Entrée », on ne déclenche que sur la partie distinctive).
    PAUSE_PROMPT_MARKER = "CM et CK"

    # Prompts terminaux « Appuyez sur Entrée pour quitter » (lignes 460/467/475/488
    # du script 01) → on répond automatiquement \n pour ne pas bloquer le subprocess.
    AUTOANSWER_MARKER = "pour quitter"

    # Métadonnées exposées à l'IHM (sélecteur de scripts du Module 01).
    # Durées estimées indicatives (secondes) + dépendances inter-étapes.
    # Source de vérité unique consommée via GET /pipeline/api/scripts.
    STEP_META = {
        "tcd":    {"duration_est": 60,  "deps": [],                 "desc": "Génération TCD Accolade (conditionnel)"},
        "iehe":   {"duration_est": 180, "deps": [],                 "desc": "Génération données + requêtes SQL IEHE"},
        "detail": {"duration_est": 120, "deps": ["iehe"],           "desc": "Fichiers détail par segment"},
        "retry":  {"duration_est": 90,  "deps": ["iehe"],           "desc": "Retry des IEHE en KO"},
        "ged":    {"duration_est": 120, "deps": [],                 "desc": "Contrôle TP / GED (conditionnel)"},
        "ged_retry": {"duration_est": 90, "deps": ["ged"],          "desc": "Retry des TP GED en KO (re-vérif IEHE puis GED)"},
        "kpi":    {"duration_est": 120, "deps": ["iehe", "detail"], "desc": "Calcul des KPI (Modele_clean.json)"},
        "bdd":    {"duration_est": 300, "deps": ["kpi"],            "desc": "Chargement / historisation BDD"},
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
        if step.id in ("iehe", "retry", "ged_retry"):
            env["PG_USER"]     = self.pg_user
            env["PG_PASSWORD"] = self.pg_password
        # Script 04 (bdd) : NE PAS injecter PG_USER/PG_PASSWORD — credentials
        # d'historisation hardcodés dans le script (collision fatale sinon).
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

    # ── Pause CM/CK (déclenchée par le prompt input() du script 01) ───
    def _trigger_pause(self, pause_id: str, proc):
        if self.unattended:
            # Run planifié : on ne bloque pas, on répond directement au input().
            self.emit_log("⏩ Pause CM/CK franchie automatiquement (mode planifié)",
                          level="warn")
            try:
                proc.stdin.write(b"\n")
                proc.stdin.flush()
            except (OSError, ValueError):
                pass
            return
        self._pause_event      = threading.Event()
        self._current_pause_id = pause_id
        msg = "Déposez CM.csv et CK.csv dans Input_Data/ puis cliquez « Continuer »"
        self.emit_pause(pause_id, msg)
        self.emit_log(f"⏸ PAUSE — {msg}", level="pause")
        self._pause_event.wait()
        self._current_pause_id = None
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
        script_path = self.base_dir / "Scripts" / step.script
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
                if self.PAUSE_PROMPT_MARKER in line_buf:
                    if line_buf.strip():
                        self.emit_log(line_buf.strip(), level="pause")
                    line_buf = ""
                    self._trigger_pause("cm_ck", proc)
                elif self.AUTOANSWER_MARKER in line_buf:
                    if line_buf.strip():
                        self.emit_log(line_buf.strip(), level="info")
                    line_buf = ""
                    self._autoanswer(proc)

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
        self.emit_pause("reliquat", msg)
        self.emit_log(f"⏸ PAUSE RELIQUAT — {msg}", level="pause")
        ev.wait()
        self._pause_event      = None
        self._current_pause_id = None

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
