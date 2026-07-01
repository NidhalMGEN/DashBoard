-- db: iehe
-- Compte les lignes en attente de traitement (stt='A TRAITER') dans iehe.recyclage
-- Aucun paramètre requis : pas de filtre date (compte instantané).
-- Auto-exécutée en mode quotidien (préfixe J_).

SELECT
    COALESCE(typcou, 'TOTAL_PAR_CODSOC') AS typcou,
    codsoc,
    COUNT(*) AS total_lignes
FROM IEHE.recyclage
WHERE stt = 'A TRAITER'
GROUP BY GROUPING SETS ((typcou, codsoc), (codsoc))
ORDER BY typcou, codsoc;
