/* BATCH 1 | GENERATED 2026-08-03 11:54:26.860894 | SOURCE: 16042026 | NB: 7 */
/* REQUETE RECHERCHE NOM / DATE NAISSANCE */
SELECT DISTINCT ON (first_name, last_name, birthDate)
  id,
  realm_id,
  idkpep,
  email,
  email_other,
  channel,
  first_name,
  last_name,
  middleName,
  phoneNumber,
  birthDate,
  type,
  originCreation,
  client,
  date_evt,
  heure_evt
FROM (
  -- CREATION
  SELECT
    usr.id,
    COALESCE(attrealm.value, '') AS realm_id,
    COALESCE(att.value, '') AS idkpep,
    usr.email,
    COALESCE(attother.value, '') AS email_other,
    COALESCE(attchannel.value, '') AS channel,
    usr.first_name,
    usr.last_name,
    COALESCE(attmiddle.value, '') AS middleName,
    COALESCE(attphone.value, '') AS phoneNumber,
    COALESCE(att2.value, '') AS birthDate,
    'CREATION' AS type,
    COALESCE(attorigin.value, '') AS originCreation,
    NULL AS client,
    TO_TIMESTAMP(usr.created_timestamp/1000)::date AS date_evt,
    TO_TIMESTAMP(usr.created_timestamp/1000)::time AS heure_evt
  FROM rcia.user_entity usr
  LEFT JOIN rcia.user_attribute attrealm ON usr.id = attrealm.user_id AND attrealm.name='societe-codeGestionnaire'
  LEFT JOIN rcia.user_attribute att ON usr.id = att.user_id AND att.name = 'kpepId'
  LEFT JOIN rcia.user_attribute att2 ON usr.id = att2.user_id AND att2.name = 'birthDate'
  LEFT JOIN rcia.user_attribute attother ON usr.id = attother.user_id AND attother.name = 'email_other'
  LEFT JOIN rcia.user_attribute attchannel ON usr.id = attchannel.user_id AND attchannel.name = 'ActivationData-DeepLink-Chanel'
  LEFT JOIN rcia.user_attribute attmiddle ON usr.id = attmiddle.user_id AND attmiddle.name = 'middleName'
  LEFT JOIN rcia.user_attribute attphone ON usr.id = attphone.user_id AND attphone.name = 'phoneNumber'
  LEFT JOIN rcia.user_attribute attorigin ON usr.id = attorigin.user_id AND attorigin.name = 'originCreation'
  WHERE usr.realm_id != 'master'
    AND TO_TIMESTAMP(usr.created_timestamp / 1000)::date BETWEEN TO_TIMESTAMP('2000-03-01', 'YYYY-MM-DD') AND TO_TIMESTAMP('2026-08-03', 'YYYY-MM-DD')
    AND (
      (usr.last_name ILIKE 'BAUTRAIT' AND att2.value = '1979-09-27')
      OR (usr.last_name ILIKE 'CARDINEAUD' AND att2.value = '1995-04-01')
      OR (usr.last_name ILIKE 'DESCHAMPS' AND att2.value = '1978-10-03')
      OR (usr.last_name ILIKE 'LEPERRE' AND att2.value = '1954-06-29')
      OR (usr.last_name ILIKE 'MOKADEM' AND att2.value = '2002-02-22')
      OR (usr.last_name ILIKE 'MOKCITE' AND att2.value = '1960-01-01')
      OR (usr.last_name ILIKE 'VALDIZAN' AND att2.value = '1983-03-22')
    )

  UNION ALL

  -- LOGIN / UPDATE_PROFILE
  SELECT
    usr.id,
    COALESCE(attrealm.value, '') AS realm_id,
    COALESCE(att.value, '') AS idkpep,
    usr.email,
    COALESCE(attother.value, '') AS email_other,
    COALESCE(attchannel.value, '') AS channel,
    usr.first_name,
    usr.last_name,
    COALESCE(attmiddle.value, '') AS middleName,
    COALESCE(attphone.value, '') AS phoneNumber,
    COALESCE(att2.value, '') AS birthDate,
    evt.type AS type,
    COALESCE(attorigin.value, '') AS originCreation,
    evt.client_id AS client,
    TO_TIMESTAMP(evt.event_time/1000)::date AS date_evt,
    TO_TIMESTAMP(evt.event_time/1000)::time AS heure_evt
  FROM rcia.user_entity usr
  LEFT JOIN rcia.user_attribute attrealm ON usr.id = attrealm.user_id AND attrealm.name='societe-codeGestionnaire'
  LEFT JOIN rcia.user_attribute att ON usr.id = att.user_id AND att.name = 'kpepId'
  LEFT JOIN rcia.user_attribute att2 ON usr.id = att2.user_id AND att2.name = 'birthDate'
  LEFT JOIN rcia.event_entity evt ON evt.user_id = usr.id
  LEFT JOIN rcia.user_attribute attother ON usr.id = attother.user_id AND attother.name = 'email_other'
  LEFT JOIN rcia.user_attribute attchannel ON usr.id = attchannel.user_id AND attchannel.name = 'ActivationData-DeepLink-Chanel'
  LEFT JOIN rcia.user_attribute attmiddle ON usr.id = attmiddle.user_id AND attmiddle.name = 'middleName'
  LEFT JOIN rcia.user_attribute attphone ON usr.id = attphone.user_id AND attphone.name = 'phoneNumber'
  LEFT JOIN rcia.user_attribute attorigin ON usr.id = attorigin.user_id AND attorigin.name = 'originCreation'
  WHERE usr.realm_id != 'master'
    AND evt.type IN ('LOGIN', 'UPDATE_PROFILE')
    AND TO_TIMESTAMP(evt.event_time / 1000)::date BETWEEN TO_TIMESTAMP('2000-03-01', 'YYYY-MM-DD') AND TO_TIMESTAMP('2026-08-03', 'YYYY-MM-DD')
    AND (
      (usr.last_name ILIKE 'BAUTRAIT' AND att2.value = '1979-09-27')
      OR (usr.last_name ILIKE 'CARDINEAUD' AND att2.value = '1995-04-01')
      OR (usr.last_name ILIKE 'DESCHAMPS' AND att2.value = '1978-10-03')
      OR (usr.last_name ILIKE 'LEPERRE' AND att2.value = '1954-06-29')
      OR (usr.last_name ILIKE 'MOKADEM' AND att2.value = '2002-02-22')
      OR (usr.last_name ILIKE 'MOKCITE' AND att2.value = '1960-01-01')
      OR (usr.last_name ILIKE 'VALDIZAN' AND att2.value = '1983-03-22')
    )
) AS unioned
ORDER BY first_name, last_name, birthDate, date_evt DESC;