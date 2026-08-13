/* BATCH 1 | GENERATED 2026-08-06 15:27:22.355036 | SOURCE: 19072026 | NB: 111 */
/* OPTIMIZED QUERY 
   Logic: Filter Users -> Find Latest Event -> Join Attributes ONCE
*/

WITH TargetUsers AS (
    -- 1. Récupération des utilisateurs cibles (Filtre primaire)
    SELECT
        usr.id,
        usr.email,
        usr.first_name,
        usr.last_name,
        usr.created_timestamp
    FROM rcia.user_entity usr
    WHERE LOWER(usr.email) IN (
        'dalier.sylvie@orange.fr','maria-stella.inzaina@ac-corse.fr','cbs34000@gmail.com','sof.besuelle@sfr.fr','ludovic.gilbaut@ac-reunion.fr','romane.boyer154@gmail.com','susicyelena@yahoo.fr','celine.coue@ac-nantes.fr','sarahblqr@gmail.com','aourirfr@yahoo.com','sylviecristol12@gmail.com','layetbergmann@gmx.net','marlyfon47@gmail.com','carlamoutier1@gmail.com','sophie.rairat@gmail.com','elise.coupeau@ac-nantes.fr','alexissefyu@yahoo.fr','bendalis@wanadoo.fr','emiliedazin@hotmail.fr','mary.e.lecorre@gmail.com','gaelle.granier@wanadoo.fr','valerie.morales-gonzales@univ-orleans.fr','valeriemoralesgonzales@gmail.com','simon.jeauffreau@gmail.com','mamadou1984@hotmail.fr','mariepierre.romey@free.fr','gabriella.rozniecka@univ-eiffel.fr','fabienne.hatesse@saintriquier.com','laure.berthonneau@orange.fr','lamydelachapellecharlotte@gmail.com','mervelay.mj@gmail.com','cris98@free.fr','kylmai26@yahoo.fr','sandrinebenjamin1@gmail.com','michel-guirado@orange.fr','pcaruel.pro@gmail.com','alexis.badia@sorbonne-nouvelle.fr','nicolas.rouger@cnrs.fr','nicolas.girard741@orange.fr','rouger.nicolas@gmail.com','guigouv@free.fr','isameyer1406@yahoo.fr','lola.rambaud@ac-amiens.fr','hicham.malitoufa@cnrs.fr','imane.yagoubi@ac-creteil.fr','micheguillaume52300@gmail.com','ericfauqueux92@gmail.com','guillaume.miche@ac-reims.fr','jennifer.clement@ac-versailles.fr','chloe.jamet@sciencespo.fr','amadou.diallo@crous-nantes.fr','olivierjouille@free.fr','fournesvanessa@gmail.com','hicham.malitoufa1@gmail.com','romane.boyer@ac-lyon.fr','herve.turlier@polytechnique.org','anne.douaire@gmail.com','simonneaujulien@gmail.com','christelle.paoli971@gmail.com','carla.moutier@ac-orleans-tours.fr','bruno.poncet531@orange.fr','marienoillet@hotmail.com','imane.yagoubi@hotmail.fr','laure.dlhaye@gmail.com','helene.lenoir1@laposte.net','nicolas.desmarez@gmail.com','laura.neichel1@gmail.com','megane.levan@ac-lyon.fr','mathilde.prudhomme@ac-clermont.fr','alheli@hotmail.fr','hugodeberranger@gmail.com','arnaud.antolinos@orange.fr','valentine_mercier@hotmail.com','ela1687@yahoo.fr','nouetolivier@free.fr','candice.grc@icloud.com','sebastienlaine@hotmail.com','elisa.poncet@ac-bordeaux.fr','eleonore.furlan200@gmail.com','christine.fourrier@gmail.com','romane.carriere@orange.fr','audrey.rignac1@ac-creteil.fr','elisemarie71@gmail.com','jose@mailo.com','helene.banegas1@ac-toulouse.fr','yvonnejouille25@free.fr','mathildeprudhomme@orange.fr','cynthiangaindiro@gmail.com','masclegilles@yahoo.fr','sophiepicavet@gmail.com','manueldematos1066@gmail.com','herrault.f@gmail.com','audreyrignac94@gmail.com','jclement.86@yahoo.com','gaelle.morois@gmail.com','julia.fauqueux@gmail.com','duplombmaxence@gmail.com','fabienne83@wanadoo.fr','agathebourretere@gmail.com','manonc3201@gmail.com','francois.herrault@ac-nantes.fr','anthony.santos1104@gmail.com','httc1@live.fr','fatiha.atmani@ac-noumea.nc','nadinerab@outlook.com','dominique.michau@gmail.com','matthieu.thibaudault@gmail.com','herve.turlier@cnrs.fr','thomas.leon-los-santos@ac-aix-marseille.fr','nigirard@unistra.fr','atranchant36@gmail.com'
    )
    AND usr.realm_id != 'master'
),

LatestEvents AS (
    -- 2. Récupération du DERNIER événement pertinent par utilisateur
    -- On évite de ramener tout l'historique pour le trier plus tard
    SELECT DISTINCT ON (evt.user_id)
        evt.user_id,
        evt.type,
        evt.client_id,
        evt.event_time
    FROM rcia.event_entity evt
    JOIN TargetUsers tu ON evt.user_id = tu.id
    WHERE evt.type IN ('LOGIN', 'UPDATE_PROFILE')
      AND TO_TIMESTAMP(evt.event_time / 1000)::date BETWEEN TO_TIMESTAMP('2000-03-01 00:00:00', 'YYYY-MM-DD HH24:MI:SS') 
                                                        AND TO_TIMESTAMP('2026-08-06 00:00:00', 'YYYY-MM-DD HH24:MI:SS')
    ORDER BY evt.user_id, evt.event_time DESC
),

UserActivity AS (
    -- 3. Comparaison Création vs Dernier Événement pour choisir la ligne finale
    SELECT
        tu.id,
        tu.email,
        tu.first_name,
        tu.last_name,
        -- Si l'événement est plus récent que la création, on prend sa date, sinon la création
        CASE 
            WHEN le.event_time IS NOT NULL AND le.event_time >= tu.created_timestamp THEN le.event_time
            ELSE tu.created_timestamp
        END as final_timestamp,
        -- Idem pour le type
        CASE 
            WHEN le.event_time IS NOT NULL AND le.event_time >= tu.created_timestamp THEN le.type
            ELSE 'CREATION'
        END as final_type,
        -- Idem pour le client
        CASE 
            WHEN le.event_time IS NOT NULL AND le.event_time >= tu.created_timestamp THEN le.client_id
            ELSE NULL
        END as final_client,
        -- Flag de validité date (pour respecter le filtre date d'origine sur la création)
        CASE
            WHEN le.event_time IS NOT NULL AND le.event_time >= tu.created_timestamp THEN 1
            WHEN TO_TIMESTAMP(tu.created_timestamp / 1000)::date BETWEEN TO_TIMESTAMP('2000-03-01 00:00:00', 'YYYY-MM-DD HH24:MI:SS') 
                                                                     AND TO_TIMESTAMP('2026-08-06 00:00:00', 'YYYY-MM-DD HH24:MI:SS') THEN 1
            ELSE 0
        END as is_valid
    FROM TargetUsers tu
    LEFT JOIN LatestEvents le ON tu.id = le.user_id
)

-- 4. Selection Finale et Jointure des Attributs (1 seule fois par user)
SELECT DISTINCT ON (ua.email)
    ua.id,
    COALESCE(attrealm.value, '') AS realm_id,
    COALESCE(attkpep.value, '') AS idkpep,
    ua.email,
    COALESCE(attother.value, '') AS email_other,
    COALESCE(attchannel.value, '') AS channel,
    ua.first_name,
    ua.last_name,
    COALESCE(attmiddle.value, '') AS middleName,
    COALESCE(attphone.value, '') AS phoneNumber,
    COALESCE(attbirth.value, '') AS birthDate,
    ua.final_type AS type,
    COALESCE(attorigin.value, '') AS originCreation,
    ua.final_client AS client,
    TO_TIMESTAMP(ua.final_timestamp/1000)::date AS date_evt,
    TO_TIMESTAMP(ua.final_timestamp/1000)::time AS heure_evt
FROM UserActivity ua
-- Jointures des attributs optimisée (se fait sur le résultat filtré)
LEFT JOIN rcia.user_attribute attrealm ON ua.id = attrealm.user_id AND attrealm.name='societe-codeGestionnaire'
LEFT JOIN rcia.user_attribute attkpep ON ua.id = attkpep.user_id AND attkpep.name = 'kpepId'
LEFT JOIN rcia.user_attribute attbirth ON ua.id = attbirth.user_id AND attbirth.name = 'birthDate'
LEFT JOIN rcia.user_attribute attother ON ua.id = attother.user_id AND attother.name = 'email_other'
LEFT JOIN rcia.user_attribute attchannel ON ua.id = attchannel.user_id AND attchannel.name = 'ActivationData-DeepLink-Chanel'
LEFT JOIN rcia.user_attribute attmiddle ON ua.id = attmiddle.user_id AND attmiddle.name = 'middleName'
LEFT JOIN rcia.user_attribute attphone ON ua.id = attphone.user_id AND attphone.name = 'phoneNumber'
LEFT JOIN rcia.user_attribute attorigin ON ua.id = attorigin.user_id AND attorigin.name = 'originCreation'
WHERE ua.is_valid = 1
ORDER BY ua.email, date_evt DESC;