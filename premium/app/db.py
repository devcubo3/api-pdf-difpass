"""Durable document state and a small SQL-backed queue for the first deployment."""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON, Column, DateTime, Float, ForeignKey, Index, Integer, MetaData, String,
    Table, Text, UniqueConstraint, and_, create_engine, delete, event, insert, or_, select, update,
)
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool

from .config import Settings


class IdempotencyConflict(ValueError):
    pass


class ConcurrentUpdate(ValueError):
    pass


metadata = MetaData()
documents = Table(
    "documents", metadata,
    Column("id", String(36), primary_key=True),
    Column("tenant_id", String(200), nullable=False),
    Column("source_sha", String(64), nullable=False),
    Column("source_path", Text, nullable=False),
    Column("external_id", String(500)),
    Column("doc_type", String(60), nullable=False),
    Column("status", String(30), nullable=False),
    Column("revision", Integer, nullable=False, default=1),
    Column("etag", Integer, nullable=False, default=1),
    Column("data_json", JSON, nullable=False, default=dict),
    Column("issues_json", JSON, nullable=False, default=list),
    Column("artifacts_json", JSON, nullable=False, default=dict),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("idempotency_key", String(200)),
    Column("fingerprint", String(64), nullable=False),
    Column("lease_until", Float),
    Column("attempt_count", Integer, nullable=False, default=0),
    UniqueConstraint("tenant_id", "idempotency_key", name="uq_documents_tenant_idempotency"),
)
Index("ix_documents_tenant_id", documents.c.tenant_id, documents.c.id)
Index("ix_documents_queue", documents.c.status, documents.c.lease_until, documents.c.created_at)
review_events = Table(
    "review_events", metadata,
    Column("id", String(36), primary_key=True),
    Column("document_id", String(36), ForeignKey("documents.id"), nullable=False),
    Column("tenant_id", String(200), nullable=False),
    Column("subject", String(200), nullable=False),
    Column("action", String(80), nullable=False),
    Column("revision", Integer, nullable=False),
    Column("details", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
Index("ix_reviews_document_tenant", review_events.c.document_id, review_events.c.tenant_id)

# Browser access uses short-lived, scoped capabilities, never a backend API token.
# Only a digest is persisted; leaking the database does not reveal usable URLs.
access_links = Table(
    "access_links", metadata,
    Column("token_sha256", String(64), primary_key=True),
    Column("document_id", String(36), ForeignKey("documents.id"), nullable=False),
    Column("tenant_id", String(200), nullable=False),
    Column("revision", Integer, nullable=False),
    Column("etag", Integer, nullable=False),
    Column("artifact", String(30), nullable=False),
    Column("page_number", Integer),
    Column("artifact_sha256", String(64), nullable=False),
    Column("disposition", String(20), nullable=False),
    Column("expires_at", Float, nullable=False),
)
Index("ix_access_links_expiry", access_links.c.expires_at)

# Additive pilot schema: reserves survive worker restarts and ambiguous requests.
ai_budget_windows = Table(
    'ai_budget_windows', metadata,
    Column('scope', String(120), primary_key=True),
    Column('reserved_microusd', Integer, nullable=False),
)
ai_calls = Table(
    'ai_calls', metadata,
    Column('key', String(64), primary_key=True),
    Column('document_id', String(36), ForeignKey('documents.id'), nullable=False),
    Column('tenant_id', String(200), nullable=False),
    Column('revision', Integer, nullable=False),
    Column('stage', String(60), nullable=False),
    Column('model', String(100), nullable=False),
    Column('reserved_microusd', Integer, nullable=False),
    Column('status', String(30), nullable=False),
    Column('response_sha256', String(64)),
    Column('usage_json', JSON, nullable=False),
    Column('created_at', DateTime(timezone=True), nullable=False),
)

# Additive schema: SaaS credits never spend the AI dollar budget. Existing call
# rows remain immutable evidence, including historical SerpApi reservations.
saas_budget_windows=Table('saas_budget_windows',metadata,
    Column('scope',String(120),primary_key=True),
    Column('reserved_credits',Integer,nullable=False))
ai_budget_grants=Table('ai_budget_grants',metadata,
    Column('document_id',String(36),ForeignKey('documents.id'),primary_key=True),
    Column('tenant_id',String(200),nullable=False),
    Column('extra_microusd',Integer,nullable=False),
    Column('subject',String(200),nullable=False),
    Column('reason',Text,nullable=False),
    Column('revision',Integer,nullable=False),
    Column('created_at',DateTime(timezone=True),nullable=False))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _document(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    result = dict(row)
    for key in ("created_at", "updated_at"):
        value = result.get(key)
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            result[key] = value.isoformat()
    return result


class Database:
    MAX_ATTEMPTS = 3
    MUTABLE = {
        "status", "revision", "data_json", "issues_json", "artifacts_json", "lease_until",
        "attempt_count", "doc_type",
    }
    STATUSES = {"queued", "processing", "needs_review", "ready", "failed", "expired"}

    def __init__(self, settings: Settings):
        self.settings = settings
        url = make_url(settings.database_url)
        if url.get_backend_name() not in {"sqlite", "postgresql"}:
            raise ValueError("DATABASE_URL must use SQLite or PostgreSQL")
        if url.drivername == "postgresql":
            url = url.set(drivername="postgresql+psycopg")
        options: dict[str, Any] = {"pool_pre_ping": True}
        if url.get_backend_name() == "sqlite":
            if url.database and url.database != ":memory:":
                Path(url.database).resolve().parent.mkdir(parents=True, exist_ok=True)
            else:
                options["poolclass"] = StaticPool
            options["connect_args"] = {"check_same_thread": False, "timeout": 30}
        self.engine = create_engine(url, **options)
        if url.get_backend_name() == "sqlite":
            @event.listens_for(self.engine, "connect")
            def sqlite_setup(connection: Any, _record: Any) -> None:
                cursor = connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.close()

    def create_schema(self) -> None:
        metadata.create_all(self.engine)

    def get(self, document_id: str, tenant_id: str) -> dict[str, Any] | None:
        with self.engine.connect() as connection:
            return _document(connection.execute(select(documents).where(
                documents.c.id == document_id, documents.c.tenant_id == tenant_id,
            )).mappings().first())

    def create_access_link(self, row: dict[str, Any], *, token_sha256: str, artifact: str,
                           page_number: int | None, artifact_sha256: str,
                           disposition: str, expires_at: float, subject: str) -> None:
        """Issue and audit against the exact state that was authorized by the API."""
        with self.engine.begin() as connection:
            # A no-op CAS takes the same row lock as review/update on PostgreSQL.
            # This also serializes writes on SQLite, without changing its ETag.
            checked = connection.execute(update(documents).where(
                documents.c.id == row["id"], documents.c.tenant_id == row["tenant_id"],
                documents.c.etag == row["etag"], documents.c.revision == row["revision"],
            ).values(etag=documents.c.etag))
            if checked.rowcount != 1:
                raise ConcurrentUpdate("Document changed before link issuance")
            connection.execute(delete(access_links).where(access_links.c.expires_at <= time.time()))
            connection.execute(insert(access_links).values(
                token_sha256=token_sha256, document_id=row["id"], tenant_id=row["tenant_id"],
                revision=row["revision"], etag=row["etag"], artifact=artifact,
                page_number=page_number, artifact_sha256=artifact_sha256,
                disposition=disposition, expires_at=expires_at,
            ))
            self._insert_review(connection, row["id"], row["tenant_id"], subject,
                "access_link_issued", row["revision"],
                {"artifact": artifact, "page_number": page_number, "expires_at": expires_at})

    def get_access_link(self, token_sha256: str) -> dict[str, Any] | None:
        with self.engine.connect() as connection:
            row = connection.execute(select(access_links).where(
                access_links.c.token_sha256 == token_sha256,
                access_links.c.expires_at > time.time(),
            )).mappings().first()
            return dict(row) if row else None

    def revoke_original_downloads(self,row,subject):
        with self.engine.begin() as connection:
            checked=connection.execute(update(documents).where(documents.c.id==row['id'],
                documents.c.tenant_id==row['tenant_id'],documents.c.etag==row['etag']).values(etag=documents.c.etag))
            if checked.rowcount!=1:
                raise ConcurrentUpdate('Document changed before link revocation')
            count=connection.execute(delete(access_links).where(access_links.c.document_id==row['id'],
                access_links.c.tenant_id==row['tenant_id'],access_links.c.artifact=='original_download')).rowcount
            self._insert_review(connection,row['id'],row['tenant_id'],subject,'original_downloads_revoked',
                                row['revision'],{'count':count})
            return count

    def enqueue(
        self, document_id: str, tenant_id: str, source_sha: str, source_path: str | Path,
        *, external_id: str | None = None, doc_type: str = "auto",
        idempotency_key: str | None = None, fingerprint: str | None = None,
        initial_data: dict | None = None,
    ) -> tuple[dict[str, Any], bool]:
        if not tenant_id or not document_id or not source_sha:
            raise ValueError("Document identity, tenant and source hash are required")
        if idempotency_key is not None and (not idempotency_key or len(idempotency_key) > 200):
            raise ValueError("Idempotency key must contain 1 to 200 characters")
        fingerprint = fingerprint or hashlib.sha256(json.dumps(
            [source_sha, external_id, doc_type], ensure_ascii=False, separators=(",", ":"),
        ).encode()).hexdigest()
        values = dict(
            id=document_id, tenant_id=tenant_id, source_sha=source_sha,
            source_path=str(source_path), external_id=external_id, doc_type=doc_type,
            status="queued", revision=1, etag=1, data_json=initial_data or {}, issues_json=[],
            artifacts_json={}, created_at=_now(), updated_at=_now(),
            idempotency_key=idempotency_key, fingerprint=fingerprint, lease_until=None,
            attempt_count=0,
        )
        try:
            with self.engine.begin() as connection:
                connection.execute(insert(documents).values(**values))
            return _document(values), True
        except IntegrityError:
            if idempotency_key is None:
                raise
            with self.engine.connect() as connection:
                existing = _document(connection.execute(select(documents).where(
                    documents.c.tenant_id == tenant_id,
                    documents.c.idempotency_key == idempotency_key,
                )).mappings().first())
            if existing is None:
                raise
            if existing["fingerprint"] != fingerprint:
                raise IdempotencyConflict("Idempotency key already used for different content") from None
            return existing, False

    def claim_next(self) -> dict[str, Any] | None:
        """Worker-only cross-tenant selector; every mutation includes the selected tenant."""
        now = time.time()
        with self.engine.begin() as connection:
            # Expired final attempts cannot be silently retried forever.
            exhausted = connection.execute(select(documents).where(
                documents.c.status == "processing", documents.c.lease_until <= now,
                documents.c.attempt_count >= self.MAX_ATTEMPTS,
            )).mappings().all()
            for row in exhausted:
                connection.execute(update(documents).where(
                    documents.c.id == row["id"], documents.c.tenant_id == row["tenant_id"],
                    documents.c.etag == row["etag"], documents.c.status == "processing",
                    documents.c.lease_until <= now,
                ).values(status="failed", lease_until=None, updated_at=_now(),
                         etag=documents.c.etag + 1,
                         issues_json=[{"code": "worker_attempts_exhausted", "message": "Processing attempts exhausted"}]))
            eligible = and_(
                documents.c.attempt_count < self.MAX_ATTEMPTS,
                or_(documents.c.status == "queued", and_(
                    documents.c.status == "processing", documents.c.lease_until <= now,
                )),
            )
            statement = select(documents).where(eligible).order_by(documents.c.created_at, documents.c.id).limit(1)
            if self.engine.dialect.name == "postgresql":
                statement = statement.with_for_update(skip_locked=True)
            row = connection.execute(statement).mappings().first()
            if row is None:
                return None
            claimed = connection.execute(update(documents).where(
                documents.c.id == row["id"], documents.c.tenant_id == row["tenant_id"],
                documents.c.etag == row["etag"], eligible,
            ).values(status="processing", etag=documents.c.etag + 1,
                     attempt_count=documents.c.attempt_count + 1, updated_at=_now(),
                     lease_until=now + self.settings.worker_timeout_seconds + 30))
            if claimed.rowcount != 1:
                return None
            return _document(connection.execute(select(documents).where(
                documents.c.id == row["id"], documents.c.tenant_id == row["tenant_id"],
            )).mappings().one())

    def update_document(
        self, document_id: str, tenant_id: str, expected_etag: int | None = None, **updates: Any,
    ) -> dict[str, Any] | None:
        review_event = updates.pop("review_event", None)
        budget_grant = updates.pop('budget_grant',None)
        if not updates or set(updates) - self.MUTABLE:
            raise ValueError("Invalid document update fields")
        if "status" in updates and updates["status"] not in self.STATUSES:
            raise ValueError("Invalid document status")
        if "revision" in updates and (not isinstance(updates["revision"], int) or updates["revision"] < 1):
            raise ValueError("Invalid revision")
        if updates.get("status") in {"ready", "needs_review", "failed", "expired", "queued"}:
            updates.setdefault("lease_until", None)
        with self.engine.begin() as connection:
            filters = [documents.c.id == document_id, documents.c.tenant_id == tenant_id]
            if expected_etag is not None:
                filters.append(documents.c.etag == expected_etag)
            result = connection.execute(update(documents).where(*filters).values(
                **updates, updated_at=_now(), etag=documents.c.etag + 1,
            ))
            if result.rowcount != 1:
                exists = connection.execute(select(documents.c.id).where(
                    documents.c.id == document_id, documents.c.tenant_id == tenant_id,
                )).first()
                if exists is not None:
                    raise ConcurrentUpdate("Document was changed by another operation")
                return None
            if budget_grant is not None:
                connection.execute(insert(ai_budget_grants).values(document_id=document_id,
                    tenant_id=tenant_id,created_at=_now(),**budget_grant))
            if review_event is not None:
                self._insert_review(connection, document_id, tenant_id, **review_event)
            return _document(connection.execute(select(documents).where(
                documents.c.id == document_id, documents.c.tenant_id == tenant_id,
            )).mappings().one())

    @staticmethod
    def _insert_review(
        connection: Any, document_id: str, tenant_id: str, subject: str, action: str,
        revision: int, details: dict[str, Any],
    ) -> dict[str, Any]:
        values = dict(id=str(uuid4()), document_id=document_id, tenant_id=tenant_id,
                      subject=subject, action=action, revision=revision,
                      details=details, created_at=_now())
        connection.execute(insert(review_events).values(**values))
        return _document(values)

    def record_review(
        self, document_id: str, tenant_id: str, subject: str, action: str,
        revision: int, details: dict[str, Any],
    ) -> dict[str, Any] | None:
        with self.engine.begin() as connection:
            exists = connection.execute(select(documents.c.id).where(
                documents.c.id == document_id, documents.c.tenant_id == tenant_id,
            )).first()
            if exists is None:
                return None
            return self._insert_review(connection, document_id, tenant_id, subject, action, revision, details)

    def reviews(self, document_id: str, tenant_id: str) -> list[dict[str, Any]]:
        with self.engine.connect() as connection:
            return [_document(row) for row in connection.execute(select(review_events).where(
                review_events.c.document_id == document_id,
                review_events.c.tenant_id == tenant_id,
            ).order_by(review_events.c.created_at, review_events.c.id)).mappings()]
