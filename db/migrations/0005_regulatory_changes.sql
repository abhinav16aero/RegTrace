BEGIN;
ALTER TABLE workflow_jobs DROP CONSTRAINT workflow_jobs_kind_check;
ALTER TABLE workflow_jobs ADD CONSTRAINT workflow_jobs_kind_check CHECK(kind IN ('load_fixture','extract_documents','assess_documents','monitor_regulation'));
CREATE TABLE regulatory_tracks (
 tenant_id text NOT NULL, id text NOT NULL, name text NOT NULL,
 baseline_extraction_id text NOT NULL, current_extraction_id text NOT NULL,
 source_id text, monitor_enabled boolean NOT NULL DEFAULT false,
 interval_seconds integer NOT NULL CHECK(interval_seconds BETWEEN 3600 AND 604800),
 next_check_at timestamptz NOT NULL DEFAULT now(), created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,id), UNIQUE(tenant_id,baseline_extraction_id),
 FOREIGN KEY(tenant_id,baseline_extraction_id) REFERENCES workflow_jobs(tenant_id,id),
 FOREIGN KEY(tenant_id,current_extraction_id) REFERENCES workflow_jobs(tenant_id,id)
);
CREATE TABLE regulatory_versions (
 tenant_id text NOT NULL, id text NOT NULL, track_id text NOT NULL, sequence integer NOT NULL,
 previous_extraction_id text NOT NULL, extraction_id text, document jsonb NOT NULL,
 kind text NOT NULL CHECK(kind IN ('full_snapshot','synthetic_snapshot','amendment')),
 status text NOT NULL CHECK(status IN ('baseline','processing','compared','formatting_only','needs_review')),
 origin text NOT NULL CHECK(origin IN ('baseline','upload','monitor')),
 created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(tenant_id,id), UNIQUE(tenant_id,track_id,sequence),
 FOREIGN KEY(tenant_id,track_id) REFERENCES regulatory_tracks(tenant_id,id),
 FOREIGN KEY(tenant_id,previous_extraction_id) REFERENCES workflow_jobs(tenant_id,id),
 FOREIGN KEY(tenant_id,extraction_id) REFERENCES workflow_jobs(tenant_id,id)
);
CREATE TABLE regulatory_change_reports (
 tenant_id text NOT NULL, version_id text NOT NULL, payload jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,version_id), FOREIGN KEY(tenant_id,version_id) REFERENCES regulatory_versions(tenant_id,id)
);
CREATE TABLE assessment_freshness_events (
 tenant_id text NOT NULL, assessment_id text NOT NULL, version_id text NOT NULL, reason text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(tenant_id,assessment_id,version_id),
 FOREIGN KEY(tenant_id,assessment_id) REFERENCES entity_versions(tenant_id,id),
 FOREIGN KEY(tenant_id,version_id) REFERENCES regulatory_versions(tenant_id,id)
);
CREATE TABLE reassessment_links (
 tenant_id text NOT NULL, version_id text NOT NULL, previous_job_id text NOT NULL, new_job_id text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(tenant_id,version_id,previous_job_id),
 FOREIGN KEY(tenant_id,version_id) REFERENCES regulatory_versions(tenant_id,id),
 FOREIGN KEY(tenant_id,previous_job_id) REFERENCES workflow_jobs(tenant_id,id),
 FOREIGN KEY(tenant_id,new_job_id) REFERENCES workflow_jobs(tenant_id,id)
);
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['regulatory_tracks','regulatory_versions','regulatory_change_reports','assessment_freshness_events','reassessment_links'] LOOP
  EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY',t);
  EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY',t);
  EXECUTE format('CREATE POLICY tenant_boundary ON %I USING (tenant_id=current_setting(''app.tenant_id'',true)) WITH CHECK (tenant_id=current_setting(''app.tenant_id'',true))',t);
 END LOOP;
 FOREACH t IN ARRAY ARRAY['regulatory_change_reports','assessment_freshness_events','reassessment_links'] LOOP
  EXECUTE format('CREATE TRIGGER immutable_record BEFORE UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION reject_version_mutation()',t);
 END LOOP;
END $$;
-- Version content/lineage is immutable; only the processing status can progress.
CREATE FUNCTION protect_regulatory_version() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
 IF TG_OP='DELETE' OR (to_jsonb(OLD)-'status') IS DISTINCT FROM (to_jsonb(NEW)-'status') THEN
  RAISE EXCEPTION 'Regulatory version content is immutable';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER immutable_version_content BEFORE UPDATE OR DELETE ON regulatory_versions
 FOR EACH ROW EXECUTE FUNCTION protect_regulatory_version();
INSERT INTO schema_migrations(version) VALUES ('0005');
COMMIT;
