/* BATCH 1 | GENERATED 2026-08-13 12:03:04.291612 | SOURCE: 31072026 | NB: 26 */
/* REQUETE RECHERCHE MIDDLE NAME / DATE NAISSANCE */
SELECT DISTINCT ON (first_name, middleName, birthDate)
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
      (attmiddle.value ILIKE 'AFFI' AND usr.first_name ILIKE 'CHAHINAZE')
      OR (attmiddle.value ILIKE 'BENZ' AND usr.first_name ILIKE 'SANDRA')
      OR (attmiddle.value ILIKE 'BESARAB' AND usr.first_name ILIKE 'CRISTINA')
      OR (attmiddle.value ILIKE 'CABANIE' AND usr.first_name ILIKE 'CHRISTOPHE')
      OR (attmiddle.value ILIKE 'CANDAU' AND usr.first_name ILIKE 'ALICIA')
      OR (attmiddle.value ILIKE 'CRESPIN' AND usr.first_name ILIKE 'ANTHONNE')
      OR (attmiddle.value ILIKE 'DAVID' AND usr.first_name ILIKE 'MIRA')
      OR (attmiddle.value ILIKE 'DELHOUM' AND usr.first_name ILIKE 'NOEMIE')
      OR (attmiddle.value ILIKE 'DELOTAL' AND usr.first_name ILIKE 'CINDY')
      OR (attmiddle.value ILIKE 'DOS SANTOS' AND usr.first_name ILIKE 'COLETTE')
      OR (attmiddle.value ILIKE 'FONTAINE' AND usr.first_name ILIKE 'TINA')
      OR (attmiddle.value ILIKE 'GUILLEMIN' AND usr.first_name ILIKE 'AURORE')
      OR (attmiddle.value ILIKE 'Gauzieux' AND usr.first_name ILIKE 'Prisca')
      OR (attmiddle.value ILIKE 'JAFARI' AND usr.first_name ILIKE 'REZA')
      OR (attmiddle.value ILIKE 'KARINDROU' AND usr.first_name ILIKE 'VESA DAVID')
      OR (attmiddle.value ILIKE 'LAVERGNE MARTENOT' AND usr.first_name ILIKE 'ANGELIQUE')
      OR (attmiddle.value ILIKE 'LEVET' AND usr.first_name ILIKE 'ANNE SOPHIE')
      OR (attmiddle.value ILIKE 'MEHIAOUI' AND usr.first_name ILIKE 'WIJDANE')
      OR (attmiddle.value ILIKE 'MEYER' AND usr.first_name ILIKE 'MARIE CHRISTINE')
      OR (attmiddle.value ILIKE 'MORENAS' AND usr.first_name ILIKE 'TESSA')
      OR (attmiddle.value ILIKE 'NDOLLOMASSALA' AND usr.first_name ILIKE 'EROLE')
      OR (attmiddle.value ILIKE 'PAIS CORDEIRO' AND usr.first_name ILIKE 'LUCELINE')
      OR (attmiddle.value ILIKE 'ROUSSEAU' AND usr.first_name ILIKE 'JUDITH')
      OR (attmiddle.value ILIKE 'THEVENIN' AND usr.first_name ILIKE 'ROMAIN')
      OR (attmiddle.value ILIKE 'TIPLICA' AND usr.first_name ILIKE 'NOAH')
      OR (attmiddle.value ILIKE 'TROCME' AND usr.first_name ILIKE 'BERTRAND')
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
      (attmiddle.value ILIKE 'AFFI' AND usr.first_name ILIKE 'CHAHINAZE')
      OR (attmiddle.value ILIKE 'BENZ' AND usr.first_name ILIKE 'SANDRA')
      OR (attmiddle.value ILIKE 'BESARAB' AND usr.first_name ILIKE 'CRISTINA')
      OR (attmiddle.value ILIKE 'CABANIE' AND usr.first_name ILIKE 'CHRISTOPHE')
      OR (attmiddle.value ILIKE 'CANDAU' AND usr.first_name ILIKE 'ALICIA')
      OR (attmiddle.value ILIKE 'CRESPIN' AND usr.first_name ILIKE 'ANTHONNE')
      OR (attmiddle.value ILIKE 'DAVID' AND usr.first_name ILIKE 'MIRA')
      OR (attmiddle.value ILIKE 'DELHOUM' AND usr.first_name ILIKE 'NOEMIE')
      OR (attmiddle.value ILIKE 'DELOTAL' AND usr.first_name ILIKE 'CINDY')
      OR (attmiddle.value ILIKE 'DOS SANTOS' AND usr.first_name ILIKE 'COLETTE')
      OR (attmiddle.value ILIKE 'FONTAINE' AND usr.first_name ILIKE 'TINA')
      OR (attmiddle.value ILIKE 'GUILLEMIN' AND usr.first_name ILIKE 'AURORE')
      OR (attmiddle.value ILIKE 'Gauzieux' AND usr.first_name ILIKE 'Prisca')
      OR (attmiddle.value ILIKE 'JAFARI' AND usr.first_name ILIKE 'REZA')
      OR (attmiddle.value ILIKE 'KARINDROU' AND usr.first_name ILIKE 'VESA DAVID')
      OR (attmiddle.value ILIKE 'LAVERGNE MARTENOT' AND usr.first_name ILIKE 'ANGELIQUE')
      OR (attmiddle.value ILIKE 'LEVET' AND usr.first_name ILIKE 'ANNE SOPHIE')
      OR (attmiddle.value ILIKE 'MEHIAOUI' AND usr.first_name ILIKE 'WIJDANE')
      OR (attmiddle.value ILIKE 'MEYER' AND usr.first_name ILIKE 'MARIE CHRISTINE')
      OR (attmiddle.value ILIKE 'MORENAS' AND usr.first_name ILIKE 'TESSA')
      OR (attmiddle.value ILIKE 'NDOLLOMASSALA' AND usr.first_name ILIKE 'EROLE')
      OR (attmiddle.value ILIKE 'PAIS CORDEIRO' AND usr.first_name ILIKE 'LUCELINE')
      OR (attmiddle.value ILIKE 'ROUSSEAU' AND usr.first_name ILIKE 'JUDITH')
      OR (attmiddle.value ILIKE 'THEVENIN' AND usr.first_name ILIKE 'ROMAIN')
      OR (attmiddle.value ILIKE 'TIPLICA' AND usr.first_name ILIKE 'NOAH')
      OR (attmiddle.value ILIKE 'TROCME' AND usr.first_name ILIKE 'BERTRAND')
    )
) AS unioned
ORDER BY first_name, middleName, birthDate, date_evt DESC;