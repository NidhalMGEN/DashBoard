/* OPTIMIZED QUERY FOR KPEP
   Logic: Filter Users by KPEP -> Find Latest Event -> Join Attributes ONCE
*/
SELECT idepsp FROM tged.docprospectadherent

WHERE codtypdoc = '1501'

--AND codsocpsp = '010'

AND tmstinj > '20260101' AND tmstinj < '20260731'
--info : tmstinj > est inclusif et tmstinj < est exclusif
AND idepsp in (
    __LISTE_IDS__
)