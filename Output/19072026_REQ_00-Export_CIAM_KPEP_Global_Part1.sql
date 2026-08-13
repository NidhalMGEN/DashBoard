/* BATCH 1 | GENERATED 2026-08-06 15:27:22.370018 | SOURCE: 19072026 | NB: 94 */
/* OPTIMIZED QUERY FOR KPEP
   Logic: Filter Users by KPEP -> Find Latest Event -> Join Attributes ONCE
*/

WITH TargetUsers AS (
    -- 1. Récupération des utilisateurs cibles via KPEP ID (Filtre primaire)
    -- On identifie d'abord les users qui possèdent un des KPEP recherchés
    SELECT
        usr.id,
        usr.email,
        usr.first_name,
        usr.last_name,
        usr.created_timestamp,
        att_kpep.value as kpep_searched -- On garde le KPEP qui a matché pour la ref
    FROM rcia.user_entity usr
    JOIN rcia.user_attribute att_kpep ON usr.id = att_kpep.user_id 
    WHERE att_kpep.name = 'kpepId'
      AND att_kpep.value IN (
          'KPEP00001124035420','KPEP00000526962915','KPEP00001064360911','KPEP00001043702222','KPEP00001123633533','KPEP00001123642800','KPEP00000114911739','KPEP00001043275539','KPEP00000300564036','KPEP00000115459515','KPEP00000422324317','KPEP00001038746121','KPEP00000952119808','KPEP00000959616404','KPEP00001051173618','KPEP00001106178030','KPEP00001065547129','KPEP00000997293030','KPEP00001124038105','KPEP00001124039519','KPEP00000298540907','KPEP00000268552705','KPEP00001123623921','KPEP00000751328127','KPEP00001124080913','KPEP00001087805008','KPEP00000151839612','KPEP00001124039317','KPEP00001123394638','KPEP00001123611826','KPEP00001073250131','KPEP00000267248020','KPEP00001044216339','KPEP00000668772321','KPEP00001124237424','KPEP00001097823523','KPEP00001124235303','KPEP00001124044426','KPEP00001068370741','KPEP00000087659816','KPEP00000545790000','KPEP00000178763935','KPEP00000049052709','KPEP00001123633129','KPEP00001044076325','KPEP00001042958721','KPEP00001013286529','KPEP00001124045941','KPEP00000137176806','KPEP00000194832933','KPEP00000034351438','KPEP00001124040529','KPEP00001123614107','KPEP00001124234941','KPEP00001113345208','KPEP00001038947014','KPEP00000666583010','KPEP00001100409208','KPEP00000591743614','KPEP00000866773729','KPEP00001124338426','KPEP00000988650339','KPEP00000842751000','KPEP00000546129636','KPEP00001088037321','KPEP00000348726505','KPEP00001044118628','KPEP00000759730105','KPEP00001123640418','KPEP00001029938317','KPEP00001124335539','KPEP00001124235505','KPEP00000457754406','KPEP00000523678818','KPEP00001124040024','KPEP00001037426715','KPEP00001106259909','KPEP00001124045133','KPEP00000189327036','KPEP00001098142824','KPEP00000272156002','KPEP00000815954739','KPEP00001040379214','KPEP00000488813204','KPEP00001123649929','KPEP00000970440915','KPEP00000917438127','KPEP00001023210422','KPEP00000460541935','KPEP00001070848539','KPEP00000980795531','KPEP00001123626505','KPEP00000408657416','KPEP00001065177630'
      )
      AND usr.realm_id != 'master'
),

LatestEvents AS (
    -- 2. Récupération du DERNIER événement pertinent par utilisateur identifié
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
    -- 3. Comparaison Création vs Dernier Événement pour choisir la ligne maître
    SELECT
        tu.id,
        tu.email,
        tu.first_name,
        tu.last_name,
        tu.kpep_searched,
        -- Timestamp final
        CASE 
            WHEN le.event_time IS NOT NULL AND le.event_time >= tu.created_timestamp THEN le.event_time
            ELSE tu.created_timestamp
        END as final_timestamp,
        -- Type final
        CASE 
            WHEN le.event_time IS NOT NULL AND le.event_time >= tu.created_timestamp THEN le.type
            ELSE 'CREATION'
        END as final_type,
        -- Client final
        CASE 
            WHEN le.event_time IS NOT NULL AND le.event_time >= tu.created_timestamp THEN le.client_id
            ELSE NULL
        END as final_client,
        -- Filtre de validité date global
        CASE
            WHEN le.event_time IS NOT NULL AND le.event_time >= tu.created_timestamp THEN 1
            WHEN TO_TIMESTAMP(tu.created_timestamp / 1000)::date BETWEEN TO_TIMESTAMP('2000-03-01 00:00:00', 'YYYY-MM-DD HH24:MI:SS') 
                                                                     AND TO_TIMESTAMP('2026-08-06 00:00:00', 'YYYY-MM-DD HH24:MI:SS') THEN 1
            ELSE 0
        END as is_valid
    FROM TargetUsers tu
    LEFT JOIN LatestEvents le ON tu.id = le.user_id
)

-- 4. Selection Finale et Jointure des Attributs (1 seule fois par KPEP trouvé)
SELECT DISTINCT ON (ua.kpep_searched)
    ua.id,
    COALESCE(attrealm.value, '') AS realm_id,
    ua.kpep_searched AS idkpep, -- On utilise la valeur du filtre initial
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
-- Jointures des attributs optimisées (uniquement sur les lignes gagnantes)
LEFT JOIN rcia.user_attribute attrealm ON ua.id = attrealm.user_id AND attrealm.name='societe-codeGestionnaire'
LEFT JOIN rcia.user_attribute attbirth ON ua.id = attbirth.user_id AND attbirth.name = 'birthDate'
LEFT JOIN rcia.user_attribute attother ON ua.id = attother.user_id AND attother.name = 'email_other'
LEFT JOIN rcia.user_attribute attchannel ON ua.id = attchannel.user_id AND attchannel.name = 'ActivationData-DeepLink-Chanel'
LEFT JOIN rcia.user_attribute attmiddle ON ua.id = attmiddle.user_id AND attmiddle.name = 'middleName'
LEFT JOIN rcia.user_attribute attphone ON ua.id = attphone.user_id AND attphone.name = 'phoneNumber'
LEFT JOIN rcia.user_attribute attorigin ON ua.id = attorigin.user_id AND attorigin.name = 'originCreation'
WHERE ua.is_valid = 1
ORDER BY ua.kpep_searched, date_evt DESC;