"""General public contexts and spend controls. Synthetic sources; no paid calls."""
from dataclasses import replace
import hashlib
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.ai_images import ContextualPlan
from app.ai_budget import BudgetLedger
from app.db import Database,ai_calls
from app.media import PhotoSearch
from app.media import PhotoPage,photo_score
from app.media import photo_identity,image_variants
from app.media_context import classify_general,grounded_context,primary_place,safe_public
from app.media_fetch import Fetched,MediaError
from app.media_web import WebDiscovery,sourced_pages
from test_media import source_settings,FakeSource,photo
from test_html_template import case


@pytest.mark.parametrize('text,expected',[
    ('Hotel: Serra Azul\nCheck-in: 01/01\nCheck-out: 03/01','hotel'),
    ('Property: Alpine Lodge\nDestination: Zermatt','hotel'),
    ('Voo: AB123\nDestino: Lisboa','flight'),
    ('Flight: XX123\nDestination: Tokyo','flight'),
    ('Flight: XX123\nCheck-in online\nDestination: Tokyo','flight'),
    ('Voucher de traslado\nDestino: Santiago','transfer'),
    ('Hotel: Sem Cadastro\nVoo: AB123','travel_bundle'),
    ('Attraction: Example Theme Park\nAdmission','ticket'),
    ('Car rental\nPickup: Madrid','car_rental'),
    ('Travel insurance\nPolicy: DEMO','insurance'),
    ('Cruise\nCabine: DEMO','cruise'),
    ('Contrato de prestação de serviços e pagamento','unknown')])
def test_services_do_not_depend_on_registered_destination(text,expected):
    assert classify_general(text)[0]==expected


def context(name='Alpine Lodge',locality='Zermatt'):
    return {'places':[{'kind':'hotel','name':name,'locality':locality,'source_block_ids':['p1-b1']}],
            'primary_index':0}


def test_context_requires_source_evidence_and_never_uses_guest_fields():
    pages=[{'page':1,'blocks':[{'text':'Property: Alpine Lodge\nDestination: Zermatt\nGuest: TEST GUEST'}]}]
    grounded=grounded_context(context(),pages)
    assert primary_place({'media_context':grounded})['name']=='Alpine Lodge'
    assert grounded_context(context(name='Invented Hotel'),pages) is None
    assert grounded_context(context(locality='Paris'),pages) is None
    assert grounded_context(context(name='TEST GUEST',locality=None),pages) is None
    assert grounded_context({**context(),'primary_index':8},pages) is None
    ambiguous={**context(),'primary_index':None}
    assert primary_place({'media_context':grounded_context(ambiguous,pages)}) is None
    assert primary_place({'title':'TEST GUEST - Voucher 99999'}) is None


@pytest.mark.parametrize('value',['Guest: JOHN EXAMPLE','CPF 000.000.000-00','Hotel 555555555',
    'Hotel contact@example.test','https://example.test','Alpine\nGuest: Example','Booking ABCDE'])
def test_public_query_blocks_private_fields(value):
    assert not safe_public(value)


def response(url='https://resort.example.test/',status='completed'):
    return {'status':status,'output':[
        {'type':'web_search_call','status':'completed','action':{'type':'search','sources':[
            {'url':url,'title':'Example Resort Coast'}]}},
        {'type':'message','content':[{'type':'output_text','text':'Guessed https://wrong.example.test/',
            'annotations':[{'type':'url_citation','url':url,'title':'Example Resort Coast'}]}]}],
        'usage':{'input_tokens':100,'output_tokens':50}}


def test_discovery_uses_tool_sources_not_guessed_model_urls():
    assert sourced_pages(response())==[{'page_url':'https://resort.example.test/','search_title':'Example Resort Coast'}]
    payload=response('https://127.0.0.1/internal')
    with pytest.raises(MediaError):
        sourced_pages(payload)


def test_public_html_accepts_empty_boolean_image_attributes():
    page=PhotoPage('<title>Test</title><meta property="og:image" content><img src="/home.jpg" alt data-srcset>',
                   'https://example.test/')
    assert any(url.endswith('/home.jpg') for url,_,_ in page.photos)
    for url,alt,base in page.photos:
        photo_score(url,alt,base)


def test_network_property_pages_exclude_global_images_and_promotions():
    markup='''<title>Alpine Lodge Zermatt</title>
    <meta property="og:image" content="https://images.example.test/different-property.jpg">
    <nav><img src="/alpine-lodge-logo.jpg" alt="Alpine Lodge"></nav>
    <div class="offer-banner"><img src="/sale.jpg"></div>
    <section class="photo-gallery"><img src="/exterior.jpg"></section>
    <script>window.__INITIAL_STATE__={"modules":[
      {"module":"AmenitiesSlider","data":{"slides":[{"image":"https://images.example.test/vista-hotel.jpg"}]}},
      {"module":"ColumnSlider","data":{"slides":[{"image":"https://images.example.test/unrelated-nearby.jpg"}]}},
      {"module":"PromoHotel","data":{"image":"https://images.example.test/promo.jpg"}}
    ]};</script>'''
    page=PhotoPage(markup,'https://alpine.example.test/')
    urls=[u for u,a,b in page.photos if photo_identity('Alpine Lodge','Zermatt',u,a,b,'web_candidate') and photo_score(u,a,b)>=0]
    assert 'https://alpine.example.test/exterior.jpg' in urls
    assert 'https://images.example.test/vista-hotel.jpg' in urls
    assert not any('different-property' in u or 'unrelated-nearby' in u or 'sale' in u or 'logo' in u or 'promo' in u for u in urls)


def test_cdn_original_variants_keep_public_url_guards():
    assert image_variants('https://cdn.example.test/x/https://photos.example.test/exterior.jpg')[0]=='https://photos.example.test/exterior.jpg'
    assert image_variants('https://cdn.example.test/x/https://127.0.0.1/private.jpg')==['https://cdn.example.test/x/https://127.0.0.1/private.jpg']
    payload=response()
    payload['output'][0]['action']['type']='open_page'
    with pytest.raises(MediaError):
        sourced_pages(payload)


@pytest.fixture
def discovery_case(source_settings):
    settings=replace(source_settings,media_openai_search_enabled=True,openai_api_key='synthetic')
    db=Database(settings)
    db.create_schema()
    row,_=db.enqueue('11111111-1111-4111-8111-111111111111','org','b'*64,'synthetic.pdf',
        doc_type='hotel',idempotency_key='general-discovery',initial_data={})
    return settings,db,row


def test_web_discovery_is_single_call_cached_and_budgeted(discovery_case,tmp_path):
    settings,db,row=discovery_case
    calls=[]
    class Client:
        def create(self,**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(model_dump=lambda:response())
    client=SimpleNamespace(responses=Client())
    search=WebDiscovery(settings,db,row,tmp_path,client=client)
    assert search.discover({'name':'Example Resort Coast','locality':None})
    assert calls[0]['max_tool_calls']==1 and calls[0]['max_output_tokens']==1200
    assert calls[0]['store'] is False and calls[0]['tools'][0]['type']=='web_search'
    assert search.usage['new_calls']==1
    assert search.discover({'name':'Example Resort Coast','locality':None})
    assert len(calls)==1 and search.usage['cache_hit']
    with db.engine.connect() as conn:
        rows=conn.execute(select(ai_calls)).mappings().all()
        assert len(rows)==1 and rows[0]['stage']=='media_search' and rows[0]['status']=='completed'
    next(tmp_path.glob('discovery-*.json')).write_text('tampered')
    with pytest.raises(MediaError) as error:
        search.discover({'name':'Example Resort Coast','locality':None})
    assert error.value.code=='MEDIA_SEARCH_UNCERTAIN' and len(calls)==1


def test_uncertain_search_is_never_repeated(discovery_case,tmp_path):
    settings,db,row=discovery_case
    calls=[]
    def fail(**kwargs):
        calls.append(kwargs)
        raise TimeoutError('synthetic')
    search=WebDiscovery(settings,db,row,tmp_path,client=SimpleNamespace(responses=SimpleNamespace(create=fail)))
    for _ in range(2):
        with pytest.raises(MediaError) as error:
            search.discover({'name':'Unregistered Lodge','locality':'Zermatt'})
        assert error.value.code=='MEDIA_SEARCH_UNCERTAIN'
    assert len(calls)==1
    assert BudgetLedger(db,row).summary()[0]['status']=='uncertain'


def test_cache_only_and_exhausted_budget_do_not_call_provider(discovery_case,tmp_path):
    settings,db,row=discovery_case
    search=WebDiscovery(settings,db,row,tmp_path,allow_paid=False)
    with pytest.raises(MediaError) as error:
        search.discover({'name':'Unknown Hotel'})
    assert error.value.code=='MEDIA_SEARCH_CACHE_REQUIRED'
    ledger=BudgetLedger(db,row)
    ledger.reserve('f'*64,'plan','gpt-6-luna',12)
    search=WebDiscovery(settings,db,row,tmp_path)
    with pytest.raises(MediaError) as error:
        search.discover({'name':'Unknown Hotel'})
    assert error.value.code=='MEDIA_SEARCH_BUDGET'


def test_additional_search_window_stops_a_third_reservation(discovery_case):
    settings,db,row=discovery_case
    ledger=BudgetLedger(db,row)
    for key in ('1','2'):
        assert ledger.reserve(key*64,'media_search','gpt-6-luna',.05,
            extra_windows=[('media-day:test',.10)]) is None
    from app.ai_budget import AIBudgetError
    with pytest.raises(AIBudgetError):
        ledger.reserve('3'*64,'media_search','gpt-6-luna',.05,
            extra_windows=[('media-day:test',.10)])
    assert len(ledger.summary())==2


def test_failed_render_can_resume_through_http_without_new_provider_calls(case,monkeypatch):
    from app import ai_images,pipeline
    from app.main import create_app
    from fastapi.testclient import TestClient
    settings,source,_,plan=case
    calls=[]
    def complete(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(status='completed',output_text=plan.model_dump_json(),usage=None)
    monkeypatch.setattr(ai_images,'OpenAI',lambda **kw:SimpleNamespace(responses=SimpleNamespace(create=complete)))
    original=pipeline.render_html_document
    def fail(*args,**kwargs):
        raise TypeError('Synthetic interrupted rendering')
    monkeypatch.setattr(pipeline,'render_html_document',fail)
    with TestClient(create_app(settings)) as client:
        auth={'Authorization':'Bearer reviewer'}
        uploaded=client.post('/v2/documents',headers={**auth,'Idempotency-Key':'cache-recovery'},
            data={'generation_scope':'preview'},files={'input_pdf':('fixture.pdf',source.read_bytes(),'application/pdf')})
        route='/v2/documents/'+uploaded.json()['document_id']
        assert pipeline.process_next(settings)
        state=client.get(route,headers=auth).json()
        assert state['status']=='failed' and len(calls)==1
        assert client.post(route+'/retry-cache',headers=auth,json={'reason':'Retry synthetic cached plan'}).status_code==428
        resumed=client.post(route+'/retry-cache',headers={**auth,'If-Match':state['etag']},json={'reason':'Retry synthetic cached plan'})
        assert resumed.status_code==202 and resumed.json()['revision']==state['revision']
        monkeypatch.setattr(pipeline,'render_html_document',original)
        assert pipeline.process_next(replace(settings,openai_api_key=None))
        review=client.get(route+'/review',headers=auth).json()
        assert review['status']=='needs_review' and review['artifact_sha256']
        assert len(calls)==1
        assert client.get(route+'/pdf',headers=auth).status_code==409


def test_unregistered_search_requires_locality_and_cache_key_preserves_it(source_settings):
    empty=source_settings.data_dir.parent/'empty.json'
    empty.write_text('{"sources":[]}')
    settings=replace(source_settings,media_sources_file=empty,media_openai_search_enabled=True)
    class LocationSource(FakeSource):
        def get(self,url,**kwargs):
            if url=='https://resort.example.test/':
                return Fetched(url,'text/html',b'<title>Example Resort Coast</title><p>Lisbon</p><section class="photo-gallery"><img src="/home-praia.jpg"></section>')
            return super().get(url,**kwargs)
    search=PhotoSearch(settings,'a',LocationSource())
    calls=[]
    search.web.discover=lambda place:calls.append(place) or [{'page_url':'https://resort.example.test/'}]
    result=search.search('Example Resort Coast',provider='openai',locality='Lisbon',kind='hotel')
    assert result['candidates'][0]['provider']=='openai_web_search'
    assert result['candidates'][0]['authority']=='web_candidate'
    assert calls==[{'name':'Example Resort Coast','locality':'Lisbon','kind':'hotel'}]
    assert search.search('Example Resort Coast',provider='openai',locality='Lisbon',kind='hotel')['cache_hit']
    with pytest.raises(MediaError) as error:
        search.search('Example Resort Coast',provider='openai',locality='Paris',kind='hotel')
    assert error.value.code=='MEDIA_NO_MATCH' and len(calls)==2
