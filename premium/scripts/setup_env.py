"""Generate unique private credentials, never print them or overwrite an existing file."""
import argparse
import json
import secrets
from pathlib import Path

parser = argparse.ArgumentParser()
mode = parser.add_mutually_exclusive_group()
mode.add_argument('--docker', action='store_true')
mode.add_argument('--shared-service', action='store_true')
args = parser.parse_args()
target = Path('.env')
tokens = {secrets.token_urlsafe(32): {'tenant_id':'difftravel', 'subject':'premium-local',
                                   'roles':['upload','read','review']}}
password = secrets.token_hex(32)
database = (f'postgresql+psycopg://difftravel_premium:{password}@db:5432/difftravel_premium'
            if args.docker else 'sqlite:///var/state.db')
content = '\n'.join([
    'PDF_DATA_DIR=var', f'DATABASE_URL={database}',
    "PDF_API_TOKENS='" + json.dumps(tokens, separators=(',',':')) + "'",
    'PDF_REVIEW_POLICY=always', 'PDF_RENDER_DPI=240',
    'PDF_WORKER_TIMEOUT_SECONDS=7200', f'POSTGRES_PASSWORD={password}',
    'PDF_RENDER_MODE=openai_images', 'OPENAI_API_KEY=',
    'PDF_PIPELINE_VERSION=legacy','PDF_MAX_OUTPUT_PAGES=100',
    'PDF_LEGACY_PDF_HOSTS={}','PDF_REQUIRE_LEGACY_PDF_URL=false',
    'PDF_PUBLIC_BASE_URL=', 'PDF_ORIGINAL_LINK_TTL_DAYS=365',
    'OPENAI_TEXT_MODEL=gpt-6-luna', 'OPENAI_IMAGE_MODEL=gpt-image-2',
    'PDF_AI_MAX_PAGES=16', 'PDF_AI_REQUEST_TIMEOUT_SECONDS=180',
    'PDF_AI_DOCUMENT_BUDGET_USD=12', 'PDF_AI_DAILY_BUDGET_USD=24', '',
])
if not args.docker:
    tesseract = Path('C:/Program Files/Tesseract-OCR/tesseract.exe')
    languages = Path('var/ocr').resolve()
    if tesseract.is_file():
        content += f'TESSERACT_CMD={tesseract.as_posix()}\n'
    if all((languages / f'{language}.traineddata').is_file() for language in ('por', 'eng')):
        content += f'TESSDATA_PREFIX={languages.as_posix()}\n'
if args.shared_service:
    lines = dict(line.split('=', 1) for line in content.splitlines() if '=' in line)
    lines.update({
        'PDF_PREMIUM_ENABLED':'true', 'PORT':'8000', 'PDF_DATA_DIR':'/app/var',
        'DATABASE_URL':'sqlite:////app/var/state.sqlite3',
        'PDF_RENDER_MODE':'html_template', 'PDF_PIPELINE_VERSION':'canonical_v1',
        'PDF_HTML_ENGINE':'weasyprint',
        'PDF_WORKER_TIMEOUT_SECONDS':'600', 'PDF_AI_DOCUMENT_BUDGET_USD':'0.15',
        'PDF_AI_DAILY_BUDGET_USD':'0.30', 'PDF_MEDIA_MODE':'search',
        'PDF_MEDIA_OPENAI_SEARCH_ENABLED':'true', 'PDF_MEDIA_SEARCH_RESERVE_USD':'0.05',
        'PDF_MEDIA_SEARCH_DAILY_BUDGET_USD':'0.10',
    })
    lines.pop('TESSERACT_CMD', None)
    lines.pop('TESSDATA_PREFIX', None)
    lines['PDF_API_TOKENS'] = lines['PDF_API_TOKENS'].strip("'")
    content = '\n'.join(f'{key}={value}' for key, value in lines.items()) + '\n'
with target.open('x', encoding='utf-8') as stream:
    stream.write(content)
try:
    target.chmod(0o600)
except OSError:
    pass
print('Created private .env. Keep it outside Git. Credentials were not printed.')
