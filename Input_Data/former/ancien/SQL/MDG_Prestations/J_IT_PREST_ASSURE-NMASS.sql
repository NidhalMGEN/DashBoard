-- db: mdg

WITH

cte_params AS (
    SELECT
        TRUNC(TO_DATE('{DATE_SUIVI}', 'DD/MM/YYYY'))     AS d_debut,
        TRUNC(TO_DATE('{DATE_SUIVI}', 'DD/MM/YYYY')) + 1 AS d_fin
    FROM DUAL
),

table_max AS (
    SELECT /*+ MATERIALIZE PARALLEL(PSDHISTOCL, 4) */
        h.PSDHIDFDOS,
        MAX(h.PSDHOPEDCP) AS max_op
    FROM LRCO_OWNER.PSDHISTOCL h
    CROSS JOIN cte_params        p
    WHERE h.PSDHDATPAI >= p.d_debut
      AND h.PSDHDATPAI <  p.d_fin
      AND h.PSDHNUMDCP  = 1
    GROUP BY h.PSDHIDFDOS
),

table_crea AS (
    SELECT
        psosidfdos,
        TRUNC(psosauddcr) AS date_creation
    FROM LRCO_OWNER.PSOSANTECL
    WHERE psosnumope = 1
      AND psosidfdos IN (SELECT PSDHIDFDOS FROM table_max)
),

table_recu AS (
    SELECT /*+ USE_NL(h c p) LEADING(m h c p) */
        p.PSEPCODORI                            AS origine,
        m.PSDHIDFDOS,
        TRUNC(h.PSDHDATPAI)                     AS date_paiement,
        c.date_creation,
        TRUNC(h.PSDHDATPAI) - c.date_creation   AS datediff
    FROM table_max                              m
    JOIN LRCO_OWNER.PSDHISTOCL                  h  ON  h.PSDHIDFDOS = m.PSDHIDFDOS
                                                   AND  h.PSDHOPEDCP = m.max_op
    JOIN table_crea                             c  ON  c.psosidfdos  = m.PSDHIDFDOS
    JOIN LRCO_OWNER.PSEPERSICL                  p  ON  p.PSEPIDFDOS  = m.PSDHIDFDOS
    WHERE h.PSDHETADCP  IN ('PA', 'RL')
      AND p.PSEPCODORI   IN ('ASSUR', 'NMASS')
),

table_finale AS (
    SELECT
        CASE origine
            WHEN 'ASSUR' THEN 'Paiements_ASSUR'
            WHEN 'NMASS' THEN 'Paiements_NMASS'
        END                                                     AS type_paiement,
        COUNT(*)                                                AS paye_total,
        SUM(CASE WHEN datediff = 0 THEN 1 ELSE 0 END)          AS "J",
        SUM(CASE WHEN datediff = 1 THEN 1 ELSE 0 END)          AS "J+1",
        SUM(CASE WHEN datediff = 2 THEN 1 ELSE 0 END)          AS "J+2",
        SUM(CASE WHEN datediff = 3 THEN 1 ELSE 0 END)          AS "J+3",
        SUM(CASE WHEN datediff = 4 THEN 1 ELSE 0 END)          AS "J+4",
        SUM(CASE WHEN datediff > 4 THEN 1 ELSE 0 END)          AS "J++",
        (SELECT d_debut FROM cte_params)                        AS date_reference
    FROM table_recu
    GROUP BY origine
)

SELECT * FROM table_finale
ORDER BY type_paiement
