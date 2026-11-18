"""Explicit portable renderer; synthetic content and no external provider."""
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.weasy_rendering import check_runtime,MeasuredDocument
from test_shared_service import shared


def test_unknown_html_engine_cannot_silently_fall_back(monkeypatch):
    monkeypatch.setenv('PDF_HTML_ENGINE','unknown')
    with pytest.raises(ValueError,match='PDF_HTML_ENGINE'):
        Settings.from_env()


def test_portable_startup_does_not_launch_chromium_or_call_openai(monkeypatch):
    def forbidden(*args,**kwargs):
        raise AssertionError('Browser/provider must not be started')
    monkeypatch.setattr('playwright.sync_api.sync_playwright',forbidden)
    monkeypatch.setattr('openai.OpenAI',forbidden)
    shared.validate_html_runtime(SimpleNamespace(html_engine='weasyprint'))


def test_portable_runtime_failure_remains_sanitized(monkeypatch,capsys):
    def failed():
        raise RuntimeError('PRIVATE_KEY_AND_PATH_DO_NOT_LOG')
    monkeypatch.setattr('app.weasy_rendering.check_runtime',failed)
    with pytest.raises(shared.StartupFailure) as captured:
        shared.validate_html_runtime(SimpleNamespace(html_engine='weasyprint'))
    shared.log_startup_failure(captured.value)
    output=capsys.readouterr().out
    assert 'code=HTML_ENGINE_UNAVAILABLE' in output
    assert 'PRIVATE_KEY_AND_PATH_DO_NOT_LOG' not in output
