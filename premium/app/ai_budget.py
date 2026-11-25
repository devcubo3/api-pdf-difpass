"""Transactional estimated-spend reservations, shared by API workers.

This limits application estimates, not the provider's invoice. An uncertain paid
call is never automatically retried/refunded. Cache digests prevent double spend
when a later worker resumes an already completed request.
"""
from datetime import datetime, timedelta, timezone
import math
from sqlalchemy import insert, select, update
from .db import ai_budget_windows, ai_calls, ai_budget_grants, saas_budget_windows
from .budget_pricing import accounted_microusd,token_cost_microusd,PRICING_VERSION,SERPAPI_MODEL,PLAN_RESERVATION_USD


class AIBudgetError(ValueError):
    def __init__(self,message,*,scope=None,limit=None,used=None,requested=None):
        super().__init__(message)
        self.details={'scope':scope,'limit_usd':limit,'accounted_estimate_usd':used,'requested_usd':requested}

    @property
    def user_message(self):
        if self.details['scope']=='day':
            return ('Limite diário de IA atingido. A equipe pode liberar o PDF de segurança '
                    'ou retomar quando houver saldo no teto diário configurado.')
        if self.details['scope']=='document':
            if self.details.get('manual_allowance_used'):
                return ('Limite por documento atingido após a tentativa adicional autorizada. '
                        'Libere o PDF de segurança ou solicite a conferência da operação.')
            return ('Limite por documento atingido. Use Gerar novamente para solicitar '
                    'uma tentativa controlada; o teto diário continua sendo respeitado.')
        return 'Limite estimado de IA atingido; consulte a operação.'


class BudgetLedger:
    def __init__(self, db, row):
        self.db, self.row = db, row

    def _document_limit(self,connection):
        grant=connection.execute(select(ai_budget_grants.c.extra_microusd).where(
            ai_budget_grants.c.document_id==self.row['id'],
            ai_budget_grants.c.tenant_id==self.row['tenant_id'])).scalar() or 0
        return self.db.settings.ai_document_budget_usd+grant/1_000_000

    def _balance(self,connection,scope):
        query=select(ai_calls).where(ai_calls.c.model!=SERPAPI_MODEL)
        if scope.startswith('doc:'):
            query=query.where(ai_calls.c.document_id==scope[4:])
        else:
            prefix='media-day:' if scope.startswith('media-day:') else 'day:'
            try:start=datetime.strptime(scope[len(prefix):],'%Y-%m-%d').replace(tzinfo=timezone.utc)
            except ValueError:
                return connection.execute(select(ai_budget_windows.c.reserved_microusd).where(
                    ai_budget_windows.c.scope==scope)).scalar() or 0
            query=query.where(ai_calls.c.created_at>=start,ai_calls.c.created_at<start+timedelta(days=1))
            if prefix=='media-day:':query=query.where(ai_calls.c.stage=='media_search')
        return sum(accounted_microusd(call) for call in connection.execute(query).mappings())

    def reserve(self, key, stage, model, estimate_usd, *, extra_windows=()):
        if not math.isfinite(estimate_usd) or estimate_usd <= 0:
            raise AIBudgetError('Invalid estimated reservation')
        amount = math.ceil(estimate_usd * 1_000_000)
        dialect = self.db.engine.dialect.name
        if dialect == 'sqlite':
            from sqlalchemy.dialects.sqlite import insert as upsert
        elif dialect == 'postgresql':
            from sqlalchemy.dialects.postgresql import insert as upsert
        else:
            raise AIBudgetError('Unsupported budget database')
        day = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        windows = [(f'day:{day}', self.db.settings.ai_daily_budget_usd),
                   (f'doc:{self.row["id"]}', self.db.settings.ai_document_budget_usd)]
        windows.extend(extra_windows)
        with self.db.engine.begin() as conn:
            windows[1]=(windows[1][0],self._document_limit(conn))
            # Fixed global-day then document order serializes cap checks across workers.
            for scope, limit in windows:
                conn.execute(upsert(ai_budget_windows).values(scope=scope, reserved_microusd=0)
                             .on_conflict_do_nothing(index_elements=['scope']))
            # A no-op UPDATE acquires the same row lock on both SQLite and PostgreSQL.
            for scope,_ in windows:
                conn.execute(update(ai_budget_windows).where(ai_budget_windows.c.scope == scope)
                             .values(reserved_microusd=ai_budget_windows.c.reserved_microusd))
            existing = conn.execute(select(ai_calls).where(ai_calls.c.key == key)).mappings().first()
            if existing:
                if existing['tenant_id'] != self.row['tenant_id'] or existing['document_id'] != self.row['id']:
                    raise AIBudgetError('AI cache identity mismatch')
                return dict(existing)
            for scope, limit in windows:
                used=self._balance(conn,scope)
                if used+amount>round(limit*1_000_000):
                    kind='day' if scope.startswith('day:') else 'document' if scope.startswith('doc:') else 'media'
                    error=AIBudgetError('Estimated AI budget exhausted',scope=kind,limit=limit,
                                        used=used/1_000_000,requested=estimate_usd)
                    if kind=='document':
                        error.details['manual_allowance_used']=limit>self.db.settings.ai_document_budget_usd
                    raise error
                conn.execute(update(ai_budget_windows).where(ai_budget_windows.c.scope==scope)
                    .values(reserved_microusd=used+amount))
            conn.execute(insert(ai_calls).values(key=key, document_id=self.row['id'],
                tenant_id=self.row['tenant_id'], revision=self.row['revision'], stage=stage,
                model=model, reserved_microusd=amount, status='reserved', usage_json={},
                created_at=datetime.now(timezone.utc)))
        return None

    def finish(self, key, digest, usage):
        with self.db.engine.begin() as conn:
            call=conn.execute(select(ai_calls).where(ai_calls.c.key==key,
                ai_calls.c.tenant_id==self.row['tenant_id'],ai_calls.c.document_id==self.row['id'])).mappings().first()
            if call is None:return
            scope='day:'+call['created_at'].strftime('%Y-%m-%d')
            conn.execute(update(ai_budget_windows).where(ai_budget_windows.c.scope==scope)
                .values(reserved_microusd=ai_budget_windows.c.reserved_microusd))
            conn.execute(update(ai_budget_windows).where(ai_budget_windows.c.scope==f'doc:{self.row["id"]}')
                .values(reserved_microusd=ai_budget_windows.c.reserved_microusd))
            usage=dict(usage or {})
            usage.pop('_budget_cost',None)
            amount=token_cost_microusd(call['model'],usage) if call['stage']=='plan' else None
            if amount is not None:
                usage['_budget_cost']={'microusd':amount,'pricing_version':PRICING_VERSION,
                                       'basis':'token_usage_estimate'}
            conn.execute(update(ai_calls).where(ai_calls.c.key == key,
                ai_calls.c.tenant_id == self.row['tenant_id'], ai_calls.c.status == 'reserved')
                .values(status='completed', response_sha256=digest, usage_json=usage or {}))
            for current in (scope,f'doc:{self.row["id"]}'):
                conn.execute(update(ai_budget_windows).where(ai_budget_windows.c.scope==current)
                    .values(reserved_microusd=self._balance(conn,current)))

    def cached(self,key):
        with self.db.engine.connect() as conn:
            row=conn.execute(select(ai_calls).where(ai_calls.c.key==key,
                ai_calls.c.document_id==self.row['id'],ai_calls.c.tenant_id==self.row['tenant_id'])).mappings().first()
            return dict(row) if row else None

    def uncertain(self, key, diagnostic=None):
        with self.db.engine.begin() as conn:
            conn.execute(update(ai_calls).where(ai_calls.c.key == key,
                ai_calls.c.tenant_id == self.row['tenant_id'], ai_calls.c.document_id==self.row['id'],
                ai_calls.c.status == 'reserved')
                .values(status='uncertain',usage_json=diagnostic or {}))

    def summary(self):
        with self.db.engine.connect() as conn:
            rows = conn.execute(select(ai_calls).where(ai_calls.c.document_id == self.row['id'],
                ai_calls.c.tenant_id == self.row['tenant_id'], ai_calls.c.revision == self.row['revision'],
                ai_calls.c.model!=SERPAPI_MODEL)).mappings()
            return [{'stage': r['stage'], 'model': r['model'], 'status': r['status'],
                     'reserved_estimate_usd': r['reserved_microusd']/1_000_000,
                     'accounted_estimate_usd':accounted_microusd(r)/1_000_000,
                     'usage': r['usage_json']} for r in rows]

    def overview(self):
        with self.db.engine.connect() as conn:
            day='day:'+datetime.now(timezone.utc).strftime('%Y-%m-%d')
            day_used=self._balance(conn,day)/1_000_000
            doc_used=self._balance(conn,'doc:'+self.row['id'])/1_000_000
            doc_limit=self._document_limit(conn)
            return {'unit':'USD','accounting':'usage_estimate_plus_unresolved_reservations',
                'pricing_version':PRICING_VERSION,'day_limit_usd':self.db.settings.ai_daily_budget_usd,
                'day_accounted_estimate_usd':day_used,
                'day_available_usd':max(0,self.db.settings.ai_daily_budget_usd-day_used),
                'document_limit_usd':doc_limit,'document_accounted_estimate_usd':doc_used,
                'document_available_usd':max(0,doc_limit-doc_used),
                'manual_allowance_used':doc_limit>self.db.settings.ai_document_budget_usd}

    def retry_grant(self,principal,reason):
        # Only an explicit authenticated manual retry can grant ONE document
        # allowance. It never increases the daily limit or releases uncertainty.
        if not any(i.get('code')=='AI_BUDGET_LIMIT' for i in self.row['issues_json']):return None
        balance=self.overview();allowance=self.db.settings.ai_manual_retry_allowance_usd
        needed=round(PLAN_RESERVATION_USD*1_000_000)
        if (balance['manual_allowance_used'] or allowance<=0
                or round(balance['document_available_usd']*1_000_000)>=needed
                or round(balance['day_available_usd']*1_000_000)<needed
                or round((balance['document_available_usd']+allowance)*1_000_000)<needed):return None
        return {'extra_microusd':round(allowance*1_000_000),'subject':principal['subject'],
                'reason':reason,'revision':self.row['revision']}


class SerpApiLedger(BudgetLedger):
    """Separate credit caps; historical and uncertain searches still count."""
    def reserve(self,key):
        now=datetime.now(timezone.utc)
        month=now.replace(day=1,hour=0,minute=0,second=0,microsecond=0)
        end=month.replace(year=month.year+1,month=1) if month.month==12 else month.replace(month=month.month+1)
        day=now.replace(hour=0,minute=0,second=0,microsecond=0)
        windows=[(f'serpapi-month:{now:%Y-%m}',month,end,self.db.settings.serpapi_monthly_search_limit),
                 (f'serpapi-day:{now:%Y-%m-%d}',day,day+timedelta(days=1),self.db.settings.serpapi_daily_search_limit)]
        if self.db.engine.dialect.name=='sqlite':
            from sqlalchemy.dialects.sqlite import insert as upsert
        else:
            from sqlalchemy.dialects.postgresql import insert as upsert
        with self.db.engine.begin() as conn:
            for scope,_,_,_ in windows:
                conn.execute(upsert(saas_budget_windows).values(scope=scope,reserved_credits=0)
                    .on_conflict_do_nothing(index_elements=['scope']))
            conn.execute(update(saas_budget_windows).where(saas_budget_windows.c.scope==windows[0][0])
                .values(reserved_credits=saas_budget_windows.c.reserved_credits))
            existing=conn.execute(select(ai_calls).where(ai_calls.c.key==key)).mappings().first()
            if existing:
                if existing['tenant_id']!=self.row['tenant_id'] or existing['document_id']!=self.row['id']:
                    raise AIBudgetError('Search cache identity mismatch')
                return dict(existing)
            for scope,start,end,limit in windows:
                used=len(conn.execute(select(ai_calls.c.key).where(ai_calls.c.model==SERPAPI_MODEL,
                    ai_calls.c.created_at>=start,ai_calls.c.created_at<end)).all())
                if used+1>limit:raise AIBudgetError('SerpApi credit ceiling reached',scope='serpapi')
                conn.execute(update(saas_budget_windows).where(saas_budget_windows.c.scope==scope)
                    .values(reserved_credits=used+1))
            conn.execute(insert(ai_calls).values(key=key,document_id=self.row['id'],tenant_id=self.row['tenant_id'],
                revision=self.row['revision'],stage='media_search',model=SERPAPI_MODEL,reserved_microusd=0,
                status='reserved',usage_json={},created_at=now))

    def finish(self,key,digest,usage):
        with self.db.engine.begin() as conn:
            conn.execute(update(ai_calls).where(ai_calls.c.key==key,ai_calls.c.tenant_id==self.row['tenant_id'],
                ai_calls.c.document_id==self.row['id'],ai_calls.c.status=='reserved')
                .values(status='completed',response_sha256=digest,usage_json=usage or {}))

    def summary(self):
        with self.db.engine.connect() as conn:
            rows=conn.execute(select(ai_calls).where(ai_calls.c.document_id==self.row['id'],
                ai_calls.c.tenant_id==self.row['tenant_id'],ai_calls.c.revision==self.row['revision'],
                ai_calls.c.model==SERPAPI_MODEL)).mappings()
            return [{'provider':'serpapi','status':r['status'],'reserved_credits':1,'usage':r['usage_json']} for r in rows]
