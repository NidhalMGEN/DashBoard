# Prompt Claude Code — Pipeline CIAM MGEN
## IHM Web Flask + Rapport CODIR automatique avec tendances historiques

---

## Contexte projet

Application desktop Windows **portable sans droits admin** pour le pipeline ETL CIAM MGEN.
Traitement hebdomadaire de données adhérents MGEN → KPI de supervision.

**Contraintes absolues :**
- Zéro installation sur le poste cible (Windows, sans droits admin)
- Livrable = dossier `dist/pipeline_ciam/` autonome avec WinPython embarqué
- Ne jamais modifier les scripts `01` à `07` existants (production)
- IHM = application web locale (Flask) ouverte dans le navigateur par défaut

---

## Architecture cible

```
KPI/
├── WinPython/WPy64-313130/python/python.exe   ← runtime embarqué
├── Scripts/
│   ├── 01_generation_donnees.py
│   ├── 02_calcul_kpi.py
│   ├── 03_generation_fichiers_detail.py
│   ├── 04_chargement_bdd.py
│   ├── 05_generation_tcd.py
│   ├── 06_iehe_retry.py
│   ├── 07_controle_tp_ged.py
│   └── iehe_ko_lib.py
├── assets/
│   └── MGEN-logo.jpg
├── templates/
│   └── index.html                             ← IHM web (à créer)
├── static/
│   ├── style.css                              ← (optionnel, inline préféré)
│   └── app.js                                 ← (optionnel, inline préféré)
├── Input_Data/
├── Output/
├── config/
├── requirements.txt
├── main.py                                    ← serveur Flask (à créer)
├── pipeline_runner.py                         ← orchestrateur (à créer)
├── report_generator.py                        ← générateur rapport (à créer)
└── build.bat                                  ← compilation PyInstaller (à créer)
```

---

## FICHIER 1 : `main.py` — Serveur Flask

### Comportement au démarrage

```python
import webbrowser, threading, time
from flask import Flask, render_template, request, Response, jsonify

app = Flask(__name__)

def open_browser():
    time.sleep(1.2)
    webbrowser.open("http://127.0.0.1:5000")

if __name__ == "__main__":
    threading.Thread(target=open_browser, daemon=True).start()
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
```

### Routes Flask

| Route | Méthode | Description |
|---|---|---|
| `GET /` | GET | Sert `index.html` |
| `POST /start` | POST | Lance le pipeline (body JSON : `{pg_user, pg_password}`) |
| `GET /stream` | GET | SSE — logs temps réel (Content-Type: text/event-stream) |
| `POST /resume` | POST | Débloque une pause (body JSON : `{pause_id}`) |
| `GET /status` | GET | Retourne l'état courant du pipeline (JSON) |
| `GET /report` | GET | Génère le rapport et retourne le chemin HTML |

### SSE (Server-Sent Events)

```python
@app.route("/stream")
def stream():
    def event_generator():
        while True:
            msg = log_queue.get()           # queue thread-safe
            yield f"data: {json.dumps(msg)}\n\n"
            if msg.get("type") == "done":
                break
    return Response(event_generator(),
                    mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})
```

Messages SSE — structure JSON :
```json
{"type": "log",    "level": "ok|warn|error|info|pause", "text": "...", "ts": "HH:MM:SS"}
{"type": "step",   "id": "iehe", "status": "running|ok|skip|error"}
{"type": "pause",  "id": "cm_ck", "message": "Déposez CM.csv et CK.csv dans Input_Data/"}
{"type": "done",   "success": true}
{"type": "progress", "pct": 43}
```

---

## FICHIER 2 : `templates/index.html` — IHM Web

### Charte graphique MGEN

```css
:root {
  --mgen-green:     #6ab023;
  --mgen-green-dk:  #4d8019;
  --mgen-green-lt:  #f0f7e6;
  --bg:             #111118;
  --bg-card:        #1a1a24;
  --bg-sidebar:     #16161f;
  --bg-input:       #2a2a38;
  --border:         #2a2a38;
  --border-active:  #3a3a50;
  --text:           #f0f0f0;
  --text-muted:     #a0a0b0;
  --text-dim:       #666680;
  --success:        #6ab023;
  --warning:        #f59e0b;
  --danger:         #ef4444;
  --info:           #60a5fa;
  --running:        #3b82f6;
  --skip:           #6b7280;
}
```

### Structure HTML — fichier unique auto-suffisant

Le fichier `index.html` contient tout (CSS inline dans `<style>`, JS inline dans `<script>`).
Pas de dépendances externes sauf Chart.js via CDN jsdelivr.

### Vue 1 — Écran de connexion (état initial)

Centré verticalement et horizontalement, fond `--bg`.

```
┌──────────────────────────────────────────────┐
│                                              │
│   [logo MGEN 48px]                           │
│   Supervision PSC                            │
│   Pipeline CIAM — v4.0                       │
│                                              │
│   ┌─────────────────────────────────────┐    │
│   │  Connexion base de données IEHE     │    │
│   │                                     │    │
│   │  Utilisateur  [__________________]  │    │
│   │  Mot de passe [__________________]  │    │
│   │                                     │    │
│   │        [▶  Lancer le pipeline]      │    │
│   └─────────────────────────────────────┘    │
│                                              │
└──────────────────────────────────────────────┘
```

- Carte centrée `max-width: 420px`, fond `--bg-card`, border-radius 12px
- Logo MGEN embarqué en base64 dans le HTML (lire `assets/MGEN-logo.jpg` et encoder)
- Champ password avec toggle visibilité (icône œil)
- Bouton vert `--mgen-green`, pleine largeur
- Touche `Entrée` soumet le formulaire
- Validation inline : bordure rouge si champ vide

### Vue 2 — Pipeline en cours (après soumission)

```
┌──────────────────────────────────────────────────────────────────┐
│ [logo 28px]  Supervision PSC         Flux: 24062026    [Quitter] │
├──────────────────────┬───────────────────────────────────────────┤
│  ÉTAPES              │  LOGS EN TEMPS RÉEL                       │
│  ─────────────────   │  ──────────────────────────────────────   │
│  ① TCD        ✓     │  [12:05:27] ✓ TCD Accolade généré        │
│  ② IEHE+SQL   ⏳    │  [12:05:31] ✓ New_S chargé : 16 461 lg   │
│  ③ Détail     —     │  [12:05:33] ⚠ Module psycopg manquant    │
│  ④ Retry      —     │  [12:05:35] ⏸ ACTION REQUISE             │
│  ④b GED       —     │    Déposez CM.csv et CK.csv               │
│  ⑤ KPI        —     │    dans Input_Data/ puis cliquez          │
│  ⑥ BDD        —     │    "Continuer"                            │
│                      │                                           │
│  ████░░░░  14%       │                                           │
├──────────────────────┴───────────────────────────────────────────┤
│  [📁 Input_Data]  [📁 Output]    [✅ Continuer ▶]               │
└──────────────────────────────────────────────────────────────────┘
```

**Panel gauche — étapes :**
- `width: 220px`, fond `--bg-sidebar`
- Chaque étape = ligne flex : numéro rond + label + badge statut
- Statuts et styles :
  ```css
  .step-pending  { color: var(--text-dim); }
  .step-running  { color: var(--info); animation: pulse 1.2s infinite; }
  .step-ok       { color: var(--success); }
  .step-skip     { color: var(--skip); text-decoration: line-through; }
  .step-error    { color: var(--danger); }
  ```
- Étape active : fond `#1e2030`, border-left 3px `--mgen-green`
- Barre de progression `<progress>` stylée vert MGEN en bas du panel

**Panel droite — logs :**
- Fond `#0e0e16`, police `Consolas 11px`, line-height 1.8
- Scroll automatique vers le bas (JS : `logBox.scrollTop = logBox.scrollHeight`)
- Colorisation par level :
  ```js
  const colors = {
    ok:    "#6ab023",
    warn:  "#f59e0b",
    error: "#ef4444",
    info:  "#a0a0b0",
    pause: "#60a5fa"
  };
  ```
- Timestamp en gris `#444460` préfixé sur chaque ligne

**Banner de pause (visible uniquement pendant une pause) :**
```html
<div id="pause-banner" style="display:none">
  <span class="pulse-dot"></span>
  <span id="pause-message">...</span>
</div>
```
- Fond `#1a2040`, border `1px solid #2a3a70`, border-radius 8px
- Point pulsé bleu animé
- Disparaît automatiquement quand `resume()` est appelé

**Toolbar bas :**
- `[📁 Input_Data]` → `fetch('/open-folder?path=Input_Data')` → ouvre l'explorateur Windows
- `[📁 Output]` → idem pour Output
- `[✅ Continuer]` → visible uniquement pendant une pause, fond `--mgen-green`, pulsé
  - Au clic : `confirm("Les fichiers sont bien présents dans Input_Data ?")` → `POST /resume`

**Bouton Quitter :**
- Visible en haut à droite, actif uniquement quand pipeline terminé
- `fetch('/shutdown')` → arrête le serveur Flask proprement

### Vue 3 — Rapport (apparaît après étape 6 OK, en dessous du pipeline)

Le rapport s'affiche **dans la même page**, sous la toolbar, dans une section qui se déploie.
Pas de navigation, pas de rechargement.

```html
<section id="report-section" style="display:none; margin-top: 2rem;">
  <!-- Injecté dynamiquement par JS après génération du rapport -->
</section>
```

Le rapport HTML généré est **injecté dans cette section** via `innerHTML` après
`GET /report` (qui retourne le HTML du rapport en string).

---

## FICHIER 3 : `pipeline_runner.py` — Orchestrateur

```python
from enum import Enum
from dataclasses import dataclass
from typing import Callable
import subprocess, threading, os, sys, queue
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

class PipelineRunner:
    STEPS = [
        Step("tcd",    "TCD Accolade",    "05_generation_tcd.py",             conditional=True),
        Step("iehe",   "IEHE + SQL",       "01_generation_donnees.py"),
        Step("detail", "Fichiers détail",  "03_generation_fichiers_detail.py"),
        Step("retry",  "Retry IEHE KO",    "06_iehe_retry.py"),
        Step("ged",    "Contrôle GED",     "07_controle_tp_ged.py",           conditional=True),
        Step("kpi",    "Calcul KPI",       "02_calcul_kpi.py"),
        Step("bdd",    "Chargement BDD",   "04_chargement_bdd.py"),
    ]

    PAUSE_PATTERNS = {
        "cm_ck":    "Appuyez sur Entrée une fois les fichiers CM et CK déposés",
        "reliquat": "Une fois TOUS les fichiers deposes",
    }
```

**Chemin Python portable :**
```python
@property
def python_exe(self) -> str:
    if getattr(sys, "frozen", False):
        # Mode PyInstaller : WinPython copié à côté du .exe
        return str(Path(sys.executable).parent / "python" / "python.exe")
    # Mode dev : WinPython dans le projet
    candidate = self.base_dir / "WinPython" / "WPy64-313130" / "python" / "python.exe"
    return str(candidate) if candidate.exists() else sys.executable
```

**Environnement par script :**
```python
def _build_env(self, step: Step) -> dict:
    env = os.environ.copy()
    if step.id in ("iehe", "retry"):
        env["PG_USER"]     = self.pg_user
        env["PG_PASSWORD"] = self.pg_password
    # Script 04 : NE PAS injecter PG_USER/PG_PASSWORD
    # (credentials BDD historisation hardcodés dans le script, collision fatale)
    return env
```

**Subprocess avec interception stdin/stdout :**
```python
proc = subprocess.Popen(
    [self.python_exe, str(script_path)],
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    stdin=subprocess.PIPE,
    text=True,
    encoding="utf-8",
    errors="replace",
    env=self._build_env(step),
    cwd=str(self.base_dir),
    creationflags=subprocess.CREATE_NO_WINDOW,
)

for line in proc.stdout:
    line = line.rstrip()
    self.emit_log(line)
    for pause_id, pattern in self.PAUSE_PATTERNS.items():
        if pattern in line:
            self._trigger_pause(pause_id, proc)
            break
```

**Pause bloquante :**
```python
def _trigger_pause(self, pause_id: str, proc):
    self._pause_event = threading.Event()
    self.emit_pause(pause_id)       # → SSE type:pause → IHM affiche le bouton
    self._pause_event.wait()        # bloque le thread runner
    proc.stdin.write("\n")          # débloque input() dans le script Python
    proc.stdin.flush()

def resume(self, pause_id: str):
    if self._pause_event:
        self._pause_event.set()
        self._pause_event = None
```

**Pause RELIQUAT (entre étape 2 et étape 3, gérée par l'orchestrateur) :**
Après que l'étape IEHE+SQL est terminée (script 01 exited), avant de lancer le script 03,
l'orchestrateur émet lui-même une pause SSE et attend `resume("reliquat")`.

---

## FICHIER 4 : `report_generator.py` — Rapport CODIR

### Source de données

Le rapport combine :
1. **JSON courant** : `Output/{PREFIX}_Modele_clean.json` (flux venant d'être chargé)
2. **Historique BDD** : `SELECT flux_id, date_import, payload FROM rptpsc.output_kpi_json ORDER BY date_import ASC`

Connexion historique :
```python
# Credentials BDD historisation (identiques à 04_chargement_bdd.py)
PG_HOST = "bdd-T0XX0052.alias"
PG_PORT = "5577"
PG_DB   = "supervisionpsc_db"
PG_USER = "rptpsc"
PG_PASSWORD = "rptpsc_xx"

def fetch_history() -> list[dict]:
    """Récupère tous les flux passés depuis rptpsc.output_kpi_json."""
    engine = create_engine(
        f"postgresql+psycopg://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/{PG_DB}"
    )
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT flux_id, date_import, payload "
            "FROM rptpsc.output_kpi_json "
            "ORDER BY date_import ASC"
        )).fetchall()
    return [{"flux_id": r[0], "date_import": r[1], "payload": r[2]} for r in rows]
```

### KPI extraits pour les tendances

Extraire ces valeurs de chaque `payload` JSONB pour construire les séries temporelles :

```python
KPI_TREND_EXTRACTORS = {
    # Clé tendance : (chemin dans le JSON, label affichage)
    "taux_matching_ciam":   (["5_CIAM", "Matching_Global", "Global", "Taux_Couverture"], "Taux matching CIAM (%)"),
    "non_rapproches":       (["5_CIAM", "Matching_Global", "Global", "Non_Rapproches"], "Non-rapprochés CIAM"),
    "taux_presence_iehe":   (["6_IEHE", "Presence_Globale", "Presents_IEHE", "Taux"], "Taux présence IEHE (%)"),
    "manquants_iehe":       (["6_IEHE", "Presence_Globale", "Manquants_IEHE", "Nombre"], "Manquants IEHE"),
    "taux_eligibilite_tp":  (["7_Carte_TP", "Eligibilite_Globale", "Population_Eligible", "Taux"], "Éligibilité TP (%)"),
    "future_tp":            (["7_Carte_TP", "Eligibilite_Globale", "Population_Future", "Nombre"], "Future TP"),
    "score_qualite":        (["5_CIAM", "Score_Qualite_Donnees", "DATA_QUALITY_OK", "Pct"], "Score qualité données (%)"),
    "taux_resolution_ko":   (["6_IEHE", "Annexe", "Retry_IEHE_KO", "Totaux", "Taux_Resolution"], "Résolution IEHE KO (%)"),
    "volume_flux":          (["2_Volumetrie_Brute", "Resultats", "Total_Lignes"], "Volume flux (lignes)"),
    "coherence_kpep":       (["5_CIAM", "Annexe", "Coherence_KPEP_3_Sources", "E2_Coherence_3_Sources_Stricte", "Pct"], "Cohérence KPEP 3 sources (%)"),
    "emails_risque_ciam":   (["5_CIAM", "Annexe", "Qualite_Emails_Enrichie", "CIAM_Emails_Keycloak", "A_Risque", "Nombre"], "Emails à risque CIAM"),
    "doublons_email_ciam":  (["5_CIAM", "Annexe", "Qualite_Comptes_CIAM", "F1d_Doublons_Email", "Nb_Emails_Dupliques"], "Doublons email CIAM"),
}

def extract_kpi(payload: dict, path: list) -> float | int | None:
    """Navigue dans le JSON imbriqué selon le chemin de clés."""
    cur = payload
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur if isinstance(cur, (int, float)) else None
```

### Structure du rapport HTML

Le rapport est un **fichier HTML unique auto-suffisant** :
- Logo MGEN embarqué en base64
- Chart.js depuis CDN jsdelivr
- Tout le CSS inline

#### Page de garde
```html
<header class="report-header">
  <img src="data:image/jpeg;base64,{LOGO_B64}" height="56" alt="MGEN">
  <div>
    <h1>Supervision PSC — Rapport CODIR</h1>
    <p>Flux du {date_flux} · Généré le {datetime.now().strftime('%d/%m/%Y à %H:%M')}</p>
    <p>{nb_flux_historiques} flux analysés · Données du {date_premier_flux} au {date_dernier_flux}</p>
  </div>
</header>
```

#### Bandeau de synthèse (4 jauges SVG)

4 KPI clés en jauges circulaires SVG côte à côte :

```python
def svg_gauge(pct: float, label: str, sublabel: str = "") -> str:
    r = 38
    circ = 2 * 3.14159 * r
    offset = circ * (1 - pct / 100)
    color = "#6ab023" if pct >= 95 else "#f59e0b" if pct >= 80 else "#ef4444"
    return f"""
    <div style="text-align:center; flex:1; min-width:140px">
      <svg width="96" height="96" viewBox="0 0 96 96">
        <circle cx="48" cy="48" r="{r}" fill="none" stroke="#e5e7eb" stroke-width="7"/>
        <circle cx="48" cy="48" r="{r}" fill="none" stroke="{color}" stroke-width="7"
                stroke-dasharray="{circ:.1f}" stroke-dashoffset="{offset:.1f}"
                transform="rotate(-90 48 48)" stroke-linecap="round"/>
        <text x="48" y="44" text-anchor="middle" font-size="15" font-weight="600" fill="{color}">{pct:.1f}%</text>
        <text x="48" y="60" text-anchor="middle" font-size="9" fill="#888">{sublabel}</text>
      </svg>
      <p style="font-size:12px;color:#555;margin-top:4px">{label}</p>
    </div>"""
```

Jauges affichées :
1. Taux matching CIAM
2. Présence IEHE
3. Éligibilité TP
4. Score qualité données

#### Sections du rapport — ordre et contenu

**Section 1 — Vue d'ensemble du flux**
- Tableau : Total lignes, Assurés bruts, Conjoints, Personnes uniques, Cible CIAM
- Fichiers d'entrée chargés (tableau statut)

**Section 2 — CIAM : Rapprochement**
- Jauge matching global + décomposition par méthode (camembert Chart.js)
- Tableau par société (B2)
- Décomposition non-rapprochés (B3)
- Tendance : graphique linéaire `Taux_Couverture` sur tous les flux historiques

**Section 3 — CIAM : Qualité des données**
- Score DATA_QUALITY_OK/KO avec décomposition des KO (barres horizontales)
- Cohérence KPEP 3 sources
- Incohérences NS↔CIAM (DDN, Nom/Prénom, Email)
- Qualité emails (invalides + à risque) par source
- Tendance : évolution `Score_Qualite` + `emails_risque_ciam`

**Section 4 — CIAM : Qualité des comptes Keycloak**
- F1a Sans email, F1b Sans KPEP, F1c Email other seulement, F1d Doublons email
- Cohérence emails CIAM↔NS (Vrais_Emails_Identiques vs Deux_Emails_Distincts)
- Potentiel enrichissement IEHE→CIAM
- Tendance : évolution `doublons_email_ciam`

**Section 5 — IEHE : Présence et qualité**
- Présence globale (jauge) + détail par type d'assuré (tableau)
- Qualité référentiel D1/D2/D3
- Assurés absents IEHE et éligibles TP
- Tendance : `taux_presence_iehe` + `manquants_iehe` sur historique

**Section 6 — IEHE KO : Suivi des retries**
- KPIs globaux retry : Total KO initial, Résolus, Encore KO, Taux résolution
- Tableau détail par fichier (les 5 derniers flux)
- Répartition par éligibilité TP (barres empilées Chart.js)
- Tendance : `taux_resolution_ko`

**Section 7 — Carte Tiers Payant**
- Éligibilité globale (jauge) + répartition Eligible/Future
- Graphique barres `Eligible_Par_Mois` (Chart.js)
- Détail par société (C3)
- C1 : Éligibles TP non rapprochés CIAM
- C2 : Distribution délai Future TP
- Tendance : `taux_eligibilite_tp` + `future_tp`

**Section 8 — Volume et tendances globales**
- Graphique multi-lignes Chart.js : tous les KPI de tendance sur l'axe temporel
- Tableau récapitulatif de tous les flux historiques (1 ligne = 1 flux, colonnes = KPI clés)
- Variation flux courant vs flux précédent (delta coloré vert/rouge)

#### Graphiques Chart.js — tendances

```javascript
// Exemple graphique tendance multi-KPI
new Chart(ctx, {
    type: 'line',
    data: {
        labels: {dates},           // ["19/06", "20/06", "21/06", ...]
        datasets: [
            {
                label: 'Taux CIAM (%)',
                data: {taux_ciam},
                borderColor: '#6ab023',
                backgroundColor: 'rgba(106,176,35,0.1)',
                tension: 0.3,
                fill: true,
            },
            {
                label: 'Présence IEHE (%)',
                data: {taux_iehe},
                borderColor: '#3b82f6',
                tension: 0.3,
            },
            // ...
        ]
    },
    options: {
        responsive: true,
        plugins: { legend: { position: 'bottom' } },
        scales: {
            y: { min: 90, max: 100, ticks: { callback: v => v + '%' } }
        }
    }
});
```

#### Coloration conditionnelle des valeurs

```python
def badge(value: float | int, key: str) -> str:
    """Retourne un badge HTML coloré selon le contexte sémantique."""
    BAD  = {"non_rapproches", "manquants", "ko", "invalides", "risque",
            "divergent", "different", "doublons", "future_tp"}
    GOOD = {"rapproches", "presents", "ok", "coherent", "identique",
            "taux_couverture", "taux_resolution", "score_qualite"}

    key_l = key.lower()
    is_bad  = any(b in key_l for b in BAD)
    is_good = any(g in key_l for g in GOOD)

    if is_bad:
        color = "#ef4444" if (isinstance(value, (int,float)) and value > 0) else "#6ab023"
    elif is_good:
        color = "#6ab023" if (isinstance(value, float) and value >= 95) else "#f59e0b"
    else:
        color = "#374151"

    return f'<span style="color:{color};font-weight:600">{value}</span>'
```

### Interface publique

```python
def generate(
    json_path: Path,
    output_dir: Path,
    assets_dir: Path,
    progress_callback: Callable[[int, str], None] | None = None
) -> Path:
    """
    Génère le rapport HTML depuis le JSON courant + historique BDD.
    Retourne le chemin du fichier HTML généré.
    Le rapport PDF est optionnel (weasyprint) — skip silencieux si absent.
    """
```

---

## FICHIER 5 : `build.bat` — Compilation PyInstaller

```bat
@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHON=WinPython\WPy64-313130\python\python.exe"

echo [1/5] Installation des dependances build...
"%PYTHON%" -m pip install pyinstaller flask weasyprint --quiet

echo [2/5] Compilation PyInstaller...
"%PYTHON%" -m PyInstaller ^
  --name pipeline_ciam ^
  --onedir ^
  --noconsole ^
  --add-data "templates;templates" ^
  --add-data "static;static" ^
  --add-data "Scripts;Scripts" ^
  --add-data "config;config" ^
  --add-data "assets;assets" ^
  --hidden-import flask ^
  --hidden-import psycopg ^
  --hidden-import psycopg.adapt ^
  --hidden-import psycopg._psycopg ^
  --hidden-import sqlalchemy.dialects.postgresql ^
  --hidden-import openpyxl.cell._writer ^
  --hidden-import yaml ^
  --hidden-import weasyprint ^
  main.py

echo [3/5] Copie WinPython (runtime Python portable)...
xcopy /E /I /Y "WinPython\WPy64-313130\python" "dist\pipeline_ciam\python"

echo [4/5] Copie dossiers projet...
xcopy /E /I /Y "Input_Data" "dist\pipeline_ciam\Input_Data"
xcopy /E /I /Y "Output"     "dist\pipeline_ciam\Output"
xcopy /E /I /Y "Scripts"    "dist\pipeline_ciam\Scripts"
xcopy /E /I /Y "config"     "dist\pipeline_ciam\config"
xcopy /E /I /Y "assets"     "dist\pipeline_ciam\assets"
copy /Y "requirements.txt"  "dist\pipeline_ciam\"

echo [5/5] Creation du lanceur...
echo @echo off > "dist\pipeline_ciam\Lancer_Pipeline.bat"
echo start "" "pipeline_ciam.exe" >> "dist\pipeline_ciam\Lancer_Pipeline.bat"

echo.
echo ================================================================
echo  BUILD TERMINE — Livrable : dist\pipeline_ciam\
echo  Double-clic sur Lancer_Pipeline.bat ou pipeline_ciam.exe
echo ================================================================
pause
```

---

## Requirements finaux

```
pandas
numpy
openpyxl
psycopg[binary]
sqlalchemy
pyyaml
oracledb
flask
weasyprint
```

---

## Contraintes techniques critiques

### Threading Flask + PipelineRunner
- Flask tourne dans le thread principal
- `PipelineRunner` tourne dans `threading.Thread(daemon=True)`
- La communication se fait via `queue.Queue` thread-safe
- **Jamais** d'appel Tkinter (pas de Tkinter du tout dans ce projet)

### SSE — keep-alive
```python
# Eviter la déconnexion SSE sur inactivité :
yield ": keep-alive\n\n"   # commentaire SSE toutes les 15s
```

### Portabilité PyInstaller
```python
# Dans main.py et report_generator.py :
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).parent
    TEMPLATE_DIR = BASE_DIR / "templates"
    app = Flask(__name__, template_folder=str(TEMPLATE_DIR))
else:
    BASE_DIR = Path(__file__).parent
    app = Flask(__name__)
```

### Subprocess Windows
```python
CREATE_NO_WINDOW = 0x08000000
proc = subprocess.Popen(..., creationflags=CREATE_NO_WINDOW)
```

### Ouverture dossier Windows (route Flask)
```python
@app.route("/open-folder")
def open_folder():
    path = request.args.get("path", "")
    full = BASE_DIR / path
    if full.exists():
        os.startfile(str(full))   # Windows : ouvre l'explorateur
    return jsonify({"ok": True})
```

### Arrêt propre Flask
```python
@app.route("/shutdown", methods=["POST"])
def shutdown():
    def stop():
        time.sleep(0.5)
        os._exit(0)
    threading.Thread(target=stop, daemon=True).start()
    return jsonify({"ok": True})
```

---

## Interdictions absolues

- Ne pas modifier les scripts `01` à `07`
- Ne pas utiliser Tkinter (IHM 100% web)
- Ne pas hardcoder de chemins absolus
- Ne pas bloquer le thread Flask avec `subprocess.run()` ou `time.sleep()` long
- Ne pas utiliser `flask.run(debug=True)` en production (reloader désactivé)

---

## Livraison attendue

1. `main.py` — serveur Flask avec toutes les routes, SSE, gestion shutdown
2. `templates/index.html` — IHM web complète, auto-suffisante, charte MGEN
3. `pipeline_runner.py` — orchestrateur thread-safe avec pauses stdin
4. `report_generator.py` — rapport HTML auto-suffisant avec tendances BDD
5. `build.bat` — build PyInstaller + assemblage livrable portable

Chaque fichier doit être autonome, importable sans erreur, et commenté.
Tester la logique SSE, le threading, et les pauses avant de livrer.
