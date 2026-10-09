BEGIN;
ALTER TABLE workflow_jobs DROP CONSTRAINT workflow_jobs_kind_check;
ALTER TABLE workflow_jobs ADD CONSTRAINT workflow_jobs_kind_check CHECK(kind IN ('load_fixture','extract_documents','assess_documents','monitor_regulation','retest_remediation'));
CREATE TABLE remediation_events (
 tenant_id text NOT NULL, remediation_id text NOT NULL, revision integer NOT NULL CHECK(revision>0),
 action text NOT NULL, actor_id text NOT NULL, note text NOT NULL CHECK(length(trim(note))>0),
 state jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,remediation_id,revision),
 FOREIGN KEY(tenant_id,remediation_id) REFERENCES entity_versions(tenant_id,id)
);
CREATE TABLE remediation_retests (
 tenant_id text NOT NULL, job_id text NOT NULL, remediation_id text NOT NULL, payload jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(tenant_id,job_id),
 FOREIGN KEY(tenant_id,job_id) REFERENCES workflow_jobs(tenant_id,id),
 FOREIGN KEY(tenant_id,remediation_id) REFERENCES entity_versions(tenant_id,id)
);
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['remediation_events','remediation_retests'] LOOP
  EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY',t);
  EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY',t);
  EXECUTE format('CREATE POLICY tenant_boundary ON %I USING (tenant_id=current_setting(''app.tenant_id'',true)) WITH CHECK (tenant_id=current_setting(''app.tenant_id'',true))',t);
  EXECUTE format('CREATE TRIGGER immutable_record BEFORE UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION reject_version_mutation()',t);
 END LOOP;
END $$;
INSERT INTO schema_migrations(version) VALUES ('0006');
COMMIT;
