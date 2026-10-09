"""Offline adapter and ingestion tests. Mock responses are not live model evidence."""
import io
import json
import httpx
import pytest
from pypdf import PdfWriter
from regtrace import extraction as ex
from regtrace.storage import ArtifactStore


def parsed(quote='Banks must verify identity before activation.'):
    return ex.Extracted(obligations=[ex.Obligation(actor='Banks',action='verify',object='identity',
         conditions=[],exceptions=[],timing='before activation',quotes=[quote])],controls=[],unresolved_context=[])


def test_document_and_exact_citations(tmp_path):
    store=ArtifactStore(tmp_path); text='Banks must verify identity before activation.'
    doc=ex.document(text.encode(),'rule.txt','regulation',store)
    result=ex.locate(parsed(),doc,text,0,text)
    citation=result['items'][0]['citations'][0]
    assert citation['start']==0 and citation['end']==len(text) and citation['pages']==[1]
    assert store.read(doc['text_sha256']).decode()[citation['start']:citation['end']]==citation['text']
    with pytest.raises(ex.ExtractionError,match='not_found'):ex.locate(parsed('Invented requirement'),doc,text,0,text)
    with pytest.raises(ex.ExtractionError,match='not_unique'):ex.locate(parsed(),doc,text*2,0,text*2)
    with pytest.raises(ex.ExtractionError,match='wrong_extraction_kind'):ex.locate(parsed(),doc|{'role':'policy'},text,0,text)


def test_empty_scanned_binary_and_csv_inputs(tmp_path):
    store=ArtifactStore(tmp_path)
    for raw,name in [(b'','a.txt'),(b'  ','a.txt'),(b'\xff','a.txt'),(b'hi\x00','a.txt'),(b'hi','a.exe')]:
        with pytest.raises(ex.ExtractionError):ex.document(raw,name,'policy',store)
    writer=PdfWriter();writer.add_blank_page(100,100);out=io.BytesIO();writer.write(out)
    with pytest.raises(ex.ExtractionError,match='requires_ocr'):ex.document(out.getvalue(),'scan.pdf','policy',store)
    with pytest.raises(ex.ExtractionError):ex.evidence(b'a,a\n1,2','a.csv',store)
    with pytest.raises(ex.ExtractionError):ex.evidence(b'a,b\n1','a.csv',store)
    assert ex.evidence(b'case,reviewer\n1,A\n2,B','a.csv',store)['rows']==2


def test_chunking_covers_every_character_without_truncation():
    text='abcd'*18000; config=ex.configuration();chunks=list(ex.chunks(text,config))
    assert len(chunks)>1
    covered=set()
    for offset,part in chunks:
        assert part==text[offset:offset+len(part)];covered.update(range(offset,offset+len(part)))
    assert len(covered)==len(text)


def test_groq_request_and_safe_failure(monkeypatch):
    monkeypatch.setenv('GROQ_API_KEY','test-secret-not-live')
    def handler(request):
        assert str(request.url)=='https://api.groq.com/openai/v1/chat/completions'
        body=json.loads(request.content)
        assert body['response_format']['json_schema']['strict'] is True
        assert body['model']=='openai/gpt-oss-120b'
        assert 'controls' not in body['response_format']['json_schema']['schema']['properties']
        assert 'test-secret-not-live' not in request.content.decode()
        content=parsed().model_dump(exclude={'controls'})
        return httpx.Response(200,json={'id':'mock-response','choices':[{'finish_reason':'stop','message':{'content':json.dumps(content)}}],
                                        'usage':{'total_tokens':123,'prompt_tokens':100,'completion_tokens':23}})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result,metadata,error=ex.call_model(ex.configuration(),'regulation','input',client=client)
    assert result==parsed() and error is None and metadata['usage']['total_tokens']==123
    assert 'test-secret-not-live' not in json.dumps(metadata)
    with httpx.Client(transport=httpx.MockTransport(lambda _:httpx.Response(401,text='private provider response'))) as client:
        result,metadata,error=ex.call_model(ex.configuration(),'policy','input',client=client)
    assert result is None and error=='provider_http_401'
    assert 'private' not in json.dumps(metadata)


def test_policy_contract_cannot_emit_obligations_and_rate_limit_is_visible(monkeypatch):
    monkeypatch.setenv('GROQ_API_KEY','test-key')
    def handler(request):
        properties=json.loads(request.content)['response_format']['json_schema']['schema']['properties']
        assert 'controls' in properties and 'obligations' not in properties
        return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'controls':[], 'unresolved_context':[]})}}]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result,metadata,error=ex.call_model(ex.configuration(),'policy','policy input',client=client)
    assert result==ex.Extracted(obligations=[],controls=[],unresolved_context=[]) and error is None
    with httpx.Client(transport=httpx.MockTransport(lambda _:httpx.Response(429,headers={'Retry-After':'37'},text='private provider diagnostic'))) as client:
        result,metadata,error=ex.call_model(ex.configuration(),'policy','policy input',client=client)
    assert error=='provider_http_429' and metadata['retry_after_seconds']==37
    assert 'private' not in json.dumps(metadata)


def test_missing_key_and_truncated_response(monkeypatch):
    monkeypatch.delenv('GROQ_API_KEY',raising=False);monkeypatch.delenv('GROQ_API_KEY_FILE',raising=False)
    assert not ex.provider_status()['configured']
    with pytest.raises(ex.ExtractionError,match='provider_key_missing'):ex.api_key()
    monkeypatch.setenv('GROQ_API_KEY','test-key')
    with httpx.Client(transport=httpx.MockTransport(lambda _:httpx.Response(200,json={
         'choices':[{'finish_reason':'length','message':{'content':'{}'}}]}))) as client:
        result,metadata,error=ex.call_model(ex.configuration(),'regulation','input',client=client)
    assert result is None and error=='provider_incomplete_or_refused'


def test_private_json_credentials_precedence_and_redacted_status(monkeypatch,tmp_path):
    secret=tmp_path/'secrets.json';secret.write_text(json.dumps({'groq_api':'local-test-secret'}))
    monkeypatch.setattr(ex,'DEFAULT_SECRETS_FILE',secret)
    assert ex.api_key()=='local-test-secret'
    assert ex.provider_status()['configured'] and 'local-test-secret' not in json.dumps(ex.provider_status())
    raw=tmp_path/'key';raw.write_text('raw-test-secret\n')
    monkeypatch.setenv('GROQ_API_KEY_FILE',str(raw));assert ex.api_key()=='raw-test-secret'
    monkeypatch.setenv('GROQ_API_KEY','env-test-secret');assert ex.api_key()=='env-test-secret'
    monkeypatch.delenv('GROQ_API_KEY');monkeypatch.delenv('GROQ_API_KEY_FILE')
    monkeypatch.setenv('GROQ_SECRETS_FILE',str(tmp_path/'missing.json'))
    with pytest.raises(ex.ExtractionError,match='provider_key_missing'):ex.api_key()


@pytest.mark.parametrize('content',['not-json-secret','["not-an-object"]','{"groq_api":123}'])
def test_malformed_secret_files_fail_without_exposing_contents(monkeypatch,tmp_path,content):
    secret=tmp_path/'bad.json';secret.write_text(content);monkeypatch.setenv('GROQ_SECRETS_FILE',str(secret))
    with pytest.raises(ex.ExtractionError) as error:ex.api_key()
    assert str(error.value)=='provider_secrets_invalid'
    assert ex.provider_status()['configured'] is False
