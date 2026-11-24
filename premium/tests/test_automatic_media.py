"""Automatic photographs remain opt-in, contextual, credited and cache-safe."""
from dataclasses import replace
import json

from fastapi.testclient import TestClient
import pymupdf
import pytest

from app.automatic_media import select_automatic_photo
from app.config import Settings
from app.document_schema import Entity
from app.extraction import extract_document
from app.main import create_app
from app.media import PhotoSearch,photo_score,photo_identity
from app.media_fetch import MediaError
from app.pipeline import process_next
from test_canonical import case,understood,evidence,fake_provider
from test_media import FakeSource,source_settings


def context():
    return {'document_type':'hotel','media_context':{'version':2,'primary_index':0,
        'places':[{'name':'Example Resort Coast','locality':'Coast','kind':'hotel','source_block_ids':['p1-b1']}]}}


def test_automatic_photo_policy_is_explicit_and_validated(monkeypatch):
    assert Settings().media_auto_use_policy=='approved_only'
    monkeypatch.setenv('PDF_MEDIA_AUTO_USE_POLICY','matched_sources')
    assert Settings.from_env().media_auto_use_policy=='matched_sources'
    with pytest.raises(ValueError): Settings(media_auto_use_policy='anything')


def test_server_automatic_search_profile_enables_photos_and_allows_approved_only_override(monkeypatch):
    monkeypatch.delenv('PDF_MEDIA_AUTO_USE_POLICY',raising=False)
    for key,value in {'PDF_REVIEW_POLICY':'validated_auto','PDF_RENDER_MODE':'html_template',
                      'PDF_PIPELINE_VERSION':'canonical_v1','PDF_MEDIA_MODE':'search'}.items():
        monkeypatch.setenv(key,value)
    assert Settings.from_env().media_auto_use_policy=='matched_sources'
    monkeypatch.setenv('PDF_MEDIA_AUTO_USE_POLICY','approved_only')
    assert Settings.from_env().media_auto_use_policy=='approved_only'


def test_official_directory_accepts_amenity_suffix_but_not_another_property(source_settings):
    search=PhotoSearch(source_settings,'a')
    assert search.source_for('Example Resort Coast – All Inclusive')['entity']=='Example Resort Coast'
    assert search.source_for('Another Resort Coast – All Inclusive') is None
    assert search.source_for('Example Resort Coast Downtown') is None
    raw=json.loads(source_settings.media_sources_file.read_text())
    raw['sources'].append({**raw['sources'][0],'page_url':'https://other.example.test/'})
    source_settings.media_sources_file.write_text(json.dumps(raw))
    assert search.source_for('Example Resort Coast – All Inclusive') is None


def test_scenic_photos_rank_above_event_and_generic_gallery_images():
    assert photo_score('https://example.test/home-foto-aerea.jpg','Panorama',0) > photo_score(
        'https://example.test/IMG123.jpg','[gallery]',60)
    assert photo_score('https://example.test/casamento.jpg','[gallery]',60) < photo_score(
        'https://example.test/home-foto-aerea.jpg','Panorama',0)


def test_global_sharing_image_needs_property_evidence_while_matching_page_gallery_is_usable():
    assert not photo_identity('Example Resort Coast','Coast','https://example.test/other-property.jpg',
                              'Pool',5,'web_candidate')
    assert photo_identity('Example Resort Coast','Coast','https://example.test/example-coast.jpg',
                          'Example Resort Coast [gallery]',60,'web_candidate')


def test_approved_only_policy_does_not_search_or_claim_automatic_permission(source_settings,monkeypatch):
    monkeypatch.setattr('app.automatic_media.PhotoSearch',lambda *a,**k:pytest.fail('No automatic use policy'))
    data=context();issues=[]
    selected=select_automatic_photo(replace(source_settings,media_mode='search'),
        {'id':'doc','tenant_id':'a','data_json':{}},data,issues,None)
    assert selected is None and data['media_decision']['reason']=='MEDIA_AUTO_USE_NOT_ENABLED'
    assert issues[0]['code']=='MEDIA_INSTITUTIONAL'


@pytest.mark.parametrize('authority,expected',[('official',True),('web_candidate',True),('reviewer_supplied',False),('destination_stock',False)])
def test_policy_selects_only_context_checked_sources_and_keeps_cache_only_paid_guard(source_settings,monkeypatch,authority,expected):
    received=[]
    class Search:
        def __init__(self,*a,**kwargs): received.append(kwargs['allow_paid'])
        def search(self,subject,**kwargs):
            assert subject=='Example Resort Coast' and kwargs['locality']=='Coast'
            return {'candidates':[{'id':'a'*64,'score':10,'provider':'website',
                'authority':authority,'source_page_url':'https://resort.example.test/'}],
                'search_usage':{'new_calls':0}}
    monkeypatch.setattr('app.automatic_media.PhotoSearch',Search)
    data=context();issues=[]
    settings=replace(source_settings,media_mode='search',media_auto_use_policy='matched_sources')
    selected=select_automatic_photo(settings,{'id':'doc','tenant_id':'a','data_json':{'ai_cache_only':True}},data,issues,None)
    assert bool(selected)==expected and received==[False]
    if expected:
        assert selected['approval_scope']=='organization_matched_sources'
        assert selected['permission_confirmed']
        assert data['media_decision']['status']=='selected'
    else:
        assert data['media_decision']['reason']=='MEDIA_AUTO_SOURCE_REVIEW'


def test_search_failure_is_reported_without_claiming_no_search_was_attempted(source_settings,monkeypatch):
    class Search:
        def __init__(self,*a,**kwargs): pass
        def search(self,*a,**kwargs): raise MediaError('MEDIA_SEARCH_NOT_CONFIGURED','Not configured')
    monkeypatch.setattr('app.automatic_media.PhotoSearch',Search)
    data=context();issues=[]
    selected=select_automatic_photo(replace(source_settings,media_mode='search',media_auto_use_policy='matched_sources'),
        {'id':'doc','tenant_id':'a','data_json':{}},data,issues,None)
    assert selected is None and data['media_decision']['reason']=='MEDIA_SEARCH_NOT_CONFIGURED'
    assert 'MEDIA_SEARCH_NOT_CONFIGURED' in issues[0]['message']


def test_http_worker_delivers_photo_and_refresh_reuses_reading_and_preserves_previous_pdf(case,source_settings,monkeypatch):
    settings,source,_,_=case
    with pymupdf.open(source) as pdf:
        page=pdf.new_page();page.insert_text((40,60),'Hotel: Example Resort Coast - All Inclusive\nLocalidade: Coast');pdf.saveIncr()
    settings=replace(settings,pipeline_version='canonical_v1',review_policy='validated_auto',media_mode='search',
        media_auto_use_policy='matched_sources',media_sources_file=source_settings.media_sources_file,
        legacy_pdf_hosts_json='{"a":["files.example.test"]}')
    extracted=extract_document(source,settings.data_dir/'setup-codes',generic=True)
    doc=understood(extracted);place=evidence(extracted,'Hotel: Example Resort Coast')
    doc.entities.append(Entity(id='hotel',role='property',name='Example Resort Coast - All Inclusive',
                               locality='Coast',evidence=[place]))
    doc.services[0].entity_ids.append('hotel');doc.primary_place_id='hotel'
    calls=fake_provider(monkeypatch,doc);fetcher=FakeSource()
    monkeypatch.setattr('app.media.PublicFetcher',lambda:fetcher)
    auth={'Authorization':'Bearer reviewer'}
    with TestClient(create_app(settings)) as client:
        created=client.post('/v2/documents',headers={**auth,'Idempotency-Key':'auto-photo'},
            data={'legacy_pdf_url':'https://files.example.test/safety.pdf'},
            files={'input_pdf':('synthetic.pdf',source.read_bytes(),'application/pdf')})
        assert created.status_code==202
        route='/v2/documents/'+created.json()['document_id'];assert process_next(settings)
        review=client.get(route+'/review',headers=auth).json()
        assert review['status']=='ready',review['issues']
        photo=review['checks']['photo']
        assert photo['authority']=='official' and photo['permission_confirmed']
        assert photo['approval_scope']=='organization_matched_sources'
        assert photo['rights_status']=='permission_pending'  # Policy is not a provider licence claim.
        assert review['checks']['validations_passed'] and review['checks']['codes_valid']
        assert review['events'][-1]['details']['media_decision']['status']=='selected'
        assert len(calls)==1 and fetcher.calls
        previous=client.get(route+'/pdf',headers=auth).content
        with pymupdf.open(stream=previous,filetype='pdf') as pdf:
            assert any('Foto: Example Resort' in p.get_text() for p in pdf)
        body={'reason':'Refresh photo using the verified reading','allow_paid_search':False}
        assert client.post(route+'/media/refresh',json=body).status_code==401
        assert client.post(route+'/media/refresh',headers=auth,json=body).status_code==428
        assert client.post(route+'/media/refresh',headers={**auth,'If-Match':'stale'},json=body).status_code==409
        assert client.post(route+'/media/refresh',headers={'Authorization':'Bearer other','If-Match':review['etag']},json=body).status_code==404
        refreshed=client.post(route+'/media/refresh',headers={**auth,'If-Match':review['etag']},json=body)
        assert refreshed.status_code==202 and refreshed.json()['revision']==2
        assert client.get(route+'/pdf?revision=1',headers=auth).content==previous
        assert process_next(replace(settings,openai_api_key=None))
        after=client.get(route+'/review',headers=auth).json()
        assert after['status']=='ready',after['issues']
        assert after['checks']['photo'] and 'plan' in after['checks']['ai_cache_hits']
        assert after['data']['understanding_snapshot']==review['data']['understanding_snapshot']
        assert len(calls)==1
        assert client.get(route+'/pdf?revision=1',headers=auth).content==previous
