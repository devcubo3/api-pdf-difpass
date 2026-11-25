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
    assert 'PDF_AI_DOCUMENT_BUDGET_USD=0.25' in content
    assert 'PDF_AI_DAILY_BUDGET_USD=1.00' in content
    token_line = next(line for line in content.splitlines() if line.startswith('PDF_API_TOKENS='))
    tokens = json.loads(token_line.split('=', 1)[1].strip("'"))
    assert len(tokens) == 1 and next(iter(tokens)) not in run.stdout + run.stderr
    second = subprocess.run([sys.executable, str(ROOT/'scripts/setup_env.py'), '--shared-service'],
                            cwd=tmp_path, capture_output=True)
    assert second.returncode != 0
    assert (tmp_path/'.env').read_text(encoding='utf-8') == content


@pytest.mark.parametrize('message,expected',[
    ('No usable sandbox! command --user-data-dir=/private/profile', 'CHROMIUM_SANDBOX_DENIED'),
    ('Chromium sandboxing failed!', 'CHROMIUM_SANDBOX_DENIED'),
    ('namespace: Operation not permitted', 'CHROMIUM_SANDBOX_DENIED'),
    ('pthread_create: Resource temporarily unavailable', 'CHROMIUM_RESOURCES_UNAVAILABLE'),
    ('Executable does not exist; executable not found', 'CHROMIUM_INSTALLATION_INCOMPLETE'),
    ('Permission denied while starting process', 'CHROMIUM_EXECUTION_DENIED'),
    ('Target page, context or browser has been closed', 'CHROMIUM_STARTUP_FAILED'),
])
def test_browser_failure_diagnostics_are_specific_without_copying_raw_message(message,expected,capsys):
    secret = 'SYNTHETIC_SECRET_DO_NOT_LOG'
    error = RuntimeError(message + ' OPENAI_API_KEY=' + secret)
    code = shared.browser_failure_code(error)
    assert code == expected
    shared.log_startup_failure(shared.StartupFailure('browser', code, 'TargetClosedError'))
    output = capsys.readouterr().out
    assert expected in output and 'stage=browser' in output
    assert secret not in output and '/private/profile' not in output


def test_unknown_preflight_failure_does_not_log_connection_credentials(capsys):
    shared.log_startup_failure(RuntimeError('postgresql://name:SYNTHETIC_PASSWORD@db/database'))
    output = capsys.readouterr().out
    assert 'PREMIUM_PREFLIGHT_FAILED' in output
    assert 'SYNTHETIC_PASSWORD' not in output and 'postgresql://' not in output
