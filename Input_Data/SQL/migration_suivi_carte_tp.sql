/* Migration de rptpsc.suivi_carte_tp vers le schéma de Scripts/suivi_carte_tp_db.py
   =================================================================================

   RENOMMAGE UNIQUEMENT. AUCUNE COLONNE AJOUTÉE.

   POURQUOI
   --------
   La table déployée porte les noms de l'ANCIENNE génération de scripts
   (`Scripts/07_controle_tp_ged.py`, la table ayant été créée automatiquement par
   `Scripts/04_chargement_bdd.py` à partir des colonnes du CSV de détail). Le code
   en production (`scriptsNewPipline/07` et `/08`) attend les noms de `_COLUMNS`.
   `CREATE TABLE IF NOT EXISTS` ne migre RIEN : le DDL du module est un no-op
   silencieux et `fetch_ged_pending()` échoue sur « column kpep_iehe does not
   exist » — le contrôle GED ne renvoie donc aucune carte.

   LES 9 RENOMMAGES
   ----------------
     kpep_reference       -> kpep_iehe
     code_societe         -> code_soc_appart
     flux_origine         -> flux_id
     date_souscription    -> date_adhesion
     flux_dernier         -> date_derniere_verif   (TEXT 'DDMMYYYY' -> DATE)
     commentaire          -> motif
     date_correction      -> date_premiere_vue     (TIMESTAMP -> DATE)
     delai_creation_jours -> date_effet_adhesion   (INTEGER -> DATE)
     statut               -> date_found_iehe       (TEXT -> DATE)

   Les trois derniers sont des RÉEMPLOIS de colonnes, pas des correspondances de
   sens : `date_premiere_vue`, `date_effet_adhesion` et `date_found_iehe` n'ont
   aucun équivalent dans l'ancien schéma. Vérifié le 13/08/2026 sur les 1034
   lignes, ce qu'on écrase ne coûte rien :
     - `date_correction`      : 0 valeur renseignée (colonne vide)
     - `delai_creation_jours` : redondant, se recalcule (found - eligibilite)
     - `statut`               : identique à `statut_final` sur 1034/1034 lignes,
                                et déjà redondant avec `date_found_ged`
                                (PRESENTE <=> date_found_ged renseignée, 0 écart)
   `date_premiere_vue` est réalimentée depuis `flux_id` ; les deux autres partent
   à NULL, l'information n'a jamais existé.

   CE QUI RESTE
   ------------
   4 colonnes de l'ancien schéma subsistent, plus lues par aucun code :
   `statut_final`, `correction_manuelle`, `auteur`, `date_import`. Elles ne sont
   PAS supprimées ici (le DROP est en bas, commenté) — `auteur` porte 1034
   valeurs, à jeter seulement en connaissance de cause.

   `statut_final` est cependant NOT NULL SANS DÉFAUT : conservée telle quelle,
   elle ferait échouer chaque INSERT du script 03, qui ne la renseigne pas. Elle
   est donc rendue nullable (§ 5) — c'est la contrepartie de ne pas la supprimer.

   LA CLÉ PRIMAIRE (§ 4)
   ---------------------
   La table déployée porte DÉJÀ une clé primaire, mais sur les MAUVAISES
   colonnes : `suivi_carte_tp_pk (kpep_reference, code_societe, offre)`. Après
   renommage elle devient `(kpep_iehe, code_soc_appart, offre)` — une clé sur le
   KPEP, alors que le KPEP est justement NULL à l'insertion (le script 03 ne le
   connaît pas encore ; c'est le script 07 qui le résout). Elle est remplacée,
   pas seulement complétée.

   LANCEMENT
   ---------
     psql -h bdd-T0XX0052.alias -p 5577 -U rptpsc -d supervisionpsc_db \
          -v ON_ERROR_STOP=1 -f Input_Data/SQL/migration_suivi_carte_tp.sql
*/

BEGIN;

-- --------------------------------------------------------------------------
-- 0. Libération des noms d'index confisqués par une table de travail
-- --------------------------------------------------------------------------
-- Les cinq noms attendus par le module (`_DDL_INDEXES` + la clé primaire) sont
-- déjà pris dans le schéma `rptpsc` — non par `suivi_carte_tp`, mais par
-- `suivi_carte_tp_prototype_2026_08`, une copie d'essai laissée en place.
--
-- Un nom d'index est unique PAR SCHÉMA, pas par table. Conséquence silencieuse :
-- `CREATE INDEX IF NOT EXISTS idx_suivi_carte_tp_kpep ON rptpsc.suivi_carte_tp`
-- voit le nom pris, émet un simple NOTICE « already exists, skipping » et ne
-- crée RIEN. `ensure_table()` renvoie alors True sur une table sans le moindre
-- index — aucune erreur, mais chaque passage de `fetch_ged_pending()` en
-- seq scan. Idem pour `ADD CONSTRAINT suivi_carte_tp_pkey`, qui lui échoue
-- franchement (« relation suivi_carte_tp_pkey already exists »).
--
-- On renomme donc les index de la table d'essai plutôt que de la supprimer :
-- réversible, et ses 966 lignes ne sont pas notre affaire. Aucun code du dépôt
-- ne la référence (vérifié le 13/08/2026).
DO $$
DECLARE
    r record;
BEGIN
    FOR r IN
        SELECT c.relname AS idx, t.relname AS tbl
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
          JOIN pg_index i     ON i.indexrelid = c.oid
          JOIN pg_class t     ON t.oid = i.indrelid
         WHERE n.nspname  = 'rptpsc'
           AND t.relname <> 'suivi_carte_tp'
           AND c.relname IN ('suivi_carte_tp_pkey',
                             'idx_suivi_carte_tp_ged_pending',
                             'idx_suivi_carte_tp_kpep',
                             'idx_suivi_carte_tp_sans_kpep',
                             'idx_suivi_carte_tp_eligibilite')
    LOOP
        -- `left(..., 63)` : au-delà, PostgreSQL tronque en silence. Une collision
        -- après troncature ferait échouer l'ALTER, donc toute la migration —
        -- ce qui est le comportement voulu, plutôt qu'un renommage surprise.
        EXECUTE format('ALTER INDEX rptpsc.%I RENAME TO %I',
                       r.idx, left(r.tbl || '_' || r.idx, 63));
        RAISE NOTICE 'Index % (table %) renommé : le nom est rendu à suivi_carte_tp.',
                     r.idx, r.tbl;
    END LOOP;
END $$;

-- --------------------------------------------------------------------------
-- 1. Renommages simples (le type est déjà bon)
-- --------------------------------------------------------------------------
ALTER TABLE rptpsc.suivi_carte_tp RENAME COLUMN kpep_reference    TO kpep_iehe;
ALTER TABLE rptpsc.suivi_carte_tp RENAME COLUMN code_societe      TO code_soc_appart;
ALTER TABLE rptpsc.suivi_carte_tp RENAME COLUMN flux_origine      TO flux_id;
ALTER TABLE rptpsc.suivi_carte_tp RENAME COLUMN date_souscription TO date_adhesion;
ALTER TABLE rptpsc.suivi_carte_tp RENAME COLUMN commentaire       TO motif;

-- --------------------------------------------------------------------------
-- 2. Renommages avec changement de type
-- --------------------------------------------------------------------------
-- `flux_dernier` est un identifiant de flux 'DDMMYYYY' (TEXT) ; le code attend
-- une DATE. TO_DATE fait la conversion, NULLIF protège les chaînes vides.
ALTER TABLE rptpsc.suivi_carte_tp RENAME COLUMN flux_dernier TO date_derniere_verif;
ALTER TABLE rptpsc.suivi_carte_tp
    ALTER COLUMN date_derniere_verif TYPE DATE
    USING TO_DATE(NULLIF(TRIM(date_derniere_verif), ''), 'DDMMYYYY');

-- Colonne VIDE (0 valeur) réemployée. `date_premiere_vue` est l'ancre du délai
-- et de la provenance : elle est réalimentée juste après, jamais laissée NULL.
ALTER TABLE rptpsc.suivi_carte_tp RENAME COLUMN date_correction TO date_premiere_vue;
ALTER TABLE rptpsc.suivi_carte_tp
    ALTER COLUMN date_premiere_vue TYPE DATE USING date_premiere_vue::date,
    ALTER COLUMN date_premiere_vue DROP NOT NULL;

-- Colonne redondante (le délai se recalcule) réemployée. Aucune donnée d'origine
-- ne correspond à une date d'effet : on part à NULL.
-- Le DROP NOT NULL est indispensable : la colonne d'origine est NOT NULL, et la
-- vider (USING NULL) violerait la contrainte avant même la fin de l'ALTER.
ALTER TABLE rptpsc.suivi_carte_tp RENAME COLUMN delai_creation_jours TO date_effet_adhesion;
ALTER TABLE rptpsc.suivi_carte_tp
    ALTER COLUMN date_effet_adhesion DROP NOT NULL,
    ALTER COLUMN date_effet_adhesion TYPE DATE USING NULL;

-- `statut` est déjà porté par `date_found_ged` (0 écart sur 1034 lignes) :
-- la colonne est libre. Aucune date de découverte IEHE n'a jamais été stockée.
ALTER TABLE rptpsc.suivi_carte_tp RENAME COLUMN statut TO date_found_iehe;
ALTER TABLE rptpsc.suivi_carte_tp
    ALTER COLUMN date_found_iehe DROP NOT NULL,
    ALTER COLUMN date_found_iehe TYPE DATE USING NULL;

-- `date_dispo_ged` : TIMESTAMP -> DATE. Laissée en TIMESTAMP,
-- `date_dispo_ged - date_eligibilite` rend un INTERVAL et `GREATEST(interval, 0)`
-- échoue. Le KPI se compte en jours pleins.
ALTER TABLE rptpsc.suivi_carte_tp
    ALTER COLUMN date_dispo_ged TYPE DATE USING date_dispo_ged::date;

-- --------------------------------------------------------------------------
-- 3. Réalimentation de date_premiere_vue
-- --------------------------------------------------------------------------
UPDATE rptpsc.suivi_carte_tp
   SET date_premiere_vue = COALESCE(TO_DATE(NULLIF(TRIM(flux_id), ''), 'DDMMYYYY'),
                                    date_derniere_verif)
 WHERE date_premiere_vue IS NULL;

-- `motif` : vocabulaire fermé côté code. L'ancien `commentaire` était du texte
-- libre ; on ne le jette pas, mais les lignes corrigées à la main reçoivent le
-- motif que le code sait relire (`fetch_controle_stats` compte dessus).
UPDATE rptpsc.suivi_carte_tp
   SET motif = 'correction_manuelle'
 WHERE UPPER(COALESCE(correction_manuelle, '')) = 'OUI';

-- --------------------------------------------------------------------------
-- 4. Clé primaire et index attendus par le module
-- --------------------------------------------------------------------------
-- `flux_id` est DÉLIBÉRÉMENT hors de la clé : une personne vue dans deux flux ne
-- doit donner qu'UNE carte, sinon le compte des « cartes attendues » est gonflé.
-- Vérifié le 13/08/2026 : 0 doublon et 0 composant NULL sur les 1034 lignes.
--
-- L'ANCIENNE CLÉ DOIT PARTIR D'ABORD. Le test `IF NOT EXISTS (... contype='p')`
-- d'origine voyait la clé héritée `(kpep_reference, code_societe, offre)`, la
-- prenait pour la bonne et ne créait rien : `_UPSERT_SQL` échouait ensuite sur
-- « there is no unique or exclusion constraint matching the ON CONFLICT
-- specification », et le script 03 n'insérait plus une seule carte.
--
-- L'ordre des trois opérations n'est pas indifférent :
--   1. supprimer la clé   — impossible de rendre `kpep_iehe` nullable tant qu'il
--                           en fait partie (« column is in a primary key »)
--   2. ajuster les NOT NULL
--   3. recréer la clé     — `num_personne` doit être NOT NULL avant.
DO $$
DECLARE
    pk_name text;
    pk_def  text;
BEGIN
    SELECT conname, pg_get_constraintdef(oid) INTO pk_name, pk_def
      FROM pg_constraint
     WHERE conrelid = 'rptpsc.suivi_carte_tp'::regclass AND contype = 'p';

    IF pk_name IS NOT NULL
       AND pk_def IS DISTINCT FROM 'PRIMARY KEY (num_personne, code_soc_appart, offre)'
    THEN
        EXECUTE format('ALTER TABLE rptpsc.suivi_carte_tp DROP CONSTRAINT %I', pk_name);
        pk_name := NULL;
    END IF;

    -- `kpep_iehe` n'était NOT NULL que parce qu'il appartenait à cette clé. Le
    -- code l'exige NULLABLE : le script 03 insère les cartes sans KPEP (il n'a
    -- pas l'index IEHE par société) et `fetch_sans_kpep()` les retrouve
    -- justement par `kpep_iehe IS NULL`. Laisser NOT NULL ferait échouer le lot
    -- entier de `upsert_cartes()`.
    ALTER TABLE rptpsc.suivi_carte_tp
        ALTER COLUMN kpep_iehe       DROP NOT NULL,
        ALTER COLUMN num_personne    SET  NOT NULL,
        ALTER COLUMN code_soc_appart SET  NOT NULL,
        ALTER COLUMN offre           SET  NOT NULL;

    IF pk_name IS NULL THEN
        ALTER TABLE rptpsc.suivi_carte_tp
            ADD CONSTRAINT suivi_carte_tp_pkey
            PRIMARY KEY (num_personne, code_soc_appart, offre);
    END IF;
END $$;

-- --------------------------------------------------------------------------
-- 5. Colonnes de l'ancien schéma conservées mais devenues bloquantes
-- --------------------------------------------------------------------------
-- `statut_final` est NOT NULL sans valeur par défaut, et plus aucun code ne la
-- renseigne : chaque INSERT du script 03 échouerait sur « null value in column
-- statut_final violates not-null constraint ». On la rend nullable plutôt que de
-- la supprimer — sa suppression reste au choix du métier (bloc en bas).
ALTER TABLE rptpsc.suivi_carte_tp
    ALTER COLUMN statut_final DROP NOT NULL;

-- --------------------------------------------------------------------------
-- 6. Index attendus par le module
-- --------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_suivi_carte_tp_ged_pending
    ON rptpsc.suivi_carte_tp (date_eligibilite) WHERE date_found_ged IS NULL;
CREATE INDEX IF NOT EXISTS idx_suivi_carte_tp_kpep
    ON rptpsc.suivi_carte_tp (kpep_iehe);
CREATE INDEX IF NOT EXISTS idx_suivi_carte_tp_sans_kpep
    ON rptpsc.suivi_carte_tp (num_personne) WHERE kpep_iehe IS NULL;
CREATE INDEX IF NOT EXISTS idx_suivi_carte_tp_eligibilite
    ON rptpsc.suivi_carte_tp (date_eligibilite);

COMMIT;

/* Nettoyage facultatif des 4 colonnes de l'ancien schéma devenues inutiles.
   `auteur` porte 1034 valeurs : à lancer seulement si cette traçabilité ne sert
   plus à personne.

     ALTER TABLE rptpsc.suivi_carte_tp
         DROP COLUMN statut_final,
         DROP COLUMN correction_manuelle,
         DROP COLUMN auteur,
         DROP COLUMN date_import;
*/
