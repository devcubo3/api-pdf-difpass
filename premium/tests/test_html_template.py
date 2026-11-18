"""Real browser/PDF tests with synthetic travellers and a fake planning response.

No provider calls, valid tickets or customer data are used by this suite.
"""
from dataclasses import replace
import json
import os
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pymupdf as fitz
import pytest
from sqlalchemy import select

from app import ai_images
from app.ai_images import AIError, ImagePipeline, Page, Plan, Section
from app.config import Settings
from app.db import Database, ai_calls
from app.extraction import extract_document
from app.html_rendering import (build_html, render_html_document, critical_value_present,
                                reconcile_source_locators)
from app.main import create_app
from app.pipeline import process_next
from test_api import synthetic_pdf


def synthetic_plan(extracted):
    code=extracted['codes'][0]
    ids=[f'p{p["page"]}-b{i+1}' for p in extracted['pages'] for i,_ in enumerate(p['blocks'])]
    return Plan(title='Universal sintético',omitted_blocks=[],pages=[Page(
        title=code['label'],eyebrow='Reserva sintética',introduction='Exemplo sem valor de ingresso.',
        sections=[Section(title='Dados do ingresso',tone='neutral',bullets=[f'Ingresso {code["ticket_number"]}']),
                  Section(title='Uso',tone='included',bullets=['Uso demonstrativo confirmado.']),
                  Section(title='Condições',tone='attention',bullets=['Documento de teste, sem validade.'])],
        code_id=code['id'],critical_values=[code['label'],code['ticket_number']],source_block_ids=ids),
        Page(title='Condições da experiência',eyebrow='Informações úteis',
            introduction='Conferência de paginação e conteúdo.',
            sections=[Section(title='Detalhes',tone='neutral',bullets=['Sem validade comercial.'])],
            code_id=None,critical_values=[],source_block_ids=ids[:1])])


@pytest.fixture
def case(tmp_path):
    settings=Settings(data_dir=tmp_path/'data',database_url=f'sqlite:///{tmp_path.as_posix()}/state.db',
        render_mode='html_template',public_base_url='https://premium.example.test',html_browser_channel=os.getenv('PDF_TEST_HTML_BROWSER_CHANNEL') or (
            'chrome' if os.name=='nt' else None),worker_timeout_seconds=7200,
        openai_api_key='fake-no-provider-access',tokens_json=json.dumps({
            'reviewer':{'tenant_id':'a','subject':'synthetic','roles':['upload','read','review']},
            'reader':{'tenant_id':'a','subject':'reader','roles':['read']},
            'other':{'tenant_id':'b','subject':'other','roles':['upload','read','review']}}))
    source=tmp_path/'synthetic.pdf'
    source.write_bytes(synthetic_pdf())
    extracted=extract_document(source,tmp_path/'codes')
    return settings,source,extracted,synthetic_plan(extracted)


def test_html_http_worker_preserves_text_codes_and_reuses_plan_without_key(case,monkeypatch):
    settings,source,_,_=case
    calls=[]
    class Responses:
        def create(self,**kwargs):
            calls.append('plan')
            # The synthetic response is derived from synthetic evidence, never a customer PDF.
            extracted=extract_document(source,settings.data_dir/'synthetic-codes')
            plan=synthetic_plan(extracted)
            return SimpleNamespace(status='completed',output_text=plan.model_dump_json(),
                                   usage=SimpleNamespace(model_dump=lambda:{'input_tokens':1}))
    fake=SimpleNamespace(responses=Responses())  # No images method: accidental image calls fail.
    monkeypatch.setattr(ai_images,'OpenAI',lambda **kwargs:fake)
    auth={'Authorization':'Bearer reviewer'}
    app=create_app(settings)
    with TestClient(app) as client:
        upload=client.post('/v2/documents',headers={**auth,'Idempotency-Key':'html-preview'},
            data={'generation_scope':'preview'},files={'input_pdf':('synthetic.pdf',source.read_bytes(),'application/pdf')})
        assert upload.status_code==202
        route='/v2/documents/'+upload.json()['document_id']
        assert process_next(settings)
        review=client.get(route+'/review',headers=auth).json()
        assert review['checks']['render_mode']=='html_template'
        assert review['checks'].get('preview_only'),review['issues']
        assert review['checks']['codes_valid']
        assert review['checks']['template_text'][0]['word_coverage']==1
        assert not review['checks']['image_generation_performed']
        assert review['checks']['rendered_pages']==1 and review['checks']['planned_pages']==2
        etag=client.get(route,headers=auth).json()['etag']
        approve=client.post(route+'/review/decision',headers={**auth,'If-Match':etag},json={
            'action':'approve','revision':review['revision'],'artifact_sha256':review['artifact_sha256']})
        assert approve.status_code==409  # Preview cannot be delivered as a complete document.
        assert client.post(route+'/template',headers=auth,json={'reason':'test'}).status_code==428
        assert client.post(route+'/template',headers={'Authorization':'Bearer reader','If-Match':etag},
                           json={'reason':'test'}).status_code==403
        assert client.post(route+'/template',headers={'Authorization':'Bearer other','If-Match':etag},
                           json={'reason':'test'}).status_code==404
        assert client.post(route+'/template',headers={**auth,'If-Match':'stale'},
                           json={'reason':'test'}).status_code==409
        assert client.post(route+'/template',headers={**auth,'If-Match':etag},
                           json={'reason':'Cached full template','generation_scope':'full'}).status_code==202
        # Cache-only generation works without an OpenAI key/client.
        def no_provider(**kwargs):
            raise AssertionError('Provider client must not be created')
        monkeypatch.setattr(ai_images,'OpenAI',no_provider)
        assert process_next(replace(settings,openai_api_key=None))
        complete=client.get(route+'/review',headers=auth).json()
        assert complete['checks']['validations_passed']
        assert complete['checks']['rendered_pages']==2
        assert complete['checks']['ai_cache_hits']==['plan']
        assert not complete['checks']['preview_only']
        assert calls==['plan']
        assert client.get(route+'/pdf',headers=auth).status_code==409
        pdf=client.get(route+'/review/pdf',headers=auth)
        assert pdf.status_code==200
        with fitz.open(stream=pdf.content,filetype='pdf') as document:
            assert len(document)==2 and document.embfile_count()==0
            assert '000123456' in document[0].get_text()
            assert 'Condições da experiência' in document[1].get_text()
            assert len(document[0].get_text())>150  # Genuine text layer, not a page raster.
        with app.state.db.engine.connect() as connection:
            assert len(connection.execute(select(ai_calls)).all())==1


def test_html_escapes_source_markup_and_does_not_load_model_asset_paths(case):
    settings,_,extracted,plan=case
    plan.pages[0].sections[0].bullets=['<script>alert(1)</script><img src="https://invalid.test/secret">']
    markup=build_html(plan,extracted['codes'],{'hero_asset':'../../private.env'},settings.brand_dir)
    assert '<script>' not in markup
    assert '<img src="https://invalid.test' not in markup
    assert '&lt;script&gt;' in markup
    assert "default-src 'none'" in markup
    assert '../../private.env' not in markup


@pytest.mark.parametrize('size', [(1400,400), (900,650)])
def test_diagonal_photo_has_same_full_bleed_header_on_every_page(case, tmp_path, size):
    import hashlib
    from PIL import Image, ImageDraw

    settings,_,extracted,plan=case
    color=(49,133,98)
    photo=Image.new('RGB',size,color)
    drawing=ImageDraw.Draw(photo)
    drawing.rectangle((size[0]*.43,size[1]*.40,size[0]*.57,size[1]*.60),fill='#c57f48')
    path=tmp_path/'panorama.jpg'
    photo.save(path,quality=95)
    asset={'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
           'credit':'Sintético','source_page_url':'https://photos.example.test/panorama'}
    planner=SimpleNamespace(ledger=SimpleNamespace(summary=lambda:[]),cache_hits=[])
    coverage={}
    result=render_html_document(settings,plan,coverage,extracted,tmp_path/'full',planner,media_asset=asset)
    preview=render_html_document(settings,plan,coverage,extracted,tmp_path/'preview',planner,
                                 scope='preview',media_asset=asset)
    headers=[row['header'] for row in result['template_layout_checks']+preview['template_layout_checks']]
    assert len(headers)==3 and all(h['passed'] and h['photo_visible'] and h==headers[0] for h in headers)
    assert headers[0]['box'][3]==pytest.approx(62*96/25.4,abs=.1)
    bboxes=[]
    for pdf_path in (result['pdf_path'],preview['pdf_path']):
        with fitz.open(pdf_path) as pdf:
            for page in pdf:
                # Chromium may crop the embedded JPEG before writing the PDF.
                entry=next(im for im in page.get_image_info()
                           if abs(im['bbox'][0]-page.rect.width*.36)<1
                           and abs(im['bbox'][2]-page.rect.x1)<1)
                box=fitz.Rect(entry['bbox'])
                bboxes.append(box)
                assert box.width/box.height==pytest.approx(entry['width']/entry['height'],abs=.01)
                pix=page.get_pixmap(alpha=False)
                # The cream plate covers this x-coordinate at the top. The
                # diagonal exposes the photo at the same x lower in the header.
                x=round(page.rect.width*.415)
                assert pix.pixel(x,8)==pytest.approx((252,250,246),abs=4)
                assert pix.pixel(x,120)==pytest.approx(color,abs=8)
                assert pix.pixel(pix.width-5,8)==pytest.approx(color,abs=8)
    assert all(list(box)==pytest.approx(list(bboxes[0]),abs=.1) for box in bboxes)


@pytest.mark.parametrize('format_name', ['Code39', 'Code128'])
def test_html_landscape_codes_keep_pixels_and_decode_after_pdf_render(case, tmp_path, format_name):
    import hashlib
    import zxingcpp
    from PIL import Image
    from app.extraction import pixel_hash
    from app.pipeline import verify_artifacts

    settings, _, extracted, _ = case
    payload='DEMO12345678'
    barcode=zxingcpp.create_barcode(payload, getattr(zxingcpp.BarcodeFormat, format_name))
    image=Image.fromarray(zxingcpp.write_barcode_to_image(barcode, scale=1, add_quiet_zones=True))
    image=image.resize((image.width, 24), Image.Resampling.NEAREST)
    assert payload in {item.text for item in zxingcpp.read_barcodes(image)}
    code_path=tmp_path/'landscape-code.png'
    image.save(code_path)
    code={'id':'synthetic-code', 'path':str(code_path), 'source_page':1,
          'sha256':hashlib.sha256(code_path.read_bytes()).hexdigest(),
          'pixel_sha256':pixel_hash(image), 'payloads':[payload],
          'purpose':'booking', 'preservation_mode':'original_image'}
    extracted['codes']=[code]
    extracted['data']['pipeline_version']='canonical_v1'
    plan=Plan(title='Demonstração', omitted_blocks=[], pages=[Page(
        title='Código sintético', eyebrow='Teste', introduction='',
        sections=[Section(title='Identificação', tone='neutral', bullets=['Sem validade comercial.'])],
        code_id=code['id'], critical_values=[], source_block_ids=['p1-b1'])])
    planner=SimpleNamespace(ledger=SimpleNamespace(summary=lambda:[]),cache_hits=[])
    rendered=render_html_document(settings,plan,{},extracted,tmp_path/'wide-code-pdf',planner)
    placement=rendered['code_placements'][0]
    assert placement['bbox'][2]-placement['bbox'][0] > 300
    assert verify_artifacts(rendered,[code])['codes_valid']
    assert all(item['passed'] for item in rendered['template_layout_checks'])


def test_html_blocks_overflow_instead_of_clipping_or_shrinking_text(case,tmp_path):
    settings,_,extracted,plan=case
    plan.pages[0].sections[0].bullets+=['Linha sintética curta.']*70
    planner=SimpleNamespace(ledger=SimpleNamespace(summary=lambda:[]),cache_hits=[])
    with pytest.raises(AIError) as error:
        render_html_document(settings,plan,{},extracted,tmp_path/'overflow',planner,scope='preview')
    assert error.value.code=='HTML_LAYOUT_OVERFLOW'
    assert not (tmp_path/'overflow/document.pdf').exists()


def test_html_cache_comparison_never_pays_for_missing_plan(case):
    settings,source,extracted,_=case
    db=Database(settings)
    db.create_schema()
    try:
        from uuid import uuid4
        row=db.enqueue(str(uuid4()),'a','a'*64,str(source))[0]
        pipeline=ImagePipeline(replace(settings,openai_api_key=None),db,row,cache_only=True)
        with pytest.raises(AIError) as error:
            pipeline.plan(extracted,source)
        assert error.value.code=='AI_CACHE_REQUIRED'
        with db.engine.connect() as connection:
            assert connection.execute(select(ai_calls)).first() is None
    finally:
        db.engine.dispose()


def test_html_critical_checks_accept_only_bounded_fact_equivalences():
    assert critical_value_present('15:00-00:00','Check-in: das 15:00 às 00:00.')
    assert critical_value_present('12:00-12:00','Check-out: das 12:00 às 12:00.')
    assert not critical_value_present('15:00-00:00','Check-in: das 16:00 às 00:00.')
    assert not critical_value_present('15:00-00:00','Origem 15:00; destino 00:00.')
    assert not critical_value_present('15:00-00:00','Check-in: das 00:00 às 15:00.')
    assert critical_value_present('08:10 ABC','Origem (ABC) 08:10; destino (XYZ) 10:15.')
    assert not critical_value_present('08:10 ABC','Origem (ABD) 08:10; destino (XYZ) 10:15.')
    assert not critical_value_present('08:10 ABC','Origem (ABC) 09:10; destino (XYZ) 10:15.')
    assert critical_value_present('100 a 600 metros','Bangalôs ficam entre 100 e 600 metros do bloco.')
    assert not critical_value_present('100 a 600 metros','Bangalôs ficam entre 100 e 700 metros do bloco.')
    assert not critical_value_present('100 a 600 metros','Bangalôs ficam entre 100 e 600 quilômetros do bloco.')
    assert not critical_value_present('ABC123','Localizador: ABC124')
    assert not critical_value_present('R$ 60,00','Taxa R$ 600,00')


def test_html_recovers_only_source_proven_locator_on_associated_page(case):
    _,_,extracted,plan=case
    extracted['pages'][0]['blocks'].append({'text':'Localizador: ABC123'})
    plan.pages[0].critical_values.extend(['ABC123','INVENTADO'])
    original=plan.model_dump_json()
    fixed,adjustments=reconcile_source_locators(plan,extracted)
    assert fixed.pages[0].sections[-1].bullets==['Localizador: ABC123']
    assert 'INVENTADO' not in ' '.join(fixed.pages[0].sections[-1].bullets)
    assert adjustments==[{'page':1,'kind':'source_locator_added','source_page':1}]
    assert plan.model_dump_json()==original
    again,second=reconcile_source_locators(fixed,extracted)
    assert again==fixed and second==[]
    plan.pages[0].source_block_ids=['p99-b1']
    unfixed,adjustments=reconcile_source_locators(plan,extracted)
    assert unfixed==plan and adjustments==[]
