BEGIN;
ALTER TABLE workflow_jobs DROP CONSTRAINT workflow_jobs_kind_check;
ALTER TABLE workflow_jobs ADD CONSTRAINT workflow_jobs_kind_check CHECK(kind IN ('load_fixture','extract_documents','assess_documents'));
INSERT INTO schema_migrations(version) VALUES ('0004');
COMMIT;
