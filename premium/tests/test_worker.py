"""Exercise actual spawn/timeout worker entrypoint, beyond in-process helpers."""
import hashlib
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4
from app.config import Settings
from app.db import Database
from app.storage import Storage
from test_api import synthetic_pdf


def test_worker_processes_a_durable_job_in_a_child_process(tmp_path):
    root = tmp_path/'private'
    settings = Settings(data_dir=root, database_url=f'sqlite:///{root.as_posix()}/state.db', render_dpi=120)
    store, db = Storage(root), Database(settings)
    db.create_schema()
    raw = synthetic_pdf()
    document_id = str(uuid4())
    source = store.commit_upload(store.stage_upload(raw), document_id)
    db.enqueue(document_id, 'worker-test', hashlib.sha256(raw).hexdigest(), source)
    env = {**os.environ, 'PDF_DATA_DIR':str(root), 'DATABASE_URL':settings.database_url,
           'PDF_RENDER_DPI':'120', 'PDF_REVIEW_POLICY':'always', 'PDF_API_TOKENS':'{}',
           'PDF_BRAND_DIR':str(settings.brand_dir), 'PDF_WORKER_TIMEOUT_SECONDS':'30',
           'PDF_RENDER_MODE':'local'}
    result = subprocess.run([sys.executable, '-m', 'app.worker', '--once'],
        cwd=Path(__file__).resolve().parents[1], env=env, capture_output=True, timeout=45)
    assert result.returncode == 0, result.stderr.decode(errors='replace')
    row = db.get(document_id, 'worker-test')
    assert row['status'] == 'needs_review', row['issues_json']
    assert row['artifacts_json']['checks']['validations_passed'] is True
    assert (root/'worker-heartbeat.json').is_file()
    db.engine.dispose()
