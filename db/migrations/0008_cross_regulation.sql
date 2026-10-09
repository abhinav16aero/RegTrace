BEGIN;
ALTER TABLE workflow_jobs DROP CONSTRAINT workflow_jobs_kind_check;
ALTER TABLE workflow_jobs ADD CONSTRAINT workflow_jobs_kind_check CHECK(kind IN ('load_fixture','extract_documents','assess_documents','monitor_regulation','retest_remediation','assess_scenario','cross_regulation'));
CREATE TABLE cross_regulation_results (
 tenant_id text NOT NULL,job_id text NOT NULL,payload jsonb NOT NULL,created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,job_id),FOREIGN KEY(tenant_id,job_id) REFERENCES workflow_jobs(tenant_id,id)
);
CREATE TABLE cross_regulation_reviews (
 tenant_id text NOT NULL,job_id text NOT NULL,pair_id text NOT NULL,revision integer NOT NULL CHECK(revision>0),
 decision text NOT NULL CHECK(decision IN ('approved','rejected','changes_requested')),note text NOT NULL CHECK(length(trim(note))>0),
 actor_id text NOT NULL,created_at timestamptz NOT NULL DEFAULT now(),PRIMARY KEY(tenant_id,job_id,pair_id,revision),
 FOREIGN KEY(tenant_id,job_id) REFERENCES cross_regulation_results(tenant_id,job_id)
);
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['cross_regulation_results','cross_regulation_reviews'] LOOP
  EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY',t);
  EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY',t);
  EXECUTE format('CREATE POLICY tenant_boundary ON %I USING (tenant_id=current_setting(''app.tenant_id'',true)) WITH CHECK (tenant_id=current_setting(''app.tenant_id'',true))',t);
  EXECUTE format('CREATE TRIGGER immutable_record BEFORE UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION reject_version_mutation()',t);
 END LOOP;
END $$;
INSERT INTO schema_migrations(version) VALUES ('0008');
COMMIT;
