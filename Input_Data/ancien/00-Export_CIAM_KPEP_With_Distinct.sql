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
          __LISTE_IDS__
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
                                                        AND TO_TIMESTAMP('2025-11-30 00:00:00', 'YYYY-MM-DD HH24:MI:SS')
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
                                                                     AND TO_TIMESTAMP('2025-11-30 00:00:00', 'YYYY-MM-DD HH24:MI:SS') THEN 1
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