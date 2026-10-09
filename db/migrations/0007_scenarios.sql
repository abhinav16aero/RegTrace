BEGIN;
ALTER TABLE workflow_jobs DROP CONSTRAINT workflow_jobs_kind_check;
ALTER TABLE workflow_jobs ADD CONSTRAINT workflow_jobs_kind_check CHECK(kind IN ('load_fixture','extract_documents','assess_documents','monitor_regulation','retest_remediation','assess_scenario'));
CREATE TABLE scenarios (
 tenant_id text NOT NULL,id text NOT NULL,name text NOT NULL,note text NOT NULL,
 baseline_job_id text NOT NULL,extraction_job_id text NOT NULL,baseline jsonb NOT NULL,
 changes jsonb NOT NULL,evidence_context jsonb,created_by text NOT NULL,created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,id),UNIQUE(tenant_id,extraction_job_id),
 FOREIGN KEY(tenant_id,baseline_job_id) REFERENCES workflow_jobs(tenant_id,id),
 FOREIGN KEY(tenant_id,extraction_job_id) REFERENCES workflow_jobs(tenant_id,id)
);
CREATE TABLE scenario_assessment_links (
 tenant_id text NOT NULL,scenario_id text NOT NULL,job_id text NOT NULL,
 PRIMARY KEY(tenant_id,scenario_id),UNIQUE(tenant_id,job_id),
 FOREIGN KEY(tenant_id,scenario_id) REFERENCES scenarios(tenant_id,id),
 FOREIGN KEY(tenant_id,job_id) REFERENCES workflow_jobs(tenant_id,id)
);
CREATE TABLE scenario_results (
 tenant_id text NOT NULL,scenario_id text NOT NULL,payload jsonb NOT NULL,created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,scenario_id),FOREIGN KEY(tenant_id,scenario_id) REFERENCES scenarios(tenant_id,id)
);
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['scenarios','scenario_assessment_links','scenario_results'] LOOP
  EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY',t);
  EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY',t);
  EXECUTE format('CREATE POLICY tenant_boundary ON %I USING (tenant_id=current_setting(''app.tenant_id'',true)) WITH CHECK (tenant_id=current_setting(''app.tenant_id'',true))',t);
  EXECUTE format('CREATE TRIGGER immutable_record BEFORE UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION reject_version_mutation()',t);
 END LOOP;
END $$;
INSERT INTO schema_migrations(version) VALUES ('0007');
COMMIT;
