-- db: mdg
-- param.DATE_DEBUT: Date début (YYYYMMDD)
-- param.DATE_FIN: Date fin (YYYYMMDD)
-- param.ORIGINES: Origines (CSV, LIKE % OK, ou TOUTES) [TOUTES]
-- param.OFFRES: Offres (CSV, LIKE % OK, ou TOUTES, ou NULL) [TOUTES]
--
-- Comptage des prestations par date d'arrivée / offre / origine / état.
-- Refacto V2 : UNION x3 -> 1 plan unique avec LEFT JOIN sur chaîne contrats,
-- ROW_NUMBER au lieu du MAX corrélé, ANSI JOIN.
--
-- Astuce ORIGINES / OFFRES :
--   - TOUTES (défaut) : pas de filtre
--   - Liste CSV : LUMAS,NMASS,OXA  (matching exact ou LIKE si % présent : MESMEN%,CVM%)
--   - NULL dans OFFRES : ne ramène que les lignes sans offre (sans psdecompcl et sans contrat)

WITH
origines_filter AS (
    SELECT TRIM(REGEXP_SUBSTR('{ORIGINES}', '[^,]+', 1, LEVEL)) AS pattern
    FROM dual
    CONNECT BY LEVEL <= REGEXP_COUNT('{ORIGINES}', ',') + 1
),
offres_filter AS (
    SELECT TRIM(REGEXP_SUBSTR('{OFFRES}', '[^,]+', 1, LEVEL)) AS pattern
    FROM dual
    CONNECT BY LEVEL <= REGEXP_COUNT('{OFFRES}', ',') + 1
),
contrats AS (
    -- Chaîne contrats : 1 ligne par (acopr1, perei1) → la plus récente active
    SELECT peafnumsee, acofcodoff
    FROM (
        SELECT
            peaf.peafnumsee,
            acof.acofcodoff,
            ach2.ach2codeta,
            ach2.ach2ddezra,
            ROW_NUMBER() OVER (
                PARTITION BY ach2.ach2acopr1, ach2.ach2perei1
                ORDER BY ach2.ach2acopr2 DESC
            ) AS rn
        FROM LRCO_OWNER.peafrocl    peaf
        JOIN LRCO_OWNER.perorocl    pero ON pero.peropeafr1 = peaf.peafidfsys
        JOIN LRCO_OWNER.perelaticl  pere ON pere.perepeper1 = pero.peropeper1
        JOIN LRCO_OWNER.ach2asshcl  ach2 ON ach2.ach2perei1 = pere.pereidfsys
        JOIN LRCO_OWNER.acofoffhci  acof ON acof.acofacopr1 = ach2.ach2acopr1
    )
    WHERE rn = 1
      AND (ach2codeta = 'VA' OR ach2ddezra > SYSDATE)
),
demandes AS (
    SELECT
        psdu.psdudatarr,
        psdu.psdunumdem,
        psde.psdecodoff,
        psde.psdeidfdos,
        psep.psepetenro,
        psep.psepcodori,
        psos.psoscodeta
    FROM LRCO_OWNER.psdurecucl psdu
    JOIN LRCO_OWNER.psosantecl psos
      ON psos.psosidfdos = psdu.psduidfsys
     AND psos.psosnumope = psdu.psduderopv
    JOIN LRCO_OWNER.psepersicl psep
      ON psep.psepidfdos = psdu.psduidfsys
     AND psep.psepnumope = psdu.psduopveph
    LEFT JOIN LRCO_OWNER.psdecompcl psde
      ON psde.psdeidfdos = psdu.psduidfsys
    WHERE psdu.psdutypdem = 'S'
      AND psdu.psdudatarr BETWEEN TO_DATE('{DATE_DEBUT}','YYYYMMDD')
                              AND TO_DATE('{DATE_FIN}','YYYYMMDD')
      AND (
            UPPER('{ORIGINES}') = 'TOUTES'
         OR EXISTS (
                SELECT 1 FROM origines_filter f
                WHERE psep.psepcodori LIKE f.pattern
            )
          )
)
SELECT
    TO_CHAR(d.psdudatarr,'YYYYMMDD')              AS "Date_arrivee",
    COALESCE(d.psdecodoff, c.acofcodoff)           AS "Offre",
    d.psepcodori                                   AS "Origine",
    d.psoscodeta                                   AS "Etat",
    COUNT(DISTINCT TO_CHAR(d.psdudatarr,'YYYYMMDD') || d.psdunumdem) AS "Nb_demandes"
FROM demandes d
LEFT JOIN contrats c
       ON c.peafnumsee = d.psepetenro
      AND d.psdeidfdos IS NULL
WHERE
       UPPER('{OFFRES}') = 'TOUTES'
    OR EXISTS (
        SELECT 1 FROM offres_filter f
        WHERE COALESCE(d.psdecodoff, c.acofcodoff) LIKE f.pattern
           OR (f.pattern = 'NULL' AND COALESCE(d.psdecodoff, c.acofcodoff) IS NULL)
    )
GROUP BY
    TO_CHAR(d.psdudatarr,'YYYYMMDD'),
    COALESCE(d.psdecodoff, c.acofcodoff),
    d.psepcodori,
    d.psoscodeta
ORDER BY 1, 2, 3, 4;
