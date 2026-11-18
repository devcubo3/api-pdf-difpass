"""One integrity gate for manual decisions and automatic validated delivery."""
import hashlib
import re

import pymupdf


class ReleaseBlocked(ValueError):
    pass


def verified_artifact(storage,artifacts,*,expected_sha=None):
    sha=artifacts.get('sha256')
    if not isinstance(sha,str) or not re.fullmatch(r'[0-9a-f]{64}',sha) or expected_sha is not None and sha!=expected_sha:
        raise ReleaseBlocked('Aprovação exige um artefato final conferido.')
    if not artifacts.get('checks',{}).get('validations_passed'):
        raise ReleaseBlocked('Há verificações bloqueantes; não é possível aprovar.')
    try:
        content=storage.read(artifacts['pdf_path'])
    except (OSError,KeyError,ValueError):
        raise ReleaseBlocked('Artefato indisponível.') from None
    if hashlib.sha256(content).hexdigest()!=sha:
        raise ReleaseBlocked('O artefato mudou; aprovação bloqueada.')
    return content


def automatic_release_event(settings,storage,row,data,artifacts,issues):
    # Missing snapshots mean the historical manual policy, never the new default.
    if settings.review_policy!='validated_auto' or data.get('release_policy','always')!='validated_auto':
        return None
    checks=artifacts.get('checks',{})
    if (settings.render_mode!='html_template' or data.get('pipeline_version')!='canonical_v1'
        or data.get('generation_scope')!='full' or checks.get('preview_only')
        or any(issue.get('blocking') for issue in issues) or not checks.get('validations_passed')):
        return None
    text=checks.get('template_text');layout=checks.get('template_layout')
    count=artifacts.get('page_count',0)
    if (not isinstance(count,int) or count<1 or count!=checks.get('planned_pages')
        or count!=checks.get('rendered_pages') or not text or not layout
        or len(text)!=count or len(layout)!=count
        or not all(item.get('passed') for item in text)
        or not all(item.get('passed') and item.get('header',{}).get('passed') for item in layout)
        or checks.get('code_verification_scope')!='all_pages' or not checks.get('codes_valid')
        or not checks.get('canonical',{}).get('evidence_validation_passed')
        or checks.get('source_blocks',0)<1 or checks.get('source_blocks')!=checks.get('composed_blocks')
        or checks.get('code_count')!=checks.get('source_code_count')
        or checks.get('blocking_issues')!=0):
        return None
    from .legacy_download import validate_legacy_url
    try:
        url=validate_legacy_url(settings,row['tenant_id'],data.get('legacy_pdf_url'))
        if not url or checks.get('original_download',{}).get('kind')!='legacy_pdf': return None
        content=verified_artifact(storage,artifacts)
        if hashlib.sha256(storage.read(row['source_path'])).hexdigest()!=row['source_sha']:
            raise ReleaseBlocked('O original mudou; liberação automática bloqueada.')
        with pymupdf.open(stream=content,filetype='pdf') as document:
            if len(document)!=count or document.embfile_count(): return None
            if sum(link.get('uri')==url for page in document for link in page.get_links())!=1: return None
    except (ValueError,OSError,KeyError):
        raise ReleaseBlocked('Não foi possível confirmar a integridade e o link de segurança.') from None
    return {'subject':'system:validated-auto','action':'automatic_release','revision':row['revision'],
            'details':{'policy':'validated_auto','artifact_sha256':artifacts['sha256'],
                       'source_sha256':row['source_sha'],'decision':'validated'}}
