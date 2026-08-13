-- db: mdg

-- ============================================================================
-- KPI5 - Plus ancien rejet pour les origines NON listees explicitement
-- (decline de S_KPI5-Min_Max_DSI_rejets.sql).
-- Le pre-filtre exclut au plus tot (CTE 'ori') les 6 origines deja couvertes
-- par leur propre fichier (NMASS, TPVIA, TPHOS, TPCVM, OXA, LUMAS).
-- Renvoie potentiellement plusieurs lignes : une par PSEPCODORI restant.
-- ============================================================================

WITH
cte_params AS (
    SELECT
        TRUNC(TO_DATE('{DATE_SUIVI}', 'DD/MM/YYYY')) - 6 AS d_debut,
        TRUNC(TO_DATE('{DATE_SUIVI}', 'DD/MM/YYYY')) + 1 AS d_fin
    FROM DUAL
),

cte_agg AS (
    SELECT
        PSOSIDFDOS,
        MIN(CASE WHEN PSOSNUMOPE = 2 THEN PSOSDDEOPE END)  AS date_arrivee,
        MAX(PSOSDDEOPE)                                     AS date_derniere_op
    FROM LRCO_OWNER.PSOSANTECL
    WHERE PSOSCODETA = 'RJ'
    GROUP BY PSOSIDFDOS
    HAVING SUM(
        CASE WHEN PSOSDDEOPE >= (SELECT d_debut FROM cte_params)
              AND PSOSDDEOPE <  (SELECT d_fin   FROM cte_params)
             THEN 1 ELSE 0 END
    ) > 0
),

-- Origine : derniere operation personne, EXCLUSION des origines deja couvertes
ori AS (
    SELECT PSEPIDFDOS, PSEPCODORI
    FROM (
        SELECT e.PSEPIDFDOS, e.PSEPCODORI,
               ROW_NUMBER() OVER (PARTITION BY e.PSEPIDFDOS ORDER BY e.PSEPNUMOPE DESC) AS rn
        FROM LRCO_OWNER.PSEPERSICL e
        WHERE e.PSEPIDFDOS IN (SELECT PSOSIDFDOS FROM cte_agg)
    )
    WHERE rn = 1
      AND PSEPCODORI NOT IN ('NMASS','TPVIA','TPHOS','TPCVM','OXA','LUMAS')
),

-- Boite de traitement : TOUTES les boites historiques du dossier
-- Un dossier peut avoir transite par plusieurs boites ; on conserve
-- chaque couple (dossier, boite) distinct pour couvrir toutes les boites
-- (aligne sur la requete de reference qui joint PSASDOSSCL sans filtre).
das AS (
    SELECT DISTINCT d.PSASIDFDOS, d.PSASBOITRT
    FROM LRCO_OWNER.PSASDOSSCL d
    WHERE d.PSASIDFDOS IN (SELECT PSEPIDFDOS FROM ori)
),

rj AS (
    SELECT PSRJIDFDOS, PSRJCODREJ
    FROM (
        SELECT r.PSRJIDFDOS, r.PSRJCODREJ,
               ROW_NUMBER() OVER (PARTITION BY r.PSRJIDFDOS ORDER BY r.PSRJNUMREJ DESC) AS rn
        FROM LRCO_OWNER.PSRJSANTCL r
        WHERE r.PSRJIDFDOS IN (SELECT PSEPIDFDOS FROM ori)
    )
    WHERE rn = 1
),

table_ranked AS (
    SELECT
        ori.PSEPCODORI,
        das.PSASBOITRT,
        rj.PSRJCODREJ,
        ROUND(ag.date_derniere_op - ag.date_arrivee) AS age_jours,
        ROW_NUMBER() OVER (
            PARTITION BY ori.PSEPCODORI, das.PSASBOITRT
            ORDER BY (ag.date_derniere_op - ag.date_arrivee) DESC NULLS LAST
        ) AS rk
    FROM cte_agg ag
    JOIN ori ON ori.PSEPIDFDOS = ag.PSOSIDFDOS
    JOIN das ON das.PSASIDFDOS = ag.PSOSIDFDOS
    JOIN rj  ON rj.PSRJIDFDOS  = ag.PSOSIDFDOS
)

SELECT
    PSEPCODORI,
    'Autre — ' || PSEPCODORI    AS LIBELLE_ORIGINE,
    PSASBOITRT,
    PSRJCODREJ,
    age_jours                   AS AGE_JOURS
FROM table_ranked
WHERE rk = 1
ORDER BY age_jours DESC NULLS LAST, PSEPCODORI, PSASBOITRT
