-- M0 immutable domain storage. Runtime jobs/auth/review projections belong to M1.
BEGIN;
CREATE TABLE schema_migrations (
    version text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE entity_versions (
    tenant_id text NOT NULL,
    id text NOT NULL,
    lineage_id text NOT NULL,
    version integer NOT NULL CHECK (version > 0),
    kind text NOT NULL CHECK (kind IN ('source','span','profile','obligation','control',
      'applicability','mapping','evidence','test_definition','test_run','assessment','gap','remediation')),
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, lineage_id, version),
    CHECK (jsonb_typeof(payload) = 'object'),
    CHECK (payload ?& ARRAY['tenant_id','id','lineage_id','version','kind','schema_version','synthetic']),
    CHECK (payload->>'tenant_id' = tenant_id AND payload->>'id' = id
      AND payload->>'lineage_id' = lineage_id AND (payload->>'version')::integer = version
      AND payload->>'kind' = kind AND payload->>'schema_version' = '1.0'),
    CHECK (kind <> 'assessment' OR (
      payload ?& ARRAY['states','evidence_ids','test_run_ids','mapping_ids']
      AND payload->'states' ?& ARRAY['test','evidence']
      AND jsonb_typeof(payload->'states'->'test') = 'string'
      AND jsonb_typeof(payload->'states'->'evidence') = 'string'
      AND jsonb_typeof(payload->'evidence_ids') = 'array'
      AND jsonb_typeof(payload->'test_run_ids') = 'array'
      AND (payload->'states'->>'test' <> 'pass' OR (
        payload->'states'->>'evidence' = 'sufficient'
        AND jsonb_array_length(payload->'evidence_ids') > 0
        AND jsonb_array_length(payload->'test_run_ids') > 0))))
);
CREATE INDEX entity_kind ON entity_versions (tenant_id, kind);
CREATE TABLE relation_rules (
    relation text NOT NULL,
    dependent_kind text NOT NULL,
    input_kind text NOT NULL,
    PRIMARY KEY (relation, dependent_kind, input_kind)
);
INSERT INTO relation_rules VALUES
 ('derived_from','span','source'), ('cites','obligation','span'), ('cites','control','span'),
 ('applies_obligation','applicability','obligation'), ('uses_profile','applicability','profile'),
 ('maps_obligation','mapping','obligation'), ('maps_control','mapping','control'),
 ('evidences_control','evidence','control'), ('uses_definition','test_run','test_definition'),
 ('tests_control','test_run','control'), ('uses_evidence','test_run','evidence'),
 ('assesses_applicability','assessment','applicability'), ('assesses_mapping','assessment','mapping'),
 ('uses_evidence','assessment','evidence'), ('uses_test','assessment','test_run'),
 ('gap_in','gap','assessment'), ('remediates','remediation','gap'), ('uses_test','remediation','test_run');
CREATE TABLE dependencies (
    tenant_id text NOT NULL,
    dependent_id text NOT NULL,
    input_id text NOT NULL,
    relation text NOT NULL,
    PRIMARY KEY (tenant_id, dependent_id, input_id, relation),
    FOREIGN KEY (tenant_id, dependent_id) REFERENCES entity_versions (tenant_id,id),
    FOREIGN KEY (tenant_id, input_id) REFERENCES entity_versions (tenant_id,id),
    CHECK (dependent_id <> input_id)
);
CREATE INDEX dependency_reverse ON dependencies (tenant_id, input_id, dependent_id);
CREATE FUNCTION validate_dependency() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF NOT EXISTS (
   SELECT 1 FROM entity_versions d JOIN entity_versions i ON i.tenant_id = d.tenant_id
   JOIN relation_rules r ON r.dependent_kind = d.kind AND r.input_kind = i.kind
   WHERE d.tenant_id = NEW.tenant_id AND d.id = NEW.dependent_id
     AND i.id = NEW.input_id AND r.relation = NEW.relation
 ) THEN RAISE EXCEPTION 'invalid dependency endpoint or relation' USING ERRCODE = '23514'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER dependency_type_check BEFORE INSERT ON dependencies
 FOR EACH ROW EXECUTE FUNCTION validate_dependency();
CREATE FUNCTION reject_version_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'immutable record: create a successor version' USING ERRCODE = '23514'; END $$;
CREATE TRIGGER immutable_entity BEFORE UPDATE OR DELETE ON entity_versions
 FOR EACH ROW EXECUTE FUNCTION reject_version_mutation();
CREATE TRIGGER immutable_dependency BEFORE UPDATE OR DELETE ON dependencies
 FOR EACH ROW EXECUTE FUNCTION reject_version_mutation();
-- Defense in depth: runtime role in M1 must be non-owner, non-superuser and NOBYPASSRLS.
ALTER TABLE entity_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE entity_versions FORCE ROW LEVEL SECURITY;
ALTER TABLE dependencies ENABLE ROW LEVEL SECURITY;
ALTER TABLE dependencies FORCE ROW LEVEL SECURITY;
CREATE POLICY entity_tenant ON entity_versions
 USING (tenant_id = current_setting('app.tenant_id', true))
 WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
CREATE POLICY dependency_tenant ON dependencies
 USING (tenant_id = current_setting('app.tenant_id', true))
 WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
INSERT INTO schema_migrations(version) VALUES ('0001');
COMMIT;
