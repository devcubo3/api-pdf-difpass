"""Synthetic provider-contract tests. No account, real traveller or paid API call."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
import base64
import hashlib
import io
import json

import pytest
import httpx
from openai import APIStatusError
from PIL import Image,ImageDraw
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import ai_images
from app.ai_images import AIError, ImagePipeline, Page, Plan, Section, validate_plan, check_image_text
from app.ai_budget import BudgetLedger, AIBudgetError
from app.config import Settings
from app.db import Database, ai_calls
from app.extraction import extract_document
from app.main import create_app
from app.pipeline import process_next
from test_api import synthetic_pdf


def synthetic_page_image():
    image=Image.new('RGB',(1536,2176),'#FCFAF6')
    ImageDraw.Draw(image).rectangle((960,720,1360,1120),fill='white')
    return image


@pytest.fixture
def setup(tmp_path):
    settings = Settings(data_dir=tmp_path, database_url=f'sqlite:///{tmp_path.as_posix()}/state.db',
        render_mode='openai_images', openai_api_key='synthetic-not-a-real-key', worker_timeout_seconds=7200,
        tokens_json=json.dumps({'synthetic':{'tenant_id':'a','subject':'tester','roles':['upload','read','review']}}))
    db = Database(settings)
    db.create_schema()
    yield settings, db
    db.engine.dispose()


def extracted_case(tmp_path):
    source = tmp_path/'fixture.pdf'
    source.write_bytes(synthetic_pdf())
    extracted = extract_document(source, tmp_path/'codes')
    ids = [f'p{p["page"]}-b{i+1}' for p in extracted['pages'] for i,_ in enumerate(p['blocks'])]
    code = extracted['codes'][0]
    plan = Plan(title='Universal sintético', omitted_blocks=[], pages=[Page(
        title=code['label'], eyebrow='Ingresso de teste', introduction='Documento sem validade.',
        sections=[Section(title='Acesso',tone='included',bullets=[f'Ingresso {code["ticket_number"]}'])],
        code_id=code['id'], critical_values=[code['label'],code['ticket_number']], source_block_ids=ids)])
    return source,extracted,plan


def test_plan_requires_complete_source_coverage_and_code_association(tmp_path):
    _,extracted,plan = extracted_case(tmp_path)
    assert validate_plan(plan,extracted,16)['coverage_passed']
    missing=plan.model_copy(deep=True)
    missing.pages[0].source_block_ids.pop()
    with pytest.raises(AIError,match='blocos'):
        validate_plan(missing,extracted,16)
    wrong=plan.model_copy(deep=True)
    wrong.pages[0].title='OUTRO VIAJANTE'
    with pytest.raises(AIError,match='Nome/número'):
        validate_plan(wrong,extracted,16)
    duplicate=plan.model_copy(deep=True)
    duplicate.pages.append(duplicate.pages[0])
    with pytest.raises(AIError,match='código original'):
        validate_plan(duplicate,extracted,16)


def test_ocr_verification_rejects_changed_critical_number(tmp_path,monkeypatch):
    _,_,plan=extracted_case(tmp_path)
    page=plan.pages[0]
    text=ai_images.page_text(page)
    image=Image.new('RGB',(300,300),'white')
    monkeypatch.setattr(ai_images,'_read_image_text',lambda *a,**kw:(text,text,0))
    assert check_image_text(image,page)['passed']
    monkeypatch.setattr(ai_images,'_read_image_text',lambda *a,**kw:(text.replace('000123456','999999999'),text.replace('000123456','999999999'),0))
    assert not check_image_text(image,page)['critical_values_match']
    monkeypatch.setattr(ai_images,'_read_image_text',lambda *a,**kw:(text+'\nR$ 99999',text+'\nR$ 99999',0))
    assert check_image_text(image,page)['unexpected_numeric_tokens_count']==1
    assert not check_image_text(image,page)['passed']


def test_budget_is_atomic_across_documents_and_workers(setup):
    settings,db=setup
    db.settings=replace(settings,ai_document_budget_usd=1,ai_daily_budget_usd=1)
    rows=[db.enqueue(str(uuid4()),'a','a'*64,'private.pdf')[0] for _ in range(4)]
    def reserve(row):
        try:
            BudgetLedger(db,row).reserve(hashlib.sha256(row['id'].encode()).hexdigest(),'page-1','gpt-image-2',.6)
            return True
        except AIBudgetError:
            return False
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(reserve,rows))==1
    with db.engine.connect() as connection:
        assert len(connection.execute(select(ai_calls)).all())==1


def test_uncertain_call_cannot_spend_again_and_completed_cache_is_verified(setup):
    settings,db=setup
    row=db.enqueue(str(uuid4()),'a','a'*64,'private.pdf')[0]
    pipeline=ImagePipeline(settings,db,row,client=object())
    count=[]
    def uncertain():
        count.append(1)
        raise TimeoutError('synthetic ambiguous request')
    with pytest.raises(TimeoutError):
        pipeline.request('plan',settings.openai_text_model,'abc','json',.1,uncertain)
    with pytest.raises(AIError,match='incerta'):
        pipeline.request('plan',settings.openai_text_model,'abc','json',.1,uncertain)
    assert len(count)==1
    def complete():
        count.append(2)
        return b'{"synthetic":true}',{'input_tokens':1}
    assert pipeline.request('plan',settings.openai_text_model,'other','json',.1,complete)
    assert pipeline.request('plan',settings.openai_text_model,'other','json',.1,complete)
    assert count==[1,2]
    target=next(p for p in pipeline.cache.glob('*.json') if p.read_bytes()==b'{"synthetic":true}')
    target.write_bytes(b'tampered')
    with pytest.raises(AIError):
        pipeline.request('plan',settings.openai_text_model,'other','json',.1,complete)


def test_missing_key_has_no_fallback_pdf(setup,monkeypatch):
    settings,db=setup
    settings=replace(settings,openai_api_key=None)
    with TestClient(create_app(settings)) as client:
        headers={'Authorization':'Bearer synthetic','Idempotency-Key':'missing-key'}
        result=client.post('/v2/documents',headers=headers,
                           files={'input_pdf':('synthetic.pdf',synthetic_pdf(),'application/pdf')})
        identifier=result.json()['document_id']
        process_next(settings)
        review=client.get(f'/v2/documents/{identifier}/review',headers=headers).json()
        assert review['status']=='needs_review'
        assert review['artifact_sha256'] is None
        assert review['issues'][0]['code']=='OPENAI_NOT_CONFIGURED'
        assert client.get(f'/v2/documents/{identifier}/review/pdf',headers=headers).status_code==409


def test_image_mode_uses_provider_and_preserves_original_code(setup,tmp_path,monkeypatch):
    settings,db=setup
    _,_,plan=extracted_case(tmp_path)
    buffer=io.BytesIO()
    synthetic_page_image().save(buffer,format='PNG')
    usage=SimpleNamespace(model_dump=lambda:{'input_tokens':1,'output_tokens':1})
    calls=[]
    def respond(**kwargs):
        assert kwargs['store'] is False
        assert kwargs['text']['format']['strict'] is True
        calls.append('plan')
        return SimpleNamespace(status='completed',output_text=plan.model_dump_json(),usage=usage)
    def edit(**kwargs):
        assert kwargs['model']=='gpt-image-2' and kwargs['quality']=='high'
        assert 'vermelho' in kwargs['prompt'].casefold() and 'verde' in kwargs['prompt'].casefold()
        calls.append('image')
        return SimpleNamespace(data=[SimpleNamespace(b64_json=base64.b64encode(buffer.getvalue()).decode())],usage=usage)
    fake=SimpleNamespace(responses=SimpleNamespace(create=respond),images=SimpleNamespace(edit=edit))
    monkeypatch.setattr(ai_images,'OpenAI',lambda **kwargs:fake)
    monkeypatch.setattr(ai_images,'check_image_text',lambda *args:{'passed':True})
    from app import pipeline
    monkeypatch.setattr(pipeline,'render_document',lambda *a,**kw:pytest.fail('Local layout must not run'))
    with TestClient(create_app(settings)) as client:
        headers={'Authorization':'Bearer synthetic','Idempotency-Key':'image-mode'}
        result=client.post('/v2/documents',headers=headers,
            files={'input_pdf':('synthetic.pdf',synthetic_pdf(),'application/pdf')})
        identifier=result.json()['document_id']
        process_next(settings)
        review=client.get(f'/v2/documents/{identifier}/review',headers=headers).json()
        assert review['checks']['render_mode']=='openai_images'
        assert review['checks']['codes_valid']
        assert review['checks']['validations_passed']
        assert calls==['plan','image']
        pdf=client.get(f'/v2/documents/{identifier}/review/pdf',headers=headers)
        assert hashlib.sha256(pdf.content).hexdigest()==review['artifact_sha256']


def test_environment_defaults_to_image_mode(monkeypatch):
    monkeypatch.delenv('PDF_RENDER_MODE',raising=False)
    assert Settings.from_env().render_mode=='openai_images'


def test_preview_makes_one_image_and_continuation_reuses_paid_cache(setup,tmp_path,monkeypatch):
    settings,db=setup
    _,_,plan=extracted_case(tmp_path)
    ticket=plan.pages[0]
    cover=Page(title='Universal Orlando',eyebrow='Resumo',introduction='Teste sem validade.',
        sections=[Section(title='Incluído',tone='included',bullets=['Ingresso de teste'])],
        code_id=None,critical_values=[],source_block_ids=ticket.source_block_ids)
    plan.pages=[cover,ticket]
    buffer=io.BytesIO()
    synthetic_page_image().save(buffer,format='PNG')
    usage=SimpleNamespace(model_dump=lambda:{'input_tokens':1,'output_tokens':1})
    calls=[]
    def respond(**kwargs):
        calls.append('plan')
        return SimpleNamespace(status='completed',output_text=plan.model_dump_json(),usage=usage)
    def edit(**kwargs):
        calls.append('image')
        return SimpleNamespace(data=[SimpleNamespace(b64_json=base64.b64encode(buffer.getvalue()).decode())],usage=usage)
    fake=SimpleNamespace(responses=SimpleNamespace(create=respond),images=SimpleNamespace(edit=edit))
    monkeypatch.setattr(ai_images,'OpenAI',lambda **kwargs:fake)
    monkeypatch.setattr(ai_images,'check_image_text',lambda *args:{'passed':True})
    with TestClient(create_app(settings)) as client:
        headers={'Authorization':'Bearer synthetic','Idempotency-Key':'preview-cache'}
        raw=synthetic_pdf()
        upload=client.post('/v2/documents',headers=headers,data={'generation_scope':'preview'},
            files={'input_pdf':('synthetic.pdf',raw,'application/pdf')})
        identifier=upload.json()['document_id']
        assert client.post('/v2/documents',headers=headers,data={'generation_scope':'full'},
            files={'input_pdf':('synthetic.pdf',raw,'application/pdf')}).status_code==409
        process_next(settings)
        route=f'/v2/documents/{identifier}'
        review=client.get(route+'/review',headers=headers).json()
        assert review['checks']['preview_only'] and review['checks']['rendered_pages']==1
        assert review['checks']['planned_pages']==2
        assert not review['checks']['validations_passed']
        assert calls==['plan','image']
        assert client.post(route+'/review/decision',headers={**headers,'If-Match':review['etag']},
            json={'action':'approve','revision':1,'artifact_sha256':review['artifact_sha256']}).status_code==409
        rebuilt=client.post(route+'/preview/rebuild',headers={**headers,'If-Match':review['etag']},
            json={'reason':'Ajustar encaixe usando somente cache'})
        assert rebuilt.status_code==202
        process_next(settings)
        review=client.get(route+'/review',headers=headers).json()
        assert set(review['checks']['ai_cache_hits'])=={'plan','page-1'}
        assert calls==['plan','image']
        assert client.post(route+'/continue',headers=headers,json={'reason':'Conferência da prévia concluída'}).status_code==428
        assert client.post(route+'/continue',headers={**headers,'If-Match':'"999"'},
            json={'reason':'Conferência da prévia concluída'}).status_code==409
        continued=client.post(route+'/continue',headers={**headers,'If-Match':review['etag']},
            json={'reason':'Conferência da prévia concluída'})
        assert continued.status_code==202 and continued.json()['revision']==1
        assert client.post(route+'/continue',headers={**headers,'If-Match':continued.json()['etag']},
            json={'reason':'Não duplicar continuação'}).status_code==409
        process_next(settings)
        final=client.get(route+'/review',headers=headers).json()
        assert final['checks']['validations_passed']
        assert not final['checks']['preview_only'] and final['checks']['rendered_pages']==2
        assert set(final['checks']['ai_cache_hits'])=={'plan','page-1'}
        assert calls==['plan','image','image']
        assert len(final['checks']['ai_usage'])==3


def test_code_slot_detection_rejects_missing_or_ambiguous_regions():
    slot=ai_images.find_code_slot(synthetic_page_image())
    assert 950<=slot[0]<=970 and 710<=slot[1]<=730
    with pytest.raises(AIError,match='espaço vazio'):
        ai_images.find_code_slot(Image.new('RGB',(1536,2176),'white'))
    ambiguous=synthetic_page_image()
    ImageDraw.Draw(ambiguous).rectangle((960,1400,1360,1800),fill='white')
    with pytest.raises(AIError,match='espaço vazio'):
        ai_images.find_code_slot(ambiguous)


def test_cache_only_rebuild_cannot_make_a_paid_request(setup):
    settings,db=setup
    row=db.enqueue(str(uuid4()),'a','a'*64,'private.pdf')[0]
    pipeline=ImagePipeline(settings,db,row,client=object(),cache_only=True)
    with pytest.raises(AIError,match='já em cache'):
        pipeline.request('plan',settings.openai_text_model,'new','json',.1,
                         lambda:pytest.fail('Cache-only must never call a provider'))
    assert pipeline.ledger.summary()==[]


def test_decorative_ocr_glyph_does_not_hide_a_real_numeric_fact(monkeypatch):
    monkeypatch.setattr(ai_images.pytesseract,'image_to_data',lambda *a,**kw:{
        'text':['00','R$','99999','2026'],'conf':[34,90,92,90],'height':[322,22,22,180]})
    text,numeric,ignored=ai_images._read_image_text(Image.new('RGB',(1536,2176),'white'))
    assert ignored==1 and '00' in text
    assert numeric=='R$ 99999 2026'


@pytest.mark.parametrize('variant_model,reservation',[
    ('gpt-image-1-mini',.1),('gpt-image-2',.15)])
@pytest.mark.parametrize('block_initial_layout',[False,True])
def test_mini_variant_reuses_plan_and_fits_codes_without_distortion(setup,tmp_path,monkeypatch,variant_model,reservation,block_initial_layout):
    settings,db=setup
    _,_,plan=extracted_case(tmp_path)
    usage=SimpleNamespace(model_dump=lambda:{'input_tokens':1,'output_tokens':1})
    calls=[]
    def respond(**kwargs):
        calls.append('plan')
        return SimpleNamespace(status='completed',output_text=plan.model_dump_json(),usage=usage)
    def edit(**kwargs):
        calls.append((kwargs['model'],kwargs['quality'],kwargs['size']))
        w,h=map(int,kwargs['size'].split('x'))
        image=Image.new('RGB',(w,h),'#FCFAF6')
        ImageDraw.Draw(image).rectangle((round(w*.63),round(h*.33),round(w*.89),round(h*.33+w*.26)),fill='white')
        stream=io.BytesIO();image.save(stream,format='PNG')
        return SimpleNamespace(data=[SimpleNamespace(b64_json=base64.b64encode(stream.getvalue()).decode())],usage=usage)
    fake=SimpleNamespace(responses=SimpleNamespace(create=respond),images=SimpleNamespace(edit=edit))
    monkeypatch.setattr(ai_images,'OpenAI',lambda **kwargs:fake)
    monkeypatch.setattr(ai_images,'check_image_text',lambda *args:{'passed':True})
    original_slot=ai_images.find_code_slot
    def slot(image):
        if block_initial_layout and len(calls)==2:
            raise AIError('AI_CODE_SLOT','Synthetic initial preview blocked')
        return original_slot(image)
    monkeypatch.setattr(ai_images,'find_code_slot',slot)
    headers={'Authorization':'Bearer synthetic','Idempotency-Key':'mini-variant'}
    with TestClient(create_app(settings)) as client:
        upload=client.post('/v2/documents',headers=headers,data={'generation_scope':'preview'},
            files={'input_pdf':('synthetic.pdf',synthetic_pdf(),'application/pdf')})
        identifier=upload.json()['document_id'];route=f'/v2/documents/{identifier}'
        process_next(settings)
    mini=replace(settings,openai_image_model=variant_model,openai_image_quality='medium',openai_image_size='1024x1536')
    with TestClient(create_app(mini)) as client:
        review=client.get(route+'/review',headers=headers).json()
        variant=client.post(route+'/preview/variant',headers={**headers,'If-Match':review['etag']},
            json={'reason':'Comparar Mini em qualidade média com plano existente'})
        assert variant.status_code==202
        process_next(mini)
        result=client.get(route+'/review',headers=headers).json()
        assert result['checks']['codes_valid']
        assert result['checks']['ai_cache_hits']==['plan']
        assert result['checks']['image_profile']=={'model':variant_model,'quality':'medium','size':'1024x1536'}
        generated=client.get(route+'/review/generated-page/1.png',headers=headers)
        assert generated.status_code==200
        assert generated.content.startswith(b'\x89PNG')
        assert client.get(route+'/review/generated-page/1.png').status_code in {401,403}
        assert client.get(route+'/review/generated-page/0.png',headers=headers).status_code==404
        assert len(result['checks']['ai_usage'])==3
        assert any(c['reserved_estimate_usd']==reservation for c in result['checks']['ai_usage'])
        assert calls==['plan',('gpt-image-2','high','1536x2176'),(variant_model,'medium','1024x1536')]
        rebuild=client.post(route+'/preview/rebuild',headers={**headers,'If-Match':result['etag']},
            json={'reason':'Recompor Mini sem gerar uma segunda imagem'})
        assert rebuild.status_code==202
        process_next(mini)
        assert len(calls)==3


def test_image_profiles_require_explicit_supported_quality_and_size():
    with pytest.raises(ValueError,match='documented'):
        Settings(openai_image_model='gpt-image-1-mini')
    settings=Settings(openai_image_model='gpt-image-1-mini',openai_image_quality='medium',openai_image_size='1024x1536')
    assert settings.openai_image_quality=='medium'


@pytest.mark.parametrize('provider_code,classification',[
    ('insufficient_quota','OPENAI_QUOTA'),
    ('credit_balance_exhausted','OPENAI_CREDIT_BALANCE_EXHAUSTED'),
    ('organization_spend_limit_exceeded','OPENAI_ORGANIZATION_SPEND_LIMIT'),
    ('project_spend_limit_exceeded','OPENAI_PROJECT_SPEND_LIMIT'),
    ('organization_usage_limit_exceeded','OPENAI_ORGANIZATION_USAGE_LIMIT'),
    ('slow_down','OPENAI_RATE_LIMIT'),
    ('rate_limit_exceeded','OPENAI_RATE_LIMIT'),
])
def test_provider_quota_error_is_classified_without_exposing_raw_error(setup,provider_code,classification):
    settings,db=setup
    row=db.enqueue(str(uuid4()),'a','a'*64,'private.pdf')[0]
    pipeline=ImagePipeline(settings,db,row,client=object())
    def rejected():
        raise APIStatusError('SYNTHETIC-PRIVATE-BODY',response=httpx.Response(429,headers={'Retry-After':'30'},
            request=httpx.Request('POST','https://api.openai.com/v1/responses')),
            body={'error':{'code':provider_code,'type':'synthetic_error_type','message':'SYNTHETIC-PRIVATE-BODY'}})
    with pytest.raises(AIError) as failure:
        pipeline.request('plan',settings.openai_text_model,'quota','json',.1,rejected)
    assert failure.value.code==classification
    assert 'SYNTHETIC-PRIVATE-BODY' not in str(failure.value)
    usage=pipeline.ledger.summary()
    assert usage[0]['usage']['classified_error']==classification
    assert usage[0]['usage']['provider_error_code']==provider_code
    assert usage[0]['usage']['retry_after']=='30'
    assert 'SYNTHETIC-PRIVATE-BODY' not in json.dumps(usage)
