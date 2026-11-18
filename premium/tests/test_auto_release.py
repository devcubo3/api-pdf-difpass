"""Automatic delivery through the API; synthetic PDFs, zero real provider calls."""
from dataclasses import replace
import hashlib
import json
from types import SimpleNamespace

import pymupdf
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.pipeline import process_next
from app.release import automatic_release_event,ReleaseBlocked
from app.storage import Storage
from test_canonical import case,generic_case,fake_provider


def automatic(case,monkeypatch):
    settings,source,extracted,understood=generic_case(case)
    settings=replace(settings,review_policy='validated_auto',media_mode='search',
                     legacy_pdf_hosts_json='{"a":["files.example.test"]}')
    calls=fake_provider(monkeypatch,understood)
    def no_search(*args,**kwargs):
        raise AssertionError('Automatic delivery must not pay for an unapproved photograph')
    monkeypatch.setattr('app.pipeline.PhotoSearch.search',no_search)
    return settings,source,calls


def send(client,source,key='automatic',**data):
    return client.post('/v2/documents',headers={'Authorization':'Bearer reviewer','Idempotency-Key':key},
        data={'legacy_pdf_url':'https://files.example.test/safety.pdf',**data},
        files={'input_pdf':('synthetic.pdf',source.read_bytes(),'application/pdf')})


def test_full_document_is_delivered_without_human_decision_and_only_one_provider_call(case,monkeypatch):
    settings,source,calls=automatic(case,monkeypatch)
    auth={'Authorization':'Bearer reviewer'}
    with TestClient(create_app(settings)) as client:
        created=send(client,source);assert created.status_code==202,created.text
        assert send(client,source).json()['document_id']==created.json()['document_id']
        route='/v2/documents/'+created.json()['document_id']
        assert process_next(settings)
        result=client.get(route,headers=auth).json()
        assert result['status']=='ready',result['issues']
        review=client.get(route+'/review',headers=auth).json()
        assert review['data']['release_policy']=='validated_auto'
        assert review['events'][-1]['action']=='automatic_release'
        assert review['events'][-1]['subject']=='system:validated-auto'
        assert review['checks']['codes_valid'] and review['checks']['validations_passed']
        assert 'MEDIA_INSTITUTIONAL' in {issue['code'] for issue in result['issues']}
        assert review['data']['hero_asset']=='premium-institutional.svg'
        delivered=client.get(route+'/pdf',headers=auth)
        assert delivered.status_code==200
        with pymupdf.open(stream=delivered.content,filetype='pdf') as pdf:
            assert pdf.embfile_count()==0
            assert 'TESTE VIAJANTE' in ''.join(page.get_text() for page in pdf)
            assert 'Baixar PDF de segurança' in pdf[-1].get_text()
            assert any(link.get('uri')=='https://files.example.test/safety.pdf' for link in pdf[-1].get_links())
        assert not process_next(settings)
        assert len(calls)==1


def test_preview_never_auto_releases(case,monkeypatch):
    settings,source,calls=automatic(case,monkeypatch)
    with TestClient(create_app(settings)) as client:
        created=send(client,source,generation_scope='preview')
        assert process_next(settings)
        route='/v2/documents/'+created.json()['document_id']
        assert client.get(route,headers={'Authorization':'Bearer reviewer'}).json()['status']=='needs_review'
        assert client.get(route+'/pdf',headers={'Authorization':'Bearer reviewer'}).status_code==409
        assert len(calls)==1


def test_existing_manual_job_is_not_approved_when_worker_policy_changes(case,monkeypatch):
    settings,source,_=automatic(case,monkeypatch)
    manual=replace(settings,review_policy='always')
    with TestClient(create_app(manual)) as client:
        created=send(client,source)
        assert process_next(settings)
        result=client.get('/v2/documents/'+created.json()['document_id'],headers={'Authorization':'Bearer reviewer'}).json()
        assert result['status']=='needs_review'


def test_auto_policy_requires_an_authorized_safety_link_before_queuing(case,monkeypatch):
    settings,source,calls=automatic(case,monkeypatch)
    with TestClient(create_app(settings)) as client:
        assert send(client,source,legacy_pdf_url='').status_code==422
        assert send(client,source,legacy_pdf_url='https://other.example.test/safety.pdf').status_code==422
        assert not process_next(settings)
        assert not calls


@pytest.mark.parametrize('render,pipeline',[('local','legacy'),('openai_images','legacy'),('html_template','legacy')])
def test_auto_policy_cannot_be_enabled_for_an_unvalidated_pipeline(render,pipeline):
    with pytest.raises(ValueError,match='validated_auto'):
        Settings(review_policy='validated_auto',render_mode=render,pipeline_version=pipeline)


@pytest.mark.parametrize('value',['[]','{"a":[{}]}','{"a":[{"asset_id":"bad","name":"Hotel","locality":null}]}'])
def test_approved_photo_catalog_rejects_invalid_or_unscoped_entries(value):
    with pytest.raises(ValueError,match='PDF_MEDIA_AUTO_CATALOG'):
        Settings(media_auto_catalog_json=value)


@pytest.fixture
def release_case(tmp_path):
    settings=Settings(data_dir=tmp_path,render_mode='html_template',pipeline_version='canonical_v1',
        review_policy='validated_auto',legacy_pdf_hosts_json='{"a":["files.example.test"]}')
    store=Storage(tmp_path)
    source=tmp_path/'source.pdf'; source.write_bytes(b'synthetic immutable source')
    final=tmp_path/'final.pdf'
    with pymupdf.open() as pdf:
        page=pdf.new_page()
        page.insert_text((30,40),'Synthetic artifact')
        page.insert_link({'kind':pymupdf.LINK_URI,'from':pymupdf.Rect(20,60,200,90),
                          'uri':'https://files.example.test/safety.pdf'})
        pdf.save(final)
    checks={'validations_passed':True,'planned_pages':1,'rendered_pages':1,
        'template_text':[{'passed':True}],
        'template_layout':[{'passed':True,'header':{'passed':True}}],
        'code_verification_scope':'all_pages','codes_valid':True,
        'canonical':{'evidence_validation_passed':True},'source_blocks':1,'composed_blocks':1,
        'code_count':0,'source_code_count':0,'blocking_issues':0,
        'original_download':{'kind':'legacy_pdf'}}
    return SimpleNamespace(settings=settings,store=store,
        row={'tenant_id':'a','revision':2,'source_path':str(source),
             'source_sha':hashlib.sha256(source.read_bytes()).hexdigest()},
        data={'release_policy':'validated_auto','pipeline_version':'canonical_v1',
              'generation_scope':'full','legacy_pdf_url':'https://files.example.test/safety.pdf'},
        artifacts={'pdf_path':str(final),'sha256':hashlib.sha256(final.read_bytes()).hexdigest(),
                   'checks':checks,'page_count':1},issues=[])


def release(c):
    return automatic_release_event(c.settings,c.store,c.row,c.data,c.artifacts,c.issues)


def test_release_audits_the_exact_source_artifact_and_revision(release_case):
    c=release_case
    event=release(c)
    assert event['revision']==2
    assert event['details']['artifact_sha256']==c.artifacts['sha256']
    assert event['details']['source_sha256']==c.row['source_sha']


@pytest.mark.parametrize('failure',['worker_disabled','old_snapshot','preview','blocked_fact','missing_text',
    'missing_page','bad_header','code_mismatch','missing_evidence','lost_block','wrong_download','wrong_tenant'])
def test_incomplete_or_unsafe_results_are_never_auto_released(release_case,failure):
    c=release_case; checks=c.artifacts['checks']
    if failure=='worker_disabled': c.settings=replace(c.settings,review_policy='always')
    elif failure=='old_snapshot': c.data.pop('release_policy')
    elif failure=='preview': c.data['generation_scope']='preview'
    elif failure=='blocked_fact': c.issues.append({'blocking':True,'code':'FACT_EVIDENCE_REVIEW'})
    elif failure=='missing_text': checks['template_text']=[]
    elif failure=='missing_page': checks['rendered_pages']=2
    elif failure=='bad_header': checks['template_layout'][0]['header']['passed']=False
    elif failure=='code_mismatch': checks['source_code_count']=1
    elif failure=='missing_evidence': checks['canonical']={}
    elif failure=='lost_block': checks['composed_blocks']=0
    elif failure=='wrong_download': checks['original_download']['kind']='supplier_original'
    elif failure=='wrong_tenant': c.row['tenant_id']='b'
    if failure=='wrong_tenant':
        with pytest.raises(ReleaseBlocked): release(c)
    else:
        assert release(c) is None


@pytest.mark.parametrize('target',['source_path','pdf_path'])
def test_tampered_source_or_final_blocks_automatic_delivery(release_case,target):
    c=release_case
    path=c.row[target] if target=='source_path' else c.artifacts[target]
    from pathlib import Path
    Path(path).write_bytes(b'tampered')
    with pytest.raises(ReleaseBlocked): release(c)


def test_pdf_must_contain_the_exact_safety_link_once(release_case):
    c=release_case
    with pymupdf.open() as pdf:
        page=pdf.new_page()
        page.insert_text((30,40),'Artifact with a wrong link')
        page.insert_link({'kind':pymupdf.LINK_URI,'from':pymupdf.Rect(20,60,200,90),
                          'uri':'https://files.example.test/other.pdf'})
        pdf.save(c.artifacts['pdf_path'])
    c.artifacts['sha256']=hashlib.sha256(c.store.read(c.artifacts['pdf_path'])).hexdigest()
    assert release(c) is None


def test_preapproved_photo_requires_exact_place_and_tenant(case,monkeypatch):
    from app.media import MediaCatalog
    settings,_,_,_=case
    asset='a'*64
    settings=replace(settings,media_auto_catalog_json=json.dumps({
        'a':[{'asset_id':asset,'name':'Example Resort','locality':'Orlando'}]}))
    catalog=MediaCatalog(settings,'a'); loads=[]
    monkeypatch.setattr(catalog,'load',lambda key:loads.append(key) or {'id':key})
    assert catalog.preapproved({'name':'  EXAMPLE resort ','locality':'Orlando'})=={'id':asset}
    assert catalog.preapproved({'name':'Example Resort','locality':'Miami'}) is None
    assert catalog.preapproved({'name':'Example','locality':'Orlando'}) is None
    assert catalog.preapproved({'name':'Example Resort','locality':None}) is None
    other=MediaCatalog(settings,'b')
    monkeypatch.setattr(other,'load',lambda key:pytest.fail('Cross-tenant photo access'))
    assert other.preapproved({'name':'Example Resort','locality':'Orlando'}) is None
    assert loads==[asset]
