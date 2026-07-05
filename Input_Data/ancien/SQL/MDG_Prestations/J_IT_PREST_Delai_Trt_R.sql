-- db: mdg

WITH

cte_params AS (
    SELECT
        TRUNC(TO_DATE('{DATE_SUIVI}', 'DD/MM/YYYY'))     AS d_debut,
        TRUNC(TO_DATE('{DATE_SUIVI}', 'DD/MM/YYYY')) + 1 AS d_fin
    FROM DUAL
),

cte_dossiers_candidats AS (
    SELECT /*+ MATERIALIZE PARALLEL(PSOSANTECL, 4) */
        DISTINCT o.PSOSIDFDOS
    FROM LRCO_OWNER.PSOSANTECL  o
    CROSS JOIN cte_params        p
    WHERE o.PSOSCODETA = 'RJ'
      AND o.PSOSDDEOPE >= p.d_debut
      AND o.PSOSDDEOPE <  p.d_fin
),

table_max AS (
    SELECT /*+ MATERIALIZE */
        o.PSOSIDFDOS,
        o.PSOSAUDDCR,
        o.PSOSCODETA,
        MAX(o.PSOSNUMOPE) AS max_op
    FROM LRCO_OWNER.PSOSANTECL    o
    JOIN cte_dossiers_candidats    d  ON  d.PSOSIDFDOS = o.PSOSIDFDOS
    WHERE o.PSOSCODETA = 'RJ'
    GROUP BY o.PSOSIDFDOS, o.PSOSAUDDCR, o.PSOSCODETA
),

table_autre AS (
    SELECT
        m.PSOSIDFDOS,
        m.PSOSAUDDCR,
        s.PSASBOITRT,
        CASE
            WHEN s.PSASBOITRT LIKE 'GES%' THEN 'Rejet_NMASS_GESTION'
            WHEN s.PSASBOITRT LIKE 'DSI%' THEN 'Rejet_NMASS_DSI'
        END AS type_rejet
    FROM table_max                     m
    JOIN LRCO_OWNER.PSOSANTECL         c  ON  c.PSOSIDFDOS  = m.PSOSIDFDOS
                                          AND  c.PSOSNUMOPE  = m.max_op
                                          AND  c.PSOSAUDDCR  = m.PSOSAUDDCR
    CROSS JOIN cte_params              p
    JOIN LRCO_OWNER.PSASDOSSCL         s  ON  s.PSASIDFDOS  = m.PSOSIDFDOS
    WHERE c.PSOSDDEOPE >= p.d_debut
      AND c.PSOSDDEOPE <  p.d_fin
      AND (s.PSASBOITRT LIKE 'GES%' OR s.PSASBOITRT LIKE 'DSI%')
    GROUP BY m.PSOSIDFDOS, m.PSOSAUDDCR, s.PSASBOITRT,
             CASE
                 WHEN s.PSASBOITRT LIKE 'GES%' THEN 'Rejet_NMASS_GESTION'
                 WHEN s.PSASBOITRT LIKE 'DSI%' THEN 'Rejet_NMASS_DSI'
             END
),

table_min AS (
    SELECT
        o.PSOSIDFDOS,
        TRUNC(o.PSOSAUDDCR) AS date_premier_rejet
    FROM LRCO_OWNER.PSOSANTECL o
    WHERE o.PSOSCODETA = 'RJ'
      AND o.PSOSNUMOPE = 2
      AND o.PSOSIDFDOS IN (SELECT PSOSIDFDOS FROM table_autre)
),

table_rejets AS (
    SELECT
        a.PSOSIDFDOS,
        a.type_rejet,
        TRUNC(a.PSOSAUDDCR) - mn.date_premier_rejet   AS datediff
    FROM table_autre                    a
    JOIN table_min                      mn ON  mn.PSOSIDFDOS = a.PSOSIDFDOS
    JOIN LRCO_OWNER.PSEPERSICL          p  ON  p.PSEPIDFDOS  = a.PSOSIDFDOS
    WHERE p.PSEPCODORI = 'NMASS'
    GROUP BY a.PSOSIDFDOS, a.type_rejet,
             TRUNC(a.PSOSAUDDCR) - mn.date_premier_rejet
),

table_finale AS (
    SELECT
        type_rejet,
        COUNT(*)                                                          AS nb_NMASS,
        SUM(CASE WHEN datediff <= 4                          THEN 1 ELSE 0 END)  AS "J<5",
        SUM(CASE WHEN datediff > 4  AND datediff <= 9        THEN 1 ELSE 0 END)  AS "J>5<10",
        SUM(CASE WHEN datediff > 9  AND datediff <= 19       THEN 1 ELSE 0 END)  AS "J>10<20",
        SUM(CASE WHEN datediff > 19                          THEN 1 ELSE 0 END)  AS "J>20",
        (SELECT d_debut FROM cte_params)                                         AS date_reference
    FROM table_rejets
    GROUP BY type_rejet
)

SELECT * FROM table_finale
ORDER BY type_rejet
