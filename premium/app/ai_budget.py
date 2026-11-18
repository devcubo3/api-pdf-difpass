"""Transactional estimated-spend reservations, shared by API workers.

This limits application estimates, not the provider's invoice. An uncertain paid
call is never automatically retried/refunded. Cache digests prevent double spend
when a later worker resumes an already completed request.
"""
from datetime import datetime, timezone
from sqlalchemy import insert, select, update
from .db import ai_budget_windows, ai_calls


class AIBudgetError(ValueError):
    pass


class BudgetLedger:
    def __init__(self, db, row):
        self.db, self.row = db, row

    def reserve(self, key, stage, model, estimate_usd, *, extra_windows=()):
        amount = round(estimate_usd * 1_000_000)
        if amount <= 0:
            raise AIBudgetError('Invalid estimated reservation')
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
            # Fixed global-day then document order serializes cap checks across workers.
            for scope, limit in windows:
                conn.execute(upsert(ai_budget_windows).values(scope=scope, reserved_microusd=0)
                             .on_conflict_do_nothing(index_elements=['scope']))
            # A no-op UPDATE acquires the same row lock on both SQLite and PostgreSQL.
            conn.execute(update(ai_budget_windows).where(ai_budget_windows.c.scope == windows[0][0])
                         .values(reserved_microusd=ai_budget_windows.c.reserved_microusd))
            existing = conn.execute(select(ai_calls).where(ai_calls.c.key == key)).mappings().first()
            if existing:
                if existing['tenant_id'] != self.row['tenant_id'] or existing['document_id'] != self.row['id']:
                    raise AIBudgetError('AI cache identity mismatch')
                return dict(existing)
            for scope, limit in windows:
                changed = conn.execute(update(ai_budget_windows).where(
                    ai_budget_windows.c.scope == scope,
                    ai_budget_windows.c.reserved_microusd + amount <= round(limit * 1_000_000),
                ).values(reserved_microusd=ai_budget_windows.c.reserved_microusd + amount))
                if changed.rowcount != 1:
                    raise AIBudgetError('Estimated AI budget exhausted')
            conn.execute(insert(ai_calls).values(key=key, document_id=self.row['id'],
                tenant_id=self.row['tenant_id'], revision=self.row['revision'], stage=stage,
                model=model, reserved_microusd=amount, status='reserved', usage_json={},
                created_at=datetime.now(timezone.utc)))
        return None

    def finish(self, key, digest, usage):
        with self.db.engine.begin() as conn:
            conn.execute(update(ai_calls).where(ai_calls.c.key == key,
                ai_calls.c.tenant_id == self.row['tenant_id'], ai_calls.c.status == 'reserved')
                .values(status='completed', response_sha256=digest, usage_json=usage or {}))

    def cached(self,key):
        with self.db.engine.connect() as conn:
            row=conn.execute(select(ai_calls).where(ai_calls.c.key==key,
                ai_calls.c.document_id==self.row['id'],ai_calls.c.tenant_id==self.row['tenant_id'])).mappings().first()
            return dict(row) if row else None

    def uncertain(self, key, diagnostic=None):
        with self.db.engine.begin() as conn:
            conn.execute(update(ai_calls).where(ai_calls.c.key == key,
                ai_calls.c.tenant_id == self.row['tenant_id'], ai_calls.c.status == 'reserved')
                .values(status='uncertain',usage_json=diagnostic or {}))

    def summary(self):
        with self.db.engine.connect() as conn:
            rows = conn.execute(select(ai_calls).where(ai_calls.c.document_id == self.row['id'],
                ai_calls.c.tenant_id == self.row['tenant_id'], ai_calls.c.revision == self.row['revision'])).mappings()
            return [{'stage': r['stage'], 'model': r['model'], 'status': r['status'],
                     'reserved_estimate_usd': r['reserved_microusd']/1_000_000,
                     'usage': r['usage_json']} for r in rows]
