-- Re-encrypt every personal-data column under a new data key (A6 follow-up). One call, one
-- transaction: registry.beneficial_owner_enc and entities.name_enc are the only encrypted
-- columns (003). Run by services/api/rekey.py, which then stores the new key in the secret.
CREATE OR REPLACE FUNCTION rekey_personal_data(old_key text, new_key text)
RETURNS TABLE(registry_rows bigint, entity_rows bigint)
LANGUAGE plpgsql AS $$
DECLARE r bigint; e bigint;
BEGIN
  UPDATE registry
     SET beneficial_owner_enc = pgp_sym_encrypt(pgp_sym_decrypt(beneficial_owner_enc, old_key), new_key)
   WHERE beneficial_owner_enc IS NOT NULL;
  GET DIAGNOSTICS r = ROW_COUNT;
  UPDATE entities
     SET name_enc = pgp_sym_encrypt(pgp_sym_decrypt(name_enc, old_key), new_key)
   WHERE name_enc IS NOT NULL;
  GET DIAGNOSTICS e = ROW_COUNT;
  RETURN QUERY SELECT r, e;
END $$;
