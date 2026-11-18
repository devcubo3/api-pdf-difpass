from dataclasses import replace
import hashlib
from types import SimpleNamespace
from urllib.parse import urlsplit

from fastapi.testclient import TestClient
import pymupdf as fitz
import pytest
from sqlalchemy import select

from app import ai_images
from app.config import Settings
from app.db import access_links
from app.main import create_app
from app.pipeline import process_next
from test_html_template import case,synthetic_plan
from app.extraction import extract_document


@pytest.mark.parametrize('url',['http://external.example','https://user:pass@host.test','https://host.test/path',
    'https://host.test?token=x','https://host.test/#fragment','javascript:alert(1)'])
def test_original_link_origin_rejects_credentials_paths_and_unsafe_schemes(url):
    with pytest.raises(ValueError):
        Settings(public_base_url=url)


def test_pdf_original_button_shares_only_source_without_unlocking_premium_and_respects_expiry_and_revocation(case,monkeypatch):
    settings,source,_,_=case
    settings=replace(settings,public_base_url='https://premium.example.test/')
    extracted=extract_document(source,settings.data_dir/'evidence-for-fake-plan')
    plan=synthetic_plan(extracted)
    fake=SimpleNamespace(responses=SimpleNamespace(create=lambda **kw:SimpleNamespace(
        status='completed',output_text=plan.model_dump_json(),usage=None)))
    monkeypatch.setattr(ai_images,'OpenAI',lambda **kw:fake)
    auth={'Authorization':'Bearer reviewer'}
    with TestClient(create_app(settings)) as client:
        created=client.post('/v2/documents',headers={**auth,'Idempotency-Key':'original-button'},
            files={'input_pdf':('test.pdf',source.read_bytes(),'application/pdf')})
        route='/v2/documents/'+created.json()['document_id']
        assert process_next(settings)
        review=client.get(route+'/review',headers=auth).json()
        assert review['checks']['validations_passed'],review['issues']
        assert review['checks']['original_download']['included']
        with fitz.open(stream=client.get(route+'/review/pdf',headers=auth).content,filetype='pdf') as pdf:
            links=[link['uri'] for link in pdf[-1].get_links() if '/v2/access/' in link.get('uri','')]
            assert len(links)==1 and len(pdf)==2 and pdf.embfile_count()==0
            assert 'Este documento foi gerado pela Diff Travel' in pdf[-1].get_text()
            assert 'Baixar PDF original' in pdf[-1].get_text()
            assert not any('/v2/access/' in link.get('uri','') for link in pdf[0].get_links())
        path=urlsplit(links[0]).path
        token=path.rsplit('/',1)[1]
        assert client.get(path).content==source.read_bytes()
        assert client.get(path,headers={'Authorization':'Bearer other'}).content==source.read_bytes()
        assert client.get(path,headers={'Authorization':'Bearer reader'}).content==source.read_bytes()
        assert client.get(route+'/pdf',headers=auth).status_code==409
        assert client.get(route+'/source').status_code==401
        draft_download=client.get(path,headers=auth)
        assert draft_download.content==source.read_bytes()
        assert 'attachment' in draft_download.headers['content-disposition']
        assert draft_download.headers['referrer-policy']=='no-referrer'
        with client.app.state.db.engine.connect() as connection:
            grant=dict(connection.execute(select(access_links)).mappings().one())
        assert token not in str(grant) and grant['artifact']=='original_download'
        assert grant['token_sha256']==hashlib.sha256(token.encode()).hexdigest()
        assert token not in client.get(route+'/review',headers=auth).text
        # Approval changes the ETag, but the immutable source link remains usable.
        approved=client.post(route+'/review/decision',headers={**auth,'If-Match':review['etag']},json={
            'action':'approve','revision':review['revision'],'artifact_sha256':review['artifact_sha256']})
        assert approved.status_code==200
        assert client.get(path).content==source.read_bytes()
        assert client.get(path).headers['cache-control']=='private, no-store'
        # Expiration of the document also revokes the usable source capability.
        database=client.app.state.db
        saved_row=database.get(created.json()['document_id'],'a')
        expired=database.update_document(saved_row['id'],'a',saved_row['etag'],status='expired')
        assert client.get(path).status_code==404
        database.update_document(saved_row['id'],'a',expired['etag'],status='ready')
        assert client.get(path+'/source').status_code==404
        assert client.get(path[:-1]+('A' if path[-1]!='A' else 'B')).status_code==404
        with monkeypatch.context() as clock:
            clock.setattr('app.db.time.time',lambda:grant['expires_at']+1)
            assert client.get(path).status_code==404
        original=source.read_bytes()
        # The persisted source lives in the API storage, separate from the fixture upload.
        row=client.app.state.db.get(created.json()['document_id'],'a')
        from pathlib import Path
        stored=Path(row['source_path'])
        stored.write_bytes(original+b'\nchanged')
        assert client.get(path).status_code==409
        stored.write_bytes(original)
        state=client.get(route,headers=auth).json()
        assert client.post(route+'/original-downloads/revoke',headers={'Authorization':'Bearer reader',
            'If-Match':state['etag']}).status_code==403
        assert client.post(route+'/original-downloads/revoke',headers=auth).status_code==428
        revoked=client.post(route+'/original-downloads/revoke',headers={**auth,'If-Match':state['etag']})
        assert revoked.status_code==200 and revoked.json()['revoked']==1
        assert client.get(path).status_code==404
