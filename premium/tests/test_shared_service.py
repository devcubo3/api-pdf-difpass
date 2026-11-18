"""Single-service routing and activation are independent of customer PDFs/AI."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('shared_start', ROOT/'shared_service/start.py')
shared = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shared)


def test_activation_requires_explicit_flag_and_valid_public_port(monkeypatch):
    monkeypatch.delenv('PDF_PREMIUM_ENABLED', raising=False)
    assert not shared.enabled()
    monkeypatch.setenv('PDF_PREMIUM_ENABLED', 'true')
    assert shared.enabled()
    for value in ('80', '18101', '18102', '65536', 'not-a-port'):
        monkeypatch.setenv('PORT', value)
        with pytest.raises(ValueError): shared.public_port()


def test_shared_credentials_are_private_and_select_html_without_postgres(tmp_path):
    run = subprocess.run([sys.executable, str(ROOT/'scripts/setup_env.py'), '--shared-service'],
                         cwd=tmp_path, capture_output=True, text=True)
    assert run.returncode == 0
    content = (tmp_path/'.env').read_text(encoding='utf-8')
    assert 'PDF_PREMIUM_ENABLED=true' in content
    assert 'DATABASE_URL=sqlite:////app/var/state.sqlite3' in content
    assert 'PDF_RENDER_MODE=html_template' in content
    assert 'PDF_AI_DOCUMENT_BUDGET_USD=0.15' in content
    token_line = next(line for line in content.splitlines() if line.startswith('PDF_API_TOKENS='))
    tokens = json.loads(token_line.split('=', 1)[1].strip("'"))
    assert len(tokens) == 1 and next(iter(tokens)) not in run.stdout + run.stderr
    second = subprocess.run([sys.executable, str(ROOT/'scripts/setup_env.py'), '--shared-service'],
                            cwd=tmp_path, capture_output=True)
    assert second.returncode != 0
    assert (tmp_path/'.env').read_text(encoding='utf-8') == content
