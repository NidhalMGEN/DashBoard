# Migration — du `main.py` monolithique à la plateforme modulaire

Cette note décrit le passage de l'application Flask mono-fichier
(`main.py` + `templates/index.html`) à l'architecture modulaire par
**blueprints** décrite dans `PROMPT_CLAUDE_CODE_FINAL.md`.

## Ce qui change

| Avant (monolithe) | Après (modulaire) |
|---|---|
| `main.py` (point d'entrée + toutes les routes) | `app.py` (factory + auto-registration depuis `_registry`) |
| `templates/index.html` (UI unique pipeline + rapport) | `templates/base.html` (layout sidebar/topbar) + un template par module |
| Logique pipeline dans `main.py` + `pipeline_runner.py` | `modules/pipeline/` (blueprint), réutilise `pipeline_runner.py` |
| `launch_SQL_query_V2.py` (CLI) | `modules/sql_runner/` (blueprint web) — logique extraite |
| — | `modules/dashboard/` (nouvelle page d'accueil supervision) |

## Nouvelle arborescence

```
app.py                      # factory Flask + enregistrement auto des blueprints
modules/
├── _registry.py            # liste des modules actifs (source de vérité nav)
├── dashboard/              # Module 00 — page d'accueil supervision
│   ├── routes.py · queries.py
│   └── templates/dashboard/index.html
├── pipeline/               # Module 01 — pipeline ETL
│   ├── routes.py · runner.py (réexporte pipeline_runner.PipelineRunner)
│   └── templates/pipeline/index.html
└── sql_runner/             # Module 02 — exécution requêtes SQL
    ├── routes.py · executor.py · env_loader.py · query_loader.py
    └── templates/sql_runner/index.html
templates/base.html         # layout commun
pipeline_runner.py          # orchestrateur (inchangé sauf sélection de scripts)
```

## Lancement

- **Avant** : `python main.py`
- **Après** : `python app.py` (le `Lancer_Pipeline.bat` a été mis à jour)

`main.py` est conservé pour compatibilité mais n'est plus l'entrée officielle.

## Ajouter un module

1. Créer `modules/<mon_module>/routes.py` exposant `bp = Blueprint(...)` avec
   une route `index` et un `template_folder="templates"`.
2. Ajouter une ligne `ModuleSpec(...)` dans `modules/_registry.py`.
3. C'est tout : la sidebar et l'enregistrement du blueprint sont automatiques.
   Pour masquer un module sans le supprimer : `enabled=False`.

## Points de compatibilité

- **Scripts 01–07 inchangés** : le pipeline les lance toujours en subprocess
  via `pipeline_runner.py`. Seul ajout : `selected_ids` pour l'exécution
  partielle (mode « Lancer la sélection »). Le mode « Pipeline complet »
  reproduit le comportement historique (toutes les étapes 01→07).
- **Credentials** :
  - Pipeline ETL : saisis dans l'IHM, passés au subprocess via `PG_USER`/`PG_PASSWORD`
    (étapes IEHE/retry uniquement) — comportement inchangé.
  - Module SQL : saisis à la demande, conservés **uniquement en session Flask**
    côté serveur, effaçables via « Se déconnecter ». Jamais en `localStorage`.
- **`launch_SQL_query_V2.py`** : la logique métier (`connect`, `execute_query`,
  `substitute_params`, `resolve_default_token`, `POST_PROCESSORS`,
  `load_environments`, scan des `.sql`) est extraite à l'identique dans
  `modules/sql_runner/{executor,env_loader,query_loader}.py`. Le script CLI
  reste utilisable tel quel.
- **État dégradé** : si une base est injoignable, les modules affichent un
  message clair sans planter (le dashboard renvoie `available:false`).

## Credentials centralisés — `config/credentials.ini`

Source de vérité unique des identifiants, éditable sans code :

1. Copier `config/credentials.ini.example` → `config/credentials.ini`.
2. Remplacer les `CHANGER_ICI` par les vrais identifiants.
3. Recharger à chaud via le bandeau topbar ou `POST /admin/reload-credentials`
   (pas de redémarrage nécessaire).

- `credentials.ini` est **gitignoré** ; seul le `.example` est versionné.
- Chargé par le singleton `config/credentials_loader.py`. Section absente/incomplète
  → état dégradé explicite (pas de crash).
- Utilisé automatiquement par le Dashboard (section `[postgresql_supervision]`),
  et pré-remplit le formulaire du SQL runner (mots de passe jamais renvoyés au
  navigateur — conservés côté serveur, complétés au moment de la connexion).
- Bandeau topbar : `✓ Credentials chargés` / `⚠ credentials.ini manquant`.

## Évolutions anticipées (non implémentées)

Le registre et l'isolation par blueprint permettent d'ajouter sans refonte :
Rapports CODIR (réutilise `report_generator.py`), Qualité données,
Monitoring temps réel, Admin (seuils/config), intégration Power BI.
