/* BATCH 1 | GENERATED 2026-08-13 12:03:04.282611 | SOURCE: 31072026 | NB: 26 */
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
      (usr.last_name ILIKE 'AFFI' AND usr.first_name ILIKE 'CHAHINAZE')
      OR (usr.last_name ILIKE 'BENZ' AND usr.first_name ILIKE 'SANDRA')
      OR (usr.last_name ILIKE 'BESARAB' AND usr.first_name ILIKE 'CRISTINA')
      OR (usr.last_name ILIKE 'CABANIE' AND usr.first_name ILIKE 'CHRISTOPHE')
      OR (usr.last_name ILIKE 'CANDAU' AND usr.first_name ILIKE 'ALICIA')
      OR (usr.last_name ILIKE 'CRESPIN' AND usr.first_name ILIKE 'ANTHONNE')
      OR (usr.last_name ILIKE 'DAVID' AND usr.first_name ILIKE 'MIRA')
      OR (usr.last_name ILIKE 'DELHOUM' AND usr.first_name ILIKE 'NOEMIE')
      OR (usr.last_name ILIKE 'DELOTAL' AND usr.first_name ILIKE 'CINDY')
      OR (usr.last_name ILIKE 'DOS SANTOS' AND usr.first_name ILIKE 'COLETTE')
      OR (usr.last_name ILIKE 'FONTAINE' AND usr.first_name ILIKE 'TINA')
      OR (usr.last_name ILIKE 'GUILLEMIN' AND usr.first_name ILIKE 'AURORE')
      OR (usr.last_name ILIKE 'Gauzieux' AND usr.first_name ILIKE 'Prisca')
      OR (usr.last_name ILIKE 'JAFARI' AND usr.first_name ILIKE 'REZA')
      OR (usr.last_name ILIKE 'KARINDROU' AND usr.first_name ILIKE 'VESA DAVID')
      OR (usr.last_name ILIKE 'LAVERGNE MARTENOT' AND usr.first_name ILIKE 'ANGELIQUE')
      OR (usr.last_name ILIKE 'LEVET' AND usr.first_name ILIKE 'ANNE SOPHIE')
      OR (usr.last_name ILIKE 'MEHIAOUI' AND usr.first_name ILIKE 'WIJDANE')
      OR (usr.last_name ILIKE 'MEYER' AND usr.first_name ILIKE 'MARIE CHRISTINE')
      OR (usr.last_name ILIKE 'MORENAS' AND usr.first_name ILIKE 'TESSA')
      OR (usr.last_name ILIKE 'NDOLLOMASSALA' AND usr.first_name ILIKE 'EROLE')
      OR (usr.last_name ILIKE 'PAIS CORDEIRO' AND usr.first_name ILIKE 'LUCELINE')
      OR (usr.last_name ILIKE 'ROUSSEAU' AND usr.first_name ILIKE 'JUDITH')
      OR (usr.last_name ILIKE 'THEVENIN' AND usr.first_name ILIKE 'ROMAIN')
      OR (usr.last_name ILIKE 'TIPLICA' AND usr.first_name ILIKE 'NOAH')
      OR (usr.last_name ILIKE 'TROCME' AND usr.first_name ILIKE 'BERTRAND')
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
      (usr.last_name ILIKE 'AFFI' AND usr.first_name ILIKE 'CHAHINAZE')
      OR (usr.last_name ILIKE 'BENZ' AND usr.first_name ILIKE 'SANDRA')
      OR (usr.last_name ILIKE 'BESARAB' AND usr.first_name ILIKE 'CRISTINA')
      OR (usr.last_name ILIKE 'CABANIE' AND usr.first_name ILIKE 'CHRISTOPHE')
      OR (usr.last_name ILIKE 'CANDAU' AND usr.first_name ILIKE 'ALICIA')
      OR (usr.last_name ILIKE 'CRESPIN' AND usr.first_name ILIKE 'ANTHONNE')
      OR (usr.last_name ILIKE 'DAVID' AND usr.first_name ILIKE 'MIRA')
      OR (usr.last_name ILIKE 'DELHOUM' AND usr.first_name ILIKE 'NOEMIE')
      OR (usr.last_name ILIKE 'DELOTAL' AND usr.first_name ILIKE 'CINDY')
      OR (usr.last_name ILIKE 'DOS SANTOS' AND usr.first_name ILIKE 'COLETTE')
      OR (usr.last_name ILIKE 'FONTAINE' AND usr.first_name ILIKE 'TINA')
      OR (usr.last_name ILIKE 'GUILLEMIN' AND usr.first_name ILIKE 'AURORE')
      OR (usr.last_name ILIKE 'Gauzieux' AND usr.first_name ILIKE 'Prisca')
      OR (usr.last_name ILIKE 'JAFARI' AND usr.first_name ILIKE 'REZA')
      OR (usr.last_name ILIKE 'KARINDROU' AND usr.first_name ILIKE 'VESA DAVID')
      OR (usr.last_name ILIKE 'LAVERGNE MARTENOT' AND usr.first_name ILIKE 'ANGELIQUE')
      OR (usr.last_name ILIKE 'LEVET' AND usr.first_name ILIKE 'ANNE SOPHIE')
      OR (usr.last_name ILIKE 'MEHIAOUI' AND usr.first_name ILIKE 'WIJDANE')
      OR (usr.last_name ILIKE 'MEYER' AND usr.first_name ILIKE 'MARIE CHRISTINE')
      OR (usr.last_name ILIKE 'MORENAS' AND usr.first_name ILIKE 'TESSA')
      OR (usr.last_name ILIKE 'NDOLLOMASSALA' AND usr.first_name ILIKE 'EROLE')
      OR (usr.last_name ILIKE 'PAIS CORDEIRO' AND usr.first_name ILIKE 'LUCELINE')
      OR (usr.last_name ILIKE 'ROUSSEAU' AND usr.first_name ILIKE 'JUDITH')
      OR (usr.last_name ILIKE 'THEVENIN' AND usr.first_name ILIKE 'ROMAIN')
      OR (usr.last_name ILIKE 'TIPLICA' AND usr.first_name ILIKE 'NOAH')
      OR (usr.last_name ILIKE 'TROCME' AND usr.first_name ILIKE 'BERTRAND')
    )
) AS unioned
ORDER BY first_name, last_name, birthDate, date_evt DESC;