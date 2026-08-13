/* BATCH 1 | GENERATED 2026-08-13 12:03:04.277608 | SOURCE: 31072026 | NB: 26 */
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
    AND TO_TIMESTAMP(usr.created_timestamp / 1000)::date BETWEEN TO_TIMESTAMP('2000-03-01', 'YYYY-MM-DD') AND TO_TIMESTAMP('2026-08-13', 'YYYY-MM-DD')
    AND (
      (usr.last_name ILIKE 'AFFI' AND att2.value = '1989-08-10')
      OR (usr.last_name ILIKE 'BENZ' AND att2.value = '1986-04-16')
      OR (usr.last_name ILIKE 'BESARAB' AND att2.value = '1991-02-20')
      OR (usr.last_name ILIKE 'CABANIE' AND att2.value = '1970-02-26')
      OR (usr.last_name ILIKE 'CANDAU' AND att2.value = '2005-03-09')
      OR (usr.last_name ILIKE 'CRESPIN' AND att2.value = '2000-08-01')
      OR (usr.last_name ILIKE 'DAVID' AND att2.value = '1993-04-29')
      OR (usr.last_name ILIKE 'DELHOUM' AND att2.value = '2004-09-14')
      OR (usr.last_name ILIKE 'DELOTAL' AND att2.value = '1981-01-20')
      OR (usr.last_name ILIKE 'DOS SANTOS' AND att2.value = '1942-04-08')
      OR (usr.last_name ILIKE 'FONTAINE' AND att2.value = '2006-11-20')
      OR (usr.last_name ILIKE 'GUILLEMIN' AND att2.value = '1984-12-19')
      OR (usr.last_name ILIKE 'Gauzieux' AND att2.value = '1993-08-31')
      OR (usr.last_name ILIKE 'JAFARI' AND att2.value = '1996-07-21')
      OR (usr.last_name ILIKE 'KARINDROU' AND att2.value = '1993-12-03')
      OR (usr.last_name ILIKE 'LAVERGNE MARTENOT' AND att2.value = '1981-05-18')
      OR (usr.last_name ILIKE 'LEVET' AND att2.value = '1984-03-14')
      OR (usr.last_name ILIKE 'MEHIAOUI' AND att2.value = '2007-02-12')
      OR (usr.last_name ILIKE 'MEYER' AND att2.value = '1956-03-07')
      OR (usr.last_name ILIKE 'MORENAS' AND att2.value = '2006-07-28')
      OR (usr.last_name ILIKE 'NDOLLOMASSALA' AND att2.value = '1986-06-10')
      OR (usr.last_name ILIKE 'PAIS CORDEIRO' AND att2.value = '1983-02-20')
      OR (usr.last_name ILIKE 'ROUSSEAU' AND att2.value = '1995-09-26')
      OR (usr.last_name ILIKE 'THEVENIN' AND att2.value = '1983-08-31')
      OR (usr.last_name ILIKE 'TIPLICA' AND att2.value = '2004-06-29')
      OR (usr.last_name ILIKE 'TROCME' AND att2.value = '1955-11-07')
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
    AND TO_TIMESTAMP(evt.event_time / 1000)::date BETWEEN TO_TIMESTAMP('2000-03-01', 'YYYY-MM-DD') AND TO_TIMESTAMP('2026-08-13', 'YYYY-MM-DD')
    AND (
      (usr.last_name ILIKE 'AFFI' AND att2.value = '1989-08-10')
      OR (usr.last_name ILIKE 'BENZ' AND att2.value = '1986-04-16')
      OR (usr.last_name ILIKE 'BESARAB' AND att2.value = '1991-02-20')
      OR (usr.last_name ILIKE 'CABANIE' AND att2.value = '1970-02-26')
      OR (usr.last_name ILIKE 'CANDAU' AND att2.value = '2005-03-09')
      OR (usr.last_name ILIKE 'CRESPIN' AND att2.value = '2000-08-01')
      OR (usr.last_name ILIKE 'DAVID' AND att2.value = '1993-04-29')
      OR (usr.last_name ILIKE 'DELHOUM' AND att2.value = '2004-09-14')
      OR (usr.last_name ILIKE 'DELOTAL' AND att2.value = '1981-01-20')
      OR (usr.last_name ILIKE 'DOS SANTOS' AND att2.value = '1942-04-08')
      OR (usr.last_name ILIKE 'FONTAINE' AND att2.value = '2006-11-20')
      OR (usr.last_name ILIKE 'GUILLEMIN' AND att2.value = '1984-12-19')
      OR (usr.last_name ILIKE 'Gauzieux' AND att2.value = '1993-08-31')
      OR (usr.last_name ILIKE 'JAFARI' AND att2.value = '1996-07-21')
      OR (usr.last_name ILIKE 'KARINDROU' AND att2.value = '1993-12-03')
      OR (usr.last_name ILIKE 'LAVERGNE MARTENOT' AND att2.value = '1981-05-18')
      OR (usr.last_name ILIKE 'LEVET' AND att2.value = '1984-03-14')
      OR (usr.last_name ILIKE 'MEHIAOUI' AND att2.value = '2007-02-12')
      OR (usr.last_name ILIKE 'MEYER' AND att2.value = '1956-03-07')
      OR (usr.last_name ILIKE 'MORENAS' AND att2.value = '2006-07-28')
      OR (usr.last_name ILIKE 'NDOLLOMASSALA' AND att2.value = '1986-06-10')
      OR (usr.last_name ILIKE 'PAIS CORDEIRO' AND att2.value = '1983-02-20')
      OR (usr.last_name ILIKE 'ROUSSEAU' AND att2.value = '1995-09-26')
      OR (usr.last_name ILIKE 'THEVENIN' AND att2.value = '1983-08-31')
      OR (usr.last_name ILIKE 'TIPLICA' AND att2.value = '2004-06-29')
      OR (usr.last_name ILIKE 'TROCME' AND att2.value = '1955-11-07')
    )
) AS unioned
ORDER BY first_name, last_name, birthDate, date_evt DESC;