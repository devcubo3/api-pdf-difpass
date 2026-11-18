"""Start independent legacy/premium runtimes behind one public HTTP port.

No provider calls are made during startup. The original image and Procfile stay
available for rollback; this entry point is used only by Dockerfile.shared.
"""
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
PREMIUM = ROOT / 'premium'
RUNTIME = Path('/tmp/difftravel-shared')
LEGACY_PORT = 18101
PREMIUM_PORT = 18102


def enabled():
    return os.getenv('PDF_PREMIUM_ENABLED', 'false').lower() == 'true'


def public_port():
    port = int(os.getenv('PORT', '8000'))
    if not 1024 <= port <= 65535 or port in {LEGACY_PORT, PREMIUM_PORT}:
        raise ValueError('PORT must be an unprivileged port distinct from the internal ports')
    return port


def prepare_premium():
    defaults = {
        'PDF_DATA_DIR': '/app/var', 'PDF_RENDER_MODE': 'html_template',
        'PDF_PIPELINE_VERSION': 'canonical_v1', 'PDF_REVIEW_POLICY': 'always',
        'PDF_WORKER_TIMEOUT_SECONDS': '600', 'PDF_AI_DOCUMENT_BUDGET_USD': '0.15',
        'PDF_AI_DAILY_BUDGET_USD': '0.30', 'PDF_MEDIA_MODE': 'search',
        'PDF_MEDIA_OPENAI_SEARCH_ENABLED': 'true',
        'PDF_MEDIA_SEARCH_RESERVE_USD': '0.05', 'PDF_MEDIA_SEARCH_DAILY_BUDGET_USD': '0.10',
    }
    for key, value in defaults.items():
        os.environ.setdefault(key, value)
    data = Path(os.environ['PDF_DATA_DIR'])
    if not data.is_absolute() or not data.is_mount():
        raise ValueError('Mount a persistent volume directly at PDF_DATA_DIR before enabling premium')
    os.environ.setdefault('DATABASE_URL', f'sqlite:///{data.as_posix()}/state.sqlite3')
    sys.path.insert(0, str(PREMIUM))
    from app.config import Settings
    from app.db import Database
    from app.storage import Storage
    settings = Settings.from_env()
    if settings.render_mode != 'html_template' or settings.pipeline_version != 'canonical_v1':
        raise ValueError('Shared deployment requires html_template and canonical_v1')
    if not settings.public_base_url or not settings.openai_api_key or not settings.api_tokens:
        raise ValueError('Configure the public HTTPS origin, OpenAI key and premium API tokens')
    # The parent holds this OS lock across the supervisor's lifetime, preventing
    # overlapping replicas/redeployments from sharing this SQL queue.
    import fcntl
    lock = (data / '.shared-service.lock').open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise ValueError('This volume already has an active shared-service instance') from None
    Storage(data)
    database = Database(settings)
    try:
        database.create_schema()
    finally:
        database.engine.dispose()
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(chromium_sandbox=True)
        browser.close()
    return lock


def nginx_config(port, premium_available):
    common = '''proxy_http_version 1.1;
        proxy_set_header Host $http_host;
        proxy_set_header Connection "";
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $http_x_forwarded_proto;
        proxy_request_buffering off;
        proxy_buffering off;
        proxy_read_timeout 650s;
        proxy_send_timeout 650s;'''
    locations = []
    for match, path in [('^~ /v2/', ''), ('= /v2', ''), ('= /healthz', ''),
                        ('= /readyz', ''), ('= /premium/openapi.json', '/openapi.json')]:
        body = (f'{common}\n proxy_pass http://127.0.0.1:{PREMIUM_PORT}{path};'
                if premium_available else
                '''default_type application/json;
                return 503 '{"detail":"Premium unavailable; check service configuration."}';''')
        locations.append(f'location {match} {{ {body} }}')
    return f'''pid {RUNTIME}/nginx.pid;
error_log /dev/stderr crit;
worker_processes 1;
events {{ worker_connections 512; }}
http {{
    access_log off;
    client_body_temp_path {RUNTIME}/body;
    proxy_temp_path {RUNTIME}/proxy;
    fastcgi_temp_path {RUNTIME}/fastcgi;
    uwsgi_temp_path {RUNTIME}/uwsgi;
    scgi_temp_path {RUNTIME}/scgi;
    server {{
        listen {port};
        client_max_body_size 0;
        {chr(10).join(locations)}
        location / {{ {common}\n proxy_pass http://127.0.0.1:{LEGACY_PORT}; }}
    }}
}}
'''


def supervisor_config(premium_available):
    programs = [('legacy', '/opt/legacy/bin/python -m uvicorn legacy_entry:app '
        '--app-dir /app/premium/shared_service '
        f'--host 127.0.0.1 --port {LEGACY_PORT} --workers 1 --no-access-log', ROOT)]
    if premium_available:
        programs.extend([
            ('premium', '/opt/premium/bin/python -m uvicorn app.main:app '
                f'--host 127.0.0.1 --port {PREMIUM_PORT} --workers 1 --no-access-log', PREMIUM),
            ('worker', '/opt/premium/bin/python -m app.worker', PREMIUM),
        ])
    programs.append(('gateway', f'/usr/sbin/nginx -c {RUNTIME}/nginx.conf -g "daemon off;"', ROOT))
    sections = [f'''[supervisord]
nodaemon=true
pidfile={RUNTIME}/supervisord.pid
logfile={RUNTIME}/supervisord.log
logfile_maxbytes=512KB
logfile_backups=1
minfds=256
minprocs=128
''']
    for name, command, directory in programs:
        sections.append(f'''[program:{name}]
command={command}
directory={directory}
autostart=true
autorestart=true
startsecs=2
startretries=10
stopasgroup=true
killasgroup=true
stopwaitsecs=25
stdout_logfile=/dev/fd/1
stdout_logfile_maxbytes=0
stderr_logfile=/dev/fd/2
stderr_logfile_maxbytes=0
''')
    return '\n'.join(sections)


def main():
    port = public_port()
    if not enabled():
        os.chdir(ROOT)
        os.execv('/opt/legacy/bin/python', ['/opt/legacy/bin/python', '-m', 'uvicorn',
            'legacy_entry:app', '--app-dir', str(PREMIUM/'shared_service'),
            '--host', '0.0.0.0', '--port', str(port), '--workers', '1', '--no-access-log'])
    RUNTIME.mkdir(mode=0o700, exist_ok=True)
    for name in ('body', 'proxy', 'fastcgi', 'uwsgi', 'scgi'):
        (RUNTIME / name).mkdir(mode=0o700, exist_ok=True)
    available = False
    lock = None
    try:
        lock = prepare_premium()
        available = True
    except Exception as error:
        # Never log exception strings: SQL/browser exceptions can contain secrets.
        print(f'Premium startup failed ({type(error).__name__}); legacy remains available. '
              'Verify storage, credentials, volume ownership and Chromium sandbox.', flush=True)
    (RUNTIME / 'state.json').write_text(json.dumps({'premium_available': available}), encoding='utf-8')
    (RUNTIME / 'nginx.conf').write_text(nginx_config(port, available), encoding='utf-8')
    (RUNTIME / 'supervisord.conf').write_text(supervisor_config(available), encoding='utf-8')
    # Supervisor closes inherited fds during startup, so fd ownership cannot be
    # delegated to it. Keep a tiny parent alive to own the instance lock and relay
    # termination to its supervised process group.
    import signal
    import subprocess
    child = subprocess.Popen(['/usr/bin/supervisord', '-c', str(RUNTIME / 'supervisord.conf')],
                             start_new_session=True)
    def terminate(_signum, _frame):
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    try:
        raise SystemExit(child.wait())
    finally:
        if lock:
            lock.close()


if __name__ == '__main__':
    main()
