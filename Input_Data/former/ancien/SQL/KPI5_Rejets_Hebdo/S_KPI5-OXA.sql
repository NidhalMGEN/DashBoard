-- db: mdg

-- ============================================================================
-- KPI5 - Plus ancien rejet pour origine = 'OXA'
-- Decline de la requete globale S_KPI5-Min_Max_DSI_rejets.sql.
-- Le pre-filtre PSEPCODORI = 'OXA' est applique au plus tot (CTE 'ori')
-- pour reduire les volumes joints et eviter le PARTITION BY global.
-- ============================================================================

WITH
cte_params AS (
    SELECT
        TRUNC(TO_DATE('{DATE_SUIVI}', 'DD/MM/YYYY')) - 6 AS d_debut,
        TRUNC(TO_DATE('{DATE_SUIVI}', 'DD/MM/YYYY')) + 1 AS d_fin
    FROM DUAL
),

-- 1 seul scan de PSOSANTECL : agrege min/max + filtre semaine via HAVING
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

-- Origine : derniere operation personne, filtree au PSEPCODORI cible
ori AS (
    SELECT PSEPIDFDOS, PSEPCODORI
    FROM (
        SELECT e.PSEPIDFDOS, e.PSEPCODORI,
               ROW_NUMBER() OVER (PARTITION BY e.PSEPIDFDOS ORDER BY e.PSEPNUMOPE DESC) AS rn
        FROM LRCO_OWNER.PSEPERSICL e
        WHERE e.PSEPIDFDOS IN (SELECT PSOSIDFDOS FROM cte_agg)
    )
    WHERE rn = 1
      AND PSEPCODORI = 'OXA'
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

-- Code rejet : dernier rejet du dossier (sur dossiers 'OXA' uniquement)
rj AS (
    SELECT PSRJIDFDOS, PSRJCODREJ
    FROM (
        SELECT r.PSRJIDFDOS, r.PSRJCODREJ,
               ROW_NUMBER() OVER (PARTITION BY r.PSRJIDFDOS ORDER BY r.PSRJNUMREJ DESC) AS rn
        FROM LRCO_OWNER.PSRJSANTCL r
        WHERE r.PSRJIDFDOS IN (SELECT PSEPIDFDOS FROM ori)
    )
    WHERE rn = 1
)

-- Top 1 par age_jours pour chaque boite de traitement (PSASBOITRT)
SELECT PSEPCODORI, LIBELLE_ORIGINE, PSASBOITRT, PSRJCODREJ, AGE_JOURS
FROM (
    SELECT
        ori.PSEPCODORI,
        'OXA'                                          AS LIBELLE_ORIGINE,
        das.PSASBOITRT,
        rj.PSRJCODREJ,
        ROUND(ag.date_derniere_op - ag.date_arrivee)         AS AGE_JOURS,
        ROW_NUMBER() OVER (
            PARTITION BY das.PSASBOITRT
            ORDER BY (ag.date_derniere_op - ag.date_arrivee) DESC NULLS LAST
        ) AS rk
    FROM cte_agg ag
    JOIN ori ON ori.PSEPIDFDOS = ag.PSOSIDFDOS
    JOIN das ON das.PSASIDFDOS = ag.PSOSIDFDOS
    JOIN rj  ON rj.PSRJIDFDOS  = ag.PSOSIDFDOS
)
WHERE rk = 1
ORDER BY AGE_JOURS DESC NULLS LAST, PSASBOITRT
