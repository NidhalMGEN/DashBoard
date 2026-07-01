-- db: mdg

WITH

cte_params AS (
    SELECT
        TRUNC(TO_DATE('{DATE_SUIVI}', 'DD/MM/YYYY'))     AS d_debut,
        TRUNC(TO_DATE('{DATE_SUIVI}', 'DD/MM/YYYY')) + 1 AS d_fin
    FROM DUAL
),

origines AS (
    SELECT
        p.PSEPIDFDOS AS dossier_id,
        p.PSEPCODORI AS origine
    FROM LRCO_OWNER.PSEPERSICL p
    WHERE p.PSEPCODORI IN (
        'TPVIA', 'TPHOS', 'TPCVM', 'OXA', 'NMASS', 'SELFC', 'LUMAS',
        'ASSUR', 'TIERS', 'REGUL'
    )
),

cte_max_both AS (
    SELECT /*+ MATERIALIZE PARALLEL(PSDHISTOCL, 4) */
        h.PSDHIDFDOS AS dossier_id,
        MAX(CASE WHEN h.PSDHDATPAI >= p.d_debut
                  AND h.PSDHDATPAI <  p.d_fin
                  AND h.PSDHETADCP IN ('PA', 'RL')
                 THEN h.PSDHOPEDCP END)               AS max_ope_paye,
        MAX(CASE WHEN h.PSDHDDEOPE >= p.d_debut
                  AND h.PSDHDDEOPE <  p.d_fin
                  AND h.PSDHETADCP IN ('VA', 'AC')
                 THEN h.PSDHOPEDCP END)               AS max_ope_attente
    FROM LRCO_OWNER.PSDHISTOCL  h
    CROSS JOIN cte_params        p
    WHERE (h.PSDHDATPAI >= p.d_debut AND h.PSDHDATPAI < p.d_fin)
       OR (h.PSDHDDEOPE >= p.d_debut AND h.PSDHDDEOPE < p.d_fin
           AND h.PSDHETADCP IN ('VA', 'AC'))
    GROUP BY h.PSDHIDFDOS
),

table_paye AS (
    SELECT /*+ MATERIALIZE */
        m.dossier_id
    FROM cte_max_both m
    WHERE m.max_ope_paye IS NOT NULL
),

montants AS (
    SELECT /*+ MATERIALIZE */
        d.psdeidfdos            AS dossier_id,
        SUM(d.psdemntrct)       AS montant_total
    FROM LRCO_OWNER.PSDECOMPCL  d
    WHERE d.psdeidfdos IN (SELECT dossier_id FROM table_paye)
    GROUP BY d.psdeidfdos
),

table_recu AS (
    SELECT
        o.origine,
        COUNT(*) AS nb_recu
    FROM LRCO_OWNER.PSDURECUCL  r
    CROSS JOIN cte_params        p
    JOIN origines                o  ON  o.dossier_id = r.PSDUIDFSYS
    WHERE r.PSDUDATARR >= p.d_debut
      AND r.PSDUDATARR <  p.d_fin
      AND r.PSDUTYPDEM = 'S'
    GROUP BY o.origine
),

table_paye_final AS (
    SELECT /*+ USE_HASH(o) */
        o.origine,
        COUNT(*)                    AS nb_payes,
        SUM(NVL(m.montant_total,0)) AS montant_payes
    FROM table_paye                  t
    JOIN origines                    o  ON  o.dossier_id = t.dossier_id
    LEFT JOIN montants               m  ON  m.dossier_id = t.dossier_id
    GROUP BY o.origine
),

table_attente AS (
    SELECT
        o.origine,
        COUNT(*) AS nb_attente
    FROM cte_max_both               m
    JOIN origines                   o  ON  o.dossier_id = m.dossier_id
    WHERE m.max_ope_attente IS NOT NULL
    GROUP BY o.origine
),

tableau_final_par_origine AS (
    SELECT
        COALESCE(r.origine, p.origine, a.origine)   AS code_origine,
        NVL(r.nb_recu,         0)                   AS nb_recu,
        NVL(p.nb_payes,        0)                   AS nb_payes,
        NVL(p.montant_payes,   0)                   AS montant_payes,
        NVL(a.nb_attente,      0)                   AS nb_attente
    FROM table_recu          r
    FULL JOIN table_paye_final p  ON  p.origine = r.origine
    FULL JOIN table_attente    a  ON  a.origine = COALESCE(r.origine, p.origine)
)

SELECT /*+ RESULT_CACHE */
    p.d_debut                                                               AS date_reference,

    MAX(CASE WHEN code_origine = 'TPVIA'  THEN nb_recu       ELSE 0 END)  AS recu_TPVIA,
    MAX(CASE WHEN code_origine = 'TPVIA'  THEN nb_payes      ELSE 0 END)  AS paye_TPVIA,
    MAX(CASE WHEN code_origine = 'TPVIA'  THEN montant_payes ELSE 0 END)  AS mnt_TPVIA,

    MAX(CASE WHEN code_origine = 'TPHOS'  THEN nb_recu       ELSE 0 END)  AS recu_TPHOS,
    MAX(CASE WHEN code_origine = 'TPHOS'  THEN nb_payes      ELSE 0 END)  AS paye_TPHOS,
    MAX(CASE WHEN code_origine = 'TPHOS'  THEN montant_payes ELSE 0 END)  AS mnt_TPHOS,

    MAX(CASE WHEN code_origine = 'TPCVM'  THEN nb_recu       ELSE 0 END)  AS recu_TPCVM,
    MAX(CASE WHEN code_origine = 'TPCVM'  THEN nb_payes      ELSE 0 END)  AS paye_TPCVM,
    MAX(CASE WHEN code_origine = 'TPCVM'  THEN montant_payes ELSE 0 END)  AS mnt_TPCVM,

    MAX(CASE WHEN code_origine = 'OXA'    THEN nb_recu       ELSE 0 END)  AS recu_OXA,
    MAX(CASE WHEN code_origine = 'OXA'    THEN nb_payes      ELSE 0 END)  AS paye_OXA,
    MAX(CASE WHEN code_origine = 'OXA'    THEN montant_payes ELSE 0 END)  AS mnt_OXA,

    MAX(CASE WHEN code_origine = 'NMASS'  THEN nb_recu       ELSE 0 END)  AS recu_NMASS,
    MAX(CASE WHEN code_origine = 'NMASS'  THEN nb_payes      ELSE 0 END)  AS paye_NMASS,
    MAX(CASE WHEN code_origine = 'NMASS'  THEN montant_payes ELSE 0 END)  AS mnt_NMASS,

    SUM(CASE WHEN code_origine IN ('ASSUR','TIERS','REGUL') THEN nb_recu       ELSE 0 END)  AS recu_TOTAL_MANUEL,
    SUM(CASE WHEN code_origine IN ('ASSUR','TIERS','REGUL') THEN nb_payes      ELSE 0 END)  AS paye_TOTAL_MANUEL,
    SUM(CASE WHEN code_origine IN ('ASSUR','TIERS','REGUL') THEN montant_payes ELSE 0 END)  AS mnt_TOTAL_MANUEL,
    SUM(CASE WHEN code_origine IN ('ASSUR','TIERS','REGUL') THEN nb_attente    ELSE 0 END)  AS attente_TOTAL_MANUEL,

    MAX(CASE WHEN code_origine = 'ASSUR'  THEN nb_recu       ELSE 0 END)  AS recu_ASSUR,
    MAX(CASE WHEN code_origine = 'ASSUR'  THEN nb_payes      ELSE 0 END)  AS paye_ASSUR,
    MAX(CASE WHEN code_origine = 'ASSUR'  THEN montant_payes ELSE 0 END)  AS mnt_ASSUR,

    MAX(CASE WHEN code_origine = 'TIERS'  THEN nb_recu       ELSE 0 END)  AS recu_TIERS,
    MAX(CASE WHEN code_origine = 'TIERS'  THEN nb_payes      ELSE 0 END)  AS paye_TIERS,
    MAX(CASE WHEN code_origine = 'TIERS'  THEN montant_payes ELSE 0 END)  AS mnt_TIERS,

    MAX(CASE WHEN code_origine = 'REGUL'  THEN nb_recu       ELSE 0 END)  AS recu_REGUL,
    MAX(CASE WHEN code_origine = 'REGUL'  THEN nb_payes      ELSE 0 END)  AS paye_REGUL,
    MAX(CASE WHEN code_origine = 'REGUL'  THEN montant_payes ELSE 0 END)  AS mnt_REGUL,

    MAX(CASE WHEN code_origine = 'SELFC'  THEN nb_recu       ELSE 0 END)  AS recu_SELFC,
    MAX(CASE WHEN code_origine = 'SELFC'  THEN nb_payes      ELSE 0 END)  AS paye_SELFC,
    MAX(CASE WHEN code_origine = 'SELFC'  THEN montant_payes ELSE 0 END)  AS mnt_SELFC,

    MAX(CASE WHEN code_origine = 'LUMAS'  THEN nb_recu       ELSE 0 END)  AS recu_LUMAS,
    MAX(CASE WHEN code_origine = 'LUMAS'  THEN nb_payes      ELSE 0 END)  AS paye_LUMAS,
    MAX(CASE WHEN code_origine = 'LUMAS'  THEN montant_payes ELSE 0 END)  AS mnt_LUMAS

FROM tableau_final_par_origine
CROSS JOIN cte_params p
