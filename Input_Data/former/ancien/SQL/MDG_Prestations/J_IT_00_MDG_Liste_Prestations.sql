-- db: mdg
-- param.DATE_DEBUT: Date début (YYYYMMDD)
-- param.DATE_FIN: Date fin (YYYYMMDD)
-- param.ORIGINES: Origines (CSV, LIKE % OK, ou TOUTES) [TOUTES]
-- param.OFFRES: Offres (CSV, LIKE % OK, ou TOUTES, ou NULL) [TOUTES]
--
-- Liste détaillée des décomptes par date d'arrivée / offre / origine / état
-- avec INSEE, fournisseur, facture, contrat.
-- Mêmes optimisations que P_IT_00_MDG_Count_Prestations.sql + remontée ach2numcin.

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
    SELECT peafnumsee, acofcodoff, ach2numcin
    FROM (
        SELECT
            peaf.peafnumsee,
            acof.acofcodoff,
            ach2.ach2numcin,
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
        psdu.psdunumfcu,
        psde.psdecodoff,
        psde.psdenumcin,
        psde.psdeidfdos,
        psep.psepetenro,
        psep.psepcodori,
        psep.psepcodfrn,
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
    TO_CHAR(d.psdudatarr,'YYYYMMDD')              AS "Date arrivee",
    COALESCE(d.psdecodoff, c.acofcodoff)           AS "Offre",
    d.psepcodori                                   AS "Origine",
    d.psoscodeta                                   AS "Etat",
    d.psdunumdem                                   AS "Demande",
    d.psepetenro                                   AS "INSEE",
    d.psepcodfrn                                   AS "Fournisseur",
    d.psdunumfcu                                   AS "Facture",
    COALESCE(d.psdenumcin, c.ach2numcin)           AS "Contrat"
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
ORDER BY 1, 2, 3, 4;
