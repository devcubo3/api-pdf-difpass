"""Docker fixture for the synthetic HTTP smoke; no application imports or keys."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

import httpx


class SharedRuntime:
    def __init__(self, image, env, output, port):
        self.image, self.env, self.output, self.port = image, env, output, port
        self.name = 'difftravel-synthetic-' + uuid4().hex[:12]
        self.volume = self.name + '-data'
        self.log = None
        self.process = None

    def docker(self, *args, check=True):
        return subprocess.run(['docker', *args], check=check, capture_output=True, text=True)

    def start(self, enabled=True, persistent=True):
        values = {key: value for key, value in self.env.items()
                  if key.startswith(('PDF_', 'OPENAI_', 'DATABASE_URL', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY'))}
        values.update({'PORT': '8000', 'PDF_PREMIUM_ENABLED': str(enabled).lower(),
            'PDF_DATA_DIR': '/app/var', 'DATABASE_URL': 'sqlite:////app/var/state.sqlite3',
            'PDF_MEDIA_OPENAI_SEARCH_ENABLED': 'false'})
        values['OPENAI_BASE_URL'] = values['OPENAI_BASE_URL'].replace('127.0.0.1', 'host.docker.internal')
        env_file = self.output / (self.name + '.env')
        with env_file.open('x', encoding='utf-8') as stream:
            stream.write('\n'.join(f'{key}={value}' for key, value in values.items()) + '\n')
        env_file.chmod(0o600)
        command = ['docker', 'run', '--name', self.name, '--env-file', str(env_file),
            '--publish', f'127.0.0.1:{self.port}:8000', '--add-host', 'host.docker.internal:host-gateway',
            '--cap-drop', 'ALL', '--read-only', '--tmpfs', '/tmp:size=256m,mode=1777',
            '--memory', '2g', '--pids-limit', '256', '--security-opt', 'no-new-privileges:true',
            '--security-opt', 'seccomp=' + str(Path(__file__).resolve().parents[1]/'seccomp-chromium.json')]
        if persistent:
            self.docker('volume', 'create', self.volume)
            command.extend(['--volume', self.volume + ':/app/var'])
        command.append(self.image)
        self.log = (self.output/(self.name+'.log')).open('wb')
        self.process = subprocess.Popen(command, stdout=self.log, stderr=self.log)
        return self.process

    def wait_healthy(self, client, base):
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                if client.get(base+'/readyz').status_code == 200:
                    return
            except httpx.TransportError:
                pass
            time.sleep(.3)
        raise RuntimeError('Shared service readiness failed; inspect private synthetic logs')

    def legacy_check(self, base):
        subprocess.run([sys.executable, str(Path(__file__).with_name('smoke_legacy_http.py')),
                        '--base-url', base], check=True)

    def verify_recovery(self, client, base, route, sha):
        self.legacy_check(base)
        assert '/v2/documents' in client.get(base+'/premium/openapi.json').json()['paths']
        assert client.get(base+'/v2/documents/nonexistent').status_code == 404
        with httpx.Client(trust_env=False) as unauthenticated:
            assert unauthenticated.get(base+route+'/review').status_code == 401
        # Kill only the worker. The supervisor must recover it while the old
        # endpoint continues to produce two-page PDFs through the same gateway.
        find_worker = "import os; from pathlib import Path; pids=[int(p.name) for p in Path('/proc').iterdir() if p.name.isdigit() and p.name!=str(os.getpid()) and (p/'cmdline').exists() and b'app.worker' in (p/'cmdline').read_bytes()]; assert len(pids)==1; "
        kill_worker = find_worker + "import signal; print(pids[0]); os.kill(pids[0],signal.SIGKILL)"
        old_pid = int(self.docker('exec', self.name, '/opt/premium/bin/python', '-c', kill_worker).stdout)
        self.legacy_check(base)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                new_pid = int(self.docker('exec', self.name, '/opt/premium/bin/python', '-c',
                                         find_worker + 'print(pids[0])').stdout)
                assert new_pid != old_pid
                self.docker('exec', self.name, '/opt/premium/bin/python', '/app/premium/shared_service/health.py')
                break
            except (subprocess.CalledProcessError, AssertionError):
                time.sleep(.5)
        else:
            raise RuntimeError('Shared worker failed to recover')
        self.docker('restart', '--time', '35', self.name)
        self.wait_healthy(client, base)
        saved = client.get(base+route+'/review').json()
        assert saved['artifact_sha256'] == sha and saved['status'] == 'needs_review'
        self.legacy_check(base)

    def close(self):
        # Only this explicitly named synthetic fixture and its generated volume.
        self.docker('rm', '--force', self.name, check=False)
        if self.process:
            self.process.wait(timeout=30)
        if self.log:
            self.log.close()
        self.docker('volume', 'rm', self.volume, check=False)


def verify_fallbacks(image, env, output, port):
    for enabled in (False, True):
        runtime = SharedRuntime(image, env, output, port)
        try:
            runtime.start(enabled=enabled, persistent=False)
            base = f'http://127.0.0.1:{port}'
            runtime.legacy_check(base)
            if enabled:
                with httpx.Client(trust_env=False) as client:
                    assert client.get(base+'/healthz').status_code == 503
                    assert client.get(base+'/v2/documents/nonexistent').status_code == 503
        finally:
            runtime.close()
