"""Probe the shared service without calling any AI provider."""
import json
import os
from pathlib import Path
import time
import urllib.request


def main():
    base = 'http://127.0.0.1:' + os.getenv('PORT', '8000')
    with urllib.request.urlopen(base + '/openapi.json', timeout=3) as response:
        assert '/generate-pdf/' in json.load(response)['paths']
    if os.getenv('PDF_PREMIUM_ENABLED', 'false').lower() == 'true':
        with urllib.request.urlopen(base + '/healthz', timeout=3) as response:
            health = json.load(response)
            assert health['render_mode'] == 'html_template'
            assert health['pipeline_version'] == 'canonical_v1'
        with urllib.request.urlopen(base + '/readyz', timeout=3) as response:
            assert json.load(response)['status'] == 'ready'
        heartbeat = Path(os.getenv('PDF_DATA_DIR', '/app/var')) / 'worker-heartbeat.json'
        assert heartbeat.exists() and time.time() - heartbeat.stat().st_mtime < 120


if __name__ == '__main__':
    main()
