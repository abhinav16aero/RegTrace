"""Strict immutable wire contracts; graph relationships point from outputs to inputs."""
from typing import Annotated, Literal
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, TypeAdapter, model_validator

Identifier = Annotated[str, Field(min_length=1, max_length=160)]
Digest = Annotated[str, Field(pattern=r'^[a-f0-9]{64}$')]
Nonempty = Annotated[str, Field(min_length=1)]


class Contract(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True)


class Record(Contract):
    schema_version: Literal['1.0'] = '1.0'
    tenant_id: Identifier
    id: Identifier
    lineage_id: Identifier
    version: Annotated[int, Field(ge=1)]
    synthetic: bool


class SourceDocument(Record):
    kind: Literal['source']
    title: Nonempty
    publisher: Nonempty
    source_url: str | None
    artifact_path: Nonempty
    sha256: Digest
    text_path: Nonempty
    text_sha256: Digest
    published_on: str | None
    effective_on: str | None
    current_law_status: Literal['not_verified', 'historical_snapshot', 'verified_as_of']
    verified_as_of: str | None = None

    @model_validator(mode='after')
    def current_status_has_date(self):
        if self.current_law_status == 'verified_as_of' and not self.verified_as_of:
            raise ValueError('verified current-law status requires an as-of date')
        if not self.synthetic and not self.source_url:
            raise ValueError('official sources require a source URL')
        return self


class SourceSpan(Record):
    kind: Literal['span']
    document_id: Identifier
    page: Annotated[int, Field(ge=1)] | None
    section: Nonempty
    start: Annotated[int, Field(ge=0)]
    end: Annotated[int, Field(gt=0)]
    text: Nonempty

    @model_validator(mode='after')
    def valid_offsets(self):
        if self.end <= self.start or self.end - self.start != len(self.text):
            raise ValueError('span offsets must exactly cover the cited text')
        return self


class BankProfile(Record):
    kind: Literal['profile']
    name: Nonempty
    jurisdiction: Nonempty
    entity_type: Nonempty
    products: tuple[Nonempty, ...]
    customer_categories: tuple[Nonempty, ...]
    channels: tuple[Nonempty, ...]
    unknown_facts: tuple[Nonempty, ...]


class Obligation(Record):
    kind: Literal['obligation']
    actor: Nonempty
    action: Nonempty
    object: Nonempty
    conditions: tuple[Nonempty, ...]
    exceptions: tuple[Nonempty, ...]
    timing: Nonempty | None
    span_ids: Annotated[tuple[Identifier, ...], Field(min_length=1)]


class Control(Record):
    kind: Literal['control']
    name: Nonempty
    owner_role: Nonempty | None
    scope: Nonempty
    frequency: Nonempty | None
    implementation: Literal['documented_only', 'implemented', 'unknown']
    expected_evidence: tuple[Nonempty, ...]
    span_ids: Annotated[tuple[Identifier, ...], Field(min_length=1)]


class Applicability(Record):
    kind: Literal['applicability']
    obligation_id: Identifier
    profile_id: Identifier
    status: Literal['applicable', 'not_applicable', 'unknown']
    reason: Nonempty
    missing_facts: tuple[Nonempty, ...]

    @model_validator(mode='after')
    def unresolved_facts(self):
        if self.missing_facts and self.status != 'unknown':
            raise ValueError('unresolved required facts imply unknown applicability')
        return self


class ClauseCoverage(Contract):
    clause: Nonempty
    status: Literal['full', 'partial', 'none', 'unassessed']
    rationale: Nonempty


class Mapping(Record):
    kind: Literal['mapping']
    obligation_id: Identifier
    control_id: Identifier
    clauses: Annotated[tuple[ClauseCoverage, ...], Field(min_length=1)]
    verification: Literal['pending', 'supported', 'unsupported', 'conflicted']


class Evidence(Record):
    kind: Literal['evidence']
    control_id: Identifier
    artifact_path: Nonempty
    sha256: Digest
    producer: Nonempty
    collected_at: AwareDatetime
    period_start: AwareDatetime
    period_end: AwareDatetime

    @model_validator(mode='after')
    def period_order(self):
        if self.period_end < self.period_start:
            raise ValueError('invalid evidence period')
        return self


class TestDefinition(Record):
    kind: Literal['test_definition']
    name: Nonempty
    procedure: Nonempty
    expected_outcome: Nonempty
    sample_rule: Nonempty


class TestRun(Record):
    kind: Literal['test_run']
    definition_id: Identifier
    control_id: Identifier
    evidence_ids: tuple[Identifier, ...]
    outcome: Literal['pass', 'fail', 'inconclusive', 'not_run']
    sample_size: Annotated[int, Field(ge=0)]
    failed_case_ids: tuple[Identifier, ...]
    limitations: Annotated[tuple[Nonempty, ...], Field(min_length=1)]

    @model_validator(mode='after')
    def evidence_required(self):
        if self.outcome in ('pass', 'fail') and (not self.evidence_ids or not self.sample_size):
            raise ValueError('executed test requires evidence and a nonempty sample')
        if self.outcome == 'pass' and self.failed_case_ids:
            raise ValueError('a failing sample cannot pass')
        return self


class AssessmentStates(Contract):
    applicability: Literal['applicable', 'not_applicable', 'unknown']
    coverage: Literal['full', 'partial', 'none', 'unassessed']
    implementation: Literal['documented_only', 'implemented', 'unknown']
    evidence: Literal['sufficient', 'insufficient', 'conflicting', 'unassessed']
    test: Literal['pass', 'fail', 'inconclusive', 'not_run']
    verification: Literal['pending', 'supported', 'unsupported', 'conflicted']
    review: Literal['pending', 'approved', 'rejected', 'changes_requested']
    freshness: Literal['current', 'stale', 'superseded']


class Assessment(Record):
    kind: Literal['assessment']
    applicability_id: Identifier
    mapping_ids: tuple[Identifier, ...]
    evidence_ids: tuple[Identifier, ...]
    test_run_ids: tuple[Identifier, ...]
    states: AssessmentStates
    rationale: Nonempty

    @model_validator(mode='after')
    def no_empty_pass(self):
        if self.states.test == 'pass' and (
            self.states.evidence != 'sufficient' or not self.evidence_ids or not self.test_run_ids
        ):
            raise ValueError('test pass requires sufficient referenced evidence and test runs')
        if self.states.coverage == 'full' and not self.mapping_ids:
            raise ValueError('full coverage requires mappings')
        return self

    @property
    def approved_effective(self) -> bool:
        s = self.states
        return (s.applicability == 'applicable' and s.coverage == 'full'
                and s.implementation == 'implemented' and s.evidence == 'sufficient'
                and s.test == 'pass' and s.verification == 'supported'
                and s.review == 'approved' and s.freshness == 'current')


class Gap(Record):
    kind: Literal['gap']
    assessment_id: Identifier
    gap_type: Literal['missing_control', 'partial_coverage', 'policy_only', 'missing_evidence',
                      'conflicting_evidence', 'execution_failure', 'unknown_applicability']
    description: Nonempty


class Remediation(Record):
    kind: Literal['remediation']
    gap_id: Identifier
    action: Nonempty
    owner_role: Nonempty | None
    acceptance_criteria: Annotated[tuple[Nonempty, ...], Field(min_length=1)]
    status: Literal['proposed', 'accepted', 'in_progress', 'awaiting_retest', 'closed', 'reopened']
    retest_ids: tuple[Identifier, ...]
    review: Literal['pending', 'approved', 'rejected', 'changes_requested']

    @model_validator(mode='after')
    def closure_has_review_and_retest(self):
        if self.status == 'closed' and (self.review != 'approved' or not self.retest_ids):
            raise ValueError('closure requires approved review and referenced retest')
        return self  # Persisted retest success must additionally be checked transactionally.


Entity = Annotated[SourceDocument | SourceSpan | BankProfile | Obligation | Control |
                   Applicability | Mapping | Evidence | TestDefinition | TestRun |
                   Assessment | Gap | Remediation, Field(discriminator='kind')]
ENTITY_ADAPTER = TypeAdapter(Entity)


class Dependency(Contract):
    tenant_id: Identifier
    dependent_id: Identifier
    input_id: Identifier
    relation: Literal['cites', 'derived_from', 'applies_obligation', 'uses_profile',
                      'maps_obligation', 'maps_control', 'evidences_control',
                      'uses_definition', 'tests_control', 'uses_evidence',
                      'assesses_applicability', 'assesses_mapping', 'uses_test',
                      'gap_in', 'remediates']


class StepResult(Contract):
    schema_version: Literal['1.0'] = '1.0'
    tenant_id: Identifier
    run_id: Identifier
    step_id: Identifier
    input_ids: tuple[Identifier, ...]
    input_hash: Digest
    configuration_hash: Digest
    status: Literal['completed', 'needs_review', 'failed', 'cancelled']
    output_ids: tuple[Identifier, ...]
    unresolved_questions: tuple[Nonempty, ...]
    error_code: Nonempty | None

    @model_validator(mode='after')
    def explicit_failures(self):
        if self.status == 'failed' and self.error_code is None:
            raise ValueError('failure requires an error code')
        if self.status == 'completed' and (self.error_code or self.unresolved_questions):
            raise ValueError('unresolved or failed work cannot report completed')
        return self
