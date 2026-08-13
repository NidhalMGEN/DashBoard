/* Cartes tiers payant présentes en GED, pour une liste de KPEP IEHE.

   1. `tmstinj` est remonté EN PLUS de `idepsp` : c'est la date d'injection
      réelle du document en GED. Le pipeline la stocke dans
      `suivi_carte_tp.date_dispo_ged` et la distingue de `date_found_ged`, qui
      est la date à laquelle NOUS avons constaté la présence. Les deux ensemble
      répondent à la question métier « date de génération ou date de mise à
      disposition ? ».

   2. MIN(...) + GROUP BY est indispensable : un même KPEP peut porter plusieurs
      documents dans la fenêtre. Sans agrégation la requête renvoie une ligne
      par document, ce qui gonflerait le nombre de cartes « trouvées ». On
      retient la PREMIÈRE injection, cohérente avec le calcul du délai.

   3. Les bornes sont des BALISES, substituées par `write_ged_sql_batches()`.
      Elles étaient auparavant codées en dur (20260101 → 20260731) : la fenêtre
      ne suivait donc plus la date du flux, et toute exécution après le
      31/07/2026 interrogeait une période vide sans lever d'erreur.
      Ne jamais y réécrire une date en dur.
      Les deux comparateurs sont STRICTS ; `build_ged_window()` décale chaque
      borne d'un jour vers l'extérieur pour que les journées extrêmes soient
      bien couvertes.
*/
SELECT idepsp, MIN(tmstinj) AS tmstinj
FROM tged.docprospectadherent

WHERE codtypdoc = '1501'

--AND codsocpsp = '010'
AND appmetrefdoc ='0019CADH'

AND tmstinj > '__DATE_DEBUT__' AND tmstinj < '__DATE_FIN__'

AND idepsp in (
    __LISTE_IDS__
)
GROUP BY idepsp
