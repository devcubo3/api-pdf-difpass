"""Customer flight readability without changing facts or repeating paid planning."""
import json
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pymupdf as fitz
import pytest

from app import ai_images
from app.ai_images import Page,Plan,Section
from app.customer_presentation import prepare_customer_plan
from app.html_rendering import critical_value_present
from app.main import create_app
from app.pipeline import process_next
from test_html_template import case


NOTE='(nome aparece truncado na fonte)'
SOURCE='''Flight details
07:50 ABC
09:55 XYZ
02h05
Origem: Cidade Alfa
Destino: Cidade Beta
Aeroporto Alfa FR…
Aeroporto Beta
Voo: 1234
22 nov
'''


def flight_fixture(*,note=NOTE):
    section=Section(title='Ida - 22 nov',tone='neutral',bullets=[
        'Cidade Alfa (ABC) 07:50 → Cidade Beta (XYZ) 09:55; duração: 02h05.',
        'Voo 1234. Origem indicada: Aeroporto Alfa FR… '+note+'.',
        'Destino: Aeroporto Beta.'])
    plan=Plan(title='Viagem sintética',omitted_blocks=[],pages=[Page(title='Seus voos',
        eyebrow='Ida e volta',introduction='Reserva demonstrativa.',sections=[section],code_id=None,
        critical_values=['22 nov','07:50 ABC','09:55 XYZ','02h05','Voo 1234'],source_block_ids=['p1-b1'])])
    extracted={'pages':[{'page':1,'blocks':[{'text':SOURCE}]}]}
    return plan,extracted


def test_flight_fields_are_organized_without_expanding_airport_or_losing_facts():
    plan,extracted=flight_fixture()
    before=plan.model_dump_json()
    normalized,flights,changes=prepare_customer_plan(plan,extracted)
    flight=flights[(0,0)]
    assert flight['departure']=={'city':'Cidade Alfa','code':'ABC','time':'07:50','airport':'Aeroporto Alfa FR…'}
    assert flight['arrival']=={'city':'Cidade Beta','code':'XYZ','time':'09:55','airport':'Aeroporto Beta'}
    assert flight['number']=='1234' and flight['duration']=='02h05'
    assert NOTE not in normalized.model_dump_json()
    assert plan.model_dump_json()==before
    assert any(c['kind']=='unsupported_editorial_note_removed' for c in changes)
    again,second,_=prepare_customer_plan(normalized,extracted)
    assert again==normalized and second==flights


def test_literal_source_note_and_material_restrictions_are_preserved():
    plan,extracted=flight_fixture()
    plan.pages[0].sections[0].bullets.append('Não reembolsável. Conexão exige troca de terminal.')
    extracted['pages'].append({'page':2,'blocks':[{'text':NOTE}]})
    normalized,flights,_=prepare_customer_plan(plan,extracted)
    assert NOTE in normalized.model_dump_json()
    assert 'Não reembolsável. Conexão exige troca de terminal.' in normalized.pages[0].sections[0].bullets


@pytest.mark.parametrize('change',[
    lambda p,e:p.pages[0].sections[0].bullets.__setitem__(0,'Cidade Alfa (ABC) 08:50 → Cidade Beta (XYZ) 09:55; duração: 02h05.'),
    lambda p,e:p.pages[0].sections[0].bullets.__setitem__(0,'Cidade Alfa (ABC) 25:50 → Cidade Beta (XYZ) 09:55; duração: 02h05.'),
    lambda p,e:p.pages[0].sections[0].bullets.__setitem__(1,'Voo 9999.'),
    lambda p,e:p.pages[0].sections[0].bullets.append('Voo 1234.'),
    lambda p,e:p.pages[0].sections[0].bullets.append('Saída: Cidade Alfa (ABC) 08:50'),
    lambda p,e:p.pages[0].sections[0].bullets.append(p.pages[0].sections[0].bullets[0]),
    lambda p,e:p.pages[0].sections[0].bullets.__setitem__(0,'O embarque será informado pela companhia.')])
def test_ambiguous_or_ungrounded_flights_keep_original_sentences(change):
    plan,extracted=flight_fixture(note='')
    change(plan,extracted)
    before=deepcopy(plan.pages[0].sections[0].bullets)
    normalized,flights,_=prepare_customer_plan(plan,extracted)
    assert flights=={} and normalized.pages[0].sections[0].bullets==before


def test_future_labeled_flights_have_the_same_layout_and_keep_other_instructions():
    plan,extracted=flight_fixture()
    plan.pages[0].sections[0].bullets=[
        'Saída: Cidade Alfa (ABC) 07:50', 'Chegada: Cidade Beta (XYZ) 09:55',
        'Voo: 1234','Duração: 02h05','Aeroporto de saída: Aeroporto Alfa FR…',
        'Aeroporto de chegada: Aeroporto Beta','Compareça com documento original.']
    normalized,flights,_=prepare_customer_plan(plan,extracted)
    assert flights[(0,0)]['extra']==['Compareça com documento original.']
    assert 'Compareça com documento original.' in normalized.pages[0].sections[0].bullets


def test_http_worker_renders_flight_fields_and_sanitizes_only_non_source_notes_using_cache(case,monkeypatch):
    settings,source,_,base=case
    source_doc=fitz.open(stream=source.read_bytes(),filetype='pdf')
    page=source_doc.new_page()
    page.insert_textbox(fitz.Rect(40,40,550,700),SOURCE,fontsize=12)
    content=source_doc.tobytes()
    source_doc.close()
    plan,_=flight_fixture()
    plan.pages[0].source_block_ids=['p2-b1']
    complete=base.model_copy(deep=True)
    complete.pages.extend(plan.pages)
    calls=[]
    def complete_plan(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(status='completed',output_text=complete.model_dump_json(),usage=None)
    monkeypatch.setattr(ai_images,'OpenAI',lambda **kw:SimpleNamespace(responses=SimpleNamespace(create=complete_plan)))
    auth={'Authorization':'Bearer reviewer'}
    with TestClient(create_app(settings)) as client:
        result=client.post('/v2/documents',headers={**auth,'Idempotency-Key':'customer-flight'},
            data={'generation_scope':'preview'},files={'input_pdf':('synthetic.pdf',content,'application/pdf')})
        route='/v2/documents/'+result.json()['document_id']
        assert process_next(settings)
        assert len(calls)==1
        assert 'não acrescente comentários' in calls[0]['instructions']
        state=client.get(route,headers=auth).json()
        result=client.post(route+'/template',headers={**auth,'If-Match':state['etag']},json={
            'reason':'Reutilizar plano sintético para conferir novos cartões de voo.','generation_scope':'full'})
        assert result.status_code==202
        assert process_next(replace(settings,openai_api_key=None))
        review=client.get(route+'/review',headers=auth).json()
        assert review['artifact_sha256'],review['issues']
        assert review['checks']['codes_valid'] and all(c['passed'] for c in review['checks']['template_text'])
        assert len(calls)==1
        pdf=client.get(route+'/review/pdf',headers=auth)
        with fitz.open(stream=pdf.content,filetype='pdf') as document:
            text='\n'.join(p.get_text(flags=fitz.TEXTFLAGS_TEXT|fitz.TEXT_INHIBIT_SPACES) for p in document)
            assert NOTE not in text
            assert 'Viaje tranquilo, Nós cuidamos de Você!' in text
            assert 'Próximo destino' not in text
            for value in ['Cidade Alfa','Cidade Beta','07:50','09:55','Voo 1234','02h05']:
                assert critical_value_present(value,text)
        assert any(c['kind']=='flight_fields_organized' for c in review['checks']['template_adjustments'])
