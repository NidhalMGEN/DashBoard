-- db: dwh
-- post_process: sum_total_doublons

WITH table_double AS (
    SELECT DISTINCT prestation_en_attente.ctrl_double_signature, libelle_long
    FROM TRCO_OWNER.prestation_en_attente, TRCO_OWNER.ctrl_double_signature
    WHERE prestation_en_attente.ctrl_double_signature = ctrl_double_signature.ctrl_double_signature
),

table_code_origin AS (
    SELECT prestation_en_attente.ctrl_double_signature,
           prestation_en_attente.code_origine,
           COUNT(DISTINCT prestation_en_attente.idfsys_dossier) AS "nombre"
    FROM TRCO_OWNER.prestation_en_attente
    GROUP BY prestation_en_attente.ctrl_double_signature, prestation_en_attente.code_origine
),

table_rejet AS (
    SELECT table_double.ctrl_double_signature,
           table_code_origin.code_origine,
           table_code_origin."nombre"
    FROM table_double, table_code_origin
    WHERE table_double.ctrl_double_signature = table_code_origin.ctrl_double_signature
)

SELECT
    table_double.ctrl_double_signature,
    table_double.libelle_long,
    CASE
        WHEN (SELECT table_rejet."nombre" FROM table_rejet WHERE table_double.ctrl_double_signature = table_rejet.ctrl_double_signature AND table_rejet.code_origine = 'DRASS') IS NULL THEN '0'
        ELSE (SELECT TO_CHAR(table_rejet."nombre") FROM table_rejet WHERE table_double.ctrl_double_signature = table_rejet.ctrl_double_signature AND table_rejet.code_origine = 'DRASS')
    END AS "drass",
    CASE
        WHEN (SELECT table_rejet."nombre" FROM table_rejet WHERE table_double.ctrl_double_signature = table_rejet.ctrl_double_signature AND table_rejet.code_origine = 'NMFOU') IS NULL THEN '0'
        ELSE (SELECT TO_CHAR(table_rejet."nombre") FROM table_rejet WHERE table_double.ctrl_double_signature = table_rejet.ctrl_double_signature AND table_rejet.code_origine = 'NMFOU')
    END AS "nmfou",
    CASE
        WHEN (SELECT table_rejet."nombre" FROM table_rejet WHERE table_double.ctrl_double_signature = table_rejet.ctrl_double_signature AND table_rejet.code_origine = 'NMASS') IS NULL THEN '0'
        ELSE (SELECT TO_CHAR(table_rejet."nombre") FROM table_rejet WHERE table_double.ctrl_double_signature = table_rejet.ctrl_double_signature AND table_rejet.code_origine = 'NMASS')
    END AS "nmass",
    CASE
        WHEN (SELECT table_rejet."nombre" FROM table_rejet WHERE table_double.ctrl_double_signature = table_rejet.ctrl_double_signature AND table_rejet.code_origine = 'NMCMU') IS NULL THEN '0'
        ELSE (SELECT TO_CHAR(table_rejet."nombre") FROM table_rejet WHERE table_double.ctrl_double_signature = table_rejet.ctrl_double_signature AND table_rejet.code_origine = 'NMCMU')
    END AS "nmcmu",
    CASE
        WHEN (SELECT table_rejet."nombre" FROM table_rejet WHERE table_double.ctrl_double_signature = table_rejet.ctrl_double_signature AND table_rejet.code_origine = 'TPVIA') IS NULL THEN '0'
        ELSE (SELECT TO_CHAR(table_rejet."nombre") FROM table_rejet WHERE table_double.ctrl_double_signature = table_rejet.ctrl_double_signature AND table_rejet.code_origine = 'TPVIA')
    END AS "tpvia",
    CASE
        WHEN (SELECT table_rejet."nombre" FROM table_rejet WHERE table_double.ctrl_double_signature = table_rejet.ctrl_double_signature AND table_rejet.code_origine = 'TPHOS') IS NULL THEN '0'
        ELSE (SELECT TO_CHAR(table_rejet."nombre") FROM table_rejet WHERE table_double.ctrl_double_signature = table_rejet.ctrl_double_signature AND table_rejet.code_origine = 'TPHOS')
    END AS "tphos",
    CASE
        WHEN (SELECT table_rejet."nombre" FROM table_rejet WHERE table_double.ctrl_double_signature = table_rejet.ctrl_double_signature AND table_rejet.code_origine = 'OXA') IS NULL THEN '0'
        ELSE (SELECT TO_CHAR(table_rejet."nombre") FROM table_rejet WHERE table_double.ctrl_double_signature = table_rejet.ctrl_double_signature AND table_rejet.code_origine = 'OXA')
    END AS "oxa",
    CASE
        WHEN (SELECT table_rejet."nombre" FROM table_rejet WHERE table_double.ctrl_double_signature = table_rejet.ctrl_double_signature AND table_rejet.code_origine = 'TPCVM') IS NULL THEN '0'
        ELSE (SELECT TO_CHAR(table_rejet."nombre") FROM table_rejet WHERE table_double.ctrl_double_signature = table_rejet.ctrl_double_signature AND table_rejet.code_origine = 'TPCVM')
    END AS "TPCVM",
    CASE
        WHEN (SELECT SUM(table_rejet."nombre") FROM table_rejet WHERE table_double.ctrl_double_signature = table_rejet.ctrl_double_signature AND table_rejet.code_origine <> 'TIERS' AND table_rejet.code_origine <> 'OXA') IS NULL THEN '0'
        ELSE (SELECT TO_CHAR(SUM(table_rejet."nombre")) FROM table_rejet WHERE table_double.ctrl_double_signature = table_rejet.ctrl_double_signature AND table_rejet.code_origine <> 'TIERS' AND table_rejet.code_origine <> 'OXA')
    END AS "Total auto",
    CASE
        WHEN (SELECT table_rejet."nombre" FROM table_rejet WHERE table_double.ctrl_double_signature = table_rejet.ctrl_double_signature AND table_rejet.code_origine = 'ASSUR') IS NULL THEN '0'
        ELSE (SELECT TO_CHAR(table_rejet."nombre") FROM table_rejet WHERE table_double.ctrl_double_signature = table_rejet.ctrl_double_signature AND table_rejet.code_origine = 'ASSUR')
    END AS "assur",
    CASE
        WHEN (SELECT table_rejet."nombre" FROM table_rejet WHERE table_double.ctrl_double_signature = table_rejet.ctrl_double_signature AND table_rejet.code_origine = 'TIERS') IS NULL THEN '0'
        ELSE (SELECT TO_CHAR(table_rejet."nombre") FROM table_rejet WHERE table_double.ctrl_double_signature = table_rejet.ctrl_double_signature AND table_rejet.code_origine = 'TIERS')
    END AS "tiers",
    CASE
        WHEN (SELECT SUM(table_rejet."nombre") FROM table_rejet WHERE table_double.ctrl_double_signature = table_rejet.ctrl_double_signature AND (table_rejet.code_origine = 'OXA' OR table_rejet.code_origine = 'TIERS')) IS NULL THEN '0'
        ELSE (SELECT TO_CHAR(SUM(table_rejet."nombre")) FROM table_rejet WHERE table_double.ctrl_double_signature = table_rejet.ctrl_double_signature AND (table_rejet.code_origine = 'OXA' OR table_rejet.code_origine = 'TIERS'))
    END AS "Total manuel",
    CASE
        WHEN (SELECT table_rejet."nombre" FROM table_rejet WHERE table_double.ctrl_double_signature = table_rejet.ctrl_double_signature AND table_rejet.code_origine = 'LUMAS') IS NULL THEN '0'
        ELSE (SELECT TO_CHAR(table_rejet."nombre") FROM table_rejet WHERE table_double.ctrl_double_signature = table_rejet.ctrl_double_signature AND table_rejet.code_origine = 'LUMAS')
    END AS "LUMAS",
    CASE
        WHEN (SELECT table_rejet."nombre" FROM table_rejet WHERE table_double.ctrl_double_signature = table_rejet.ctrl_double_signature AND table_rejet.code_origine = 'SELFC') IS NULL THEN '0'
        ELSE (SELECT TO_CHAR(table_rejet."nombre") FROM table_rejet WHERE table_double.ctrl_double_signature = table_rejet.ctrl_double_signature AND table_rejet.code_origine = 'SELFC')
    END AS "SELFC",
    CASE
        WHEN (SELECT SUM(table_rejet."nombre") FROM table_rejet WHERE table_double.ctrl_double_signature = table_rejet.ctrl_double_signature) IS NULL THEN 0
        ELSE (SELECT SUM(table_rejet."nombre") FROM table_rejet WHERE table_double.ctrl_double_signature = table_rejet.ctrl_double_signature)
    END AS "Total"
FROM table_double
WHERE table_double.ctrl_double_signature <> '@'
ORDER BY "Total" DESC
