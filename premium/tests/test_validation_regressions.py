"""Synthetic regressions for text geometry, monetary boundaries and evidence."""
from copy import deepcopy
from types import SimpleNamespace
import os

import pymupdf as fitz
import pytest
from fastapi.testclient import TestClient

from app.ai_images import AIError,Page,Plan,Section
from app.config import Settings
from app.document_schema import Understanding,Entity,Evidence,Fact,Service
from app.document_validation import contains,validate_understanding
from app.entity_evidence import repair_public_entity_evidence
from app.html_rendering import check_pdf_text,render_html_document
from app.main import create_app
from app.pdf_text import extract_printed_text
from app.pipeline import process_next
from test_canonical import generic_case,fake_provider
from test_html_template import case


@pytest.mark.parametrize('source,expected',[
    ('Taxas para esta reserva116.42 Real Brasileiro pagável na chegada.',True),
    ('Taxas: 2116.42 Real Brasileiro',False),
    ('Taxas: -116.42 Real Brasileiro',False),
    ('Taxas: -R$116.42 Real Brasileiro',False),
    ('Taxas: 116.42 Real Brasileiro adicional',True),
    ('Taxas: 116.420 Real Brasileiro',False),
    ('Taxas: 116.42 USD',False),
    ('Taxas: 0.116.42 Real Brasileiro',False),
    ('Taxas: 116.42 Real Brasileiro2',False),
])
def test_monetary_label_boundary_preserves_full_currency_digits_and_sign(source,expected):
    assert contains('116.42 Real Brasileiro',source,financial=True)==expected


def test_financial_quote_cannot_hide_a_glued_longer_number():
    source={'pages':[{'page':1,'blocks':[{'text':'Taxas: 2116.42 Real Brasileiro'}]}],'codes':[]}
    doc=Understanding(facts=[Fact(id='amount',service_id=None,entity_id=None,kind='amount',
        label='Taxas',raw_value='116.42 Real Brasileiro',status='supported',
        evidence=[Evidence(block_id='p1-b1',quote='116.42 Real Brasileiro')])])
    assert any(i['code']=='FACT_EVIDENCE_REVIEW' for i in validate_understanding(doc,source)[1])


def test_same_cited_block_can_supply_unchanged_public_locality():
    source={'pages':[{'page':1,'blocks':[{'text':'Cidade Exemplo (BR)\nAEROPORTO EXEMPLO'}]}],'codes':[]}
    doc=Understanding(entities=[Entity(id='airport',role='location',name='AEROPORTO EXEMPLO',
        locality='Cidade Exemplo (BR)',evidence=[Evidence(block_id='p1-b1',quote='AEROPORTO EXEMPLO')])])
    before=deepcopy(source);identity=doc.entities[0].model_dump()
    assert repair_public_entity_evidence(doc,source)[0]['policy']=='cited_block_evidence_v1'
    assert not any(i.get('field')=='entities.airport' for i in validate_understanding(doc,source)[1])
    after=doc.entities[0].model_dump();after['evidence']=identity['evidence']
    assert after==identity and before==source
    assert repair_public_entity_evidence(doc,source)==[]


@pytest.mark.parametrize('change',['locality','quote','person'])
def test_same_block_repair_cannot_invent_locality_quote_or_repair_people(change):
    source={'pages':[{'page':1,'blocks':[{'text':'Cidade Exemplo\nAeroporto Exemplo'}]}],'codes':[]}
    entity=Entity(id='e',role='location',name='Aeroporto Exemplo',locality='Cidade Exemplo',
                  evidence=[Evidence(block_id='p1-b1',quote='Aeroporto Exemplo')])
    if change=='locality': entity.locality='Cidade Inventada'
    if change=='quote': entity.evidence[0].quote='Texto Inventado'
    if change=='person': entity.role='person'
    doc=Understanding(entities=[entity]);before=doc.model_dump()
    assert repair_public_entity_evidence(doc,source)==[]
    assert doc.model_dump()==before
    assert any(i.get('field')=='entities.e' for i in validate_understanding(doc,source)[1])


@pytest.mark.parametrize('change',[None,'different_heading','nonadjacent','different_page','person','extra_claim'])
def test_contact_title_needs_two_exact_adjacent_headings(change):
    source={'pages':[{'page':1,'blocks':[{'text':'Assistência durante a viagem\nDescrição geral.'},
        {'text':'Horário Comercial\nTelefone: 0000-0000'}]}],'codes':[]}
    entity=Entity(id='contact',role='contact',name='Assistência durante a viagem — Horário Comercial',
        evidence=[Evidence(block_id='p1-b2',quote='Horário Comercial\nTelefone: 0000-0000')])
    if change=='different_heading': source['pages'][0]['blocks'][0]['text']='Outro título'
    if change=='nonadjacent': source['pages'][0]['blocks'].insert(1,{'text':'Outro assunto'});entity.evidence[0].block_id='p1-b3'
    if change=='different_page': source['pages'].append({'page':2,'blocks':[source['pages'][0]['blocks'].pop()]});entity.evidence[0].block_id='p2-b1'
    if change=='person': entity.role='person'
    if change=='extra_claim': entity.name+=' sem custos'
    doc=Understanding(entities=[entity]);before=deepcopy(source);name=entity.name
    repairs=repair_public_entity_evidence(doc,source)
    invalid=any(i.get('field')=='entities.contact' for i in validate_understanding(doc,source)[1])
    assert invalid==(change is not None)
    assert bool(repairs)==(change is None)
    assert source==before and entity.name==name


def test_rendered_source_fields_keep_word_gaps_and_tracked_heading(tmp_path):
    settings=Settings(render_mode='html_template',pipeline_version='canonical_v1',
        html_engine=os.getenv('PDF_TEST_HTML_ENGINE','chromium'),render_dpi=120,
        html_browser_channel='chrome' if os.name=='nt' else None)
    page=Page(title='Sua hospedagem',eyebrow='Sua viagem',introduction='',code_id=None,
        sections=[Section(title='Dados preservados',tone='neutral',bullets=[
            'Hotel Exemplo\nRua das Flores, 10', 'Pessoa Exemplo\nAdulto',
            'Outra Pessoa\nAdulto', 'Taxas: 116.42 Real Brasileiro'])],
        critical_values=['Hotel Exemplo','Rua das Flores, 10','Pessoa Exemplo','Adulto','116.42 Real Brasileiro'],
        source_block_ids=['p1-b1'])
    plan=Plan(title='Sua hospedagem',pages=[page],omitted_blocks=[])
    extracted={'data':{'pipeline_version':'canonical_v1'},'codes':[],
               'pages':[{'page':1,'blocks':[{'text':'Dados sintéticos'}]}]}
    planner=SimpleNamespace(ledger=SimpleNamespace(summary=lambda:[]),cache_hits=[])
    result=render_html_document(settings,plan,{},extracted,tmp_path,planner)
    assert all(x['passed'] and x['word_coverage']==1 for x in result['template_text_checks'])
    with fitz.open(result['pdf_path']) as document:
        text=extract_printed_text(document[0],result['template_layout_checks'][0]['text_tracking_boxes'])
        assert check_pdf_text(text,page)['passed']
        # Source omissions and changed monetary values still fail at full coverage.
        assert not check_pdf_text(text.replace('Outra Pessoa',''),page)['passed']
        assert not check_pdf_text(text.replace('116.42','116.43'),page)['passed']


def test_render_failure_retains_prior_evidence_issues_timings_and_safe_page_checks(case,monkeypatch):
    settings,source,extracted,document=generic_case(case)
    document.entities[0].name='PESSOA INVENTADA'
    calls=fake_provider(monkeypatch,document)
    def fail(*args,**kwargs):
        raise AIError('HTML_TEXT_MISMATCH','Conferir texto.',checks={'html_engine':'synthetic',
            'template_text':[{'page':1,'passed':False,'missing_word_count':1}]})
    monkeypatch.setattr('app.pipeline.render_html_document',fail)
    auth={'Authorization':'Bearer reviewer'}
    with TestClient(create_app(settings)) as client:
        created=client.post('/v2/documents',headers={**auth,'Idempotency-Key':'diagnostic-failure'},
            files={'input_pdf':('synthetic.pdf',source.read_bytes(),'application/pdf')})
        assert created.status_code==202
        assert process_next(settings)
        route='/v2/documents/'+created.json()['document_id']
        review=client.get(route+'/review',headers=auth).json()
        assert review['status']=='needs_review'
        assert {i['code'] for i in review['issues']}>={'HTML_TEXT_MISMATCH','ENTITY_EVIDENCE_REVIEW'}
        assert review['checks']['template_text'][0]['missing_word_count']==1
        assert not review['checks']['canonical']['evidence_validation_passed']
        assert review['data']['processing_timings']['worker_total_seconds']>=0
        assert {'extraction','understanding','validation','media','rendering'}<=set(
            review['data']['processing_timings']['stages_seconds'])
        assert client.get(route+'/pdf',headers=auth).status_code==409
        assert len(calls)==1


def test_independent_overflows_are_remeasured_together_without_losing_cards():
    from app.pagination import paginate_measured
    pages=[Page(title='Código',eyebrow='Sua viagem',introduction='',code_id=f'code-{i}',
        source_block_ids=[f'p{i+1}-b1'],critical_values=[],
        sections=[Section(title='Dados',tone='neutral',bullets=[f'Informação {i}/{j}']) for j in range(4)])
        for i in range(8)]
    calls=[]
    def measure(plan):
        calls.append(len(plan.pages))
        return [{'passed':len(p.sections)<=2} for p in plan.pages], 'synthetic markup'
    result,layout,_=paginate_measured(Plan(title='Viagem',pages=pages,omitted_blocks=[]),measure,40)
    assert len(calls)<6  # Previously one full-document measurement per failure.
    assert all(item['passed'] for item in layout)
    assert [p.code_id for p in result.pages if p.code_id]==[p.code_id for p in pages]
    assert [s.bullets for p in result.pages for s in p.sections]==[s.bullets for p in pages for s in p.sections]


def test_displacement_from_a_previous_overflow_does_not_split_an_atomic_code_page():
    from app.pagination import paginate_measured
    first=Page(title='Dados',eyebrow='Viagem',introduction='',code_id='first',
        source_block_ids=['p1-b1'],critical_values=[],
        sections=[Section(title='Dados',tone='neutral',bullets=['Primeiro']),
                  Section(title='Dados',tone='neutral',bullets=['Segundo'])])
    atomic=Page(title='Código',eyebrow='Viagem',introduction='',code_id='atomic',
        source_block_ids=['p2-b1'],critical_values=[],sections=[])
    def measure(plan):
        preceding_overflow=False;layout=[]
        for item in plan.pages:
            overflow=len(item.sections)>1
            layout.append({'passed':not overflow and not preceding_overflow,'split_needed':overflow})
            preceding_overflow|=overflow
        return layout,'synthetic markup'
    result,layout,_=paginate_measured(Plan(title='Viagem',pages=[first,atomic],omitted_blocks=[]),measure,10)
    assert all(item['passed'] for item in layout)
    assert len(result.pages)==3
    assert result.pages[-1].code_id=='atomic' and result.pages[-1].sections==[]
