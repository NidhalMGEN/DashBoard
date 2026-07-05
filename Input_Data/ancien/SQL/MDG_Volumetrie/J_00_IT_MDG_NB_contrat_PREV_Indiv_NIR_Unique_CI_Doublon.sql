-- db: mdg

-- =======================================================================
-- Comptage du nombre total de contrats individuels PRÉVOYANCE et de
-- doublons par code offre.
--   nb_assures_doublonnes : nombre d'assurés (N° INSEE) concernés par des doublons
--   nb_doublons           : nombre de contrats en doublon
--
-- Version optimisée :
--   - ACH2ASSHCL et PEROROCL ne sont plus lus 3+ fois par sous-requête
--     corrélée MAX(...) ; ils sont scannés une seule fois et la "dernière
--     version" par clé est retenue via ROW_NUMBER() (top-1 per group).
--   - Les CTE sont matérialisées (/*+ MATERIALIZE */) pour éviter le
--     pipelining et les recalculs.
--   - Parallélisme activé sur la grosse table ACH2ASSHCL.
-- =======================================================================
WITH
-- Dernière version de chaque ligne (ACH2ACOPR1, ACH2ACASR2) :
-- remplace la corrélation " = (SELECT MAX(ACH2ACOPR2) ...)".
ach2_latest AS (
    SELECT /*+ MATERIALIZE */ *
    FROM (
        SELECT /*+ PARALLEL(A, 4) */
               A.ACH2ACOPR1,
               A.ACH2ACOPR2,
               A.ACH2ACASR2,
               A.ACH2CODETA,
               A.ACH2NUMCIN,
               A.ACH2CODTAS,
               A.ACH2PEREI1,
               ROW_NUMBER() OVER (
                   PARTITION BY A.ACH2ACOPR1, A.ACH2ACASR2
                   ORDER BY A.ACH2ACOPR2 DESC
               ) AS rn_latest
        FROM LRCO_OWNER.ACH2ASSHCL A
    )
    WHERE rn_latest = 1
),

-- Contrats avec au moins un bénéficiaire actif (VA) sur la dernière version.
Table_Non_Radie AS (
    SELECT /*+ MATERIALIZE */ DISTINCT l.ACH2ACOPR1
    FROM ach2_latest l
    WHERE l.ACH2CODETA = 'VA'
),

-- Bénéficiaires actifs (VA) des contrats non radiés.
Table_benef AS (
    SELECT /*+ MATERIALIZE */
           l.ACH2NUMCIN,
           l.ACH2CODTAS,
           l.ACH2PEREI1
    FROM ach2_latest l
    WHERE l.ACH2CODETA = 'VA'
      AND l.ACH2ACOPR1 IN (SELECT tnr.ACH2ACOPR1 FROM Table_Non_Radie tnr)
),

-- Dernière organisation par personne (PEROROCL) : remplace la corrélation
-- " = (SELECT MAX(PEROPEEVR1) ...)".
pero_latest AS (
    SELECT /*+ MATERIALIZE */ PEROPEPER1, PEROPEAFR1
    FROM (
        SELECT R.PEROPEPER1,
               R.PEROPEAFR1,
               ROW_NUMBER() OVER (
                   PARTITION BY R.PEROPEPER1
                   ORDER BY R.PEROPEEVR1 DESC
               ) AS rn_latest
        FROM LRCO_OWNER.PEROROCL R
    )
    WHERE rn_latest = 1
),

Table_benef_NOM AS (
    SELECT /*+ MATERIALIZE */
           P.PEREIDFSYS,
           F.PEAFNUMSEE
    FROM LRCO_OWNER.PERELATICL P
    JOIN pero_latest            R  ON  P.PEREPEPER1 = R.PEROPEPER1
    JOIN LRCO_OWNER.PEAFROCL    F  ON  R.PEROPEAFR1 = F.PEAFIDFSYS
    WHERE P.PEREIDFSYS IN (SELECT B.ACH2PEREI1 FROM Table_benef B)
),

-- Dernières souscriptions ASSPRI des contrats non radiés, pour les
-- bénéficiaires effectivement porteurs d'un NIR, restreint aux offres
-- de prévoyance (INPPREVIND, MEPMENP002, ...).
Final AS (
    SELECT BN.PEAFNUMSEE,              -- N° INSEE
           A.ACH2NUMCIN,                -- Contrat individuel
           O.ACOFCODOFF                 -- Code offre
    FROM ach2_latest              A
    JOIN LRCO_OWNER.ACOFOFFHCI    O   ON  A.ACH2ACOPR1 = O.ACOFACOPR1
    JOIN LRCO_OWNER.PROFFRECL     P   ON  P.PROFCODSIT = O.ACOFCODSIT
                                     AND P.PROFCODOFF = O.ACOFCODOFF
    JOIN Table_benef              B   ON  B.ACH2PEREI1 = A.ACH2PEREI1
    JOIN Table_benef_NOM          BN  ON  A.ACH2PEREI1 = BN.PEREIDFSYS
    WHERE A.ACH2CODTAS = 'ASSPRI'
      AND A.ACH2ACOPR1 IN (SELECT tnr.ACH2ACOPR1 FROM Table_Non_Radie tnr)
      -- Offres de prévoyance retenues
      AND O.ACOFCODOFF IN (
              'INPPREVIND','MEPMENP002','MEPMENP201','MEPMEAE001',
              'MEPMEAT002','MEPMAS001','INPCULT002','MEPCULT003'
          )
      -- AND O.ACOFCODOFF IN ('MESMEAE102','MESMEAE202')               -- Offre du MEAE
      -- AND O.ACOFCODOFF IN ('MESMEN001','MESMEN3001','MESMEN2003')
),

Synthese AS (
    SELECT F.ACOFCODOFF,
           F.PEAFNUMSEE,
           COUNT(DISTINCT F.ACH2NUMCIN) AS nb_contrats
    FROM Final F
    GROUP BY F.ACOFCODOFF, F.PEAFNUMSEE
)

-- Bloc global + détail par code offre
SELECT 'GLOBAL'                               AS niveau,
       NULL                                    AS code_offre,
       COUNT(DISTINCT PEAFNUMSEE)             AS nb_assures,
       SUM(nb_contrats)                        AS nb_contrats_individuels,
       COUNT(CASE WHEN nb_contrats > 1 THEN 1 END) AS nb_assures_doublonnes,
       SUM(nb_contrats) - COUNT(DISTINCT PEAFNUMSEE) AS nb_doublons
FROM Synthese
UNION ALL
SELECT 'PAR_OFFRE'                             AS niveau,
       S.ACOFCODOFF                            AS code_offre,
       COUNT(DISTINCT S.PEAFNUMSEE)            AS nb_assures,
       SUM(nb_contrats)                        AS nb_contrats_individuels,
       COUNT(CASE WHEN nb_contrats > 1 THEN 1 END) AS nb_assures_doublonnes,
       SUM(nb_contrats) - COUNT(DISTINCT S.PEAFNUMSEE) AS nb_doublons
FROM Synthese S
GROUP BY S.ACOFCODOFF
ORDER BY niveau, code_offre
