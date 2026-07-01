-- db: dwh

WITH ref_origines AS (
    SELECT 'DRASS' AS code FROM DUAL UNION ALL
    SELECT 'NMFOU' FROM DUAL UNION ALL
    SELECT 'NMASS' FROM DUAL UNION ALL
    SELECT 'LUMAS' FROM DUAL UNION ALL
    SELECT 'NMCMU' FROM DUAL UNION ALL
    SELECT 'TPVIA' FROM DUAL UNION ALL
    SELECT 'TPHOS' FROM DUAL UNION ALL
    SELECT 'TPCVM' FROM DUAL UNION ALL
    SELECT 'OXA'   FROM DUAL
),
ref_manuel AS (
    SELECT 'ASSUR' AS code FROM DUAL UNION ALL
    SELECT 'SELFC' FROM DUAL UNION ALL
    SELECT 'TIERS' FROM DUAL UNION ALL
    SELECT 'REGUL' FROM DUAL
),

dwh_integre AS (
    SELECT
        'DWH-Intégré' AS requete,
        code_origine,
        SUM(nombre_dossiers) AS nb
    FROM TRCO_owner.prestation_nb_integre
    WHERE date_extraction = TO_DATE('{DATE_SUIVI}', 'DD/MM/YYYY')
    GROUP BY code_origine
),

dwh_paye AS (
    SELECT
        'DWH_Payé' AS requete,
        COALESCE(dp.code_origine, di.code_origine) AS code_origine,
        COUNT(DISTINCT COALESCE(dp.idfsys_dossier, di.idfsys_dossier)) AS nb
    FROM (
        SELECT code_origine, idfsys_dossier
        FROM TRCO_owner.decaiss_paiem_sante_dec
        WHERE etat_decompte IN ('PA', 'RL')
          AND date_effet = TO_DATE('{DATE_SUIVI}', 'DD/MM/YYYY')
    ) dp
    FULL JOIN (
        SELECT code_origine, idfsys_dossier
        FROM TRCO_owner.decaiss_indu_dec
        WHERE date_maj_dwh = TO_DATE('{DATE_SUIVI}', 'DD/MM/YYYY')
    ) di ON di.idfsys_dossier = dp.idfsys_dossier
    GROUP BY COALESCE(dp.code_origine, di.code_origine)
),

dwh_releve AS (
    SELECT
        'DWH_Relevé' AS requete,
        code_origine,
        COUNT(DISTINCT idfsys_dossier) AS nb
    FROM TRCO_owner.decaiss_paiem_sante_dec
    WHERE etat_decompte = 'RL'
      AND date_imput_lot = TO_DATE('{DATE_SUIVI}', 'DD/MM/YYYY')
    GROUP BY code_origine
),

dwh_attente AS (
    SELECT
        'DWH-Attente' AS requete,
        code_origine,
        COUNT(*) AS nb
    FROM TRCO_owner.prestation_en_attente
    WHERE date_creation_dcpt = TO_DATE('{DATE_SUIVI}', 'DD/MM/YYYY')
    GROUP BY code_origine
),

dwh_rejets AS (
    SELECT
        'DWH_Rejets' AS requete,
        code_origine,
        COUNT(*) AS nb
    FROM TRCO_owner.prestation_rejet_dossier
    WHERE date_arrivee = TO_DATE('{DATE_SUIVI}', 'DD/MM/YYYY')
    GROUP BY code_origine
),

final AS (
    SELECT
        requete,
        NVL(SUM(CASE WHEN code_origine = 'DRASS' THEN nb END), 0) AS drass,
        NVL(SUM(CASE WHEN code_origine = 'NMFOU' THEN nb END), 0) AS nmfou,
        NVL(SUM(CASE WHEN code_origine = 'NMASS' THEN nb END), 0) AS nmass,
        NVL(SUM(CASE WHEN code_origine = 'LUMAS' THEN nb END), 0) AS lumas,
        NVL(SUM(CASE WHEN code_origine = 'NMCMU' THEN nb END), 0) AS nmcmu,
        NVL(SUM(CASE WHEN code_origine = 'TPVIA' THEN nb END), 0) AS tpvia,
        NVL(SUM(CASE WHEN code_origine = 'TPHOS' THEN nb END), 0) AS tphos,
        NVL(SUM(CASE WHEN code_origine = 'TPCVM' THEN nb END), 0) AS tpcvm,
        NVL(SUM(CASE WHEN code_origine = 'OXA'   THEN nb END), 0) AS oxa,
        NVL(SUM(CASE WHEN code_origine IN (SELECT code FROM ref_origines) THEN nb END), 0) AS total_auto,
        NVL(SUM(CASE WHEN code_origine = 'ASSUR' THEN nb END), 0) AS assur,
        NVL(SUM(CASE WHEN code_origine = 'SELFC' THEN nb END), 0) AS selfc,
        NVL(SUM(CASE WHEN code_origine = 'TIERS' THEN nb END), 0) AS tiers,
        NVL(SUM(CASE WHEN code_origine = 'REGUL' THEN nb END), 0) AS regul,
        NVL(SUM(CASE WHEN code_origine IN (SELECT code FROM ref_manuel) THEN nb END), 0) AS total_manuel
    FROM (
        SELECT * FROM dwh_integre
        UNION ALL SELECT * FROM dwh_paye
        UNION ALL SELECT * FROM dwh_releve
        UNION ALL SELECT * FROM dwh_attente
        UNION ALL SELECT * FROM dwh_rejets
    )
    GROUP BY requete
)

SELECT * FROM final
ORDER BY CASE requete
           WHEN 'DWH-Intégré' THEN 1
           WHEN 'DWH_Payé'    THEN 2
           WHEN 'DWH_Relevé'  THEN 3
           WHEN 'DWH-Attente' THEN 4
           WHEN 'DWH_Rejets'  THEN 5
           ELSE 99
         END
