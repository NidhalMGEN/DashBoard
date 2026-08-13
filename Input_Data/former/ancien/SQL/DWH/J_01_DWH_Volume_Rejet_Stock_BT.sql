-- db: dwh
-- post_process: sum_boite_prefixes_stock

WITH table_boite AS (
    SELECT DISTINCT prestation_rejet_boite.code_boite
    FROM TRCO_OWNER.prestation_rejet_boite
),

table_codes AS (
    SELECT DISTINCT prestation_rejet_detail.code_rejet, libelle_long
    FROM TRCO_OWNER.prestation_rejet_detail, TRCO_OWNER.code_rejet
    WHERE prestation_rejet_detail.code_rejet = code_rejet.code_rejet
),

table_code_origin AS (
    SELECT prestation_rejet_boite.code_boite,
           prestation_rejet_detail.code_rejet,
           prestation_rejet_dossier.code_origine,
           COUNT(DISTINCT prestation_rejet_detail.idfsys_dossier) AS "nombre"
    FROM TRCO_OWNER.prestation_rejet_dossier,
         TRCO_OWNER.prestation_rejet_detail,
         TRCO_OWNER.prestation_rejet_boite
    WHERE prestation_rejet_dossier.idfsys_dossier = prestation_rejet_detail.idfsys_dossier
      AND prestation_rejet_boite.idfsys_dossier = prestation_rejet_detail.idfsys_dossier
    GROUP BY prestation_rejet_boite.code_boite,
             prestation_rejet_detail.code_rejet,
             prestation_rejet_dossier.code_origine
),

table_rejet AS (
    SELECT table_boite.code_boite,
           table_codes.code_rejet,
           table_codes.libelle_long,
           table_code_origin.code_origine,
           table_code_origin."nombre"
    FROM table_codes, table_code_origin, table_boite
    WHERE table_codes.code_rejet = table_code_origin.code_rejet
      AND table_boite.code_boite = table_code_origin.code_boite
)

SELECT
    table_boite.code_boite,
    table_codes.code_rejet,
    table_codes.libelle_long,
    CASE
        WHEN (SELECT table_rejet."nombre" FROM table_rejet WHERE table_rejet.code_boite = table_boite.code_boite AND table_codes.code_rejet = table_rejet.code_rejet AND table_rejet.code_origine = 'DRASS') IS NULL THEN '0'
        ELSE (SELECT TO_CHAR(table_rejet."nombre") FROM table_rejet WHERE table_rejet.code_boite = table_boite.code_boite AND table_codes.code_rejet = table_rejet.code_rejet AND table_rejet.code_origine = 'DRASS')
    END AS "drass",
    CASE
        WHEN (SELECT table_rejet."nombre" FROM table_rejet WHERE table_rejet.code_boite = table_boite.code_boite AND table_codes.code_rejet = table_rejet.code_rejet AND table_rejet.code_origine = 'NMFOU') IS NULL THEN '0'
        ELSE (SELECT TO_CHAR(table_rejet."nombre") FROM table_rejet WHERE table_rejet.code_boite = table_boite.code_boite AND table_codes.code_rejet = table_rejet.code_rejet AND table_rejet.code_origine = 'NMFOU')
    END AS "nmfou",
    CASE
        WHEN (SELECT table_rejet."nombre" FROM table_rejet WHERE table_rejet.code_boite = table_boite.code_boite AND table_codes.code_rejet = table_rejet.code_rejet AND table_rejet.code_origine = 'NMASS') IS NULL THEN '0'
        ELSE (SELECT TO_CHAR(table_rejet."nombre") FROM table_rejet WHERE table_rejet.code_boite = table_boite.code_boite AND table_codes.code_rejet = table_rejet.code_rejet AND table_rejet.code_origine = 'NMASS')
    END AS "nmass",
    CASE
        WHEN (SELECT table_rejet."nombre" FROM table_rejet WHERE table_rejet.code_boite = table_boite.code_boite AND table_codes.code_rejet = table_rejet.code_rejet AND table_rejet.code_origine = 'NMCMU') IS NULL THEN '0'
        ELSE (SELECT TO_CHAR(table_rejet."nombre") FROM table_rejet WHERE table_rejet.code_boite = table_boite.code_boite AND table_codes.code_rejet = table_rejet.code_rejet AND table_rejet.code_origine = 'NMCMU')
    END AS "nmcmu",
    CASE
        WHEN (SELECT table_rejet."nombre" FROM table_rejet WHERE table_rejet.code_boite = table_boite.code_boite AND table_codes.code_rejet = table_rejet.code_rejet AND table_rejet.code_origine = 'TPVIA') IS NULL THEN '0'
        ELSE (SELECT TO_CHAR(table_rejet."nombre") FROM table_rejet WHERE table_rejet.code_boite = table_boite.code_boite AND table_codes.code_rejet = table_rejet.code_rejet AND table_rejet.code_origine = 'TPVIA')
    END AS "tpvia",
    CASE
        WHEN (SELECT table_rejet."nombre" FROM table_rejet WHERE table_rejet.code_boite = table_boite.code_boite AND table_codes.code_rejet = table_rejet.code_rejet AND table_rejet.code_origine = 'TPHOS') IS NULL THEN '0'
        ELSE (SELECT TO_CHAR(table_rejet."nombre") FROM table_rejet WHERE table_rejet.code_boite = table_boite.code_boite AND table_codes.code_rejet = table_rejet.code_rejet AND table_rejet.code_origine = 'TPHOS')
    END AS "tphos",
    CASE
        WHEN (SELECT table_rejet."nombre" FROM table_rejet WHERE table_rejet.code_boite = table_boite.code_boite AND table_codes.code_rejet = table_rejet.code_rejet AND table_rejet.code_origine = 'OXA') IS NULL THEN '0'
        ELSE (SELECT TO_CHAR(table_rejet."nombre") FROM table_rejet WHERE table_rejet.code_boite = table_boite.code_boite AND table_codes.code_rejet = table_rejet.code_rejet AND table_rejet.code_origine = 'OXA')
    END AS "oxa",
    CASE
        WHEN (SELECT table_rejet."nombre" FROM table_rejet WHERE table_rejet.code_boite = table_boite.code_boite AND table_codes.code_rejet = table_rejet.code_rejet AND table_rejet.code_origine = 'TPCVM') IS NULL THEN '0'
        ELSE (SELECT TO_CHAR(table_rejet."nombre") FROM table_rejet WHERE table_rejet.code_boite = table_boite.code_boite AND table_codes.code_rejet = table_rejet.code_rejet AND table_rejet.code_origine = 'TPCVM')
    END AS "TPCVM",
    CASE
        WHEN (SELECT SUM(table_rejet."nombre") FROM table_rejet WHERE table_rejet.code_boite = table_boite.code_boite AND table_codes.code_rejet = table_rejet.code_rejet AND table_rejet.code_origine <> 'TIERS' AND table_rejet.code_origine <> 'OXA') IS NULL THEN '0'
        ELSE (SELECT TO_CHAR(SUM(table_rejet."nombre")) FROM table_rejet WHERE table_rejet.code_boite = table_boite.code_boite AND table_codes.code_rejet = table_rejet.code_rejet AND table_rejet.code_origine <> 'TIERS' AND table_rejet.code_origine <> 'OXA')
    END AS "Total auto",
    CASE
        WHEN (SELECT table_rejet."nombre" FROM table_rejet WHERE table_rejet.code_boite = table_boite.code_boite AND table_codes.code_rejet = table_rejet.code_rejet AND table_rejet.code_origine = 'ASSUR') IS NULL THEN '0'
        ELSE (SELECT TO_CHAR(table_rejet."nombre") FROM table_rejet WHERE table_rejet.code_boite = table_boite.code_boite AND table_codes.code_rejet = table_rejet.code_rejet AND table_rejet.code_origine = 'ASSUR')
    END AS "assur",
    CASE
        WHEN (SELECT table_rejet."nombre" FROM table_rejet WHERE table_rejet.code_boite = table_boite.code_boite AND table_codes.code_rejet = table_rejet.code_rejet AND table_rejet.code_origine = 'TIERS') IS NULL THEN '0'
        ELSE (SELECT TO_CHAR(table_rejet."nombre") FROM table_rejet WHERE table_rejet.code_boite = table_boite.code_boite AND table_codes.code_rejet = table_rejet.code_rejet AND table_rejet.code_origine = 'TIERS')
    END AS "tiers",
    CASE
        WHEN (SELECT SUM(table_rejet."nombre") FROM table_rejet WHERE table_rejet.code_boite = table_boite.code_boite AND table_codes.code_rejet = table_rejet.code_rejet AND (table_rejet.code_origine = 'OXA' OR table_rejet.code_origine = 'TIERS')) IS NULL THEN '0'
        ELSE (SELECT TO_CHAR(SUM(table_rejet."nombre")) FROM table_rejet WHERE table_rejet.code_boite = table_boite.code_boite AND table_codes.code_rejet = table_rejet.code_rejet AND (table_rejet.code_origine = 'OXA' OR table_rejet.code_origine = 'TIERS'))
    END AS "Total manuel",
    CASE
        WHEN (SELECT table_rejet."nombre" FROM table_rejet WHERE table_rejet.code_boite = table_boite.code_boite AND table_codes.code_rejet = table_rejet.code_rejet AND table_rejet.code_origine = 'LUMAS') IS NULL THEN '0'
        ELSE (SELECT TO_CHAR(table_rejet."nombre") FROM table_rejet WHERE table_rejet.code_boite = table_boite.code_boite AND table_codes.code_rejet = table_rejet.code_rejet AND table_rejet.code_origine = 'LUMAS')
    END AS "LUMAS",
    CASE
        WHEN (SELECT table_rejet."nombre" FROM table_rejet WHERE table_rejet.code_boite = table_boite.code_boite AND table_codes.code_rejet = table_rejet.code_rejet AND table_rejet.code_origine = 'SELFC') IS NULL THEN '0'
        ELSE (SELECT TO_CHAR(table_rejet."nombre") FROM table_rejet WHERE table_rejet.code_boite = table_boite.code_boite AND table_codes.code_rejet = table_rejet.code_rejet AND table_rejet.code_origine = 'SELFC')
    END AS "SELFC",
    CASE
        WHEN (SELECT SUM(table_rejet."nombre") FROM table_rejet WHERE table_rejet.code_boite = table_boite.code_boite AND table_codes.code_rejet = table_rejet.code_rejet) IS NULL THEN 0
        ELSE (SELECT SUM(table_rejet."nombre") FROM table_rejet WHERE table_rejet.code_boite = table_boite.code_boite AND table_codes.code_rejet = table_rejet.code_rejet)
    END AS "Total"
FROM table_codes, table_boite
WHERE (SELECT SUM(table_rejet."nombre") FROM table_rejet WHERE table_rejet.code_boite = table_boite.code_boite AND table_codes.code_rejet = table_rejet.code_rejet) IS NOT NULL
ORDER BY table_boite.code_boite, "Total" DESC
