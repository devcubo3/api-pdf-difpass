"""Cost reconciliation, old reservations, SaaS isolation and bounded retries."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime,timezone
import hashlib
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import insert,select,update

from app.ai_budget import BudgetLedger,SerpApiLedger,AIBudgetError
from app.budget_pricing import token_cost_microusd
from app.db import ai_calls,ai_budget_windows,ai_budget_grants,ConcurrentUpdate
from app.main import create_app
from app.pipeline import process_next
from test_media_general import discovery_case
from test_media import source_settings
from test_canonical import case,understood,fake_provider
from app.extraction import extract_document


@pytest.fixture
def accounting(discovery_case):
    settings,db,row=discovery_case
    settings=replace(settings,ai_document_budget_usd=.25,ai_daily_budget_usd=1)
    db.settings=settings
    return settings,db,row


def test_known_usage_prices_cached_tokens_and_keeps_unknown_usage_conservative():
    assert token_cost_microusd('gpt-6-luna',{'input_tokens':10000,'output_tokens':1000})==1500
    assert token_cost_microusd('gpt-6-luna',{'input_tokens':10000,'output_tokens':1000,
        'input_tokens_details':{'cached_tokens':5000}})==1050
    assert token_cost_microusd('unknown-model',{'input_tokens':1,'output_tokens':1}) is None
    assert token_cost_microusd('gpt-6-luna',{'input_tokens':1}) is None
    assert token_cost_microusd('gpt-6-luna',{'input_tokens':1,'output_tokens':1,
        'input_tokens_details':{'cached_tokens':2}}) is None
    assert token_cost_microusd('gpt-6-luna',{'input_tokens':1,'output_tokens':1,
        '_service_tier':'unknown-premium'}) is None


def test_completed_normal_pdfs_do_not_consume_the_full_reservation(accounting):
    settings,db,row=accounting
    for _ in range(12):
        doc,_=db.enqueue(str(uuid4()),'org','a'*64,'synthetic.pdf')
        ledger=BudgetLedger(db,doc);key=hashlib.sha256(doc['id'].encode()).hexdigest()
        ledger.reserve(key,'plan','gpt-6-luna',.10)
        ledger.finish(key,'b'*64,{'input_tokens':10000,'output_tokens':1000})
        assert ledger.summary()[0]['reserved_estimate_usd']==.10
        assert ledger.summary()[0]['accounted_estimate_usd']==.0015
    assert BudgetLedger(db,row).overview()['day_accounted_estimate_usd']==.018


def test_completed_legacy_calls_reconcile_without_resetting_audit_history(accounting):
    settings,db,row=accounting
    now=datetime.now(timezone.utc);day=f'day:{now:%Y-%m-%d}'
    with db.engine.begin() as conn:
        conn.execute(insert(ai_budget_windows).values(scope=day,reserved_microusd=1_000_000))
        conn.execute(insert(ai_calls).values(key='a'*64,document_id=row['id'],tenant_id=row['tenant_id'],
            revision=1,stage='plan',model='gpt-6-luna',reserved_microusd=100_000,status='completed',
            usage_json={'input_tokens':10000,'output_tokens':1000},created_at=now))
        conn.execute(insert(ai_calls).values(key='b'*64,document_id=row['id'],tenant_id=row['tenant_id'],
            revision=1,stage='media_search',model='serpapi:google_images',reserved_microusd=50_000,
            status='completed',usage_json={'search_requests':1},created_at=now))
    ledger=BudgetLedger(db,row)
    assert ledger.overview()['day_accounted_estimate_usd']==.0015
    ledger.reserve('c'*64,'plan','gpt-6-luna',.1)
    assert ledger.overview()['day_accounted_estimate_usd']==.1015
    assert len(SerpApiLedger(db,row).summary())==1
    with db.engine.connect() as conn:
        old=conn.execute(select(ai_calls).where(ai_calls.c.key=='a'*64)).mappings().one()
        assert old['reserved_microusd']==100_000 and old['status']=='completed'


def test_uncertain_and_unpriced_calls_keep_reservations(accounting):
    settings,db,row=accounting;ledger=BudgetLedger(db,row)
    ledger.reserve('a'*64,'plan','gpt-6-luna',.1);ledger.uncertain('a'*64)
    ledger.reserve('b'*64,'plan','unknown-model',.1)
    ledger.finish('b'*64,'c'*64,{'input_tokens':1,'output_tokens':1})
    assert ledger.overview()['document_accounted_estimate_usd']==.2
    with pytest.raises(AIBudgetError) as error:ledger.reserve('c'*64,'plan','gpt-6-luna',.1)
    assert error.value.details['scope']=='document'


def test_concurrent_reservations_still_respect_the_global_one_dollar_limit(accounting):
    settings,db,row=accounting
    db.settings=replace(settings,ai_document_budget_usd=1)
    rows=[db.enqueue(str(uuid4()),'org','a'*64,'synthetic.pdf')[0] for _ in range(8)]
    def reserve(doc):
        try:BudgetLedger(db,doc).reserve(hashlib.sha256(doc['id'].encode()).hexdigest(),'plan','unknown',.25);return True
        except AIBudgetError:return False
    with ThreadPoolExecutor(max_workers=8) as pool:assert sum(pool.map(reserve,rows))==4
    assert BudgetLedger(db,row).overview()['day_accounted_estimate_usd']==1


def test_serpapi_credits_are_independent_even_when_ai_budget_is_full(accounting):
    settings,db,row=accounting
    db.settings=replace(settings,ai_document_budget_usd=1,serpapi_daily_search_limit=1)
    BudgetLedger(db,row).reserve('a'*64,'plan','unknown',1)
    searches=SerpApiLedger(db,row);searches.reserve('b'*64)
    searches.uncertain('b'*64)
    with pytest.raises(AIBudgetError):searches.reserve('c'*64)
    assert BudgetLedger(db,row).overview()['day_accounted_estimate_usd']==1
    assert searches.summary()[0]['reserved_credits']==1


def test_manual_allowance_and_document_update_are_atomic(accounting):
    settings,db,row=accounting
    db.settings=replace(settings,ai_document_budget_usd=.15)
    ledger=BudgetLedger(db,row);ledger.reserve('a'*64,'plan','unknown',.1);ledger.uncertain('a'*64)
    row=db.update_document(row['id'],row['tenant_id'],row['etag'],status='needs_review',
        issues_json=[{'code':'AI_BUDGET_LIMIT'}])
    grant=BudgetLedger(db,row).retry_grant({'subject':'staff'},'Explicit manual retry')
    assert grant['extra_microusd']==100_000
    changed=db.update_document(row['id'],row['tenant_id'],row['etag'],status='queued',
        revision=2,budget_grant=grant)
    with pytest.raises(ConcurrentUpdate):
        db.update_document(row['id'],row['tenant_id'],row['etag'],status='queued',budget_grant=grant)
    ledger=BudgetLedger(db,changed)
    assert ledger.retry_grant({'subject':'staff'},'Another retry') is None
    ledger.reserve('b'*64,'plan','unknown',.1)
    assert ledger.overview()['document_limit_usd']==.25
    with db.engine.connect() as conn:assert len(conn.execute(select(ai_budget_grants)).all())==1


def test_allowance_never_raises_daily_limit(accounting):
    settings,db,row=accounting
    db.settings=replace(settings,ai_document_budget_usd=.10,ai_daily_budget_usd=.15)
    ledger=BudgetLedger(db,row);ledger.reserve('a'*64,'plan','unknown',.10);ledger.uncertain('a'*64)
    with db.engine.begin() as conn:
        conn.execute(insert(ai_budget_grants).values(document_id=row['id'],tenant_id=row['tenant_id'],
            extra_microusd=100_000,subject='staff',reason='Explicit retry',revision=1,created_at=datetime.now(timezone.utc)))
    with pytest.raises(AIBudgetError) as error:ledger.reserve('b'*64,'plan','unknown',.10)
    assert error.value.details['scope']=='day'


def test_http_budget_retry_uses_existing_contract_and_staff_authorization(case,monkeypatch):
    settings,source,_,_=case
    settings=replace(settings,pipeline_version='canonical_v1',ai_document_budget_usd=.15,ai_daily_budget_usd=1)
    doc=understood(extract_document(source,settings.data_dir/'setup',generic=True))
    calls=fake_provider(monkeypatch,doc)
    auth={'Authorization':'Bearer reviewer'}
    with TestClient(create_app(settings)) as client:
        response=client.post('/v2/documents',headers={**auth,'Idempotency-Key':'budget-retry'},
            files={'input_pdf':('synthetic.pdf',source.read_bytes(),'application/pdf')})
        route='/v2/documents/'+response.json()['document_id'];db=client.app.state.db
        row=db.get(response.json()['document_id'],'a')
        ledger=BudgetLedger(db,row);ledger.reserve('a'*64,'plan','unknown',.10);ledger.uncertain('a'*64)
        row=db.update_document(row['id'],row['tenant_id'],row['etag'],status='needs_review',
            issues_json=[{'code':'AI_BUDGET_LIMIT','blocking':True,'message':'Synthetic previous limit'}])
        review=client.get(route+'/review',headers=auth).json()
        body={'reason':'Explicit staff retry through the existing Bubble action'}
        assert client.post(route+'/reprocess',json=body).status_code==401
        assert client.post(route+'/reprocess',headers=auth,json=body).status_code==428
        assert client.post(route+'/reprocess',headers={'Authorization':'Bearer other','If-Match':review['etag']},json=body).status_code==404
        assert client.post(route+'/reprocess',headers={**auth,'If-Match':'stale'},json=body).status_code==409
        assert client.post(route+'/reprocess',headers={**auth,'If-Match':review['etag']},
            json={**body,'daily_budget_override_usd':100}).status_code==422
        result=client.post(route+'/reprocess',headers={**auth,'If-Match':review['etag']},json=body)
        assert result.status_code==202 and result.json()['revision']==2
        assert process_next(settings)
        after=client.get(route+'/review',headers=auth).json()
        assert len(calls)==1 and after['artifact_sha256'] is not None,after['issues']
        assert after['budget']['manual_allowance_used'] and after['budget']['day_limit_usd']==1
        events=[e for e in after['events'] if e['action']=='reprocess']
        assert events[-1]['details']['budget_authorization']['extra_microusd']==100_000
        with db.engine.connect() as conn:
            previous=conn.execute(select(ai_calls).where(ai_calls.c.key=='a'*64)).mappings().one()
            assert previous['status']=='uncertain' and previous['reserved_microusd']==100_000
