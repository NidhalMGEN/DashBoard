# Audit de la solution — Pipeline CIAM MGEN

Audit réalisé en confrontant l'implémentation aux fichiers de référence :
- `docs/PROMPT_CLAUDE_CODE_FINAL.md` (spécification cible)
- `ETL_vf.bat` (séquence de production réelle — vérité terrain)
- `Scripts/01` à `07` (scripts métier, non modifiables)

## Verdict initial

La solution **ne fonctionnait pas** : le pipeline gelait après l'étape 2 (IEHE).
Quatre défauts ont été identifiés et corrigés dans `pipeline_runner.py`.

## Défauts identifiés et corrigés

### 1. Blocage définitif après l'étape 2 (CRITIQUE)

`Scripts/01_generation_donnees.py` se termine **systématiquement** (ligne 488) par :
```python
input("\nAppuyez sur Entrée pour quitter...")
```
ainsi que sur chaque chemin d'erreur (lignes 460, 467, 475).

L'orchestrateur ne gérait que le prompt CM/CK. Le subprocess restait donc bloqué
indéfiniment sur ce `input()`, stdin étant un pipe jamais alimenté.

**Correctif** : détection du marqueur `pour quitter` → envoi automatique de `\n`
pour laisser le script se terminer proprement.

### 2. Pause CM/CK jamais détectée (CRITIQUE)

Les prompts `input()` des scripts n'ont **pas de `\n` final**. L'ancienne lecture
`for line in proc.stdout` ne produit une ligne que sur un `\n` → le texte du prompt
n'était jamais transmis, et la pause n'était jamais déclenchée. S'ajoutait le
double tampon classique de l'itération sur un pipe.

**Correctif** : lecture **incrémentale** via `read1()` + décodeur UTF-8 incrémental.
Le buffer partiel est inspecté après chaque lecture, ce qui permet de détecter un
prompt même sans `\n` terminal. Marqueur de pause discriminant : `CM et CK`.

### 3. Crash UnicodeEncodeError sous Windows (CRITIQUE)

Les scripts impriment des emojis (`✅ ⏸️ 🔹 ❌`). Sous Windows, le `stdout` d'un
subprocess hérite de l'encodage `cp1252` → `UnicodeEncodeError` au premier emoji.

**Correctif** : injection de `PYTHONUTF8=1`, `PYTHONIOENCODING=utf-8`,
`PYTHONUNBUFFERED=1` dans l'environnement de chaque script enfant.

### 4. Déclencheurs conditionnels imprécis

L'ancien code testait `*Accolade*` / `*TP_GED*`. `ETL_vf.bat` utilise les motifs
exacts `*_Accolade - KPI*.xlsx` et `*_TP_GED.csv`.

**Correctif** : `TRIGGER_GLOBS` aligné sur `ETL_vf.bat`.

## Points conformes (vérifiés)

| Élément | Statut |
|---|---|
| Ordre des étapes (TCD→IEHE→[reliquat]→Détail→Retry→GED→KPI→BDD) | ✓ conforme à `ETL_vf.bat` |
| Pause RELIQUAT gérée par l'orchestrateur entre IEHE et Détail | ✓ (comme le `pause` batch) |
| `PG_USER`/`PG_PASSWORD` injectés pour 01 et 06 uniquement | ✓ |
| Script 04 : **pas** d'injection (credentials hardcodés) | ✓ |
| JSON courant `{prefix}_Modele_clean.json` dans `Output/` | ✓ (écrit par script 02) |
| Routes Flask, SSE, `/resume`, `/report`, `/shutdown` | ✓ |
| Arrêt du pipeline sur erreur d'une étape non conditionnelle | ✓ |

## Tests effectués

- Pause CM/CK détectée puis reprise sur « Continuer » → script continue ✓
- Prompt terminal « pour quitter » auto-répondu → exit 0 ✓
- Emojis dans la sortie sans crash ✓
- Script en échec (exit 1) → pipeline stoppé, `success=False` ✓
- Routes Flask : `GET /` 200, `GET /status` 200, `POST /start` validation 400,
  `GET /report` 404 sans JSON ✓
