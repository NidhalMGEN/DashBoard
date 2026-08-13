-- db: mdg

WITH
cte_params AS (
    SELECT
        TRUNC(TO_DATE('{DATE_SUIVI}', 'DD/MM/YYYY'))     AS d_debut,
        TRUNC(TO_DATE('{DATE_SUIVI}', 'DD/MM/YYYY')) + 1 AS d_fin
    FROM DUAL
),

cte_max_both AS (
    SELECT /*+ MATERIALIZE PARALLEL(PSDHISTOCL, 8) */
        h.PSDHIDFDOS,
        MAX(CASE WHEN h.PSDHDATPAI >= p.d_debut
                  AND h.PSDHDATPAI <  p.d_fin
                  AND h.PSDHETADCP IN ('PA', 'RL')
                 THEN h.PSDHOPEDCP END) AS max_ope_datpai,
        MAX(CASE WHEN h.PSDHAUDDCR >= p.d_debut
                  AND h.PSDHAUDDCR <  p.d_fin
                  AND h.PSDHDDEOPE >= p.d_debut
                  AND h.PSDHDDEOPE <  p.d_fin
                 THEN h.PSDHOPEDCP END) AS max_ope_ddeope
    FROM LRCO_OWNER.PSDHISTOCL  h
    CROSS JOIN cte_params        p
    WHERE (h.PSDHDATPAI >= p.d_debut AND h.PSDHDATPAI < p.d_fin)
       OR (h.PSDHAUDDCR >= p.d_debut AND h.PSDHAUDDCR < p.d_fin
           AND h.PSDHDDEOPE >= p.d_debut AND h.PSDHDDEOPE < p.d_fin)
    GROUP BY h.PSDHIDFDOS
),

src_integres AS (
    SELECT /*+ PARALLEL(PSDURECUCL, 8) */
        p.PSEPCODORI,
        COUNT(*) AS Nombre
    FROM LRCO_OWNER.PSDURECUCL  r
    CROSS JOIN cte_params        pr
    JOIN LRCO_OWNER.PSEPERSICL  p  ON  p.PSEPIDFDOS = r.PSDUIDFSYS
    WHERE r.PSDUTYPDEM = 'S'
      AND r.PSDUAUDDCR >= pr.d_debut
      AND r.PSDUAUDDCR <  pr.d_fin
    GROUP BY p.PSEPCODORI
),

src_payes_releves AS (
    SELECT /*+ MATERIALIZE USE_NL(h r p) LEADING(m h r p) */
        p.PSEPCODORI,
        h.PSDHETADCP,
        COUNT(*) AS Nombre
    FROM cte_max_both             m
    JOIN LRCO_OWNER.PSDHISTOCL   h  ON  h.PSDHIDFDOS = m.PSDHIDFDOS
                                    AND  h.PSDHOPEDCP = m.max_ope_datpai
    JOIN LRCO_OWNER.PSDURECUCL   r  ON  r.PSDUIDFSYS  = h.PSDHIDFDOS
    JOIN LRCO_OWNER.PSEPERSICL   p  ON  p.PSEPIDFDOS  = r.PSDUIDFSYS
    WHERE m.max_ope_datpai IS NOT NULL
      AND h.PSDHETADCP IN ('PA', 'RL')
      AND r.PSDUTYPDEM = 'S'
    GROUP BY p.PSEPCODORI, h.PSDHETADCP
),

src_attentes AS (
    SELECT /*+ USE_NL(h r p) LEADING(m h r p) */
        p.PSEPCODORI,
        COUNT(*) AS Nombre
    FROM cte_max_both             m
    JOIN LRCO_OWNER.PSDHISTOCL   h  ON  h.PSDHIDFDOS = m.PSDHIDFDOS
                                    AND  h.PSDHOPEDCP = m.max_ope_ddeope
    JOIN LRCO_OWNER.PSDURECUCL   r  ON  r.PSDUIDFSYS  = h.PSDHIDFDOS
    JOIN LRCO_OWNER.PSEPERSICL   p  ON  p.PSEPIDFDOS  = r.PSDUIDFSYS
    WHERE m.max_ope_ddeope IS NOT NULL
      AND h.PSDHETADCP IN ('AC', 'VA')
      AND r.PSDUTYPDEM = 'S'
    GROUP BY p.PSEPCODORI
),

src_rejets AS (
    SELECT /*+ PARALLEL(PSOSANTECL, 8) */
        p.PSEPCODORI,
        COUNT(*) AS Nombre
    FROM LRCO_OWNER.PSOSANTECL  o
    CROSS JOIN cte_params        pr
    JOIN LRCO_OWNER.PSDURECUCL  r  ON  r.PSDUIDFSYS  = o.PSOSIDFDOS
    JOIN LRCO_OWNER.PSEPERSICL  p  ON  p.PSEPIDFDOS  = r.PSDUIDFSYS
    WHERE o.PSOSCODETA = 'RJ'
      AND o.PSOSNUMOPE = 2
      AND o.PSOSDDEOPE >= pr.d_debut
      AND o.PSOSDDEOPE <  pr.d_fin
      AND r.PSDUTYPDEM = 'S'
    GROUP BY p.PSEPCODORI
),

all_sources AS (
    SELECT 'MDG_Intégrés' AS requete, PSEPCODORI, Nombre
    FROM src_integres

    UNION ALL

    SELECT 'MDG_Payés', PSEPCODORI, SUM(Nombre) AS Nombre
    FROM src_payes_releves
    GROUP BY PSEPCODORI

    UNION ALL

    SELECT 'MDG_Relevés', PSEPCODORI, Nombre
    FROM src_payes_releves
    WHERE PSDHETADCP = 'RL'

    UNION ALL

    SELECT 'MDG_Attente', PSEPCODORI, Nombre
    FROM src_attentes

    UNION ALL

    SELECT 'MDG_Rejets', PSEPCODORI, Nombre
    FROM src_rejets
)

SELECT
    requete,
    SUM(CASE WHEN PSEPCODORI = 'DRASS'  THEN Nombre ELSE 0 END)  AS DRASS,
    SUM(CASE WHEN PSEPCODORI = 'NMFOU'  THEN Nombre ELSE 0 END)  AS NMFOU,
    SUM(CASE WHEN PSEPCODORI = 'NMASS'  THEN Nombre ELSE 0 END)  AS NMASS,
    SUM(CASE WHEN PSEPCODORI = 'LUMAS'  THEN Nombre ELSE 0 END)  AS LUMAS,
    SUM(CASE WHEN PSEPCODORI = 'NMCMU'  THEN Nombre ELSE 0 END)  AS NMCMU,
    SUM(CASE WHEN PSEPCODORI = 'TPVIA'  THEN Nombre ELSE 0 END)  AS TPVIA,
    SUM(CASE WHEN PSEPCODORI = 'TPHOS'  THEN Nombre ELSE 0 END)  AS TPHOS,
    SUM(CASE WHEN PSEPCODORI = 'TPCVM'  THEN Nombre ELSE 0 END)  AS TPCVM,
    SUM(CASE WHEN PSEPCODORI = 'OXA'    THEN Nombre ELSE 0 END)  AS OXA,
    SUM(CASE WHEN PSEPCODORI IN ('DRASS','NMFOU','NMASS','LUMAS','NMCMU',
                                  'TPVIA','TPHOS','TPCVM','OXA')
             THEN Nombre ELSE 0 END)                              AS TOTAL_AUTO,
    SUM(CASE WHEN PSEPCODORI = 'ASSUR'  THEN Nombre ELSE 0 END)  AS ASSUR,
    SUM(CASE WHEN PSEPCODORI = 'SELFC'  THEN Nombre ELSE 0 END)  AS SELFC,
    SUM(CASE WHEN PSEPCODORI = 'TIERS'  THEN Nombre ELSE 0 END)  AS TIERS,
    SUM(CASE WHEN PSEPCODORI = 'REGUL'  THEN Nombre ELSE 0 END)  AS REGUL,
    SUM(CASE WHEN PSEPCODORI IN ('ASSUR','SELFC','TIERS','REGUL')
             THEN Nombre ELSE 0 END)                              AS TOTAL_MANUEL,

    SUM(CASE WHEN PSEPCODORI NOT IN ('DRASS','NMFOU','NMASS','NMCMU',
                                      'TPVIA','TPHOS','TPCVM','OXA',
                                      'ASSUR','TIERS','REGUL',
                                      'SELFC','LUMAS')
             THEN Nombre ELSE 0 END)                              AS AUTRES
FROM all_sources
GROUP BY requete
ORDER BY
    CASE requete
        WHEN 'MDG_Intégrés' THEN 1
        WHEN 'MDG_Payés'    THEN 2
        WHEN 'MDG_Relevés'  THEN 3
        WHEN 'MDG_Attente'  THEN 4
        WHEN 'MDG_Rejets'   THEN 5
    END
