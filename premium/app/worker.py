"""Durable SQL queue worker. Each PDF runs in a killable isolated process."""
import argparse
import json
import multiprocessing
import time
from .config import Settings
from .db import Database, ConcurrentUpdate
from .pipeline import process_claim
from .storage import Storage


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--env-file')
    args = parser.parse_args()
    if args.env_file:
        from dotenv import load_dotenv
        load_dotenv(args.env_file, override=False)
    settings = Settings.from_env()
    store = Storage(settings.data_dir)
    db = Database(settings)
    db.create_schema()

    def heartbeat():
        store._write_atomic(store.root / 'worker-heartbeat.json', json.dumps({'timestamp': time.time()}).encode())

    try:
        while True:
            heartbeat()
            row = db.claim_next()
            if row:
                child = multiprocessing.get_context('spawn').Process(target=process_claim, args=(settings, row))
                child.start()
                started = time.monotonic()
                try:
                    while child.is_alive() and time.monotonic()-started < settings.worker_timeout_seconds:
                        child.join(timeout=2)
                        heartbeat()
                    timed_out = child.is_alive()
                    if timed_out:
                        child.terminate()
                        child.join(timeout=5)
                        if child.is_alive():
                            child.kill()
                            child.join(timeout=5)
                    if timed_out or child.exitcode != 0:
                        try:
                            current=db.get(row['id'],row['tenant_id'])
                            # Canonical checkpoints advance ETag without changing
                            # ownership. Never clobber another claim or a final result.
                            if (current and current['status']=='processing' and current['revision']==row['revision']
                                    and current['attempt_count']==row['attempt_count'] and current['lease_until']==row['lease_until']):
                                db.update_document(row['id'], row['tenant_id'], current['etag'],
                                    status='queued' if row['attempt_count'] < db.MAX_ATTEMPTS else 'failed',
                                    issues_json=[{'code':'WORKER_TIMEOUT' if timed_out else 'WORKER_CRASH',
                                                  'blocking':True, 'message':'Processamento interrompido.'}])
                        except ConcurrentUpdate:
                            pass
                finally:
                    if child.is_alive():
                        child.terminate()
                        child.join(timeout=5)
            if args.once:
                return
            if not row:
                time.sleep(1)
    finally:
        db.engine.dispose()


if __name__ == '__main__':
    main()
