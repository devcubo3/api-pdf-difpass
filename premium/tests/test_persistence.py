from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest

from app.config import Settings
from app.db import ConcurrentUpdate, Database, IdempotencyConflict
from app.storage import Storage


@pytest.fixture
def database(tmp_path):
    settings = Settings(data_dir=tmp_path, database_url=f"sqlite:///{(tmp_path / 'state.db').as_posix()}")
    database = Database(settings)
    database.create_schema()
    yield database
    database.engine.dispose()


def enqueue(database, tenant="agency-a", **kwargs):
    return database.enqueue(str(uuid4()), tenant, "a" * 64, "private/source.pdf", **kwargs)


def test_configuration_does_not_expose_secrets(monkeypatch, tmp_path):
    monkeypatch.setenv("PDF_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "test-secret-key")
    monkeypatch.setenv("PDF_API_TOKENS", '{"token-secret":{"tenant_id":"a","subject":"person","roles":["read"]}}')
    settings = Settings.from_env()
    assert settings.data_dir == tmp_path
    assert settings.api_tokens["token-secret"]["tenant_id"] == "a"
    assert "test-secret-key" not in repr(settings)
    assert "token-secret" not in repr(settings)
    with pytest.raises(ValueError, match="review_policy"):
        Settings(review_policy="auto_if_valid")


def test_idempotency_is_scoped_to_tenant_and_compares_fingerprint(database):
    original, created = enqueue(database, idempotency_key="request-1", fingerprint="b" * 64)
    repeated, created_again = enqueue(database, idempotency_key="request-1", fingerprint="b" * 64)
    assert created and not created_again
    assert repeated["id"] == original["id"]
    with pytest.raises(IdempotencyConflict):
        enqueue(database, idempotency_key="request-1", fingerprint="c" * 64)
    independent, created = enqueue(database, "agency-b", idempotency_key="request-1", fingerprint="c" * 64)
    assert created and independent["id"] != original["id"]


def test_tenant_filter_and_compare_and_swap(database):
    row, _ = enqueue(database)
    assert database.get(row["id"], "agency-b") is None
    assert database.update_document(row["id"], "agency-b", row["etag"], status="failed") is None
    changed = database.update_document(row["id"], "agency-a", row["etag"], status="needs_review")
    assert changed["etag"] == row["etag"] + 1
    with pytest.raises(ConcurrentUpdate):
        database.update_document(row["id"], "agency-a", row["etag"], status="ready")
    assert database.get(row["id"], "agency-a")["status"] == "needs_review"


def test_parallel_workers_cannot_claim_the_same_job(database):
    row, _ = enqueue(database)
    with ThreadPoolExecutor(max_workers=8) as executor:
        claims = list(executor.map(lambda _: database.claim_next(), range(8)))
    successful = [item for item in claims if item is not None]
    assert len(successful) == 1
    assert successful[0]["id"] == row["id"]
    assert successful[0]["attempt_count"] == 1
    assert database.claim_next() is None


def test_expired_lease_is_recovered_and_old_worker_cannot_publish(database):
    row, _ = enqueue(database)
    first = database.claim_next()
    database.update_document(row["id"], row["tenant_id"], first["etag"], lease_until=1)
    recovered = database.claim_next()
    assert recovered["attempt_count"] == 2
    with pytest.raises(ConcurrentUpdate):
        database.update_document(row["id"], row["tenant_id"], first["etag"], status="ready")
    database.update_document(row["id"], row["tenant_id"], recovered["etag"], lease_until=1)
    final_attempt = database.claim_next()
    assert final_attempt["attempt_count"] == 3
    database.update_document(row["id"], row["tenant_id"], final_attempt["etag"], lease_until=1)
    assert database.claim_next() is None
    exhausted = database.get(row["id"], row["tenant_id"])
    assert exhausted["status"] == "failed"
    assert exhausted["issues_json"][0]["code"] == "worker_attempts_exhausted"


def test_review_audit_is_tenant_scoped_and_atomic_with_update(database):
    row, _ = enqueue(database)
    assert database.record_review(row["id"], "agency-b", "reviewer", "approve", 1, {}) is None
    event = {"subject": "reviewer", "action": "approve", "revision": 1, "details": {"sha": "expected"}}
    database.update_document(row["id"], "agency-a", row["etag"], status="ready", review_event=event)
    assert database.reviews(row["id"], "agency-b") == []
    audit = database.reviews(row["id"], "agency-a")
    assert len(audit) == 1 and audit[0]["details"]["sha"] == "expected"
    current = database.get(row["id"], "agency-a")
    with pytest.raises(TypeError):
        database.update_document(row["id"], "agency-a", current["etag"], status="failed", review_event={})
    assert database.get(row["id"], "agency-a")["status"] == "ready"


def test_storage_source_is_immutable_and_names_cannot_escape(tmp_path):
    storage = Storage(tmp_path / "private")
    identifier = str(uuid4())
    staged = storage.stage_upload(b"original")
    original = storage.commit_upload(staged, identifier)
    assert not staged.exists() and storage.read(original) == b"original"
    duplicate = storage.stage_upload(b"replacement")
    with pytest.raises(FileExistsError):
        storage.commit_upload(duplicate, identifier)
    assert original.read_bytes() == b"original"
    storage.delete_staged(duplicate)
    artifact = storage.atomic_write(identifier, 1, "preview.png", b"complete")
    assert artifact.read_bytes() == b"complete"
    assert not list(artifact.parent.glob("*.part"))
    for name in ("../bad.pdf", "a\\bad.pdf", "C:bad.pdf", ".."):
        with pytest.raises(ValueError):
            storage.atomic_write(identifier, 1, name, b"bad")
    with pytest.raises(ValueError):
        storage.document_dir("../outside")
    with pytest.raises(ValueError):
        storage.read(tmp_path / "outside.pdf")


def test_revision_storage_is_separate(tmp_path):
    storage = Storage(tmp_path)
    identifier = str(uuid4())
    first = storage.atomic_write(identifier, 1, "premium.pdf", b"revision one")
    second = storage.atomic_write(identifier, 2, "premium.pdf", b"revision two")
    assert first != second
    assert first.read_bytes() == b"revision one"
    assert second.read_bytes() == b"revision two"
