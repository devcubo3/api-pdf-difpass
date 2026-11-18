"""One claimed job per process, with immutable attempt directories and CAS publication."""
from __future__ import annotations
import hashlib
import io
import json
import logging
from pathlib import Path
from uuid import uuid4

import pymupdf as fitz
import zxingcpp
from PIL import Image

from .config import Settings
from .db import Database, ConcurrentUpdate
from .storage import Storage
from .extraction import extract_document, pixel_hash
from .rendering import render_document
from .ai_images import ImagePipeline, AIError
from .ai_budget import BudgetLedger
from .html_rendering import render_html_document
from .media import MediaCatalog,PhotoSearch,choose_candidate
from .media_fetch import MediaError
from .media_context import grounded_context,legacy_context,primary_place
from .original_download import issue_original_download
from .diagnostics import safe_error

log = logging.getLogger(__name__)


def verify_artifacts(rendered: dict, codes: list[dict]) -> dict:
    pdf = Path(rendered['pdf_path'])
    results = []
    with fitz.open(pdf) as doc:
        if doc.embfile_count():
            raise ValueError('Unexpected PDF attachment')
        image_hashes = set()
        for page in doc:
            for image_info in page.get_images():
                # Page rasters are large; codes are verified independently.
                if image_info[2] * image_info[3] <= 4_000_000:
                    image = Image.open(io.BytesIO(doc.extract_image(image_info[0])['image']))
                    image_hashes.add(pixel_hash(image))
        for code in codes:
            placement = next(p for p in rendered['code_placements'] if p['id'] == code['id'])
            page = doc[placement['page']-1]
            box = fitz.Rect(placement['bbox'])
            box = fitz.Rect(box.x0-5, box.y0-5, box.x1+5, box.y1+5) & page.rect
            raster = Image.open(io.BytesIO(page.get_pixmap(clip=box, dpi=240, alpha=False).tobytes('png')))
            decoded = {result.text for result in zxingcpp.read_barcodes(raster)}
            results.append({'id': code['id'], 'source_page': code['source_page'],
                            'output_page': placement['page'],
                            'original_pixels_preserved': code['pixel_sha256'] in image_hashes,
                            'payload_matches': set(code['payloads']).issubset(decoded),
                            'preservation_mode': code['preservation_mode']})
    return {'codes': results, 'code_count': len(codes),
            'codes_valid': all(r['original_pixels_preserved'] and r['payload_matches'] for r in results),
            'original_pages_embedded': False, 'attachments': 0,
            'pdf_sha256': hashlib.sha256(pdf.read_bytes()).hexdigest()}


def process_claim(settings: Settings, row: dict) -> None:
    db = Database(settings)
    store = Storage(settings.data_dir)
    # The attempt suffix prevents a worker whose lease expired from changing
    # files belonging to the winner, even when its publication CAS fails.
    attempt_dir = store.revision_dir(row['id'], row['revision']) / f'attempt-{uuid4().hex}'
    attempt_dir.mkdir()
    stage='integrity'
    canonical=row['data_json'].get('pipeline_version')=='canonical_v1'
    data=dict(row['data_json'])
    def checkpoint(next_stage):
        nonlocal row,stage
        stage=next_stage
        data['processing_stage']=stage
        if 'image_pipeline' in locals():
            data['ai_cached_calls']=image_pipeline.stage_keys
        if canonical:
            row=db.update_document(row['id'],row['tenant_id'],row['etag'],
                status='processing',data_json=dict(data))
    try:
        source = Path(row['source_path'])
        if hashlib.sha256(store.read(source)).hexdigest() != row['source_sha']:
            raise ValueError('Source integrity mismatch')
        stage='extraction'
        if canonical and settings.render_mode!='html_template':
            raise AIError('PIPELINE_RUNTIME_MISMATCH','O pipeline canonical_v1 exige o modo HTML.')
        extracted = extract_document(source, attempt_dir / 'codes', row['doc_type'],generic=canonical)
        data = extracted['data']
        overrides = row['data_json'].get('overrides', {})
        data.update(overrides)
        data['overrides'] = overrides
        data['generation_scope']=row['data_json'].get('generation_scope','full')
        for key in ('pipeline_version','legacy_pdf_url','understanding_snapshot','fact_overrides','requested_document_type',
                    'ai_cache_only','ai_plan_cache_only','ai_cached_calls','release_policy'):
            if key in row['data_json']:
                data[key]=row['data_json'][key]
        data['evidence']=extracted['pages']
        data['codes']=[{k:v for k,v in c.items() if k!='path'} for c in extracted['codes']]
        if row['data_json'].get('public_context_version'):
            data['public_context_version']=row['data_json']['public_context_version']
        if row['data_json'].get('editorial_version'):
            data['editorial_version']=row['data_json']['editorial_version']
        media_asset=None
        selection=row['data_json'].get('media_selection')
        automatic=data.get('release_policy','always')=='validated_auto'
        if selection:
            data['media_selection']=selection
        if row['data_json'].get('media_search'):
            data['media_search']=row['data_json']['media_search']
        if not data['sections'] and not extracted['codes']:
            data['evidence'] = extracted['pages']
            db.update_document(row['id'], row['tenant_id'], row['etag'], status='needs_review',
                               data_json=data, issues_json=extracted['issues'],
                               artifacts_json={'history':row['artifacts_json'].get('history', {}),
                                               'checks':{'validations_passed':False}})
            return
        if settings.render_mode in {'openai_images','html_template'}:
            checkpoint('understanding' if canonical else 'planning')
            if settings.render_mode == 'openai_images':
                data['image_profile_requested']={'model':settings.openai_image_model,
                    'quality':settings.openai_image_quality,'size':settings.openai_image_size}
            image_pipeline=ImagePipeline(settings, db, row,cache_only=bool(row['data_json'].get('ai_cache_only')),
                plan_cache_only=bool(row['data_json'].get('ai_plan_cache_only')))
            if settings.render_mode == 'html_template':
                if canonical:
                    from .understanding import understand
                    from .document_schema import Fact
                    from .document_validation import validate_understanding
                    from .presentation import compile_presentation,public_context
                    document=understand(image_pipeline,extracted,source)
                    overrides=data.get('fact_overrides',{})
                    document.facts=[Fact.model_validate({**f.model_dump(),**overrides.get(f.id,{})}) for f in document.facts]
                    data['understanding']=document.model_dump()
                    data['understanding_snapshot']=image_pipeline.understanding_snapshot
                    data['ai_cached_calls']=image_pipeline.stage_keys
                    checkpoint('validation')
                    validation,canonical_issues=validate_understanding(document,extracted)
                    data['canonical_validation']=validation
                    extracted['issues'].extend(canonical_issues)
                    kinds={s.kind for s in document.services}
                    data['document_type']=next(iter(kinds)) if len(kinds)==1 else ('travel_bundle' if kinds else 'unknown')
                    if data.get('requested_document_type','auto') not in {'auto',data['document_type']}:
                        extracted['issues'].append({'code':'TYPE_MISMATCH','blocking':True,
                            'message':'O tipo solicitado exige conferência com os serviços identificados.'})
                    plan,coverage=compile_presentation(document,extracted,canonical_issues)
                    data['media_context']=grounded_context(public_context(document,canonical_issues),extracted['pages']) or {
                        'places':[],'primary_index':None,'version':2}
                    from .itinerary_media import PHOTO_CONTEXT_POLICY
                    data['media_context_policy']=PHOTO_CONTEXT_POLICY
                    data['presentation_coverage']=coverage
                else:
                    plan,coverage=image_pipeline.plan(extracted,source)
                data['presentation_plan']=plan.model_dump()
                if canonical:
                    pass
                elif row['data_json'].get('public_context_version')==2:
                    context=getattr(plan,'media_context',None)
                    data['media_context']=grounded_context(context.model_dump() if context else {},extracted['pages']) or {
                        'places':[],'primary_index':None,'version':2}
                else:
                    data['media_context']=legacy_context(data)
                checkpoint('media')
                if automatic and not (selection and selection.get('permission_confirmed')):
                    # Do not pay to discover a photo that cannot be delivered.
                    # Approval is scoped by tenant and an exact grounded place.
                    selection=None
                    data.pop('media_selection',None)
                    data.pop('photo_provenance',None)
                    try:
                        approved=MediaCatalog(settings,row['tenant_id']).preapproved(primary_place(data))
                        if approved:
                            selection={'asset_id':approved['id'],'permission_confirmed':True,
                                       'approval_scope':'configured_catalog'}
                            data['media_selection']=selection
                    except MediaError:
                        selection=None
                    if not selection:
                        data['hero_asset']='premium-institutional.svg'
                        extracted['issues'].append({'code':'MEDIA_INSTITUTIONAL','blocking':False,
                            'message':'Arte institucional utilizada; nenhuma busca paga de foto foi solicitada.'})
                if not automatic and not selection and settings.media_mode=='search' and data['document_type']!='proposal':
                    try:
                        place=primary_place(data)
                        if not place:
                            raise MediaError('MEDIA_CONTEXT_REVIEW','Local ausente ou ambíguo; confirmar o contexto público antes da busca.')
                        search=PhotoSearch(settings,row['tenant_id'],db=db,row=row,
                            allow_paid=not bool(row['data_json'].get('ai_cache_only'))).search(place['name'],
                                locality=place.get('locality'),kind=place.get('kind'))
                        data['media_search']=search
                        candidate=choose_candidate(search,row['id'])
                        selection={'asset_id':candidate['id'],'permission_confirmed':False}
                        data['media_selection']=selection
                    except MediaError as exc:
                        extracted['issues'].append({'code':exc.code,'blocking':False,'message':exc.message})
                        data['hero_asset']=None  # Never present an AI scene as a found photograph.
                    except Exception as exc:
                        diagnostic=safe_error(exc,'media')
                        log.error('Optional media failed document=%s diagnostic=%s',row['id'],diagnostic)
                        data['media_diagnostic']=diagnostic
                        data['hero_asset']=None
                        extracted['issues'].append({'code':'MEDIA_ENRICHMENT_FAILED','blocking':False,
                            'message':'Fotografia indisponível; versão institucional utilizada.'})
                if selection:
                    try:
                        media_asset=MediaCatalog(settings,row['tenant_id']).load(selection['asset_id'])
                        data['photo_provenance']={**MediaCatalog.public(media_asset),
                            'permission_confirmed':bool(selection.get('permission_confirmed'))}
                    except MediaError as exc:
                        if not canonical: raise
                        selection=None; media_asset=None; data['hero_asset']='premium-institutional.svg' if automatic else None
                        data.pop('media_selection',None); data.pop('photo_provenance',None)
                        extracted['issues'].append({'code':exc.code,'blocking':False,
                            'message':'Foto selecionada indisponível; versão institucional utilizada.'})
                    except Exception as exc:
                        if not canonical: raise
                        diagnostic=safe_error(exc,'media')
                        log.error('Optional selected media failed document=%s diagnostic=%s',row['id'],diagnostic)
                        data['media_diagnostic']=diagnostic
                        selection=None; media_asset=None; data['hero_asset']='premium-institutional.svg' if automatic else None
                        data.pop('media_selection',None); data.pop('photo_provenance',None)
                        extracted['issues'].append({'code':'MEDIA_ENRICHMENT_FAILED','blocking':False,
                            'message':'Foto selecionada indisponível; versão institucional utilizada.'})
                checkpoint('rendering')
                original_download=(issue_original_download(settings,db,row) if data['generation_scope']=='full' else None)
                rendered=render_html_document(settings,plan,coverage,extracted,attempt_dir/'rendered',
                    image_pipeline,scope=data['generation_scope'],media_asset=media_asset,original_download=original_download)
                if data['generation_scope']=='full' and not original_download:
                    extracted['issues'].append({'code':'ORIGINAL_LINK_NOT_CONFIGURED','blocking':True,
                        'message':'Configurar PDF_PUBLIC_BASE_URL e reconstruir para incluir o download do original.'})
            else:
                rendered=image_pipeline.render(extracted,source,attempt_dir/'rendered',scope=data['generation_scope'])
            data['ai_cached_calls']=image_pipeline.stage_keys
            data['presentation_plan'] = rendered['content_plan']
        else:
            rendered = render_document(data, extracted['codes'], attempt_dir / 'rendered', settings.brand_dir,
                                       dpi=settings.render_dpi)
            rendered['render_mode'] = 'local'
        preview=rendered.get('generation_scope')=='preview'
        checked_codes=extracted['codes']
        if preview:
            placed={p['id'] for p in rendered['code_placements']}
            checked_codes=[c for c in extracted['codes'] if c['id'] in placed]
        stage='artifact_validation'
        checks = verify_artifacts(rendered, checked_codes)
        issues = extracted['issues']
        checks['render_mode'] = rendered['render_mode']
        if settings.render_mode in {'openai_images','html_template'}:
            checks['content_plan'] = rendered['content_plan_checks']
            if canonical:
                checks['canonical']=data['canonical_validation']
                checks['pipeline_version']='canonical_v1'
            checks['ai_usage'] = rendered['ai_usage']
            checks['ai_cache_hits']=rendered['ai_cache_hits']
            if settings.render_mode == 'openai_images':
                checks['image_text'] = rendered['image_text_checks']
                checks['image_profile']=rendered['image_profile']
                if not all(c['passed'] for c in rendered['image_text_checks']):
                    issues.append({'code':'AI_IMAGE_TEXT_MISMATCH', 'blocking':True,
                        'message':'A leitura da imagem não confirmou todo o texto planejado. Não aprovar.'})
            else:
                checks['html_engine']=rendered['html_engine']
                checks['template_text']=rendered['template_text_checks']
                checks['template_layout']=rendered['template_layout_checks']
                checks['template_version']=rendered['template_version']
                checks['template_sha256']=rendered['template_sha256']
                checks['template_adjustments']=rendered.get('template_adjustments',[])
                checks['image_generation_performed']=False
                checks['original_download']=rendered.get('original_download',{'included':False})
                if media_asset:
                    checks['photo']={k:data['photo_provenance'][k] for k in ('id','sha256','provider',
                        'source_page_url','publisher','authority','rights_status','permission_confirmed')}
                    if not selection.get('permission_confirmed'):
                        issues.append({'code':'PHOTO_USAGE_REVIEW','blocking':True,
                            'message':'Fotografia disponível em rascunho. Confirmar autorização/licença de uso antes de liberar ao cliente.'})
            checks['preview_only']=preview
            checks['planned_pages']=rendered['planned_page_count']
            checks['rendered_pages']=rendered['page_count']
            checks['source_code_count']=len(extracted['codes'])
            checks['code_verification_scope']='preview_pages' if preview else 'all_pages'
            if preview:
                issues.append({'code':'PREVIEW_ONLY','blocking':True,
                    'message':'Prévia de uma página. Não é o documento completo e não pode ser aprovada.'})
            if rendered['content_plan']['omitted_blocks']:
                issues.append({'code':'AI_NOISE_REVIEW', 'blocking':False,
                    'message':'Conferir os rodapés/ruídos classificados no plano de apresentação.'})
        if not checks['codes_valid']:
            issues.append({'code': 'CODE_VERIFICATION_FAILED', 'blocking': True,
                           'message': 'A conferência do código no PDF final falhou.'})
        if automatic and any(call['status']!='completed' for call in BudgetLedger(db,row).summary()):
            issues.append({'code':'AI_CALL_UNCERTAIN','blocking':True,
                           'message':'Há uma chamada de IA não concluída; conferir antes de liberar.'})
        checks['blocking_issues'] = sum(bool(i.get('blocking')) for i in issues)
        checks['source_blocks'] = sum(len(p['blocks']) for p in extracted['pages'])
        checks['composed_blocks'] = (sum(len(s.get('sources', [s])) for s in data['sections'])
                                     + sum(len(c.get('detail_sources', [])) for c in extracted['codes']))
        checks['unique_composed_blocks'] = len(data['sections'])
        checks['validations_passed'] = (checks['codes_valid'] and not checks['blocking_issues']
                                        and checks['source_blocks'] == checks['composed_blocks'])
        # Source evidence remains private and is never embedded in the PDF.
        data['evidence'] = extracted['pages']
        data['codes'] = [{k:v for k,v in c.items() if k != 'path'} for c in extracted['codes']]
        history = dict(row['artifacts_json'].get('history', {}))
        artifacts = {**rendered, 'checks': checks, 'sha256': checks['pdf_sha256'], 'history': history}
        artifacts['page_sha256'] = [hashlib.sha256(Path(p).read_bytes()).hexdigest()
                                    for p in rendered['page_paths']]
        from .release import automatic_release_event,ReleaseBlocked
        try:
            event=automatic_release_event(settings,store,row,data,artifacts,issues)
        except ReleaseBlocked:
            event=None
            issues.append({'code':'AUTO_RELEASE_INTEGRITY','blocking':True,
                           'message':'A integridade final exige conferência; entrega automática bloqueada.'})
            checks['blocking_issues']+=1
            checks['validations_passed']=False
        data['processing_stage']='ready' if event else 'review'
        db.update_document(row['id'], row['tenant_id'], row['etag'], status='ready' if event else 'needs_review',
                           data_json=data, artifacts_json=artifacts, issues_json=issues,
                           doc_type=data['document_type'],review_event=event)
    except (AIError,MediaError) as exc:
        try:
            failure_data=locals().get('data', {})
            failure_data['processing_stage']=stage
            if 'image_pipeline' in locals():
                failure_data['ai_cached_calls']=image_pipeline.stage_keys
            if 'extracted' in locals():
                failure_data['evidence']=extracted['pages']
            db.update_document(row['id'], row['tenant_id'], row['etag'], status='needs_review',
                data_json=failure_data,
                issues_json=[{'code':exc.code,'blocking':True,'message':exc.message}],
                artifacts_json={'history':row['artifacts_json'].get('history',{}),
                                'checks':{'validations_passed':False,'render_mode':settings.render_mode,
                                          'ai_usage':BudgetLedger(db,row).summary()}})
        except ConcurrentUpdate:
            pass
    except ConcurrentUpdate:
        log.info('Discarded stale processing attempt document=%s', row['id'])
    except Exception as exc:
        diagnostic=safe_error(exc,stage)
        log.error('Processing failed document=%s diagnostic=%s', row['id'], diagnostic)
        try:
            failure_data=locals().get('data',row['data_json'])
            failure_data['diagnostic']=diagnostic
            failure_data['processing_stage']=stage
            if 'image_pipeline' in locals():
                failure_data['ai_cached_calls']=image_pipeline.stage_keys
            db.update_document(row['id'], row['tenant_id'], row['etag'], status='failed',
                               data_json=failure_data,
                               artifacts_json={'history':row['artifacts_json'].get('history',{}),
                                    'checks':{'validations_passed':False,'render_mode':settings.render_mode,
                                              'ai_usage':BudgetLedger(db,row).summary()}},
                               issues_json=[{'code':'PROCESSING_FAILED','blocking':True,
                                             'message':'Falha no processamento. Consulte a operação.'}])
        except ConcurrentUpdate:
            pass
    finally:
        db.engine.dispose()


def process_next(settings: Settings) -> bool:
    """Synchronous helper for tests/local one-shot runs; worker adds a hard timeout."""
    db = Database(settings)
    try:
        db.create_schema()
        row = db.claim_next()
    finally:
        db.engine.dispose()
    if row is None:
        return False
    process_claim(settings, row)
    return True
