"""Real PostgreSQL concurrency check, enabled only against a dedicated test DB."""
import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4
import pytest
from sqlalchemy.engine import make_url
from app.config import Settings
from app.db import Database


def test_postgres_single_claim_and_tenant_isolation(tmp_path):
    url = os.environ.get('PDF_TEST_DATABASE_URL')
    if not url:
        pytest.skip('PostgreSQL test URL not configured')
    if make_url(url).database != 'difftravel_test':
        pytest.fail('Use a dedicated database named difftravel_test')
    db = Database(Settings(data_dir=tmp_path, database_url=url))
    try:
        db.create_schema()
        identifier, tenant = str(uuid4()), 'test-' + uuid4().hex
        db.enqueue(identifier, tenant, 'a'*64, tmp_path/'source.pdf')
        assert db.get(identifier, 'other-tenant') is None
        with ThreadPoolExecutor(max_workers=8) as pool:
            claims = list(pool.map(lambda _: db.claim_next(), range(8)))
        assert sum(c is not None and c['id'] == identifier for c in claims) == 1
        claimed = db.get(identifier, tenant)
        assert claimed['status'] == 'processing' and claimed['attempt_count'] == 1
    finally:
        db.engine.dispose()
