BEGIN;
CREATE TABLE app_users (
 id text PRIMARY KEY, tenant_id text NOT NULL, username text UNIQUE NOT NULL,
 password_hash text NOT NULL, role text NOT NULL CHECK(role IN ('analyst','reviewer')),
 disabled boolean NOT NULL DEFAULT false, failed_attempts integer NOT NULL DEFAULT 0,
 locked_until timestamptz
);
CREATE TABLE sessions (
 token_hash text PRIMARY KEY, user_id text NOT NULL REFERENCES app_users(id),
 csrf_token text NOT NULL, expires_at timestamptz NOT NULL
);
CREATE TABLE workflow_jobs (
 tenant_id text NOT NULL, id text NOT NULL, idempotency_key text NOT NULL,
 kind text NOT NULL CHECK(kind='load_fixture'),
 status text NOT NULL CHECK(status IN ('queued','running','awaiting_review','completed','failed','cancelled')),
 input_hash text NOT NULL, configuration_hash text NOT NULL, input_payload jsonb NOT NULL,
 requested_by text NOT NULL, cancel_requested boolean NOT NULL DEFAULT false,
 attempts integer NOT NULL DEFAULT 0, lease_token text, lease_expires_at timestamptz,
 output_ids jsonb NOT NULL DEFAULT '[]', error_code text,
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,id), UNIQUE(tenant_id,idempotency_key)
);
CREATE INDEX runnable_jobs ON workflow_jobs(tenant_id,status,created_at);
CREATE TABLE review_events (
 tenant_id text NOT NULL, id text NOT NULL, assessment_id text NOT NULL,
 revision integer NOT NULL CHECK(revision>0), decision text NOT NULL
 CHECK(decision IN ('approved','rejected','changes_requested')), note text NOT NULL CHECK(length(trim(note))>0),
 actor_id text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,id), UNIQUE(tenant_id,assessment_id,revision),
 FOREIGN KEY(tenant_id,assessment_id) REFERENCES entity_versions(tenant_id,id)
);
CREATE TABLE audit_events (
 tenant_id text NOT NULL, id text NOT NULL, actor_id text NOT NULL,
 action text NOT NULL, resource_id text NOT NULL, details jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(tenant_id,id)
);
CREATE INDEX audit_resource ON audit_events(tenant_id,resource_id,created_at);
CREATE TRIGGER immutable_review BEFORE UPDATE OR DELETE ON review_events
 FOR EACH ROW EXECUTE FUNCTION reject_version_mutation();
CREATE TRIGGER immutable_audit BEFORE UPDATE OR DELETE ON audit_events
 FOR EACH ROW EXECUTE FUNCTION reject_version_mutation();
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['workflow_jobs','review_events','audit_events'] LOOP
  EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY',t);
  EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY',t);
  EXECUTE format('CREATE POLICY tenant_boundary ON %I USING (tenant_id=current_setting(''app.tenant_id'',true)) WITH CHECK (tenant_id=current_setting(''app.tenant_id'',true))',t);
 END LOOP;
END $$;
INSERT INTO schema_migrations(version) VALUES ('0002');
COMMIT;
