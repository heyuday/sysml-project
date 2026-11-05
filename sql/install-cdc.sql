-- Install once on a freshly loaded, dedicated benchmark database.
CREATE SCHEMA sysml;
CREATE EXTENSION IF NOT EXISTS hstore WITH SCHEMA public;
CREATE TABLE sysml.clock (singleton boolean PRIMARY KEY CHECK(singleton), v bigint NOT NULL);
INSERT INTO sysml.clock VALUES(true,0);
CREATE TABLE sysml.events (
 v bigint NOT NULL, seq bigint GENERATED ALWAYS AS IDENTITY, tx bigint NOT NULL,
 rel text NOT NULL, op text NOT NULL, cols text[] NOT NULL,
 before_image jsonb, after_image jsonb, emitted timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(v,seq)
);
CREATE TABLE sysml.tracked(rel text PRIMARY KEY);
CREATE FUNCTION sysml.fence() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 -- Transactional row lock serializes writers, including out-of-broker clients.
 UPDATE sysml.clock SET v=v+1;
 IF TG_OP='TRUNCATE' THEN
   INSERT INTO sysml.events(v,tx,rel,op,cols) SELECT v,txid_current(),TG_TABLE_NAME,TG_OP,'{}' FROM sysml.clock;
 END IF;
 RETURN NULL;
END $$;
CREATE FUNCTION sysml.capture() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE o jsonb; n jsonb; cs text[]; old_text jsonb; new_text jsonb;
BEGIN
 IF TG_OP<>'INSERT' THEN o=to_jsonb(OLD); old_text=public.hstore_to_jsonb(public.hstore(OLD)); END IF;
 IF TG_OP<>'DELETE' THEN n=to_jsonb(NEW); new_text=public.hstore_to_jsonb(public.hstore(NEW)); END IF;
 -- Compare PostgreSQL field text. JSONB equality alone erases numeric scale
 -- and whitespace in json columns, both observable in the result protocol.
 SELECT ARRAY(SELECT key FROM jsonb_object_keys(COALESCE(new_text,old_text)) key
   WHERE (old_text->key) IS DISTINCT FROM (new_text->key)) INTO cs;
 INSERT INTO sysml.events(v,tx,rel,op,cols,before_image,after_image)
 SELECT v,txid_current(),TG_TABLE_NAME,TG_OP,cs,o,n FROM sysml.clock;
 RETURN NULL;
END $$;
DO $$
DECLARE r record;
BEGIN
 FOR r IN SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
 WHERE n.nspname='public' AND c.relkind='r' LOOP
   EXECUTE format('CREATE TRIGGER sysml_fence BEFORE INSERT OR UPDATE OR DELETE OR TRUNCATE ON public.%I FOR EACH STATEMENT EXECUTE FUNCTION sysml.fence()',r.relname);
   EXECUTE format('CREATE TRIGGER sysml_change AFTER INSERT OR UPDATE OR DELETE ON public.%I FOR EACH ROW EXECUTE FUNCTION sysml.capture()',r.relname);
   INSERT INTO sysml.tracked VALUES(r.relname);
 END LOOP;
 IF NOT EXISTS(SELECT FROM sysml.tracked) THEN RAISE EXCEPTION 'Load benchmark tables before installing CDC'; END IF;
END $$;
