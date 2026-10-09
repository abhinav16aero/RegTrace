BEGIN;
CREATE TABLE presentation_records (
 tenant_id text NOT NULL, id text NOT NULL, kind text NOT NULL,
 reference text NOT NULL, title text NOT NULL, curated boolean NOT NULL DEFAULT false,
 provenance jsonb NOT NULL DEFAULT '{}', PRIMARY KEY(tenant_id,id), UNIQUE(tenant_id,reference)
);
CREATE TABLE presentation_workspaces (
 tenant_id text PRIMARY KEY, name text NOT NULL, is_demo boolean NOT NULL DEFAULT false,
 manifest jsonb NOT NULL DEFAULT '{}'
);
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['presentation_records','presentation_workspaces'] LOOP
  EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY',t);
  EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY',t);
  EXECUTE format('CREATE POLICY tenant_boundary ON %I USING (tenant_id=current_setting(''app.tenant_id'',true)) WITH CHECK (tenant_id=current_setting(''app.tenant_id'',true))',t);
 END LOOP;
END $$;
ALTER TABLE app_users ADD COLUMN display_name text;
INSERT INTO schema_migrations(version) VALUES ('0010');
COMMIT;
