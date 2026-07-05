-- db: iehe
-- param.DATE_JOUR: Date IEHE du jour (YYYYMMDD) [TODAY]
--
-- Comptage IEHE par typcou et état (TRAITEES / TRAITEES PAR RECYCLAGE / RECYCLES / REJETEES).
-- ATTENTION : la DATE_JOUR à saisir correspond à la date CALENDAIRE d'exécution
-- (ex : run du dimanche soir → date du lundi). Le défaut [TODAY] couvre le cas standard,
-- mais on peut écraser la valeur pour rattraper un run manqué.
--
-- Origine : IEHE_Compteurs_code_société.sql (PostgreSQL session vars supprimées).
-- La requête d'inspection trailing (SELECT * FROM IEHE.rejet WHERE typcou='ECHEANCIER_COTISATIONS')
-- a été retirée — à exécuter manuellement si besoin de diagnostic.

WITH base AS (

    -- TRAITEES (envois Kafka hors recyclage)
    SELECT
        typcou,
        'TRAITEES' AS Etat,
        COUNT(*) AS Nombre,
        MIN(dattrt) AS du,
        MAX(dattrt) AS au
    FROM iehe.envoi_kafka
    WHERE nmficcob <> 'RECYCLAGE'
      AND to_char(dattrt, 'YYYYMMDD') = '{DATE_JOUR}'
    GROUP BY typcou

    UNION ALL

    -- TRAITEES PAR RECYCLAGE
    SELECT
        typcou,
        'TRAITEES PAR RECYCLAGE' AS Etat,
        COUNT(*) AS Nombre,
        MIN(dattrt) AS du,
        MAX(dattrt) AS au
    FROM iehe.envoi_kafka
    WHERE nmficcob LIKE 'RECYCLAGE%'
      AND to_char(dattrt, 'YYYYMMDD') = '{DATE_JOUR}'
    GROUP BY typcou

    UNION ALL

    -- RECYCLES (reçues du jour et placées en recyclage)
    SELECT
        typcou,
        'RECYCLES' AS Etat,
        COUNT(*) AS Nombre,
        MIN(to_date(substr(flx, strpos(flx, 'dateTraitement')+17, 8),'YYYYMMDD')) AS du,
        MAX(to_date(substr(flx, strpos(flx, 'dateTraitement')+17, 8),'YYYYMMDD')) AS au
    FROM iehe.recyclage
    WHERE substr(flx, strpos(flx, 'dateTraitement')+17, 8) = '{DATE_JOUR}'
    GROUP BY typcou

    UNION ALL

    -- REJETEES (sur règle fonctionnelle)
    SELECT
        typcou,
        'REJETEES' AS Etat,
        COUNT(*) AS Nombre,
        MIN(dattrt) AS du,
        MAX(dattrt) AS au
    FROM iehe.rejet
    WHERE to_char(dattrt, 'YYYYMMDD') = '{DATE_JOUR}'
    GROUP BY typcou
)

SELECT
    typcou,
    CASE WHEN Etat IS NULL THEN 'TOTAL' ELSE Etat END AS Etat,
    SUM(Nombre) AS Nombre,
    MIN(du) AS du,
    MAX(au) AS au,
    to_char(justify_interval(MAX(au) - MIN(du)), 'HH24:MI:SS') AS duree_hms
FROM base
GROUP BY GROUPING SETS (
    (typcou, Etat),   -- détail par état
    (typcou)          -- total par typcou
)
ORDER BY typcou, Etat;
