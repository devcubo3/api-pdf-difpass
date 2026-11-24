"""Authenticated document API. Expensive work runs only in the separate worker."""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
from urllib.parse import urlencode
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text,select

from .config import Settings
from .db import ConcurrentUpdate, Database, IdempotencyConflict,ai_calls
from .extraction import InputError, validate_pdf
from .storage import Storage
from .media import MediaCatalog,PhotoSearch,choose_candidate
from .media_fetch import MediaError
from .media_context import primary_place
from .ai_budget import BudgetLedger
from .legacy_download import validate_legacy_url
from .document_schema import Evidence

PDF_LOCK = threading.RLock()


class BodyTooLarge(Exception):
    pass


class LimitBody:
    """Bound multipart bodies including chunked uploads, before parser spooling."""
    def __init__(self, app, max_bytes):
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        limit = self.max_bytes + 1024 * 1024
        consumed = 0

        async def limited_receive():
            nonlocal consumed
            message = await receive()
            consumed += len(message.get('body', b''))
            if consumed > limit:
                raise BodyTooLarge()
            return message

        try:
            await self.app(scope, limited_receive, send)
        except BodyTooLarge:
            await JSONResponse({'detail': 'Requisição acima do limite.'}, status_code=413)(scope, receive, send)


class Decision(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['approve', 'reject']
    revision: int = Field(ge=1)
    artifact_sha256: str | None = Field(default=None, pattern=r'^[0-9a-f]{64}$')
    reason: str | None = Field(default=None, min_length=1, max_length=2000)


class SummaryItem(BaseModel):
    model_config = ConfigDict(extra='forbid')
    label: str = Field(min_length=1, max_length=100)
    value: str = Field(min_length=1, max_length=500)
    source_page: int = Field(ge=1)


class Changes(BaseModel):
    model_config = ConfigDict(extra='forbid')
    title: str | None = Field(default=None, min_length=1, max_length=150)
    subtitle: str | None = Field(default=None, min_length=1, max_length=250)
    summary: list[SummaryItem] | None = Field(default=None, max_length=20)


class FactChange(BaseModel):
    model_config=ConfigDict(extra='forbid')
    fact_id: str = Field(min_length=1,max_length=80)
    raw_value: str = Field(min_length=1,max_length=4000)
    evidence: list[Evidence] = Field(min_length=1,max_length=20)
    service_id: str | None = None
    entity_id: str | None = None


class FactCorrection(BaseModel):
    model_config=ConfigDict(extra='forbid')
    changes: list[FactChange] = Field(min_length=1,max_length=100)
    reason: str = Field(min_length=1,max_length=2000)


class Correction(BaseModel):
    model_config = ConfigDict(extra='forbid')
    changes: Changes
    reason: str = Field(min_length=1, max_length=2000)


class Reprocess(BaseModel):
    model_config = ConfigDict(extra='forbid')
    reason: str = Field(min_length=1, max_length=2000)


class TemplateComparison(Reprocess):
    generation_scope: Literal['full','preview'] = 'preview'


class MediaSearchRequest(Reprocess):
    subject: str | None = Field(default=None,min_length=3,max_length=150)
    source_page_url: str | None = Field(default=None,max_length=2500)
    locality: str | None = Field(default=None,min_length=3,max_length=120)
    kind: Literal['hotel','attraction','destination'] | None = None
    provider: Literal['auto','official','brave','pexels','openai','serpapi'] = 'auto'
    allow_paid_search: bool = False
    auto_select: bool = False


class MediaSelection(Reprocess):
    asset_id: str = Field(pattern=r'^[0-9a-f]{64}$')
    permission_confirmed: bool = False


class MediaRefresh(Reprocess):
    allow_paid_search: bool = False


class AccessLink(BaseModel):
    model_config = ConfigDict(extra='forbid')
    artifact: Literal['pdf', 'review_pdf', 'preview', 'page', 'source'] = 'pdf'
    revision: int = Field(ge=1)
    page_number: int | None = Field(default=None, ge=1)
    ttl_seconds: int = Field(default=60, ge=30, le=300)
    disposition: Literal['inline', 'attachment'] = 'inline'


def etag(row):
    return f'"{row["etag"]}"'


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    db, storage = Database(settings), Storage(settings.data_dir)

    @asynccontextmanager
    async def lifespan(_app):
        db.create_schema()
        yield
        db.engine.dispose()

    api = FastAPI(title='Diff Travel Premium PDF API', version='2.0.0', lifespan=lifespan)
    api.state.settings, api.state.db, api.state.storage = settings, db, storage
    api.add_middleware(LimitBody, max_bytes=settings.max_upload_bytes)
    bearer = HTTPBearer(auto_error=False)

    def identity(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        if credentials and credentials.scheme.lower() == 'bearer':
            for token, principal in settings.api_tokens.items():
                if hmac.compare_digest(token.encode(), credentials.credentials.encode()):
                    return principal
        raise HTTPException(401, 'Autenticação necessária.', headers={'WWW-Authenticate': 'Bearer'})

    def require(role):
        def check(principal=Depends(identity)):
            if role not in principal['roles']:
                raise HTTPException(403, 'Permissão insuficiente.')
            return principal
        return check

    def document(document_id, principal):
        row = db.get(document_id, principal['tenant_id'])
        if row is None:
            raise HTTPException(404, 'Documento não encontrado.')
        if row['status'] == 'expired':
            raise HTTPException(410, 'Documento expirado.')
        return row

    def precondition(row, value):
        if value is None:
            raise HTTPException(428, 'Envie If-Match com o ETag da revisão.')
        if value not in (etag(row), str(row['etag'])):
            raise HTTPException(409, 'A revisão mudou. Recarregue antes de continuar.')

    def public(row):
        artifacts = row['artifacts_json']
        available = row['status'] == 'ready'
        return {'document_id': row['id'], 'status': row['status'], 'revision': row['revision'],
                'etag': etag(row), 'document_type': row['doc_type'], 'issues': row['issues_json'],
                'pipeline_version':row['data_json'].get('pipeline_version','legacy'),
                'processing_stage':'queued' if row['status']=='queued' else row['data_json'].get('processing_stage'),
                'created_at': row['created_at'], 'updated_at': row['updated_at'],
                'status_url': f'/v2/documents/{row["id"]}',
                'artifacts': {'premium_pdf': f'/v2/documents/{row["id"]}/pdf' if available else None,
                              'preview': f'/v2/documents/{row["id"]}/preview' if available else None,
                              'page_count': artifacts.get('page_count') if available else None},
                'approved_revisions': [int(k) for k, value in artifacts.get('history', {}).items()
                                       if value.get('approved')]}

    def update(row, **values):
        try:
            return db.update_document(row['id'], row['tenant_id'], row['etag'], **values)
        except ConcurrentUpdate:
            raise HTTPException(409, 'A revisão mudou durante a operação.') from None

    def private_file(path, media_type, name, disposition='inline', sha=None):
        try:
            target = storage._contained(Path(path))
            if not target.is_file():
                raise HTTPException(410, 'Arquivo indisponível.')
            if sha and hashlib.sha256(target.read_bytes()).hexdigest() != sha:
                raise HTTPException(409, 'Integridade do artefato não confere; consulte a operação.')
        except (ValueError, TypeError):
            raise HTTPException(410, 'Arquivo indisponível.') from None
        return FileResponse(target, media_type=media_type, filename=name,
                            content_disposition_type=disposition,
                            headers={'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff',
                                     'Referrer-Policy': 'no-referrer'})

    def selected_artifacts(row, revision, *, draft=False):
        if revision is not None and revision != row['revision']:
            archived = row['artifacts_json'].get('history', {}).get(str(revision), {})
            if not archived.get('approved'):
                raise HTTPException(404, 'Revisão aprovada não encontrada.')
            return archived['artifacts']
        if row['status'] != 'ready' and not (draft and row['status'] == 'needs_review'):
            raise HTTPException(409, 'Documento ainda não liberado.')
        if not row['artifacts_json'].get('pdf_path'):
            raise HTTPException(409, 'Artefato ainda indisponível.')
        return row['artifacts_json']

    def access_target(row, artifact, page_number):
        if artifact == 'source':
            return row['source_path'], row['source_sha'], 'application/pdf', 'original.pdf'
        artifacts = selected_artifacts(row, None, draft=artifact != 'pdf')
        if artifact in {'pdf', 'review_pdf'}:
            return artifacts['pdf_path'], artifacts['sha256'], 'application/pdf', f'diff-travel-r{row["revision"]}.pdf'
        number = page_number if artifact == 'page' else 1
        paths, hashes = artifacts.get('page_paths', []), artifacts.get('page_sha256', [])
        if number is None or number < 1 or number > len(paths) or number > len(hashes):
            raise HTTPException(404, 'Página verificada não encontrada.')
        return paths[number-1], hashes[number-1], 'image/png', f'page-{number}.png'

    def read_upload(upload):
        content = upload.file.read(settings.max_upload_bytes + 1)
        if len(content) > settings.max_upload_bytes:
            raise HTTPException(413, 'Arquivo acima do limite.')
        return content

    @api.get('/healthz')
    def health():
        return {'status': 'ok', 'version': '2.0.0', 'render_mode':settings.render_mode,
                'pipeline_version':settings.pipeline_version,'html_engine':settings.html_engine,
                'review_policy':settings.review_policy,'media_mode':settings.media_mode,
                'media_auto_use_policy':settings.media_auto_use_policy,
                'serpapi_configured':bool(settings.serpapi_api_key)}

    @api.get('/readyz')
    def ready():
        try:
            with db.engine.connect() as connection:
                connection.execute(text('SELECT 1'))
            check = storage.stage_upload(b'readiness')
            storage.delete_staged(check)
            if not (settings.brand_dir / 'wordmark-premium.png').is_file():
                raise ValueError('Missing brand')
            if settings.render_mode == 'openai_images' and not (settings.brand_dir/'style-cover.png').is_file():
                raise ValueError('Image brand reference missing')
            if settings.render_mode == 'html_template':
                from .html_rendering import TEMPLATE_DIR
                if not (TEMPLATE_DIR/'premium.html').is_file():
                    raise ValueError('HTML template missing')
        except Exception:
            raise HTTPException(503, 'Dependência indisponível.') from None
        return {'status': 'ready'}

    @api.post('/v2/documents', status_code=202)
    def upload(input_pdf: UploadFile = File(...), brand_id: str = Form('difftravel-premium-v1'),
               document_type: Literal['auto', 'travel_bundle', 'ticket', 'hotel', 'proposal','flight','transfer','car_rental','insurance','cruise'] = Form('auto'),
               external_document_id: str | None = Form(None), context: str = Form('{}'),
               generation_scope: Literal['full','preview'] = Form('full'),
               legacy_pdf_url: str | None = Form(None),
               idempotency_key: str | None = Header(None, alias='Idempotency-Key'),
               principal=Depends(require('upload'))):
        if not idempotency_key or len(idempotency_key) > 200:
            raise HTTPException(400, 'Idempotency-Key deve ter entre 1 e 200 caracteres.')
        if (settings.require_legacy_pdf_url or settings.review_policy=='validated_auto') and not legacy_pdf_url:
            raise HTTPException(422,'Informe a URL do PDF padrão para o botão de download.')
        try:
            legacy_pdf_url=validate_legacy_url(settings,principal['tenant_id'],legacy_pdf_url)
        except ValueError as exc:
            raise HTTPException(422,str(exc)) from None
        if legacy_pdf_url and settings.render_mode!='html_template':
            raise HTTPException(422,'O botão do PDF padrão exige o modo HTML.')
        if brand_id != 'difftravel-premium-v1':
            raise HTTPException(422, 'Pacote de marca não suportado.')
        if generation_scope == 'preview' and settings.render_mode not in {'openai_images','html_template'}:
            raise HTTPException(422, 'Prévia exige modo de imagens ou template HTML.')
        if external_document_id and len(external_document_id) > 500:
            raise HTTPException(422, 'Referência externa acima do limite.')
        if len(context) > 8192:
            raise HTTPException(422, 'Contexto acima do limite.')
        try:
            if json.loads(context) != {}:
                raise ValueError()
        except (ValueError, TypeError):
            raise HTTPException(422, 'Esta versão aceita apenas contexto vazio; a fonte determina os dados.') from None
        content = read_upload(input_pdf)
        staged = storage.stage_upload(content)
        try:
            with PDF_LOCK:
                validate_pdf(staged, settings.max_pages,allow_blank_pages=settings.pipeline_version=='canonical_v1')
            source_sha = hashlib.sha256(content).hexdigest()
            fingerprint = hashlib.sha256(json.dumps([source_sha, brand_id, document_type,
                external_document_id, 'local-evidence-v1', 'image-first-v1', settings.render_dpi,
                settings.render_mode,generation_scope,settings.pipeline_version,legacy_pdf_url],
                separators=(',', ':')).encode()).hexdigest()
            document_id = str(uuid4())
            source = storage.commit_upload(staged, document_id)
            try:
                row, created = db.enqueue(document_id, principal['tenant_id'], source_sha, source,
                    external_id=external_document_id, doc_type=document_type,
                    idempotency_key=idempotency_key, fingerprint=fingerprint,
                    initial_data={'generation_scope':generation_scope,'public_context_version':2,'editorial_version':2,
                                  'pipeline_version':settings.pipeline_version,'legacy_pdf_url':legacy_pdf_url,
                                  'release_policy':settings.review_policy,
                                  'requested_document_type':document_type})
            except IdempotencyConflict:
                source.unlink(missing_ok=True)
                raise HTTPException(409, 'Idempotency-Key já usada com outra entrada.') from None
            except Exception:
                source.unlink(missing_ok=True)
                raise
            if not created:
                source.unlink(missing_ok=True)
            return JSONResponse(public(row), status_code=202, headers={'ETag': etag(row)})
        except InputError as exc:
            raise HTTPException(400, str(exc)) from None
        finally:
            storage.delete_staged(staged)

    @api.get('/v2/documents/{document_id}')
    def status(document_id: str, principal=Depends(require('read'))):
        row = document(document_id, principal)
        return JSONResponse(public(row), headers={'ETag': etag(row), 'Cache-Control': 'no-store'})

    @api.get('/v2/documents/{document_id}/source')
    def source(document_id: str, principal=Depends(require('review'))):
        row = document(document_id, principal)
        return private_file(row['source_path'], 'application/pdf', 'original.pdf', sha=row['source_sha'])

    @api.get('/v2/documents/{document_id}/pdf')
    def pdf(document_id: str, revision: int | None = Query(None, ge=1),
            disposition: Literal['inline', 'attachment'] = 'inline', principal=Depends(require('read'))):
        row = document(document_id, principal)
        artifacts = selected_artifacts(row, revision)
        return private_file(artifacts['pdf_path'], 'application/pdf', f'diff-travel-r{revision or row["revision"]}.pdf',
                            disposition, artifacts['sha256'])

    @api.get('/v2/documents/{document_id}/preview')
    def preview(document_id: str, principal=Depends(require('read'))):
        row = document(document_id, principal)
        artifacts = selected_artifacts(row, None, draft='review' in principal['roles'])
        return private_file(artifacts['page_paths'][0], 'image/png', 'preview.png',
                            sha=(artifacts.get('page_sha256') or [None])[0])

    @api.get('/v2/documents/{document_id}/pages/{page_number}.png')
    def page(document_id: str, page_number: int, principal=Depends(require('read'))):
        row = document(document_id, principal)
        artifacts = selected_artifacts(row, None, draft='review' in principal['roles'])
        paths = artifacts['page_paths']
        if page_number < 1 or page_number > len(paths):
            raise HTTPException(404, 'Página não encontrada.')
        hashes = artifacts.get('page_sha256') or [None] * len(paths)
        return private_file(paths[page_number-1], 'image/png', f'page-{page_number}.png',
                            sha=hashes[page_number-1])

    @api.get('/v2/documents/{document_id}/review')
    def review(document_id: str, principal=Depends(require('review'))):
        row = document(document_id, principal)
        return JSONResponse({'document_id': document_id, 'revision': row['revision'], 'etag': etag(row),
            'artifact_sha256': row['artifacts_json'].get('sha256'), 'status': row['status'],
            'data': row['data_json'], 'issues': row['issues_json'],
            'checks': row['artifacts_json'].get('checks', {}), 'events': db.reviews(document_id, row['tenant_id'])},
            headers={'ETag': etag(row), 'Cache-Control': 'no-store'})

    @api.get('/v2/documents/{document_id}/review/generated-page/{page_number}.png')
    def generated_page(document_id: str,page_number: int,principal=Depends(require('review'))):
        row=document(document_id,principal)
        if not 1<=page_number<=settings.ai_max_pages:
            raise HTTPException(404,'Página gerada não encontrada.')
        model=row['data_json'].get('image_profile_requested',{}).get('model',settings.openai_image_model)
        stage=f'page-{page_number}'
        stage_keys=row['data_json'].get('ai_cached_calls')
        if stage_keys is not None and stage not in stage_keys:
            raise HTTPException(404,'Página gerada não encontrada nesta tentativa.')
        with db.engine.connect() as connection:
            query=select(ai_calls).where(ai_calls.c.document_id==document_id,
                ai_calls.c.tenant_id==principal['tenant_id'],ai_calls.c.revision==row['revision'],
                ai_calls.c.stage==stage,ai_calls.c.model==model,ai_calls.c.status=='completed')
            if stage_keys is not None:
                query=query.where(ai_calls.c.key==stage_keys[stage])
            call=connection.execute(query.order_by(ai_calls.c.created_at.desc()).limit(1)).mappings().first()
        if call is None:
            raise HTTPException(404,'Página gerada não encontrada.')
        path=storage.revision_dir(document_id,row['revision'])/'ai-cache'/f'{call["key"]}.png'
        return private_file(path,'image/png','imagem-gerada-para-revisao.png',sha=call['response_sha256'])

    @api.get('/v2/documents/{document_id}/review/pdf')
    def draft_pdf(document_id: str, principal=Depends(require('review'))):
        row = document(document_id, principal)
        artifacts = selected_artifacts(row, None, draft=True)
        return private_file(artifacts['pdf_path'], 'application/pdf', 'rascunho-revisao.pdf', sha=artifacts['sha256'])

    @api.post('/v2/documents/{document_id}/access-links', status_code=201)
    def issue_access_link(document_id: str, body: AccessLink,
                          if_match: str | None = Header(None, alias='If-Match'),
                          principal=Depends(require('read'))):
        row = document(document_id, principal)
        if body.artifact in {'source', 'review_pdf'} or row['status'] != 'ready':
            if 'review' not in principal['roles']:
                raise HTTPException(403, 'Permissão de revisão necessária.')
        precondition(row, if_match)
        if body.revision != row['revision']:
            raise HTTPException(409, 'A revisão mudou. Recarregue antes de continuar.')
        if (body.artifact == 'page') != (body.page_number is not None):
            raise HTTPException(422, 'page_number é obrigatório apenas para artifact=page.')
        path, sha, media_type, name = access_target(row, body.artifact, body.page_number)
        if not sha:
            raise HTTPException(409, 'Artefato sem verificação de integridade.')
        # Validate the existing bytes as well as the state before issuing a capability.
        private_file(path, media_type, name, sha=sha)
        token = secrets.token_urlsafe(32)
        expires_at = time.time() + body.ttl_seconds
        try:
            db.create_access_link(row, token_sha256=hashlib.sha256(token.encode()).hexdigest(),
                artifact=body.artifact, page_number=body.page_number, artifact_sha256=sha,
                disposition=body.disposition, expires_at=expires_at, subject=principal['subject'])
        except ConcurrentUpdate:
            raise HTTPException(409, 'A revisão mudou durante a operação.') from None
        return JSONResponse({'document_id': document_id, 'revision': row['revision'],
            'artifact': body.artifact, 'artifact_sha256': sha,
            'access_path': f'/v2/access/{token}',
            'expires_at': datetime.fromtimestamp(expires_at, timezone.utc).isoformat(),
            'expires_in': body.ttl_seconds}, status_code=201, headers={'Cache-Control': 'no-store'})

    @api.get('/v2/access/{token}', include_in_schema=False)
    def access_file(token: str):
        unavailable = HTTPException(404, 'Link indisponível ou expirado.',
            headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})
        if len(token) != 43 or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in token):
            raise unavailable
        grant = db.get_access_link(hashlib.sha256(token.encode()).hexdigest())
        if grant is None:
            raise unavailable
        row = db.get(grant['document_id'], grant['tenant_id'])
        if grant['artifact']=='original_download':
            if row is None or row['status']=='expired' or row['source_sha']!=grant['artifact_sha256']:
                raise unavailable
            # This capability shares only the immutable supplier source. Its access
            # is independent of the separate approval required for the premium PDF.
            return private_file(row['source_path'],'application/pdf','original.pdf','attachment',row['source_sha'])
        if row is None or row['status'] == 'expired' or row['revision'] != grant['revision'] or row['etag'] != grant['etag']:
            raise unavailable
        try:
            path, sha, media_type, name = access_target(row, grant['artifact'], grant['page_number'])
        except HTTPException:
            raise unavailable from None
        if sha != grant['artifact_sha256']:
            raise unavailable
        return private_file(path, media_type, name, grant['disposition'], sha)

    @api.post('/v2/documents/{document_id}/original-downloads/revoke')
    def revoke_original_downloads(document_id: str,if_match: str | None = Header(None,alias='If-Match'),
                                  principal=Depends(require('review'))):
        row=document(document_id,principal)
        precondition(row,if_match)
        try:
            count=db.revoke_original_downloads(row,principal['subject'])
        except ConcurrentUpdate:
            raise HTTPException(409,'A revisão mudou durante a operação.') from None
        return {'revoked':count}

    def new_revision(row, principal, reason, changes=None,*,reuse_understanding=False,fact_changes=None,action=None):
        if row['status'] not in {'needs_review', 'failed', 'ready'}:
            raise HTTPException(409, 'Aguarde o processamento terminar.')
        artifacts = row['artifacts_json']
        history = dict(artifacts.get('history', {}))
        if artifacts.get('pdf_path'):
            history[str(row['revision'])] = {'approved': row['status'] == 'ready',
                'artifacts': {k:v for k,v in artifacts.items() if k != 'history'}}
        data = dict(row['data_json'])
        data.pop('ai_cache_only',None)
        data.pop('ai_plan_cache_only',None)
        if data.get('pipeline_version')=='canonical_v1':
            if reuse_understanding or changes:
                data.update(ai_cache_only=True,ai_plan_cache_only=True)
            else:
                data.pop('understanding_snapshot',None)
        data['overrides'] = {**data.get('overrides', {}), **(changes or {})}
        return update(row, status='queued', revision=row['revision']+1, attempt_count=0,
            data_json=data, artifacts_json={'history': history}, issues_json=[],
            review_event={'subject':principal['subject'], 'action':action or ('correct_facts' if fact_changes else ('correct' if changes else 'reprocess')),
                          'revision':row['revision'], 'details':{'reason':reason, 'changes':fact_changes or changes or {}}})

    @api.patch('/v2/documents/{document_id}/review', status_code=202)
    def correct(document_id: str, body: Correction, if_match: str | None = Header(None, alias='If-Match'),
                principal=Depends(require('review'))):
        row = document(document_id, principal)
        precondition(row, if_match)
        changes = body.changes.model_dump(exclude_none=True)
        if not changes:
            raise HTTPException(422, 'Nenhuma correção informada.')
        for item in changes.get('summary', []):
            if item['source_page'] > row['data_json'].get('source_page_count', 0):
                raise HTTPException(422, 'Página de evidência inválida.')
        return public(new_revision(row, principal, body.reason, changes))

    @api.patch('/v2/documents/{document_id}/facts',status_code=202)
    def correct_facts(document_id: str,body: FactCorrection,
            if_match: str | None=Header(None,alias='If-Match'),principal=Depends(require('review'))):
        row=document(document_id,principal)
        precondition(row,if_match)
        if row['data_json'].get('pipeline_version')!='canonical_v1' or not row['data_json'].get('understanding_snapshot'):
            raise HTTPException(409,'Correção por campo exige leitura estruturada concluída.')
        from .document_schema import Understanding,Fact
        from .document_validation import validate_understanding
        current=Understanding.model_validate(row['data_json']['understanding'])
        facts={x.id:x for x in current.facts}
        if len({x.fact_id for x in body.changes})!=len(body.changes):
            raise HTTPException(422,'Campo duplicado na correção.')
        overrides=dict(row['data_json'].get('fact_overrides',{}))
        for change in body.changes:
            if change.fact_id not in facts:
                raise HTTPException(422,'Campo não encontrado nesta revisão.')
            values=change.model_dump(exclude_unset=True)
            values.pop('fact_id')
            updated=Fact.model_validate({**facts[change.fact_id].model_dump(),**values,'status':'supported'})
            facts[change.fact_id]=updated
            overrides[change.fact_id]={**values,'status':'supported'}
        current.facts=list(facts.values())
        evidence=row['data_json'].get('evidence',[])
        _,issues=validate_understanding(current,{'pages':evidence,'codes':row['data_json'].get('codes',[])})
        changed_ids={x.fact_id for x in body.changes}
        if any(x.get('field') in {'facts.'+k for k in changed_ids} for x in issues):
            raise HTTPException(422,'A correção deve corresponder à fonte e aos vínculos existentes.')
        # Feed server-validated overrides into a new audited revision. Raw paid
        # response is immutable; rebuilding uses its verified snapshot, no new call.
        shadow={**row,'data_json':{**row['data_json'],'fact_overrides':overrides}}
        return public(new_revision(shadow,principal,body.reason,reuse_understanding=True,
                                   fact_changes=[x.model_dump(exclude_unset=True) for x in body.changes]))

    @api.post('/v2/documents/{document_id}/reprocess', status_code=202)
    def reprocess(document_id: str, body: Reprocess, if_match: str | None = Header(None, alias='If-Match'),
                  principal=Depends(require('review'))):
        row = document(document_id, principal)
        precondition(row, if_match)
        return public(new_revision(row, principal, body.reason))

    @api.post('/v2/documents/{document_id}/continue',status_code=202)
    def continue_generation(document_id: str,body: Reprocess,
            if_match: str | None = Header(None,alias='If-Match'),principal=Depends(require('review'))):
        row=document(document_id,principal)
        precondition(row,if_match)
        checks=row['artifacts_json'].get('checks',{})
        text_checks=checks.get('template_text') if settings.render_mode=='html_template' else checks.get('image_text')
        if (settings.render_mode not in {'openai_images','html_template'} or row['status'] != 'needs_review'
                or row['data_json'].get('generation_scope') != 'preview'
                or checks.get('render_mode')!=settings.render_mode or not checks.get('preview_only')
                or not text_checks or not all(c.get('passed') for c in text_checks)):
            raise HTTPException(409,'Somente uma prévia concluída pode ter a geração continuada.')
        data={**row['data_json'],'generation_scope':'full',
              'ai_cache_only':settings.render_mode=='html_template','ai_plan_cache_only':True}
        # Same source/revision: verified cached plan/page are reused, never re-billed.
        return public(update(row,status='queued',attempt_count=0,data_json=data,issues_json=[],
            review_event={'subject':principal['subject'],'action':'continue_generation',
                          'revision':row['revision'],'details':{'reason':body.reason}}))

    def media_error(exc):
        code=404 if exc.code in {'MEDIA_NOT_FOUND','MEDIA_NO_MATCH'} else 422
        return HTTPException(code,{'code':exc.code,'message':exc.message})

    def selected_photo_state(row,asset_id,confirmed=False):
        MediaCatalog(settings,row['tenant_id']).load(asset_id)
        artifacts={**row['artifacts_json']}
        artifacts['checks']={**artifacts.get('checks',{}),'validations_passed':False}
        issues=[i for i in row['issues_json'] if i.get('code')!='MEDIA_REBUILD_REQUIRED']
        issues.append({'code':'MEDIA_REBUILD_REQUIRED','blocking':True,
            'message':'Foto alterada. Reconstruir o PDF para conferir o novo arquivo.'})
        artifacts['checks']['blocking_issues']=sum(bool(i.get('blocking')) for i in issues)
        return artifacts,issues,{'asset_id':asset_id,'permission_confirmed':confirmed}

    @api.post('/v2/documents/{document_id}/media/search')
    def media_search(document_id: str,body: MediaSearchRequest,
            if_match: str | None=Header(None,alias='If-Match'),principal=Depends(require('review'))):
        row=document(document_id,principal)
        precondition(row,if_match)
        if row['status']!='needs_review' or settings.render_mode!='html_template':
            raise HTTPException(409,'Busca de fotografia exige documento HTML em revisão.')
        place=primary_place(row['data_json']) or {}
        subject=body.subject or place.get('name')
        if not subject or row['doc_type']=='proposal':
            raise HTTPException(422,'Informe o nome público do hotel, destino ou atração.')
        try:
            result=PhotoSearch(settings,row['tenant_id'],db=db,row=row,allow_paid=body.allow_paid_search).search(subject,body.source_page_url,body.provider,
                locality=body.locality or (place.get('locality') if not body.subject else None),
                kind=body.kind or (place.get('kind') if not body.subject else None))
            data={**row['data_json'],'media_search':result}
            values={}
            if body.auto_select:
                candidate=choose_candidate(result,row['id'])
                artifacts,issues,selection=selected_photo_state(row,candidate['id'])
                data['media_selection']=selection
                values.update(artifacts_json=artifacts,issues_json=issues)
            changed=update(row,data_json=data,**values,review_event={'subject':principal['subject'],
                'action':'photo_search','revision':row['revision'],'details':{'reason':body.reason,
                    'provider':body.provider,'auto_select':body.auto_select,'candidates':len(result['candidates'])}})
        except MediaError as exc:
            raise media_error(exc) from None
        return JSONResponse({**result,'etag':etag(changed),
            'selected_asset_id':changed['data_json'].get('media_selection',{}).get('asset_id'),
            'candidates':[{**c,'preview_url':f'/v2/documents/{document_id}/media/{c["id"]}.jpg'}
                          for c in result['candidates']]},headers={'Cache-Control':'private, no-store','ETag':etag(changed)})

    @api.get('/v2/documents/{document_id}/media/context')
    def media_context(document_id: str,principal=Depends(require('review'))):
        row=document(document_id,principal)
        return JSONResponse({'context':row['data_json'].get('media_context'),
            'selection_policy':row['data_json'].get('media_context_policy'),
            'primary_place':primary_place(row['data_json']),
            'ai_usage':BudgetLedger(db,row).summary(),
            'search_usage':[call for call in BudgetLedger(db,row).summary() if call['stage']=='media_search']},
            headers={'Cache-Control':'private, no-store'})

    @api.get('/v2/documents/{document_id}/media/{asset_id}.jpg')
    def media_preview(document_id: str,asset_id: str,principal=Depends(require('review'))):
        row=document(document_id,principal)
        allowed={c['id'] for c in row['data_json'].get('media_search',{}).get('candidates',[])}
        allowed.add(row['data_json'].get('media_selection',{}).get('asset_id'))
        if asset_id not in allowed:
            raise HTTPException(404,'Foto não encontrada neste documento.')
        try:
            asset=MediaCatalog(settings,row['tenant_id']).load(asset_id)
        except MediaError as exc:
            raise media_error(exc) from None
        return private_file(asset['path'],'image/jpeg','foto.jpg',sha=asset['sha256'])

    @api.get('/v2/documents/{document_id}/media/maps')
    def media_maps_reference(document_id: str,principal=Depends(require('review'))):
        row=document(document_id,principal)
        subject=(row['data_json'].get('media_search',{}).get('entity')
                 or row['data_json'].get('media_search',{}).get('subject') or row['data_json'].get('title'))
        if not subject or row['doc_type']=='proposal':
            raise HTTPException(422,'Documento sem contexto público de destino/estabelecimento.')
        return JSONResponse({'subject':subject,'url':'https://www.google.com/maps/search/?'+urlencode({'api':1,'query':subject}),
            'purpose':'location_and_photo_reference','photo_import_enabled':False,
            'reason':'Importação de fotos Places para PDF permanente depende de configuração e validação de uso; nenhum conteúdo Google é armazenado.'},
            headers={'Cache-Control':'private, no-store'})

    @api.post('/v2/documents/{document_id}/media/refresh',status_code=202)
    def refresh_media(document_id: str,body: MediaRefresh,
            if_match: str | None=Header(None,alias='If-Match'),principal=Depends(require('review'))):
        row=document(document_id,principal)
        precondition(row,if_match)
        if (settings.render_mode!='html_template' or settings.media_mode!='search'
                or settings.media_auto_use_policy!='matched_sources'
                or row['data_json'].get('pipeline_version')!='canonical_v1'
                or not row['data_json'].get('understanding_snapshot')):
            raise HTTPException(409,'Atualização de foto exige busca automática configurada e leitura salva.')
        data={**row['data_json'],'media_refresh_allow_paid':body.allow_paid_search}
        for key in ('media_selection','photo_provenance','media_decision','media_search'):
            data.pop(key,None)
        changed=new_revision({**row,'data_json':data},principal,body.reason,
                             reuse_understanding=True,action='refresh_media')
        return JSONResponse(public(changed),status_code=202,headers={'ETag':etag(changed)})

    @api.post('/v2/documents/{document_id}/media/select')
    def media_select(document_id: str,body: MediaSelection,
            if_match: str | None=Header(None,alias='If-Match'),principal=Depends(require('review'))):
        row=document(document_id,principal)
        precondition(row,if_match)
        if row['status']!='needs_review' or settings.render_mode!='html_template':
            raise HTTPException(409,'Seleção de fotografia exige documento HTML em revisão.')
        if body.asset_id not in {c['id'] for c in row['data_json'].get('media_search',{}).get('candidates',[])}:
            raise HTTPException(404,'Foto não encontrada neste documento.')
        try:
            artifacts,issues,selection=selected_photo_state(row,body.asset_id,body.permission_confirmed)
        except MediaError as exc:
            raise media_error(exc) from None
        changed=update(row,data_json={**row['data_json'],'media_selection':selection},
            artifacts_json=artifacts,issues_json=issues,
            review_event={'subject':principal['subject'],'action':'photo_select','revision':row['revision'],
                'details':{'reason':body.reason,'asset_id':body.asset_id,'permission_confirmed':body.permission_confirmed}})
        return JSONResponse(public(changed),headers={'ETag':etag(changed)})

    @api.post('/v2/documents/{document_id}/template',status_code=202)
    def compare_template(document_id: str,body: TemplateComparison,
            if_match: str | None = Header(None,alias='If-Match'),principal=Depends(require('review'))):
        row=document(document_id,principal)
        precondition(row,if_match)
        if (settings.render_mode!='html_template' or row['status']!='needs_review'
                or not row['data_json'].get('presentation_plan')
                or not row['data_json'].get('ai_cached_calls',{}).get('plan')):
            raise HTTPException(409,'Comparação HTML exige um planejamento existente em revisão.')
        # Preserve the source/revision/cache. A missing or changed plan can never trigger a paid call.
        return public(update(row,status='queued',attempt_count=0,issues_json=[],
            data_json={**row['data_json'],'generation_scope':body.generation_scope,
                       'ai_cache_only':True,'ai_plan_cache_only':True},
            review_event={'subject':principal['subject'],'action':'compare_html_template',
                'revision':row['revision'],'details':{'reason':body.reason,'scope':body.generation_scope}}))

    @api.post('/v2/documents/{document_id}/retry-cache',status_code=202)
    def retry_cache(document_id: str,body: Reprocess,
            if_match: str | None = Header(None,alias='If-Match'),principal=Depends(require('review'))):
        row=document(document_id,principal)
        precondition(row,if_match)
        complete_plan=any(c['stage']=='plan' and c['status']=='completed' for c in BudgetLedger(db,row).summary())
        if settings.render_mode!='html_template' or row['status'] not in {'failed','needs_review'} or not complete_plan:
            raise HTTPException(409,'Retomada exige planejamento concluído em cache e documento HTML interrompido/em revisão.')
        return public(update(row,status='queued',attempt_count=0,issues_json=[],
            data_json={**row['data_json'],'ai_cache_only':True,'ai_plan_cache_only':True},
            review_event={'subject':principal['subject'],'action':'retry_from_cache',
                          'revision':row['revision'],'details':{'reason':body.reason}}))

    @api.post('/v2/documents/{document_id}/preview/variant',status_code=202)
    def preview_variant(document_id: str,body: Reprocess,
            if_match: str | None = Header(None,alias='If-Match'),principal=Depends(require('review'))):
        row=document(document_id,principal)
        precondition(row,if_match)
        if (settings.render_mode!='openai_images' or row['status']!='needs_review'
                or row['data_json'].get('generation_scope')!='preview'
                or row['artifacts_json'].get('checks',{}).get('render_mode')!='openai_images'):
            raise HTTPException(409,'Comparação de perfil exige uma prévia existente.')
        return public(update(row,status='queued',attempt_count=0,issues_json=[],
            data_json={**row['data_json'],'ai_cache_only':False,'ai_plan_cache_only':True},
            review_event={'subject':principal['subject'],'action':'preview_image_variant',
                'revision':row['revision'],'details':{'reason':body.reason,'model':settings.openai_image_model,
                    'quality':settings.openai_image_quality,'size':settings.openai_image_size}}))

    @api.post('/v2/documents/{document_id}/preview/rebuild',status_code=202)
    def rebuild_preview(document_id: str,body: Reprocess,
            if_match: str | None = Header(None,alias='If-Match'),principal=Depends(require('review'))):
        row=document(document_id,principal)
        precondition(row,if_match)
        if (settings.render_mode!='openai_images' or row['status']!='needs_review'
                or row['data_json'].get('generation_scope')!='preview'
                or not row['artifacts_json'].get('checks',{}).get('preview_only')):
            raise HTTPException(409,'Reconstrução sem cobrança exige uma prévia existente.')
        return public(update(row,status='queued',attempt_count=0,issues_json=[],
            data_json={**row['data_json'],'ai_cache_only':True},
            review_event={'subject':principal['subject'],'action':'rebuild_preview_from_cache',
                          'revision':row['revision'],'details':{'reason':body.reason}}))

    @api.post('/v2/documents/{document_id}/review/decision')
    def decide(document_id: str, body: Decision, if_match: str | None = Header(None, alias='If-Match'),
               principal=Depends(require('review'))):
        row = document(document_id, principal)
        precondition(row, if_match)
        artifacts = row['artifacts_json']
        if row['status'] != 'needs_review' or body.revision != row['revision'] or body.artifact_sha256 != artifacts.get('sha256'):
            raise HTTPException(409, 'Decisão não corresponde ao rascunho atual.')
        if body.action == 'approve':
            if not body.artifact_sha256:
                raise HTTPException(409,'Aprovação exige um artefato final conferido.')
            from .release import verified_artifact,ReleaseBlocked
            try:
                verified_artifact(storage,artifacts,expected_sha=body.artifact_sha256)
            except ReleaseBlocked as error:
                raise HTTPException(409,str(error)) from None
        elif not body.reason or not body.reason.strip():
            raise HTTPException(422, 'Informe o motivo da rejeição.')
        result = update(row, status='ready' if body.action == 'approve' else 'failed',
            issues_json=row['issues_json'] if body.action == 'approve' else
                [{'code':'REVIEW_REJECTED','blocking':True,'message':'Revisão rejeitada pela operação.'}],
            review_event={'subject':principal['subject'], 'action':body.action, 'revision':body.revision,
                          'details':{'reason':body.reason, 'artifact_sha256':body.artifact_sha256}})
        return JSONResponse(public(result), headers={'ETag': etag(result)})

    return api


app = create_app()
