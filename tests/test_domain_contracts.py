import csv
import json
from copy import deepcopy
from datetime import datetime
from pathlib import Path

import pytest
from pydantic import ValidationError
from regtrace.contracts import ENTITY_ADAPTER, StepResult

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = json.loads((ROOT/'data/fixtures/onboarding.json').read_text())


def record(id):
    return deepcopy(next(e for e in FIXTURE['entities'] if e['id'] == id))


def parse(data):
    return ENTITY_ADAPTER.validate_json(json.dumps(data))


def test_fixture_failure_matches_actual_evidence():
    with (ROOT/'data/fixtures/synthetic-evidence.csv').open() as handle:
        rows = list(csv.DictReader(handle))
    failed = [r['case_id'] for r in rows if datetime.fromisoformat(r['review_at']) > datetime.fromisoformat(r['activation_at'])]
    assert failed == record('TR1-v1')['failed_case_ids'] == ['CASE-002']
    assert len(rows) == record('TR1-v1')['sample_size']
    assert all(r['reviewer'] for r in rows)


@pytest.mark.parametrize('evidence',['insufficient','conflicting','unassessed'])
def test_missing_or_conflicting_evidence_cannot_pass(evidence):
    data=record('A2-v1'); data['states']['evidence']=evidence
    with pytest.raises(ValidationError): parse(data)


@pytest.mark.parametrize('field',['evidence_ids','test_run_ids','mapping_ids'])
def test_pass_cannot_omit_references(field):
    data=record('A2-v1'); data[field]=[]
    with pytest.raises(ValidationError): parse(data)


def test_human_approval_does_not_override_failed_test():
    data=record('A1-v1'); data['states']['review']='approved'
    assert not parse(data).approved_effective


@pytest.mark.parametrize('field,value',[('applicability','unknown'),('coverage','partial'),
    ('implementation','documented_only'),('verification','unsupported'),('review','pending'),('freshness','stale')])
def test_effectiveness_requires_every_dimension(field,value):
    data=record('A2-v1'); data['states']['review']='approved'
    assert parse(data).approved_effective
    data['states'][field]=value
    assert not parse(data).approved_effective


def test_unknown_fields_and_coercion_are_rejected():
    for key,value in [('compliant',True),('version','1')]:
        data=record('A1-v1'); data[key]=value
        with pytest.raises(ValidationError): parse(data)


def test_invalid_source_span_is_rejected():
    data=record('SPAN-O1-v1'); data['end']+=1
    with pytest.raises(ValidationError): parse(data)


def test_missing_profile_facts_remain_unknown():
    data=record('APP1-v1'); data['missing_facts']=['customer risk category']
    with pytest.raises(ValidationError): parse(data)
    data['status']='unknown'; assert parse(data).status=='unknown'


def test_evidence_requires_timezone_and_valid_period():
    for field,value in [('collected_at','2026-09-04T00:00:00'),('period_end','2026-08-01T00:00:00Z')]:
        data=record('EV1-v1'); data[field]=value
        with pytest.raises(ValidationError): parse(data)


def test_failed_sample_cannot_be_labelled_pass():
    data=record('TR1-v1'); data['outcome']='pass'
    with pytest.raises(ValidationError): parse(data)


def test_remediation_cannot_close_without_review_and_retest():
    data=record('REM1-v1'); data['status']='closed'
    with pytest.raises(ValidationError): parse(data)


def test_unresolved_step_cannot_report_completion():
    data=dict(tenant_id='demo-bank',run_id='run-1',step_id='step-1',input_ids=['O1-v1'],
              input_hash='a'*64,configuration_hash='b'*64,status='completed',output_ids=[],
              unresolved_questions=['Missing evidence'],error_code=None)
    with pytest.raises(ValidationError): StepResult.model_validate_json(json.dumps(data))
    data['status']='needs_review'
    assert StepResult.model_validate_json(json.dumps(data)).status=='needs_review'
