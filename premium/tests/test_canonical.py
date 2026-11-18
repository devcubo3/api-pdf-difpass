"""General evidence/relationship/HTTP tests; synthetic PDFs, zero provider calls."""
from dataclasses import replace
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pymupdf
import pytest

from app import ai_images
from app.config import Settings
from app.document_schema import Understanding,Entity,Service,Fact,Evidence,CodeAssociation,strict_schema
from app.document_validation import validate_understanding,contains
from app.extraction import extract_document
from app.main import create_app
from app.pipeline import process_next
from app.presentation import compile_presentation,public_context
from app.ai_images import Page,Plan,Section,AIError
from app.html_rendering import render_html_document
from app.media import PhotoPage
from app.reading_quality import merge_readings
from app.legacy_download import validate_legacy_url
from test_html_template import case


def evidence(extracted,needle):
    for page in extracted['pages']:
        for i,b in enumerate(page['blocks'],1):
            if needle in b['text']:
                return Evidence(block_id=f'p{page["page"]}-b{i}',quote=b['text'])
    raise AssertionError(needle)


def understood(extracted):
    guest=evidence(extracted,'TESTE VIAJANTE'); number=evidence(extracted,'000123456')
    product=evidence(extracted,'1 Day Park Ticket')
    return Understanding(entities=[Entity(id='person',role='person',name='TESTE VIAJANTE',evidence=[guest])],
        services=[Service(id='service',kind='ticket',name=None,entity_ids=['person'],evidence=[product])],
        facts=[Fact(id='number',service_id='service',entity_id='person',kind='ticket_number',
            label='Número do ingresso',raw_value='000123456',status='supported',evidence=[number])],
        code_associations=[CodeAssociation(code_id=c['id'],purpose='ticket',service_ids=['service'],
            holder_entity_ids=['person'],number_fact_ids=['number'],evidence=[guest,number]) for c in extracted['codes']])


def generic_case(case):
    settings,source,_,_=case
    settings=replace(settings,pipeline_version='canonical_v1')
    extracted=extract_document(source,settings.data_dir/'generic-codes',generic=True)
    return settings,source,extracted,understood(extracted)


def fake_provider(monkeypatch,document):
    calls=[]
    def create(**kwargs):
        calls.append(kwargs)
        assert kwargs['text']['format']['name']=='difftravel_understanding_v1'
        return SimpleNamespace(status='completed',output_text=document.model_dump_json(),
                               usage=SimpleNamespace(model_dump=lambda:{'input_tokens':1,'output_tokens':1}))
    monkeypatch.setattr(ai_images,'OpenAI',lambda **kwargs:SimpleNamespace(responses=SimpleNamespace(create=create)))
    return calls


def test_generic_extractor_does_not_use_destination_directory_or_ticket_wording(case):
    settings,source,extracted,document=generic_case(case)
    assert extracted['data']['document_type']=='unknown'
    assert not any(i['code']=='UNSUPPORTED_FORMAT' for i in extracted['issues'])
    assert 'purpose' not in extracted['codes'][0]
    assert extracted['codes'][0]['source_bbox'] is not None
    assert all('quality' in p for p in extracted['pages'])
    assert all('raw_text' in b for p in extracted['pages'] for b in p['blocks'])
    checks,issues=validate_understanding(document,extracted)
    assert not issues and checks['evidence_validation_passed']


def test_title_is_independent_but_wrong_person_number_or_quote_blocks(case):
    _,_,extracted,document=generic_case(case)
    _,issues=validate_understanding(document,extracted)
    plan,coverage=compile_presentation(document,extracted,issues)
    assert next(p for p in plan.pages if p.code_id).title=='Seu ingresso'
    assert coverage['residual_blocks_preserved']>0
    document.entities[0].name='PESSOA INVENTADA'
    _,issues=validate_understanding(document,extracted)
    assert {x['code'] for x in issues}>={'ENTITY_EVIDENCE_REVIEW'}
    document.entities[0].name='TESTE VIAJANTE'
    document.facts[0].raw_value='999999999'
    _,issues=validate_understanding(document,extracted)
    assert 'FACT_EVIDENCE_REVIEW' in {x['code'] for x in issues}
    assert 'CODE_ASSOCIATION_REVIEW' in {x['code'] for x in issues}


def test_unreadable_code_missing_association_and_invented_id_are_not_silently_accepted(case):
    _,_,extracted,document=generic_case(case)
    document.code_associations=[]
    document.unreadable_code_pages=[1]
    _,issues=validate_understanding(document,extracted)
    assert {i['code'] for i in issues}=={'CODE_UNREADABLE','CODE_ASSOCIATION_REVIEW'}
    document=understood(extracted)
    document.code_associations[0].code_id='invented'
    with pytest.raises(AIError): validate_understanding(document,extracted)


def test_same_page_different_subject_is_rejected_even_when_all_text_is_present(case):
    _,_,extracted,document=generic_case(case)
    document.entities.append(Entity(id='other',role='person',name='TESTE VIAJANTE',evidence=document.entities[0].evidence))
    document.code_associations[0].holder_entity_ids=['other']
    _,issues=validate_understanding(document,extracted)
    assert 'CODE_ASSOCIATION_REVIEW' in {i['code'] for i in issues}


def test_general_hotel_terms_do_not_prove_hotel_booking(case):
    _,_,extracted,document=generic_case(case)
    document.services[0].kind='hotel'
    _,issues=validate_understanding(document,extracted)
    assert 'SERVICE_FIELDS_REVIEW' in {i['code'] for i in issues}


@pytest.mark.parametrize('value,text,expected',[
    ('10','100',False),('10','10,50',False),('000123','0001234',False),
    ('000123','Bilhete: 000123',True),('10,50','R$ 10,50',True),('JOSÉ','José',True)])
def test_literal_matching_does_not_fuzzy_match_critical_values(value,text,expected):
    assert contains(value,text)==expected


@pytest.mark.parametrize('value,source,expected',[
    ('10,50','Valor: -R$ 10,50',False),('R$ 10,50','Valor: -R$ 10,50',False),
    ('-R$ 10,50','Valor: -R$ 10,50',True),('10,50','Valor: R$ 10,50',True),
    ('$10.00','Valor: $10.00,50',False)])
def test_financial_evidence_cannot_drop_sign_or_numeric_suffix(value,source,expected):
    assert contains(value,source,financial=True)==expected


@pytest.mark.parametrize('source,value,quote',[
    ('Valor: 100','10','10'),('Valor: -R$ 10,50','10,50','10,50')])
def test_short_quote_cannot_hide_the_original_numeric_context(source,value,quote):
    extracted={'data':{},'pages':[{'page':1,'blocks':[{'text':source,'bbox':[0,0,100,40]}]}],'codes':[]}
    doc=Understanding(facts=[Fact(id='f',service_id=None,entity_id=None,kind='amount',label='Valor',
        raw_value=value,status='supported',evidence=[Evidence(block_id='p1-b1',quote=quote)])])
    _,issues=validate_understanding(doc,extracted)
    assert 'FACT_EVIDENCE_REVIEW' in {i['code'] for i in issues}


def test_card_packing_keeps_two_original_codes_separate_and_all_values_visible():
    from app.pagination import coalesce_cards
    pages=[Page(title='Trechos',eyebrow='Viagem',introduction='',
        sections=[Section(title='Voo',tone='neutral',bullets=['Número ABC123'])],code_id=None,
        critical_values=['ABC123'],source_block_ids=['p1-b1','p2-b1']),
        Page(title='Primeiro código',eyebrow='Viagem',introduction='',sections=[],
             code_id='p1-code1',critical_values=[],source_block_ids=['p1-b1']),
        Page(title='Segundo código',eyebrow='Viagem',introduction='',sections=[],
             code_id='p2-code1',critical_values=[],source_block_ids=['p2-b1'])]
    result=coalesce_cards(Plan(title='Sua viagem',pages=pages,omitted_blocks=[]))
    assert [p.code_id for p in result.pages if p.code_id]==['p1-code1','p2-code1']
    assert any('ABC123' in p.critical_values for p in result.pages)


def test_unlabelled_fraction_or_date_is_not_discarded_as_page_number():
    extracted={'data':{},'pages':[{'page':1,'blocks':[{'text':'1/2','bbox':[0,0,100,20]}]}],'codes':[]}
    doc=Understanding()
    plan,_=compile_presentation(doc,extracted,[])
    assert not plan.omitted_blocks
    assert any('1/2' in s.bullets for p in plan.pages for s in p.sections)


def test_schema_is_strict_recursively_and_does_not_request_layout():
    schema=strict_schema(Understanding)
    for node in [schema,*schema['$defs'].values()]:
        assert node['required']==list(node['properties'])
        assert node['additionalProperties'] is False
    assert 'pages' not in schema['properties'] and 'critical_values' not in schema['properties']


def test_free_form_label_cannot_add_unsupported_benefits(case):
    _,_,extracted,document=generic_case(case)
    document.facts[0].kind='custom_reference'
    document.facts[0].label='Entrada gratuita garantida'
    document.services[0].kind='other'
    _,issues=validate_understanding(document,extracted)
    plan,_=compile_presentation(document,extracted,issues)
    visible='\n'.join(b for p in plan.pages for s in p.sections for b in s.bullets)
    assert 'Entrada gratuita garantida' not in visible
    assert '000123456' in visible


@pytest.mark.parametrize('types',[None,{},[],['Organization','Hotel'],['Hotel',{}],'Hotel'])
def test_photo_jsonld_heterogeneous_types_do_not_crash(types):
    raw=json.dumps({'@type':types,'name':'Example Hotel','image':'https://example.org/hotel.jpg'})
    PhotoPage('<script type="application/ld+json">'+raw+'</script>','https://example.org/')


def test_mixed_reading_preserves_native_values_and_adds_only_uncovered_regions():
    native=[{'text':'Booking 000123','raw_text':'Booking 000123','bbox':[0,0,100,20]}]
    ocr=[{'text':'Wrong 999','bbox':[0,0,100,20]},{'text':'Hotel contact','bbox':[0,80,100,100]}]
    blocks,method=merge_readings(native,ocr)
    assert method=='mixed'
    assert [b['text'] for b in blocks]==['Booking 000123','Hotel contact']


def test_http_worker_canonical_legacy_button_and_cache_only_correction(case,monkeypatch):
    settings,source,extracted,document=generic_case(case)
    settings=replace(settings,require_legacy_pdf_url=True,legacy_pdf_hosts_json='{"a":["files.example.test"]}')
    calls=fake_provider(monkeypatch,document)
    auth={'Authorization':'Bearer reviewer'}
    with TestClient(create_app(settings)) as client:
        def upload(url='https://files.example.test/standard.pdf',key='canonical'):
            return client.post('/v2/documents',headers={**auth,'Idempotency-Key':key},
                data={'legacy_pdf_url':url},files={'input_pdf':('any-provider.pdf',source.read_bytes(),'application/pdf')})
        assert upload(url='https://unapproved.test/file.pdf').status_code==422
        created=upload(); assert created.status_code==202,created.text
        assert upload(url='https://files.example.test/other.pdf').status_code==409
        route='/v2/documents/'+created.json()['document_id']
        assert process_next(settings)
        review=client.get(route+'/review',headers=auth).json()
        assert review['checks'].get('validations_passed'),review['issues']
        assert review['data']['document_type']=='ticket'
        assert review['data']['processing_stage']=='review'
        assert review['data']['understanding_snapshot']
        assert review['checks']['codes_valid']
        assert review['checks']['original_download']['kind']=='legacy_pdf'
        with pymupdf.open(stream=client.get(route+'/review/pdf',headers=auth).content,filetype='pdf') as pdf:
            assert pdf.embfile_count()==0
            assert any(x.get('uri')=='https://files.example.test/standard.pdf' for x in pdf[-1].get_links())
            assert 'Link disponível até' not in pdf[-1].get_text()
            assert 'TESTE VIAJANTE' in '\n'.join(p.get_text() for p in pdf)
        assert client.get(route+'/pdf',headers=auth).status_code==409
        patch={'reason':'Conferência sintética da fonte','changes':[{'fact_id':'number','raw_value':'000123456',
                'evidence':[e.model_dump() for e in document.facts[0].evidence]}]}
        assert client.patch(route+'/facts',headers=auth,json=patch).status_code==428
        assert client.patch(route+'/facts',headers={**auth,'If-Match':review['etag']},json={
            **patch,'changes':[{**patch['changes'][0],'raw_value':'999999999'}]}).status_code==422
        response=client.patch(route+'/facts',headers={**auth,'If-Match':review['etag']},json=patch)
        assert response.status_code==202,response.text
        def deny(**kwargs): raise AssertionError('No new provider client')
        monkeypatch.setattr(ai_images,'OpenAI',deny)
        assert process_next(replace(settings,openai_api_key=None))
        updated=client.get(route+'/review',headers=auth).json()
        assert updated['revision']==2 and updated['checks']['validations_passed'],updated['issues']
        assert len(calls)==1
        assert updated['events'][-1]['action']=='correct_facts'


def test_optional_photo_crash_preserves_document_and_sanitizes_diagnostic(case,monkeypatch):
    settings,source,extracted,document=generic_case(case)
    # A source-grounded public entity makes the media stage execute.
    e=evidence(extracted,'Universal Orlando')
    document.services[0].name='Universal Orlando'
    document.services[0].evidence.append(e)
    document.entities.append(Entity(id='place',role='location',name='Universal Orlando',evidence=[e]))
    document.services[0].entity_ids.append('place')
    fake_provider(monkeypatch,document)
    def fail(*args,**kwargs): raise TypeError('private-value-must-not-appear')
    monkeypatch.setattr('app.pipeline.PhotoSearch.search',fail)
    settings=replace(settings,media_mode='search')
    with TestClient(create_app(settings)) as client:
        auth={'Authorization':'Bearer reviewer'}
        created=client.post('/v2/documents',headers={**auth,'Idempotency-Key':'photo-failure'},
            files={'input_pdf':('synthetic.pdf',source.read_bytes(),'application/pdf')})
        assert process_next(settings)
        r=client.get('/v2/documents/'+created.json()['document_id']+'/review',headers=auth).json()
        assert r['checks']['validations_passed'],r['issues']
        assert 'MEDIA_ENRICHMENT_FAILED' in {i['code'] for i in r['issues']}
        assert 'private-value' not in json.dumps(r['data']['media_diagnostic'])


def test_measured_pagination_continues_long_content_without_shrinking(case,tmp_path):
    settings,_,extracted,_=case
    extracted['data']['pipeline_version']='canonical_v1'
    text=' '.join(f'Condição {i}: preservar obrigação e ressalva.' for i in range(95))
    plan=Plan(title='Viagem sintética',omitted_blocks=[],pages=[Page(title='Condições',eyebrow='Sua viagem',
        introduction='',sections=[Section(title='Regras',tone='neutral',bullets=[text])],
        code_id=None,critical_values=[],source_block_ids=['p1-b1'])])
    planner=SimpleNamespace(ledger=SimpleNamespace(summary=lambda:[]),cache_hits=[])
    result=render_html_document(settings,plan,{},extracted,tmp_path/'paginated',planner)
    assert result['page_count']>1
    assert all(x['passed'] and not x['compact'] for x in result['template_layout_checks'])
    with pymupdf.open(result['pdf_path']) as pdf:
        complete='\n'.join(p.get_text() for p in pdf)
        for i in range(95): assert f'Condição {i}:' in complete


def test_measured_layout_pulls_a_short_following_card_into_spare_space(case,tmp_path):
    settings,_,extracted,_=case
    extracted['data']['pipeline_version']='canonical_v1'
    plan=Plan(title='Condições',omitted_blocks=[],pages=[Page(title='Condições',eyebrow='Viagem',
        introduction='',code_id=None,source_block_ids=['p1-b1'],critical_values=['REF-A','REF-B'],sections=[
            Section(title='Primeira',tone='neutral',bullets=['REF-A '+('Obrigação e ressalva. '*75)]),
            Section(title='Contato',tone='neutral',bullets=['REF-B canal de contato.']),
            Section(title='Última',tone='neutral',bullets=['Informação adicional. '*75])])])
    planner=SimpleNamespace(ledger=SimpleNamespace(summary=lambda:[]),cache_hits=[])
    result=render_html_document(settings,plan,{},extracted,tmp_path/'compact',planner)
    assert result['page_count']>=2
    assert 'Contato' in [s['title'] for s in result['content_plan']['pages'][0]['sections']]
    assert all(x['passed'] for x in result['template_text_checks'])


@pytest.mark.parametrize('kind,field,body',[
    ('flight','flight_number','Bilhete eletrônico\nCompanhia Boreal\nVoo XY 010'),
    ('hotel','checkin','Property: Oak Lodge\nCheck-in: 31/10/2026'),
    ('car_rental','pickup','Locadora: Estrada Livre\nRetirada: 31/10/2026\nReconfirme check-in e check-out no hotel.'),
    ('insurance','policy_number','Seguro de viagem\nPolicy number: 000987'),
    ('cruise','cabin','Cruise\nCabine: 000987'),
    ('other','custom_field','Passeio reservado\nArmário: 000987'),
])
def test_service_schema_and_preservation_do_not_require_a_supplier_parser(kind,field,body):
    value='XY 010' if kind=='flight' else ('31/10/2026' if kind in {'hotel','car_rental'} else '000987')
    extracted={'data':{},'pages':[{'page':1,'blocks':[{'text':body,'bbox':[10,10,500,200]}]}],'codes':[]}
    ev=Evidence(block_id='p1-b1',quote=body)
    doc=Understanding(services=[Service(id='s',kind=kind,name=None,entity_ids=[],evidence=[ev])],
        facts=[Fact(id='f',service_id='s',entity_id=None,kind=field,label='Informação',
                    raw_value=value,status='supported',evidence=[ev])])
    _,issues=validate_understanding(doc,extracted)
    assert not issues
    plan,coverage=compile_presentation(doc,extracted,issues)
    text='\n'.join(b for p in plan.pages for s in p.sections for b in s.bullets)
    assert body in text and value in text


@pytest.mark.parametrize('url',[
    'http://files.example.test/doc.pdf','https://files.example.test.evil.test/doc.pdf',
    'https://other.example.test/doc.pdf','https://user:pass@files.example.test/doc.pdf',
    'https://127.0.0.1/doc.pdf','https://files.example.test:444/doc.pdf'])
def test_legacy_url_checks_exact_tenant_origin_and_scheme(url):
    settings=Settings(legacy_pdf_hosts_json='{"a":["files.example.test"]}')
    with pytest.raises(ValueError): validate_legacy_url(settings,'a',url)
    with pytest.raises(ValueError): validate_legacy_url(settings,'b','https://files.example.test/doc.pdf')


def test_public_place_is_a_grounded_entity_separate_from_product_name(case):
    _,_,extracted,document=generic_case(case)
    e=evidence(extracted,'Universal Orlando')
    document.entities.append(Entity(id='park',role='location',name='Universal Orlando',evidence=[e]))
    document.services[0].entity_ids.append('park')
    _,issues=validate_understanding(document,extracted)
    context=public_context(document,issues)
    assert context['places'][0]['name']=='Universal Orlando' and context['primary_index']==0
    document.primary_place_id='person'
    assert public_context(document,issues)['places'][0]['name']!='TESTE VIAJANTE'


def test_structured_flight_card_preserves_dates_airports_and_separate_legs(case,tmp_path):
    settings,_,extracted,_=case
    settings=replace(settings,pipeline_version='canonical_v1')
    body='Voo XY 0123\nOrigem: Alfa (ABC)\nDestino: Beta (XYZ)\nSaída: 10:15\nChegada: 12:40\nData: 31/10/2026\nLocalizador da companhia: AIR123\nReserva: BOOK123'
    extracted={'data':{'pipeline_version':'canonical_v1'},'pages':[{'page':1,
        'blocks':[{'text':body,'bbox':[0,0,500,500]}]}],'codes':[]}
    ev=Evidence(block_id='p1-b1',quote=body)
    values={'flight_number':'XY 0123','origin':'Alfa (ABC)','destination':'Beta (XYZ)',
            'departure_time':'10:15','arrival_time':'12:40','departure_date':'31/10/2026'}
    doc=Understanding(services=[Service(id='leg',kind='flight',name=None,entity_ids=[],evidence=[ev])],facts=[
        Fact(id=k,service_id='leg',entity_id=None,kind=k,label=k,raw_value=v,status='supported',evidence=[ev]) for k,v in values.items()])
    for key,label,value in [('air','Localizador da companhia','AIR123'),('book','Localizador da reserva','BOOK123')]:
        doc.facts.append(Fact(id=key,service_id='leg',entity_id=None,kind='booking_reference',label=label,
            raw_value=value,status='supported',evidence=[ev]))
    _,issues=validate_understanding(doc,extracted); assert not issues
    plan,coverage=compile_presentation(doc,extracted,issues)
    assert extracted['data']['structured_flights']
    planner=SimpleNamespace(ledger=SimpleNamespace(summary=lambda:[]),cache_hits=[])
    rendered=render_html_document(settings,plan,coverage,extracted,tmp_path/'flight',planner)
    assert all(x['passed'] for x in rendered['template_text_checks'])
    markup=(tmp_path/'flight/document.html').read_text(encoding='utf-8')
    assert 'flight-card' in markup and 'terminal-time' in markup
    assert 'Localizador da companhia' in markup and 'Localizador da reserva' in markup


def test_identical_condition_text_is_presented_once_with_both_material_categories():
    from app.document_schema import Condition
    text='Documentos originais e check-in com 2 horas de antecedência.'
    ev=Evidence(block_id='p1-b1',quote=text)
    doc=Understanding(services=[Service(id='s',kind='other',name=None,entity_ids=[],evidence=[ev])],
        conditions=[Condition(id='c1',service_id=None,kind='checkin',evidence=[ev]),
                    Condition(id='c2',service_id=None,kind='documentation',evidence=[ev])])
    extracted={'data':{},'pages':[{'page':1,'blocks':[{'text':text,'bbox':[0,0,500,100]}]}],'codes':[]}
    _,issues=validate_understanding(doc,extracted)
    plan,_=compile_presentation(doc,extracted,issues)
    bullets=[b for p in plan.pages for s in p.sections for b in s.bullets]
    assert bullets.count(text)==1
    assert any('Check-in' in s.title and 'Documentação' in s.title for p in plan.pages for s in p.sections)


def test_tampered_understanding_snapshot_is_not_regenerated_or_billed(case,monkeypatch):
    settings,source,_,document=generic_case(case)
    calls=fake_provider(monkeypatch,document)
    with TestClient(create_app(settings)) as client:
        auth={'Authorization':'Bearer reviewer'}
        r=client.post('/v2/documents',headers={**auth,'Idempotency-Key':'snapshot'},
            files={'input_pdf':('synthetic.pdf',source.read_bytes(),'application/pdf')})
        route='/v2/documents/'+r.json()['document_id']
        assert process_next(settings)
        before=client.get(route+'/review',headers=auth).json()
        from pathlib import Path
        Path(before['data']['understanding_snapshot']['path']).write_bytes(b'{}')
        assert client.post(route+'/retry-cache',headers={**auth,'If-Match':before['etag']},json={'reason':'Synthetic tamper test'}).status_code==202
        assert process_next(replace(settings,openai_api_key=None))
        result=client.get(route+'/review',headers=auth).json()
        assert result['issues'][0]['code']=='UNDERSTANDING_SNAPSHOT_INVALID'
        assert len(calls)==1
