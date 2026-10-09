BEGIN;
ALTER TABLE workflow_jobs DROP CONSTRAINT workflow_jobs_kind_check;
ALTER TABLE workflow_jobs ADD CONSTRAINT workflow_jobs_kind_check CHECK(kind IN ('load_fixture','extract_documents'));
CREATE TABLE document_sets (
 tenant_id text NOT NULL, id text NOT NULL, payload jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(tenant_id,id)
);
CREATE TABLE extraction_steps (
 tenant_id text NOT NULL, job_id text NOT NULL, step text NOT NULL, payload jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(tenant_id,job_id,step),
 FOREIGN KEY(tenant_id,job_id) REFERENCES workflow_jobs(tenant_id,id)
);
CREATE TABLE model_calls (
 tenant_id text NOT NULL, id text NOT NULL, job_id text NOT NULL, step text NOT NULL, metadata jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(tenant_id,id),
 FOREIGN KEY(tenant_id,job_id) REFERENCES workflow_jobs(tenant_id,id)
);
CREATE INDEX calls_by_job ON model_calls(tenant_id,job_id,step);
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['document_sets','extraction_steps','model_calls'] LOOP
  EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY',t);
  EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY',t);
  EXECUTE format('CREATE POLICY tenant_boundary ON %I USING (tenant_id=current_setting(''app.tenant_id'',true)) WITH CHECK (tenant_id=current_setting(''app.tenant_id'',true))',t);
  EXECUTE format('CREATE TRIGGER immutable_record BEFORE UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION reject_version_mutation()',t);
 END LOOP;
END $$;
INSERT INTO schema_migrations(version) VALUES ('0003');
COMMIT;
